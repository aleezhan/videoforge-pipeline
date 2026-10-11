"""Промпты сценариста. Отдельно от стадии: стадия — про файлы и порядок шагов,
здесь — про то, что именно спрашиваем у модели.

Правила берутся из `ctx.blueprint` (playbook + брифы разобранных каналов) и
`ctx.rules` — в текст промпта они не зашиваются. Библиотека хуков, ритм монтажа,
правила открытия живут в playbook и обновляются каждым новым анализом; зашить их
сюда значило бы заморозить знание на дне написания кода.

Первая строка каждого промпта — маркер шага: по нему заглушка отличает вызовы
друг от друга, не угадывая по формулировкам.

Длинный ролик идёт по главам (`CHAPTERS` → `DRAFT_CHAPTER` + `CRITIQUE_CHAPTER`
на каждую главу → `SEAMS` по целому). Форма блока и правила письма у главы те
же, что у сценария одним заходом, — они вынесены в общие куски, чтобы две
разметки не разошлись. Строки `LINKS:` — JSON одной строкой: их читает и
модель, и заглушка.
"""

from __future__ import annotations

from videoforge.core.script import LAYER_EMOTION, LAYER_FRAME, LAYER_TEXT

# не «short-form»: тот же сценарист пишет и шортс, и ролик на полчаса, а длину
# он узнаёт из EPISODE — рамка «короткий формат» тянула бы длинный текст в дробь
SYSTEM = "You are a video script writer for a video channel."
STEP_MARK = "VIDEOFORGE-STEP:"

# Общая шапка: что за канал, что за ролик, какие правила действуют.
_CONTEXT = """CHANNEL RULES (this channel never breaks them)
{rules}

WHAT WORKS, MEASURED ON ANALYSED CHANNELS (rules carry their own status:
"держится на N каналах" is evidence, "опровергнуто" means the rule failed
somewhere and its scope is narrower than it looks — respect both)
{blueprint}

EPISODE
{episode}"""

ANGLES = (
    STEP_MARK
    + """ angles
One topic is not one video. Turn this topic into several DIFFERENT videos.

TOPIC
{topic}

"""
    + _CONTEXT
    + """

Return ONLY a valid JSON object, no fences, no commentary:
{{"options": [{{"id": "a1",
  "title": "how this angle is pitched, one line",
  "premise": "what this particular video is about, one sentence",
  "hook": "the actual first sentence the viewer hears",
  "curiosity": 1, "novelty": 1, "visual": 1, "payoff": 1,
  "why": "why these scores, citing a rule above"}}],
 "rationale": "what separates the strongest angle from the rest"}}

Rules:
- Between 5 and 10 angles. They must differ in what the video IS, not in
  wording: a different question, a different promise, a different payoff.
- Score 1-5 each: curiosity (how badly the viewer needs the answer), novelty
  (how much it is NOT what everyone already says), visual (is there something
  to SHOW, not just narrate), payoff (what the video pays back for its promise).
- `why` must cite a rule from the section above by its wording, or say plainly
  that no measured rule covers this angle. Do not invent evidence.
- Write every value in {language}."""
)

STORY = (
    STEP_MARK
    + """ story
Build the CURIOSITY CHAIN of this video. Not a list of scenes — a chain of gaps.

CHOSEN ANGLE
{angle}{length}

"""
    + _CONTEXT
    + """

Return ONLY a valid JSON object, no fences, no commentary:
{{"gaps": [{{"id": "g1",
  "knows": "what the viewer knows by this point",
  "wants": "the open question that keeps them here",
  "gets": "what they actually receive here",
  "opens": "the new question this raises (empty for the last link only)",
  "planned_sec": 0.0,
  "emotion": "the emotional intent of this link",
  "escalation": "why the stakes are higher than in the previous link"}}],
 "ending": "how the main gap closes INSIDE the video",
 "target_duration_sec": 0.0}}

Rules:
- Every link must raise the stakes over the previous one. A link that only
  restates the previous one is not a link — merge or drop it.
- Every link except the last opens a new question. The last one closes the main
  gap inside this video: the answer is never deferred to another product.
- `planned_sec` is how long that link should last on screen; they must sum to
  roughly the episode's target duration. Vary them — a chain of equal links is
  a flat rhythm, and flat rhythm is what makes viewers leave.
- Write every value in {language}."""
)

