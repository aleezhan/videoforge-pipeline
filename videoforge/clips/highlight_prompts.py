"""Промпт выбора моментов. Отдельно от логики, как у монтажёра и сценариста:
там — файлы и порядок шагов, здесь — что именно спрашиваем у модели.

Что здесь важно и почему:

- Модель получает **транскрипт построчно** и тепловую карту пересмотров
  отдельным блоком. Вход у `claude -p` почти бесплатен по времени до ~60 тысяч
  знаков (замер: 6 с на запуск плюс ~90 символов ответа в секунду), а ответ
  короткий — десяток кандидатов. Транскрипт длиннее `CHUNK_MAX_CHARS` идёт
  кусками (`highlights.chunk_plan`), и тогда после SOURCE стоит блок PART:
  который кусок из скольких, какие минуты, где перекрытие с предыдущим. Без
  него модель принимает первую строку за начало разговора и «размазывает»
  кандидатов по тому, что видит, а не по записи.
- Числа в промпте — ТЕ ЖЕ константы, что проверяет python (`MIN_SEC`, `MAX_SEC`,
  `TURN_LEAD_MAX_SEC`): просить «20–75 секунд» и молча чинить другое — значит
  спорить с собственной моделью каждый прогон.
- Пики карты названы окнами, а не точками: окно у двухчасового видео — полторы
  минуты, и выдавать их за границы момента нельзя. Это подсказка, где смотреть.
- Первая строка — маркер шага: по нему заглушка LLM отличает вызов.
"""

from __future__ import annotations

from typing import Any

from videoforge.clips.highlights import (
    HEADLINE_MAX_CHARS,
    MAX_SEC,
    MIN_SEC,
    TURN_LEAD_MAX_SEC,
    Chunk,
    format_clock,
    moment_sentences,
)
from videoforge.clips.source import (
    Chapter,
    HeatPoint,
    Sentence,
    SourceInfo,
    chapter_at,
)

SYSTEM = "You pick self-contained moments out of a long conversation for vertical short-form video."
STEP_MARK = "VIDEOFORGE-STEP:"
STEP_HIGHLIGHTS = "highlights"
STEP_CRITIQUE = "highlights_critique"
TURN_MARK = ">>"  # в строке транскрипта: с этого предложения говорит другой человек

PROMPT = (
    STEP_MARK
    + " "
    + STEP_HIGHLIGHTS
    + """
You are choosing moments from a long recorded conversation to cut into vertical
short videos. You do not write anything new and you do not edit: you point at
pieces of the recording that already work on their own.

SOURCE
{source}{part}

{signal}
{peaks}

CHANNEL RULES (this channel never breaks them)
{rules}

WHAT WORKS, MEASURED ON ANALYSED CHANNELS (rules carry their own status:
"держится на N каналах" is evidence, "опровергнуто" means the rule failed
somewhere and its scope is narrower than it looks — respect both)
{blueprint}

TRANSCRIPT (one sentence per line: [time] (chapter) text. "{turn}" marks a
sentence where another person starts speaking. The chapter titles come from the
author of the video and may be in another language — that is fine.)
{transcript}

Return ONLY a valid JSON object:
{{"candidates": [{{"start": "0:12:43", "end": "0:13:31",
   "title": "short name of the moment",
   "hook": "what holds the viewer in the very first second",
   "why": "why this is a moment, one sentence",
   "kind": "story"}}]}}

What makes a moment worth cutting — in this order:
1. **It ends with something.** A punchline, a number, a confession, a verdict, a
   turn. A stretch of pleasant conversation that just stops is not a moment.
2. **The first two seconds already hold.** The opening line is a claim, a
   question, a conflict or a number — not a warm-up, not "ну вот", not the tail
   of someone else's sentence.
3. **A stranger understands it.** Names, nicknames, matches, deals and events
   the viewer has not been told about kill the moment: "Мага не предупредил, он
   трансфер сделал" means nothing to someone who did not watch the podcast.
   Either the moment explains who and what inside itself, or it is not a moment.
4. **It is about one thing.** If the answer wanders into a second topic, cut
   before the wandering starts.

Rules:
- Up to {count} candidates. Give every moment that passes the four points above
  and nothing else: a weak moment costs more than a missing one, and everything
  you send is read again and judged.
- {min_sec:g}-{max_sec:g} seconds long. `start` is the first word of a sentence,
  `end` is the last word of a sentence — never mid-sentence. Copy the times from
  the TRANSCRIPT lines, do not invent them.
- If the host's question sets the answer up and is shorter than {lead_sec:g}
  seconds, start from the question — otherwise the viewer gets an answer to
  nothing.
- Never two candidates from the same place, and spread them over the whole
  {scope}, not over the lines you read first.
- `title`, `hook` and `why` in {language}. `hook` is a phrase from the moment
  itself, not a description of it.
- `kind`: story | claim | conflict | humor | advice.{retry}
"""
)

