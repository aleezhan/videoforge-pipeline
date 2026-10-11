"""Паузы речи: срез вшитой тишины озвучки и пауза на стыке сцен по смыслу.

Замер 16.09.2026 (`research/_probes/speech_pauses_2026-09-16/report.md`): у
конкурентов doc_channel пауза на конце предложения 0.14–0.58 с и зависит от того,
что дальше; у нас после почти каждого предложения стояло 1.5–1.7 с. Длину давал
не смысл, а стык сцен: edge-tts вшивает тишину в mp3 (ru_male 0.24 с в начале и
0.97 в конце, william 0.19 и 0.42), сборка добавляла 0.5 с хвоста кадра и добивку
до плана, а сцена ≈ одно предложение.

Здесь всё, что про паузу считает python: окно среза по таймингам слов, класс
стыка (что кончилось и что начинается), его секунды, место склейки внутри паузы
(замер 17.09.2026, `lead_in`) и замер пауз готового ролика по тем же словам
(пороги — в `core/storyboard.py` рядом с порогами тишины).
Модели тут нечего решать — класс виден по знаку, по разметке сценария и по
`Scene.continues`.
"""

from __future__ import annotations

import hashlib
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

from videoforge.core.captions import Word
from videoforge.core.script import ScriptBlock
from videoforge.core.storyboard import LONG_PAUSE_SEC, Scene

# --- срез вшитой тишины. Запас вокруг таймингов слов edge-tts, замер 16.09.2026
# на 78 репликах ep001 каждым голосом (скретчпад pace/edges.py): звук первого
# слова поднимается над −60 дБ через 85–160 мс ПОСЛЕ начала слова в .srt, а
# последнее слово затихает ниже −60 дБ за 2–177 мс ДО конца слова в .srt —
# тайминги сервиса шире звука с обеих сторон. 40 мс сверху — страховка на голос
# или провайдера, у которого тайминги окажутся впритык: атака согласной и хвост
# гласной короче 40 мс не бывают слышны отдельно, а больше — уже снова пауза
TRIM_PAD_SEC = 0.04

# --- пауза стыка по классу: медианы референсного канала A (три ролика, пауза = отрезок на
# 25 дБ тише речи, место и знак — из авто-субтитров с пунктуацией)
PAUSE_CLAUSE_SEC = 0.2  # разрез внутри предложения: после запятой 0.16–0.25
PAUSE_SENTENCE_SEC = 0.45  # конец предложения: 0.45–0.57
PAUSE_QUESTION_SEC = 0.55  # после вопроса: 0.54–0.59
PAUSE_POINT_SEC = 0.7  # новый пункт или абзац («Стадия…», «Третий…»): 0.66–0.80
PAUSE_BLOCK_SEC = 1.0  # конец блока сценария — поворот: самые длинные 1.0–1.3
# разброс вокруг класса — детерминированно от id сцены: у конкурентов соседние
# паузы отличаются на 0.14–0.17 с, а одна и та же пауза на каждом стыке звучит
# метрономом (у референсного канала B она статична, но в десять раз короче нашей)
PAUSE_JITTER_SEC = 0.1
PAUSE_MIN_SEC = 0.05  # ниже этого пауза перестаёт быть паузой и слова слипаются

PauseClass = Literal["clause", "sentence", "question", "point", "block"]
PAUSE_SEC: dict[PauseClass, float] = {
    "clause": PAUSE_CLAUSE_SEC,
    "sentence": PAUSE_SENTENCE_SEC,
    "question": PAUSE_QUESTION_SEC,
    "point": PAUSE_POINT_SEC,
    "block": PAUSE_BLOCK_SEC,
}

