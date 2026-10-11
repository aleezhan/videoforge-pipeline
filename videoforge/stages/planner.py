"""Планнер: сценарий → раскадровка (scenes в storyboard.json).

Единственная стадия, которая превращает текст в структуру. На вход берёт
`01_script/script.md` (его пишет режиссёр или человек), а если сценария нет —
`storyboard.brief`. На выход — сцены с озвучкой, промптом кадра и движением;
дальше по ним работают audio → images → video → assemble. `duration_sec` планнера —
ориентир: стадия озвучки перемеряет его по готовому голосу.

Три источника правил, от общего к частному, — так же они и передаются модели:
1. `ctx.blueprint` — что сработало у разобранных каналов (research/): ритм
   монтажа, правила хука, приёмы подачи. Чужой опыт, не обязателен.
2. `ctx.rules` — rules.md проекта: чего этот канал не делает никогда.
3. `ctx.project` — числа: длительность, длина клипа, язык, формат кадра.

Каст сцены проверяется здесь, а не в стадии картинок: опечатка в id, дошедшая
до генерации, стоит денег. Неизвестный id планнер вычищает и пишет в лог.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter
from typing import Any, NamedTuple, cast

from pydantic import ValidationError

from videoforge.core.io import read_model
from videoforge.core.project import SCRIPT_DIR
from videoforge.core.resources import VISUAL_KINDS, Style, list_resources
from videoforge.core.script import (
    RETENTION_FILE,
    SCRIPT_FILE,
    WORDS_PER_SEC,
    Retention,
    parse_script,
)
from videoforge.core.stage import Stage, StageContext, StageResult
from videoforge.core.storyboard import (
    OVERLAY_MAX_CHARS,
    OverlayKind,
    Scene,
    SceneOverlay,
    SceneTempo,
    shots,
)
from videoforge.providers.base import ProviderError
from videoforge.stages.audio import MIN_SCENE_SEC
from videoforge.stages.llm_json import strip_fences
from videoforge.stages.readability import (
    STOP_WORDS,
    ReadabilityResult,
    check_readability,
    format_block,
)
from videoforge.stages.readability import note as readability_note
from videoforge.stages.style_conflict import (
    Conflict,
    describe,
    find_conflicts,
    judge_conflicts,
    retry_reason,
)
from videoforge.stages.variation import blocking, check_variation

# Потолка числа сцен нет (16.09.2026). Был MAX_SCENES = 200 «от ответа, который
# развалит бюджет»: число взято без замера, а 24 минуты при сценах по 5–6 с —
# это 250–290 сцен. Деньги держит бюджет эпизода — прогноз перед стадией и
# `ctx.afford` перед каждым платным вызовом; раздутый ответ держит сумма
# длительностей против цели (`DURATION_TOLERANCE`), где сцена считается не
# короче `MIN_SCENE_SEC`: тысяча сцен по полсекунды не проходит её по построению

SYSTEM = "You are a storyboard planner for a short-form video production pipeline."

_PROMPT = """Turn this script into a storyboard: a flat list of scenes.

Every scene is ONE picture the viewer looks at while its narration is spoken:
one generated still image (the pipeline animates it or moves a camera over it)
with one piece of narration over it, usually one or two sentences. There is no
editing inside a scene.

EPISODE
{episode}

SCRIPT
{script}

WHERE SOMETHING CHANGES FOR THE VIEWER (measured on this very script by the
writer stage: each mark is a point where information, a question, an emotion,
the scale or the place changes)
{rhythm}

Every mark is a scene boundary: something changes for the viewer there, so the
picture changes too. Between two marks the picture changes where the thing the
narration talks about changes, and a stretch that keeps talking about one thing
is one picture held under all of its sentences. Cutting every stretch into
equal halves, whatever the words say, is how a storyboard ends up with one flat
pulse from start to finish.

PROJECT RULES (this channel never breaks them)
{rules}

VISUAL STYLE (fixed for the project; its prefix is added to every frame prompt
automatically, so never name another rendering technique — no "photorealistic",
no "3D render" — and never put in the picture what the style forbids: words on
signs, coins or pages go to `overlay`, not into the frame)
{style}

WHAT WORKS IN THIS NICHE (measured on analysed channels; advice, not law)
{blueprint}

CAST YOU MAY USE (exact ids, nothing else exists)
{cast}

Return ONLY a valid JSON array of scenes, in order, no fences, no commentary:
[{{"narration": "spoken text, language {language}",
   "visual_prompt": "what is in frame, English, one static image",
   "motion_prompt": "what moves and how, English",
   "tempo": "action | calm",
   "cast": ["characters/id", "props/id"],
   "overlay": {{"kind": "caption | source | label", "text": "on-screen text, {language}",
               "emphasis": ["key word from text"]}},
   "duration_sec": <seconds this scene needs>}}]

Rules:
- A picture is a unit of meaning: the viewer takes it in while one or two
  sentences are spoken, and its length comes from those words, not from a
  quota. The rhythm of this project, worked out for you — an orientation,
  not a norm: {picture}
  Neighbouring short sentences about the same thing share one picture; a new
  picture starts where the thing the narration talks about changes. A short
  sentence that turns the story (a question, a verdict) can hold a picture of
  its own. The eye needs about {min_scene} s to take in a frame, so that is
  where the shortest scenes sit.
- A sentence of more than {long_sentence_words} words is two pictures: the
  first carries its opening ~{cut_words} words (about {cut_sec} s of voice) and
  ends at a comma or between two words, the second carries the rest, and the
  voice runs on across the cut. {long_sentences}
- Narration must fit its own duration when read aloud: the voice used by this
  project speaks {words_per_sec} words per second (measured, not guessed), so
  a scene holds about {words_per_sec} words for every second of it (a
  {scene_duration} s scene — about {words_per_scene} words). Count the words
  of every scene; a scene whose narration runs long ends on a frozen frame.
- THE TIME BUDGET, already worked out for you — follow it, do not recompute it:
  {budget}
  The total is the length of the episode, not a suggestion: a scene's duration
  becomes the floor for its voice-over, so seconds invented here turn into
  seconds of silence in the finished video. An answer that overshoots the total
  is rejected and asked again.
- duration_sec is NOT a constant. A storyboard where every scene is near the
  average has no peaks at all, and a flat pulse is what makes viewers leave.
  Two things must be countable in your answer: the OPENING is cut denser than
  the middle (short scenes while the hook lands, longer ones once the video
  starts explaining), and the long scenes of the budget above are real — a
  long scene of {long_min}–{long_max} s is one picture held under a passage of
  3–5 sentences that stays on one thing: a scene with dialogue, the
  description of one stage or one item, a quote with its explanation, the
  closing enumeration. Asking for a long scene costs you nothing: the pipeline
  cuts such a beat into consecutive shots of the same frame by itself, and the
  voice goes on across them.
