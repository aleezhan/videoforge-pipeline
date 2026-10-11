"""Референс композиции у сцены и проверка кадров по описанию пачкой
(`features/scene_reference_image`; замеры `projects/_bench/test8b`, `test9`).

Кадр «не про то» слепым перекатом сида не лечится: стадия картинок после
каждого круга отдаёт кадры llm пачками по двадцать, кадр с вердиктом `block`
получает фразу `fix` в описание и найденный по запросу вердикта референс
композиции — и идёт на повтор. Человек в этом не участвует: он видит строку
на воротах и референс рядом с кадром в отчёте и пульте.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from videoforge.core.manifest import load_manifest
from videoforge.core.report import render_report
from videoforge.core.runner import required_providers, run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, load_storyboard, save_storyboard
from videoforge.paths import ROOT
from videoforge.providers import claude_cli
from videoforge.providers.base import (
    FrameToReview,
    FrameVerdict,
    ProviderError,
    ProviderSet,
    Reference,
)
from videoforge.providers.comfy_http import klein_graph, klein_reference_graph
from videoforge.providers.mock import MockImage, MockLlm, MockSearch
from videoforge.providers.review import BATCH, parse_review, review_prompt

QUERY = "row of identical objects receding into distance"
FIX = "identical wings lined up one behind another toward a vanishing point in bright haze"


class Recording(MockImage):
    """Художник-заглушка, который помнит, с какими референсами его звали."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[Reference]] = []

    def generate(self, prompt, *, references, aspect_ratio, out_path):  # type: ignore[no-untyped-def, override]
        self.calls.append(list(references))
        return super().generate(
            prompt, references=references, aspect_ratio=aspect_ratio, out_path=out_path
        )


def _block(sid: str, search: str = QUERY) -> FrameVerdict:
    return FrameVerdict(
        id=sid,
        match=False,
        severity="block",
        see="a heap of overlapping wings on the ground",
        issue="heap instead of a row receding into haze",
        fix=FIX,
        search=search,
    )


def _run(episode: Path, providers: ProviderSet, **opts: object):  # type: ignore[no-untyped-def]
    return run_episode(
        episode,
        providers,
        RunOptions(only="images", auto_approve=True, raise_errors=True, **opts),  # type: ignore[arg-type]
    )


