"""Выбор моментов: транскрипт длинного видео → кандидаты в шортсы.

Уровень источника, а не эпизода: эпизодов ещё нет — они появляются из выбранных
кандидатов (`videoforge source pick`). Артефакт — `sources/<id>/highlights.json`
плюс отчёт `highlights.html`, который читает человек.

Разделение труда то же, что у монтажёра (`stages/edit.py`): **модель ранжирует
и объясняет, числа считает python**. Модель называет момент и говорит, чем он
цепляет; границы привязываются к предложениям транскрипта, длина проверяется
против 20–75 секунд, пересечения сливаются, а «здесь пересматривали чаще» берётся
из тепловой карты YouTube — измеренного сигнала, а не выдуманной «оценки
виральности», которая плывёт между прогонами.

Блок (длина вне диапазона, неразобранное время) → один повтор с причинами, как
в `edit`. После повтора негодные кандидаты выбрасываются, а не роняют команду:
у источника нет ворот и статусов, и терять десять хороших моментов из-за одного
двухсекундного нельзя. Что выброшено — написано в `warnings` и в отчёте.

Момент можно назвать и руками — отрезком времени (`source pick --at
20:41-21:33 --title …`, `candidate_from_span`). Выбор моментов недетерминирован:
два разбора одного подкаста дали разные списки, и хорошие моменты первого во
втором просто исчезли; повтор разбора их не возвращает, а отрезок — да. Границы
у такого момента притягиваются к предложениям ТЕМ ЖЕ шагом, что у кандидатов
модели (`snap_span`), длина меряется против тех же 20–75 с, но вне их это
предупреждение с числом, а не отказ: человек выбирал сам.

Многочасовой источник (стрим на 4–8 часов) в один вызов не влезает — либо
буквально, либо модель теряет середину. Транскрипт длиннее `CHUNK_MAX_CHARS`
режется на куски (`chunk_plan`: по главам, иначе по предложениям, с
перекрытием `OVERLAP_SEC`), широкий поиск идёт по каждому куску своим вызовом
со своей квотой, а критик читает кандидатов всех кусков разом (или пачками,
если не влезли). Часовой подкаст (~55 тысяч знаков строк) остаётся одним
вызовом, как раньше; подкаст на 2,5 часа (345 тысяч знаков вместе с
playbook, ~250 без него) уходит в 4–5 кусков — по замеру это быстрее и
предсказуемее одного вызова на 158–569 с.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from videoforge.clips.source import (
    HEAT_SKIP_HEAD_SEC,
    TRANSCRIPT_FILE,
    Chapter,
    HeatPoint,
    Sentence,
    SourceInfo,
    Transcript,
    chapter_at,
    top_peaks,
)
from videoforge.core.io import read_model, write_model
from videoforge.providers.base import LlmProvider

HIGHLIGHTS_FILE = "highlights.json"

# Длительность момента. Ниже 20 с шортс не успевает ни поставить вопрос, ни
# ответить; выше 75 с его досматривает меньшинство, а площадки любят короткое.
MIN_SEC = 20.0
MAX_SEC = 75.0
DEFAULT_COUNT = 12
# Доля пересечения (от более короткого кандидата), после которой это один и тот
# же момент, названный дважды
OVERLAP_LIMIT = 0.5
# Насколько далеко назад тянем начало к началу реплики. Ответ гостя длится
# минутами, и «к началу реплики» без потолка превратило бы сорокасекундный
# момент в шестиминутный; короткая подводка — другое дело, её берём целиком
TURN_LEAD_MAX_SEC = 8.0
SNAP_SLACK_SEC = 0.5  # допуск НА СТАРТЕ: модель промахивается по времени на доли секунды
# Конец момента. Куски автосубтитров кончаются по паузе, длине и смене
# говорящего — то есть могут оборваться на «я». Дотягиваем до конца мысли:
# до знака препинания или до паузы, но не дальше, чем на столько секунд
SENTENCE_END = (".", "?", "!", "…")
TAIL_PAUSE_SEC = 0.4
TAIL_EXTEND_MAX_SEC = 3.0
START_SKIP_MAX_SEC = 3.0  # столько же на старте: с хвоста чужой фразы сдвигаемся вперёд
LLM_ATTEMPTS = 2  # ответ + один повтор по причинам; третьего нет — дальше человек
PEAKS_IN_PROMPT = 12  # сколько пиков карты пересмотров показать модели
# Сколько кандидатов просить сверх нужного. Один заход даёт «первый попавшийся
# список»: три прогона на одном подкасте дали три разных набора. Широкая сеть
# плюс критик устойчивее удачного броска — тот же приём, что у сценариста
# (черновик → критик), и та же причина: закрывать слабое надёжнее, чем угадывать
# лучшее с первого раза
OVERSAMPLE = 2
TITLE_MAX_CHARS = 90  # заголовок Shorts обрезается в ленте примерно здесь
# подпись над кадром (`video.headline`): у референсных каналов нарезок 50–85 знаков в 2–4 строки над
# кадром; длиннее — это абзац, и он съедает место, где зритель смотрит кадр
HEADLINE_MAX_CHARS = 90
# выброс реплик (`video.tighten`). Короче этого вырезанное не выбрасывается:
# склейка ради полсекунды дёргает кадр сильнее, чем экономит время
MIN_DROP_SEC = 1.0
# ниже этого момент после выброса не сжимается вовсе — критик выбросил лишнего.
# Меньше MIN_SEC намеренно: сцена-мем у референсного канала длится 19.7–29.2 с, а выброс
# реплик и должен делать ролик короче
TIGHT_MIN_SEC = 15.0
# Сколько знаков транскрипта уходит в один вызов. Порог — из замера claude_cli
# (llm-call-cost-model): вход в 51 тысячу знаков добавил к вызову 0.3 с, в 64
# тысячи — ~7 с, а весь подкаст на 2,5 часа (345 тысяч знаков) дал 158 и 569 с
# на двух ОДИНАКОВЫХ вызовах при ответе в три килобайта — втрое дороже формулы
# и с трёхкратным разбросом. То есть вход бесплатен до ~60 тысяч знаков и дорог
# на сотнях; стрим на 4–8 часов (40–60 тысяч слов) вдобавок рискует не влезть
# в контекст или потерять середину. Считаются строки транскрипта: правила и
# blueprint в промпте постоянны и с длиной записи не растут
CHUNK_MAX_CHARS = 60_000
# Перекрытие соседних кусков. Момент максимальной длины (`MAX_SEC`) плюс всё,
# что python дотянет при привязке (`TURN_LEAD_MAX_SEC` + `TAIL_EXTEND_MAX_SEC`)
# — 86 с: кусок, начатый на 90 с раньше своей территории, видит целиком любой
# момент, который сел на шов с предыдущим. Названный дважды сливается тем же
# `_merge_overlaps`, что и дубли внутри одного ответа
OVERLAP_SEC = 90.0
CHUNK_MIN_COUNT = 2  # квота кандидатов на кусок пропорциональна длине, но не меньше


class Piece(BaseModel):
    """Кусок момента, который остаётся после выброса скучных реплик: секунды
    источника и его текст. Каждый кусок станет своей сценой эпизода."""

    start_sec: float
    end_sec: float
    text: str = ""


class Candidate(BaseModel):
    """Момент-кандидат: что предложила модель, приведённое к предложениям."""

    id: str  # h01, h02 … в порядке отчёта; at2041 — отрезок, названный человеком (20:41)
    start_sec: float
    end_sec: float
    title: str = ""
    hook: str = ""  # чем цепляет первая секунда
    why: str = ""  # почему это момент
    kind: str = ""  # story | claim | conflict | humor | advice
    # упаковка под площадку, её пишет критик выжившим моментам
    publish_title: str = ""  # что зритель видит в ленте
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    verdict: str = ""  # keep | drop — решение критика; пусто, если критика не было
    verdict_why: str = ""  # чем держит или чего зритель не поймёт
    chapter: str = ""
    heat: float = 0.0  # максимум тепловой карты внутри момента; 0 — карты нет
    speaker_turns: int = 0  # смен говорящего внутри момента
    text: str = ""  # предложения целиком — их же получит сцена эпизода
    # подпись над кадром (`video.headline`): реакция зрителя, пишет критик
    headline: str = ""
    # что остаётся после выброса скучных реплик (`video.tighten`): куски подряд
    # жёсткими склейками. Пусто — момент одним куском от start до end
    pieces: list[Piece] = Field(default_factory=list)
    dropped_text: list[str] = Field(default_factory=list)  # выброшенные реплики — для отчёта

    @property
    def duration_sec(self) -> float:
        return round(self.end_sec - self.start_sec, 2)

    @property
    def played_sec(self) -> float:
        """Сколько момент длится в шортсе: сумма кусков, а без выброса — весь."""
        if not self.pieces:
            return self.duration_sec
        return round(sum(p.end_sec - p.start_sec for p in self.pieces), 2)


class Highlights(BaseModel):
    source_id: str = ""
    llm: str = ""
    generated_at: datetime | None = None
    requested: int = DEFAULT_COUNT  # сколько кандидатов просили у модели
    # вызовов модели всего: по одному на кусок транскрипта (плюс повтор по блокам)
    # и по одному на пачку критика; 3 у короткого подкаста — поиск, повтор, критик
    calls: int = 0
    chunks: int = 1  # на сколько кусков резали транскрипт; 1 — влез целиком
    candidates: list[Candidate] = Field(default_factory=list)
    # моменты, которые критик закрыл: с причиной, чтобы человек мог не согласиться
    rejected: list[Candidate] = Field(default_factory=list)
    fixes: list[str] = Field(default_factory=list)  # что подвинул python
    warnings: list[str] = Field(default_factory=list)  # что выброшено и почему


# ---------------------------------------------------------------- время


def format_clock(seconds: float) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}"


_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,3}):(\d{1,2}(?:[.,]\d+)?)$")
# границы отрезка разделяет любое тире: с клавиатуры идёт «-», из отчёта копируется «–»
_SPAN_SEP = re.compile(r"\s*[-–—]\s*")


def _clock_seconds(m: re.Match[str]) -> float:
    h, mm, ss = m.groups()
    return int(h or 0) * 3600 + int(mm) * 60 + float(ss.replace(",", "."))


def parse_clock(value: Any) -> float | None:
    """'0:12:43' | '12:43' | 763 | '763.5' → секунды. Не разобрали — None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if m := _CLOCK.match(text):
        return _clock_seconds(m)
    try:
        return float(text)
    except ValueError:
        return None


