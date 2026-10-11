"""Субтитры из таймингов озвучки: слова → реплики → ASS.

Тайминги слов уже есть — их отдаёт сам синтезатор (edge-tts `WordBoundary`, рядом
с mp3 лежит `.srt` по слову на реплику). Распознавать речь заново незачем: это было
бы переоткрытие с ошибками того, что известно точно.

Слова группируются в реплики по правилам чтения (не больше N слов и M символов,
разрыв на паузе и на знаке препинания), подсветка произносимого слова — караоке-теги
ASS (`\\k`), которые libass рендерит в один проход ffmpeg. Смещение каждой сцены в
финале — сумма ИЗМЕРЕННЫХ длин собранных клипов, а не плановых: AAC-кадр ≈ 23 мс,
и на сотне сцен догадка уехала бы на полсекунды.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from pydantic import BaseModel, Field

from videoforge.core.resources import CaptionStyle
from videoforge.core.storyboard import OVERLAY_MAX_CHARS, SceneOverlay

MIN_EVENT_SEC = 0.8  # реплика короче этого не успевает прочитаться — держим её дольше
EVENT_GAP_SEC = 0.01  # зазор между соседними репликами, чтобы не мигали внахлёст


@dataclass
class OverlayEvent:
    """Плашка (`Scene.overlay`) на таймлайне финала. Текст зритель читает, а
    не слушает, поэтому ни слов, ни караоке у неё нет — одна строка на всё
    окно сцены (или на несколько сцен подряд с той же плашкой)."""

    start: float
    end: float
    text: str
    kind: str  # caption | source | label
    emphasis: frozenset[str] = frozenset()  # слова из text цветом подсветки
    scene_id: str = ""


def overlay_window(
    scene_start: float, scene_end: float, overlay: SceneOverlay
) -> tuple[float, float] | None:
    """Секунды финала, где плашка на экране: коробка сцены, урезанная её окном
    (`SceneOverlay.start_sec`/`end_sec` — от начала сцены). Без окна — вся
    сцена, как было. Конец за пределами сцены режется по сцене: у нарезки хук
    «0–2 с» на сорокасекундной сцене даёт две секунды, а не сорок.

    Одна функция на сборку, QA и пульт: плашка обязана мериться и показываться
    в те же секунды, в которые нарисована, иначе контраст считался бы по фону,
    под которым текста нет. Окно целиком за сценой (start_sec дальше её конца)
    → None: события нет, и в счёт плашек финала оно не попадает."""
    start = scene_start if overlay.start_sec is None else scene_start + overlay.start_sec
    end = scene_end if overlay.end_sec is None else min(scene_end, scene_start + overlay.end_sec)
    start, end = round(start, 3), round(end, 3)
    return (start, end) if end > start else None


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    turn: bool = False  # с этого слова говорит другой человек (маркер транскриптов)

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class SceneWords:
    """Слова одной сцены и её место в финале."""

    scene_id: str
    offset_sec: float  # начало сцены в готовом файле — по измеренным клипам
    duration_sec: float  # коробка сцены: слова за её пределами не показываются
    words: list[Word] = field(default_factory=list)
    emphasis: frozenset[str] = frozenset()  # слова-акценты (их выбирает монтаж)


@dataclass
class CaptionEvent:
    start: float  # секунды в финале
    end: float
    words: list[Word]
    scene_id: str
    # текст реплики на языке зрителя (стадия translate). Слова остаются словами
    # речи — по ним считаются тайминги и покрытие, — а на экран идёт этот текст
    # целиком: подсветка по слову перевода невозможна, порядок слов другой
    text: str | None = None


class TranslatedLine(BaseModel):
    """Одна реплика субтитров на языке зрителя: окно речи и текст."""

    start: float  # от нуля сцены, как слова в .srt
    end: float
    source: str  # что сказано (язык речи) — для проверки глазами
    text: str  # что показать (язык субтитров)


class TranslatedCaptions(BaseModel):
    """Реплики сцены на языке зрителя. Пишет стадия translate, читает сборка.

    Группировка реплик — та же, что у субтитров на языке речи (по стилю), но
    сборка ей не доверяет: реплики берутся отсюда по времени, а не пересчитываются,
    иначе смена стиля молча рассинхронила бы перевод с речью."""

    language: str
    source_language: str
    llm: str  # какая модель переводила — смена модели делает перевод устаревшим
    style_id: str  # чьи лимиты слов/символов задали реплики
    lines: list[TranslatedLine] = Field(default_factory=list)


_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _seconds(stamp: str) -> float:
    m = _TIME.search(stamp)
    if not m:
        raise ValueError(f"не тайм-код SRT: {stamp!r}")
    h, mnt, s, frac = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + int(s) + int(frac) / 10 ** len(frac)


TURN_MARK = ">>"  # реплика из одного маркера: дальше говорит другой человек


def parse_srt_words(text: str) -> list[Word]:
    """SRT → слова. edge-tts пишет по слову на реплику; если в реплике несколько
    слов, её время делится между ними пропорционально длине. Реплика `>>`
    (так YouTube помечает смену говорящего) словом не становится — она даёт
    признак `turn` следующему слову."""
    words: list[Word] = []
    turn = False
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        times = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if times is None:
            continue
        a, b = lines[times].split("-->", 1)
        start, end = _seconds(a), _seconds(b)
        tokens = " ".join(lines[times + 1 :]).split()
        if tokens == [TURN_MARK]:
            turn = True
            continue
        if not tokens:
            continue
        total = sum(len(t) for t in tokens) or 1
        t = start
        for token in tokens:
            span = (end - start) * len(token) / total
            words.append(Word(token, round(t, 3), round(t + span, 3), turn=turn))
            turn = False
            t += span
    return words


_SENTENCE_END = re.compile(r"[.!?…](?:[\"'»)\]])?$")
_CLAUSE_BREAK = re.compile(r"[,—:;](?:[\"'»)\]])?$")


ORPHAN_CHARS = 2  # слово не длиннее — предлог или союз, в конце реплики он висит сиротой


def _is_orphan(word: Word) -> bool:
    return len(word.text.strip(".,!?…:;»\"'")) <= ORPHAN_CHARS


def group_words(
    words: list[Word], *, max_words: int, max_chars: int, max_gap_sec: float
) -> list[list[Word]]:
    """Слова → реплики по правилам чтения. Разрыв: лимит слов или символов, пауза
    длиннее `max_gap_sec`, конец предложения, запятая после двух и более слов.
    Предлог в конце реплики («…только у» / «полюсов») переезжает в следующую —
    на первом живом прогоне так рвалась каждая третья реплика."""
    groups: list[list[Word]] = []
    cur: list[Word] = []
    chars = 0
    for w in words:
        gap = w.start - cur[-1].end if cur else 0.0
        candidate = chars + (1 if cur else 0) + len(w.text)
        if cur and (candidate > max_chars or len(cur) >= max_words or gap >= max_gap_sec):
            carry: list[Word] = []
            if len(cur) >= 2 and _is_orphan(cur[-1]) and gap < max_gap_sec:
                carry = [cur.pop()]
            groups.append(cur)
            cur = carry
            chars = sum(len(x.text) for x in cur) + max(0, len(cur) - 1)
        cur.append(w)
        chars += len(w.text) + (1 if chars else 0)
        if _SENTENCE_END.search(w.text) or (_CLAUSE_BREAK.search(w.text) and len(cur) >= 2):
            groups.append(cur)
            cur, chars = [], 0
    if cur:
        groups.append(cur)
    return groups


def _scene_events(
    scene: SceneWords, groups: list[list[Word]], texts: list[str | None]
) -> list[CaptionEvent]:
    """Группы слов сцены → реплики на таймлайне финала. Реплика держится не
    меньше `MIN_EVENT_SEC`, но не залезает на следующую и не выходит из коробки
    сцены."""
    box_end = scene.offset_sec + scene.duration_sec
    scene_events: list[CaptionEvent] = []
    for g, text in zip(groups, texts, strict=True):
        start = scene.offset_sec + g[0].start
        end = max(scene.offset_sec + g[-1].end, start + MIN_EVENT_SEC)
        scene_events.append(CaptionEvent(start, min(end, box_end), g, scene.scene_id, text))
    for cur, nxt in zip(scene_events, scene_events[1:], strict=False):
        cur.end = min(cur.end, nxt.start - EVENT_GAP_SEC)
    return [e for e in scene_events if e.end > e.start]


def _inside_box(scene: SceneWords) -> list[Word]:
    box_end = scene.offset_sec + scene.duration_sec
    return [w for w in scene.words if scene.offset_sec + w.start < box_end - 0.05]


def build_events(scenes: list[SceneWords], style: CaptionStyle) -> list[CaptionEvent]:
    """Реплики всех сцен на общем таймлайне — на языке речи, группировка по стилю."""
    events: list[CaptionEvent] = []
    for scene in scenes:
        groups = group_words(
            _inside_box(scene),
            max_words=style.max_words,
            max_chars=style.max_chars,
            max_gap_sec=style.max_gap_sec,
        )
        events.extend(_scene_events(scene, groups, [None] * len(groups)))
    return events


def translated_events(scene: SceneWords, captions: TranslatedCaptions) -> list[CaptionEvent]:
    """Реплики сцены на языке зрителя: окна из файла перевода, слова речи внутри
    каждого окна (по ним меряется покрытие), текст — перевод. Окно без слов
    (речь подрезали после перевода) пропускается: показывать перевод того, чего
    не слышно, нельзя."""
    inside = _inside_box(scene)
    groups: list[list[Word]] = []
    texts: list[str | None] = []
    for line in captions.lines:
        words = [w for w in inside if w.end > line.start + 0.01 and w.start < line.end - 0.01]
        if not words or not line.text.strip():
            continue
        groups.append(words)
        texts.append(line.text.strip())
    return _scene_events(scene, groups, texts)


# ------------------------------------------------------------------ ASS


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' → '&HAABBGGRR' (ASS хранит цвет задом наперёд)."""
    h = hex_color.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"цвет пишется как #RRGGBB: {hex_color!r}")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_time(seconds: float) -> str:
    cs = int(round(max(0.0, seconds) * 100))
    h, rest = divmod(cs, 360000)
    m, rest = divmod(rest, 6000)
    s, c = divmod(rest, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _ass_text(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\\", "")


_ALIGN = {"bottom": 2, "center": 5, "top": 8}


def _pop_tag(style: CaptionStyle, base_scale: int) -> str:
    """Пружина произносимого слова: рывок вверх, недолёт, возврат к базе.

    `\\t` — единственный способ анимировать кегль в ASS, и он живёт внутри
    реплики: время в нём считается от её начала. Поэтому у `pop` реплика ASS
    приходится на слово, а не на фразу — иначе анимировать можно было бы только
    первое слово. Три отрезка вместо двух: без недолёта слово не подпрыгивает,
    а просто раздувается."""
    up = max(60, int(round(style.pop_ms * 0.35)))
    down = max(up + 40, int(round(style.pop_ms * 0.7)))
    peak = round(base_scale * style.pop_scale / 100)
    dip = round(base_scale * style.pop_undershoot / 100)
    return (
        f"\\fscx{base_scale}\\fscy{base_scale}"
        f"\\t(0,{up},\\fscx{peak}\\fscy{peak})"
        f"\\t({up},{down},\\fscx{dip}\\fscy{dip})"
        f"\\t({down},{style.pop_ms},\\fscx{base_scale}\\fscy{base_scale})"
    )


def _pop_dialogues(
    ev: CaptionEvent, style: CaptionStyle, marks: set[str], *, spoken: str, unspoken: str
) -> list[tuple[float, float, str]]:
    """Реплика с прыгающими словами → по строке ASS на слово: (начало, конец, текст).

    Строка перерисовывается на каждом слове целиком, а не дописывается: у ASS нет
    способа доанимировать уже нарисованное. Границы соседних строк совпадают
    ровно — тогда libass рисует их встык, без мигания и без наложения."""
    out: list[tuple[float, float, str]] = []
    # время слова — от нуля сцены, а `ev.start` — от нуля финала; разница между
    # ними и есть смещение сцены. Складывать их напрямую нельзя: реплика уехала
    # бы на своё же начало ещё раз
    base = ev.start - ev.words[0].start
    for i, active in enumerate(ev.words):
        start = max(ev.start, base + active.start) if i else ev.start
        end = base + ev.words[i + 1].start if i + 1 < len(ev.words) else ev.end
        end = min(end, ev.end)
        if end <= start:
            continue
        parts: list[str] = []
        for j, word in enumerate(ev.words):
            text = _ass_text(word.text.upper() if style.uppercase else word.text)
            marked = word.text.lower().strip(".,!?…:;") in marks
            scale = style.emphasis_scale if marked else 100
            color = _ass_color(style.emphasis_color) if marked else (spoken if j <= i else unspoken)
            tag = _pop_tag(style, scale) if j == i else f"\\fscx{scale}\\fscy{scale}"
            parts.append(f"{{\\1c{color}\\2c{color}{tag}}}{text}")
        fade = ""
        if style.fade_ms:
            fade_in = style.fade_ms if i == 0 else 0
            fade_out = style.fade_ms if i == len(ev.words) - 1 else 0
            if fade_in or fade_out:
                fade = f"{{\\fad({fade_in},{fade_out})}}"
        out.append((start, end, fade + " ".join(parts)))
    return out


# Появление слова (`animation: reveal`) идёт с замедлением: сначала быстро,
# к концу медленно. `\move` в ASS только равномерный, поэтому подъём разбит на
# два отрезка — за 45% времени слово проходит 75% пути, остаток доезжает
# медленно. Прозрачность едет теми же отрезками, чтобы свет и движение
# заканчивались вместе
REVEAL_SPLIT_T = 0.45
REVEAL_SPLIT_D = 0.75


def _anchor(style: CaptionStyle, size: tuple[int, int]) -> tuple[int, int]:
    """Точка привязки строки для `\\move`: середина по ширине и край по
    `margin_pct` — те же числа, что в строке `Style:`. Обе реплики появления
    (осевшая и поднимающаяся) считают её одинаково, поэтому слово доезжает ровно
    туда, где потом стоит, без скачка на стыке."""
    w, h = size
    margin_v = int(round(h * style.margin_pct / 100))
    y = {"bottom": h - margin_v, "center": h // 2, "top": margin_v}[style.position]
    return w // 2, y


HIDDEN = "\\alpha&HFF&"  # слово не нарисовано, но держит своё место в строке


def _spread_words(text: str, start: float, end: float) -> list[Word]:
    """Текст перевода → слова с временем внутри окна реплики, поровну по длине.

    Своих таймингов у перевода нет и быть не может: порядок слов другой. Окно
    реплики при этом ИЗМЕРЕНО по речи, и деление внутри него — та же оценка, что
    уже делает `parse_srt_words` для многословной реплики .srt. Её хватает,
    чтобы строка набиралась в такт говорящему; на подсветку «вот это слово
    сейчас произносят» она не тянет — поэтому у перевода подсветки нет."""
    tokens = text.split()
    total = sum(len(t) for t in tokens) or 1
    span = max(0.0, end - start)
    out: list[Word] = []
    t = start
    for token in tokens:
        share = span * len(token) / total
        out.append(Word(token, t, t + share))
        t += share
    return out


def _reveal_dialogues(
    ev: CaptionEvent,
    style: CaptionStyle,
    marks: set[str],
    *,
    current: str,
    settled: str,
    size: tuple[int, int],
    reflow: bool,
) -> list[tuple[int, float, float, str]]:
    """Реплика со словами, появляющимися по очереди → строки ASS: (слой, начало,
    конец, текст). `reflow` — строка растёт (`grow`) или стоит на месте (`reveal`).

    Слово выезжает снизу и проявляется из прозрачного, к концу замедляясь.
    Рисует его верхняя реплика, в которой всё остальное спрятано в
    прозрачность: спрятанные слова держат своё место, поэтому слово доезжает
    ровно туда, где осядет, — раскладку обеих реплик libass считает по одной и
    той же строке, и мерить ширину текста самим не нужно.

    Варианты отличаются только тем, из чего состоит строка. `reveal`: реплика
    разложена целиком с первого кадра, будущие слова спрятаны, текст никуда не
    двигается. `grow`: строка — только произнесённые слова, и каждое новое
    сдвигает прежние, потому что строка заново встаёт по центру.

    Появившееся слово до конца своего окна рисует ОДНА реплика. Так стало не
    сразу: сначала подъём передавал слово осевшей строке, и на стыке оно
    пропадало на кадр (замер по кадрам: 0 пикселей текста на 0.52 с при 25 к/с).
    Время реплики ASS округлено до сотых секунды — передавать по такому стыку
    нельзя, и правильный ответ не «подвинуть границу», а не делать её вовсе.

    Позиция строки задаётся явно (`\\move`/`\\pos` от `_anchor`): `\\move` живёт
    на реплике целиком, и без общей точки привязки поднимающееся слово и осевшая
    строка разъехались бы на пару пикселей."""
    w, h = size
    font_px = max(12, int(round(min(w, h) * style.size_pct / 100)))
    rise = font_px * style.reveal_rise_pct / 100
    x, y = _anchor(style, size)
    base = ev.start - ev.words[0].start  # см. `_pop_dialogues`
    out: list[tuple[int, float, float, str]] = []

    def draw(word: Word, *, color: str, alpha: str = "") -> str:
        text = _ass_text(word.text.upper() if style.uppercase else word.text)
        marked = word.text.lower().strip(".,!?…:;") in marks
        scale = style.emphasis_scale if marked else 100
        paint = _ass_color(style.emphasis_color) if marked else color
        return f"{{\\1c{paint}\\2c{paint}\\fscx{scale}\\fscy{scale}{alpha}}}{text}"

    for i, active in enumerate(ev.words):
        start = max(ev.start, base + active.start) if i else ev.start
        end = base + ev.words[i + 1].start if i + 1 < len(ev.words) else ev.end
        end = min(end, ev.end)
        if end <= start:
            continue
        shown = ev.words[: i + 1] if reflow else ev.words
        window = int(round((end - start) * 1000))
        # появление не длиннее, чем слово вообще на экране
        ms = min(style.reveal_ms, window)
        t1 = max(1, int(round(ms * REVEAL_SPLIT_T)))
        mid = round(rise * (1 - REVEAL_SPLIT_D), 1)
        a1 = f"&H{int(round(255 * (1 - REVEAL_SPLIT_D))):02X}&"

        # нижняя реплика: осевшие слова. Появляющееся и будущие прозрачны —
        # первое рисует верхняя реплика, вторые ждут своей очереди
        parts = [
            draw(word, color=settled) if j < i else draw(word, color=settled, alpha=HIDDEN)
            for j, word in enumerate(shown)
        ]
        fade = f"\\fad(0,{style.fade_ms})" if style.fade_ms and i == len(ev.words) - 1 else ""
        out.append((0, start, end, f"{{\\pos({x},{y}){fade}}}" + " ".join(parts)))

        # верхняя реплика: появляющееся слово. Подъём — двумя отрезками (быстрый
        # и медленный), второй тянется до конца окна: после `\move` и `\t`
        # значения замирают на конечных, и слово просто стоит на своём месте
        for seg_start, seg_end, y_from, y_to, a_from, a_to in (
            (0, t1, y + rise, y + mid, "&HFF&", a1),
            (t1, window, y + mid, float(y), a1, "&H00&"),
        ):
            if seg_end <= seg_start:
                continue
            travel = max(1, min(seg_end, ms) - seg_start)
            move = f"\\move({x},{y_from:.0f},{x},{y_to:.0f},0,{travel})"
            word_tag = f"\\alpha{a_from}\\t(0,{travel},\\alpha{a_to})"
            line = [draw(word, color=settled, alpha=HIDDEN) for word in shown]
            line[i] = draw(active, color=current, alpha=word_tag)
            out.append(
                (
                    1,
                    start + seg_start / 1000,
                    start + seg_end / 1000,
                    f"{{{move}}}" + " ".join(line),
                )
            )
    return out


# --- плашки: поля подложки в долях кегля и раскладка по видам. Вывод реплики —
# по центру сверху, ярлык пункта — в левом верхнем углу, источник — в правом.
# Всё сверху: субтитры живут внизу, и два текста в одной полосе не читаются
OVERLAY_PAD_EM = 0.3
_OVERLAY_ALIGN = {"caption": 8, "label": 7, "source": 9}
_OVERLAY_STYLE = {"caption": "Overlay", "label": "OverlayLabel", "source": "OverlaySource"}


def overlay_view(style: CaptionStyle) -> CaptionStyle:
    """Плашка как стиль субтитров: те же формулы кегля, полосы (`caption_band`)
    и контраста (QA) работают без второй реализации. Позиция всегда `top`;
    `outline` здесь — поля подложки, в единицах 1080-кадра, как у субтитров."""
    return style.model_copy(
        update={
            "size_pct": style.overlay_size_pct,
            "margin_pct": style.overlay_margin_pct,
            "position": "top",
            "max_chars": OVERLAY_MAX_CHARS,
            "box_opacity": style.overlay_box_opacity,
            "box_color": style.overlay_box_color,
            "animation": "none",
            "mode": "phrase",
            "outline": OVERLAY_PAD_EM * style.overlay_size_pct * 10.8,
        }
    )


def _style_line(
    name: str,
    style: CaptionStyle,
    *,
    size: tuple[int, int],
    primary: str,
    secondary: str,
    alignment: int,
    margin_l: int,
    margin_r: int,
) -> str:
    """Строка `Style:` ASS из чисел стиля — общая для субтитров и плашек."""
    w, h = size
    short = min(w, h)
    scale = short / 1080
    font_size = max(12, int(round(short * style.size_pct / 100)))
    margin_v = int(round(h * style.margin_pct / 100))
    # подложка: BorderStyle 3 рисует прямоугольник цветом BackColour вместо
    # обводки; `outline` при этом — поля прямоугольника вокруг текста
    boxed = style.box_opacity > 0
    border_style = 3 if boxed else 1
    # без подложки BackColour — цвет тени (полупрозрачный чёрный, как было)
    back = (
        _ass_color(style.box_color, alpha=int(round(255 * (1 - min(1.0, style.box_opacity)))))
        if boxed
        else "&H80000000"
    )
    return (
        f"Style: {name},{style.font},{font_size},{primary},{secondary},"
        f"{_ass_color(style.outline_color)},{back},{-1 if style.bold else 0},0,0,0,"
        f"100,100,0,0,{border_style},{style.outline * scale:.1f},{style.shadow * scale:.1f},"
        f"{alignment},{margin_l},{margin_r},{margin_v},1"
    )


_EDGE_PUNCT = '.,!?…:;"«»“”()'  # края слова; апостроф и дефис — часть слова
GRADIENT_STRIPES = 10
# Буквы занимают не всю высоту строки: сверху место под выносные, снизу — под
# хвосты. Градиент тянется по заглавной букве, а не по строке, иначе в буквах
# остаётся его середина и края цветов не видно. Доли кегля, подобраны рендером
GRADIENT_TOP = 0.2
GRADIENT_BOTTOM = 0.9
_COLOR_TAG = re.compile(r"\\([12])c&H[0-9A-Fa-f]{2}([0-9A-Fa-f]{6})")  # как пишет `_ass_color`


def _mix(ass_bgr: str, to_bgr: str, t: float) -> str:
    """Цвет ASS `BBGGRR` на доле `t` пути к другому."""
    a = [int(ass_bgr[i : i + 2], 16) for i in (0, 2, 4)]
    b = [int(to_bgr[i : i + 2], 16) for i in (0, 2, 4)]
    return "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b, strict=True))


def gradient_lines(
    text: str, style: CaptionStyle, *, size: tuple[int, int], primary: str, secondary: str
) -> list[str]:
    """Текст реплики ASS → он же полосами `\\clip` с цветом по высоте.

    Градиента в ASS нет. Реплика рисуется `GRADIENT_STRIPES` раз, каждая копия
    видна в своей горизонтальной полосе, и все цвета заливки в ней (`\\1c`,
    `\\2c` и цвета стиля после `\\r`) сдвинуты к `gradient_to` на долю полосы.
    Обводку каждая копия рисует свою, одного цвета, — на стыке полос шва нет.
    Полосы стоят в координатах кадра, а не текста: слово на пружине `pop`
    растёт сквозь них, градиент остаётся на месте — как у нарисованного."""
    w, h = size
    font_px = max(12, int(round(min(w, h) * style.size_pct / 100)))
    _, y = _anchor(style, size)
    top = {"bottom": y - font_px, "center": y - font_px / 2, "top": float(y)}[style.position]
    y0 = top + font_px * GRADIENT_TOP
    y1 = top + font_px * GRADIENT_BOTTOM
    to = _ass_color(style.gradient_to)[4:]  # &H00BBGGRR → BBGGRR
    out: list[str] = []
    for k in range(GRADIENT_STRIPES):
        t = k / (GRADIENT_STRIPES - 1)
        lo = 0 if k == 0 else int(round(y0 + (y1 - y0) * k / GRADIENT_STRIPES))
        step = (y1 - y0) / GRADIENT_STRIPES
        hi = h if k == GRADIENT_STRIPES - 1 else int(round(y0 + step * (k + 1)))
        base = f"\\1c&H00{_mix(primary[4:], to, t)}\\2c&H00{_mix(secondary[4:], to, t)}"

        def shade(m: re.Match[str], t: float = t) -> str:
            return f"\\{m.group(1)}c&H00{_mix(m.group(2), to, t)}"

        body = _COLOR_TAG.sub(shade, text)
        body = body.replace("\\r}", f"\\r{base}}}")
        out.append(f"{{\\clip(0,{lo},{w},{hi}){base}}}{body}")
    return out


def _overlay_dialogue(ev: OverlayEvent, style: CaptionStyle) -> str:
    """Плашка → строка ASS: слой 1 (над субтитрами), ключевые слова цветом
    подсветки, остальное — основным цветом стиля через `\\r`."""
    marks = {m.lower().strip(".,!?…:;»«\"'") for m in ev.emphasis}
    highlight = _ass_color(style.highlight)
    parts: list[str] = []
    for word in ev.text.split():
        text = _ass_text(word)
        if word.lower().strip(".,!?…:;»«\"'") in marks:
            parts.append(f"{{\\1c{highlight}}}{text}{{\\r}}")
        else:
            parts.append(text)
    fade = f"{{\\fad({style.fade_ms},{style.fade_ms})}}" if style.fade_ms else ""
    name = _OVERLAY_STYLE.get(ev.kind, "Overlay")
    return (
        f"Dialogue: 1,{_ass_time(ev.start)},{_ass_time(ev.end)},{name},,0,0,0,,"
        f"{fade}{' '.join(parts)}"
    )


def build_ass(
    events: list[CaptionEvent],
    style: CaptionStyle,
    *,
    size: tuple[int, int],
    emphasis: dict[str, frozenset[str]] | None = None,
    overlays: list[OverlayEvent] | None = None,
    overlay_right_share: float | None = None,
) -> str:
    """ASS-файл: один стиль, одна реплика на Dialogue, слова через караоке-теги.

    В караоке PrimaryColour — уже произнесённое (у нас подсветка), SecondaryColour —
    ещё нет (основной цвет текста). Длительность `\\k` каждого слова тянется до
    начала следующего, поэтому в паузах подсветка остаётся на последнем слове, а не
    гаснет.

    Стили с анимацией идут другой дорогой: на слово — своя реплика ASS
    (`pop` — пружина на произносимом слове, `reveal` — слова появляются по
    очереди и выезжают снизу; см. `_pop_dialogues` и `_reveal_dialogues`).
    Караоке-тегов там нет: цвет расставляется явно, потому что строка и так
    перерисовывается на каждом слове.

    `overlays` — плашки (`Scene.overlay`) в том же файле своими стилями: один
    фильтр ffmpeg рисует и субтитры, и текст сверху. `overlay_right_share` —
    правое поле плашек в долях ширины (колонка кнопок площадки), если оно
    больше бокового поля стиля."""
    w, h = size
    if style.strip_punctuation:
        events = [
            replace(
                ev,
                words=[replace(x, text=x.text.strip(_EDGE_PUNCT) or x.text) for x in ev.words],
            )
            for ev in events
        ]
    karaoke = style.mode == "karaoke" and style.highlight.lower() != style.color.lower()
    primary = _ass_color(style.highlight if karaoke else style.color)
    secondary = _ass_color(style.color)
    emphasis = emphasis or {}
    margin_lr = int(round(w * style.side_margin_pct / 100))
    styles = [
        _style_line(
            "Default",
            style,
            size=size,
            primary=primary,
            secondary=secondary,
            alignment=_ALIGN[style.position],
            margin_l=margin_lr,
            margin_r=margin_lr,
        )
    ]
    if overlays:
        view = overlay_view(style)
        right = int(round(w * max(style.side_margin_pct / 100, overlay_right_share or 0.0)))
        for kind, name in _OVERLAY_STYLE.items():
            styles.append(
                _style_line(
                    name,
                    view,
                    size=size,
                    primary=secondary,  # основной цвет: текст читают целиком, караоке нет
                    secondary=secondary,
                    alignment=_OVERLAY_ALIGN[kind],
                    margin_l=margin_lr,
                    margin_r=right,
                )
            )
    header = "\n".join(
        [
            "[Script Info]",
            "; videoforge captions — из таймингов озвучки, стиль " + style.id,
            "ScriptType: v4.00+",
            f"PlayResX: {w}",
            f"PlayResY: {h}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            *styles,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )
    emph_tag = (
        f"{{\\1c{_ass_color(style.emphasis_color)}\\2c{_ass_color(style.emphasis_color)}"
        f"\\fscx{style.emphasis_scale}\\fscy{style.emphasis_scale}}}"
    )
    lines = [header]
    for ev in events:
        marks = {m.lower().strip(".,!?…:;") for m in emphasis.get(ev.scene_id, frozenset())}
        if ev.text is not None and style.animation in ("reveal", "grow"):
            # перевод со стилем, где слова появляются по очереди: время внутри
            # ИЗМЕРЕННОГО окна реплики делится между словами перевода по длине
            # (`_spread_words`). Подсветка здесь значит не «это слово произносят
            # сейчас» — такого о переводе знать нельзя, — а «это слово только что
            # появилось»; по построению это правда, и цвет стиля работает как на
            # языке речи
            spread = CaptionEvent(
                start=ev.start,
                end=ev.end,
                words=_spread_words(ev.text, ev.start, ev.end),
                scene_id=ev.scene_id,
            )
            if spread.words:
                for layer, start, end, text in _reveal_dialogues(
                    spread,
                    style,
                    marks,
                    current=primary,
                    settled=secondary,
                    size=size,
                    reflow=style.animation == "grow",
                ):
                    lines.append(
                        f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},"
                        f"Default,,0,0,0,,{text}"
                    )
                continue
        if ev.text is not None:
            # перевод: фраза целиком, без караоке и без пружины — подсветка по
            # слову речи здесь ничего не значит, у перевода другой порядок слов.
            # Цвет — основной (не подсветка): в караоке PrimaryColour это уже
            # «произнесённое», а фразу целиком читают как непроизнесённую строку
            text = _ass_text(ev.text.upper() if style.uppercase else ev.text)
            fade = f"{{\\fad({style.fade_ms},{style.fade_ms})}}" if style.fade_ms else ""
            lines.append(
                f"Dialogue: 0,{_ass_time(ev.start)},{_ass_time(ev.end)},Default,,0,0,0,,"
                f"{fade}{{\\1c{secondary}}}{text}"
            )
            continue
        if style.animation == "pop":
            for start, end, text in _pop_dialogues(
                ev, style, marks, spoken=primary, unspoken=secondary
            ):
                lines.append(
                    f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"
                )
            continue
        if style.animation in ("reveal", "grow"):
            for layer, start, end, text in _reveal_dialogues(
                ev,
                style,
                marks,
                current=primary,
                settled=secondary,
                size=size,
                reflow=style.animation == "grow",
            ):
                lines.append(
                    f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"
                )
            continue
        parts: list[str] = []
        for i, word in enumerate(ev.words):
            text = _ass_text(word.text.upper() if style.uppercase else word.text)
            if karaoke:
                nxt = ev.words[i + 1].start if i + 1 < len(ev.words) else word.end
                cs = max(1, int(round((nxt - word.start) * 100)))
                text = f"{{\\k{cs}}}{text}"
            if word.text.lower().strip(".,!?…:;") in marks:
                text = f"{emph_tag}{text}{{\\r}}"
            parts.append(text)
        fade = f"{{\\fad({style.fade_ms},{style.fade_ms})}}" if style.fade_ms else ""
        lines.append(
            f"Dialogue: 0,{_ass_time(ev.start)},{_ass_time(ev.end)},Default,,0,0,0,,"
            f"{fade}{' '.join(parts)}"
        )
    if style.gradient_to:
        # градиент — последним шагом над уже готовыми репликами: какой дорогой
        # они ни собраны (караоке, пружина, появление, перевод), полосы одни
        striped = [lines[0]]
        for line in lines[1:]:
            head, text = line.split(",Default,,0,0,0,,", 1)
            striped += [
                f"{head},Default,,0,0,0,,{part}"
                for part in gradient_lines(
                    text, style, size=size, primary=primary, secondary=secondary
                )
            ]
        lines = striped
    for ov in overlays or []:
        if ov.end > ov.start and ov.text.strip():
            lines.append(_overlay_dialogue(ov, style))
    return "\n".join(lines) + "\n"


def stretch_overlays(
    overlays: list[OverlayEvent], *, from_sec: float, speed: float
) -> list[OverlayEvent]:
    """Плашки на замедленном хвосте — тем же преобразованием, что реплики."""
    if speed >= 1.0:
        return overlays

    def at(t: float) -> float:
        return round(t if t <= from_sec else from_sec + (t - from_sec) / speed, 3)

    return [
        OverlayEvent(at(o.start), at(o.end), o.text, o.kind, o.emphasis, o.scene_id)
        for o in overlays
    ]


def stretch_events(
    events: list[CaptionEvent], *, from_sec: float, speed: float
) -> list[CaptionEvent]:
    """Замедленный хвост ролика → те же реплики, растянутые вместе с речью.

    Всё, что до `from_sec`, не двигается; дальше время идёт в `1/speed` раз
    медленнее. Растягиваются и границы реплик, и тайминги слов внутри них —
    иначе на замедленном хвосте подсветка убежала бы вперёд голоса. Слова
    хранятся временем от нуля сцены, но `ev.start` всегда равен времени первого
    слова с этим смещением, поэтому после пересчёта смещение становится нулевым
    и всё остаётся согласованным."""
    if speed >= 1.0:
        return events

    def at(t: float) -> float:
        return round(t if t <= from_sec else from_sec + (t - from_sec) / speed, 3)

    out: list[CaptionEvent] = []
    for ev in events:
        base = ev.start - ev.words[0].start if ev.words else 0.0
        words = [Word(w.text, at(base + w.start), at(base + w.end), turn=w.turn) for w in ev.words]
        out.append(CaptionEvent(at(ev.start), at(ev.end), words, ev.scene_id, ev.text))
    return out


def speech_seconds(words: list[Word]) -> float:
    return round(sum(w.seconds for w in words), 3)


def slice_words(words: list[Word], start_sec: float, end_sec: float) -> list[Word]:
    """Слова окна [start, end) со временем от нуля окна — для нарезок.

    Транскрипт источника пословный и общий на всё видео; сцене-отрезку нужны
    только её слова и от своего нуля. Слово на границе не выбрасывается, а
    подрезается: оно звучит в кадре, и караоке без него мигало бы дырой."""
    out: list[Word] = []
    for w in words:
        if w.end <= start_sec or w.start >= end_sec:
            continue
        out.append(
            Word(
                w.text,
                round(max(w.start, start_sec) - start_sec, 3),
                round(min(w.end, end_sec) - start_sec, 3),
                turn=w.turn,
            )
        )
    return out


CHAR_EM = 0.55  # средняя ширина символа в долях кегля — оценка для числа строк
LINE_HEIGHT = 1.2  # межстрочный интервал libass в долях кегля


def caption_band(style: CaptionStyle, size: tuple[int, int]) -> tuple[float, float]:
    """Полоса кадра, которую занимает самая длинная реплика стиля: (верх, низ)
    в долях высоты. Считается из тех же чисел, из которых строится ASS, —
    детерминированно, без рендера; ширина символа — оценка, поэтому число
    строк округляется вверх."""
    w, h = size
    short = min(w, h)
    font_px = max(12, int(round(short * style.size_pct / 100)))
    margin_v = int(round(h * style.margin_pct / 100))
    usable = w - 2 * int(round(w * style.side_margin_pct / 100))
    per_line = max(1, int(usable / (CHAR_EM * font_px)))
    lines = max(1, -(-style.max_chars // per_line))
    # прыжок реально увеличивает строку: в верхней точке пружины слово выше
    # кегля, и в зону интерфейса площадки оно попадает именно там
    peak = style.pop_scale / 100 if style.animation == "pop" else 1.0
    band = lines * font_px * LINE_HEIGHT * peak + 2 * style.outline * short / 1080
    if style.position == "bottom":
        bottom = float(h - margin_v)
        top = bottom - band
    elif style.position == "top":
        top = float(margin_v)
        bottom = top + band
    else:
        top = (h - band) / 2
        bottom = top + band
    # появление приходит снизу: пока слово едет, оно висит ниже своего места —
    # и в зону интерфейса площадки попадает именно там, а не в покое
    if style.animation in ("reveal", "grow"):
        bottom += font_px * style.reveal_rise_pct / 100
    return (round(max(0.0, top) / h, 3), round(min(float(h), bottom) / h, 3))


def _srt_stamp(seconds: float) -> str:
    h, rest = divmod(max(0.0, seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")


def words_to_srt(words: list[Word]) -> str:
    """Слова → SRT по слову на реплику: формат, который пишет озвучка (edge-tts)
    и читает `parse_srt_words`. Общий выход для всех транскриберов; смена
    говорящего — отдельная реплика `>>` нулевой длины перед словом."""
    blocks: list[str] = []
    for w in words:
        if w.turn:
            stamp = _srt_stamp(w.start)
            blocks.append(f"{len(blocks) + 1}\n{stamp} --> {stamp}\n{TURN_MARK}\n")
        blocks.append(
            f"{len(blocks) + 1}\n{_srt_stamp(w.start)} --> {_srt_stamp(w.end)}\n{w.text}\n"
        )
    return "\n".join(blocks)


# ---------------------------------------------------------------- чтение обратно

# Dialogue: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text —
# девять полей до текста, а в самом тексте запятых сколько угодно
ASS_FIELDS = 9
_ASS_TAGS = re.compile(r"\{[^}]*\}")
_ASS_K = re.compile(r"\{[^}]*?\\[kK][fo]?(\d+)[^}]*?\}([^{]*)")
_ASS_BREAK = re.compile(r"\\[Nnh]")
MERGE_GAP_SEC = 0.06  # соседние строки с тем же текстом — одна реплика, а не мигание


@dataclass
class AssEvent:
    """Строка, которую зритель видит на экране, прочитанная из готового ASS."""

    start: float
    end: float
    text: str
    layer: int = 0
    style: str = ""
    words: list[Word] = field(default_factory=list)  # из караоке-тегов, если они есть


def _ass_seconds(stamp: str) -> float:
    h, m, s = stamp.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _ass_plain(text: str) -> str:
    return " ".join(_ASS_BREAK.sub(" ", _ASS_TAGS.sub("", text)).split())


def _ass_words(text: str, start: float, end: float) -> list[Word]:
    """Слова из караоke-тегов `\\k` (сотые доли секунды). Нет тегов — нет слов:
    выдумывать тайминги по длине слова здесь нельзя, дорожка ровно для того и
    нужна, чтобы отличать измеренное от предположенного."""
    words: list[Word] = []
    t = start
    for cs, chunk in _ASS_K.findall(text):
        span = int(cs) / 100
        token = _ass_plain(chunk)
        if token:
            words.append(Word(token, round(t, 3), round(min(end, t + span), 3)))
        t += span
    return words


def parse_ass_events(text: str) -> list[AssEvent]:
    """Готовый ASS → что и когда стоит на экране.

    Читать приходится именно файл, а не пересчитывать реплики заново: на экран
    попало то, что в нём написано. Но анимированные стили пишут одну реплику
    ДЕСЯТКАМИ строк — `reveal` перерисовывает всю строку на каждом слове (на
    двадцатисекундном шортсе 234 строки на 13 реплик), — поэтому соседние
    строки с одинаковым текстом сливаются в одну: на экране это и есть одна
    реплика, которая просто перерисовывается."""
    events: list[AssEvent] = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(":", 1)[1].split(",", ASS_FIELDS)
        if len(parts) <= ASS_FIELDS:
            continue
        try:
            layer = int(parts[0].strip() or 0)
            start, end = _ass_seconds(parts[1]), _ass_seconds(parts[2])
        except ValueError:
            continue
        body = parts[ASS_FIELDS]
        plain = _ass_plain(body)
        if not plain or end <= start:
            continue
        events.append(
            AssEvent(
                round(start, 3),
                round(end, 3),
                plain,
                layer,
                parts[3].strip(),
                _ass_words(body, start, end),
            )
        )
    events.sort(key=lambda e: (e.start, e.end))
    merged: list[AssEvent] = []
    for ev in events:
        same = next(
            (
                m
                for m in reversed(merged)
                if m.text == ev.text and ev.start <= m.end + MERGE_GAP_SEC
            ),
            None,
        )
        if same is None:
            merged.append(ev)
            continue
        same.end = max(same.end, ev.end)
        if ev.words and not same.words:
            same.words = ev.words
    return merged


_VTT_CUE = re.compile(r"(\d\d:\d\d:\d\d\.\d\d\d)\s*-->\s*(\d\d:\d\d:\d\d\.\d\d\d)")
_VTT_WORD = re.compile(r"<(\d\d:\d\d:\d\d\.\d\d\d)><c>([^<]+)</c>")
_VTT_TAG = re.compile(r"<[^>]+>")
WORD_STEP_SEC = 0.36  # запасной темп речи: медиана по живому файлу, если мерить нечего
MAX_WORD_SEC = 2.0  # дольше слово на экране не держим: дальше это уже пауза
# [Music], [Applause], [Аплодисменты] — пометки YouTube «здесь не речь». Словом
# не становятся: на экране это мусор, а в счёте слов — враньё
_VTT_NOISE = re.compile(r"^[\[(].*[\])]$")


def _vtt_cues(text: str) -> list[list[tuple[str, float | None]]]:
    """Реплики .vtt → списки слов; у слова есть тайм-код или его нет.

    Слова до первого тега (`head`) времени НЕ получают: в roll-up это хвост
    предыдущей строки, и приписать им начало реплики — значит отправить их на
    секунды назад."""
    cues: list[list[tuple[str, float | None]]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _VTT_CUE.search(line):
            continue
        words: list[tuple[str, float | None]] = []
        for raw in lines[i + 1 :]:
            if _VTT_CUE.search(raw):
                break
            head = _VTT_TAG.sub("", raw.split("<")[0]).strip()
            words.extend((token, None) for token in head.split() if not _VTT_NOISE.match(token))
            for stamp, token in _VTT_WORD.findall(raw):
                token = token.strip()
                if token and not _VTT_NOISE.match(token):
                    words.append((token, _ass_seconds(stamp)))
        if words:
            cues.append(words)
    return cues


def cue_plain_text(text: str) -> str:
    """Файл субтитров → голый текст без разметки, тайм-кодов и номеров.

    Нужен там, где пословного слоя нет: слова песни показать поштучно нельзя,
    а прочитать их человек всё равно должен."""
    out: list[str] = []
    for line in text.splitlines():
        clean = _VTT_TAG.sub("", line).strip()
        if not clean or "-->" in clean or clean.isdigit() or clean == "WEBVTT":
            continue
        if not out or out[-1] != clean:  # roll-up повторяет строку целиком
            out.append(clean)
    return "\n".join(out)


def _overlap(tail: list[str], head: list[str]) -> int:
    """Насколько конец уже собранного совпадает с началом новой реплики."""
    for k in range(min(len(tail), len(head)), 0, -1):
        if tail[-k:] == head[:k]:
            return k
    return 0


def parse_vtt_words(text: str) -> list[Word]:
    """WebVTT с пословными тайм-кодами → слова. Так YouTube отдаёт автосубтитры
    и речи, и песни: `<00:00:12.345><c>слово</c>`.

    Живой формат — roll-up: строка повторяется с нарастающим хвостом, и часть
    слов приходит БЕЗ своего тайм-кода — они уже были показаны в прошлой
    строке. Поэтому слова сначала склеиваются в одну последовательность по
    перекрытию (порядок в файле — это порядок произнесения), и только потом
    расставляются во времени: у кого тайм-код есть — по нему, у кого нет —
    ровным темпом между соседями.

    Так делать пришлось не из любви к алгоритмам. Простой разбор (тот, что
    жил в скрипте эдита) давал первому слову строки время НАЧАЛА строки —
    на 3.9 с раньше настоящего при темпе речи 0.36 с на слово. После сортировки
    по времени 19 слов из 33 вставали не на свои места, одно терялось вовсе, а
    на экране получалось «GO ON DEEPER» там, где поют «to go, you should know».
    Слово «ON» при этом жило на экране один кадр — замерено на готовом файле."""
    cues = _vtt_cues(text)
    sequence: list[tuple[str, float | None]] = []
    for words in cues:
        texts = [t for t, _ in words]
        k = _overlap([t for t, _ in sequence], texts)
        for i, (token, stamp) in enumerate(words):
            if i < k:  # это слово уже в последовательности — берём его время, если оно нашлось
                pos = len(sequence) - k + i
                if stamp is not None and sequence[pos][1] is None:
                    sequence[pos] = (sequence[pos][0], stamp)
            else:
                sequence.append((token, stamp))
    known = [(i, at) for i, (_, at) in enumerate(sequence) if at is not None]
    if not known:
        return []
    gaps = sorted(
        b - a for (_, a), (_, b) in zip(known, known[1:], strict=False) if 0 < b - a < MAX_WORD_SEC
    )
    step = gaps[len(gaps) // 2] if gaps else WORD_STEP_SEC
    times: list[float] = [0.0] * len(sequence)
    for slot, (i, at) in enumerate(known):
        times[i] = at
        prev_i, prev_at = known[slot - 1] if slot else (-1, at - step * (i + 1))
        for j in range(prev_i + 1, i):  # слова без тайм-кода — ровно между соседями
            times[j] = prev_at + (at - prev_at) * (j - prev_i) / (i - prev_i)
    last_i, last_at = known[-1]
    for j in range(last_i + 1, len(sequence)):
        times[j] = last_at + step * (j - last_i)
    words_out: list[Word] = []
    for i, (token, _) in enumerate(sequence):
        start = times[i]
        nxt = times[i + 1] if i + 1 < len(times) else start + step
        end = max(start + 0.05, min(nxt, start + MAX_WORD_SEC))
        words_out.append(Word(token, round(start, 3), round(end, 3)))
    return words_out


# Пометки автоматической расшифровки: их ставит распознавалка, автор текста
# песни — никогда. Наличие хотя бы одной значит, что перед нами не текст, а
# догадка машины о том, что она услышала сквозь музыку и зал
_CUE_END = re.compile(r"-->\s*(\d\d:\d\d:\d\d[.,]\d+)")
ASR_MARKERS = ("[Applause]", "[Music]", "[Laughter]", "[Cheering]", "Kind: captions")
LYRICS_SLACK_SEC = 1.0  # хвост тишины, на который файл вправе пережить трек


def read_lyrics(path: Path) -> tuple[list[Word], str, str]:
    """Слова песни с таймингами, текст для чтения и сырое содержимое файла.

    Пословный слой есть не у всякого файла: тогда дорожки не будет, но текст
    прочитать всё равно можно. Сырой текст отдаётся третьим, потому что по нему
    проверяют сам файл — пометки распознавалки видны только в нём."""
    text = path.read_text(encoding="utf-8", errors="replace")
    words: list[Word] = []
    if path.suffix.lower() == ".vtt":
        words = parse_vtt_words(text)
    elif path.suffix.lower() == ".srt":
        words = parse_srt_words(text)
    return (words, "", text) if words else ([], cue_plain_text(text), text)


def lyrics_problems(text: str, words: list[Word], *, track_sec: float | None) -> list[str]:
    """Почему этому файлу слов нельзя верить. Пусто — придраться нечем.

    Появилось после разбора 10.09.2026. К треку живого выступления (213.7 с)
    лежал `.vtt`, у которого последняя реплика стояла на 219.6 с. Субтитры
    не бывают длиннее своего ролика — значит файл был от ДРУГОЙ записи, и
    слова расходились со звуком на секунды. Стоило это собранного ролика, где
    текст на экране не совпадал с вокалом, и целой строчки в `check.md`,
    объявлявшей приём проверенным.

    Считает python, а не глаз, и по числам, которые есть всегда: длина файла
    против длины трека и пометки распознавалки. Вопрос «а тот ли это файл»
    человек задать забудет — он смотрит на слова, а слова похожи."""
    problems: list[str] = []
    # длина файла — по ПОСЛЕДНЕЙ РЕПЛИКЕ, а не по последнему слову: у живой
    # записи хвост это «[Applause]» без слов, и по словам файл выглядит короче
    # трека даже когда он от другого ролика (208.8 против 219.6 на dua_lipa)
    ends = [_seconds(m.group(1)) for m in _CUE_END.finditer(text)]
    span = max(ends) if ends else (words[-1].end if words else 0.0)
    if track_sec and span > track_sec + LYRICS_SLACK_SEC:
        problems.append(
            f"расшифровка длиннее трека на {span - track_sec:.1f} с "
            f"({span:.1f} против {track_sec:.1f}) — субтитры не бывают длиннее своего ролика, "
            "значит файл от ДРУГОЙ записи, и времена слов к этому треку не относятся"
        )
    elif track_sec and span < track_sec / 2:
        problems.append(
            f"расшифровка кончается на {span:.1f} с, а трек идёт {track_sec:.1f} с — "
            "она покрывает меньше половины"
        )
    if any(marker in text for marker in ASR_MARKERS):
        problems.append(
            "машинная расшифровка (пометки распознавалки в файле): слова и времена — "
            "оценка, а не текст песни. Живой вокал распознаётся с пропусками"
        )
    return problems
