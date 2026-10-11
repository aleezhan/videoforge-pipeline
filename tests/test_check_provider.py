"""Контролёр кадров — отдельный слот провайдеров (`features/vision_check_provider`).

Один llm на текст и проверки не давал проверять кадры при
VF_LLM_PROVIDER=claude_cli: текст на подписке за $0, а картинки он в продакшене
не смотрит — стадии images/video шли с VF_VISION_CHECK=off, и кадры не
проверял никто (Test 3, 08.09.2026). Теперь текст идёт в `llm`, проверки — в
`check` (VF_CHECK_PROVIDER); без него это один и тот же провайдер.

Контролёр живёт на бесплатном тире Gemini (15 RPM / 500 RPD), поэтому у
провайдера темп и повтор на 429/503; отказ модели смотреть (block_reason) —
не вердикт, а «проверка не состоялась».
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from videoforge.cli import controller_status
from videoforge.config import ProviderNames
from videoforge.core.manifest import load_manifest
from videoforge.core.project import ProjectSpec, create_episode, create_project
from videoforge.core.runner import required_providers, run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard, save_storyboard
from videoforge.providers import gemini
from videoforge.providers.base import CheckResult, ProviderError, ProviderSet, Reference
from videoforge.providers.mock import MockLlm
from videoforge.providers.registry import build_providers, missing_keys

# ------------------------------------------------------------- слот check


def test_check_defaults_to_llm_and_follows_a_swapped_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без VF_CHECK_PROVIDER контролёр — тот же провайдер, что llm. Это
    умолчание конфигурации, а не копия на момент сборки: набор подменяют по
    частям (`providers.llm = Picky()`), и проверка обязана уйти к подменённому."""
    monkeypatch.delenv("VF_CHECK_PROVIDER", raising=False)
    names = ProviderNames.from_env()
    assert names.check == ""
    assert names.as_dict()["check"] == names.llm

    providers = build_providers(ProviderNames.mock())
    assert providers.checker is None
    assert providers.check is providers.llm
    other = MockLlm()
    providers.llm = other
    assert providers.check is other
    assert providers.names()["check"] == "mock"