def parse_span(text: str) -> tuple[float, float]:
    """'20:41-21:33' | '1:20:41-1:21:33' → (начало, конец) в секундах.

    Только часы, без голых секунд («1241-1293» — ошибка): человек читает 20:41
    в плеере и в отчёте, и команда обязана понимать ровно то, что он видит.
    Дробные секунды («20:41.5») допустимы, конец строго позже начала."""
    parts = _SPAN_SEP.split(text.strip())
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"отрезок пишется как 20:41-21:33 или 1:20:41-1:21:33, пришло {text!r}")
    bounds: list[float] = []
    for part in parts:
        m = _CLOCK.match(part)
        if m is None:
            raise ValueError(f"не разобрать время {part!r}: пиши как 20:41 или 1:20:41")
        bounds.append(_clock_seconds(m))
    start, end = bounds
    if end <= start:
        raise ValueError(f"конец {format_clock(end)} не позже начала {format_clock(start)}")
    return start, end


def heat_at(
    heatmap: list[HeatPoint],
    start_sec: float,
    end_sec: float,
    *,
    skip_head_sec: float = HEAT_SKIP_HEAD_SEC,
) -> float:
    """Самое пересматриваемое окно внутри момента. Карты нет — 0.

    Стартовое окно не считается — то же правило, что у `top_peaks`: начало
    смотрят все, кто открыл видео, и 1.0 там означает «ролик запустили», а не
    «сюда возвращались». Первый живой прогон это и показал: момент на 28-й
    секунде получил heat 1.00 и встал первым в списке, не имея за собой ни
    одного измеренного пересмотра."""
    values = [
        h.value
        for h in heatmap
        if h.start_sec >= skip_head_sec and h.start_sec < end_sec and h.end_sec > start_sec
    ]
    return round(max(values), 3) if values else 0.0


# ---------------------------------------------------------------- границы по предложениям


def starts_thought(sentences: list[Sentence], i: int) -> bool:
    """Мысль на этом «предложении» начинается, а не продолжается.

    То же правило, что у конца, только с другой стороны: если предыдущий кусок
    оборвался без знака и без паузы, этот — его хвост. Первый живой шортс на
    65 секунд начинался со слова «там.» — хвоста фразы «…за нас пусть поболеет
    там», которой зритель не слышал."""
    return i <= 0 or ends_thought(sentences, i - 1)


