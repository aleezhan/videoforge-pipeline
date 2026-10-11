"""Полезные источники: файл из шаблона, проверка записи, порядок, API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videoforge.api.app import create_app
from videoforge.core.links import (
    KINDS,
    create_link,
    list_links,
    load_link,
    sort_key,
    validate_link,
)


def test_scaffold_from_template(links_root: Path) -> None:
    path, problems = create_link(
        "qarqyn",
        title="Панель накрутки",
        url="https://example.com",
        kind="service",
        area="accounts",
        summary="Продаёт просмотры и лайки на пяти площадках.",
        why="Справка о рынке накрутки: чужие аномальные цифры объясняются отсюда.",
        cost="цены только в каталоге услуг",
        source="разговор 09.09.2026",
    )
    assert path == links_root / "qarqyn.yaml" and problems == []
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Полезный источник")  # комментарии шаблона на месте
    link = load_link("qarqyn")
    assert link.kind == "service" and link.area == "accounts"
    assert link.kind_title == "Сервисы" and link.area_title == "Аккаунты и статистика"
    assert link.host == "example.com" and link.added == link.checked
    assert [x.id for x in list_links()] == ["qarqyn"]

    with pytest.raises(FileExistsError):
        create_link("qarqyn", title="x", url="https://example.com")
    with pytest.raises(ValueError):
        create_link("bad-id", title="x", url="https://example.com")
    with pytest.raises(ValueError):
        create_link("x", title="x", url="https://example.com", kind="cinema")
    with pytest.raises(ValueError):
        create_link("x", title="x", url="https://example.com", area="kitchen")


def test_validate_and_sort(links_root: Path) -> None:
    create_link(
        "svc",
        title="Сервис",
        url="https://a.example",
        kind="service",
        summary="есть",
        why="пригодится",
    )
    create_link(
        "art",
        title="Статья",
        url="https://b.example",
        kind="article",
        summary="есть",
        why="пригодится",
    )
    # закладка без «зачем нам» и с ссылкой без схемы — просто ссылка, а не запись
    (links_root / "bare.yaml").write_text(
        "title: Голая ссылка\nurl: example.com\nkind: tool\nsummary: ''\n", encoding="utf-8"
    )
    (links_root / "broken.yaml").write_text("title: [\n", encoding="utf-8")
    items = sorted(list_links(), key=sort_key)
    assert [x.id for x in items] == ["svc", "bare", "art"]  # порядок видов из KINDS
    problems = {x.id: validate_link(x) for x in items}
    assert problems["svc"] == [] and problems["art"] == []
    assert any("http" in p for p in problems["bare"])
    assert any("summary" in p for p in problems["bare"])
    assert any("why" in p for p in problems["bare"])
    assert list(KINDS)[0] == "service" and "other" in KINDS


def test_api(links_root: Path) -> None:
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    assert client.get("/api/links").json()["total"] == 0
    create_link(
        "qarqyn",
        title="Панель накрутки",
        url="https://qarqyn.example/catalog",
        kind="service",
        area="accounts",
        summary="просмотры и лайки",
        why="справка о рынке",
    )
    create_link("mus", title="Музыка", url="https://mus.example", kind="assets", summary="", why="")
    body = client.get("/api/links").json()
    assert [i["id"] for i in body["items"]] == ["qarqyn", "mus"]
    assert body["counts"]["service"] == 1 and body["counts"]["assets"] == 1
    first = body["items"][0]
    assert first["kind_title"] == "Сервисы" and first["area_title"] == "Аккаунты и статистика"
    assert first["host"] == "qarqyn.example" and first["problems"] == []
    assert body["items"][1]["problems"]  # без summary и why
    assert [a["id"] for a in body["areas"]][:2] == ["script", "planner"]
    detail = client.get("/api/links/qarqyn").json()
    assert detail["title"] == "Панель накрутки" and detail["url"].endswith("/catalog")
    assert client.get("/api/links/none").status_code == 404
