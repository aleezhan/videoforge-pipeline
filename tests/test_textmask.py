"""Текст-маска: видео сквозь буквы и рост слова в полный кадр.

Проверяется то, на чём приём уже ломался вживую: кадр обязан раскрыться
ПОЛНОСТЬЮ, и это должно работать для любой буквы, а не только для узкой.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from videoforge.core.textmask import (
    FULL_SHARE,
    MAX_SCALE,
    MaskWord,
    grow_to_full,
    open_anchor,
    render,
    white_share,
    word_frame,
)

FONT = Path(__file__).resolve().parents[1] / "videoforge/assets/fonts/RussoOne-Regular.ttf"
FRAME = (120, 200)  # маленький кадр: проверяется математика роста, а не разрешение


def _glyph(letter: str) -> Image.Image:
    img = word_frame(letter, FONT, size=90, frame=FRAME)
    box = img.getbbox()
    return img.crop(box) if box else img


def test_growth_opens_the_whole_frame() -> None:
    """Ради этого приём и существует: буква растёт, пока за ней не откроется
    кадр. На живом ролике рост считали формулой и остановились на половине —
    просвет 516 px из 1080, кадр не открывался никогда."""
    glyph = _glyph("I")
    scale = grow_to_full(glyph, FRAME)
    assert white_share(glyph, scale, FRAME) >= FULL_SHARE
    # и это не «взять побольше»: чуть меньше найденного кадр ещё не закрыт
    assert white_share(glyph, scale * 0.8, FRAME) < FULL_SHARE


def test_a_letter_with_a_hole_opens_the_frame_too() -> None:
    """Здесь стояла проверка «у O масштаб больше, чем у I в полтора раза» — и
    она была зелёной, пока приём был сломан: подбор возвращал перелёт за предел
    (256 при пределе 200), а перелёт тоже «больше в полтора раза». Число не
    отвечает на вопрос, ради которого приём существует; отвечает открытый кадр.

    У «O» середина пустая, поэтому рост из середины рамки не открывает кадр НИ
    НА КАКОМ масштабе: сначала видно кольцо, потом кадр целиком проваливается в
    дыру. Расти надо из сердцевины штриха."""
    glyph = _glyph("O")
    anchor = open_anchor(glyph)
    scale = grow_to_full(glyph, FRAME, anchor=anchor)
    assert scale < MAX_SCALE, "упереться в предел — это «не открылось», а не ответ"
    assert white_share(glyph, scale, FRAME, anchor) >= FULL_SHARE
    # тот же рост из середины рамки кадр не открывает — ради этого всё и делалось
    assert white_share(glyph, scale, FRAME) < FULL_SHARE


def test_growth_starts_where_the_ink_is_not_where_the_box_is() -> None:
    """Точка роста — сердцевина штриха, а не любая краска у середины. Точка на
    краю штриха открывает кадр только на 0.926 даже на пределе масштаба: с той
    стороны, где край, чёрное поспевает за ростом и достаёт до углов."""
    ax, ay = open_anchor(_glyph("O"))
    assert 0.0 <= ax <= 1.0 and 0.0 <= ay <= 1.0
    # у кольца сердцевина заведомо в стороне от середины рамки — там дыра
    assert abs(ax - 0.5) > 0.2 or abs(ay - 0.5) > 0.2


def test_render_writes_a_frame_per_video_frame(tmp_path: Path) -> None:
    words = [MaskWord("ЭТО", 0.0), MaskWord("Я", 0.4)]
    made = render(
        words, tmp_path, FONT, frame=FRAME, fps=10, grow_end_sec=1.0, grow_sec=0.4, condense=0.66
    )
    assert made == 10  # секунда при 10 кадрах в секунду
    assert len(sorted(tmp_path.glob("*.png"))) == made
    last = Image.open(tmp_path / f"{made - 1:04d}.png").convert("L")
    px = last.load()
    lit = sum(1 for y in range(0, FRAME[1], 8) for x in range(0, FRAME[0], 8) if px[x, y] > 128)
    total = len(range(0, FRAME[1], 8)) * len(range(0, FRAME[0], 8))
    assert lit / total >= FULL_SHARE  # последний кадр маски — открытый кадр


def _open_share(path: Path) -> float:
    img = Image.open(path).convert("L")
    data = img.tobytes()
    xs, ys = range(0, FRAME[0], 8), range(0, FRAME[1], 8)
    lit = sum(1 for y in ys for x in xs if data[y * FRAME[0] + x] > 128)
    return lit / (len(xs) * len(ys))


def test_last_frame_opens_even_when_the_word_ends_in_a_ring(tmp_path: Path) -> None:
    """Тот самый брак, из-за которого всё это переписано: слово, кончающееся
    кольцевой буквой, росло из середины рамки — то есть из дыры — и анимация
    заканчивалась ЧЁРНЫМ экраном вместо открытой картинки. Проверяется не
    математика роста, а готовый последний кадр."""
    made = render(
        [MaskWord("ВОТ", 0.0), MaskWord("О", 0.4)],
        tmp_path,
        FONT,
        frame=FRAME,
        fps=10,
        grow_end_sec=1.0,
        grow_sec=0.4,
    )
    assert _open_share(tmp_path / f"{made - 1:04d}.png") >= FULL_SHARE
