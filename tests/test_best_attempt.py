"""Оставлять лучшую попытку кадра, а не последнюю (`features/image_best_attempt`).

Вердикт vision-модели плывёт между прогонами: на ep002 кадр «медь льётся
вверх» проверка забраковала дважды и пропустила на третьей — и в ролик ушёл
именно он. Поэтому попытки хранятся все (`Scene.attempt_log`), а после
исчерпания `max_attempts` остаётся та, у которой замечаний меньше; при равном
числе — более поздняя (она делалась с учётом причин отказа).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core.manifest import load_manifest
from videoforge.core.report import render_report
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import (
    Attempt,
    Scene,
    Storyboard,
    best_attempt,
    load_storyboard,
    save_storyboard,
)
from videoforge.providers.base import ProviderSet
from videoforge.providers.mock import MockLlm


def _one_checked_scene(episode: Path) -> None:
    """Одна сцена с кастом: чеклист непустой, значит проверка кадра зовётся, и
    вердикты заглушки уходят ровно этой сцене (у стиля свой чеклист, и вторая
    сцена иначе съела бы их первой)."""
    sb = load_storyboard(episode)
    sb.scenes = sb.scenes[:1]
    sb.scenes[0].cast = ["characters/timur"]
    save_storyboard(episode, sb)


def _picky(mock_providers: ProviderSet, verdicts: list[list[str]]) -> MockLlm:
    llm = MockLlm()
    llm.check_issues = verdicts
    mock_providers.llm = llm
    return llm


def test_fewest_issues_wins_and_the_later_of_equals_breaks_the_tie(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """2 → 1 → 1 замечание: остаётся третья — из двух равных та, что делалась
    позже, с причинами отказа второй в промпте. Статус при этом fail: выбор
    лучшей не делает её прошедшей."""
    _one_checked_scene(episode)
    _picky(mock_providers, [["поток вверх", "лишний объект"], ["поток вверх"], ["ракурс"]])

    report = run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, max_attempts=3)
    )

    assert report.stopped_at == "images"
    s1 = load_storyboard(episode).scenes[0]
    assert s1.attempts["image"] == 3
    assert s1.assets.image and s1.assets.image.endswith("s1_v03.png")
    assert s1.checks.image == "fail"
    assert s1.checks.notes["image"] == "ракурс"  # замечания выбранной, не последней вообще
    log = s1.attempt_log["image"]
    assert [(a.n, a.passed, len(a.issues)) for a in log] == [
        (1, False, 2),
        (2, False, 1),
        (3, False, 1),
    ]
    assert [a.path for a in log] == [f"02_images/s1_v{n:02d}.png" for n in (1, 2, 3)]
    assert all((episode / a.path).exists() for a in log), "файлы попыток должны остаться"
    message = load_manifest(episode).stages["images"].message
    assert "s1: не прошла после 3 попыток, оставлена v03 (1 замечание)" in message


def test_the_best_attempt_is_not_always_the_last(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """2 → 1 → 3: остаётся вторая. До этого в раскадровке оставалась та, на
    которой кончился счётчик, — здесь это худшая из трёх."""
    _one_checked_scene(episode)
    _picky(mock_providers, [["a", "b"], ["c"], ["d", "e", "f"]])

    run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, max_attempts=3)
    )

    s1 = load_storyboard(episode).scenes[0]
    assert s1.assets.image and s1.assets.image.endswith("s1_v02.png")
    assert s1.checks.image == "fail" and s1.checks.notes["image"] == "c"
    assert len(s1.attempt_log["image"]) == 3
    message = load_manifest(episode).stages["images"].message
    assert "оставлена v02 (1 замечание)" in message


def test_a_passed_attempt_still_ends_the_cycle(episode: Path, mock_providers: ProviderSet) -> None:
    """Прошедшая попытка обрывает цикл, как и раньше: журнал из двух записей,
    третьей генерации нет, актив — прошедшая."""
    _one_checked_scene(episode)
    llm = _picky(mock_providers, [["не похож"], []])

    report = run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, max_attempts=3)
    )

    assert report.ok, report.reason
    assert llm.check_calls == 2
    s1 = load_storyboard(episode).scenes[0]
    assert s1.attempts["image"] == 2
    assert s1.assets.image and s1.assets.image.endswith("s1_v02.png")
    assert s1.checks.image == "pass" and "image" not in s1.checks.notes
    log = s1.attempt_log["image"]
    assert [(a.n, a.passed, a.issues) for a in log] == [(1, False, ["не похож"]), (2, True, [])]
    assert all(a.provider == "mock" for a in log)


def test_the_choice_is_among_this_runs_attempts_only(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Повтор после правки промпта сравнивает только новые попытки: кадр от
    старого промпта не выставляется против кадров от нового, даже если у него
    замечаний было меньше. Старые записи остаются в журнале как история."""
    _one_checked_scene(episode)
    _picky(mock_providers, [["x"], ["y", "z"], ["w", "v"]])
    opts = RunOptions(only="images", auto_approve=True, max_attempts=1)
    run_episode(episode, mock_providers, opts)  # v01: 1 замечание

    sb = load_storyboard(episode)
    sb.scenes[0].visual_prompt = "a completely different frame"
    save_storyboard(episode, sb)
    run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, max_attempts=2)
    )

    s1 = load_storyboard(episode).scenes[0]
    assert s1.assets.image and s1.assets.image.endswith("s1_v03.png")  # поздняя из новых равных
    assert [a.n for a in s1.attempt_log["image"]] == [1, 2, 3]  # история цела