def _refs(episode: Path) -> dict:
    return json.loads((episode / "02_images/refs/refs.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------- стадия


def test_a_frame_not_about_the_scene_gets_a_reference_and_a_retake(
    episode: Path, mock_providers: ProviderSet
) -> None:
    llm = MockLlm()
    llm.review_verdicts = {"s1": [_block("s1")]}
    mock_providers.llm = llm
    image = Recording()
    mock_providers.image = image

    report = _run(episode, mock_providers, max_attempts=2)

    assert report.ok, report.reason
    s1, s2 = load_storyboard(episode).scenes
    assert (s1.attempts["image"], s2.attempts["image"]) == (2, 1)
    # референс найден, скачан, записан с происхождением и вписан в сцену
    assert s1.reference_assets == ["02_images/refs/s1.png"]
    assert (episode / "02_images/refs/s1.png").exists()
    entry = _refs(episode)["s1"]
    assert entry["query"] == QUERY and entry["license"] == "CC0" and entry["pick"] == 1
    assert entry["url"].startswith("file:") and entry["file"] == "02_images/refs/s1.png"
    assert mock_providers.search is not None and mock_providers.search.calls == [QUERY]  # type: ignore[attr-defined]
    # повтор шёл с референсом композиции и с правкой в описании, первая попытка — без
    assert [r.role for r in image.calls[0]] == []
    assert [r.role for r in image.calls[2]] == ["composition"]
    assert image.calls[2][0].path == episode / "02_images/refs/s1.png"
    assert s1.fixes == [FIX]
    assert FIX in (episode / "02_images/s1.prompt.txt").read_text(encoding="utf-8")
    # после удачного повтора кадр про то; журнал помнит, что первая была не про то
    assert s1.checks.image == "pass" and s1.review["image"].severity == "ok"
    log = s1.attempt_log["image"]
    assert [(a.n, a.passed) for a in log] == [(1, False), (2, True)]
    assert "не про то: heap instead of a row receding into haze" in log[0].issues
    # проверка шла пачкой: первый круг — обе сцены, второй — только повтор
    assert llm.reviewed == [["s1", "s2"], ["s1"]]
    message = load_manifest(episode).stages["images"].message
    assert "по описанию не про то: 1" in message
    assert "референсы: s1 → file:" in message


def test_a_detail_is_a_note_for_the_human_not_a_retake(
    episode: Path, mock_providers: ProviderSet
) -> None:
    llm = MockLlm()
    llm.review_verdicts = {
        "s2": [
            FrameVerdict(
                id="s2",
                match=False,
                severity="note",
                see="a map close-up",
                issue="lighting is cooler than described",
            )
        ]
    }
    mock_providers.llm = llm

    report = _run(episode, mock_providers, max_attempts=2)

    assert report.ok, report.reason
    s2 = load_storyboard(episode).scenes[1]
    assert s2.attempts["image"] == 1 and s2.checks.image == "pass"
    assert s2.checks.notes["image"] == "деталь: lighting is cooler than described"
    assert s2.review["image"].severity == "note"
    assert "детали в заметках: s2" in load_manifest(episode).stages["images"].message


def test_without_a_search_hit_the_retake_goes_without_a_reference(
    episode: Path, mock_providers: ProviderSet
) -> None:
    llm = MockLlm()
    llm.review_verdicts = {"s1": [_block("s1")]}
    mock_providers.llm = llm
    mock_providers.search = MockSearch(candidates=0)

    report = _run(episode, mock_providers, max_attempts=2)

    assert report.ok, report.reason
    s1 = load_storyboard(episode).scenes[0]
    assert s1.attempts["image"] == 2 and s1.reference_assets == []
    assert s1.fixes == [FIX]  # правка описания всё равно ушла в повтор
    assert _refs(episode)["s1"]["found"] == 0
    assert "референс не нашёлся: s1" in load_manifest(episode).stages["images"].message


class FailingSearch(MockSearch):
    """Поиск, который отвечает отказом сервиса — как Commons с 429 на ep003."""

    def search(self, query: str, *, limit: int = 8):  # type: ignore[no-untyped-def, override]
        raise ProviderError("Wikimedia Commons: HTTPStatusError: 429 Too Many Requests")


def test_a_search_refusal_does_not_stop_the_stage(
    episode: Path, mock_providers: ProviderSet
) -> None:
    llm = MockLlm()
    llm.review_verdicts = {"s1": [_block("s1")]}
    mock_providers.llm = llm
    mock_providers.search = FailingSearch()

    report = _run(episode, mock_providers, max_attempts=2)

    assert report.ok, report.reason
    s1 = load_storyboard(episode).scenes[0]
    # повтор ушёл без референса, но с правкой описания
    assert s1.attempts["image"] == 2 and s1.reference_assets == []
    assert s1.fixes == [FIX]
    assert "референс не нашёлся: s1" in load_manifest(episode).stages["images"].message


def test_commons_retries_a_429_before_giving_up(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from videoforge.providers import commons

    answers = [
        httpx.Response(429, headers={"retry-after": "3"}),
        httpx.Response(200, json={"query": {"search": []}}),
    ]
    slept: list[float] = []
    monkeypatch.setattr(commons, "_sleep", slept.append)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: answers.pop(0)))

    assert commons._get(client, {"action": "query"}) == {"query": {"search": []}}
    assert slept == [3.0]

    always = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429)))
    with pytest.raises(ProviderError, match="429"):
        commons._get(always, {"action": "query"})


def test_the_model_may_pick_no_candidate(episode: Path, mock_providers: ProviderSet) -> None:
    llm = MockLlm()
    llm.review_verdicts = {"s1": [_block("s1")]}
    llm.pick = 0
    mock_providers.llm = llm

    _run(episode, mock_providers, max_attempts=2)

    s1 = load_storyboard(episode).scenes[0]
    assert s1.reference_assets == [] and llm.pick_calls == 1
    entry = _refs(episode)["s1"]
    assert entry["pick"] == 0 and len(entry["candidates"]) == 2
    assert (episode / "02_images/refs/s1/candidates.png").exists()


