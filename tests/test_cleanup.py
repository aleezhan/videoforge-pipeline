"""Уборка промежуточных файлов эпизода: классы по реальным раскладкам, сухой
прогон ничего не трогает, удаление — только выбранное и только под замком."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from videoforge.api.app import create_app
from videoforge.cli import app
from videoforge.core.cleanup import CleanupRefused, classify, cleanup, to_dict
from videoforge.core.manifest import load_manifest
from videoforge.core.materials import Material, Materials, save_materials
from videoforge.core.runner import Busy, episode_lock
from videoforge.core.storyboard import SourceSegment, load_storyboard, save_storyboard


def _touch(path: Path, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _fill(edir: Path) -> None:
    """Эпизод ai_video после прогона — те же имена, что у doc_channel/ep001 и tests/t01.

    Активы: у s1 выбран кадр v02 (v01 — отвергнутая попытка), клип v01 (v02 —
    лишняя попытка); у s2 клип сцены — `.out.mp4`, как у сцен с переходом."""
    sb = load_storyboard(edir)
    s1, s2 = sb.scenes
    s1.assets.image = "02_images/s1_v02.png"
    s1.assets.video = "03_video/s1_v01.mp4"
    s1.assets.audio = "04_audio/s1.mp3"
    s1.assets.captions = "04_audio/s1.srt"
    s1.assets.clip = "05_assemble/s1.mp4"
    s1.assets.thumbs = ["05_assemble/thumbs/s1_1.jpg"]
    s2.assets.image = "02_images/s2_v01.png"
    s2.assets.video = "03_video/s2_v01.mp4"
    s2.assets.clip = "05_assemble/s2.out.mp4"
    sb.final.video = "05_assemble/final.mp4"
    save_storyboard(edir, sb)
    for rel, size in JUNK.items():
        _touch(edir / rel, size)
    for rel, size in REGENERABLE.items():
        _touch(edir / rel, size)
    for rel in KEEP:
        _touch(edir / rel)


JUNK = {
    "05_assemble/s1.mp4": 1000,
    "05_assemble/s2.mp4": 1000,
    "05_assemble/s2.out.mp4": 500,
    "05_assemble/s1.in.mp4": 500,
    "05_assemble/s2.cut.mp4": 500,
    "05_assemble/scenes.mp4": 2000,
    "05_assemble/scenes.txt": 10,
    "05_assemble/final.mp4.peaks.json": 10,
    "05_assemble/final.poster.jpg": 100,
    "05_assemble/preview_tiktok.png": 100,
    "05_assemble/shots.json": 10,
    "05_assemble/shots/001.jpg": 100,
    "05_assemble/qa/spot01.jpg": 100,
    "05_assemble/qa/cut01_s1_s2_before.jpg": 100,
    "03_video/s1_v01.motion.json": 10,
    "03_video/s1_v01_frames/f1.jpg": 100,
    "04_audio/s1.mp3.peaks.json": 10,
    "manifest.json.tmp": 10,
}
REGENERABLE = {
    "02_images/s1_v01.png": 3000,  # отвергнутая попытка: в assets выбран v02
    "03_video/s2_v02.mp4": 3000,  # лишняя попытка: в assets выбран v01
}
KEEP = [
    "05_assemble/final.mp4",
    "05_assemble/captions.ass",
    "05_assemble/qa.json",
    "05_assemble/montage.json",
    "05_assemble/timeline.json",
    "05_assemble/thumbnail.jpg",
    "05_assemble/thumbs/s1_1.jpg",
    "05_assemble/final.before_model_effects.mp4",  # чужой файл рядом с финалом
    "02_images/s1_v02.png",
    "02_images/s2_v01.png",
    "02_images/s1.prompt.txt",
    "03_video/s1_v01.mp4",
    "03_video/s2_v01.mp4",
    "03_video/s1.prompt.txt",
    "04_audio/s1.mp3",
    "04_audio/s1.srt",
    "01_script/script.md",
    "05_edit/edit_plan.json",
    "06_publish/publish.md",
    "report.html",
    "run.log",
    "mystery.bin",
]


def _classes(edir: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {
        "junk": set(),
        "regenerable": set(),
        "downloaded": set(),
        "keep": set(),
    }
    for e in classify(edir):
        out[e.cls].add(e.path)
    return out


def test_classes_follow_real_layouts(episode: Path) -> None:
    _fill(episode)
    got = _classes(episode)
    assert got["junk"] == set(JUNK)
    assert got["regenerable"] == set(REGENERABLE)
    assert got["downloaded"] == set()
    assert set(KEEP) | {"storyboard.json", "manifest.json"} == got["keep"]
    # замок прогона — не файл эпизода, в списке его нет
    with episode_lock(episode, what="прогон"):
        assert ".run.lock" not in {e.path for e in classify(episode)}


def test_dry_run_deletes_nothing_and_counts(episode: Path) -> None:
    _fill(episode)
    before = {p.relative_to(episode).as_posix() for p in episode.rglob("*") if p.is_file()}
    result = cleanup(episode, ("junk", "regenerable"))
    assert not result.applied and result.deleted == []
    assert {e.path for e in result.chosen} == set(JUNK) | set(REGENERABLE)
    assert result.would_free_bytes == sum(JUNK.values()) + sum(REGENERABLE.values())
    assert {p.relative_to(episode).as_posix() for p in episode.rglob("*") if p.is_file()} == before
    assert load_manifest(episode).cleanup is None

    view = to_dict(result)
    assert view["classes"]["junk"]["files"] == len(JUNK)
    assert view["classes"]["keep"]["items"][0]["why"]
    assert view["applied"] is False and view["freed_mb"] == 0.0


def test_apply_removes_only_chosen_classes_and_leaves_a_trace(episode: Path) -> None:
    _fill(episode)
    result = cleanup(episode, ("junk",), apply=True)
    assert result.applied and {e.path for e in result.deleted} == set(JUNK)
    assert result.freed_bytes == sum(JUNK.values())
    for rel in JUNK:
        assert not (episode / rel).exists(), rel
    for rel in list(REGENERABLE) + KEEP:
        assert (episode / rel).exists(), rel
    # опустевшие папки подчищены, непустые — нет
    assert not (episode / "03_video" / "s1_v01_frames").exists()
    assert not (episode / "05_assemble" / "qa").exists()
    assert (episode / "05_assemble").is_dir()
    trace = load_manifest(episode).cleanup
    assert trace is not None and trace.classes == ["junk"] and trace.files == len(JUNK)
    assert trace.freed_mb == round(sum(JUNK.values()) / (1024 * 1024), 1)

    # второй заход по regenerable — трогает только их
    again = cleanup(episode, ("regenerable",), apply=True)
    assert {e.path for e in again.deleted} == set(REGENERABLE)
    assert (episode / "03_video" / "s1_v01.mp4").exists()


def test_keep_and_unknown_classes_are_refused(episode: Path) -> None:
    _fill(episode)
    with pytest.raises(ValueError, match="keep"):
        cleanup(episode, ("keep",))
    with pytest.raises(ValueError, match="nope"):
        cleanup(episode, ("junk", "nope"))


def _clip_scene(edir: Path, projects_root: Path, *, url: str, file: str = "") -> None:
    """Сцена-нарезка, уже кадрированная: текущий актив — `_reframed`, рядом
    докачанный кусок, его недокачанная половина и точный отрезок до кропа."""
    sb = load_storyboard(edir)
    s1 = sb.scenes[0]
    s1.source = SourceSegment(path="sources/vid", start_sec=10.0, end_sec=20.0)
    s1.assets.video = "03_video/s1_reframed.mp4"
    save_storyboard(edir, sb)
    sdir = projects_root / "t" / "sources" / "vid"
    sdir.mkdir(parents=True)
    (sdir / "source.json").write_text(
        json.dumps({"id": "vid", "url": url, "file": file, "duration_sec": 60.0}),
        encoding="utf-8",
    )
    _touch(edir / "03_video" / "s1_src.mp4", 5000)
    _touch(edir / "03_video" / "s1_src_v.mp4", 1000)
    _touch(edir / "03_video" / "s1_src.json")
    _touch(edir / "03_video" / "s1.mp4", 3000)
    _touch(edir / "03_video" / "s1_reframed.mp4", 2000)
    _touch(edir / "03_video" / "s1_reframed.json")


def test_downloaded_needs_a_confirmed_link(episode: Path, projects_root: Path) -> None:
    _clip_scene(episode, projects_root, url="https://www.youtube.com/watch?v=abc")
    got = _classes(episode)
    assert got["downloaded"] == {"03_video/s1_src.mp4", "03_video/s1_src_v.mp4", "03_video/s1.mp4"}
    assert {"03_video/s1_reframed.mp4", "03_video/s1_reframed.json", "03_video/s1_src.json"} <= got[
        "keep"
    ]
    entries = {e.path: e for e in classify(episode)}
    assert entries["03_video/s1_src.mp4"].restorable
    assert entries["03_video/s1_src.mp4"].restore == "https://www.youtube.com/watch?v=abc"

    result = cleanup(episode, ("downloaded",), apply=True)
    assert {e.path for e in result.deleted} == got["downloaded"]
    assert (episode / "03_video" / "s1_reframed.mp4").exists()


def test_downloaded_without_link_stays_and_says_why(episode: Path, projects_root: Path) -> None:
    _clip_scene(episode, projects_root, url="", file=str(projects_root / "gone.mp4"))
    entries = {e.path: e for e in classify(episode)}
    piece = entries["03_video/s1_src.mp4"]
    assert piece.cls == "downloaded" and not piece.restorable and "ни ссылки" in piece.why

    result = cleanup(episode, ("downloaded",), apply=True)
    assert result.deleted == [] and result.chosen == []
    assert {e.path for e in result.blocked} == {
        "03_video/s1_src.mp4",
        "03_video/s1_src_v.mp4",
        "03_video/s1.mp4",
    }
    assert (episode / "03_video" / "s1_src.mp4").exists()

    # локальный файл, который ЕСТЬ, — тоже подтверждение
    (projects_root / "gone.mp4").write_bytes(b"v")
    assert (
        classify(episode) and {e.path: e for e in classify(episode)}["03_video/s1.mp4"].restorable
    )


def test_script_built_episode_reads_the_passport(episode: Path) -> None:
    """Ролик мимо конвейера (tests/t04): src/ по паспорту, seg/ мусор, norm/
    воспроизводимо, скрипты и маски из паспорта остаются."""
    save_materials(
        episode,
        Materials(
            made_by="build.py",
            items=[
                Material(role="clip", title="Дождь", path="src/c01.mp4", link="https://x/1"),
                Material(role="clip", title="Без ссылки", path="src/c02.mp4"),
                Material(role="mask", title="Маска", path="masks/"),
                Material(role="script", title="Сборка", path="build.py"),
            ],
        ),
    )
    for rel in ("src/c01.mp4", "src/c02.mp4", "src/c03.mp4", "seg/000.mp4", "norm/c01.mp4",
                "masks/0000.png", "build.py", "audio.wav", "05_assemble/shots/001.jpg",
                "05_assemble/shots.json", "05_assemble/final.mp4"):  # fmt: skip
        _touch(episode / rel)
    entries = {e.path: e for e in classify(episode)}
    assert entries["src/c01.mp4"].cls == "downloaded" and entries["src/c01.mp4"].restorable
    assert entries["src/c02.mp4"].cls == "downloaded" and not entries["src/c02.mp4"].restorable
    assert entries["src/c03.mp4"].cls == "downloaded" and not entries["src/c03.mp4"].restorable
    assert "нет в паспорте" in entries["src/c03.mp4"].why
    assert entries["seg/000.mp4"].cls == "junk"
    assert entries["norm/c01.mp4"].cls == "regenerable"
    assert entries["05_assemble/shots/001.jpg"].cls == "junk"
    assert entries["05_assemble/shots.json"].cls == "junk"
    for rel in ("masks/0000.png", "build.py", "audio.wav", "05_assemble/materials.json"):
        assert entries[rel].cls == "keep", rel
    assert "паспорта" in entries["build.py"].why

    result = cleanup(episode, ("downloaded",), apply=True)
    assert {e.path for e in result.deleted} == {"src/c01.mp4"}
    assert {e.path for e in result.blocked} == {"src/c02.mp4", "src/c03.mp4"}


def test_locked_episode_is_not_touched(episode: Path) -> None:
    _fill(episode)
    with episode_lock(episode, what="прогон"):
        with pytest.raises(Busy, match="прогон"):
            cleanup(episode, ("junk",), apply=True)
        assert cleanup(episode, ("junk",)).chosen  # сухой прогон читает и под замком
    for rel in JUNK:
        assert (episode / rel).exists(), rel
    assert load_manifest(episode).cleanup is None


def test_bench_and_research_are_off_limits(
    projects_root: Path, research_root: Path, episode: Path
) -> None:
    bench = projects_root / "_bench" / "episodes" / "x"
    _touch(bench / "storyboard.json")
    with pytest.raises(CleanupRefused, match="_bench"):
        classify(bench)
    study = research_root / "chan" / "episodes" / "x"
    _touch(study / "storyboard.json")
    with pytest.raises(CleanupRefused, match="research"):
        classify(study)
    with pytest.raises(CleanupRefused, match="не эпизод"):
        classify(projects_root / "t")


def test_cli_dry_run_then_yes(episode: Path) -> None:
    _fill(episode)
    runner = CliRunner()
    dry = runner.invoke(app, ["episode", "cleanup", "t/e1"])
    assert dry.exit_code == 0, dry.output
    assert "junk" in dry.output and "--yes" in dry.output and "05_assemble/scenes.mp4" in dry.output
    assert (episode / "05_assemble" / "scenes.mp4").exists()

    done = runner.invoke(app, ["episode", "cleanup", "t/e1", "--yes"])
    assert done.exit_code == 0, done.output
    assert f"удалено файлов: {len(JUNK)}" in done.output
    assert not (episode / "05_assemble" / "scenes.mp4").exists()
    assert (episode / "02_images" / "s1_v01.png").exists()  # regenerable не просили
    assert load_manifest(episode).cleanup is not None

    more = runner.invoke(app, ["episode", "cleanup", "t/e1", "--class", "regenerable", "--yes"])
    assert more.exit_code == 0, more.output
    assert not (episode / "02_images" / "s1_v01.png").exists()

    bad = runner.invoke(app, ["episode", "cleanup", "t/e1", "--class", "keep"])
    assert bad.exit_code == 1

    with episode_lock(episode, what="прогон"):
        busy = runner.invoke(app, ["episode", "cleanup", "t/e1", "--yes"])
        assert busy.exit_code == 1 and "уже идёт" in busy.output


def test_api_plan_and_delete(episode: Path, accounts_root: Path) -> None:
    _fill(episode)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    plan = client.get("/api/projects/t/episodes/e1/cleanup")
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert body["classes"]["junk"]["files"] == len(JUNK) and body["applied"] is False
    assert body["selected"] == ["junk"] and len(body["chosen"]) == len(JUNK)
    assert (episode / "05_assemble" / "scenes.mp4").exists()
    assert client.get("/api/projects/t/episodes/zzz/cleanup").status_code == 404

    # чужая страница в браузере — 403, как у всех мутирующих запросов пульта
    cross = client.post(
        "/api/projects/t/episodes/e1/cleanup",
        json={"classes": ["junk"]},
        headers={"Origin": "http://evil.example"},
    )
    assert cross.status_code == 403
    assert (episode / "05_assemble" / "scenes.mp4").exists()

    with episode_lock(episode, what="сборка"):
        busy = client.post("/api/projects/t/episodes/e1/cleanup", json={"classes": ["junk"]})
        assert busy.status_code == 409 and "уже идёт" in busy.json()["detail"]

    bad = client.post("/api/projects/t/episodes/e1/cleanup", json={"classes": ["keep"]})
    assert bad.status_code == 400

    done = client.post("/api/projects/t/episodes/e1/cleanup", json={"classes": ["junk"]})
    assert done.status_code == 200, done.text
    assert done.json()["applied"] is True and len(done.json()["deleted"]) == len(JUNK)
    assert not (episode / "05_assemble" / "scenes.mp4").exists()
    assert (episode / "02_images" / "s1_v01.png").exists()
