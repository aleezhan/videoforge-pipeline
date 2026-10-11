"""Стадия reframe: горизонтальный клип → вертикальный кадр.

Детектор лиц на синтетическом видео ничего не найдёт, и это правильно: здесь
проверяется всё вокруг него — геометрия окна, кусочно-постоянный кроп, ручное
окно человека, идемпотентность и то, что звук переживает кадрирование.
Насколько хорошо YuNet ловит лица, проверяется живьём на настоящем подкасте.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import SOURCE_SEC, make_test_video

from videoforge.core.faces import Face
from videoforge.core.ffmpeg import (
    aspect_size,
    crop_x_expr,
    probe_size,
    probe_stream_duration,
    reframe_video,
    run,
)
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import ReframeWindow, load_storyboard, save_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.stages.reframe import SUFFIX, _crop_size, plan_windows, window_for


def _face(cx: float, w: float = 200.0) -> Face:
    return Face(x=cx - w / 2, y=400.0, w=w, h=w, score=0.9)


def test_detector_downscales_by_the_long_side_and_answers_in_source_pixels() -> None:
    """Кадр уменьшается по БОЛЬШЕЙ стороне: у 4K подкаста это ширина — как и
    было, окна кадрирования не сдвинулись ни на пиксель (сверено 14.09.2026 на
    шести кадрах живого отрезка h08); у вертикального 1080×1920 — высота, а
    до 14.09.2026 такой кадр шёл в детектор целиком. Боксы выходят в пикселях
    исходника, крупные первыми — на этом стоит `window_for`."""
    from videoforge.core.faces import fit, to_source

    assert fit(3840, 2160) == (1 / 3, (1280, 720))
    assert fit(1080, 1920) == (2 / 3, (720, 1280))
    assert fit(640, 360) == (1.0, (640, 360))  # маленький кадр не трогается
    # строка YuNet: x, y, w, h, десять координат точек лица, score последним
    found = [[10, 20, 30, 40, *([0] * 10), 0.7], [100, 200, 300, 400, *([0] * 10), 0.9]]
    faces = to_source(found, 1 / 3)
    assert [round(f.x) for f in faces] == [300, 30]  # крупное первым
    assert (faces[0].x, faces[0].y, faces[0].w, faces[0].h) == pytest.approx((300, 600, 900, 1200))
    assert faces[0].score == 0.9
    assert to_source(None, 0.5) == []  # детектор ничего не нашёл — None, а не []


def test_crop_size_is_the_largest_window_of_the_target_shape() -> None:
    assert _crop_size(3840, 2160, (1080, 1920)) == (1214, 2160)  # 16:9 → 9:16
    assert _crop_size(1080, 1920, (1920, 1080)) == (1080, 608)  # вертикаль → горизонт
    assert _crop_size(1080, 1920, (1080, 1920)) == (1080, 1920)  # уже нужной формы


def test_window_follows_the_biggest_face_and_covers_what_fits() -> None:
    width, crop_w = 3840, 1214
    # одно лицо справа — окно на нём, а не по центру кадра
    x, used = window_for([_face(2610)], width=width, crop_w=crop_w)
    assert used == 1 and x <= 2610 <= x + crop_w
    # два лица рядом влезают в одно окно
    x, used = window_for([_face(1900, 300), _face(2200, 250)], width=width, crop_w=crop_w)
    assert used == 2 and x <= 1900 and 2200 <= x + crop_w
    # общий план: лица по краям, вертикаль их не вмещает — остаётся крупное
    x, used = window_for([_face(830, 160), _face(2940, 140)], width=width, crop_w=crop_w)
    assert used == 1 and x <= 830 <= x + crop_w
    # лиц нет — по центру, и окно не вылезает за кадр
    x, used = window_for([], width=width, crop_w=crop_w)
    assert used == 0 and x == (width - crop_w) // 2 // 2 * 2
    # лицо у самого края: окно упирается в границу, а не уезжает за неё
    x, _ = window_for([_face(50)], width=width, crop_w=crop_w)
    assert x == 0
    x, _ = window_for([_face(3800)], width=width, crop_w=crop_w)
    assert x == (width - crop_w) // 2 * 2


def test_crop_expression_is_piecewise_constant() -> None:
    assert crop_x_expr([(0.0, 300)]) == "300"
    assert crop_x_expr([]) == "(iw-ow)/2"
    expr = crop_x_expr([(0.0, 100), (2.5, 900), (7.0, 400)])
    assert expr == "if(lt(t,2.500),100,if(lt(t,7.000),900,400))"


def test_manual_window_beats_the_detector(tmp_path: Path) -> None:
    """Человек посмотрел и решил — детектор не зовётся вовсе (и не нашёл бы)."""
    clip = make_test_video(tmp_path / "src.mp4")
    plan = plan_windows(
        clip,
        duration=SOURCE_SEC,
        width=3840,
        height=2160,
        crop=(1214, 2160),
        target=(1080, 1920),
        manual=ReframeWindow(x=0.25),
    )
    windows, warnings, by = plan.windows, plan.warnings, plan.by
    assert by == "manual" and not warnings
    assert len(windows) == 1 and windows[0].x == (round(0.25 * 3840 - 607)) // 2 * 2


def test_hidden_cut_is_found_by_the_face_jump(tmp_path: Path) -> None:
    """Склейку между двумя камерами одной студии детектор картинки не видит:
    та же комната, тот же свет. Лицо при этом переезжает через полкадра — по
    прыжку план и делится, иначе один герой двадцать секунд разрезан краем
    (живой шортс podcast_clips/src01_h19, 09.09.2026).

    Детектор подменён: OpenCV на синтетическом видео лиц не найдёт, а проверять
    надо не его, а решение по координатам."""
    clip = make_test_video(tmp_path / "podcast.mp4", seconds=20.0, size="640x360")
    cut_at = 12.0

    def detector(_clip: Path, seconds: list[float]) -> list[list[Face]]:
        return [[_face(1300.0 if t < cut_at else 2900.0)] for t in seconds]

    plan = plan_windows(
        clip,
        duration=20.0,
        width=3840,
        height=2160,
        crop=(1214, 2160),
        target=(1080, 1920),
        manual=None,
        detect=detector,
    )
    windows, warnings, by = plan.windows, plan.warnings, plan.by
    assert by == "yunet" and len(windows) == 2
    assert windows[0].x < windows[1].x  # окно переехало вслед за героем
    # секунда склейки уточнена делением пополам, а не «до следующей пробы»
    assert abs(windows[1].start_sec - cut_at) <= 0.3
    assert any("не увидел" in w for w in warnings)  # человеку сказано, что склейка скрытая


def test_reframe_video_keeps_sound_and_changes_shape(tmp_path: Path) -> None:
    src = tmp_path / "wide.mp4"
    run(
        [
            "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=24:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(src),
        ]
    )  # fmt: skip
    out = tmp_path / "tall.mp4"
    reframe_video(src, out, crop=(606, 1080), offsets=[(0.0, 0), (2.0, 1200)], size=(1080, 1920))
    assert probe_size(out) == (1080, 1920)
    assert (probe_stream_duration(out, "0:a:0") or 0) > 3.0  # звук пережил кадрирование


@pytest.mark.render
def test_reframe_turns_a_clip_into_the_episode_format(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    assert run_episode(clip_episode, mock_providers, RunOptions(only="cut")).ok
    before = load_storyboard(clip_episode).scenes[0]
    assert probe_size(clip_episode / before.assets.video) == (640, 360)

    report = run_episode(clip_episode, mock_providers, RunOptions(only="reframe"))
    assert report.ok, report.reason
    scene = load_storyboard(clip_episode).scenes[0]
    assert scene.assets.video == f"03_video/s01{SUFFIX}.mp4"
    out = clip_episode / scene.assets.video
    assert probe_size(out) == aspect_size("9:16")
    assert (probe_stream_duration(out, "0:a:0") or 0) > 20.0  # речь на месте
    # лиц в синтетическом видео нет — это должно быть сказано, а не спрятано
    assert "reframe" in scene.checks.notes and scene.provenance["reframe"] == "center"


@pytest.mark.render
def test_reframe_is_idempotent_and_redone_after_a_new_cut(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    run_episode(clip_episode, mock_providers, RunOptions(only="reframe"))
    out = clip_episode / "03_video" / f"s01{SUFFIX}.mp4"
    stamp = out.stat().st_mtime_ns

    run_episode(clip_episode, mock_providers, RunOptions(only="reframe"))
    assert out.stat().st_mtime_ns == stamp  # второй раз не пересчитывает

    # момент перерезали — кадрирование устарело вместе с клипом
    run_episode(clip_episode, mock_providers, RunOptions(only="cut", force=True))
    run_episode(clip_episode, mock_providers, RunOptions(only="reframe"))
    assert out.stat().st_mtime_ns != stamp
    assert load_storyboard(clip_episode).scenes[0].assets.video.endswith(f"{SUFFIX}.mp4")


@pytest.mark.render
def test_manual_window_in_the_storyboard_is_used(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    sb = load_storyboard(clip_episode)
    sb.scenes[0].reframe = ReframeWindow(x=0.2)
    save_storyboard(clip_episode, sb)
    assert run_episode(clip_episode, mock_providers, RunOptions(only="reframe")).ok
    scene = load_storyboard(clip_episode).scenes[0]
    assert scene.provenance["reframe"] == "manual"
    assert "reframe" not in scene.checks.notes  # окно от человека вопросов не вызывает


def test_reframe_needs_no_provider_keys() -> None:
    from videoforge.stages.reframe import ReframeStage

    assert ReframeStage.needs == ()
    assert ReframeStage().estimate_cost(None) == 0.0  # type: ignore[arg-type]


@pytest.mark.parametrize("width,height", [(1080, 1920), (1080, 1080)])
def test_already_vertical_clip_is_left_alone(
    clip_episode: Path, mock_providers: ProviderSet, width: int, height: int, tmp_path: Path
) -> None:
    run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    sb = load_storyboard(clip_episode)
    tall = clip_episode / "03_video" / "tall.mp4"
    run(
        [
            "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=24:duration=3",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(tall),
        ]
    )  # fmt: skip
    sb.scenes[0].assets.video = "03_video/tall.mp4"
    sb.scenes[0].duration_sec = 3.0
    save_storyboard(clip_episode, sb)
    report = run_episode(clip_episode, mock_providers, RunOptions(only="reframe"))
    assert report.ok
    # 1:1 внутри 9:16 всё же режется, а 9:16 — уже нужной формы
    if (width, height) == (1080, 1920):
        assert load_storyboard(clip_episode).scenes[0].assets.video == "03_video/tall.mp4"