def snap_start(sentences: list[Sentence], t: float) -> int:
    """Индекс предложения, с которого начинается момент.

    Начало — начало ближайшего предложения не позже указанного, но не обрывок
    чужой фразы: с хвоста незакрытой мысли сдвигаемся вперёд. Если получившееся
    начало оказалось в середине чужой реплики, а реплика началась недавно, берём
    её с начала — зритель не должен входить в середину ответа. Далёкое начало
    реплики (монолог на минуты) не трогаем: потолок `TURN_LEAD_MAX_SEC`."""
    i = 0
    for k, s in enumerate(sentences):
        if s.start_sec <= t + SNAP_SLACK_SEC:
            i = k
        else:
            break
    limit = sentences[i].start_sec + START_SKIP_MAX_SEC if sentences else 0.0
    while (
        not starts_thought(sentences, i)
        and i + 1 < len(sentences)
        and sentences[i + 1].start_sec <= limit
    ):
        i += 1
    j = i
    while j > 0 and not sentences[j].speaker_turn:
        j -= 1
    if (
        j < i
        and starts_thought(sentences, j)
        and sentences[i].start_sec - sentences[j].start_sec <= TURN_LEAD_MAX_SEC
    ):
        i = j
    return i


def ends_thought(sentences: list[Sentence], i: int) -> bool:
    """Мысль на этом «предложении» закончилась: знак конца фразы или пауза после.

    «Предложение» у автосубтитров — кусок по паузе, длине и смене говорящего, а
    не по смыслу: он спокойно кончается на «я». Знак препинания в дорожке есть,
    и пауза измерима — этого хватает, чтобы отличить конец мысли от разрыва."""
    text = sentences[i].text.rstrip()
    if text.endswith(SENTENCE_END):
        return True
    if i + 1 >= len(sentences):
        return True
    return sentences[i + 1].start_sec - sentences[i].end_sec >= TAIL_PAUSE_SEC


def snap_end(sentences: list[Sentence], t: float, *, first: int) -> int:
    """Индекс последнего предложения момента.

    Назад не двигаем никогда: допуск, который на старте безобиден (момент
    начнётся чуть раньше), на конце стоит панчлайна. Первый живой шортс кончался
    на «Я вот когда я смотрю, кто Реалу забивает, я» — следующие 0.9 секунды
    «сразу в блейклист» были шуткой, ради которой момент и выбрали. Поэтому
    граница округляется вверх и дотягивается до конца мысли."""
    last = first
    for k in range(first, len(sentences)):
        last = k
        if sentences[k].end_sec >= t:
            break
    limit = sentences[last].end_sec + TAIL_EXTEND_MAX_SEC
    while (
        not ends_thought(sentences, last)
        and last + 1 < len(sentences)
        and sentences[last + 1].end_sec <= limit
    ):
        last += 1
    return last


def snap_span(
    start: float,
    end: float,
    *,
    sentences: list[Sentence],
    chapters: list[Chapter],
    heatmap: list[HeatPoint],
) -> Candidate:
    """Отрезок → кандидат с границами по предложениям, главой, heat и текстом.

    Один шаг на момент от модели и на момент от человека: привязка одна,
    иначе два пути разошлись бы, и отрезок, названный руками, начинался бы с
    обрывка чужой фразы, от которого кандидатов модели уже вылечили. Без id и
    без слов о моменте — их пишет тот, кто отрезок назвал."""
    first = snap_start(sentences, start)
    last = snap_end(sentences, end, first=first)
    start_sec, end_sec = sentences[first].start_sec, sentences[last].end_sec
    return Candidate(
        id="",
        start_sec=round(start_sec, 2),
        end_sec=round(end_sec, 2),
        chapter=chapter_at(chapters, start_sec) or sentences[first].chapter,
        heat=heat_at(heatmap, start_sec, end_sec),
        speaker_turns=sum(1 for s in sentences[first + 1 : last + 1] if s.speaker_turn),
        text=" ".join(s.text for s in sentences[first : last + 1]),
    )


def moment_sentences(sentences: list[Sentence], cand: Any) -> list[Sentence]:
    """Предложения транскрипта внутри момента — те же, из которых собран его
    текст (`snap_span` ставит границы ровно по ним)."""
    return [
        s
        for s in sentences
        if s.start_sec >= cand.start_sec - 0.05 and s.end_sec <= cand.end_sec + 0.05
    ]


def tighten_pieces(lines: list[Sentence], drop: set[int]) -> tuple[list[Piece], list[str], str]:
    """Номера реплик, которые критик выбросил (1 — первая реплика момента), →
    куски, которые остаются, выброшенные тексты и пометка, если выброс не принят.

    Первая и последняя реплики не выбрасываются никогда: начало держит хук,
    конец — развязку. Вырезанный промежуток короче `MIN_DROP_SEC` склеивается
    обратно — склейка дороже полсекунды. Если после выброса момент короче
    `TIGHT_MIN_SEC`, не выбрасывается ничего: значит, выбросили суть."""
    if not lines:
        return [], [], ""
    wanted = {n for n in drop if 1 < n < len(lines)}
    runs: list[list[Sentence]] = []
    removed: list[str] = []
    for n, s in enumerate(lines, start=1):
        if n in wanted:
            removed.append(s.text)
            continue
        if runs and n - 1 not in wanted:
            runs[-1].append(s)
        else:
            runs.append([s])
    pieces = [
        Piece(start_sec=r[0].start_sec, end_sec=r[-1].end_sec, text=" ".join(x.text for x in r))
        for r in runs
    ]
    # короткий вырез — не склейка: соседние куски сливаются обратно
    merged: list[Piece] = []
    for piece in pieces:
        if merged and piece.start_sec - merged[-1].end_sec < MIN_DROP_SEC:
            gap = [
                s.text
                for s in lines
                if s.start_sec >= merged[-1].end_sec - 0.01 and s.end_sec <= piece.start_sec + 0.01
            ]
            removed = [t for t in removed if t not in gap]
            merged[-1] = Piece(
                start_sec=merged[-1].start_sec,
                end_sec=piece.end_sec,
                text=" ".join([merged[-1].text, *gap, piece.text]),
            )
        else:
            merged.append(piece)
    if len(merged) <= 1:
        return [], [], ""
    played = sum(p.end_sec - p.start_sec for p in merged)
    if played < TIGHT_MIN_SEC:
        return (
            [],
            [],
            (f"выброс реплик не принят: осталось бы {played:.0f} с, меньше {TIGHT_MIN_SEC:g} с"),
        )
    return merged, removed, ""