def test_best_attempt_rule() -> None:
    """Меньше замечаний — лучше; при равном счёте — поздняя; прошедшая — всегда."""

    def a(n: int, issues: int, passed: bool = False) -> Attempt:
        return Attempt(n=n, path=f"p{n}", passed=passed, issues=[f"i{k}" for k in range(issues)])

    assert best_attempt([a(1, 2), a(2, 1), a(3, 1)]).n == 3
    assert best_attempt([a(1, 2), a(2, 1), a(3, 3)]).n == 2
    assert best_attempt([a(1, 0), a(2, 0)]).n == 2
    assert best_attempt([a(1, 0, passed=True), a(2, 0)]).n == 1
    with pytest.raises(ValueError):
        best_attempt([])


def test_old_storyboards_without_the_log_still_load(tmp_path: Path) -> None:
    """Раскадровка прошлой версии читается: попыток у неё не записано, а не ноль."""
    scene = Scene.model_validate({"id": "s1", "order": 1, "attempts": {"image": 3}})
    assert scene.attempt_log == {} and scene.retried() == {}

    sb = Storyboard(project_id="p", scenes=[scene])
    scene.attempt_log["image"] = [Attempt(n=1, path="02_images/s1_v01.png", passed=False)]
    save_storyboard(tmp_path, sb)
    loaded = load_storyboard(tmp_path).scenes[0]
    assert loaded.attempt_log["image"][0].path == "02_images/s1_v01.png"
    assert loaded.retried() == {}  # одна попытка — показывать нечего


@pytest.mark.render
def test_video_keeps_the_best_clip_and_measures_it(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """У клипов то же правило; длина и профиль движения — у выбранного клипа."""
    _one_checked_scene(episode)
    # первая проверка — кадр (проходит), дальше три клипа: 2 → 1 → 2 замечания
    _picky(mock_providers, [[], ["a", "b"], ["c"], ["d", "e"]])

    report = run_episode(
        episode,
        mock_providers,
        RunOptions(start="images", stop="video", auto_approve=True, max_attempts=3),
    )

    assert report.stopped_at == "video"
    s1 = load_storyboard(episode).scenes[0]
    assert s1.assets.video and s1.assets.video.endswith("s1_v02.mp4")
    assert s1.checks.video == "fail" and s1.checks.notes["video"] == "c"
    assert [len(a.issues) for a in s1.attempt_log["video"]] == [2, 1, 2]
    assert s1.measured_sec["video"] == pytest.approx(6.0, abs=0.2)
    assert "s1: не прошла после 3 попыток, оставлена v02 (1 замечание)" in (
        load_manifest(episode).stages["video"].message
    )


def test_report_and_pult_list_the_attempts(episode: Path, mock_providers: ProviderSet) -> None:
    """Отчёт и пульт показывают попытки там, где их было больше одной, и
    отмечают выбранную; у сцены с одной попыткой списка нет."""
    from videoforge.api import data

    _one_checked_scene(episode)
    _picky(mock_providers, [["a", "b"], ["c"]])
    run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, max_attempts=2)
    )

    sb = load_storyboard(episode)
    page = render_report(episode, sb, load_manifest(episode)).read_text(encoding="utf-8")
    assert "попытки image" in page
    assert "v01 — 2 замеч." in page and "v02 — 1 замеч." in page
    assert "02_images/s1_v02.png</a> ← выбрана" in page

    rows = data.episode_detail("t", "e1")["scenes"]
    log = rows[0]["attempt_log"]
    assert [(r["kind"], r["n"], r["passed"], r["issues"], r["kept"]) for r in log] == [
        ("image", 1, False, 2, False),
        ("image", 2, False, 1, True),
    ]
    assert log[1]["path"] == "02_images/s1_v02.png" and log[1]["url"]
    assert rows[0]["attempts"] == {"image": 2}  # прежнее поле пульта не тронуто
