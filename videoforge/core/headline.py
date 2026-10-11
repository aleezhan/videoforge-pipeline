"""Подпись над кадром: текст с цветными эмодзи → PNG, который сборка кладёт поверх.

Зачем картинкой, а не строкой ASS, как субтитры и плашки: подпись нарезчиков
мультсериалов почти всегда кончается эмодзи («Jerry was genuinely INCHES from
death he's so lucky they ran out 😭»), а libass цветных глифов не рисует — эмодзи
выходит белым силуэтом или пустым квадратом. Pillow рисует цветной шрифт
(`embedded_color`), поэтому текст и эмодзи раскладываются здесь построчно двумя
шрифтами, а в ffmpeg уходит готовая картинка — тем же входом, что водяной знак.

Внешний вид — по замеру референсных каналов нарезок мультсериалов (
разбор референсных каналов нарезок, 17.09.2026): белый TikTok Sans без подложки,
по центру, несколько строк над кадром. Тонкая чёрная обводка добавлена для поля
`blur`: на размытой копии кадра белый текст без неё теряется.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from videoforge.paths import fonts_dir

HEADLINE_FONT = "TikTokSans-SemiBold.ttf"
# Colour emoji font: `VF_EMOJI_FONT` if set, otherwise Segoe UI Emoji on Windows.
# Without a usable font the emoji are dropped from the headline instead of being
# drawn as boxes; the headline still reads without them.
EMOJI_FONTS = tuple(
    Path(p) for p in (os.environ.get("VF_EMOJI_FONT", ""), "C:/Windows/Fonts/seguiemj.ttf") if p
)
SIZE_SHARE = 0.048  # кегль, доля ширины кадра: 52 px на 1080 (у референсного канала строка ~55 px)
WIDTH_SHARE = 0.80  # ширина строки, доля ширины кадра: поля по 10% с обеих сторон
LINE_SPACING = 1.22  # межстрочный интервал в кеглях
STROKE_SHARE = 0.06  # обводка, доля кегля: 3 px при 52
GAP_SHARE = 0.022  # зазор между подписью и кадром, доля высоты выхода: 42 px на 1920
TOP_SHARE = 0.12  # без кадра fit подпись стоит на этой высоте сверху
MAX_LINES = 5  # длиннее — модель написала абзац, а не подпись; лишнее обрезается


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or cp in (0x200D, 0xFE0F, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D)
    )


def _runs(text: str) -> list[tuple[str, bool]]:
    """Слово → куски подряд идущих букв и эмодзи: (текст, эмодзи ли)."""
    out: list[tuple[str, bool]] = []
    for ch in text:
        emoji = _is_emoji(ch)
        if out and out[-1][1] == emoji:
            out[-1] = (out[-1][0] + ch, emoji)
        else:
            out.append((ch, emoji))
    # селектор варианта и склейка ZWJ сами по себе не рисуются
    return [
        (t.replace("\ufe0f", "").replace("\u200d", ""), e)
        for t, e in out
        if t.strip("\ufe0f\u200d")
    ]


@dataclass(frozen=True)
class Headline:
    """Готовая подпись: картинка и где она лежит в пикселях финала."""

    path: Path
    text: str
    x: int
    y: int
    width: int
    height: int


def _emoji_font(size: int) -> ImageFont.FreeTypeFont | None:
    """First emoji font that exists and loads at `size`; None if there is none.

    Bitmap colour fonts (CBDT/sbix) only load at their native sizes; such a font
    is skipped rather than failing the render."""
    for path in EMOJI_FONTS:
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return None


class _Fonts:
    def __init__(self, size: int) -> None:
        self.size = size
        self.text = ImageFont.truetype(str(fonts_dir() / HEADLINE_FONT), size)
        self.emoji = _emoji_font(size)

    def width(self, word: str) -> float:
        total = 0.0
        for chunk, emoji in _runs(word):
            if emoji and self.emoji is None:
                continue
            font = self.emoji if emoji else self.text
            assert font is not None
            total += font.getlength(chunk)
        return total


def wrap(text: str, fonts: _Fonts, max_width: float) -> list[str]:
    """Жадный перенос по словам в пределах `max_width` пикселей."""
    space = fonts.text.getlength(" ")
    lines: list[str] = []
    current: list[str] = []
    width = 0.0
    for word in text.split():
        w = fonts.width(word)
        if current and width + space + w > max_width:
            lines.append(" ".join(current))
            current, width = [word], w
        else:
            width = width + space + w if current else w
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines[:MAX_LINES]


def render(text: str, out: Path, *, frame_width: int) -> tuple[int, int]:
    """Подпись → PNG с прозрачным фоном шириной строки кадра. Возвращает размер."""
    size = max(12, int(round(frame_width * SIZE_SHARE)))
    fonts = _Fonts(size)
    max_width = frame_width * WIDTH_SHARE
    lines = wrap(text.strip(), fonts, max_width)
    line_h = int(round(size * LINE_SPACING))
    stroke = max(1, int(round(size * STROKE_SHARE)))
    pad = stroke * 2 + int(size * 0.25)  # эмодзи выше строчных букв — запас сверху и снизу
    width = int(max_width) + 2 * pad
    height = line_h * max(1, len(lines)) + 2 * pad
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    space = fonts.text.getlength(" ")
    for i, line in enumerate(lines):
        line_w = sum(fonts.width(w) for w in line.split()) + space * (len(line.split()) - 1)
        x = (width - line_w) / 2
        baseline = pad + i * line_h + size  # базовая линия: верх строки + кегль
        for j, word in enumerate(line.split()):
            if j:
                x += space
            for chunk, emoji in _runs(word):
                if emoji:
                    if fonts.emoji is None:
                        continue
                    draw.text((x, baseline), chunk, font=fonts.emoji, anchor="ls",
                              embedded_color=True)  # fmt: skip
                    x += fonts.emoji.getlength(chunk)
                else:
                    draw.text((x, baseline), chunk, font=fonts.text, anchor="ls",
                              fill=(255, 255, 255, 255), stroke_width=stroke,
                              stroke_fill=(0, 0, 0, 255))  # fmt: skip
                    x += fonts.text.getlength(chunk)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return width, height


def place(
    box: tuple[int, int],
    *,
    size: tuple[int, int],
    frame: tuple[int, int, int, int] | None,
) -> tuple[int, int]:
    """Левый верхний угол подписи: по центру, низом на `GAP_SHARE` над кадром
    `fit` (x, y, ширина, высота). Кадра нет или места над ним не хватает — на
    высоте `TOP_SHARE` сверху; за верхний край не уезжает."""
    w, h = size
    bw, bh = box
    x = max(0, (w - bw) // 2)
    top = int(round(h * TOP_SHARE))
    if frame is not None:
        y = frame[1] - int(round(h * GAP_SHARE)) - bh
        if y >= 0:
            return x, y
    return x, max(0, min(top, h - bh))


def fingerprint(text: str) -> str:
    """Отпечаток подписи для имени файла и сборки: текст + шрифты + размеры."""
    h = hashlib.sha256(text.strip().encode())
    h.update(f"|{HEADLINE_FONT}|{SIZE_SHARE}|{WIDTH_SHARE}|{STROKE_SHARE}|{GAP_SHARE}".encode())
    return h.hexdigest()[:12]