def bounds_note(start: float, end: float, cand: Candidate) -> str:
    """Что границы подвинуты — только если это видно на часах. Сдвиг на доли
    секунды внутри одного предложения — не новость, а двенадцать строк
    «0:00:28 → 0:00:28» в отчёте прячут те две, где границу правда двигали."""
    before = f"{format_clock(start)}–{format_clock(end)}"
    after = f"{format_clock(cand.start_sec)}–{format_clock(cand.end_sec)}"
    return f"границы {before} → {after} (по предложениям)" if before != after else ""


def length_note(cand: Candidate) -> str:
    """Длина вне 20–75 с — числом; пусто, если в норме. Что с этим делать,
    решает вызывающий: у модели это блок, у человека — предупреждение."""
    if MIN_SEC <= cand.duration_sec <= MAX_SEC:
        return ""
    return f"{cand.duration_sec:.0f} с — момент должен длиться {MIN_SEC:g}–{MAX_SEC:g} с"


@dataclass
class Resolved:
    candidates: list[Candidate] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # причины повтора; кандидат выброшен
    fixes: list[str] = field(default_factory=list)  # подвинуто, слито, обрезано


def _label(item: dict[str, Any], number: int) -> str:
    title = str(item.get("title") or "").strip()
    return f"кандидат {number} «{title}»" if title else f"кандидат {number}"


def resolve_candidates(
    raw: Any,
    *,
    sentences: list[Sentence],
    chapters: list[Chapter],
    heatmap: list[HeatPoint],
    limit: int = DEFAULT_COUNT,
) -> Resolved:
    """Ответ модели → кандидаты с границами по предложениям, heat и текстом.

    Порядок: разобрать время → привязать к предложениям → проверить длину →
    слить пересечения → отсортировать по heat. Негодное не чинится молча:
    каждая выброшенная запись оставляет причину в `problems` — её получит модель
    на повторе, а если повтор уже был, увидит человек.

    Два шага, потому что у длинного транскрипта ответов несколько — по одному
    на кусок: разбор (`parse_candidates`) идёт по каждому ответу и даёт повтор
    по причинам своему куску, доводка (`finish_candidates`) — один раз по всем,
    иначе момент, названный на шве двумя кусками, остался бы дважды."""
    out = parse_candidates(raw, sentences=sentences, chapters=chapters, heatmap=heatmap)
    out.candidates = finish_candidates(out.candidates, out.fixes, limit=limit)
    return out


def parse_candidates(
    raw: Any,
    *,
    sentences: list[Sentence],
    chapters: list[Chapter],
    heatmap: list[HeatPoint],
) -> Resolved:
    """Один ответ модели → кандидаты в порядке ответа, без слияния и без
    окончательных id: их даёт `finish_candidates`, когда все ответы собраны."""
    out = Resolved()
    if not sentences:
        out.problems.append("транскрипт пустой — выбирать не из чего")
        return out
    items = raw.get("candidates") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        out.problems.append(f"ожидался объект {{'candidates': [...]}}, пришло {type(raw).__name__}")
        return out

    kept: list[Candidate] = []
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            out.problems.append(f"кандидат {number}: ожидался объект, пришло {type(item).__name__}")
            continue
        start = parse_clock(item.get("start"))
        end = parse_clock(item.get("end"))
        if start is None or end is None:
            out.problems.append(
                f"{_label(item, number)}: не разобрать время "
                f"({item.get('start')!r} → {item.get('end')!r}); пиши как 0:12:43"
            )
            continue
        if end <= start:
            out.problems.append(
                f"{_label(item, number)}: конец {format_clock(end)} не позже начала "
                f"{format_clock(start)}"
            )
            continue
        cand = snap_span(start, end, sentences=sentences, chapters=chapters, heatmap=heatmap)
        if moved := bounds_note(start, end, cand):
            out.fixes.append(f"{_label(item, number)}: {moved}")
        if problem := length_note(cand):
            out.problems.append(
                f"{_label(item, number)} "
                f"({format_clock(cand.start_sec)}–{format_clock(cand.end_sec)}): {problem}"
            )
            continue
        if not str(item.get("why") or "").strip():
            out.fixes.append(f"{_label(item, number)}: без «почему» — решать человеку")
        cand.id = f"h{len(kept) + 1:02d}"  # временный: настоящий — после сортировки
        cand.title = str(item.get("title") or "").strip()
        cand.hook = str(item.get("hook") or "").strip()
        cand.why = str(item.get("why") or "").strip()
        cand.kind = str(item.get("kind") or "").strip().lower()
        kept.append(cand)
    out.candidates = kept
    return out


def finish_candidates(
    candidates: list[Candidate], fixes: list[str], *, limit: int = DEFAULT_COUNT
) -> list[Candidate]:
    """Слить пересечения → отсортировать по heat → обрезать → дать id.

    Вход — кандидаты всех ответов в одном списке (у нескольких кусков —
    чередованием, см. `interleave`): слияние через шов между кусками ничем не
    отличается от слияния двух названий одного места в одном ответе."""
    kept = _merge_overlaps(list(candidates), fixes)
    # heat — измеренный сигнал, он и решает; при равном (или без карты пересмотров)
    # остаётся порядок модели: она перечисляет лучшее первым
    rank = {id(c): i for i, c in enumerate(kept)}
    kept.sort(key=lambda c: (-c.heat, rank[id(c)]))
    if len(kept) > limit:
        fixes.append(f"кандидатов {len(kept)} — оставлено {limit} с самым высоким heat")
        kept = kept[:limit]
    for n, cand in enumerate(kept, start=1):
        cand.id = f"h{n:02d}"
    return kept


def _merge_overlaps(candidates: list[Candidate], fixes: list[str]) -> list[Candidate]:
    """Два кандидата из одного места — один момент. Остаётся тот, где heat выше
    (при равном — тот, что длиннее: у него больше шансов быть самодостаточным)."""
    kept: list[Candidate] = []
    for cand in candidates:
        clash = None
        for other in kept:
            overlap = min(cand.end_sec, other.end_sec) - max(cand.start_sec, other.start_sec)
            shorter = min(cand.duration_sec, other.duration_sec)
            if shorter > 0 and overlap / shorter > OVERLAP_LIMIT:
                clash = other
                break
        if clash is None:
            kept.append(cand)
            continue
        winner, loser = (
            (cand, clash)
            if (cand.heat, cand.duration_sec) > (clash.heat, clash.duration_sec)
            else (clash, cand)
        )
        fixes.append(
            f"«{loser.title or format_clock(loser.start_sec)}» пересекается с "
            f"«{winner.title or format_clock(winner.start_sec)}» — оставлен второй "
            f"(heat {winner.heat:g} против {loser.heat:g})"
        )
        if winner is cand:
            kept[kept.index(clash)] = cand
    return kept


