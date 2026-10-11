"""The `circle` reframe layout: the clip inside a circle over a background video.

A square cut from the source sits in a circle with a white ring and a drop shadow
(like a Telegram video message); a full-frame background clip plays underneath.
The numbers come from an approved hand-made probe at 1080x1920 and scale with
the output width.

Mask, shadow and ring are drawn by Pillow into a temp folder (the circle is
rendered 4x larger and downsampled with Lanczos for a smooth edge); ffmpeg only
stacks the layers. Geometry is computed in Python, so the layout is known before
rendering and is recorded in the clip's passport.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw, ImageFilter

from videoforge.core.ffmpeg import FIT_BLUR_DARKEN, FIT_BLUR_SCALE, FPS, fps_expr, run

# кружок 880 px на выходе шириной 1080, центр на 860 из 1920: выше середины —
# низ кадра закрывает интерфейс площадки (подпись, звук, кнопки)
DIAMETER_SHARE = 880 / 1080
CENTER_Y_SHARE = 860 / 1920
RING_SHARE = 12 / 1080  # белая обводка поверх края круга — прячет лесенку маски
SHADOW_ALPHA = 0.55
SHADOW_BLUR_SHARE = 22 / 1080
SHADOW_DROP_SHARE = 14 / 1080  # тень сдвинута вниз: свет сверху
# Центр квадрата по высоте вертикального исходника, если лиц нет. Не середина:
# на пробе t10 головы собак стояли на 0.12–0.50 высоты, квадрат по центру
# срезал их; в вертикальных роликах главное обычно выше середины — низ у
# TikTok закрыт его же подписью
DEFAULT_Y = 0.40
SUPERSAMPLE = 4


@dataclass(frozen=True)
class Circle:
    """Где кружок на выходе: левый верхний угол квадрата и диаметр, пиксели."""

    x: int
    y: int
    d: int
    ring: int


def circle_on(size: tuple[int, int]) -> Circle:
    w, h = size
    d = round(w * DIAMETER_SHARE) // 2 * 2
    cy = round(h * CENTER_Y_SHARE)
    return Circle(x=(w - d) // 2, y=cy - d // 2, d=d, ring=max(2, round(w * RING_SHARE)))


def square_crop(
    width: int,
    height: int,
    *,
    x_share: float | None = None,
    y_share: float | None = None,
    zoom: float = 1.0,
) -> tuple[int, int, int]:
    """Квадрат из исходника: (сторона, левый край, верх). Сторона — короткая
    сторона кадра (`zoom` — во сколько раз меньше); центр — (`x_share`,
    `y_share`), пусто — середина по ширине и `DEFAULT_Y` по высоте у
    вертикального кадра. Квадрат не выходит за кадр."""
    side = int(min(width, height) / max(1.0, zoom)) // 2 * 2
    cx = (0.5 if x_share is None else x_share) * width
    default_y = DEFAULT_Y if height > width else 0.5
    cy = (default_y if y_share is None else y_share) * height
    x = min(max(0, round(cx - side / 2)), width - side)
    y = min(max(0, round(cy - side / 2)), height - side)
    return side, x, y


def face_center(
    frames: list[list[tuple[float, float, float]]], *, width: int, height: int
) -> tuple[float, float] | None:
    """Центр квадрата по лицам, доли кадра: медиана центров самого крупного
    лица каждой пробы. `frames` — на каждую пробу лица (центр x, центр y,
    площадь). Лиц нет ни в одной пробе — None."""
    centers = [max(faces, key=lambda f: f[2])[:2] for faces in frames if faces]
    if not centers:
        return None
    return median(c[0] for c in centers) / width, median(c[1] for c in centers) / height


def background_start(ref: str, background_sec: float, clip_sec: float) -> float:
    """С какой секунды фона начинается кусок под ролик: своя у каждого эпизода
    и та же при пересборке — по id, а не случайно (как `assemble.music_start`)."""
    room = background_sec - clip_sec
    if room <= 0:
        return 0.0
    pick = int(hashlib.sha256(ref.encode()).hexdigest(), 16) % 10_000 / 10_000
    return round(pick * room, 2)


def _disc(size: int, radius: float, *, fill: int | None = None, ring: int = 0) -> Image.Image:
    """Круг в квадрате `size`: залитый (`fill`) или кольцо толщиной `ring`,
    которое растёт от радиуса внутрь."""
    k = SUPERSAMPLE
    img = Image.new("L", (size * k, size * k), 0)
    c, r = size * k / 2, radius * k
    box = (c - r, c - r, c + r, c + r)
    if ring:
        ImageDraw.Draw(img).ellipse(box, outline=255, width=ring * k)
    else:
        ImageDraw.Draw(img).ellipse(box, fill=fill)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _layers(folder: Path, circle: Circle, size: tuple[int, int]) -> tuple[Path, Path, Path]:
    """Маска круга, тень под ним (во весь выход) и обводка над ним."""
    w, h = size
    d, cx, cy = circle.d, circle.x + circle.d // 2, circle.y + circle.d // 2

    mask = folder / "mask.png"
    _disc(d, d / 2, fill=255).save(mask)

    under = folder / "shadow.png"
    pad = round(w * SHADOW_BLUR_SHARE) * 2
    alpha = Image.new("L", (w, h), 0)
    blob = _disc(d + 2 * pad, d / 2 + circle.ring / 2, fill=round(255 * SHADOW_ALPHA))
    alpha.paste(blob, (cx - d // 2 - pad, cy - d // 2 - pad + round(w * SHADOW_DROP_SHARE)))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(alpha.filter(ImageFilter.GaussianBlur(round(w * SHADOW_BLUR_SHARE))))
    shadow.save(under)

    over = folder / "ring.png"
    side = d + 2 * circle.ring + 8
    edge = _disc(side, d / 2 + circle.ring - 2, ring=circle.ring)
    ring = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ring.paste(Image.new("RGBA", (side, side), (255, 255, 255, 255)),
               (cx - side // 2, cy - side // 2), edge)  # fmt: skip
    ring.save(over)
    return mask, under, over


def circle_video(
    src: Path,
    out: Path,
    *,
    crop: tuple[int, int, int],
    size: tuple[int, int],
    duration: float,
    fps: float = FPS,
    background: Path | None = None,
    background_start: float = 0.0,
    fill: str = "black",
) -> Circle:
    """Квадрат `crop` (сторона, x, y) исходника в кружке поверх фона.

    Фон — видео `background` с секунды `background_start` (короче ролика —
    зацикливается; звук не берётся), без фона — поле `fill`: чёрное или
    размытая копия того же кадра, как у `fit_video`. Звук исходника копируется:
     `fps` стоит на каждой ветке до `overlay` —
    после него он съедает последний кадр (грабли t04)."""
    ow, oh = size
    side, sx, sy = crop
    circle = circle_on(size)
    rate = fps_expr(fps)
    dur = f"{duration:.3f}"
    with tempfile.TemporaryDirectory(prefix="vf_circle_") as tmp:
        mask, under, over = _layers(Path(tmp), circle, size)
        inputs = ["-i", str(src)]
        if background is not None:
            inputs += ["-stream_loop", "-1", "-ss", f"{background_start:.3f}",
                       "-i", str(background)]  # fmt: skip
        still = ["-loop", "1", "-framerate", rate, "-t", dur]
        first = 2 if background is not None else 1
        for layer in (mask, under, over):
            inputs += [*still, "-i", str(layer)]
        m, u, o = first, first + 1, first + 2

        head = "[0:v]split=2[src][back];" if background is None and fill == "blur" else ""
        clip_in = "[src]" if head else "[0:v]"
        if background is not None:
            back = (
                f"[1:v]fps={rate},scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{oh},setsar=1[bg]"
            )
        elif fill == "blur":
            bw, bh = max(2, ow // FIT_BLUR_SCALE // 2 * 2), max(2, oh // FIT_BLUR_SCALE // 2 * 2)
            back = (
                f"[back]fps={rate},scale={bw}:{bh}:force_original_aspect_ratio=increase,"
                f"crop={bw}:{bh},boxblur=10:2,scale={ow}:{oh},"
                f"eq=brightness={FIT_BLUR_DARKEN},setsar=1[bg]"
            )
        else:
            back = f"color=c=black:s={ow}x{oh}:r={rate}:d={dur}[bg]"
        graph = ";".join([
            head + back,
            f"{clip_in}fps={rate},crop={side}:{side}:{sx}:{sy},"
            f"scale={circle.d}:{circle.d}:flags=lanczos,setsar=1,format=yuva420p[clip]",
            f"[{m}:v]fps={rate},format=gray[mask]",
            "[clip][mask]alphamerge[round]",
            f"[{u}:v]fps={rate},format=rgba[shadow]",
            f"[{o}:v]fps={rate},format=rgba[ring]",
            "[bg][shadow]overlay=0:0[b1]",
            f"[b1][round]overlay={circle.x}:{circle.y}[b2]",
            "[b2][ring]overlay=0:0,format=yuv420p[v]",
        ])  # fmt: skip
        run([
            *inputs,
            "-filter_complex", graph,
            "-map", "[v]", "-map", "0:a:0?", "-c:a", "copy", "-t", dur,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(out),
        ])  # fmt: skip
    return circle
