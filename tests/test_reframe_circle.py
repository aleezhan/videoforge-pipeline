"""The `circle` reframe layout: geometry, square selection from the frame, a
per-episode stretch of the background clip, and rendering."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from videoforge.core.circle import (
    DEFAULT_Y,
    Circle,
    background_start,
    circle_on,
    circle_video,
    face_center,
    square_crop,
)
from videoforge.core.ffmpeg import frame_at, probe_size, probe_stream_duration, run
from videoforge.core.resources import KINDS, Background, create_resource, load_resource

OUT = (1080, 1920)


def test_circle_sits_above_the_middle_like_the_approved_probe() -> None:
    """Числа пробы t10, которую одобрил владелец: кружок 880 px, центр на 860 —
    низ под ним свободен, его закрывает интерфейс площадки."""
    assert circle_on(OUT) == Circle(x=100, y=420, d=880, ring=12)
    small = circle_on((540, 960))
    assert small.d == 440 and small.x == 50 and small.y + small.d // 2 == 430


def test_square_is_cut_above_the_middle_of_a_vertical_clip() -> None:
    """Вертикальный ролик: квадрат во всю ширину, центр на DEFAULT_Y высоты —
    на пробе квадрат по центру срезал головы собак."""
    side, x, y = square_crop(720, 1228)
    assert (side, x) == (720, 0)
    assert y == round(DEFAULT_Y * 1228 - 360)
    # центр задан — квадрат идёт за ним, но не выезжает за кадр
    assert square_crop(720, 1228, y_share=0.0)[2] == 0
    assert square_crop(720, 1228, y_share=1.0)[2] == 1228 - 720
    # горизонтальный — квадрат во всю высоту посередине ширины
    assert square_crop(1920, 1080) == (1080, 420, 0)
    assert square_crop(1920, 1080, x_share=0.9)[1] == 1920 - 1080
    # zoom — квадрат меньше, крупнее на выходе
    assert square_crop(720, 1228, zoom=2.0)[0] == 360


def test_face_center_is_the_median_of_the_biggest_face_per_probe() -> None:
    frames = [
        [(100.0, 300.0, 50.0), (500.0, 900.0, 900.0)],  # крупное — второе
        [],
        [(520.0, 880.0, 1000.0)],
        [(480.0, 920.0, 800.0)],
    ]
    cx, cy = face_center(frames, width=1000, height=2000)  # type: ignore[misc]
    assert cx == pytest.approx(0.5) and cy == pytest.approx(0.45)
    assert face_center([[], []], width=1000, height=2000) is None


def test_every_episode_gets_its_own_stretch_of_the_background() -> None:
    """Кусок фона — по id эпизода: пересборка берёт тот же, соседний ролик —
    другой, и кусок целиком внутри фона."""
    a = background_start("p/tt_1/s01", 300.0, 20.0)
    assert a == background_start("p/tt_1/s01", 300.0, 20.0)
    starts = {background_start(f"p/tt_{n}/s01", 300.0, 20.0) for n in range(20)}
    assert len(starts) > 15
    assert all(0.0 <= s <= 280.0 for s in starts)
    assert background_start("p/tt_1/s01", 10.0, 20.0) == 0.0  # фон короче — с начала


def test_every_resource_kind_has_a_template_that_loads(tmp_path: Path) -> None:
    """`videoforge resource new <вид>` — шаблон с комментариями, который
    проходит схему; новый вид (фоны, 22.09.2026) без шаблона падал бы на KeyError."""
    for kind in KINDS:
        path = create_resource(kind, "probe", base=tmp_path)
        assert path.exists(), kind
    bg = load_resource("backgrounds", "probe", Background, [tmp_path])
    assert bg.file == "probe.mp4" and bg.path() == tmp_path / "backgrounds" / "probe.mp4"


def _colour(path: Path, colour: str, size: str, seconds: float, *, audio: bool) -> Path:
    args = ["-f", "lavfi", "-i", f"color=c={colour}:size={size}:rate=10:duration={seconds:g}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds:g}", "-c:a", "aac"]
    run([*args, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)])
    return path


def test_circle_video_puts_the_clip_in_a_ring_over_the_background(tmp_path: Path) -> None:
    """Красный ролик в кружке, синий фон вокруг, белая обводка по краю; звук
    ролика на месте, длина — ролика, а не фона."""
    clip = _colour(tmp_path / "clip.mp4", "red", "180x320", 1.0, audio=True)
    bg = _colour(tmp_path / "bg.mp4", "blue", "180x320", 3.0, audio=False)
    out = tmp_path / "out.mp4"
    size = (180, 320)
    circle = circle_video(
        clip, out, crop=square_crop(180, 320), size=size, duration=1.0, fps=10,
        background=bg, background_start=1.2,
    )  # fmt: skip
    assert probe_size(out) == size
    assert probe_stream_duration(out, "0:a:0") is not None
    png = frame_at(out, 0.5, tmp_path / "f.png")
    img = Image.open(png).convert("RGB")
    cx, cy, r = circle.x + circle.d // 2, circle.y + circle.d // 2, circle.d // 2
    inside = img.getpixel((cx, cy))
    corner = img.getpixel((5, 5))
    edge = img.getpixel((cx, circle.y + 1))  # верх круга — обводка
    assert inside[0] > 150 and inside[2] < 80  # красный ролик
    assert corner[2] > 150 and corner[0] < 80  # синий фон
    assert min(edge) > 170  # белая обводка
    assert img.getpixel((cx, cy + r + 30))[2] > 100  # под кружком снова фон

    black = tmp_path / "black.mp4"
    circle_video(clip, black, crop=square_crop(180, 320), size=size, duration=1.0, fps=10)
    img = Image.open(frame_at(black, 0.5, tmp_path / "b.png")).convert("RGB")
    assert max(img.getpixel((5, 5))) < 30  # без фона — чёрное поле