# ---------------------------------------------------------------- момент от человека

# hook и why у момента по времени: критика не было, и запись говорит это прямо,
# а не изображает «чем цепляет первая секунда» словами, которых никто не писал
HUMAN_WHY = "выбран человеком по времени"


def span_id(start: float) -> str:
    """Имя момента от человека — по минуте и секунде, которые он НАЗВАЛ:
    `at2041` для 20:41, `at8041` для 1:20:41. Не по привязанным границам:
    в команде и в списке эпизодов человек ищет то, что набрал."""
    m, s = divmod(int(start), 60)
    return f"at{m:02d}{s:02d}"


def candidate_from_span(
    start: float,
    end: float,
    *,
    title: str,
    sentences: list[Sentence],
    chapters: list[Chapter],
    heatmap: list[HeatPoint],
) -> tuple[Candidate, list[str]]:
    """Отрезок, названный человеком, → кандидат той же формы, что у модели,
    плюс заметки: что подвинуто и что ПРОВЕРИТЬ.

    Привязка к предложениям та же (`snap_span`), длина меряется против тех же
    20–75 с — но вне их это не отказ, а предупреждение с числом: человек
    выбирал сам, у него могут быть причины, а модель, взявшая 84 секунды,
    как раз и была поводом дать ему слово. Критика здесь нет, поэтому
    заголовок обязателен — больше его дать некому, — а упаковки (описание,
    хэштеги) у момента не будет: её спросит у модели `publish`."""
    if not title.strip():
        raise ValueError("у момента по времени нет критика — заголовок даёт человек (--title)")
    if not sentences:
        raise ValueError("транскрипт пустой — привязывать границы не к чему")
    if start >= sentences[-1].end_sec:
        raise ValueError(
            f"отрезок начинается в {format_clock(start)}, а транскрипт кончается в "
            f"{format_clock(sentences[-1].end_sec)}"
        )
    cand = snap_span(start, end, sentences=sentences, chapters=chapters, heatmap=heatmap)
    cand.id = span_id(start)
    cand.title = title.strip()
    cand.hook = cand.why = HUMAN_WHY
    notes = [moved] if (moved := bounds_note(start, end, cand)) else []
    if problem := length_note(cand):
        notes.append(f"ПРОВЕРЬ: {problem}")
    return cand, notes


# ---------------------------------------------------------------- файлы и свежесть


def highlights_path(sdir: Path) -> Path:
    return sdir / HIGHLIGHTS_FILE


def load_highlights(sdir: Path) -> Highlights | None:
    path = highlights_path(sdir)
    return read_model(path, Highlights) if path.exists() else None


def save_highlights(sdir: Path, data: Highlights) -> None:
    write_model(highlights_path(sdir), data)


def is_fresh(sdir: Path, existing: Highlights, llm_name: str) -> bool:
    """Как у плана монтажа: файл новее своего входа и сделан текущей моделью.
    Правка руками тоже проходит — она новее и побеждает."""
    if existing.llm != llm_name or not existing.candidates:
        return False
    stamp = highlights_path(sdir).stat().st_mtime
    source = sdir / TRANSCRIPT_FILE
    return not (source.exists() and source.stat().st_mtime > stamp)


# ---------------------------------------------------------------- куски транскрипта


@dataclass(frozen=True)
class Chunk:
    """Кусок транскрипта на один вызов широкого поиска.

    Окно куска (`first`…`last`) шире его территории (`own_first`…`last`):
    голова — хвост предыдущего куска на `OVERLAP_SEC`, чтобы момент на шве
    был виден целиком хоть одному вызову. Квота кандидатов считается по
    своим строкам (`chars`), не по окну — иначе перекрытие считалось бы дважды."""

    index: int  # с единицы: «кусок 3/7»
    first: int  # первое предложение окна, включая перекрытие с предыдущим куском
    own_first: int  # первое своё предложение: до него — хвост предыдущего куска
    last: int  # последнее предложение окна, включительно
    chars: int  # знаков своих строк — доля куска в квоте кандидатов
    start_sec: float  # окно по часам: с перекрытием
    own_start_sec: float
    end_sec: float


def _line_chars(s: Sentence) -> int:
    """Длина строки транскрипта в промпте: «[0:12:43] (глава) >> текст». Часы,
    скобки и маркер — 14 знаков сверх текста и названия главы. Точность до
    знака не нужна: порог взят из замера с трёхкратным разбросом, а не из
    контракта, и сама разметка живёт в `highlight_prompts.transcript_text`."""
    return len(s.text) + len(s.chapter) + 14


def _chapter_runs(sentences: list[Sentence], chapters: list[Chapter]) -> list[tuple[int, int]]:
    """Отрезки подряд идущих предложений одной главы: (первое, последнее).
    Без глав — весь транскрипт одним отрезком."""
    runs: list[tuple[int, int]] = []
    current = None
    for i, s in enumerate(sentences):
        key = chapter_at(chapters, s.start_sec) if chapters else ""
        if runs and key == current:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
            current = key
    return runs