SYSTEM_CRITIC = (
    "You are the editor of a shorts channel. You decide what goes out. "
    "You are paid for the videos that hold, not for the ones you approved."
)

CRITIQUE = (
    STEP_MARK
    + " "
    + STEP_CRITIQUE
    + """
Below are moments picked from a long conversation, with their full text. You
close the weak ones and package the ones that survive.

SOURCE: {source_title}
ANSWER LANGUAGE: {language}

CANDIDATES
{candidates}

For every candidate return a verdict. **Closing is the default.** A moment
survives only if all four are true:
- it ends with something — a punchline, a number, a confession, a turn;
- its first two seconds already hold: a claim, a question, a conflict, a number;
- a stranger who never watched this podcast understands it without help. Names
  and events the viewer was not told about are grounds to close it, even if the
  scene is funny to those who know;
- it is about one thing from beginning to end.

`why` is the reason to CLOSE, in one sentence, naming what exactly the viewer
does not get. For a moment you keep, `why` says what makes it hold — also one
sentence, also concrete. Never write praise without a reason.

For every SURVIVING moment also write the packaging:
- `title`: what the viewer sees over the preview and decides on in one second.
  Up to {title_max} characters, in {language}. It must make a stranger want to
  tap: lead with the strongest word — a known name, a number, a quoted phrase —
  and give the setup without the payoff (the video pays it off). The intrigue
  must come FROM THE MOMENT: a claim, a number, a confession, a question that
  the video answers. Never a summary ("The guest talks about his father"), never a
  promise the moment does not keep, no "ШОК", no all-caps, no emoji.
- `description`: two or three sentences — what happens and who is speaking.
  No "watch the full episode", no links, no hashtags: clip channels do not
  send the viewer away, and hashtags go to `hashtags`.
- `hashtags`: 3-6 items without the "#", lowercase, in {language} where natural.{packaging}

Return ONLY a valid JSON object:
{{"verdicts": [{{"id": "h01", "keep": true, "why": "...",
   "title": "...", "description": "...", "hashtags": ["..."]{extra_fields}}}]}}
"""
)

# Правила канала критику — только когда он пишет то, что по ним и делается:
# подпись над кадром и выброс реплик (`video.headline`, `video.tighten`). Без
# этих флагов промпт критика тот же, что был, и упаковка каналов, собранных до
# 17.09.2026, не меняется от того, что появилось здесь
CRITIC_RULES = """

CHANNEL RULES (the packaging and the cuts follow them)
{rules}"""

# Подпись над кадром — по замеру нарезчиков мультсериалов (лучшая медиана
# референсных каналов; разбор референсных каналов нарезок, 17.09.2026): реплика зрителя, а не
# заголовок, и такие ролики собирают в 3–6 раз больше комментариев на просмотр
HEADLINE = """
- `headline`: the text shown ABOVE the video for the whole short. It is not the
  title: the title makes a stranger tap, the headline is what a fan would write
  in the comments — a reaction, an opinion, a joke or a question about what
  happens, casual, in {language}; slang is welcome ("genuinely", "bro",
  "lowkey", "no way"). End it with one or two emoji that carry the tone
  (😭 💀 🤯 😂 ❤️ 🔥). Up to {headline_max} characters. Never give the punchline
  away before the video does, never a summary of the scene."""

