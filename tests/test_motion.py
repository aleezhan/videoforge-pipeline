"""Профиль движения клипа: математика, замер ffmpeg, мёртвый клип в стадии video,
строка motion в материале монтажёра, пик в линте."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from videoforge.core.editplan import EditPlan, SceneEdit, Segment, sources_path
from videoforge.core.ffmpeg import aspect_size, image_to_video, run
from videoforge.core.motion import (
    DEAD_ENERGY,
    MotionProfile,
    load_or_measure,
    motion_energy,
    motion_path,
)
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, SceneAssets, Storyboard, load_storyboard
from videoforge.providers.base import GenResult, ProviderSet
from videoforge.stages.edit_lint import lint_plan

OPTS = RunOptions(auto_approve=True, raise_errors=True)


# ------------------------------------------------------------------ математика


def test_profile_shapes_dead_steady_and_peaked() -> None:
    dead = MotionProfile(energy=[0.03, 0.04, 0.04])
    assert dead.is_dead() and not dead.is_steady()
    assert dead.describe() == "static (no motion)"
    assert dead.quiet_windows() == [(0.0, 1.5)] and dead.peak_window() is None

    steady = MotionProfile(energy=[0.5] * 8)
    assert steady.is_steady() and not steady.is_dead()
    assert steady.describe().startswith("steady") and steady.peak_window() is None

    peaked = MotionProfile(energy=[0.1, 0.1, 0.2, 1.0, 1.2, 1.1, 0.2])
    assert not peaked.is_steady()
    assert peaked.peak_window() == (1.5, 3.0)
    assert peaked.quiet_windows() == [(0.0, 1.5), (3.0, 3.5)]
    assert peaked.describe() == "quiet 0–1.5s, 3–3.5s, peak 1.5–3s"
    assert MotionProfile().describe() == "not measured"


# ------------------------------------------------------------------ замер ffmpeg


def test_motion_energy_tells_static_from_ken_burns(tmp_path: Path) -> None:
    static = tmp_path / "static.mp4"
    run(["-f", "lavfi", "-i", "color=c=gray:s=320x240:d=2:r=24", "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-preset", "ultrafast", str(static)])  # fmt: skip
    assert MotionProfile(energy=motion_energy(static)).is_dead()

    still = tmp_path / "still.png"
    img = Image.new("RGB", (320, 240), (200, 120, 60))
    for x in range(0, 320, 40):  # текстура: у ровной заливки наезд ничего не меняет
        img.paste((30, 60, 120), (x, 0, x + 20, 240))
    img.save(still)
    moving = tmp_path / "moving.mp4"
    image_to_video(still, moving, duration=3, size=(320, 240))
    profile = load_or_measure(moving)
    assert not profile.is_dead() and profile.peak >= DEAD_ENERGY
    assert motion_path(moving).exists()  # профиль лежит рядом с клипом
    assert load_or_measure(moving) == profile  # второй раз — из файла


# ------------------------------------------------------------------ линт: пик не режется


def _storyboard() -> Storyboard:
    scenes = [
        Scene(
            id=f"s{i}",
            order=i,
            duration_sec=5.0,
            narration=f"слово{i}.",
            assets=SceneAssets(video=f"03_video/s{i}.mp4"),
            measured_sec={"audio": 4.0},
        )
        for i in range(1, 3)
    ]
    return Storyboard(project_id="t", scenes=scenes)


def _lint(plan: EditPlan):  # noqa: ANN202 — тип из модуля линта
    peaked = MotionProfile(energy=[0.1, 0.1, 1.0, 1.1, 0.2, 0.2, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    return lint_plan(
        plan,
        _storyboard(),
        duration_of=lambda rel: 6.0,
        style_exists=lambda rid: True,
        track_exists=lambda rid: True,
        motion_of=lambda rel: peaked,  # пик 1–2 с — внутри коробки 5 с
    )


def test_lint_warns_when_segments_skip_the_motion_peak() -> None:
    cut_out = EditPlan(
        scenes=[SceneEdit(scene_id="s1", segments=[Segment(in_sec=2.5, out_sec=6.0)])]
    )
    assert any(f.code == "motion_peak_cut" for f in _lint(cut_out).warnings)
    kept = EditPlan(scenes=[SceneEdit(scene_id="s1", segments=[Segment(in_sec=0.5, out_sec=5.5)])])
    assert not any(f.code == "motion_peak_cut" for f in _lint(kept).findings)
    assert not any(f.code == "motion_peak_cut" for f in _lint(EditPlan()).findings)  # умолчание


# ------------------------------------------------------------------ конвейер


@pytest.mark.render
def test_pipeline_measures_motion_and_names_dead_clips(
    episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(
        episode, mock_providers, RunOptions(stop="edit", auto_approve=True, raise_errors=True)
    )
    sb = load_storyboard(episode)
    s1 = sb.ordered_scenes()[0]
    assert s1.assets.video and motion_path(episode / s1.assets.video).exists()
    assert "motion" not in s1.checks.notes  # Ken Burns заглушки — живой клип
    sources = json.loads(sources_path(episode).read_text(encoding="utf-8"))
    assert all(c["motion"] for c in sources["clips"])  # профиль дошёл до монтажёра

    # генеративный провайдер отдал статичный кадр — стадия video называет сцену
    def static_animate(
        image: Path,
        prompt: str,
        *,
        duration_sec: int,
        aspect_ratio: str,
        out_path: Path,
        **_: object,  # chain, motion_reference — заглушке они не нужны
    ) -> GenResult:
        w, h = aspect_size(aspect_ratio)
        src = f"color=c=gray:s={w}x{h}:d={duration_sec}:r=24"
        run(["-f", "lavfi", "-i", src, "-pix_fmt", "yuv420p", "-c:v", "libx264",
             "-preset", "ultrafast", str(out_path)])  # fmt: skip
        return GenResult(out_path, 0.0, {})

    mock_providers.video.animate = static_animate  # type: ignore[method-assign]
    report = run_episode(
        episode,
        mock_providers,
        RunOptions(only="video", auto_approve=True, raise_errors=True, force=True),
    )
    assert report.ok, report.reason
    _, result = report.executed[0]
    assert "без движения" in result.message and "s1" in result.message
    notes = load_storyboard(episode).ordered_scenes()[0].checks.notes
    assert "motion" in notes and "без движения" in notes["motion"]