def chunk_plan(
    sentences: list[Sentence],
    chapters: list[Chapter],
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap_sec: float = OVERLAP_SEC,
) -> list[Chunk]:
    """Транскрипт → куски не длиннее `max_chars` знаков своих строк.

    Влезает целиком — один кусок без перекрытия: поведение то же, что до
    кусков. Иначе границы идут по главам: глава — единица смысла, и момент
    редко живёт на её стыке. Соседние главы сливаются в один кусок, пока
    влезают (тридцать девять глав — не тридцать девять вызовов), глава
    длиннее лимита режется по предложениям. Без глав — просто по
    предложениям, жадно до лимита. Хвостовой кусок может выйти коротким —
    это не беда: квота ему достаётся по длине с полом `CHUNK_MIN_COUNT`.

    Перекрытие: окно каждого куска, кроме первого, начинается на `overlap_sec`
    раньше своей территории, по началу предложения. Одностороннее, потому что
    этого хватает: любой момент, севший на шов, целиком лежит в окне
    следующего куска (см. расчёт у `OVERLAP_SEC`)."""
    if not sentences:
        return []
    sizes = [_line_chars(s) for s in sentences]
    acc = [0]
    for size in sizes:
        acc.append(acc[-1] + size)

    def span(first: int, last: int) -> int:
        return acc[last + 1] - acc[first]

    n = len(sentences)
    if span(0, n - 1) <= max_chars:
        return [
            Chunk(
                index=1,
                first=0,
                own_first=0,
                last=n - 1,
                chars=span(0, n - 1),
                start_sec=sentences[0].start_sec,
                own_start_sec=sentences[0].start_sec,
                end_sec=sentences[-1].end_sec,
            )
        ]

    # 1. отрезки: по главам; глава длиннее лимита — по предложениям
    segments: list[tuple[int, int]] = []
    for first, last in _chapter_runs(sentences, chapters):
        start = first
        for i in range(first, last + 1):
            if i > start and span(start, i) > max_chars:
                segments.append((start, i - 1))
                start = i
        segments.append((start, last))
    # 2. соседние отрезки сливаются, пока влезают
    merged: list[tuple[int, int]] = []
    for first, last in segments:
        if merged and span(merged[-1][0], last) <= max_chars:
            merged[-1] = (merged[-1][0], last)
        else:
            merged.append((first, last))
    # 3. перекрытие: окно начинается раньше своей территории
    chunks: list[Chunk] = []
    for k, (own_first, last) in enumerate(merged, start=1):
        first = own_first
        floor = sentences[own_first].start_sec - overlap_sec
        while first > 0 and sentences[first - 1].start_sec >= floor:
            first -= 1
        chunks.append(
            Chunk(
                index=k,
                first=first,
                own_first=own_first,
                last=last,
                chars=span(own_first, last),
                start_sec=sentences[first].start_sec,
                own_start_sec=sentences[own_first].start_sec,
                end_sec=sentences[last].end_sec,
            )
        )
    return chunks


def chunk_quota(chunk: Chunk, *, total_chars: int, count: int) -> int:
    """Сколько кандидатов просить у куска: доля от `OVERSAMPLE` × запрошенного
    по длине куска, вверх, но не меньше `CHUNK_MIN_COUNT`. У единственного
    куска — ровно `OVERSAMPLE` × запрошенного, как было."""
    if total_chars <= 0:
        return CHUNK_MIN_COUNT
    share = math.ceil(count * OVERSAMPLE * chunk.chars / total_chars)
    return max(CHUNK_MIN_COUNT, share)


def heat_in_window(heatmap: list[HeatPoint], chunk: Chunk) -> list[HeatPoint]:
    """Окна карты пересмотров, задевающие окно куска. Пики чужого куска модели
    не показываем: они зовут смотреть туда, где у неё нет строк."""
    return [h for h in heatmap if h.start_sec < chunk.end_sec and h.end_sec > chunk.start_sec]


def interleave(answers: list[list[Candidate]]) -> list[Candidate]:
    """Ответы кусков в один список чередованием: первый кандидат каждого куска,
    потом вторые… Модель перечисляет лучшее первым, и без карты пересмотров
    этот порядок — единственный ранг; склейка «кусок за куском» отдала бы
    первые места началу записи, а обрезка по лимиту выбросила бы её конец.
    У одного куска — его ответ как есть."""
    out: list[Candidate] = []
    for k in range(max((len(a) for a in answers), default=0)):
        out += [a[k] for a in answers if k < len(a)]
    return out


CRITIQUE_ITEM_CHARS = 200  # шапка кандидата у критика сверх текста: время, heat, title, why


def critique_batches(
    candidates: list[Candidate], *, max_chars: int = CHUNK_MAX_CHARS
) -> list[list[Candidate]]:
    """Кандидаты критику пачками, если их полный текст не влезает в один вызов.

    Критик судит каждый момент сам по себе — четыре условия, кончается ли чем-то,
    поймёт ли посторонний — и НЕ ранжирует: порядок оставшихся задаёт измеренный
    heat (`apply_verdicts`), а не его мнение. Поэтому пачки вердиктов не меняют.
    Компромисс один: два момента об одном и том же из разных мест записи он в
    разных пачках рядом не увидит. В одной пачке он их сегодня тоже не сводит —
    сливаются только пересечения по времени, — так что это не потеря, а граница,
    которую надо помнить, если критика когда-нибудь попросят сравнивать.
    Порядок кандидатов сохраняется, кандидат длиннее лимита идёт своей пачкой."""
    batches: list[list[Candidate]] = []
    size = 0
    for cand in candidates:
        chars = len(cand.text) + CRITIQUE_ITEM_CHARS
        if batches and size + chars <= max_chars:
            batches[-1].append(cand)
            size += chars
        else:
            batches.append([cand])
            size = chars
    return batches


# ---------------------------------------------------------------- вызов модели


def _ask_json(
    llm: LlmProvider, prompt: str, *, system: str, log: logging.Logger
) -> tuple[Any, float]:
    """Два шанса получить валидный JSON; второй — с текстом ошибки. Как в
    `stages/llm_json.py`, но без StageContext: у источника нет ни манифеста, ни
    бюджета — стоимость возвращается вызывающему."""
    last_error = ""
    cost = 0.0
    for attempt in range(2):
        suffix = f"\n\nYour previous answer was not valid JSON ({last_error})." if attempt else ""
        result = llm.complete(prompt + suffix, system=system, json_mode=True)
        cost += result.cost_usd
        try:
            return json.loads(_strip_fences(result.text)), cost
        except json.JSONDecodeError as exc:
            last_error = str(exc)[:100]
            log.warning("невалидный JSON от %s (попытка %d): %s", llm.name, attempt + 1, exc)
    raise ValueError(f"LLM {llm.name} так и не вернул валидный JSON: {last_error}")


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        t = (t[first_nl + 1 :] if first_nl != -1 else "").rstrip()
        t = t[:-3] if t.endswith("```") else t
    return t.strip()


@dataclass
class HighlightsReport:
    path: Path
    highlights: Highlights
    skipped: bool = False  # артефакт был свежим, модель не звали
    seconds: float = 0.0
    cost_usd: float = 0.0