# --- где на паузе стыка стоит склейка. Замер 17.09.2026
# (`research/_probes/scene_cuts_2026-09-17/report.md`, три ролика референсного канала A):
# склейка на паузе конца предложения стоит через 0.29–0.37 с после конца речи и
# за 0.28–0.33 с до следующего слова, медиана положения 0.46–0.59 длины паузы
# (`numbers.json`, cut_in_sentence_pause.rel_in_pause), сама пауза 0.69–0.77 с.
# Картинка меняется в тишине, и зритель видит, о чём сейчас скажут, до первого
# слова. Здесь — какая доля паузы достаётся ЗАХОДУ новой сцены (её картинка до
# первого слова): середина. Разрез внутри предложения — ноль: голос идёт дальше
# через склейку, и она ложится туда же, где стояла до 17.09, — на начало файла
# озвучки, за запас среза до слова
CUT_MID_SHARE = 0.5
CUT_IN_SHARE: dict[PauseClass, float] = {
    "clause": 0.0,
    "sentence": CUT_MID_SHARE,
    "question": CUT_MID_SHARE,
    "point": CUT_MID_SHARE,
    "block": CUT_MID_SHARE,
}
# где заход записан: `Scene.measured_sec[LEAD_IN_KEY]` — секунды картинки сцены до
# начала её озвучки. `duration_sec` его уже включает. Ключ ставит стадия audio при
# озвучке (0.0) и уточняет при расчёте длительностей; нет ключа — озвучено до
# 17.09.2026, пауза стыка целиком в хвосте прошлой сцены, и коробку по новому
# правилу не пересчитывают (клипы не устаревают). Отдельного поля в схеме нет
# намеренно: заход — длительность внутри коробки, как `frozen` и `audio_raw`
LEAD_IN_KEY = "lead_in"

_CLOSING = "»\"')]”’ \t\n"
_SENTENCE_MARKS = ".!?…"
# начало нового пункта по первому слову реплики. Список короткий намеренно:
# «Первый человек…» — не пункт, а «Стадия третья» и «Во-вторых» — всегда он
_POINT_OPENERS = re.compile(
    r"^[«\"(]?\s*(?:стадия|пункт|шаг|во-первых|во-вторых|в-третьих|в-четв[её]ртых|в-пятых)\b",
    re.IGNORECASE,
)


def ending(text: str) -> Literal["clause", "sentence", "question"]:
    """Чем кончается реплика: вопросом, предложением или ничем (запятая, тире,
    двоеточие — разрез внутри предложения). Кавычки и скобки после знака не
    мешают: «…из глины»."""
    tail = text.rstrip(_CLOSING)
    if not tail:
        return "clause"
    if "?" in tail[-3:] and tail[-1] in _SENTENCE_MARKS:
        return "question"
    return "sentence" if tail[-1] in _SENTENCE_MARKS else "clause"


def trim_window(
    words: list[Word], duration: float, *, pad: float = TRIM_PAD_SEC
) -> tuple[float, float]:
    """Окно звука, который остаётся после среза: от начала первого слова до
    конца последнего, с запасом `pad`, не шире файла. Слов нет — файл целиком."""
    if not words:
        return 0.0, duration
    start = min(max(0.0, words[0].start - pad), duration)
    end = min(duration, max(words[-1].end + pad, start))
    return round(start, 3), round(end, 3)


def shift_words(words: list[Word], by: float) -> list[Word]:
    """Слова, сдвинутые на `by` секунд (срез начала файла → минус его длина)."""
    return [
        Word(w.text, round(max(0.0, w.start + by), 3), round(max(0.0, w.end + by), 3), turn=w.turn)
        for w in words
    ]