- The voice runs from the first second of the video to the last: every scene
  carries narration in proportion to its length, about {words_per_sec} words
  per second, so a {long_sec} s scene carries about {long_words} words. The
  picture changes under the voice; the voice does not wait for the picture.
  Two places are the exception and may carry no words: the very first scene
  when it is a flash of at most 1 second before the first word, and the very
  last scene when it is an end screen of at most 5 seconds.
- visual_prompt describes a SINGLE frozen moment that an image model can draw:
  no "then", no "while", no cuts. Do not describe cast members' faces, bodies
  or clothing — their reference images are attached automatically; name them
  only by their role in the frame.
- The frame shows the NOUN of the narration: the concrete thing, place or
  person the words are about, so that a viewer with the sound off understands
  in one second what the scene is about. A grave for words about burial, an
  open book for words about a verse, a market for words about trade. A frame
  that hints at something the words never say ("three grooves without
  letters", "a ribbon of pure flame") is a riddle, not an illustration — it is
  rejected. A metaphor is allowed at most once per chapter (one numbered point
  or one block of the script) and only when the narration names it itself
  (clay and fire, the scales, the seed). Light, glow, particles, smoke are
  accents for the scene that has their source (a lamp, a fire, stars), not a
  default for every frame.
- Anything the viewer must READ goes to `overlay`, never into the picture: the
  picture shows the object, the plate shows the words. Three kinds: `caption`
  — the one line of this scene's narration worth keeping (the conclusion,
  the claim; `emphasis` names the key word — a noun or a number from the
  text, never a particle or a pronoun — drawn in the highlight colour),
  `source` — the citation the claim rests on («Коран 55:15», «Бухари 3336»),
  `label` — the list counter («ПУНКТ 3») on the first scene of a point. At most
  one plate per scene, at most {overlay_chars} characters; not every scene
  needs one — a plate on every scene is noise, a plate on the scene that states
  the claim is how the video reads with the sound off. Omit the field when
  there is nothing to read.
