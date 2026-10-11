"""Артефакты сценариста: подачи → цепочка любопытства → [главы] → критик → удержание.

Отдельно от `storyboard.py` намеренно: раскадровка — контракт между стадиями,
а это след работы одной стадии. В `core`, потому что пишет их `director`, а
читает `planner` (retention.json задаёт ритм) — обе стадии зависят от ядра, но
не друг от друга.

Каждый артефакт помнит, какой моделью сделан (`llm`): сменился провайдер —
артефакт устарел, ровно как актив сцены устаревает по `Scene.provenance`.
Файлы лежат в `01_script/` эпизода и правятся руками: поправил `chosen` в
angles.json — всё, что ниже по цепочке, пересоберётся само (свежесть по mtime).
Главы длинного ролика — исключение: их свежесть по отпечаткам в разборе главы
(`ChapterCritique`), потому что план всех глав лежит одним файлом.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field

ANGLES_FILE = "angles.json"
STORY_FILE = "story.json"
SCRIPT_FILE = "script.md"
CRITIQUE_FILE = "critique.json"
RETENTION_FILE = "retention.json"
ROUNDS_DIR = "rounds"
# длинный ролик пишется по главам: план глав, текст каждой главы отдельным
# файлом (script.md собирается из них), проход критика по стыкам
CHAPTERS_FILE = "chapters.json"
CHAPTERS_DIR = "chapters"
SEAMS_FILE = "seams.json"

# Три слоя блока сценария. Одно место на промпт и на разбор — иначе сценарий
# пишется по одной разметке, а читается по другой.
LAYER_TEXT = "Текст"
LAYER_FRAME = "Кадр"
LAYER_EMOTION = "Эмоция"

# Темп речи с паузами — слов на секунду хронометража. Не «спокойное чтение вслух»
# из головы, а замер: текст ep001 (78 реплик, 669 слов) через стадию
# audio со срезом вшитой тишины и паузой по классу (16.09.2026, edge-tts, "+0%",
# без полов планнера): en-AU-WilliamMultilingualNeural (william, голос doc_channel) —
# 336 с, 1.99 слова/с; ru-RU-DmitryNeural (ru_male) — 314 с, 2.13. С полами
# старой раскадровки (planned_sec, добивка ≤ 0.3 с) — 1.91 и 2.03.
# Прежние 1.9 (1182 слова за 610 с, ru_male, 08.09) мерились вместе с паузой
# 1.5 с на стыке сцен; после её исправления сценарий недобирал бы хронометраж.
# Для другого языка или голоса величину надо перемерить тем же способом
WORDS_PER_SEC = 2.0
# какую долю блока должна занимать речь: остальное — пауза и длинный кадр.
# Ниже этого ролик провисает тишиной, и на длинном формате это заметно
SPEECH_FILL = 0.85


def words_for(seconds: float) -> int:
    """Сколько слов должно звучать в блоке, чтобы он не провисал тишиной.
    Одна формула на блок сценария, звено цепочки и главу."""
    return max(4, round(seconds * SPEECH_FILL * WORDS_PER_SEC))


# что критик обязан искать: причины закрыть ролик, а не поводы похвалить
FINDING_KINDS = (
    "скучное начало",
    "предсказуемость",
    "ровный ритм",
    "слабый payoff",
    "сцена ни за чем",
)
# что ищет критик стыков: то, чего не видно при чтении по одной главе
SEAM_KINDS = (
    "повтор между главами",
    "обещание не закрыто",
    "сквозная линия оборвана",
    "стык без крючка",
)
# что считается сменой в разметке удержания
CHANGE_KINDS = ("информация", "вопрос", "эмоция", "масштаб", "локация")


class Artifact(BaseModel):
    """Общее у всех артефактов: чем и когда сделано."""

    llm: str = ""
    generated_at: datetime | None = None


class Angle(BaseModel):
    """Одна подача темы. Одна тема — не один ролик."""

    id: str
    title: str
    premise: str  # о чём ролик именно в этой подаче, одно предложение
    hook: str  # первая фраза
    curiosity: int = 0  # 1-5: насколько сильно хочется узнать ответ
    novelty: int = 0  # 1-5: насколько это не сказано уже всеми
    visual: int = 0  # 1-5: есть ли что показывать, а не только рассказывать
    payoff: int = 0  # 1-5: чем ролик расплачивается за обещание
    why: str = ""  # обоснование оценок со ссылкой на правило playbook

    @property
    def total(self) -> int:
        return self.curiosity + self.novelty + self.visual + self.payoff


class Angles(Artifact):
    topic: str = ""
    options: list[Angle] = Field(default_factory=list)
    chosen: str = ""  # id подачи; правится руками — правка выигрывает
    rationale: str = ""


class Gap(BaseModel):
    """Звено цепочки любопытства. Не сцена: сцены собирает планнер."""

    id: str
    knows: str  # что зритель знает к этому моменту
    wants: str  # чего хочет узнать — открытый разрыв
    gets: str  # что получает здесь
    opens: str = ""  # какой новый вопрос возник; у последнего звена пусто
    planned_sec: float = 0.0  # сколько это звено должно занимать
    emotion: str = ""  # эмоциональное намерение
    escalation: str = ""  # почему ставка выше, чем в предыдущем звене


class Story(Artifact):
    angle_id: str = ""
    gaps: list[Gap] = Field(default_factory=list)
    ending: str = ""  # чем закрывается главный разрыв — внутри ролика
    target_duration_sec: float = 0.0


class Finding(BaseModel):
    """Находка критика: где зритель уйдёт и что с этим сделать."""

    kind: str  # из FINDING_KINDS
    where: str  # блок сценария или таймкод
    why_viewer_leaves: str
    fix: str  # конкретная правка, а не «усилить»
    blocking: bool = False  # без этой правки сценарий не берём в работу


class Critique(Artifact):
    round: int = 1
    findings: list[Finding] = Field(default_factory=list)
    verdict: str = ""

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]


class Chapter(BaseModel):
    """Глава длинного ролика: подряд идущие звенья цепочки, у которых свой
    вопрос и свой конец, тянущий в следующую главу.

    `planned_sec` и `words` считает python из звеньев и цели эпизода — при
    каждом чтении заново, поэтому правка этих чисел руками ничего не даёт:
    длину главы меняет состав `links`. (Секунды звеньев живут в story.json, но
    её правка делает устаревшим весь план глав — он строится заново.)"""

    id: str  # ch01 — имя файла главы в chapters/
    title: str
    about: str = ""  # о чём глава и что зритель из неё выносит
    links: list[str] = Field(default_factory=list)  # id звеньев story.json по порядку
    ends_with: str = ""  # чем кончается: крючок в следующую; у последней — чем закрыто обещание
    planned_sec: float = 0.0  # считает python
    words: int = 0  # норма речи главы, считает python


class ChapterPlan(Artifact):
    promise: str = ""  # что обещает начало ролика — последняя глава обязана это отдать
    throughline: str = ""  # что идёт через все главы: вопрос, арка героя; пусто — ничего
    chapters: list[Chapter] = Field(default_factory=list)
    target_duration_sec: float = 0.0
    # что python не устроило в раскладке после повтора: глава длиннее потолка,
    # число глав вне границ. Не ошибка — предупреждение человеку на воротах
    warnings: list[str] = Field(default_factory=list)


class ChapterCritique(Critique):
    """Разбор одной главы плюс то, по чему видно, что глава актуальна.

    Отпечатки, а не mtime: план всех глав — один файл, и по его mtime не
    отличить «поменялась запись этой главы» от «поменялась соседняя». Текст —
    тоже отпечатком: правка главы руками и есть расхождение `text_sha` с файлом."""

    chapter: str = ""
    plan_sha: str = ""  # запись главы в chapters.json + её звенья — из чего написана
    text_sha: str = ""  # текст главы, который разбирал критик
    manual: bool = False  # текст правил руками: стадия его не переписывает, только судит


class Beat(BaseModel):
    """Точка на таймлайне, где что-то меняется. Только отметка смены — судить,
    нужен ли кусок, здесь нечем: отметка стоит там, где что-то происходит, и
    потому «выбрасываемой» не бывает по построению. Этим занят BlockCheck."""

    t_sec: float
    changes: list[str] = Field(default_factory=list)  # из CHANGE_KINDS
    note: str = ""


class BlockCheck(BaseModel):
    """«Уберём этот блок — станет хуже?» Судится по блокам сценария, а не по
    отметкам смен: выбрасывают куски текста, а не точки на таймлайне."""

    id: str  # id блока сценария
    removable: bool = False
    if_removed: str = ""  # что именно потеряется — или почему ничего


class DeadZone(BaseModel):
    """Отрезок, на котором для зрителя ничего не меняется. Считает python по
    отметкам смен: где смен нет, там и мёртвая зона, спрашивать модель не о чем."""

    start_sec: float
    end_sec: float

    @property
    def seconds(self) -> float:
        return round(self.end_sec - self.start_sec, 1)


class Retention(Artifact):
    beats: list[Beat] = Field(default_factory=list)
    blocks: list[BlockCheck] = Field(default_factory=list)
    dead_zones: list[DeadZone] = Field(default_factory=list)  # считает python
    score: int = 0  # 0-100, выставляет LLM по измеренным числам
    score_reasons: dict[str, str] = Field(default_factory=dict)  # составляющая → обоснование
    weakest: str = ""  # самое слабое место хронометража

    def marks(self) -> list[float]:
        return sorted(b.t_sec for b in self.beats)


_HEADER = re.compile(r"^##\s+(?P<id>\S+)\s*·\s*(?P<title>.+?)\s*·\s*(?P<sec>[\d.]+)\s*s?\s*$")
# заголовок главы в собранном script.md — та же форма уровнем выше
_CHAPTER = re.compile(r"^#\s+(?P<id>\S+)\s*·\s*(?P<title>.+?)\s*·\s*(?P<sec>[\d.]+)\s*s?\s*$")
_LAYER = re.compile(rf"^\*\*({LAYER_TEXT}|{LAYER_FRAME}|{LAYER_EMOTION}):?\*\*:?\s*(?P<body>.*)$")


def chapter_heading(chapter: Chapter) -> str:
    return f"# {chapter.id} · {chapter.title} · {chapter.planned_sec:g}s"


def is_chapter_heading(line: str) -> bool:
    return bool(_CHAPTER.match(line.strip()))


def assemble_script(chapters: list[Chapter], texts: dict[str, str]) -> str:
    """script.md длинного ролика: главы подряд, у каждой заголовок. Формат
    блоков внутри — тот же, что у сценария одним заходом: планнер и разметка
    удержания читают его, не зная о главах."""
    parts = [f"{chapter_heading(ch)}\n\n{texts[ch.id].strip()}\n" for ch in chapters]
    return "\n".join(parts)


def split_chapters(markdown: str) -> dict[str, str]:
    """Собранный script.md → текст каждой главы по её id (без заголовка).
    Обратное к `assemble_script`: правку script.md руками раскладываем по главам."""
    parts: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in markdown.splitlines():
        if heading := _CHAPTER.match(raw.strip()):
            current = parts.setdefault(heading["id"], [])
        elif current is not None:
            current.append(raw)
    return {cid: "\n".join(lines).strip() + "\n" for cid, lines in parts.items()}


class ScriptBlock(BaseModel):
    """Блок готового сценария — как он реально записан в script.md."""

    id: str
    title: str = ""
    planned_sec: float = 0.0
    text: str = ""  # закадровый текст
    # тот же текст по абзацам (пустая строка в script.md): новый абзац внутри
    # блока — поворот или новый пункт, и пауза перед ним длиннее
    # (`core/pauses.py`). Один абзац — список из одного текста
    paragraphs: list[str] = Field(default_factory=list)
    chapter: str = ""  # id главы, если сценарий собран по главам

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def speech_sec(self) -> float:
        return round(self.words / WORDS_PER_SEC, 1)


def parse_script(markdown: str) -> list[ScriptBlock]:
    """script.md → блоки. Критик может слить или выбросить блок, поэтому что
    реально написано, известно только отсюда, а не из story.json.

    Закадровый текст тянется до следующего слоя или до следующего блока: на
    длинной реплике модель разбивает его на абзацы, и брать одну первую строку —
    значит недосчитать речь в разы. Заголовок главы текст тоже обрывает: иначе
    он уезжает в реплику последнего блока предыдущей главы."""
    blocks: list[ScriptBlock] = []
    parts: list[str] = []
    layer: str | None = None
    chapter = ""

    def flush() -> None:
        if blocks and parts:
            blocks[-1].text = " ".join(p for p in parts if p).strip()
            paragraphs: list[list[str]] = [[]]
            for p in parts:
                if p:
                    paragraphs[-1].append(p)
                elif paragraphs[-1]:
                    paragraphs.append([])
            blocks[-1].paragraphs = [" ".join(p) for p in paragraphs if p]

    for raw in markdown.splitlines():
        line = raw.strip()
        if heading := _CHAPTER.match(line):
            flush()
            parts, layer, chapter = [], None, heading["id"]
        elif header := _HEADER.match(line):
            flush()
            parts, layer = [], None
            blocks.append(
                ScriptBlock(
                    id=header["id"],
                    title=header["title"],
                    planned_sec=float(header["sec"]),
                    chapter=chapter,
                )
            )
        elif not blocks:
            continue
        elif found := _LAYER.match(line):
            flush()
            layer = found[1]
            parts = [found["body"].strip()] if layer == LAYER_TEXT else []
        elif layer == LAYER_TEXT and line:
            parts.append(line)
        elif layer == LAYER_TEXT and parts and parts[-1]:
            parts.append("")  # пустая строка внутри текста — граница абзаца
    flush()
    return blocks


def dead_zones(beats: list[Beat], *, limit_sec: float, total_sec: float) -> list[DeadZone]:
    """Промежутки между сменами длиннее `limit_sec` — и хвост после последней смены.

    Хвост считается отдельно: сценарий, размеченный до 517-й секунды при
    хронометраже 600, — это не «конец», а необъявленная мёртвая зона в минуту."""
    marks = sorted(b.t_sec for b in beats)
    if not marks:
        return [DeadZone(start_sec=0.0, end_sec=total_sec)] if total_sec > limit_sec else []
    bounds = [0.0, *marks, total_sec] if total_sec > marks[-1] else [0.0, *marks]
    return [
        DeadZone(start_sec=a, end_sec=b)
        for a, b in zip(bounds, bounds[1:], strict=False)
        if b - a > limit_sec
    ]
