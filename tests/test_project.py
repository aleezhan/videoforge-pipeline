"""Проект (канал) и эпизод: скаффолд из шаблонов, наследование настроек, ресурсы."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core.manifest import load_manifest
from videoforge.core.project import (
    ProjectSpec,
    create_episode,
    create_project,
    list_episodes,
    list_projects,
    load_project,
    missing_resources,
    parse_ref,
    platform_defaults,
    resolve_episode,
)
from videoforge.core.resources import (
    Character,
    Voice,
    create_resource,
    load_resource,
    rename_resource,
    search_dirs,
)
from videoforge.core.runner import project_context
from videoforge.core.storyboard import ResourceRefs, load_storyboard


def test_project_scaffold_is_valid_and_commented(projects_root: Path) -> None:
    pdir, warnings = create_project(
        ProjectSpec(
            id="country_telling",
            name="Рассказы о странах",
            description="Короткие истории про страны",
            channel_name="Страны с Тимуром",
            platforms=["tiktok", "youtube_shorts"],
            character="timur",
            style="pixar3d",
            voice="ru_male",
            niche="kids_adventure",
            budget_per_episode_usd=3.0,
        )
    )
    assert (pdir / "project.yaml").exists() and (pdir / "rules.md").exists()
    assert (pdir / "episodes").is_dir()
    assert warnings == []
    assert "# Проект = канал" in (pdir / "project.yaml").read_text(encoding="utf-8")

    cfg = load_project("country_telling")
    assert cfg.name == "Рассказы о странах"
    assert cfg.channel.platforms == ["tiktok", "youtube_shorts"]
    assert cfg.video.aspect_ratio == "9:16" and cfg.video.target_duration_sec == 60
    assert cfg.resources.character == "timur" and cfg.budget_per_episode_usd == 3.0
    assert cfg.scenes_estimate == 10
    assert list_projects() == ["country_telling"]
    assert "Рассказы о странах" in (pdir / "rules.md").read_text(encoding="utf-8")


def test_project_warns_about_missing_resources(projects_root: Path) -> None:
    _, warnings = create_project(ProjectSpec(id="p", character="nobody", style="pixar3d"))
    assert len(warnings) == 1 and "characters/nobody" in warnings[0]


def test_project_local_resource_is_not_missing(projects_root: Path) -> None:
    """Проверка ссылок смотрит туда же, куда конвейер: проектный ресурс — не пропажа."""
    pdir, _ = create_project(ProjectSpec(id="p"))
    local = pdir / "resources" / "caption_styles"
    local.mkdir(parents=True)
    (local / "own.yaml").write_text("id: own\nname: own\n", encoding="utf-8")
    refs = ResourceRefs(caption_style="own")
    assert missing_resources(refs) == ["caption_styles/own"]
    assert missing_resources(refs, search_dirs(pdir)) == []


def test_project_rejects_unknown_platform(projects_root: Path) -> None:
    with pytest.raises(ValueError):
        create_project(ProjectSpec(id="p", platforms=["vk"]))


def test_platform_defaults() -> None:
    assert platform_defaults(["youtube_shorts", "tiktok"]) == ("9:16", 60)
    assert platform_defaults(["youtube"]) == ("16:9", 900)
    assert platform_defaults(["youtube", "tiktok"])[0] == "9:16"  # разные форматы → вертикаль


def test_episode_inherits_project(projects_root: Path) -> None:
    create_project(
        ProjectSpec(id="p", platforms=["youtube"], character="timur", budget_per_episode_usd=2.5)
    )
    edir = create_episode("p", "ep001", title="Япония", brief="про суши")
    assert edir == projects_root / "p" / "episodes" / "ep001"
    sb = load_storyboard(edir)
    assert sb.project_id == "p" and sb.episode_id == "ep001"
    assert sb.aspect_ratio == "16:9" and sb.resources.character == "timur"
    assert sb.title == "Япония" and sb.brief == "про суши"
    m = load_manifest(edir)
    assert m.budget_usd == 2.5 and m.episode_id == "ep001"
    assert list_episodes("p") == ["ep001"]
    assert resolve_episode("p/ep001") == edir

    cfg, rules = project_context(edir)
    assert cfg and cfg.id == "p"
    assert "## Информация" in rules

    with pytest.raises(FileExistsError):
        create_episode("p", "ep001")


def test_parse_ref() -> None:
    assert parse_ref("a/b") == ("a", "b")
    assert parse_ref("a\\b") == ("a", "b")
    for bad in ("a", "a/", "/b", "a/b/c"):
        with pytest.raises(ValueError):
            parse_ref(bad)


def test_resource_scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VF_RESOURCES_DIR", str(tmp_path))
    path = create_resource("character", "hero", name="Герой")
    assert path == tmp_path / "characters" / "hero.yaml"
    assert (tmp_path / "characters" / "hero").is_dir()
    obj = load_resource("characters", "hero", Character)
    assert obj.name == "Герой" and obj.reference_images == [] and obj.checklist == []
    with pytest.raises(FileExistsError):
        create_resource("characters", "hero")
    with pytest.raises(ValueError):
        create_resource("robot", "x")
    for kind in ("style", "voice", "niche"):
        create_resource(kind, "x")


def test_resource_rename_keeps_id_and_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Переименование правит одну строку: id и комментарии шаблона на месте.

    Комментарии проверяются не для красоты — YAML ресурса человек правит
    руками, и `model_dump` вместо точечной правки стёр бы всю подсказку."""
    monkeypatch.setenv("VF_RESOURCES_DIR", str(tmp_path))
    create_resource("voice", "ava", name="Ава")
    path = rename_resource("voice", "ava", "  Рассказчик шортсов  ")
    text = path.read_text(encoding="utf-8")
    obj = load_resource("voices", "ava", Voice)
    assert obj.name == "Рассказчик шортсов" and obj.id == "ava"
    assert "# Голос — ресурс" in text and "ShortName из списка voices" in text
    assert text.count("name:") == 1
    with pytest.raises(ValueError):
        rename_resource("voice", "ava", "   ")
    with pytest.raises(FileNotFoundError):
        rename_resource("voice", "nobody", "Икс")
    assert load_resource("voices", "ava", Voice).name == "Рассказчик шортсов"