def select_highlights(
    sdir: Path,
    *,
    info: SourceInfo,
    transcript: Transcript,
    llm: LlmProvider,
    rules: str = "",
    blueprint: str = "",
    platforms: list[str] | None = None,
    target_duration_sec: int = 60,
    language: str = "ru",
    count: int = DEFAULT_COUNT,
    force: bool = False,
    log: logging.Logger | None = None,
    headline: bool = False,
    tighten: bool = False,
) -> HighlightsReport:
    """Транскрипт → кандидаты → критик → `highlights.json`. Идемпотентно.

    Два вызова модели, как у сценариста черновик и критик. Первый ищет широко
    (`OVERSAMPLE` × запрошенного: один заход давал «первый попавшийся список» —
    три прогона на одном подкасте дали три разных набора). Второй читает
    кандидатов ПОЛНЫМ ТЕКСТОМ и закрывает слабые с причиной, а выжившим пишет
    заголовок, описание и хэштеги. Закрытые не исчезают: они в `rejected`
    вместе с причиной, и человек вправе не согласиться.

    Блокирующие причины дают один повтор с их текстом. После повтора негодные
    кандидаты остаются в `warnings`, а годные — в файле: решает человек.

    Транскрипт длиннее `CHUNK_MAX_CHARS` идёт кусками (`chunk_plan`): широкий
    поиск — по одному вызову на кусок со своей квотой и своими пиками карты,
    повтор по блоку — тоже свой; кандидаты всех кусков сливаются одной
    доводкой. Критик читает всех разом, а не влезли — пачками
    (`critique_batches`): вердикт у него на момент, а не на список.

    `headline` и `tighten` — настройки канала (`video.headline`, `video.tighten`):
    критик ещё пишет подпись над кадром и называет лишние реплики по номерам."""
    # промпт читает константы отсюда, поэтому импорт здесь, а не наверху:
    # наверху он был бы кольцевым
    from videoforge.clips import highlight_prompts as p

    log = log or logging.getLogger("videoforge.highlights")
    existing = load_highlights(sdir)
    if existing is not None and not force and is_fresh(sdir, existing, llm.name):
        return HighlightsReport(highlights_path(sdir), existing, skipped=True)
    if (
        existing is not None
        and not force
        and llm.name.startswith("mock")
        and existing.llm != llm.name
    ):
        # Заглушка «свежее» настоящего разбора только потому, что сменилась
        # модель, — и молча кладёт мок-моменты поверх часа работы живой. Выбор
        # моментов недетерминирован (три прогона дают три набора), поэтому
        # затёртое не восстанавливается повтором.
        raise RuntimeError(
            f"{highlights_path(sdir).name} собран моделью {existing.llm!r}: "
            f"заглушка его не заменяет. Нужно правда переписать — --force"
        )

    chunks = chunk_plan(transcript.sentences, info.chapters, max_chars=CHUNK_MAX_CHARS)
    total_chars = sum(c.chars for c in chunks)
    if len(chunks) > 1:
        log.info(
            "выбор моментов: транскрипт %d тыс. знаков — режу на %d кусков по ≤%d тыс.",
            total_chars // 1000,
            len(chunks),
            CHUNK_MAX_CHARS // 1000,
        )
    seconds = cost = 0.0
    calls = 0
    answers: list[list[Candidate]] = []  # кандидаты по кускам, в порядке ответа модели
    fixes: list[str] = []
    problems: list[str] = []
    for chunk in chunks:
        single = len(chunks) == 1
        tag = "выбор моментов" if single else f"кусок {chunk.index}/{len(chunks)}"
        window = transcript.sentences[chunk.first : chunk.last + 1]
        heat = info.heatmap if single else heat_in_window(info.heatmap, chunk)
        quota = chunk_quota(chunk, total_chars=total_chars, count=count)
        rejected: list[str] | None = None
        parsed = Resolved()
        for attempt in range(1, LLM_ATTEMPTS + 1):
            prompt = p.highlights_prompt(
                info=info,
                sentences=window,
                peaks=top_peaks(heat, PEAKS_IN_PROMPT),
                rules=rules,
                blueprint=blueprint,
                platforms=platforms or [],
                target_duration_sec=target_duration_sec,
                language=language,
                count=quota,
                part=None if single else (chunk, len(chunks)),
                rejected=rejected,
            )
            log.info(
                "%s: вызов модели %d/%d, предложений %d, промпт %.0f КБ",
                tag,
                attempt,
                LLM_ATTEMPTS,
                len(window),
                len(prompt.encode("utf-8")) / 1024,
            )
            started = time.monotonic()
            raw, call_cost = _ask_json(llm, prompt, system=p.SYSTEM, log=log)
            seconds += time.monotonic() - started
            cost += call_cost
            calls += 1
            # привязка — по ВСЕМУ транскрипту, а не по окну куска: граница
            # момента у края окна иначе упёрлась бы в край, а не в предложение
            parsed = parse_candidates(
                raw,
                sentences=transcript.sentences,
                chapters=info.chapters,
                heatmap=info.heatmap,
            )
            if not parsed.problems:
                break
            log.warning("%s: отклонено — %s", tag, "; ".join(parsed.problems))
            rejected = parsed.problems
        answers.append(parsed.candidates)
        prefix = "" if single else f"{tag}: "
        fixes += [prefix + f for f in parsed.fixes]
        problems += [prefix + w for w in parsed.problems]
        if not single:
            log.info(
                "%s (%s–%s): %d кандидатов, вызовов %d, $%.3f",
                tag,
                format_clock(chunk.own_start_sec),
                format_clock(chunk.end_sec),
                len(parsed.candidates),
                calls,
                cost,
            )
    kept = finish_candidates(interleave(answers), fixes, limit=count * OVERSAMPLE)

    dropped: list[Candidate] = []
    if kept:
        batches = critique_batches(kept, max_chars=CHUNK_MAX_CHARS)
        verdicts: list[Any] = []
        for k, batch in enumerate(batches, start=1):
            label = f" (пачка {k}/{len(batches)})" if len(batches) > 1 else ""
            log.info("критик%s: читаю %d кандидатов целиком…", label, len(batch))
            started = time.monotonic()
            raw, call_cost = _ask_json(
                llm,
                p.critique_prompt(
                    batch,
                    source_title=info.title,
                    language=language,
                    title_max=TITLE_MAX_CHARS,
                    rules=rules,
                    headline=headline,
                    tighten=tighten,
                    sentences=transcript.sentences if tighten else None,
                ),
                system=p.SYSTEM_CRITIC,
                log=log,
            )
            seconds += time.monotonic() - started
            cost += call_cost
            calls += 1
            items = raw.get("verdicts") if isinstance(raw, dict) else raw
            if isinstance(items, list):
                verdicts += items
            else:
                log.warning(
                    "критик%s не вернул список verdicts — кандидаты остаются без вердикта", label
                )
        kept, dropped = apply_verdicts(
            kept,
            {"verdicts": verdicts},
            limit=count,
            log=log,
            sentences=transcript.sentences if tighten else None,
            fixes=fixes,
        )

    data = Highlights(
        source_id=info.id,
        llm=llm.name,
        generated_at=datetime.now(UTC),
        requested=count,
        calls=calls,
        chunks=len(chunks),
        candidates=kept,
        rejected=dropped,
        fixes=fixes,
        warnings=problems,
    )
    save_highlights(sdir, data)
    return HighlightsReport(highlights_path(sdir), data, seconds=seconds, cost_usd=cost)


