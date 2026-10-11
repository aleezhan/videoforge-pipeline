"""Предпросмотр под площадку: готовый кадр с зонами интерфейса поверх него.

Зачем: в плеере ролик выглядит нормально, а в приложении низ кадра закрывают имя
канала, описание и строка звука, справа — колонка кнопок. Увидеть это до
публикации иначе нельзя: `qa` считает попадание субтитров в зону числами
(`PLATFORMS[..]["ui_zone"]`), но «14% против 15%» ничего не говорит глазу.
Здесь те же числа рисуются прямо на кадре.

Это макет, а не эмулятор приложения: кнопок и текста мы не рисуем, только
границы. Площадки двигают интерфейс без предупреждения, и честнее показать
прямоугольник с подписью, чем нарисовать чужой интерфейс образца сегодняшнего
дня и выдать его за правду.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from videoforge.core.ffmpeg import FPS, run
from videoforge.paths import fonts_dir

ZONE_COLOR = "red@0.28"  # закрытое интерфейсом
SAFE_COLOR = "0x35d07f"  # граница чистой области
LABEL_FONT = "PTSans-Bold.ttf"


def zone_filters(zone: dict[str, float], size: tuple[int, int], *, scale: float = 1.0) -> str:
    """Фильтры ffmpeg, рисующие зоны интерфейса и границу безопасной области.

    `zone` — доли кадра сверху, снизу и справа. Безопасная область очерчивается
    зелёным: всё, что внутри неё, зритель увидит на любой площадке из списка.
    Подпись площадки сюда не входит: её рисует Pillow (`label_image`)."""
    w, h = size
    top = max(0.0, zone.get("top", 0.0))
    bottom = max(0.0, zone.get("bottom", 0.0))
    right = max(0.0, zone.get("right", 0.0))
    parts: list[str] = []
    if top > 0:
        parts.append(f"drawbox=x=0:y=0:w={w}:h={int(h * top)}:color={ZONE_COLOR}:t=fill")
    if bottom > 0:
        parts.append(
            f"drawbox=x=0:y={int(h * (1 - bottom))}:w={w}:h={int(h * bottom)}"
            f":color={ZONE_COLOR}:t=fill"
        )
    if right > 0:
        parts.append(
            f"drawbox=x={int(w * (1 - right))}:y=0:w={int(w * right)}:h={h}"
            f":color={ZONE_COLOR}:t=fill"
        )
    safe_h = int(h * (1 - top - bottom))
    parts.append(
        f"drawbox=x=0:y={int(h * top)}:w={int(w * (1 - right))}:h={safe_h}"
        f":color={SAFE_COLOR}:t={max(2, int(3 * scale))}"
    )
    return ",".join(parts)


def label_image(label: str, out: Path, *, scale: float = 1.0) -> Path | None:
    """Подпись площадки → PNG: белый текст на полупрозрачной чёрной плашке.

    Pillow, а не `drawtext`: этого фильтра нет в сборке imageio-ffmpeg под Linux
    (она без libfreetype), и предпросмотр там падал «Filter not found». Так же
    рисуется заголовок ролика (`core.headline`). None — нет подписи или шрифта."""
    font_path = fonts_dir() / LABEL_FONT
    if not label or not font_path.exists():
        return None
    font = ImageFont.truetype(str(font_path), max(14, int(30 * scale)))
    border = max(4, int(8 * scale))
    left, top, right, bottom = (int(round(v)) for v in font.getbbox(label))
    image = Image.new("RGBA", (right - left + 2 * border, bottom - top + 2 * border))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, image.width, image.height), fill=(0, 0, 0, 140))  # black@0.55
    draw.text((border - left, border - top), label, font=font, fill=(255, 255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


def _zones_graph(
    zone: dict[str, float], size: tuple[int, int], label_png: Path | None, *, then: str = ""
) -> tuple[list[str], str]:
    """Дополнительные входы и граф с выходом `[v]`: зоны на кадре, подпись над
    безопасной областью, затем фильтры `then`. Картинка подписи — один кадр:
    overlay держит его до конца ролика."""
    zones = f"[0:v]{zone_filters(zone, size)}"
    tail = f",{then}" if then else ""
    if label_png is None:
        return [], f"{zones}{tail}[v]"
    y = int(size[1] * max(0.0, zone.get("top", 0.0))) + 12
    return ["-i", str(label_png)], f"{zones}[z];[z][1:v]overlay=x=(W-w)/2:y={y}{tail}[v]"


def busiest_caption_sec(ass: Path) -> float | None:
    """Секунда самой длинной реплики субтитров — где риск попасть под интерфейс
    наибольший. Нет файла или реплик — None, и кадр берут из середины ролика."""
    if not ass.exists():
        return None
    best: tuple[int, float] | None = None
    for line in ass.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) < 10:
            continue
        try:
            start, end = (_ass_seconds(fields[1]), _ass_seconds(fields[2]))
        except ValueError:
            continue
        text = re.sub(r"\{[^}]*\}", "", fields[9])
        if best is None or len(text) > best[0]:
            best = (len(text), (start + end) / 2)
    return best[1] if best else None


def _ass_seconds(stamp: str) -> float:
    h, m, s = stamp.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def preview_frame(
    video: Path,
    out: Path,
    *,
    at_sec: float,
    zone: dict[str, float],
    size: tuple[int, int],
    label: str = "",
) -> Path:
    """Один кадр ролика с зонами интерфейса поверх."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        extra, graph = _zones_graph(zone, size, label_image(label, Path(tmp) / "label.png"))
        run(
            [
                "-ss", f"{max(0.0, at_sec):.3f}", "-i", str(video), *extra,
                "-filter_complex", graph, "-map", "[v]", "-frames:v", "1",
                str(out),
            ]
        )  # fmt: skip
    return out


def preview_video(
    video: Path,
    out: Path,
    *,
    zone: dict[str, float],
    size: tuple[int, int],
    label: str = "",
    fps: int = FPS,
) -> Path:
    """Весь ролик с зонами — смотреть целиком, со звуком."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        label_png = label_image(label, Path(tmp) / "label.png")
        extra, graph = _zones_graph(zone, size, label_png, then=f"fps={fps}")
        run(
            [
                "-i", str(video), *extra,
                "-filter_complex", graph,
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "20",
                "-c:a", "copy",
                str(out),
            ]
        )  # fmt: skip
    return out
