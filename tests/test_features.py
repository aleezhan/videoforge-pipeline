"""Планы и возможности: файл из шаблона, статусы, проверка, API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videoforge.api.app import create_app
from videoforge.core.features import (
    AREAS,
    STATUSES,
    create_feature,
    list_features,
    load_feature,
    set_status,
    sort_key,
    validate_feature,
)


def test_scaffold_from_template(features_root: Path) -> None:
    path, problems = create_feature(
        "blender_control",
        title="Болванка из Blender задаёт движение",
        area="video",
        summary="Кубики и камера из Blender → готовое видео повторяет движение точь-в-точь.",
        gives="управление камерой без лотереи промпта",
        cost="работа человека в Blender на каждую сцену",
        source="разговор 08.09.2026",
    )
    assert path == features_root / "blender_control.yaml" and problems == []
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Возможность инструмента")  # комментарии шаблона на месте
    feat = load_feature("blender_control")
    assert feat.status == "idea" and feat.area == "video" and feat.is_open
    assert feat.area_title == "Видео" and feat.status_title == "идея"
    assert feat.recorded and feat.done_at is None
    assert [f.id for f in list_features()] == ["blender_control"]

    with pytest.raises(FileExistsError):
        create_feature("blender_control", title="x")
    with pytest.raises(ValueError):
        create_feature("bad-id", title="x")
    with pytest.raises(ValueError):
        create_feature("x", title="x", area="kitchen")
    with pytest.raises(ValueError):
        create_feature("x", title="x", status="maybe")


def test_status_change_keeps_comments(features_root: Path) -> None:
    create_feature("longer_clips", title="Клипы длиннее", area="video", summary="8–10 с")
    path, problems = set_status("longer_clips", "done")
    feat = load_feature("longer_clips")
    assert feat.status == "done" and feat.done_at == feat.recorded
    # done без how_to_use — умеем, а как включить, неизвестно
    assert any("how_to_use" in p for p in problems)
    text = path.read_text(encoding="utf-8")
    assert "# idea | planned" in text  # комментарий у status: остался
    assert "# Возможность инструмента" in text

    set_status("longer_clips", "planned")
    assert load_feature("longer_clips").done_at is None
    with pytest.raises(ValueError):
        set_status("longer_clips", "someday")
    with pytest.raises(FileNotFoundError):
        set_status("nobody", "done")


def test_validate_and_sort(features_root: Path) -> None:
    create_feature("a_done", title="A", area="ui", status="done", summary="есть")
    create_feature("b_idea", title="B", area="video", summary="идея")
    create_feature("c_progress", title="C", area="edit", status="in_progress", summary="делаем")
    (features_root / "d_dropped.yaml").write_text(
        "title: D\nstatus: dropped\narea: video\nsummary: нет\n", encoding="utf-8"
    )
    (features_root / "broken.yaml").write_text("title: [\n", encoding="utf-8")
    items = sorted(list_features(), key=sort_key)
    assert [f.id for f in items] == ["c_progress", "b_idea", "a_done", "d_dropped"]
    problems = {f.id: validate_feature(f) for f in items}
    assert any("how_to_use" in p for p in problems["a_done"])
    assert any("причины" in p for p in problems["d_dropped"])
    assert problems["b_idea"] == [] and problems["c_progress"] == []
    assert set(STATUSES) == {"idea", "planned", "in_progress", "done", "dropped"}
    assert "video" in AREAS and "other" in AREAS


def test_topic_groups_records_inside_an_area(features_root: Path) -> None:
    """Тема — слово из словаря: опечатка видна как проблема записи, а не как
    вторая карточка на карте; пустой `topic:` — «темы нет», не ошибка."""
    path, problems = create_feature(
        "cover", title="Обложка", area="publish", topic="thumbnail", summary="кадр"
    )
    assert problems == [] and load_feature("cover").topic_title == "Обложка"
    assert "topic:" in path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        create_feature("x", title="x", topic="kitchen")
    (features_root / "odd.yaml").write_text(
        "title: O\narea: publish\ntopic: kitchen\nsummary: есть\n", encoding="utf-8"
    )
    assert any("тема" in p for p in validate_feature(load_feature("odd")))
    (features_root / "bare.yaml").write_text(
        "title: B\narea: publish\ntopic:\nsummary: есть\n", encoding="utf-8"
    )
    assert load_feature("bare").topic == "" and validate_feature(load_feature("bare")) == []

    body = TestClient(create_app(ui_dist=Path("nonexistent"))).get("/api/features").json()
    assert {"id": "thumbnail", "title": "Обложка"} in body["topics"]
    assert {i["id"]: i["topic_title"] for i in body["items"]}["cover"] == "Обложка"


def test_api(features_root: Path) -> None:
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    assert client.get("/api/features").json()["total"] == 0
    create_feature("swap", title="Замена предмета", area="video", summary="кот вместо мальчика")
    create_feature("ui", title="Пульт", area="ui", status="done", summary="есть")
    body = client.get("/api/features").json()
    assert [i["id"] for i in body["items"]] == ["swap", "ui"]
    assert body["counts"]["idea"] == 1 and body["counts"]["done"] == 1
    assert body["items"][0]["area_title"] == "Видео" and body["items"][0]["status_title"] == "идея"
    assert body["items"][1]["problems"]  # done без how_to_use
    assert [a["id"] for a in body["areas"]][:2] == ["script", "planner"]
    detail = client.get("/api/features/swap").json()
    assert detail["title"] == "Замена предмета" and detail["problems"] == []
    assert client.get("/api/features/none").status_code == 404
