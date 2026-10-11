"""Каст сцены: персонажи/места/предметы, портфолио референсов, проектные ресурсы."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core.manifest import load_manifest
from videoforge.core.resources import (
    Character,
    Prop,
    add_reference_image,
    create_resource,
    load_resource,
    parse_entity_ref,
    search_dirs,
)
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard, save_storyboard
from videoforge.providers.base import ProviderSet


def test_parse_entity_ref() -> None:
    assert parse_entity_ref("characters/timur") == ("characters", "timur")
    assert parse_entity_ref("prop/compass") == ("props", "compass")
    assert parse_entity_ref("location/forest") == ("locations", "forest")
    with pytest.raises(ValueError):
        parse_entity_ref("styles/pixar3d")  # стиль — не участник каста
    with pytest.raises(ValueError):
        parse_entity_ref("timur")


def test_portfolio_autodiscovery_and_add_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VF_RESOURCES_DIR", str(tmp_path))
    create_resource("prop", "compass", name="Компас")
    assert load_resource("props", "compass", Prop).reference_paths() == []

    img = tmp_path / "shot.png"
    img.write_bytes(b"png")
    dest = add_reference_image("prop", "compass", img)
    assert dest == tmp_path / "props" / "compass" / "shot.png"
    dest2 = add_reference_image("props", "compass", img)  # имя занято → суффикс
    assert dest2.name == "shot_2.png"

    names = [p.name for p in load_resource("props", "compass", Prop).reference_paths()]
    assert names == ["shot.png", "shot_2.png"]

    with pytest.raises(ValueError):
        add_reference_image("style", "x", img)  # у стиля портфолио нет


def test_project_resource_overrides_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VF_RESOURCES_DIR", str(tmp_path / "global"))
    proj = tmp_path / "proj"
    create_resource("character", "hero", name="Глобальный")
    create_resource("character", "hero", name="Проектный", base=proj / "resources")

    assert load_resource("characters", "hero", Character, search_dirs(proj)).name == "Проектный"
    assert load_resource("characters", "hero", Character).name == "Глобальный"


def test_scene_cast_flows_into_prompt_and_refs(episode: Path, mock_providers: ProviderSet) -> None:
    proj_res = episode.parent.parent / "resources"
    create_resource("prop", "compass", name="Компас", base=proj_res)
    (proj_res / "props" / "compass.yaml").write_text(
        'id: compass\nprompt_block: "an old brass compass with a red needle"\n'
        'checklist: ["компас латунный с красной стрелкой"]\n',
        encoding="utf-8",
    )
    (proj_res / "props" / "compass" / "ref.png").write_bytes(b"png")

    sb = load_storyboard(episode)
    sb.scenes[0].cast = ["characters/timur", "props/compass"]
    save_storyboard(episode, sb)

    report = run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, raise_errors=True)
    )
    assert report.ok, report.reason

    prompt = (episode / "02_images" / "s1.prompt.txt").read_text(encoding="utf-8")
    assert "brass compass" in prompt  # блок предмета в промпте
    assert "Timur" in prompt  # блок персонажа в промпте
    assert "must match their reference images" in prompt  # у компаса есть референс

    prompt_s2 = (episode / "02_images" / "s2.prompt.txt").read_text(encoding="utf-8")
    assert "brass compass" not in prompt_s2  # пустой cast → в кадре никого
    assert "Timur" not in prompt_s2  # герой попадает в кадр только по явному cast


def test_unknown_cast_fails_before_spending(episode: Path, mock_providers: ProviderSet) -> None:
    sb = load_storyboard(episode)
    sb.scenes[0].cast = ["props/nope"]
    save_storyboard(episode, sb)

    report = run_episode(episode, mock_providers, RunOptions(only="images", auto_approve=True))
    assert report.reason and "каст" in report.reason
    assert load_manifest(episode).stages["images"].status == "failed"
    images_dir = episode / "02_images"
    assert not images_dir.exists() or not list(images_dir.glob("*.png"))