# Длина длинного ролика для цепочки: звено — такт внутри главы, а не глава.
# Числа подставляет стадия (`director._story_length`); у короткого ролика пусто
STORY_LENGTH = """

LENGTH: {target_sec} seconds, about {links} links. This is a long video: a link
is one beat of {link_min}-{link_max} seconds, not a chapter — the links will be
grouped into chapters afterwards. Keep the stakes rising across the whole
runtime, not only in the first minutes."""

# Форма блока — одна на сценарий одним заходом и на главу: парсер у них общий
_SHAPE = (
    """## {{link id}} · {{block title}} · {{planned_sec}}s

**"""
    + LAYER_TEXT
    + """:** what the narrator says, word for word, in {language}
**"""
    + LAYER_FRAME
    + """:** what is on screen — one visual intent, not a shot list
**"""
    + LAYER_EMOTION
    + """:** what the viewer should feel here, and from what"""
)

_BLOCK_RULES = """- Hit the word count of every block. A block written short leaves the video
  hanging in silence, and on a long runtime that is what makes people leave.
  Count the words of each block before you answer.
- Keep the block ids of the chain. If you merge or drop a link, say so in the
  first line of the block that absorbed it — a chain the script silently
  abandons cannot be checked against anything.
- The frame layer names the NOUN of the block — the thing, place or person the
  words are about — so that a viewer with the sound off understands in one
  second what this block is about. A frame that hints at something the words
  never say is a riddle, not an illustration. A metaphor (clay and fire, the
  scales, the seed) is allowed at most once per block, and only when the
  narration itself names it. Anything the viewer must READ (a quote, a source,
  a list counter) is written as text for the assembly layer, not drawn.
- No block exists "for context". If a block only sets things up, fold it into
  the one it sets up."""

DRAFT = (
    STEP_MARK
    + """ draft
Write the script for this video.

CURIOSITY CHAIN (one block per link, in order; `words` is how many words of
narration that block MUST contain — it is measured after you answer)
{story}

TOTAL NARRATION REQUIRED: {total_words} words (±10%)

"""
    + _CONTEXT
    + """{revision}

Return the script as Markdown, nothing else — no preamble, no fences around
the whole answer. One section per link of the chain, in this exact shape:

"""
    + _SHAPE
    + """

Rules:
"""
    + _BLOCK_RULES
    + """
- The first block earns the next five seconds on its own. Take the hook from
  the rules above if a measured rule covers this case."""
)

_CRITIQUE_ANSWER = """Return ONLY a valid JSON object, no fences, no commentary:
{{"findings": [{{"kind": "one of: {kinds}",
  "where": "WHERE",
  "why_viewer_leaves": "what happens in the viewer's head there",
  "fix": "the concrete rewrite — a sentence to change, a block to cut or move",
  "blocking": true}}],
 "verdict": "one sentence: is this script worth producing as is"}}"""

_CRITIQUE_RULES = """- `fix` must be actionable: name the sentence, the block, the order. "Усилить",
  "добавить динамики", "сделать интереснее" are not fixes — they are noise.
- `blocking` = true only when the script should not go into production without
  that fix. Be strict but honest: marking everything blocking is as useless as
  marking nothing.
- If the script genuinely has no blocking problem, return an empty findings list
  and say so in the verdict. Do not invent findings to look thorough.
- Write every value in {language}."""

CRITIQUE = (
    STEP_MARK
    + """ critique
You are the critic. Do NOT praise anything. Your only job is to find the reasons
a viewer will close this video, and to name the exact fix for each one.

SCRIPT
{script}

CURIOSITY CHAIN IT WAS BUILT FROM
{story}

"""
    + _CONTEXT
    + """

"""
    + _CRITIQUE_ANSWER.replace("WHERE", "the block id or the timecode")
    + """

Rules:
"""
    + _CRITIQUE_RULES
)

