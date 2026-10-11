"""Нейроязык в сценарии: список маркеров проверяет python, а не критик.

Критик-LLM пишет теми же клише, которые должен ловить, и от прогона к
прогону называет то одно, то другое. Слова-маркеры и шаблоны — вещь
детерминированная: попадание называет строку и фрагмент, и находка уходит в
цикл черновик ↔ критик рядом с недобором речи (`director._short_blocks`),
чтобы модель получила её в повтор с причинами.

Правила — `docs/reference/anti-neurospeak/prompt.md`, номер правила лежит в
`Marker.rule`; README там помечает 2, 3, 7 и 9 кандидатами в детерминированные
находки. Список ведётся НА ЯЗЫКЕ ПРОЕКТА (`Storyboard.language`): английские
delve и tapestry в русском сценарии не появятся, а русские штампы собраны с
живых черновиков (`projects/doc_channel/episodes/ep001/01_script/rounds/`,
14.09.2026), а не выдуманы: маркер, которого в своих текстах нет, добавляет
только ложные срабатывания. Английский список — из README и общих наблюдений,
живого английского черновика ещё не было, и цифр по нему нет.

Главный риск — ложные срабатывания: «не X, а Y» бывает уместным. Поэтому
находка называет строку и маркер, а не переписывает её, и блокирует только по
плотности на 1000 слов (`CLICHE_BLOCK_PER_1000_WORDS`); ниже порога те же
находки идут предупреждением. Пополнять список — сюда, в `MARKERS`, и только
тем, что нашли в своём черновике или что reference называет прямо.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field

from videoforge.core.script import (
    _HEADER,
    _LAYER,
    LAYER_TEXT,
    Finding,
    is_chapter_heading,
    parse_script,
)

# Порог блока — плотность попаданий на 1000 слов закадрового текста. Замер
# 14.09.2026 по единственному живому эпизоду (doc_channel/ep001, три
# раунда): раунд 1 (491 слово) — 4 попадания, 8.1/1000; раунд 2 (680 слов,
# самый сырой — критик нашёл в нём 7 находок) — 8 попаданий, 11.8/1000; раунд 3
# (723 слова) = одобренный человеком script.md — 5 попаданий, 6.9/1000. Порог 10
# отделяет сырой раунд от одобренного текста; выборка — один эпизод, и число
# надо перемерить, когда черновиков станет десяток.
CLICHE_BLOCK_PER_1000_WORDS = 10.0
# Одно попадание — фраза, а не почерк: на шортсе в 60 слов единственное
# уместное «не X, а Y» давало бы 16/1000 и блок. Блок начинается с повтора.
CLICHE_BLOCK_MIN_HITS = 2
# Правило 3: «рубленые предложения подряд». Три предложения по четыре слова и
# короче — уже дробь; «Он не забыл. Он не испугался. Он не перепутал.» ровно
# такая (3 + 3 + 3). Два коротких подряд — обычная ударная пара, не ловим.
CHOPPY_RUN = 3
CHOPPY_MAX_WORDS = 4
# Одно начало у предложений подряд — про короткие фразы («We stored… We
# dumped… We lived…»). Длинная анафора — приём: «Он не отрицал Аллаха: он
# обращается к Нему напрямую. Он не отрицал приказ: он его понял» стоит в
# одобренном сценарии ep001 по десять слов на фразу, и ловить её нельзя
ANAPHORA_MAX_WORDS = 6

FINDING_KIND = "нейроязык"


@dataclass(frozen=True)
class Marker:
    """Один шаблон нейроязыка: регулярка, имя для человека и правило reference."""

    rule: int  # номер правила в docs/reference/anti-neurospeak/prompt.md
    name: str  # короткое имя: попадает в находку и в сообщение стадии
    pattern: re.Pattern[str]
    fix: str  # правка в формулировке критика: убрать / заменить на что


@dataclass(frozen=True)
class Hit:
    """Попадание: где, что именно и по какому правилу."""

    line: int  # строка текста, с единицы
    block: str  # id блока сценария; пусто у голого текста
    fragment: str
    rule: int
    name: str
    fix: str


def _m(rule: int, name: str, pattern: str, fix: str, flags: int = 0) -> Marker:
    return Marker(rule, name, re.compile(pattern, re.IGNORECASE | flags), fix)


# Русский список: что нашлось в черновиках ep001 (пометка «черновик»), и что
# reference называет дословно (пометка «reference»). Ничего сверх этого.
_RU: list[Marker] = [
    # правило 1, reference: пафосные вводные
    _m(
        1,
        "пафосная вводная",
        r"\b(?:в современном мире|на сегодняшний день|в условиях (?:быстро )?меняющ\w+)\b",
        "убрать вводную и начать с факта: без неё фраза значит то же",
    ),
    # правило 2, reference: «не просто…, а…» (в черновиках не встретилось, но
    # это частный случай «не X, а Y», названный прямо). «не просто так» — идиома
    _m(
        2,
        "«не просто X, а Y»",
        r"\bне просто (?!так\b)[^,.;:!?\n]{1,40}?[,—–-]+\s*а\s+[^\s,.;:!?]+",
        "сказать, что Y; «не просто X» убрать — противопоставление с пустым X изображает драму",
    ),
    # правило 2, черновик: «не по природе, а по делам», «не грех их разделил, а
    # ответ на грех» — по два-три на раунд
    _m(
        2,
        "«не X, а Y»",
        r"\bне\s+[^,.;:!?«»—–\n]{1,40},\s+а\s+[^\s,.;:!?]+",
        "сказать прямо, что Y; «не X» оставить только там, где зритель правда думает X",
    ),
    # правило 2, черновик: «Это было не помрачение. Это был выбор», «это не мелкая
    # неточность — это ошибка»
    _m(
        2,
        "«это не X, это Y»",
        r"\bэто\s+(?:был[аио]?\s+)?не\s+[^.!?;:—–\n]{1,60}[.,;:—–]+\s*это\s+(?:был[аио]?\s+)?"
        r"(?!не\b)[^\s,.;:!?]*",
        "оставить одно предложение про Y, «это не X» убрать",
    ),
    # правило 2 (псевдодрама), черновик: «И вот что страшнее всего:», «И вот в
    # чём разница», «И вот как эта пылинка звучит» — трижды на два раунда
    _m(
        2,
        "подводка «и вот что»",
        r"\bи вот (?:что|в чём|как|почему)\b",
        "убрать подводку и сказать сразу то, что за ней",
    ),
    # правило 2 (псевдодрама), черновик: «вопрос, который меняет всё»
    _m(2, "«меняет всё»", r"\bменяет всё\b", "назвать, что именно меняется"),
    # черновик: «кем он был на самом деле» — критик вырезал фразу целиком
    _m(11, "«на самом деле»", r"\bна самом деле\b", "убрать: без него фраза значит то же"),
    # правило 7, reference: «во-первых, во-вторых, в-третьих»
    _m(
        7,
        "искусственный список",
        r"\b(?:во-первых|во-вторых|в-третьих)\b",
        "перечислить без счётчика; если список нужен, пункт называется словом, а не номером",
    ),
    # правило 7, черновик: «Первое: … Второе: … Третье, и вот здесь всё» внутри
    # одной реплики. Ловится только вся тройка в одном блоке: пронумерованный
    # список по абзацу на пункт — форма ролика, а не штамп
    _m(
        7,
        "счётчик «первое, второе, третье» в одной реплике",
        r"\bпервое[:,].{1,400}?\bвторое[:,].{1,400}?\bтреть[ея][:,]",
        "убрать счётчик: три мысли подряд читаются и без «первое, второе»",
        re.DOTALL,
    ),
    # правило 8, reference: канцелярит
    _m(
        8,
        "канцелярит",
        r"\b(?:данн(?:ый|ого|ому|ым|ом|ая|ой|ую|ое)|имеет (?:важное|большое|ключевое) значение"
        r"|позволяет (?:повысить|улучшить|обеспечить|достичь))\b",
        "заменить разговорным словом: «этот», «важно», «даёт»",
    ),
    # правило 9, reference: пустые экспертные фразы («важно понимать» — та же
    # фраза с другим глаголом, названа в features/script_cliche_lint)
    _m(
        9,
        "пустая экспертная фраза",
        r"\b(?:важно (?:отметить|понимать|помнить)|следует (?:учитывать|отметить)|таким образом)\b",
        "убрать фразу и сразу сказать то, что стояло за ней",
    ),
    # правило 10, reference: гиперобобщения
    _m(
        10,
        "гиперобобщение",
        r"\b(?:в наше время|в современном бизнесе|каждый предприниматель)\b",
        "заменить обобщение конкретным человеком или случаем",
    ),
]

_EPITHET = (
    r"(?:rich|vibrant|deep|profound|powerful|crucial|vital|essential|dynamic|innovative"
    r"|seamless|robust|comprehensive|intricate|nuanced|bold|exciting|groundbreaking"
    r"|revolutionary|timeless|captivating|compelling|meticulous|unwavering)"
)

# Английский список — из README reference и известных маркеров; живого
# английского черновика не было, замера плотности по нему нет.
_EN: list[Marker] = [
    _m(
        1,
        "pompous opener",
        r"\b(?:in today'?s (?:fast-paced |ever-changing |modern |digital |busy )?world"
        r"|in this day and age|in a world where|in the ever-evolving)\b",
        "cut the opener and start with the fact",
    ),
    _m(
        2,
        "it's not X, it's Y",
        r"\bit'?s not (?:just|only|about|merely) [^.,;:!?\n]{1,40}[,;.—–-]+\s*it'?s\b",
        "say Y; drop the «not X» half",
    ),
    _m(
        2,
        "not just X, but Y",
        r"\bnot (?:just|only|merely) [^,.;:!?\n]{1,40}, but\b",
        "say Y; drop the «not just X» half",
    ),
    _m(
        2,
        "this isn't X. It's Y",
        r"\bthis (?:isn'?t|is not) [^.!?\n]{1,40}[.—–-]+\s*(?:it'?s|this is)\b",
        "keep the sentence about Y, cut «this isn't X»",
    ),
    _m(
        7,
        "artificial list",
        r"\b(?:firstly|secondly|thirdly)\b",
        "list without the counter; if the list is real, name each item instead of numbering it",
    ),
    _m(
        9,
        "empty expert phrase",
        r"\b(?:it'?s (?:important|worth|crucial) (?:to note|noting|to remember|to understand)"
        r"|in conclusion|at the end of the day|needless to say)\b",
        "cut the phrase and say what stood behind it",
    ),
    _m(
        11,
        "marker word",
        r"\b(?:delv(?:e|es|ed|ing)|tapestry|game[- ]chang(?:er|ing)|let'?s dive in"
        r"|unlock(?:s|ed|ing)? the|unleash(?:es|ed|ing)?|a testament to|the (?:\w+ )?landscape of"
        r"|navigat(?:e|ing) the|in the realm of|embark on|buckle up|look no further"
        r"|cutting-edge|seamless(?:ly)?|elevate)\b",
        "replace with the plain word for what actually happens",
    ),
    _m(
        11,
        "paired epithets",
        rf"\b{_EPITHET}\s+(?:and|yet)\s+{_EPITHET}\b",
        "keep one adjective — or none: the noun should carry the meaning",
    ),
]

MARKERS: dict[str, list[Marker]] = {"ru": _RU, "en": _EN}

_CHOPPY = Marker(
    3,
    "рубленые предложения подряд",
    re.compile(r"(?!)"),  # структурное правило: ищется по предложениям, не регуляркой
    "слить в одно предложение с союзом или двоеточием: три обрубка подряд читаются как речь робота",
)
_ANAPHORA = Marker(
    3,
    "одно начало у предложений подряд",
    re.compile(r"(?!)"),
    "переставить начала: три предложения с одного слова — почерк модели («We stored… We dumped…»)",
)

_SENTENCE = re.compile(r"\S[^.!?…]*(?:[.!?…]+|$)")
_LEAD_PUNCT = "«»\"'„“”()[]—–-:;,"


def find_cliches(text: str, language: str) -> list[Hit]:
    """Попадания в голом тексте: строка, фрагмент, правило. Блок пустой."""
    return _scan([(no, line) for no, line in enumerate(text.splitlines(), 1)], language, "")


def lint_script(markdown: str, language: str) -> ClicheReport:
    """Проверка script.md: только слой «Текст» — то, что зритель услышит.
    Кадр и эмоция — служебные слои, их читает планнер, а не зритель."""
    hits: list[Hit] = []
    chunk: list[tuple[int, str]] = []
    block = ""
    layer: str | None = None

    def flush() -> None:
        nonlocal chunk
        if chunk:
            hits.extend(_scan(chunk, language, block))
        chunk = []

    for no, raw in enumerate(markdown.splitlines(), 1):
        line = raw.strip()
        if is_chapter_heading(line):  # заголовок главы — не реплика
            flush()
            layer = None
        elif header := _HEADER.match(line):
            flush()
            block, layer = header["id"], None
        elif not block:
            continue
        elif found := _LAYER.match(line):
            flush()
            layer = found[1]
            if layer == LAYER_TEXT and found["body"].strip():
                chunk.append((no, found["body"].strip()))
        elif layer == LAYER_TEXT and line:
            chunk.append((no, line))
    flush()
    words = sum(b.words for b in parse_script(markdown))
    return ClicheReport(language=_lang(language), words=words, hits=hits)


def cliche_findings(markdown: str, language: str) -> list[Finding]:
    """Находки для критика: блокирующие по порогу плотности, иначе предупреждение."""
    return lint_script(markdown, language).findings()


@dataclass(frozen=True)
class ClicheReport:
    language: str
    words: int  # слов закадрового текста
    hits: list[Hit] = field(default_factory=list)

    @property
    def per_1000(self) -> float:
        return round(len(self.hits) * 1000 / self.words, 1) if self.words else 0.0

    @property
    def blocking(self) -> bool:
        return (
            len(self.hits) >= CLICHE_BLOCK_MIN_HITS and self.per_1000 >= CLICHE_BLOCK_PER_1000_WORDS
        )

    @property
    def rules(self) -> list[int]:
        return sorted({h.rule for h in self.hits})

    def findings(self) -> list[Finding]:
        blocking = self.blocking
        return [
            Finding(
                kind=FINDING_KIND,
                where=f"{hit.block}, строка {hit.line}" if hit.block else f"строка {hit.line}",
                why_viewer_leaves=(
                    f"«{hit.fragment}» — {hit.name} (anti-neurospeak, правило {hit.rule}): "
                    "шаблон слышен как текст нейросети, и доверие к голосу падает"
                ),
                fix=hit.fix,
                blocking=blocking,
            )
            for hit in self.hits
        ]

    def summary(self) -> str:
        """Строка в сообщение стадии: без неё проверку в прогоне не отличить от
        её отсутствия."""
        n = len(self.hits)
        text = f"нейроязык: {n} {_plural(n, 'попадание', 'попадания', 'попаданий')}"
        if n:
            text += f" ({self.per_1000:g}/1000 слов), правила: {', '.join(map(str, self.rules))}"
        if self.language not in MARKERS:
            text += f" — список маркеров для «{self.language}» не заведён, проверен один ритм фраз"
        return text


# ------------------------------------------------------------------ внутри


def _lang(language: str) -> str:
    return language.split("-")[0].split("_")[0].lower()


def _scan(lines: list[tuple[int, str]], language: str, block: str) -> list[Hit]:
    """Маркеры и правило 3 по куску текста. Кусок — одна реплика целиком, с
    номерами строк: «это не X. Это Y» переживает разрыв абзаца, а рубленые
    фразы считаются по предложениям, а не по строкам."""
    if not lines:
        return []
    text = "\n".join(t for _, t in lines)
    starts: list[int] = []
    numbers: list[int] = []
    offset = 0
    for no, t in lines:
        starts.append(offset)
        numbers.append(no)
        offset += len(t) + 1

    def line_at(pos: int) -> int:
        return numbers[max(0, bisect_right(starts, pos) - 1)]

    spans: list[tuple[int, int, Marker, str]] = []
    for marker in MARKERS.get(_lang(language), []):
        for m in marker.pattern.finditer(text):
            spans.append((m.start(), m.end(), marker, m.group(0)))
    # «не просто X, а Y» лежит внутри «не X, а Y»: одно место — одно попадание,
    # побеждает более длинный (более частный) шаблон
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    found: list[tuple[int, Hit]] = []
    last_end = -1
    for start, end, marker, fragment in spans:
        if start < last_end:
            continue
        found.append((start, _hit(line_at(start), block, fragment, marker)))
        last_end = end
    found.extend(_rhythm(text, line_at, block))
    found.sort(key=lambda f: f[0])
    return [hit for _, hit in found]


def _hit(line: int, block: str, fragment: str, marker: Marker) -> Hit:
    short = re.sub(r"\s+", " ", fragment).strip()
    if len(short) > 70:
        short = short[:67].rstrip() + "…"
    return Hit(line, block, short, marker.rule, marker.name, marker.fix)


def _rhythm(text: str, line_at: Callable[[int], int], block: str) -> list[tuple[int, Hit]]:
    """Правило 3 — по предложениям, язык не важен: рубленая дробь из коротких
    предложений и три подряд с одного слова."""
    sentences = [(m.start(), m.group(0).strip()) for m in _SENTENCE.finditer(text)]
    sentences = [(pos, s) for pos, s in sentences if s]
    found: list[tuple[int, Hit]] = []
    covered: set[int] = set()

    i = 0
    while i < len(sentences):
        j = i
        while j < len(sentences) and len(sentences[j][1].split()) <= CHOPPY_MAX_WORDS:
            j += 1
        if j - i >= CHOPPY_RUN:
            pos = sentences[i][0]
            fragment = " ".join(s for _, s in sentences[i:j])
            found.append((pos, _hit(line_at(pos), block, fragment, _CHOPPY)))
            covered.update(range(i, j))
        i = max(j, i + 1)

    i = 0
    while i < len(sentences):
        first = _first_word(sentences[i][1])
        j = i
        while (
            j < len(sentences)
            and first
            and _first_word(sentences[j][1]) == first
            and len(sentences[j][1].split()) <= ANAPHORA_MAX_WORDS
        ):
            j += 1
        # рубленый ряд на том же месте уже назван — второй находкой не дублируем
        if j - i >= CHOPPY_RUN and covered.isdisjoint(range(i, j)):
            pos = sentences[i][0]
            fragment = " ".join(s for _, s in sentences[i:j])
            found.append((pos, _hit(line_at(pos), block, fragment, _ANAPHORA)))
        i = max(j, i + 1)
    return found


def _first_word(sentence: str) -> str:
    word = sentence.lstrip(_LEAD_PUNCT).split(maxsplit=1)
    return word[0].strip(_LEAD_PUNCT + ".!?…").lower() if word else ""


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
