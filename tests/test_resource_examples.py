"""Витрина ресурса и связь «ресурс ↔ приём библиотеки»."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videoforge.api.app import create_app
from videoforge.core.resource_examples import (
    RECIPES,
    NeedsSynth,
    NoRecipe,
    find_example,
    render_example,
)
from videoforge.core.resources import KINDS, CaptionStyle, Voice, load_resource

STYLE = """
id: shorts
name: Шортсы
techniques: [caption_font/russo_one, caption_color/white_yellow, captions/nosuch]
preview_format: "9:16"
font: "Russo One"
font_file: RussoOne-Regular.ttf
size_pct: 7.0
uppercase: true
position: center
margin_pct: 0.0
max_words: 3
max_chars: 14
"""


@pytest.fixture
def resources_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированные resources/: ресурсы репозитория в тесты не тянутся."""
    root = tmp_path / "resources"
    (root / "caption_styles").mkdir(parents=True)
    (root / "caption_styles" / "shorts.yaml").write_text(STYLE, encoding="utf-8")
    (root / "voices").mkdir()
    (root / "voices" / "ru.yaml").write_text(
        "id: ru\nvoice: ru-RU-DmitryNeural\n", encoding="utf-8"
    )
    monkeypatch.setenv("VF_RESOURCES_DIR", str(root))
    return root


def test_caption_example_is_rendered_by_the_real_renderer(resources_root: Path) -> None:
    """Клип собирается тем же путём, что финал ролика: стиль → ASS → ffmpeg."""
    style = load_resource("caption_styles", "shorts", CaptionStyle, [resources_root])
    assert find_example(style) is None
    out = render_example("caption_styles", style)
    assert out == resources_root / "caption_styles" / "shorts" / "example.mp4"
    assert out.stat().st_size > 1000
    assert not list(out.parent.glob("*.ass")), "промежуточный ASS остался в витрине"
    example = find_example(style)
    assert example is not None and example.kind == "video" and example.path == out


def test_kinds_without_a_recipe_say_so(resources_root: Path) -> None:
    """У музыки витрины не бывает — это файл; ошибка объясняет, а не падает."""
    style = load_resource("caption_styles", "shorts", CaptionStyle, [resources_root])
    with pytest.raises(NoRecipe):
        render_example("music", style)
    voice = load_resource("voices", "ru", Voice, [resources_root])
    with pytest.raises(NeedsSynth):  # синтез приходит из CLI: core не знает провайдеров
        render_example("voices", voice)
    assert set(RECIPES) <= set(KINDS)


def test_api_shows_example_and_both_sides_of_the_link(resources_root: Path) -> None:
    style = load_resource("caption_styles", "shorts", CaptionStyle, [resources_root])
    render_example("caption_styles", style)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))

    card = next(
        i
        for i in client.get("/api/resources/caption_styles").json()["items"]
        if i["id"] == "shorts"
    )
    assert card["example_kind"] == "video"
    assert card["example_url"].split("?")[0] == "/files/resources/caption_styles/shorts/example.mp4"

    detail = client.get("/api/resources/caption_styles/shorts").json()
    refs = {t["ref"]: t for t in detail["techniques"]}
    assert refs["caption_font/russo_one"]["title"] == "Russo One"
    assert refs["caption_font/russo_one"]["missing"] is False
    assert refs["captions/nosuch"]["missing"] is True  # опечатка видна, а не молчит
    assert detail["example_command"].startswith("videoforge resource gen-example caption_style")

    used = client.get("/api/library/caption_font/russo_one").json()["used_by_resources"]
    assert {(u["kind"], u["id"]) for u in used} == {("caption_styles", "shorts")}
    assert client.get("/api/library/caption_color/cream_on_glass").json()["used_by_resources"] == []


def test_latin_only_font_gets_a_latin_sample() -> None:
    """Шрифт без кириллицы русскую фразу не нарисует — libass молча подставит
    запасной, и витрина покажет чужой шрифт (три мультяшных стиля вышли на ней
    одинаковыми). Такому стилю витрина пишет фразу латиницей."""
    from videoforge.core.resource_examples import has_cyrillic
    from videoforge.paths import fonts_dir

    assert has_cyrillic(fonts_dir() / "RussoOne-Regular.ttf")
    assert not has_cyrillic(fonts_dir() / "TitanOne-Regular.ttf")
    assert has_cyrillic(fonts_dir() / "no_such_font.ttf")  # о пропаже скажет рендер