# Выброс реплик: жёсткие склейки внутри момента. Номера — те же [n], которыми
# размечен текст кандидата (`candidates_text`); python переводит их в секунды
TIGHTEN = """
- `drop`: numbers of the sentences to CUT OUT of a surviving moment because they
  are dead weight — a filler line, a detour, a repeat, a reaction that stalls
  the joke. The numbers are the [n] marks in the text. What stays is played
  back to back with hard cuts, so it must still make sense and still land its
  payoff: never drop the first or the last sentence, a line the payoff needs, or
  the beat right before the punchline. Most good scenes need no cuts — then
  return an empty list."""

# The heatmap is YouTube's "most replayed" graph: a measured signal, not a model's guess
REPLAYS_HEAD = """WHERE VIEWERS REWATCHED (YouTube replay graph — measured, not an opinion. Each
line is a window of the recording and how often it was replayed relative to the
most replayed one. Use them to know where to look, not as boundaries.)"""

RETRY = """

YOUR PREVIOUS ANSWER WAS REJECTED by the checker. Fix exactly these and return
the whole answer again (the candidates that were fine can stay as they are):
{reasons}"""


def source_text(
    info: SourceInfo, *, platforms: list[str], target_duration_sec: int, language: str
) -> str:
    lines = [
        f"title: {info.title or info.id}",
        f"channel: {info.channel or '—'}",
        f"length: {format_clock(info.duration_sec)}",
        f"spoken language: {info.transcript_language or info.language}",
        f"platforms of the shorts channel: {', '.join(platforms) or '—'}",
        f"target length of one short: {target_duration_sec} s",
        f"answer language: {language}",
    ]
    return "\n".join(lines)


def peaks_text(peaks: list[HeatPoint], chapters: list[Chapter]) -> str:
    """Пики карты по времени, а не по величине: так видно, что они разбросаны по
    записи. Ширина окна названа прямо — у двухчасового видео это полторы минуты,
    и принять её за границы момента нельзя."""
    if not peaks:
        return "(no replay graph for this video — rely on the transcript alone)"
    window = sum(h.end_sec - h.start_sec for h in peaks) / len(peaks)
    lines = [f"windows of about {window:.0f} s each, the {len(peaks)} most replayed:"]
    lines += [
        f"{format_clock(h.start_sec)}-{format_clock(h.end_sec)}  {h.value:.2f}  "
        f"{chapter_at(chapters, h.start_sec)}".rstrip()
        for h in sorted(peaks, key=lambda h: h.start_sec)
    ]
    return "\n".join(lines)


PART = """

PART {index} OF {total}: {start}–{end} of a {length} recording. The other parts
are read in separate calls with the same rules: pick only from the lines below
and spread the candidates over this part.{overlap}"""

OVERLAP = """ The lines before {own_start}
repeat the tail of part {previous} so that a moment sitting on the seam is not
lost — a moment that starts there counts."""


def part_text(part: tuple[Chunk, int] | None, *, duration_sec: float) -> str:
    """Блок PART: который кусок из скольких, какие минуты, где перекрытие.
    Пусто у единственного куска — промпт тогда тот же, что до кусков."""
    if part is None:
        return ""
    chunk, total = part
    overlap = (
        OVERLAP.format(own_start=format_clock(chunk.own_start_sec), previous=chunk.index - 1)
        if chunk.own_first > chunk.first
        else ""
    )
    return PART.format(
        index=chunk.index,
        total=total,
        start=format_clock(chunk.start_sec),
        end=format_clock(chunk.end_sec),
        length=format_clock(duration_sec),
        overlap=overlap,
    )