# ------------------------------------------------------------------ по главам

CHAPTERS = (
    STEP_MARK
    + """ chapters
Split this video into CHAPTERS. The curiosity chain below is already built; a
chapter is a run of consecutive links that reads as one complete sub-story —
its own question, its own turn, and an ending that pulls into the next chapter.
Each chapter will be written separately, so this plan is what holds them together.

CHOSEN ANGLE
{angle}

CURIOSITY CHAIN (planned_sec of every link is already computed from the target
duration; a chapter lasts the sum of its links)
LINKS: {links}
HOW THE MAIN GAP CLOSES: {ending}

TARGET DURATION: {target_sec} seconds.
Between {lo} and {hi} chapters, each at most {max_sec} seconds and at least {min_sec} seconds.

"""
    + _CONTEXT
    + """{revision}

Return ONLY a valid JSON object, no fences, no commentary:
{{"promise": "what the opening promises — the thing the last chapter must pay",
 "throughline": "what runs through all chapters (a question, a hero's arc) and where
   it opens and closes; empty if nothing does",
 "chapters": [{{"id": "ch01",
   "title": "short chapter title",
   "about": "what happens here and what the viewer takes from it, 1-2 sentences",
   "links": ["g1", "g2"],
   "ends_with": "the open question its last sentence leaves; for the last chapter,
     how the promise is paid"}}]}}

Rules:
- Every link of the chain goes into exactly one chapter, in the chain's order:
  chapters are consecutive runs of links. Do not add, drop, merge or reorder
  links — the chain is fixed at this step.
- Respect the bounds above; they are computed, not a suggestion.
- Take the chapter structure from the rules above when a measured rule
  describes it: the scheme every chapter follows, where the most charged
  chapter sits, where the carrying question opens and closes.
- Chapters differ in what happens, not in wording. A chapter that only sets up
  the next one is not a chapter — give its links to the chapter it sets up.
- Every chapter except the last ends on an open question. The last one pays the
  promise inside this video.
- Write every value in {language}."""
)

DRAFT_CHAPTER = (
    STEP_MARK
    + """ draft_chapter
Write ONE chapter of a longer video's script: chapter {chapter_id}, number
{chapter_no} of {chapter_count}. Only this chapter — the others are written
separately and joined afterwards.

CHAPTER PLAN OF THE WHOLE VIDEO
{plan}

THIS CHAPTER
{chapter}

LINKS OF THIS CHAPTER (one block per link, in order; `words` is how many words
of narration that block MUST contain — it is measured after you answer)
LINKS: {links}

TOTAL NARRATION REQUIRED IN THIS CHAPTER: {total_words} words (±10%)

ALREADY WRITTEN — the narration of the chapters before this one, as the viewer
has just heard it
{written}

"""
    + _CONTEXT
    + """{revision}

Return the chapter as Markdown, nothing else — no preamble, no chapter heading,
no fences around the whole answer. One section per link, in this exact shape:

"""
    + _SHAPE
    + """

Rules:
"""
    + _BLOCK_RULES
    + """
- Continue from where the text above stops. Do not greet the viewer again, do
  not re-introduce the topic, and do not repeat a fact, source, example or
  image the chapters above already used — the viewer has just heard them.
- {position}"""
)

# Место главы в ролике — подставляет стадия (`director._position`). Начало и
# конец главы — разные правила: первая открывает ролик, последняя платит
# обещание, остальные подхватывают вопрос предыдущей и оставляют свой
POSITION_FIRST = (
    "This is the FIRST chapter: its first block opens the whole video and earns "
    "the next five seconds on its own. Take the hook and the opening from the "
    "rules above if a measured rule covers this case."
)
POSITION_AFTER = "It picks up where {prev_id} stopped: «{prev_ends_with}»."
POSITION_BRIDGE = (
    "Its last block leaves the viewer with «{ends_with}» — the question that "
    "pulls them into the next chapter."
)
POSITION_LAST = (
    "This is the LAST chapter: it pays the promise of the opening («{promise}») "
    "and closes the main gap inside this video («{ending}»). The answer is never "
    "deferred to another video."
)