def apply_verdicts(
    candidates: list[Candidate],
    raw: Any,
    *,
    limit: int = DEFAULT_COUNT,
    log: logging.Logger | None = None,
    sentences: list[Sentence] | None = None,
    fixes: list[str] | None = None,
) -> tuple[list[Candidate], list[Candidate]]:
    """Вердикты критика → (оставленные, закрытые). Порядок оставшихся не трогаем:
    его задаёт измеренный heat, а не мнение модели. Кандидат, про которого критик
    промолчал, остаётся — но без упаковки, и это видно по пустому `verdict`.

    `headline` и `drop` в вердикте читаются, только если их просили (критик без
    этих флагов их и не пишет); `drop` переводится в куски по `sentences`, а
    непринятый выброс оставляет строку в `fixes`."""
    verdicts = raw.get("verdicts") if isinstance(raw, dict) else raw
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(verdicts, list):
        for item in verdicts:
            if isinstance(item, dict) and item.get("id"):
                by_id[str(item["id"])] = item
    elif log is not None:
        log.warning("критик не вернул список verdicts — оставляем всех кандидатов")

    kept: list[Candidate] = []
    dropped: list[Candidate] = []
    for cand in candidates:
        item = by_id.get(cand.id)
        if item is None:
            kept.append(cand)
            continue
        cand.verdict = "keep" if item.get("keep") else "drop"
        cand.verdict_why = str(item.get("why") or "").strip()
        if cand.verdict == "drop":
            dropped.append(cand)
            continue
        cand.publish_title = str(item.get("title") or "").strip()[:TITLE_MAX_CHARS]
        cand.description = str(item.get("description") or "").strip()
        tags = item.get("hashtags")
        if isinstance(tags, list):
            cand.hashtags = [str(t).lstrip("#").strip() for t in tags if str(t).strip()][:8]
        cand.headline = str(item.get("headline") or "").strip()[:HEADLINE_MAX_CHARS]
        drop = item.get("drop")
        if sentences is not None and isinstance(drop, list) and drop:
            numbers: set[int] = set()
            for n in drop:
                try:
                    numbers.add(int(n))
                except (TypeError, ValueError):
                    continue
            pieces, removed, note = tighten_pieces(moment_sentences(sentences, cand), numbers)
            cand.pieces, cand.dropped_text = pieces, removed
            if note and fixes is not None:
                fixes.append(f"{cand.id}: {note}")
        kept.append(cand)
    if len(kept) > limit:
        # Критик их оставил, они просто не влезли в запрошенное число. В отчёте
        # это обязано читаться иначе, чем «закрыт с причиной»: момент, который
        # прошёл и критика, и длину, — рабочий кандидат, и человек вправе взять
        # его по id, а не гадать, чем тот провинился.
        for extra in kept[limit:]:
            extra.verdict_why = (
                f"критик оставил, но по heat это {kept.index(extra) + 1}-й из "
                f"{len(kept)} — за пределами запрошенных {limit}"
            )
        dropped.extend(kept[limit:])
        kept = kept[:limit]
    # id не перенумеровываем: в списке останутся дыры (h01, h03, h07), зато
    # закрытый h05 не превратится в чужой момент с тем же именем
    return kept, dropped


# ---------------------------------------------------------------- кандидат → эпизод

SCENE_ID = "s01"


def episode_from_candidate(
    project_id: str, source_id: str, candidate: Candidate, *, episode_id: str | None = None
) -> Path:
    """Выбранный момент → эпизод из одной сцены-отрезка исходника.

    Путь источника пишется относительно ПРОЕКТА (`sources/<id>`), а не эпизода:
    из одного подкаста выходит десяток шортсов, и копировать его в каждый эпизод
    незачем — стадии `cut` этого пути хватит, чтобы найти `source.json` и
    пословный транскрипт."""
    from videoforge.core.project import create_episode
    from videoforge.core.storyboard import (
        PublishInfo,
        Scene,
        SourceSegment,
        load_storyboard,
        save_storyboard,
    )
    from videoforge.paths import SOURCES_DIR

    brief = " · ".join(x for x in (candidate.hook, candidate.verdict_why or candidate.why) if x)
    edir = create_episode(
        project_id,
        episode_id or f"{source_id}_{candidate.id}",
        title=candidate.publish_title or candidate.title,
        brief=brief,
    )
    sb = load_storyboard(edir)
    if candidate.publish_title:
        # упаковка едет с моментом: её придумал критик, который читал текст
        # целиком, и переписывать её на публикации незачем — только править
        # руками. Без критика (момент по времени; кандидат, о котором критик
        # промолчал) раскадровка остаётся БЕЗ упаковки: `publish` спрашивает
        # модель только при пустом заголовке, и один рабочий title здесь
        # означал бы эпизод без описания и хэштегов. Заголовок автора модель
        # всё равно видит — он лежит в `title` эпизода и уходит в промпт
        sb.publish = PublishInfo(
            title=candidate.publish_title,
            description=candidate.description,
            hashtags=list(candidate.hashtags),
        )
    sb.headline = candidate.headline
    # выброс реплик: каждый оставшийся кусок — своя сцена-отрезок того же
    # источника, склейка между ними жёсткая (план монтажа по умолчанию). Без
    # выброса — одна сцена на весь момент, как было
    pieces = candidate.pieces or [
        Piece(start_sec=candidate.start_sec, end_sec=candidate.end_sec, text=candidate.text)
    ]
    sb.scenes = [
        Scene(
            id=SCENE_ID if len(pieces) == 1 else f"s{n:02d}",
            order=n,
            duration_sec=round(piece.end_sec - piece.start_sec, 2),
            planned_sec=round(piece.end_sec - piece.start_sec, 2),
            narration=piece.text,
            source=SourceSegment(
                path=f"{SOURCES_DIR}/{source_id}",
                start_sec=piece.start_sec,
                end_sec=piece.end_sec,
            ),
        )
        for n, piece in enumerate(pieces, start=1)
    ]
    save_storyboard(edir, sb)
    return edir
