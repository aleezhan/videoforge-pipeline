from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from videoforge.core.storyboard import Scene, Storyboard, load_storyboard, save_storyboard
from videoforge.paths import ROOT


def test_roundtrip(tmp_path: Path) -> None:
    sb = Storyboard(project_id="p", scenes=[Scene(id="a", order=2), Scene(id="b", order=1)])
    save_storyboard(tmp_path, sb)
    loaded = load_storyboard(tmp_path)
    assert [s.id for s in loaded.ordered_scenes()] == ["b", "a"]
    assert loaded.scenes[0].checks.image == "pending"


def test_scene_ids_unique() -> None:
    with pytest.raises(ValidationError):
        Storyboard(project_id="p", scenes=[Scene(id="a", order=1), Scene(id="a", order=2)])


def test_demo_storyboard_is_valid() -> None:
    sb = load_storyboard(ROOT / "projects" / "demo" / "episodes" / "map")
    assert len(sb.scenes) == 2
    assert sb.resources.character == "timur"
    assert sb.project_id == "demo" and sb.episode_id == "map"