def jitter(scene_id: str) -> float:
    """Разброс паузы в пределах ±`PAUSE_JITTER_SEC` — от id сцены, а не от
    случая: повторный прогон обязан дать ту же длительность, иначе отпечаток
    сборки меняется на ровном месте."""
    value = int(hashlib.sha256(scene_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    return round((value * 2 - 1) * PAUSE_JITTER_SEC, 3)


def norm(token: str) -> str:
    """Слово для сверки текста с таймингами и сценарием: без ударений, ё = е,
    только буквы и цифры."""
    decomposed = unicodedata.normalize("NFD", token.lower())
    plain = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    plain = unicodedata.normalize("NFC", plain).replace("ё", "е")
    return re.sub(r"[\W_]+", "", plain)


@dataclass
class ScriptMarks:
    """Что сценарий знает о стыках сцен: какие сцены кончают блок и какие
    начинают новый абзац внутри блока. Пусто — сценария нет (написан руками
    сразу в раскадровку) или ни одна реплика с ним не совпала."""

    block_end: set[str] = field(default_factory=set)
    point_start: set[str] = field(default_factory=set)


def script_marks(scenes: list[Scene], blocks: list[ScriptBlock]) -> ScriptMarks:
    """Сцены → место в сценарии: сверка слов реплик со словами блоков.

    Связи «сцена ← блок» в раскадровке нет: планнер переписывает текст по
    сценам и может его перефразировать (у ep001 «за какую именно её часть» стало
    «за какую часть»). Поэтому сверяются последовательности слов целиком
    (`SequenceMatcher` без autojunk — служебные слова тоже опоры), и сцене
    достаются первое и последнее совпавшие слова сценария. Блок кончился, если
    следующая сопоставленная сцена начинается в другом блоке или сцен дальше
    нет; абзац начался, если между концом одной сцены и началом следующей в
    сценарии стоит начало абзаца."""
    script: list[tuple[str, int, bool]] = []  # слово, номер блока, начало абзаца
    for bi, block in enumerate(blocks):
        for pi, paragraph in enumerate(block.paragraphs or [block.text]):
            opening = pi > 0  # первое слово абзаца, кроме первого абзаца блока
            for token in paragraph.split():
                if not (n := norm(token)):
                    continue
                script.append((n, bi, opening))
                opening = False
    ordered = sorted(scenes, key=lambda s: s.order)
    spoken: list[tuple[str, int]] = []  # слово, номер сцены
    for si, scene in enumerate(ordered):
        spoken.extend((n, si) for token in scene.narration.split() if (n := norm(token)))
    marks = ScriptMarks()
    if not script or not spoken:
        return marks
    matcher = SequenceMatcher(
        None, [w for w, _, _ in script], [w for w, _ in spoken], autojunk=False
    )
    span: dict[int, tuple[int, int]] = {}  # сцена → (первое, последнее) слово сценария
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            si = spoken[b + k][1]
            lo, hi = span.get(si, (a + k, a + k))
            span[si] = (min(lo, a + k), max(hi, a + k))
    mapped = sorted(span)
    for pos, si in enumerate(mapped):
        last = span[si][1]
        if pos + 1 == len(mapped):
            marks.block_end.add(ordered[si].id)
            continue
        nxt = mapped[pos + 1]
        first = span[nxt][0]
        if script[last][1] != script[first][1]:
            marks.block_end.add(ordered[si].id)
        elif any(script[k][2] for k in range(last + 1, first + 1)):
            marks.point_start.add(ordered[nxt].id)
    return marks


def _starts_point(scene: Scene, nxt: Scene, marks: ScriptMarks) -> bool:
    if nxt.id in marks.point_start or _POINT_OPENERS.match(nxt.narration.strip()):
        return True
    # ярлык пункта («ПУНКТ 3») планнер ставит на первую сцену пункта
    label = nxt.overlay if nxt.overlay is not None and nxt.overlay.kind == "label" else None
    return label is not None and (scene.overlay is None or scene.overlay.text != label.text)


def pause_class(scene: Scene, nxt: Scene | None, marks: ScriptMarks) -> PauseClass:
    """Класс паузы после реплики сцены — от сильного к слабому: конец ролика и
    блока сценария, новый пункт, вопрос, конец предложения, разрез внутри
    предложения. Продолжение того же плана (`Scene.continues`), разрезанное
    посреди предложения, — всегда разрез: голос идёт дальше через склейку."""
    if nxt is None:
        return "block"
    end = ending(scene.narration)
    if nxt.continues == scene.id and end == "clause":
        return "clause"
    if scene.id in marks.block_end:
        return "block"
    if _starts_point(scene, nxt, marks):
        return "point"
    return end


def pause_after(scene: Scene, nxt: Scene | None, marks: ScriptMarks, *, tail_sec: float) -> float:
    """Секунды тишины между последним словом сцены и первым словом следующей.

    `tail_sec` (`video.scene_tail_sec`) — время КАРТИНКИ без голоса вокруг
    склейки, а не голоса: пауза его покрывает, а не прибавляется к нему (max, а
    не сумма). До 17.09.2026 это было время старой картинки после слова; теперь
    склейка стоит посреди паузы (`lead_in`), и те же секунды делят хвост
    уходящей сцены и заход новой: старой картинке после слова остаётся около
    половины паузы (у референсного канала A 0.29–0.37 с), у последней сцены хвост целиком
    её. Хвост не нужен продолжению того же плана —
    кадр не меняется. Разброс ложится поверх: пауза не короче хвоста минус
    0.1 с, чего глаз не заметит, а метроном из одинаковых пауз слышно."""
    cls = pause_class(scene, nxt, marks)
    base = PAUSE_SEC[cls]
    if nxt is None or nxt.continues != scene.id:
        base = max(base, tail_sec)
    return round(max(PAUSE_MIN_SEC, base + jitter(scene.id)), 3)


def lead_in(cls: PauseClass, pause: float, *, head: float, tail_pad: float) -> float:
    """Заход следующей сцены: сколько секунд её картинка идёт до начала её
    озвучки — часть паузы стыка, отданная новой сцене (`CUT_IN_SHARE`).

    Склейка ставится так, чтобы до первого слова оставалась доля паузы; файл
    озвучки сам начинается за `head` до слова (запас среза), поэтому заход —
    доля минус `head`, не меньше нуля. Сверху его держит звук уходящей сцены:
    её файл кончается через `tail_pad` после последнего слова, и склейка его не
    режет. Хронометраж от захода не меняется — сколько получила новая сцена,
    столько отдала уходящая."""
    wanted = CUT_IN_SHARE[cls] * pause - head
    room = pause - tail_pad - head
    return round(max(0.0, min(wanted, room)), 3)


def scene_lead_in(scene: Scene) -> float:
    """Заход сцены: через сколько секунд после её первого кадра начинается её
    озвучка — ноль её `.srt`. От него отсчитывают сборка (звук, слова,
    субтитры, замер пауз), монтажный лист пульта и материал монтажёра. Без
    отдельной озвучки (нарезка: звук живёт в клипе, немая сцена) — ноль.

    Плашка (`Scene.overlay`) от захода НЕ отсчитывается: она часть кадра и
    появляется вместе с ним, в той же тишине до слова. Сдвинутая на заход, она
    выскакивала бы через четверть секунды после склейки второй сменой картинки.
    Эффекты, звуки и ступени музыки плана живут в секундах финала, а не сцены,
    и сдвигать их тоже нечего."""
    if not scene.assets.audio:
        return 0.0
    return max(0.0, scene.measured_sec.get(LEAD_IN_KEY, 0.0))


def sentence_ends(narration: str, words: list[Word]) -> list[bool]:
    """Какие слова таймингов кончают предложение. Тайминги edge-tts знаков не
    несут («Ассаляму», «алейкум»), знаки — в тексте реплики; слова сверяются по
    написанию, число «55» против «пятьдесят пять» — это несовпадение, которое
    `SequenceMatcher` обходит. Последнее слово реплики берёт её концовку."""
    flags = [False] * len(words)
    if not words:
        return flags
    tokens = [(norm(t), ending(t) != "clause") for t in narration.split()]
    tokens = [t for t in tokens if t[0]]
    matcher = SequenceMatcher(
        None, [t for t, _ in tokens], [norm(w.text) for w in words], autojunk=False
    )
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            flags[b + k] = tokens[a + k][1]
    flags[-1] = narration.strip() != "" and ending(narration) != "clause"
    return flags


@dataclass
class TimedWord:
    """Слово на шкале готового ролика."""

    scene_id: str
    start: float
    end: float
    sentence_end: bool = False


@dataclass
class SpeechPause:
    scene_id: str  # сцена, после слова которой пауза
    at_sec: float
    sec: float


@dataclass
class SpeechPauses:
    """Паузы между словами готового ролика: медиана на конце предложения и
    паузы дольше `LONG_PAUSE_SEC`. Хвост после последнего слова сюда не входит
    — у него свой замер (`FinalOutput.silence_tail_sec`)."""

    words: int  # сколько слов мерили: 0 — мерить было нечего, и «пауз нет» не ответ
    sentence_median_sec: float | None
    long: list[SpeechPause]


def speech_pauses(words: list[TimedWord]) -> SpeechPauses:
    ordered = sorted(words, key=lambda w: w.start)
    sentence: list[float] = []
    long: list[SpeechPause] = []
    for cur, nxt in zip(ordered, ordered[1:], strict=False):
        gap = max(0.0, nxt.start - cur.end)
        if cur.sentence_end:
            sentence.append(gap)
        if gap > LONG_PAUSE_SEC:
            long.append(SpeechPause(cur.scene_id, round(cur.end, 3), round(gap, 3)))
    median = round(statistics.median(sentence), 3) if sentence else None
    return SpeechPauses(len(ordered), median, long)