CRITIQUE_CHAPTER = (
    STEP_MARK
    + """ critique_chapter
You are the critic of ONE chapter of a longer video. Do NOT praise anything.
Your only job is to find the reasons a viewer will close the video inside this
chapter, and to name the exact fix for each one.

CHAPTER {chapter_id} ({chapter_no} of {chapter_count})
{chapter}

LINKS IT WAS WRITTEN FROM
LINKS: {links}

CHAPTER TEXT
{script}

CHAPTER PLAN OF THE WHOLE VIDEO (context only: how the chapters join is judged
separately, after all of them are written)
{plan}

"""
    + _CONTEXT
    + """

"""
    + _CRITIQUE_ANSWER.replace("WHERE", "the block id")
    + """

Rules:
- Judge this chapter against its plan: does it deliver what `about` says, and
  does its last block leave the viewer with `ends_with`.
"""
    + _CRITIQUE_RULES
)

SEAMS = (
    STEP_MARK
    + """ seams
You are the critic of the WHOLE script. It was written chapter by chapter, and
every chapter has already been criticised on its own. Do NOT praise anything
and do NOT repeat per-chapter criticism: find only what a chapter-by-chapter
reading cannot see.

SCRIPT (chapter headings: # chapter id · title · seconds)
{script}

CHAPTER PLAN
{plan}

HOW THE MAIN GAP MUST CLOSE: {ending}

"""
    + _CONTEXT
    + """

"""
    + _CRITIQUE_ANSWER.replace(
        "WHERE",
        "the id of the ONE chapter that must be rewritten (ch03), then the block "
        "id if one block is enough (ch03 g7)",
    )
    + """

Look for:
- repetition between chapters: the same fact, source, example, image or phrase
  told twice. The viewer notices the second time. Name the LATER chapter.
- the promise of the opening: paid by the end, or silently dropped.
- the throughline: opened, carried and closed where the plan says, or lost
  somewhere between chapters.
- seams: a chapter that restarts cold instead of picking up the question the
  previous chapter ended with.

Rules:
"""
    + _CRITIQUE_RULES
)

RETENTION = (
    STEP_MARK
    + """ retention
Mark this script on a timeline: where does something change for the viewer, and
which blocks could be cut without loss.

SCRIPT
{script}

TARGET DURATION: {target_sec} seconds. Blocks: {block_ids}

Return ONLY a valid JSON object, no fences, no commentary:
{{"beats": [{{"t_sec": 0.0,
  "changes": ["any of: {changes}"],
  "note": "what changes there"}}],
 "blocks": [{{"id": "block id",
  "removable": false,
  "if_removed": "what is lost if this block is cut — or why nothing is"}}]}}

Rules:
- A beat is a point where something CHANGES for the viewer: new information, a
  new question, a shift of emotion, a change of scale, a change of place. A
  stretch that changes nothing gets no beat — that silence in the markup is
  itself the finding, so do not invent beats to fill it.
- Mark the whole runtime, right up to {target_sec} seconds. Stopping early
  hides the emptiest part of the video.
- Judge `removable` for EVERY block id listed above, honestly. A script where
  nothing can be cut has not been examined. If a block only repeats what the
  previous one established, say so.
- Write every value in {language}."""
)

SCORE = (
    STEP_MARK
    + """ score
Score how well this script holds attention, using the measurements below.

SCRIPT
{script}

MEASURED ON THE MARKUP (computed, not estimated — do not contradict these)
{measured}

Return ONLY a valid JSON object, no fences, no commentary:
{{"score": 0,
 "score_reasons": {{"открытие": "...", "плотность смен": "...",
   "мёртвые зоны": "...", "payoff": "..."}},
 "weakest": "the weakest stretch of the runtime and why"}}

Rules:
- `score` is 0-100. Anchor it on the numbers above: a script with long dead
  zones or a thin opening cannot score high no matter how good the writing is.
- Every key of `score_reasons` must cite a number or a timecode from the
  measurements. A reason without a number is not a reason.
- Two different scripts must not get the same score by default — the numbers
  differ, so the score must move with them.
- Write every value in {language}."""
)