def test_a_named_check_provider_is_its_own_instance_with_its_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VF_LLM_PROVIDER", "claude_cli")
    monkeypatch.setenv("VF_CHECK_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    names = ProviderNames.from_env()
    assert names.as_dict()["llm"] == "claude_cli" and names.as_dict()["check"] == "gemini"
    # ключ спрашивается у контролёра, хотя llm ключа не требует
    assert missing_keys([names.as_dict()[slot] for slot in ("llm", "check")]) == ["GEMINI_API_KEY"]

    providers = build_providers(
        ProviderNames(image="mock", video="mock", tts="mock", llm="mock", check="mock")
    )
    assert providers.checker is not None and providers.check is not providers.llm


def test_images_and_video_need_the_checker_not_the_llm(episode: Path) -> None:
    # картинкам llm тоже нужен — на проверку по описанию пачкой и выбор
    # референса (14.09.2026); клипам — только контролёр (и провайдер картинок-сцен)
    assert required_providers(episode, RunOptions(only="images")) == ["image", "check", "llm"]
    assert required_providers(episode, RunOptions(only="video")) == ["video", "still", "check"]
    assert required_providers(episode, RunOptions(only="planner")) == ["llm"]


# ------------------------------------------------------------- стадии


class Counting(MockLlm):
    """Заглушка, которая считает текстовые вызовы отдельно от проверок."""

    def __init__(self) -> None:
        super().__init__()
        self.completes = 0

    def complete(self, prompt: str, **kwargs):  # type: ignore[no-untyped-def, override]
        self.completes += 1
        return super().complete(prompt, **kwargs)


@pytest.fixture
def briefed_episode(projects_root: Path) -> Path:
    """Эпизод с брифом и без сцен: сценарист и планнер зовут llm, картинки —
    контролёра. Как в test_planner."""
    create_project(
        ProjectSpec(
            id="p",
            name="Контролёр",
            platforms=["youtube_shorts"],
            character="timur",
            style="pixar3d",
            voice="ru_male",
            target_duration_sec=30,
            scene_duration_sec=6.0,
        )
    )
    return create_episode("p", "e1", title="проба", brief="мальчик находит старую карту")


def test_text_goes_to_the_llm_and_frames_to_the_checker(
    briefed_episode: Path, mock_providers: ProviderSet
) -> None:
    llm, checker = Counting(), Counting()
    mock_providers.llm = llm
    mock_providers.checker = checker

    report = run_episode(
        briefed_episode,
        mock_providers,
        RunOptions(auto_approve=True, raise_errors=True, stop="images"),
    )
    assert report.ok, report.reason

    scenes = load_storyboard(briefed_episode).scenes
    assert scenes and all(s.checks.image == "pass" for s in scenes)
    assert llm.completes > 0 and llm.check_calls == 0  # сценарист и планнер — в llm
    assert checker.check_calls == len(scenes) and checker.completes == 0  # кадры — контролёру
    # манифест помнит, чем сделан эпизод, — и кто проверял
    assert load_manifest(briefed_episode).providers["check"] == "mock"
    assert "check" in mock_providers.names()


def test_a_service_limit_is_a_refusal_not_a_crash(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Суточный лимит контролёра (429 RESOURCE_EXHAUSTED) — тоже отказ, а не
    падение стадии: ep003, 17.09.2026, сутки Gemini кончились на 20-м кадре и
    уронили стадию с поднятой картой."""

    class OutOfQuota(MockLlm):
        def check_images(
            self,
            images: list[Path],
            *,
            references: list[Reference],
            checklist: list[str],
            context: str,
        ) -> CheckResult:
            self.check_calls += 1
            raise ProviderError("Gemini, проверка кадров: 429 RESOURCE_EXHAUSTED после 3 попыток")

    sb = load_storyboard(episode)
    sb.scenes[0].cast = ["characters/timur"]
    save_storyboard(episode, sb)
    mock_providers.checker = OutOfQuota()

    report = run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, max_attempts=3)
    )
    assert report.ok, report.reason

    s1 = load_storyboard(episode).scenes[0]
    assert s1.checks.image == "skipped"
    assert "429 RESOURCE_EXHAUSTED" in s1.checks.notes["image"]
    assert s1.attempts["image"] == 1, "чужой лимит не повод перегенерировать кадр"
    # лимит бьёт по всем сценам с чеклистом, а не по одной
    assert "проверок отказано: 2" in load_manifest(episode).stages["images"].message


def test_a_refusal_is_a_skipped_check_not_a_verdict(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Контролёр отказался смотреть (block_reason) — проверка не состоялась:
    кадр не перегенерируется (претензий к нему нет) и не считается
    проверенным; стадия идёт дальше и называет число отказов."""

    class Refusing(MockLlm):
        def check_images(
            self,
            images: list[Path],
            *,
            references: list[Reference],
            checklist: list[str],
            context: str,
        ) -> CheckResult:
            self.check_calls += 1
            if self.check_calls == 1:
                return CheckResult(passed=False, refused="block_reason=SAFETY", cost_usd=0.0004)
            return CheckResult(passed=True, raw="mock")

    sb = load_storyboard(episode)
    sb.scenes[0].cast = ["characters/timur"]
    save_storyboard(episode, sb)
    checker = Refusing()
    mock_providers.checker = checker

    report = run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, max_attempts=3)
    )
    assert report.ok, report.reason

    s1, s2 = load_storyboard(episode).scenes
    assert s1.checks.image == "skipped"
    assert s1.checks.notes["image"] == "контролёр отказался (block_reason=SAFETY)"
    assert s1.attempts["image"] == 1, "отказ контролёра не повод перегенерировать"
    assert s1.attempt_log["image"][0].passed is True  # не провал: попытка не отвергнута
    assert s2.checks.image == "pass"
    m = load_manifest(episode)
    assert "проверок отказано: 1" in m.stages["images"].message
    # входные токены отказа всё равно посчитаны провайдером и записаны
    assert m.spent_usd == pytest.approx(0.0004)


# ------------------------------------------------------------- gemini: темп, повтор, отказ


