"""Чистая логика remotion-провайдера: разбор motion_prompt и длительности.

Сам рендер Node не гоняем: тесты не зависят от npm install, как платные
провайдеры не зависят от ключей. Реальный клип — руками через doctor + run.
"""

from pathlib import Path

import pytest

from videoforge.providers.base import ProviderError
from videoforge.providers.remotion_video import (
    STRENGTH_DEFAULT,
    STRENGTH_FAST,
    STRENGTH_SLOW,
    RemotionVideo,
    parse_motion,
)


def test_parse_motion_kinds() -> None:
    assert parse_motion("one single slow push in toward the planet").motion == "push_in"
    assert parse_motion("camera slowly pulls back to reveal the map").motion == "pull_out"
    assert parse_motion("gentle pan left across the valley").motion == "pan_left"
    assert parse_motion("camera pans right following the river").motion == "pan_right"
    assert parse_motion("slow tilt up from the roots to the crown").motion == "tilt_up"
    assert parse_motion("locked static shot, no camera movement").motion == "static"


def test_parse_motion_priority_pull_over_push() -> None:
    # «zoom out» не должен прочитаться как «zoom in»
    assert parse_motion("slow zoom out from the scene").motion == "pull_out"


def test_parse_motion_default_is_push_in() -> None:
    parsed = parse_motion("particles drift over the illustration")
    assert parsed.motion == "push_in"
    assert parsed.strength == STRENGTH_DEFAULT


def test_parse_motion_tempo() -> None:
    assert parse_motion("slow push in").strength == STRENGTH_SLOW
    assert parse_motion("fast push in").strength == STRENGTH_FAST


def test_pick_duration_is_exact_ceiling() -> None:
    video = RemotionVideo()
    assert video.pick_duration(6.2) == 7
    assert video.pick_duration(6.0) == 6
    assert video.pick_duration(14.9) == 15  # потолка провайдера нет
    assert video.pick_duration(0.3) == 1


def test_price_is_zero() -> None:
    assert RemotionVideo().price_for(60) == 0.0


def test_missing_project_is_honest_error(tmp_path: Path) -> None:
    video = RemotionVideo(project_dir=tmp_path / "nowhere")
    with pytest.raises(ProviderError, match="нет Remotion-проекта"):
        video._check_project()


def test_missing_node_modules_names_the_fix(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    video = RemotionVideo(project_dir=tmp_path)
    with pytest.raises(ProviderError, match="npm install"):
        video._check_project()
