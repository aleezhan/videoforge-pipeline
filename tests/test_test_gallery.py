"""Проект-полигон (`kind: test`) и галерея его роликов в пульте.

Смысл проверки: человек заходит в раздел «Тесты» и сразу видит, какие ролики
собраны и что каждым проверяли. Значит, карточка обязана нести обложку,
вопрос из `check.md` и длительность, а сам полигон не должен притворяться
живым каналом в списке проектов.
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_test_video
from fastapi.testclient import TestClient

from videoforge.api import data
from videoforge.api.app import create_app
from videoforge.core.project import ProjectSpec, create_episode, create_project
from videoforge.core.storyboard import load_storyboard, save_storyboard


def _test_project(projects_root: Path) -> Path:
    create_project(ProjectSpec(id="tp", name="Тестовые", kind="test"))
    edir = create_episode(
        "tp", "t01", title="Субтитры на снегу", check="читаются ли субтитры на светлом фоне"
    )
    out = edir / "05_assemble"
    out.mkdir(exist_ok=True)
    make_test_video(out / "final.mp4", seconds=6.0, size="1080x1920")
    sb = load_storyboard(edir)
    sb.final.video = "05_assemble/final.mp4"
    sb.final.duration_sec = 6.0
    save_storyboard(edir, sb)
    return edir


def test_check_file_is_written_by_hand_and_read_by_the_gallery(
    episode: Path, projects_root: Path
) -> None:
    edir = _test_project(projects_root)
    assert (edir / "check.md").exists()

    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    body = client.get("/api/tests").json()
    assert [p["id"] for p in body["projects"]] == ["tp"]
    card = body["items"][0]
    assert card["ref"] == "tp/t01" and card["title"] == "Субтитры на снегу"
    # подпись карточки — сам вопрос из check.md, а не его заголовок
    assert card["question"] == "читаются ли субтитры на светлом фоне"
    assert card["duration_sec"] == 6.0 and card["aspect_ratio"] == "9:16"
    assert card["video_url"].split("?")[0].endswith("/final.mp4")
    # обложка снимается один раз и лежит рядом с роликом
    assert card["poster_url"].split("?")[0].endswith("/final.poster.jpg")
    assert (edir / "05_assemble" / "final.poster.jpg").exists()

    # человек правит check.md руками — галерея читает файл, а не свою копию
    (edir / "check.md").write_text("держит ли лицо на 5 кадрах\n", encoding="utf-8")
    card = client.get("/api/tests").json()["items"][0]
    assert card["question"] == "держит ли лицо на 5 кадрах"


def test_unfinished_test_is_listed_apart(projects_root: Path) -> None:
    create_project(ProjectSpec(id="tp", name="Тестовые", kind="test"))
    create_episode("tp", "t02", title="Ещё не собран")
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    body = client.get("/api/tests").json()
    assert body["items"] == [] and [c["ref"] for c in body["pending"]] == ["tp/t02"]
    assert body["total"] == 1


def test_test_project_is_not_a_live_channel(
    episode: Path, projects_root: Path, accounts_root: Path
) -> None:
    """Даже с аккаунтом полигон остаётся полигоном: его ролики смотрят, а не
    публикуют, и в «живых» ему делать нечего."""
    from videoforge.core.accounts import create_account

    _test_project(projects_root)
    create_account("tp_yt", network="youtube", project="tp", handle="@tp")
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    items = {p["id"]: p for p in client.get("/api/projects").json()["items"]}
    assert items["tp"]["kind"] == "test" and items["tp"]["status"] == "demo"
    assert items["t"]["kind"] == "channel"
    assert client.get("/api/projects/tp").json()["status"] == "demo"


def test_poster_is_not_redone_while_the_video_is_the_same(projects_root: Path) -> None:
    edir = _test_project(projects_root)
    video = edir / "05_assemble" / "final.mp4"
    first = data.poster_path(video)
    assert first is not None
    stamp = first.stat().st_mtime_ns
    assert data.poster_path(video) == first
    assert first.stat().st_mtime_ns == stamp  # второй заход ffmpeg не гоняет
