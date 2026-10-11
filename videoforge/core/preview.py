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
from pathlib import Path

from videoforge.core.ffmpeg import FPS, filter_path, run
from videoforge.paths import fonts_dir

ZONE_COLOR = "red@0.28"  # закрытое интерфейсом
SAFE_COLOR = "0x35d07f"  # граница чистой области
LABEL_FONT = "PTSans-Bold.ttf"


def zone_filters(
    zone: dict[str, float], size: tuple[int, int], *, label: str = "", scale: float = 1.0
) -> str:
    """Фильтры ffmpeg, рисующие зоны интерфейса и границу безопасной области.

    `zone` — доли кадра сверху, снизу и справа. Безопасная область очерчивается
    зелёным: всё, что внутри неё, зритель увидит на любой площадке из списка."""
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
    if label:
        font = fonts_dir() / LABEL_FONT
        if font.exists():
            parts.append(
                f"drawtext=fontfile={filter_path(font)}:text='{label}'"
                f":x=(w-text_w)/2:y={int(h * top) + int(12 * scale)}"
                f":fontsize={max(14, int(30 * scale))}:fontcolor=white"
                f":box=1:boxcolor=black@0.55:boxborderw={max(4, int(8 * scale))}"
            )
    return ",".join(parts)


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
    run(
        [
            "-ss", f"{max(0.0, at_sec):.3f}", "-i", str(video), "-frames:v", "1",
            "-vf", zone_filters(zone, size, label=label),
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
    run(
        [
            "-i", str(video),
            "-vf", zone_filters(zone, size, label=label) + f",fps={fps}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "20",
            "-c:a", "copy",
            str(out),
        ]
    )  # fmt: skip
    return out
