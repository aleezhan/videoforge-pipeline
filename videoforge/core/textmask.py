"""Текст-маска: видео сквозь буквы и рост слова в полный кадр.

Приём из эталонных рилсов: на чёрном идут слова песни, сквозь них видно клип, а
последнее слово строки растёт, пока не откроет кадр целиком. Здесь он живёт
кадрами маски (белый текст на чёрном); видео подставляет ffmpeg через
`alphamerge` + `overlay`. Умножением (`blend=multiply`) этого делать НЕЛЬЗЯ:
нулевая цветность в YUV — не чёрный, и кадр становится ярко-зелёным.

Три вещи здесь измерены, а не придуманы, и «упростив» их легко вернуть брак.

**Растёт САМА БУКВА, а не прямоугольник по её рамке.** Прямоугольник подменял
форму на первом же кадре роста — буква мгновенно превращалась в брусок.

**Насколько растить — подбирается рендером, а не формулой.** Три попытки
посчитать (по рамке глифа, по вписанному белому квадрату, по полосам через его
центр) дали три разных ответа, и все три оставляли просвет в половину экрана:
516 и 558 px из 1080 на последнем кадре. Мягкий край после сжатия делает любую
формулу спорной, а отрендеренный кадр не спорит. Отсюда `grow_to_full`.

**Растёт из ближайшей к середине КРАСКИ, а не из середины рамки.** Здесь стояло
обратное — будто подбор рендером «сам справляется с буквой-кольцом, надо лишь
расти дольше». Это неверно, и проверено 11.09.2026: у «О» середина пустая, кадр
сначала показывает кольцо, а потом целиком проваливается в дыру. «ВСЁ», «СНОВА»
и «О» не открывали кадр НИ НА КАКОМ масштабе до предела — максимум 0.47–0.55,
дальше ноль, и `grow_to_full` возвращал 256 при пределе 200, то есть рендер
растил букву до чёрного экрана. Приём жил потому, что в первом ролике середина
последнего слова случайно оказалась залитой. Точку роста ищет `open_anchor`.

**Раскрытие идёт ease-IN, а не ease-out.** ease-out даёт 12% роста за первый
кадр — буква пропадает раньше, чем её успели прочитать.

Приём и его замеры — `library/caption_animation/letter_opens.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FULL_SHARE = 0.995  # доля белого, при которой кадр считается раскрытым
PROBE_STEP = 8  # каждый восьмой пиксель: доля белого так же верна, считается мгновенно
EASE_POWER = 2.2  # ease-IN: буква держит форму, потом распахивается
FIT_SHARE = 0.80  # какую долю ширины кадра занимает самое длинное слово
MAX_SCALE = 200.0  # предел подбора: дальше расти незачем даже кольцевой букве


@dataclass(frozen=True)
class MaskWord:
    """Слово маски: что показать и с какой секунды ролика."""

    text: str
    at: float


def ease_open(u: float) -> float:
    return u**EASE_POWER


def fit_size(
    words: list[str],
    font_path: Path,
    *,
    width: int,
    condense: float = 1.0,
    share: float = FIT_SHARE,
) -> int:
    """Кегль, при котором самое длинное слово занимает `share` ширины кадра."""
    longest = max(words, key=len)
    for size in range(40, 800, 2):
        font = ImageFont.truetype(str(font_path), size)
        box = font.getbbox(longest)
        if (box[2] - box[0]) * condense >= share * width:
            return size
    return 400


def word_frame(
    word: str, font_path: Path, *, size: int, frame: tuple[int, int], condense: float = 1.0
) -> Image.Image:
    """Кадр маски: слово белым по центру чёрного кадра.

    `condense` сжимает по горизонтали — своего узкого шрифта у нас нет, а
    широкие буквы в вертикальном кадре занимают всю ширину тремя символами."""
    w, h = frame
    font = ImageFont.truetype(str(font_path), size)
    x0, y0, x1, y1 = font.getbbox(word)
    text = Image.new("L", (int(x1 - x0) + 8, int(y1 - y0) + 8), 0)
    ImageDraw.Draw(text).text((4 - x0, 4 - y0), word, fill=255, font=font)
    if condense != 1.0:
        text = text.resize(
            (max(1, int(text.width * condense)), text.height), Image.Resampling.LANCZOS
        )
    out = Image.new("L", (w, h), 0)
    out.paste(text, ((w - text.width) // 2, (h - text.height) // 2))
    return out


def open_anchor(glyph: Image.Image) -> tuple[float, float]:
    """Точка, ИЗ КОТОРОЙ растить: ближайший к середине глифа залитый пиксель.

    Растить из середины рамки нельзя. У «О» там дыра, и как бы буква ни росла,
    в кадре остаётся чёрное: сначала видно кольцо, потом кадр целиком
    проваливается внутрь дыры. Измерено на кадре 216×384: «ВСЁ», «СНОВА» и «О»
    не открывают кадр НИ НА КАКОМ масштабе до предела — максимум 0.47–0.55, и
    дальше падение до нуля. Раньше приём работал по случайности: в первом
    ролике середина последнего слова оказалась залитой.

    Если же расти из места, где краска есть, пятно вокруг него разрастается и
    закрывает кадр целиком — независимо от того, кольцевая буква или нет.

    Берётся не любая краска, а СЕРДЦЕВИНА штриха — и это тоже измерено. Точка
    у края штриха открывает кадр только на 0.926 даже на пределе масштаба: с
    той стороны, где край, чёрное поспевает за ростом и достаёт до углов.
    Сердцевину ищем стачиванием: слой за слоем убираем по пикселю белого, пока
    не исчезнет всё; последний уцелевший слой и есть самое толстое место. Из
    него берём ближайшую к середине точку — так соблюдены оба условия.

    Возвращается доля ширины и высоты, а не пиксели: глиф потом масштабируют.
    """
    w, h = glyph.width, glyph.height
    # рамка в один чёрный пиксель: без неё стачивание щадит краску у самого края
    # картинки (соседей за границей нет), и «сердцевиной» оказался бы край
    core = Image.new("L", (w + 2, h + 2), 0)
    core.paste(glyph.point(lambda v: 255 if v > 128 else 0), (1, 1))
    while True:
        thinner = core.filter(ImageFilter.MinFilter(3))
        if thinner.getbbox() is None:  # следующий слой стёр бы всё
            break
        core = thinner

    box = core.getbbox()
    if box is None:  # пустой глиф: расти неоткуда, ведём себя как раньше
        return 0.5, 0.5

    # по сердцевине идём подряд, без шага: она бывает толщиной в пиксель, и
    # прореженный поиск её просто не замечал — «О» тогда откатывалась к середине
    # рамки, то есть ровно в дыру. Ищем только внутри её рамки, это дёшево
    data = core.tobytes()
    cw = w + 2
    cx, cy = (w - 1) / 2, (h - 1) / 2
    best: tuple[int, int] | None = None
    best_d = 0.0
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        row = y * cw
        for x in range(x0, x1):
            if data[row + x] > 128:
                gx, gy = x - 1, y - 1  # снять чёрную рамку
                d = (gx - cx) ** 2 + (gy - cy) ** 2
                if best is None or d < best_d:
                    best, best_d = (gx, gy), d
    if best is None:
        return 0.5, 0.5
    return (best[0] + 0.5) / w, (best[1] + 0.5) / h


def paste_xy(
    frame: tuple[int, int], size: tuple[int, int], anchor: tuple[float, float], drift: float = 1.0
) -> tuple[int, int]:
    """Куда класть растянутый глиф.

    `drift` = 0 — глиф стоит по центру кадра (так он и висел до роста), 1 — в
    центре кадра стоит `anchor`. Переход между ними и есть уход буквы к точке
    роста; без него она прыгнула бы на первом же кадре анимации.
    """
    w, h = frame
    gw, gh = size
    mid_x, mid_y = (w - gw) / 2, (h - gh) / 2
    to_x, to_y = w / 2 - anchor[0] * gw, h / 2 - anchor[1] * gh
    return round(mid_x + (to_x - mid_x) * drift), round(mid_y + (to_y - mid_y) * drift)


def white_share(
    glyph: Image.Image,
    scale: float,
    frame: tuple[int, int],
    anchor: tuple[float, float] = (0.5, 0.5),
) -> float:
    """Какая доля кадра открыта, если растянуть глиф в `scale` раз вокруг `anchor`."""
    w, h = frame
    gw = max(1, round(glyph.width * scale))
    gh = max(1, round(glyph.height * scale))
    out = Image.new("L", (w, h), 0)
    out.paste(glyph.resize((gw, gh), Image.Resampling.LANCZOS), paste_xy(frame, (gw, gh), anchor))
    # tobytes, а не load(): у режима «L» это ровно w*h байт построчно, то есть
    # яркость точки — просто целое по индексу. load() отдаёт C-аксессор, у
    # которого тип пикселя зависит от режима и потому неизвестен заранее
    data = out.tobytes()
    xs, ys = range(0, w, PROBE_STEP), range(0, h, PROBE_STEP)
    lit = sum(1 for y in ys for x in xs if data[y * w + x] > 128)
    return lit / (len(xs) * len(ys))


def grow_to_full(
    glyph: Image.Image,
    frame: tuple[int, int],
    *,
    anchor: tuple[float, float] | None = None,
    limit: float = MAX_SCALE,
) -> float:
    """Во сколько раз растить глиф, чтобы кадр открылся ПОЛНОСТЬЮ.

    Подбор делением пополам по отрендеренному кадру. Почему не формула — в
    заголовке модуля: формулы пробовали трижды и трижды получили половину.

    Ответ никогда не больше `limit`: раньше удвоение выскакивало за предел (для
    кольцевых букв возвращало 256 при пределе 200), и это значило «кадр не
    открылся», но выглядело как обычное число — рендер послушно растил букву до
    чёрного экрана.
    """
    if anchor is None:
        anchor = open_anchor(glyph)
    low, high = 1.0, 2.0
    while high < limit and white_share(glyph, high, frame, anchor) < FULL_SHARE:
        low, high = high, min(high * 2, limit)
        if high >= limit:
            break
    for _ in range(12):  # 12 делений дают точность лучше процента
        mid = (low + high) / 2
        if white_share(glyph, mid, frame, anchor) < FULL_SHARE:
            low = mid
        else:
            high = mid
    # вверх, а не к ближайшему: `high` — это найденный масштаб, на котором кадр
    # УЖЕ открыт, и округление вниз возвращало его в закрытое состояние. У
    # кольцевой буквы доля белого на границе прыгает круто, и полсотых решают
    return min(ceil(high * 100) / 100, limit)


def render(
    words: list[MaskWord],
    out_dir: Path,
    font_path: Path,
    *,
    frame: tuple[int, int],
    fps: int,
    grow_end_sec: float,
    grow_sec: float,
    condense: float = 1.0,
) -> int:
    """Кадры маски в `out_dir` (`0000.png`…). Возвращает, сколько их вышло.

    Слова держатся по своим секундам, последнее — до начала роста; рост кончается
    на `grow_end_sec`, и туда сажают музыкальное событие: вход барабанов или дроп.
    Первое слово показывается с нуля, без чёрной паузы в начале ролика."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()

    size = fit_size([w.text for w in words], font_path, width=frame[0], condense=condense)
    at_frame = [0, *[round(w.at * fps) for w in words[1:]], round((grow_end_sec - grow_sec) * fps)]
    frames: list[Image.Image] = []
    for i, word in enumerate(words):
        img = word_frame(word.text, font_path, size=size, frame=frame, condense=condense)
        frames.extend([img] * max(0, at_frame[i + 1] - at_frame[i]))

    last = word_frame(words[-1].text, font_path, size=size, frame=frame, condense=condense)
    box = last.getbbox()
    glyph = last.crop(box) if box else last
    # Растём не из середины рамки, а из ближайшей к ней краски: у кольцевой
    # буквы в середине дыра, и рост «по центру» кадр не открывает никогда.
    # Тем же `t` буква и уходит к этой точке — иначе прыжок на первом кадре.
    anchor = open_anchor(glyph)
    k_max = grow_to_full(glyph, frame, anchor=anchor)
    steps = max(1, round(grow_end_sec * fps) - at_frame[-1])
    for step in range(steps):
        t = ease_open((step + 1) / steps)
        scale = 1.0 + (k_max - 1.0) * t
        gw = max(1, round(glyph.width * scale))
        gh = max(1, round(glyph.height * scale))
        img = Image.new("L", frame, 0)
        img.paste(
            glyph.resize((gw, gh), Image.Resampling.LANCZOS), paste_xy(frame, (gw, gh), anchor, t)
        )
        frames.append(img)

    for i, img in enumerate(frames):
        img.save(out_dir / f"{i:04d}.png")
    return len(frames)
