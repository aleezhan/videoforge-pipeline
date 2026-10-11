"""Портфолио персонажа: генерация ядра, конвенция подпапок, add-ref --as."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core.resources import Character, add_reference_image, create_resource, load_resource
from videoforge.providers.mock import MockImage
from videoforge.stages.portfolio import (
    CANDIDATES_DIR,
    CORE_SHOT_NAMES,
    generate_shots,
    missing_core_shots,
)
from videoforge.stages.prompts import resource_references


@pytest.fixture
def hero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Character:
    monkeypatch.setenv("VF_RESOURCES_DIR", str(tmp_path))
    create_resource("character", "hero", name="Герой")
    (tmp_path / "characters" / "hero.yaml").write_text(
        'id: hero\nprompt_block: "a tall hero with a scar over the left eyebrow"\n',
        encoding="utf-8",
    )
    return load_resource("characters", "hero", Character)


def test_candidates_go_to_subfolder_and_are_not_references(hero: Character) -> None:
    results = generate_shots(MockImage(), hero, ["face_front"], count=3)
    names = [r.path.name for r in results]
    assert names == ["face_front_v01.png", "face_front_v02.png", "face_front_v03.png"]
    assert all(r.path.parent.name == CANDIDATES_DIR for r in results)
    assert sum(r.cost_usd for r in results) == 0.0
    # кандидаты — не ядро: в сцены не идут, ядро всё ещё пустое
    assert hero.reference_paths() == []
    assert missing_core_shots(hero) == list(CORE_SHOT_NAMES)
    # повторный запуск продолжает нумерацию, а не перезаписывает
    again = generate_shots(MockImage(), hero, ["face_front"])
    assert again[0].path.name == "face_front_v04.png"


def test_accepted_face_becomes_reference_for_next_shots(hero: Character) -> None:
    seed = generate_shots(MockImage(), hero, ["face_front"])[0]
    dest = add_reference_image("character", "hero", seed.path, rename="face_front")
    assert dest == hero.folder() / "face_front.png"
    assert "face_front" not in missing_core_shots(hero)
    # имя кадра — часть подписи референса
    assert [r.label for r in resource_references("character", hero)] == [
        "character 'hero' (face_front)"
    ]
    # следующие кадры ядра генерируются с одобренным лицом как референсом
    body = generate_shots(MockImage(), hero, ["body_full_front"])[0]
    assert body.meta["references"] == 1


def test_add_ref_as_refuses_to_overwrite(hero: Character) -> None:
    seed = generate_shots(MockImage(), hero, ["face_front"], count=2)
    add_reference_image("character", "hero", seed[0].path, rename="face_front")
    with pytest.raises(FileExistsError):
        add_reference_image("character", "hero", seed[1].path, rename="face_front")


def test_generate_unknown_shot_fails_before_spending(hero: Character) -> None:
    with pytest.raises(ValueError):
        generate_shots(MockImage(), hero, ["face_front", "nope"])
    assert not (hero.folder() / CANDIDATES_DIR).exists() or not list(
        (hero.folder() / CANDIDATES_DIR).iterdir()
    )