def test_the_retake_can_fail_too_and_the_best_attempt_stays(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Оба круга не про то: остаётся лучшая попытка со статусом fail, ворота
    получают причину — референс не спасает всегда, и это видно."""
    llm = MockLlm()
    llm.review_verdicts = {"s1": [_block("s1"), _block("s1")]}
    mock_providers.llm = llm

    report = _run(episode, mock_providers, max_attempts=2)

    assert not report.ok and report.stopped_at == "images"
    s1 = load_storyboard(episode).scenes[0]
    assert s1.attempts["image"] == 2 and s1.checks.image == "fail"
    assert s1.checks.notes["image"].startswith("не про то:")
    assert "s1: не прошла после 2 попыток" in load_manifest(episode).stages["images"].message


def test_a_manual_reference_is_used_and_is_part_of_the_fingerprint(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Файл, положенный в reference_assets руками, уходит художнику так же; его
    содержимое — во входах кадра: подменили картинку под тем же именем — кадр
    устарел, не трогали — не переделывается."""
    ref = episode / "02_images/refs/s1.png"
    ref.parent.mkdir(parents=True)
    Image.new("RGB", (64, 64), "red").save(ref)
    sb = load_storyboard(episode)
    sb.scenes[0].reference_assets = ["02_images/refs/s1.png"]
    save_storyboard(episode, sb)
    image = Recording()
    mock_providers.image = image

    _run(episode, mock_providers)
    assert [r.role for r in image.calls[0]] == ["composition"] and image.calls[0][0].path == ref
    assert load_storyboard(episode).scenes[0].attempts["image"] == 1

    _run(episode, mock_providers)  # ничего не менялось — не переделывается
    assert load_storyboard(episode).scenes[0].attempts["image"] == 1

    Image.new("RGB", (64, 64), "blue").save(ref)
    _run(episode, mock_providers)
    assert load_storyboard(episode).scenes[0].attempts["image"] == 2


def test_a_missing_manual_reference_fails_before_spending(
    episode: Path, mock_providers: ProviderSet
) -> None:
    sb = load_storyboard(episode)
    sb.scenes[0].reference_assets = ["02_images/refs/nope.png"]
    save_storyboard(episode, sb)
    image = Recording()
    mock_providers.image = image

    report = run_episode(episode, mock_providers, RunOptions(only="images", auto_approve=True))

    assert not report.ok and "nope.png" in (report.reason or "")
    assert image.calls == []


def test_review_can_be_switched_off(
    episode: Path, mock_providers: ProviderSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VF_FRAME_REVIEW", "off")
    llm = MockLlm()
    llm.review_verdicts = {"s1": [_block("s1")]}
    mock_providers.llm = llm

    report = _run(episode, mock_providers, max_attempts=2)

    assert report.ok and llm.review_calls == 0
    assert load_storyboard(episode).scenes[0].attempts["image"] == 1
    assert "проверка по описанию: нет (VF_FRAME_REVIEW=off)" in (
        load_manifest(episode).stages["images"].message
    )


def test_an_llm_without_batch_review_is_named_in_the_message(
    episode: Path, mock_providers: ProviderSet
) -> None:
    class NoReview(MockLlm):
        reviews_frames = False

    mock_providers.llm = NoReview()
    report = _run(episode, mock_providers)
    assert report.ok
    assert "проверка по описанию: нет (mock не смотрит кадры пачкой)" in (
        load_manifest(episode).stages["images"].message
    )


def test_frames_go_to_the_model_in_batches(episode: Path, mock_providers: ProviderSet) -> None:
    sb = load_storyboard(episode)
    sb.scenes = [
        Scene(id=f"s{i:02d}", order=i, visual_prompt=f"frame number {i}", motion_prompt="still")
        for i in range(1, BATCH + 6)
    ]
    save_storyboard(episode, sb)
    llm = MockLlm()
    mock_providers.llm = llm

    report = _run(episode, mock_providers)

    assert report.ok, report.reason
    assert [len(batch) for batch in llm.reviewed] == [BATCH, 5]


def test_images_stage_needs_the_llm_for_the_review(episode: Path) -> None:
    assert required_providers(episode, RunOptions(only="images")) == ["image", "check", "llm"]


def test_report_and_pult_show_the_reference_and_the_verdict(
    episode: Path, mock_providers: ProviderSet
) -> None:
    from videoforge.api import data

    llm = MockLlm()
    llm.review_verdicts = {"s1": [_block("s1")]}
    mock_providers.llm = llm
    _run(episode, mock_providers, max_attempts=2)

    sb = load_storyboard(episode)
    page = render_report(episode, sb, load_manifest(episode)).read_text(encoding="utf-8")
    assert 'href="02_images/refs/s1.png"' in page
    assert "по описанию" in page and "правки" in page and FIX in page

    row = data.episode_detail("t", "e1")["scenes"][0]
    assert row["references"][0]["path"] == "02_images/refs/s1.png" and row["references"][0]["url"]
    assert row["review"]["severity"] == "ok" and row["fixes"] == [FIX]


# ------------------------------------------------------------- разбор ответа


def test_parse_review_is_strict_about_ids_and_severity(tmp_path: Path) -> None:
    frames = [
        FrameToReview(id=sid, path=tmp_path / f"{sid}.png", description="d")
        for sid in ("s1", "s2", "s3")
    ]
    text = json.dumps(
        {
            "scenes": [
                {"id": "s1", "match": True, "severity": "weird", "see": "fine"},
                {"id": "s2", "match": False, "issue": "heap", "fix": "row", "search": "row of x"},
                {"id": "s9", "match": False, "severity": "block"},
            ]
        }
    )
    verdicts = parse_review("```json\n" + text + "\n```", frames)
    assert [(v.id, v.severity) for v in verdicts] == [
        ("s1", "ok"),
        ("s2", "block"),
    ]  # s3 — нет вердикта
    assert verdicts[1].search == "row of x"
    with pytest.raises(ProviderError):
        parse_review("not json", frames)
    with pytest.raises(ProviderError):
        parse_review('{"verdicts": []}', frames)
    prompt = review_prompt(frames)
    assert str((tmp_path / "s2.png").resolve()) in prompt and '"severity"' in prompt


# ------------------------------------------------------------- провайдеры


def test_klein_reference_graph_chains_references_into_the_positive() -> None:
    base = klein_graph("p", width=1280, height=720, seed=1, lora="", lora_strength=1.0, prefix="x")
    g = klein_reference_graph(base, ["a.jpg", "b.png"])
    assert g["guider"]["inputs"]["positive"] == ["pos_ref2", 0]
    assert g["pos_ref2"]["inputs"]["conditioning"] == ["pos_ref1", 0]
    assert g["pos_ref1"]["inputs"] == {"conditioning": ["pos", 0], "latent": ["ref_lat1", 0]}
    assert g["ref_lat1"]["inputs"] == {"pixels": ["ref_scale1", 0], "vae": ["vae", 0]}
    assert g["ref_scale1"]["inputs"]["resolution_steps"] == 16
    assert g["ref_img2"]["inputs"]["image"] == "b.png"
    assert base["guider"]["inputs"]["positive"] == ["pos", 0]  # исходный граф не тронут


def _fake_claude(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[dict]:
    calls: list[dict] = []

    def run(args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"args": args, **kwargs})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "claude")
    monkeypatch.delenv("VF_CLAUDE_CLI_MODEL", raising=False)
    return calls


def test_claude_cli_runs_from_the_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude -p` не читает файлы вне своей рабочей папки: cwd — корень, не
    папка, из которой человек открыл терминал."""
    calls = _fake_claude(monkeypatch, "ok")
    claude_cli.ClaudeCli().complete("hi")
    assert calls[0]["cwd"] == str(ROOT)


def test_claude_cli_reviews_a_batch_in_one_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frames = [
        FrameToReview(id=sid, path=tmp_path / f"{sid}.png", description="d") for sid in ("a", "b")
    ]
    answer = json.dumps(
        {"scenes": [{"id": "a", "match": True}, {"id": "b", "match": False, "severity": "note"}]}
    )
    calls = _fake_claude(monkeypatch, answer)
    result = claude_cli.ClaudeCli().review_frames(frames)
    assert [(v.id, v.severity) for v in result.verdicts] == [("a", "ok"), ("b", "note")]
    assert len(calls) == 1 and result.cost_usd == 0.0
    sent = calls[0]["input"]
    assert (
        str((tmp_path / "a.png").resolve()) in sent and str((tmp_path / "b.png").resolve()) in sent
    )


def test_review_prompt_carries_channel_rules_and_defects(tmp_path: Path) -> None:
    """Правила канала описание сцены не скажет — их несёт сам кадр на проверку,
    и без них в промпте нет строки правил вовсе."""
    from videoforge.providers.base import FrameToReview
    from videoforge.providers.review import DEFECTS, review_prompt

    plain = FrameToReview(id="s01", path=tmp_path / "a.png", description="a mosque")
    ruled = FrameToReview(
        id="s02", path=tmp_path / "b.png", description="a mosque", rules=("no crosses on domes",)
    )
    assert DEFECTS in review_prompt([plain]) and "Channel rules" not in review_prompt([plain])
    assert "- no crosses on domes" in review_prompt([plain, ruled])