class _Models:
    """`client.models` заглушки: по исходу на вызов — ответ или исключение."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate_content(self, **kwargs: object) -> object:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _ok(text: str, *, tokens_in: int = 2000, tokens_out: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
        prompt_feedback=None,
        usage_metadata=SimpleNamespace(
            prompt_token_count=tokens_in, candidates_token_count=tokens_out
        ),
    )


def _blocked(reason: str = "SAFETY") -> SimpleNamespace:
    """Как Google отвечает на чеклист «приметы лица + религиозная сцена»:
    без ошибки HTTP, без кандидатов, с block_reason на запросе."""
    return SimpleNamespace(
        text=None,
        candidates=[],
        prompt_feedback=SimpleNamespace(block_reason=SimpleNamespace(name=reason)),
        usage_metadata=SimpleNamespace(prompt_token_count=1500, candidates_token_count=0),
    )


def _quota(delay: str | None = "7s") -> Exception:
    from google.genai import errors

    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]
    body = {
        "error": {
            "code": 429,
            "message": "quota exceeded",
            "status": "RESOURCE_EXHAUSTED",
            "details": details if delay else [],
        }
    }
    return errors.ClientError(429, body)


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[list[float], Path]:
    """Ожидание записывается, а не спится; окно темпа — своё на тест."""
    slept: list[float] = []
    monkeypatch.setattr(gemini, "_sleep", slept.append)
    monkeypatch.setattr(gemini, "_paces", {})
    monkeypatch.delenv("VF_GEMINI_RPM", raising=False)
    frame = tmp_path / "frame.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(frame)
    return slept, frame


def _llm(outcomes: list[object], model: str = "gemini-3.5-flash-lite") -> gemini.GeminiLlm:
    llm = gemini.GeminiLlm(model=model)
    llm._client = SimpleNamespace(models=_Models(outcomes))
    return llm


def test_429_is_retried_with_the_pause_the_server_asks_for(
    quiet: tuple[list[float], Path],
) -> None:
    slept, frame = quiet
    llm = _llm([_quota("7s"), _quota("7s"), _ok('{"pass": true, "issues": []}')])
    result = llm.check_images([frame], references=[], checklist=["x"], context="y")
    assert result.passed and not result.refused
    assert llm._client.models.calls == 3
    assert slept == [7.0, 7.0]  # пауза от сервера, не своя
    # цена считана из usage по таблице: 2000 × $0.30/М + 20 × $2.50/М
    assert result.cost_usd == pytest.approx((2000 * 0.30 + 20 * 2.50) / 1_000_000)


def test_retry_after_header_and_own_backoff(quiet: tuple[list[float], Path]) -> None:
    from google.genai import errors

    slept, frame = quiet
    unavailable = errors.ServerError(
        503,
        {"error": {"message": "unavailable", "status": "UNAVAILABLE"}},
        httpx.Response(503, headers={"retry-after": "3"}),
    )
    llm = _llm([unavailable, _quota(None), _ok('{"pass": false, "issues": ["шов"]}')])
    result = llm.check_images([frame], references=[], checklist=["x"], context="y")
    assert result.issues == ["шов"]
    # заголовок → 3 с; без подсказки — своя экспонента со второй попытки: 4 с
    assert slept == [3.0, 4.0]


def test_three_quota_errors_give_up_and_other_errors_do_not_retry(
    quiet: tuple[list[float], Path],
) -> None:
    from google.genai import errors

    slept, frame = quiet
    llm = _llm([_quota(), _quota(), _quota()])
    with pytest.raises(ProviderError, match="после 3 попыток"):
        llm.check_images([frame], references=[], checklist=["x"], context="y")
    assert llm._client.models.calls == 3 and len(slept) == 2

    gone = errors.ClientError(404, {"error": {"message": "no such model", "status": "NOT_FOUND"}})
    llm = _llm([gone, _ok("{}")])
    with pytest.raises(ProviderError, match="404"):
        llm.complete("hi")
    assert llm._client.models.calls == 1  # 404 не повторяется


def test_a_block_is_a_refusal_with_its_reason_and_its_cost(
    quiet: tuple[list[float], Path],
) -> None:
    _, frame = quiet
    llm = _llm([_blocked("SAFETY")])
    result = llm.check_images([frame], references=[], checklist=["x"], context="y")
    assert result.refused == "block_reason=SAFETY"
    assert result.passed is False and result.issues == []
    assert result.cost_usd == pytest.approx(1500 * 0.30 / 1_000_000)  # вход посчитан

    # текстовой стадии молчание не отдаётся пустой строкой — причина в ошибке
    llm = _llm([_blocked("PROHIBITED_CONTENT")])
    with pytest.raises(ProviderError, match="PROHIBITED_CONTENT"):
        llm.complete("hi")


def test_an_empty_answer_without_a_reason_is_also_a_refusal() -> None:
    silent = SimpleNamespace(
        text="",
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
        prompt_feedback=SimpleNamespace(block_reason=None),
    )
    assert gemini._refusal(silent) == "пустой ответ"
    cut = SimpleNamespace(
        text="",
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="SAFETY"))],
        prompt_feedback=None,
    )
    assert gemini._refusal(cut) == "finish_reason=SAFETY"
    assert gemini._refusal(_ok("ok")) == ""


def test_pace_holds_the_rpm_over_any_sixty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    slept: list[float] = []

    def sleep(sec: float) -> None:
        slept.append(sec)
        clock[0] += sec

    monkeypatch.setattr(gemini, "_sleep", sleep)
    pace = gemini.Pace(3, clock=lambda: clock[0])
    for _ in range(3):
        assert pace.wait() == 0.0
        clock[0] += 5.0  # три вызова за 15 с — в лимите
    assert pace.wait() == pytest.approx(45.0)  # четвёртый ждёт, пока первому не минет 60 с
    assert slept == [pytest.approx(45.0)]
    clock[0] += 61.0
    assert pace.wait() == 0.0  # окно опустело — без ожидания

    monkeypatch.setenv("VF_GEMINI_RPM", "0")
    assert gemini.Pace(gemini.rpm_limit()).wait() == 0.0  # 0 — без ограничения
    monkeypatch.setenv("VF_GEMINI_RPM", "12")
    monkeypatch.setattr(gemini, "_paces", {})
    assert gemini.pace("m").rpm == 12 and gemini.pace("m") is gemini.pace("m")


def test_unknown_model_price_is_zero_but_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    gemini._unpriced_warned.discard("gemini-9-unknown")
    with caplog.at_level("WARNING", logger="videoforge.gemini"):
        assert gemini._usage_cost(_ok("x"), "gemini-9-unknown") == 0.0
    assert "gemini-9-unknown" in caplog.text and "неизвестна" in caplog.text
    assert not gemini.price_known("gemini-9-unknown") and gemini.price_known(
        "gemini-3.5-flash-lite"
    )


# ------------------------------------------------------------- doctor


def _names(llm: str, check: str = "") -> ProviderNames:
    return ProviderNames(image="mock", video="mock", tts="mock", llm=llm, check=check)


def test_doctor_names_the_controller_and_warns_when_it_cannot_see(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VF_VISION_CHECK", raising=False)
    lines = controller_status(_names("claude_cli"))
    assert lines[0].startswith("контролёр кадров и клипов (VF_CHECK_PROVIDER): claude_cli")
    assert "тот же, что llm" in lines[0]
    assert any("не смотрит" in line and "VF_CHECK_PROVIDER=gemini" in line for line in lines)

    monkeypatch.setenv("GEMINI_TEXT_MODEL", "gemini-3.5-flash-lite")
    lines = controller_status(_names("claude_cli", "gemini"))
    assert "gemini" in lines[0] and "тот же" not in lines[0]
    assert any("ключ GEMINI_API_KEY" in line for line in lines)  # факт наличия, не значение
    assert any("gemini-3.5-flash-lite" in line and "вызовов/мин" in line for line in lines)
    assert not any("неизвестна" in line for line in lines)

    monkeypatch.setenv("GEMINI_TEXT_MODEL", "gemini-9-unknown")
    lines = controller_status(_names("claude_cli", "gemini"))
    assert any("цена модели gemini-9-unknown неизвестна" in line for line in lines)

    monkeypatch.setenv("VF_VISION_CHECK", "off")
    assert any("VF_VISION_CHECK=off" in line for line in controller_status(_names("gemini")))