- motion_prompt is what moves for the whole clip — never a cut or a scene
  change — and `tempo` says which of two kinds of scene this is. `action`:
  something happens in the frame; write the movement as a continuous sequence
  of 2–3 actions that fills the clip from the first second to the last ("the
  rider spurs the horse, leans into the bend, the horse clears the fence"),
  never a single event — a video model stretches one event over the whole
  clip and it plays as slow motion. `calm`: a breath, a landscape, a still
  object; one slow movement (a slow push in, a drifting pan, an object
  growing) and nothing more — the pipeline will not push the tempo on such a
  scene.
- cast lists only ids from the CAST list above, exactly as written. List the
  main character explicitly in every frame where he or she appears; an empty
  list means nobody is in the frame (landscape, object, symbol). Never invent an id.
- The first scene is the hook and must earn the next five seconds on its own.
- Write narration in {language}; visual_prompt and motion_prompt in English."""


def script_path(ctx: StageContext) -> Any:
    return ctx.project_dir / SCRIPT_DIR / SCRIPT_FILE


def style_text(style: Style | None) -> str:
    """Стиль проекта для промпта планнера: чем рисуем и чего в кадре не бывает.

    До 14.09.2026 планнер стиля не видел вовсе и узнавал о нём только из
    причин повтора — ep002 написал восемь кадров в фотореализме при стиле
    «мягкая 2D-иллюстрация», и это стоило восьми забракованных генераций.
    Проверка по словам (`style_conflict`) ловит конфликт постфактум; показать
    стиль заранее дешевле, чем переспрашивать."""
    if style is None:
        return "—"
    rows = [f"rendering: {style.prompt_prefix.strip()}" if style.prompt_prefix.strip() else ""]
    if style.negative.strip():
        rows.append(f"never in the frame: {style.negative.strip()}")
    if style.forbid:
        rows.append(f"also forbidden: {'; '.join(style.forbid)}")
    return "\n".join(r for r in rows if r) or "—"


# доля хронометража в длинных планах. Прежняя раскладка «каждая десятая сцена
# в 2.5 раза длиннее средней» давала ровно четверть; число сцен из неё убрано
LONG_SHARE = 0.25


def time_budget(*, target_sec: float, long_sec: float) -> str:
    """Раскладка секунд — посчитанная, а не оставленная модели.

    «Сумма M секунд» + «четверть в длинных планах» — арифметика, и модель её не
    решает: она либо теряет длинные планы, либо вылетает за хронометраж.
    Считаем деление здесь, как норму слов для сценариста.

    Числа сцен в раскладке нет (16.09.2026): «about N scenes» из средней длины
    сцены делало медиану нормой, и пять коротких сцен подряд модель укрупняла
    под счёт. Сколько сцен в остатке — решает содержание."""
    if target_sec <= 0:
        return "—"
    long_count = int(target_sec * LONG_SHARE // long_sec) if long_sec > 0 else 0
    if long_count == 0:
        return f"{target_sec:.0f} seconds in total, cut by content."
    rest = target_sec - long_count * long_sec
    scenes = "scene" if long_count == 1 else "scenes"
    # длинная сцена — длинный кадр ПОД РЕЧЬЮ, и слова называются числом: до
    # 16.09.2026 здесь стояло «LONG scenes», промпт звал их «выдохами», и на
    # ep001 все четыре длинные сцены вышли немыми по 15 с (замер
    # research/_probes/speech_pauses_2026-09-16)
    words = round(long_sec * WORDS_PER_SEC)
    return (
        f"{target_sec:.0f} seconds in total:\n"
        f"  · {long_count} LONG {scenes} of ~{long_sec:.0f} s = {long_count * long_sec:.0f} s, "
        f"each held under ~{words} words of narration\n"
        f"  · the remaining {rest:.0f} s cut by content — as many pictures as the words need, "
        "denser at the opening"
    )


# --- ритм картинок. Замер research/_probes/scene_cuts_2026-09-17 (17.09.2026):
# у референсного канала A картинка держит 12–18 слов — в роликах-списках одно предложение
# (медиана плана 5.4–6.6 с), в истории «7 стадий Барзаха» два (9.5 с, 43% планов
# — несколько целых предложений). У ep001 78% сцен было ровно одно предложение
# по 8 слов и 12 смен картинки в минуту против 6–10. Отдельного поля «формат»
# нет: ориентир — `video.scene_duration_sec` проекта (история ~9.5, список ~6),
# слова на картинку из него считает python, модель получает число.

# предложение длиннее стольких «картинок» слов — две картинки посреди фразы: у
# списков длинная фраза (больше ~20 слов) чаще делится, чем держится одной
LONG_SENTENCE_FACTOR = 1.6
# …но не короче стольких секунд речи: восьмисекундная фраза держится одной
# картинкой при любом ориентире
LONG_SENTENCE_MIN_SEC = 8.0
# первая картинка длинной фразы — столько секунд речи: в «7 типах» половина
# склеек посреди фразы стоит после картинки ровно на 5.0 с
MID_PHRASE_CUT_SEC = 5.0
# длинный план: только в истории, 15–24 с (13% планов, 23% времени), и всегда
# под речью 26–44 слов — диалог, описание стадии, цитата с пояснением,
# финальное перечисление; в роликах-списках самый длинный 12–14 с
LONG_PICTURE_SEC = 15.0
LONG_PICTURE_MAX_SEC = 24.0

_CLOSING = "»\"')]”’"
_WORD = re.compile(r"\w")


def long_plan_sec(scene_sec: float) -> float:
    """Длина длинного плана в раскладке секунд: два ориентира картинки, в
    пределах измеренных 15–24 с. Прежние «2.5 ориентира» при 9.5 с давали 24 с
    — верхний край, а длинные планы «Барзаха» в среднем 17.5 с."""
    return float(min(LONG_PICTURE_MAX_SEC, max(LONG_PICTURE_SEC, round(scene_sec * 2))))


def _ends_sentence(token: str) -> bool:
    tail = token.rstrip(_CLOSING)
    return bool(tail) and tail[-1] in ".!?…"


def word_count(tokens: list[str]) -> int:
    """Слов среди токенов: тире и прочие знаки отдельным токеном словом не считаются."""
    return sum(1 for t in tokens if _WORD.search(t))


def sentences_of(text: str) -> list[list[str]]:
    """Текст → предложения списками токенов (по пробелам, знаки при словах).
    Конец предложения — точка, «!», «?» или многоточие, кавычка после знака не
    мешает. Хвост без знака — тоже предложение: реплика, оборванная склейкой."""
    out: list[list[str]] = []
    current: list[str] = []
    for token in text.split():
        current.append(token)
        if _ends_sentence(token) and word_count(current):
            out.append(current)
            current = []
    if word_count(current):
        out.append(current)
    return out


def narration_of(script: str) -> str:
    """Закадровый текст сценария: слой «Текст» блоков script.md, а если блоков
    нет (brief, сценарий руками без слоёв) — сам текст без заголовков."""
    blocks = [b.text for b in parse_script(script) if b.text]
    if blocks:
        return " ".join(blocks)
    return " ".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


class PictureRhythm(NamedTuple):
    """Ритм картинок проекта — числа, которые модель получает готовыми."""

    scene_sec: float  # ориентир `video.scene_duration_sec`
    words: int  # слов на картинку: ориентир × WORDS_PER_SEC
    sentence_words: float  # медиана слов в предложении этого текста; 0 — предложений нет
    long_sentence_words: int  # предложение длиннее — две картинки
    cut_words: int  # первая картинка длинной фразы — столько слов

    @property
    def sentences(self) -> float:
        """Предложений на картинку по предложениям этого сценария, до половины."""
        if self.sentence_words <= 0:
            return 0.0
        return max(1.0, round(self.words / self.sentence_words * 2) / 2)


def picture_rhythm(scene_sec: float, script: str = "") -> PictureRhythm:
    """Ориентир картинки в словах и предложениях — арифметика, а не просьба к
    модели: «9.5 с при 2.0 слова/с» она пересчитает в слова кое-как, а в
    предложения своего же сценария — никак."""
    words = max(1, round(scene_sec * WORDS_PER_SEC))
    lengths = [word_count(s) for s in sentences_of(narration_of(script))] if script else []
    return PictureRhythm(
        scene_sec=scene_sec,
        words=words,
        sentence_words=float(statistics.median(lengths)) if lengths else 0.0,
        long_sentence_words=max(
            round(words * LONG_SENTENCE_FACTOR), round(LONG_SENTENCE_MIN_SEC * WORDS_PER_SEC)
        ),
        cut_words=round(MID_PHRASE_CUT_SEC * WORDS_PER_SEC),
    )


def cut_long_sentence(tokens: list[str], cut_words: int) -> tuple[list[str], list[str]]:
    """Длинная фраза → две картинки: где резать. Первая часть — около
    `cut_words` слов (~5 с речи): ближайшая к этому месту запятая, двоеточие,
    точка с запятой или тире в пределах трёх слов (полторы секунды), а нет их —
    ровно между словами. Хвост не короче `MIN_SCENE_SEC` речи: короче кадр не
    читается, и фраза такой длины остаётся одной картинкой."""
    tail_min = round(MIN_SCENE_SEC * WORDS_PER_SEC)
    counts: list[int] = []  # слов до разреза перед токеном j
    total = 0
    for token in tokens:
        counts.append(total)
        total += 1 if _WORD.search(token) else 0
    if total < cut_words + tail_min:
        return tokens, []
    window = 3
    candidates = [
        j
        for j in range(1, len(tokens))
        if abs(counts[j] - cut_words) <= window
        and total - counts[j] >= tail_min
        and (tokens[j - 1].rstrip(_CLOSING)[-1:] in ",;:" or tokens[j] in ("—", "–"))
    ]
    if candidates:
        j = min(candidates, key=lambda j: (abs(counts[j] - cut_words), j))
    else:
        j = next(j for j in range(1, len(tokens)) if counts[j] >= cut_words)
    return tokens[:j], tokens[j:]


# сколько длинных фраз показать модели с готовым разрезом: у списка на 24 минуты
# их десяток-полтора, дальше — счёт
LONG_SENTENCES_SHOWN = 30


def long_sentences_text(script: str, rhythm: PictureRhythm) -> str:
    """Длинные фразы сценария с уже выбранным разрезом — считает python: модель
    слова в своей фразе не считает, а резать «после пяти секунд» без счёта нельзя."""
    found = [
        s for s in sentences_of(narration_of(script)) if word_count(s) > rhythm.long_sentence_words
    ]
    if not found:
        return "No sentence of this script runs that long."
    rows = []
    for tokens in found[:LONG_SENTENCES_SHOWN]:
        head, tail = cut_long_sentence(tokens, rhythm.cut_words)
        rows.append(f"  · «{' '.join(head)} | {' '.join(tail[:4])}…» ({word_count(tokens)} words)")
    more = len(found) - LONG_SENTENCES_SHOWN
    rows += [f"  · and {more} more of the same kind"] if more > 0 else []
    return (
        "These sentences of the script run that long; the cut («|») is already chosen:\n"
        + "\n".join(rows)
    )


def picture_text(rhythm: PictureRhythm) -> str:
    """Строка ориентира для промпта: слова на картинку, секунды, предложения."""
    text = (
        f"a picture holds about {rhythm.words} words — the median picture on the analysed "
        f"channels runs {rhythm.scene_sec:g} s, at {WORDS_PER_SEC} words per second"
    )
    if rhythm.sentences:
        text += (
            f"; the sentences of this script run about {rhythm.sentence_words:g} words, so a "
            f"picture carries about {rhythm.sentences:g} of them"
        )
    return text + "."


class RhythmStats(NamedTuple):
    """Ритм готовой раскадровки по КАРТИНКАМ: продолжения одного плана
    (`Scene.continues`) — одна картинка, а не несколько."""

    pictures: int
    median_sec: float
    sentences: float  # медиана предложений на картинку с речью (доли — части фраз)
    one_sentence_share: float  # доля картинок с речью, где ровно одно целое предложение
    long_pictures: int  # картинок не короче LONG_PICTURE_SEC
    long_time_share: float
    long_sentences_whole: int  # фраз длиннее порога, оставшихся на одной картинке

    def describe(self, rhythm: PictureRhythm) -> str:
        sentences = f" (ориентир ~{rhythm.sentences:g})" if rhythm.sentences else ""
        text = (
            f"ритм: медиана картинки {self.median_sec:.1f} с (ориентир {rhythm.scene_sec:g}), "
            f"предложений на картинку {self.sentences:g}{sentences}, "
            f"ровно одно предложение {self.one_sentence_share:.0%}, "
            f"длинных планов ≥ {LONG_PICTURE_SEC:g} с: {self.long_pictures} "
            f"({self.long_time_share:.0%} времени)"
        )
        if self.long_sentences_whole:
            text += (
                f", фраз длиннее {rhythm.long_sentence_words} слов на одной картинке: "
                f"{self.long_sentences_whole}"
            )
        return text


def rhythm_stats(scenes: list[Scene], rhythm: PictureRhythm) -> RhythmStats:
    """Сколько предложений несёт картинка — теми же категориями, что замер
    17.09: доля фразы на картинке считается по словам, «ровно одно» — картинка
    начинается с начала фразы и кончается её концом."""
    pictures = shots(scenes)
    lengths = [sum(s.duration_sec for s in group) for group in pictures]
    owner: list[int] = []  # картинка каждого слова по порядку
    ends: list[bool] = []  # слово кончает предложение
    for k, group in enumerate(pictures):
        for scene in group:
            for token in scene.narration.split():
                if _WORD.search(token):
                    owner.append(k)
                    ends.append(_ends_sentence(token))
                elif ends and _ends_sentence(token):
                    ends[-1] = True  # «…» отдельным токеном кончает фразу слова перед ним
    counted = Counter(owner)
    words = [counted[k] for k in range(len(pictures))]
    shares = [0.0] * len(pictures)
    one = [False] * len(pictures)
    whole_long = 0
    start = 0
    for i in range(len(owner)):
        if not ends[i] and i + 1 < len(owner):
            continue  # хвост без знака в конце ролика — тоже предложение
        sentence = owner[start : i + 1]
        start = i + 1
        for k in sentence:
            shares[k] += 1 / len(sentence)
        if len(set(sentence)) > 1:
            continue  # фразу режет склейка
        one[sentence[0]] = words[sentence[0]] == len(sentence)
        whole_long += int(len(sentence) > rhythm.long_sentence_words)
    spoken = [k for k in range(len(pictures)) if words[k]]
    total = sum(lengths)
    long = [x for x in lengths if x >= LONG_PICTURE_SEC - 1e-6]
    return RhythmStats(
        pictures=len(pictures),
        median_sec=float(statistics.median(lengths)) if lengths else 0.0,
        sentences=round(float(statistics.median(shares[k] for k in spoken)), 1) if spoken else 0.0,
        one_sentence_share=sum(1 for k in spoken if one[k]) / len(spoken) if spoken else 0.0,
        long_pictures=len(long),
        long_time_share=sum(long) / total if total > 0 else 0.0,
        long_sentences_whole=whole_long,
    )


def load_rhythm(ctx: StageContext) -> list[dict[str, Any]]:
    """Разметка смен из `retention.json` сценариста — источник ритма.

    Её нет, если сценарий написан руками: тогда планнер работает как раньше,
    по одной проектной длительности. Битая разметка — не повод ронять стадию."""
    path = ctx.project_dir / SCRIPT_DIR / RETENTION_FILE
    if not path.exists():
        return []
    try:
        retention = read_model(path, Retention)
    except (ValidationError, ValueError) as exc:
        ctx.log.warning("%s не читается (%s) — планирую без разметки ритма", RETENTION_FILE, exc)
        return []
    return [
        {"t_sec": b.t_sec, "changes": b.changes, "note": b.note}
        for b in sorted(retention.beats, key=lambda b: b.t_sec)
    ]


def available_cast(ctx: StageContext) -> list[str]:
    """Все визуальные ресурсы, видимые эпизоду: сначала проектные, потом общие."""
    seen: list[str] = []
    for base in ctx.resource_dirs:
        for kind, ids in list_resources(base).items():
            if kind not in VISUAL_KINDS:
                continue
            for resource_id in ids:
                ref = f"{kind}/{resource_id}"
                if ref not in seen:
                    seen.append(ref)
    return sorted(seen)


# где реплику можно разрезать, от лучшего места к худшему: конец предложения,
# запятая, тире, двоеточие, точка с запятой (там у живого диктора пауза 0.2–0.3 с,
# замер research/_probes/speech_pauses_2026-09-16), и крайний случай — между
# словами
_BREAKS = (r"(?<=[.!?…])\s+", r"(?<=[,;:])\s+|\s+(?=[—–]\s)", r"\s+")


def split_narration(text: str, parts: int) -> list[str]:
    """Реплику — на `parts` частей примерно поровну по длине, ни одной пустой.

    Нужно, когда бит не влезает в один клип: переносить весь текст на первый
    план нельзя — озвучка окажется длиннее клипа, и кадр замрёт.

    Режется по предложениям; не хватает предложений — по запятой, тире,
    двоеточию; нет и их — между словами. До 16.09.2026 бит из одного
    предложения отдавал весь текст первому клипу, а второй молчал по плану:
    на ep001 так вышло 15 немых половинок и 52 с тишины. Разрез между словами —
    минимальное безопасное решение: голос через склейку двух клипов потребовал бы
    резать один mp3 между сценами в озвучке и сборке. Пауза на таком стыке —
    вшитая тишина mp3 (ru_male ~1.2 с), и её срезает следующая волна правок
    озвучки; немая половина хуже. Пустые части остаются, только когда слово
    одно — такую раскадровку ловит `silence_findings`."""
    if parts < 2 or not text.strip():
        return [text] + [""] * (parts - 1)
    for pattern in _BREAKS:
        pieces = [p for p in re.split(pattern, text.strip()) if p.strip()]
        if len(pieces) >= parts:
            return _group(pieces, parts)
    return [text] + [""] * (parts - 1)


def _group(pieces: list[str], parts: int) -> list[str]:
    """Куски подряд → `parts` частей, в каждой хотя бы один кусок, границы —
    ближе всего к равным долям по числу знаков."""
    ends: list[int] = []  # сумма знаков после каждого куска
    for piece in pieces:
        ends.append((ends[-1] if ends else 0) + len(piece))
    total = ends[-1]
    cuts: list[int] = []
    start = 0
    for k in range(1, parts):
        goal = total * k / parts
        # часть k кончается после куска j-1: в ней хотя бы один кусок, и на
        # оставшиеся части кусков хватает
        options = range(start + 1, len(pieces) - (parts - k) + 1)
        cut = min(options, key=lambda j: (abs(ends[j - 1] - goal), j))
        cuts.append(cut)
        start = cut
    bounds = [0, *cuts, len(pieces)]
    return [" ".join(pieces[a:b]) for a, b in zip(bounds, bounds[1:], strict=False)]


def split_long_scenes(scenes: list[Scene], ceiling: float) -> list[Scene]:
    """Бит длиннее самого длинного клипа → несколько сцен одного плана.

    Так длинный кадр 15–20 с из playbook становится выполнимым на провайдере,
    который умеет только 6 или 10 секунд. Кадр один, голос идёт через все
    звенья: реплика делится между ними (`split_narration`). Делит python, а не
    модель: деление арифметическое, лишний вызов ничего не добавит."""
    out: list[Scene] = []
    for scene in scenes:
        parts = max(1, math.ceil(scene.duration_sec / ceiling - 1e-6))
        if parts == 1:
            out.append(scene)
            continue
        piece = round(scene.duration_sec / parts, 1)
        texts = split_narration(scene.narration, parts)
        previous = ""
        for n, text in enumerate(texts):
            part = scene.model_copy(deep=True)
            part.id = scene.id if n == 0 else f"{scene.id}_{n + 1}"
            part.duration_sec = piece
            part.planned_sec = piece
            part.narration = text
            part.continues = previous or None
            previous = part.id
            out.append(part)
    for i, scene in enumerate(out, start=1):
        scene.order = i
    return out


OVERLAY_KINDS: tuple[OverlayKind, ...] = ("caption", "source", "label")


def parse_overlay(raw: Any, scene_no: int) -> SceneOverlay | None:
    """Плашка сцены из ответа модели. Пусто или null — плашки нет; неверный вид
    или текст длиннее предела — брак ответа, модель отвечает второй раз:
    плашка на 120 знаков не читается за секунду, а резать её молча — менять
    смысл, который написала модель."""
    if raw is None or raw == "" or raw == {}:
        return None
    if isinstance(raw, str):
        raw = {"kind": "caption", "text": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"сцена {scene_no}: overlay должен быть объектом или null")
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    kind = str(raw.get("kind") or "caption").strip().lower()
    if kind not in OVERLAY_KINDS:
        raise ValueError(
            f"сцена {scene_no}: overlay.kind {kind!r} — допустимы {', '.join(OVERLAY_KINDS)}"
        )
    if len(text) > OVERLAY_MAX_CHARS:
        raise ValueError(
            f"сцена {scene_no}: плашка «{text[:40]}…» длиннее {OVERLAY_MAX_CHARS} знаков — "
            f"за секунду не прочитать; оставь одну мысль"
        )
    words = {w.lower().strip(".,!?…:;»«\"'") for w in text.split()}
    emphasis: list[str] = []
    for item in raw.get("emphasis") or []:
        if not isinstance(item, str):
            continue
        clean = item.strip().lower().strip(".,!?…:;»«\"'")
        # подсвечивается слово из самого текста, и не частица: на ep001 модель
        # красила «не», «только», «Три» — ключевое слово плашки это существительное
        # или число, служебное слово цветом ничего не говорит
        if clean in words and clean not in STOP_WORDS and (len(clean) >= 3 or clean.isdigit()):
            emphasis.append(item.strip())
    return SceneOverlay(text=text, kind=kind, emphasis=emphasis)  # type: ignore[arg-type]


TEMPOS: tuple[SceneTempo, ...] = ("action", "calm")


def parse_tempo(raw: Any, scene_no: int) -> SceneTempo | None:
    """Темп сцены из ответа модели. Пусто или null — не сказано, решит стадия
    video по словам промпта; значение вне двух допустимых — брак ответа, модель
    отвечает второй раз: молча принять «medium» за «action» значит выдумать
    решение за неё."""
    if raw is None or raw == "":
        return None
    value = str(raw).strip().lower()
    if value not in TEMPOS:
        raise ValueError(f"сцена {scene_no}: tempo {value!r} — допустимы {', '.join(TEMPOS)}")
    return cast(SceneTempo, value)


DURATION_TOLERANCE = 0.15  # насколько раскадровка вправе разойтись с целью
# Переспроса «сцен больше 1.4× оценки» больше нет (16.09.2026). Он появился на
# ep001 (просили ~67, пришло 82) под мыслью «каждая сцена стоит клипа», но
# оценка бралась из медианной длины сцены, и переспрос бил ровно по коротким
# сценам, которые содержание просит: пять реплик по 2 с — не дробность. Цена
# сцены держится бюджетом и долей клипов (`video.clip_share`), а не счётом


# --- тишина. Замер research/_probes/speech_pauses_2026-09-16: у конкурентов
# посреди ролика нет ни одной паузы длиннее 2.1 с на ~85 минутах звука, доля
# тишины 0–1.8%; у ep001 — 52%, и 112 с из неё дали немые сцены раскадровки:
# четыре «длинные сцены-выдоха» по 15 с и 15 немых половинок разрезанных бит.
# Без слов законны два места: вспышка до первого слова (у конкурентов первое
# слово на 0.0–0.44 с) и конечная заставка (у референсного канала A хвост 3.7–6.4 с)
SILENT_OPENING_SEC = 1.0
SILENT_END_SCREEN_SEC = 5.0
# немых секунд в раскадровке (кроме двух законных мест) — не больше доли
# хронометража, и ни одной немой серии сцен подряд длиннее этого
SILENT_SHARE_LIMIT = 0.01
SILENT_RUN_LIMIT_SEC = 1.5


def silent_allowed(scene: Scene, *, first: bool, last: bool) -> bool:
    """Сцена без слов на законном месте: первая — вспышка не дольше секунды,
    последняя (не продолжение плана) — конечная заставка до 5 с."""
    if first and scene.duration_sec <= SILENT_OPENING_SEC:
        return True
    return last and not scene.continues and scene.duration_sec <= SILENT_END_SCREEN_SEC


class Silence(NamedTuple):
    """Немые места раскадровки до денег: сколько секунд, какая доля и какие
    серии сцен подряд длиннее `SILENT_RUN_LIMIT_SEC`. Законные места
    (`silent_allowed`) не считаются."""

    seconds: float
    share: float
    runs: list[tuple[list[str], float]]  # id сцен серии и её длина

    @property
    def ok(self) -> bool:
        return self.share <= SILENT_SHARE_LIMIT and not self.runs

    def describe(self) -> str:
        runs = "; ".join(f"{ids[0]}…{ids[-1]} {sec:.1f} с" for ids, sec in self.runs[:6])
        more = f" (+{len(self.runs) - 6})" if len(self.runs) > 6 else ""
        return (
            f"немых {self.seconds:.1f} с ({self.share:.1%} при пределе "
            f"{SILENT_SHARE_LIMIT:.0%})"
            + (f", серии длиннее {SILENT_RUN_LIMIT_SEC:g} с: {runs}{more}" if runs else "")
        )


def silence_findings(scenes: list[Scene]) -> Silence:
    """Сколько в раскадровке немых секунд и где они стоят сериями.

    Считается по раскадровке, до озвучки и генерации: немая сцена ep001 стоила
    кадра и трёх клипов, прежде чем проверка готового файла её увидела."""
    ordered = sorted(scenes, key=lambda s: s.order)
    total = sum(s.duration_sec for s in ordered)
    seconds = 0.0
    runs: list[tuple[list[str], float]] = []
    run: list[Scene] = []

    def close() -> None:
        length = sum(s.duration_sec for s in run)
        if run and length > SILENT_RUN_LIMIT_SEC + 1e-6:
            runs.append(([s.id for s in run], round(length, 2)))
        run.clear()

    for n, scene in enumerate(ordered):
        mute = not scene.narration.strip() and not silent_allowed(
            scene, first=n == 0, last=n == len(ordered) - 1
        )
        if mute:
            seconds += scene.duration_sec
            run.append(scene)
        else:
            close()
    close()
    return Silence(round(seconds, 2), seconds / total if total > 0 else 0.0, runs)


def parse_scenes(
    raw: Any,
    *,
    known_cast: set[str],
    default_duration: float,
    ceiling: float = 0.0,
    target_sec: float = 0.0,
) -> list[Scene]:
    """Ответ LLM → сцены. Нумерация наша: порядок задаёт список, а не модель.

    Длительность сцены с речью не короче `MIN_SCENE_SEC`: короче кадр не
    читается, и стадия озвучки всё равно поднимет её до этого пола — поэтому и
    сумма против цели считается по длине, которую сцена получит на экране.

    Сцена без слов — брак ответа, кроме вспышки в начале и заставки в конце
    (`silent_allowed`): голос идёт через весь ролик, картинка меняется под ним."""
    if not isinstance(raw, list):
        raise ValueError(f"ожидался список сцен, пришло {type(raw).__name__}")
    if not raw:
        raise ValueError("модель вернула пустой список сцен")

    scenes: list[Scene] = []
    dropped: list[str] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"сцена {i}: ожидался объект, пришло {type(item).__name__}")
        cast = [c for c in item.get("cast") or [] if isinstance(c, str)]
        dropped += [c for c in cast if c not in known_cast]
        narration = str(item.get("narration") or "").strip()
        duration = float(item.get("duration_sec") or default_duration)
        if narration:
            duration = max(MIN_SCENE_SEC, duration)
        scenes.append(
            Scene(
                id=f"s{i:02d}",
                order=i,
                duration_sec=duration,
                planned_sec=duration,  # пол для озвучки: сцена не схлопнется до реплики
                narration=narration,
                visual_prompt=str(item.get("visual_prompt") or "").strip(),
                motion_prompt=str(item.get("motion_prompt") or "").strip(),
                tempo=parse_tempo(item.get("tempo"), i),
                cast=[c for c in cast if c in known_cast],
                overlay=parse_overlay(item.get("overlay"), i),
            )
        )
    # немая сцена — первым: на ep001 четыре таких по 15 с и дали дыры тишины,
    # а номера должны дожить до модели в обрезанном до 200 знаков тексте ошибки
    mute = [
        i
        for i, scene in enumerate(scenes, start=1)
        if not scene.narration and not silent_allowed(scene, first=i == 1, last=i == len(scenes))
    ]
    if mute:
        shown = ", ".join(str(i) for i in mute[:12]) + ("…" if len(mute) > 12 else "")
        raise ValueError(
            f"сцены без текста: {shown} — голос идёт через весь ролик, у сцены реплика по её "
            f"длине ({WORDS_PER_SEC} слова/с); без слов только первая ≤ {SILENT_OPENING_SEC:g} с "
            f"и последняя ≤ {SILENT_END_SCREEN_SEC:g} с"
        )
    if ceiling > 0:
        # длинный бит разрешён и режется автоматически: сцены одного плана
        scenes = split_long_scenes(scenes, ceiling)
    if dropped:
        raise ValueError(f"каст, которого нет в ресурсах: {', '.join(sorted(set(dropped)))}")
    # хронометраж — не пожелание: длительности сцен становятся полом для озвучки,
    # поэтому лишние минуты раскадровки превращаются в лишние минуты тишины
    total = sum(s.duration_sec for s in scenes)
    if target_sec > 0 and abs(total - target_sec) > target_sec * DURATION_TOLERANCE:
        raise ValueError(
            f"сумма длительностей {total:.0f} с при целевых {target_sec:.0f} с — "
            f"разойтись можно не больше чем на {DURATION_TOLERANCE:.0%}; "
            f"{'сократи' if total > target_sec else 'удлини'} сцены"
        )
    # однообразие — такой же брак ответа, как выдуманный каст, и ловится так же
    # до денег: переспросить модель дешевле, чем оплатить пачку одинаковых кадров.
    # Последним, потому что хронометраж и каст чинить осмысленнее в первую очередь
    if severe := blocking(check_variation(scenes)):
        raise ValueError("; ".join(f.message for f in severe))
    return scenes


class PlannerStage(Stage):
    name = "planner"
    # у видео-провайдера планнер спрашивает только потолок длительности — это
    # свойство пресета, ключ для него не нужен
    needs = ("llm",)

    def run(self, ctx: StageContext) -> StageResult:
        if ctx.storyboard.scenes and not ctx.options.force:
            return StageResult(
                status="skipped", message=f"сцены уже есть: {len(ctx.storyboard.scenes)}"
            )

        script_file = script_path(ctx)
        script = script_file.read_text(encoding="utf-8").strip() if script_file.exists() else ""
        source = ctx.rel(script_file) if script else "brief эпизода"
        if not script:
            script = ctx.storyboard.brief.strip()
        if not script:
            return StageResult(
                status="failed",
                message=(
                    f"не из чего планировать: нет ни {SCRIPT_DIR}/{SCRIPT_FILE}, "
                    "ни brief в storyboard.json"
                ),
            )

        video = ctx.project.video if ctx.project else None
        # медиана длины сцены у разобранных каналов — ориентир модели, не норма
        scene_duration = video.scene_duration_sec if video else 6.0
        ceiling = float(max(ctx.providers.video.supported_durations))
        target_sec = float(video.target_duration_sec) if video else 0.0
        long_sec = long_plan_sec(scene_duration)  # «длинный план» раскладки секунд
        # слова и предложения на картинку — из ориентира и предложений этого текста
        picture = picture_rhythm(scene_duration, script)
        cast = available_cast(ctx)
        rhythm = load_rhythm(ctx)

        ctx.log.info(
            "планирую раскадровку на %s из %s (смен в разметке: %s)…",
            f"{target_sec:.0f} с" if target_sec else "хронометраж сценария",
            source,
            len(rhythm) if rhythm else "нет",
        )
        prompt = _PROMPT.format(
            episode=json.dumps(
                {
                    "title": ctx.storyboard.title,
                    "brief": ctx.storyboard.brief,
                    "aspect_ratio": ctx.storyboard.aspect_ratio,
                    "target_duration_sec": video.target_duration_sec if video else None,
                },
                ensure_ascii=False,
            ),
            script=script,
            rhythm=json.dumps(rhythm, ensure_ascii=False) if rhythm else "—",
            rules=ctx.rules or "—",
            style=style_text(ctx.resources.style),
            blueprint=ctx.blueprint or "—",
            cast=json.dumps(cast, ensure_ascii=False),
            language=ctx.storyboard.language,
            scene_duration=f"{scene_duration:g}",
            min_scene=f"{MIN_SCENE_SEC:g}",
            picture=picture_text(picture),
            long_sentence_words=picture.long_sentence_words,
            cut_words=picture.cut_words,
            cut_sec=f"{MID_PHRASE_CUT_SEC:g}",
            long_sentences=long_sentences_text(script, picture),
            budget=time_budget(target_sec=target_sec, long_sec=long_sec),
            long_sec=f"{long_sec:g}",
            long_min=f"{LONG_PICTURE_SEC:g}",
            long_max=f"{LONG_PICTURE_MAX_SEC:g}",
            long_words=round(long_sec * WORDS_PER_SEC),
            words_per_sec=WORDS_PER_SEC,
            words_per_scene=picture.words,
            overlay_chars=OVERLAY_MAX_CHARS,
        )

        last_error = ""
        # раскадровка, прошедшая всё, кроме читаемости: если повтор окажется хуже
        # (не разберётся), остаётся она — загадки в кадре лучше, чем упавшая стадия
        kept: tuple[list[Scene], ReadabilityResult] | None = None
        scenes: list[Scene] = []
        readability = ReadabilityResult()
        style = ctx.resources.style
        style_caught = 0  # конфликтов по словам, из-за которых модель переспрашивали
        style_failed = ""  # перечисление для человека, если и повтор против стиля
        for attempt in range(2):  # второй заход — с текстом ошибки, как в стадиях анализа
            suffix = f"\n\nYour previous answer was rejected: {last_error}" if attempt else ""
            style_failed = ""
            try:
                result = ctx.providers.llm.complete(prompt + suffix, system=SYSTEM, json_mode=True)
            except ProviderError as exc:
                return StageResult(status="failed", message=f"LLM: {exc}")
            ctx.add_cost(result.cost_usd, ctx.providers.llm.name)
            try:
                scenes = parse_scenes(
                    json.loads(strip_fences(result.text)),
                    known_cast=set(cast),
                    default_duration=scene_duration,
                    ceiling=ceiling,
                    target_sec=target_sec,
                )
            except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
                last_error = str(exc)[:200]
                scenes = []
                ctx.log.warning("ответ планнера не принят (попытка %d): %s", attempt + 1, exc)
                continue
            # промпт против стиля — брак ответа того же рода, что выдуманный каст
            # и однообразие, и ловится так же до денег: на ep002 (09.09.2026)
            # восемь кадров подряд были описаны в фотореализме при 2D-стиле, и
            # каждый стоил кадра и проверки. По битам: продолжения копируют
            # промпт. Не в parse_scenes, потому что модели нужен полный список
            # причин и запреты стиля, а текст ошибки там режется до 200 знаков.
            # Раньше читаемости: конфликт виден по словам, вызов не нужен
            if conflicts := find_conflicts([s for s in scenes if not s.continues], style):
                style_caught += len(conflicts)
                style_failed = describe(conflicts)
                last_error = retry_reason(conflicts, style)
                scenes = []
                ctx.log.warning(
                    "ответ планнера не принят (попытка %d): промпт против стиля: %s",
                    attempt + 1,
                    style_failed,
                )
                continue
            # немые места после разбиения бит. Немую сцену модели parse_scenes уже
            # бракует; сюда доходит то, что python разрезать не смог (реплика из
            # одного слова на бит длиннее клипа). Блок с одним повтором, после
            # повтора — ПРОВЕРЬ на воротах, до денег
            if not attempt and not (silence := silence_findings(scenes)).ok:
                last_error = (
                    f"{silence.describe()} — сцены с s01 по порядку твоего списка, "
                    "_2/_3 — части одной сцены; дай этим сценам реплику по их длине"
                )
                scenes = []
                ctx.log.warning(
                    "ответ планнера не принят (попытка %d): %s", attempt + 1, silence.describe()
                )
                continue
            # читаемость без звука — второй вызов, как критик у сценариста: модель
            # описывает кадр вслепую, python сверяет с репликой. Несвязанные сцены —
            # блок с одним повтором; после повтора — предупреждение человеку на ворота
            with ctx.step("читаемость без звука", f"попытка {attempt + 1}") as step:
                try:
                    readability = check_readability(ctx, scenes)
                except ValueError as exc:
                    ctx.log.warning("проверка читаемости не удалась: %s", exc)
                    readability = ReadabilityResult()
                step.note += (
                    f", сцен {readability.checked}, вызовов {readability.calls}, "
                    f"загадок {len(readability.unrelated)}"
                )
            if readability.ok:
                break
            if attempt:
                # повтор не обязан быть лучше: на ep001 (08.09.2026) модель починила
                # шесть названных сцен и написала двенадцать новых загадок. Какую
                # попытку оставить, решает счёт, а не порядок
                if kept is not None and len(kept[1].unrelated) <= len(readability.unrelated):
                    ctx.log.warning(
                        "повтор планнера читается хуже (%d загадок против %d) — "
                        "остаётся первая раскадровка",
                        len(readability.unrelated),
                        len(kept[1].unrelated),
                    )
                    scenes, readability = kept
                break
            kept = (scenes, readability)
            last_error = format_block(readability.unrelated)
            ctx.log.warning(
                "раскадровка не читается без звука (%d сцен), переспрашиваю: %s",
                len(readability.unrelated),
                "; ".join(f.scene_id for f in readability.unrelated),
            )
        if not scenes and kept is not None:
            scenes, readability = kept
        if not scenes:
            reason = f"промпт против стиля: {style_failed}" if style_failed else last_error
            return StageResult(status="failed", message=f"планнер не дал раскадровку: {reason}")
        for riddle in readability.unrelated:
            for scene in scenes:
                if scene.id == riddle.scene_id:
                    scene.checks.notes["readability"] = readability_note(riddle)
            ctx.log.warning("сцена %s: %s", riddle.scene_id, readability_note(riddle))
        silence = silence_findings(scenes)
        if not silence.ok:
            mute_ids = {i for ids, _ in silence.runs for i in ids}
            for scene in scenes:
                if scene.id in mute_ids:
                    scene.checks.notes["silence"] = (
                        "сцена без реплики в немой серии — в готовом ролике это тишина"
                    )
            ctx.log.warning("раскадровка с тишиной: %s", silence.describe())

        # спорное — модели, одним вызовом на эпизод, и только когда по словам
        # чисто (раскадровка с конфликтом по словам сюда не доходит). Совет, не
        # ворота: вердикт плывёт между прогонами, как у QA, — заметка на сцене и
        # «ПРОВЕРЬ» в сообщении, решает человек
        judged: list[Conflict] = []
        if style is not None:
            with ctx.step("промпт против стиля", "мнение модели") as step:
                judged = judge_conflicts(ctx, [s for s in scenes if not s.continues], style)
                step.note += f", спорных {len(judged)}"
            by_id = {s.id: s for s in scenes}
            for conflict in judged:
                by_id[conflict.scene_id].checks.notes["style"] = conflict.why
                ctx.log.warning(
                    "сцена %s: против стиля по мнению модели — %s", conflict.scene_id, conflict.why
                )

        # длинный бит мог разъехаться на несколько кадров — считаем картинки, а
        # не кадры: продолжения одного плана — одна картинка
        stats = rhythm_stats(scenes, picture)
        if target_sec >= 180 and not stats.long_pictures:
            # не ошибка: ритм — измеренный совет, а не закон. Но человек на
            # воротах должен знать, что пульс вышел ровным
            ctx.log.warning(
                "ни одного плана длиннее %g с на %d картинках — ритм ровный по всему "
                "хронометражу; измеренный у разобранных каналов ритм просит "
                "примерно каждый десятый кадр длинным",
                LONG_PICTURE_SEC,
                stats.pictures,
            )

        # блокирующее уже отсеяно в parse_scenes; сюда доходит то, о чём решает
        # человек на воротах — раскадровка не бракуется, но и не молчит
        advisory = check_variation(scenes)
        for finding in advisory:
            ctx.log.warning("однообразие (%s): %s", finding.code, finding.message)

        ctx.storyboard.scenes = scenes
        ctx.save()
        planned = sum(s.duration_sec for s in scenes)
        lengths = sorted({round(s.duration_sec, 1) for s in scenes})
        note = f" · однообразие: {', '.join(f.code for f in advisory)}" if advisory else ""
        overlays = sum(1 for s in scenes if s.overlay is not None)
        readable = readability.checked - len(readability.unrelated)
        riddles = ""
        if readability.unrelated:
            ids = ", ".join(f.scene_id for f in readability.unrelated[:8])
            more = f" (+{len(readability.unrelated) - 8})" if len(readability.unrelated) > 8 else ""
            riddles = f" · ПРОВЕРЬ: без звука не читаются {ids}{more}"
        # по словам в готовой раскадровке конфликтов нет по построению — число
        # говорит, сколько их сняли повтором; мнение модели — на воротах
        style_note = ""
        if style is not None:
            style_note = (
                f" · конфликтов со стилем: {style_caught + len(judged)} "
                f"(по словам {style_caught}{' → повтор' if style_caught else ''}, "
                f"по мнению модели {len(judged)})"
            )
        if judged:
            ids = ", ".join(c.scene_id for c in judged[:8])
            more = f" (+{len(judged) - 8})" if len(judged) > 8 else ""
            riddles += f" · ПРОВЕРЬ: против стиля по мнению модели {ids}{more}"
        if not silence.ok:
            riddles += f" · ПРОВЕРЬ тишину: {silence.describe()}"
        elif silence.seconds:
            riddles += f" · немых {silence.seconds:g} с ({silence.share:.1%})"
        return StageResult(
            status="done",
            message=(
                f"сцен: {len(scenes)} · картинок: {stats.pictures} · "
                f"хронометраж ~{planned:.0f} с · {stats.describe(picture)} · "
                f"длительности: {', '.join(f'{v:g}' for v in lengths)} с · "
                f"плашек: {overlays} · читаемость: {readable}/{readability.checked}"
                f"{style_note} · из {source}{note}{riddles}"
            ),
            changed=True,
        )