def transcript_text(sentences: list[Sentence]) -> str:
    """Предложения построчно. Глава — в каждой строке: так строку можно вырвать
    из контекста, и так же выглядит кусок, когда транскрипт режется на части."""
    return "\n".join(
        f"[{format_clock(s.start_sec)}] ({s.chapter}) "
        f"{TURN_MARK + ' ' if s.speaker_turn else ''}{s.text}"
        for s in sentences
    )


def candidates_text(candidates: list[Any], *, sentences: list[Sentence] | None = None) -> str:
    """Кандидаты критику: с временем, длиной, heat и ПОЛНЫМ текстом.

    Текст целиком — потому что решение «поймёт ли посторонний» принимается по
    словам, а не по названию, которое первая модель себе же и придумала.
    `sentences` — транскрипт источника: тогда текст идёт построчно с номерами
    [n] и длиной реплики — по номерам критик называет, что выбросить."""
    lines = []
    heat = "rewatched"
    for c in candidates:
        if sentences is not None:
            text = "\n" + "\n".join(
                f"[{n}] ({s.end_sec - s.start_sec:.1f} s) {s.text}"
                for n, s in enumerate(moment_sentences(sentences, c), start=1)
            )
        else:
            text = f" {c.text}"
        lines.append(
            f"### {c.id} · {format_clock(c.start_sec)}–{format_clock(c.end_sec)} · "
            f"{c.duration_sec:.0f} s · {heat} {c.heat:.2f} · {c.kind or '—'}\n"
            f"working title: {c.title or '—'}\n"
            f"picked because: {c.why or '—'}\n"
            f"text:{text}"
        )
    return "\n\n".join(lines)


def critique_prompt(
    candidates: list[Any],
    *,
    source_title: str,
    language: str,
    title_max: int,
    rules: str = "",
    headline: bool = False,
    tighten: bool = False,
    sentences: list[Sentence] | None = None,
) -> str:
    """`headline` и `tighten` — критик ещё
    пишет подпись над кадром и называет лишние реплики (нужен `sentences`); с
    ними же он получает правила канала — подпись и склейки делаются по ним."""
    packaging = ""
    extra = ""
    if headline:
        packaging += HEADLINE.format(language=language, headline_max=HEADLINE_MAX_CHARS)
        extra += ', "headline": "..."'
    numbered = tighten and sentences is not None
    if numbered:
        packaging += TIGHTEN
        extra += ', "drop": []'
    if packaging and rules.strip():
        packaging = CRITIC_RULES.format(rules=rules.strip()) + "\n" + packaging
    return CRITIQUE.format(
        candidates=candidates_text(candidates, sentences=sentences if numbered else None),
        source_title=source_title or "—",
        language=language,
        title_max=title_max,
        packaging=packaging,
        extra_fields=extra,
    )


def highlights_prompt(
    *,
    info: SourceInfo,
    sentences: list[Sentence],
    peaks: list[HeatPoint],
    rules: str,
    blueprint: str,
    platforms: list[str],
    target_duration_sec: int,
    language: str,
    count: int,
    part: tuple[Chunk, int] | None = None,
    rejected: list[str] | None = None,
) -> str:
    """`part` — (кусок, сколько всего), когда транскрипт идёт кусками;
    `sentences` тогда — строки окна куска, `peaks` — пики внутри него,
    `count` — квота куска."""
    return PROMPT.format(
        source=source_text(
            info,
            platforms=platforms,
            target_duration_sec=target_duration_sec,
            language=language,
        ),
        part=part_text(part, duration_sec=info.duration_sec),
        scope="recording" if part is None else "part",
        signal=REPLAYS_HEAD,
        peaks=peaks_text(peaks, info.chapters),
        rules=rules.strip() or "(none)",
        blueprint=blueprint.strip() or "(none)",
        transcript=transcript_text(sentences),
        turn=TURN_MARK,
        count=count,
        min_sec=MIN_SEC,
        max_sec=MAX_SEC,
        lead_sec=TURN_LEAD_MAX_SEC,
        language=language,
        retry=RETRY.format(reasons="\n".join(f"- {r}" for r in rejected)) if rejected else "",
    )
