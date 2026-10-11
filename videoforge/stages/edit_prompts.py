"""Промпт монтажёра (стадия edit). Отдельно от стадии, как у сценариста:
стадия — про файлы и порядок шагов, здесь — что именно спрашиваем у модели.

Что здесь важно и почему:

- Модель получает ИЗМЕРЕННЫЙ материал (`sources.json`: секунда начала, коробка,
  длина клипа, речь, хвост тишины), а не догадки, и структуру из
  `retention.json` — где для зрителя что-то меняется. Переход имеет смысл там,
  где смена, а не где угодно.
- Ответ — только ОТКЛОНЕНИЯ от заготовки «hard cut везде». Время `claude -p`
  растёт с длиной ответа (6 с + символы/90), полный план на 100 сцен — минуты,
  отклонения — секунды.
- Квоты называются числами прямо в промпте, и это ТЕ ЖЕ константы, что
  проверяет линт (`edit_lint`): модель, которой дали переходы, ставит их везде,
  и лучше сказать «не больше N» заранее, чем возвращать план на переделку.
- Правила канала и измеренные приёмы приходят из `ctx.rules` и `ctx.blueprint`
  (в playbook уже есть «переход через заслонку скрывает несовместимость двух
  сгенерированных сцен») — в текст промпта они не зашиваются. Третий такой
  вход — библиотека приёмов по точкам стадии (`EditStage.decisions`): только
  реализованные и подходящие под условия ролика, с указанием, чем исполняются.
  Модели прямо сказано, через какие поля она на них влияет: приём, который
  исполняет рендерер или ставит человек (даккинг, подложка, звуки), — факт о
  миксе, а не просьба, и включать музыку из-за приёма нельзя.
- Первая строка — маркер шага: по нему заглушка LLM отличает вызов, а строка
  `SCENES:` даёт ей id сцен, чтобы ответ заглушки ссылался на настоящие сцены.
- С 14.09.2026 модель пишет и четыре списка таймлайна — `effects`, `sfx`,
  `music.cues`, `outro` (`features/edit_model_effects`). Правила — из рецепта
  `/epic-edit`, проверенного на двух живых шортсах: удар НА склейку или на
  ударное слово, пролёт кончается на склейке (значит начинается до неё), дроп —
  на слове кульминации, хвост — только когда конец того просит. Секунды — по
  шкале ГОТОВОГО ролика, той же, что `at` в MATERIAL. Звуки — только id из
  блока AVAILABLE (`sfx_text`): выдуманный id линт блокирует, и это причина
  повтора, а не молчаливая потеря. Квоты — те же константы линта, что и у
  переходов, с полом на коротких роликах (`timeline_quota`).
"""

from __future__ import annotations

import math

from videoforge.core.editplan import Sources
from videoforge.core.motion import MotionProfile
from videoforge.core.resources import Sfx
from videoforge.core.script import Retention
from videoforge.stages.edit_lint import (
    CROSSFADE_MAX_SEC,
    DIP_MAX_SEC,
    EFFECT_MAX_SEC,
    EFFECTS_FLOOR,
    EFFECTS_PER_MIN_WARN,
    EMPHASIS_MAX,
    MUSIC_GAIN_RANGE_DB,
    MUSIC_STEPS_WARN,
    OUTRO_SPEED_RANGE,
    OUTRO_TAKE_RANGE_SEC,
    SAME_TRANSITION_RUN_WARN,
    SFX_FLOOR,
    SFX_GAIN_RANGE_DB,
    SFX_PER_MIN_WARN,
    TRANSITION_SHARE_WARN,
)

SYSTEM = "You are a film editor cutting a narrated video from finished, approved scene clips."
STEP_MARK = "VIDEOFORGE-STEP:"
STEP_EDIT = "edit"
TRANSITION_MIN_SEC = 0.2  # короче — зритель видит мигание, а не переход

_CONTEXT = """CHANNEL RULES (this channel never breaks them)
{rules}

WHAT WORKS, MEASURED ON ANALYSED CHANNELS (rules carry their own status:
"держится на N каналах" is evidence, "опровергнуто" means the rule failed
somewhere and its scope is narrower than it looks — respect both)
{blueprint}

HOUSE TECHNIQUES (the technique library, narrowed to what the renderer can do
and to this video's conditions; each says what renders it. You act on them
only through the fields you return — music_gain_db, music (cues, start_sec)
and sfx. Ducking and the bed are set by the project: treat them as facts about
the mix, never compensate for them, and never switch music on or off because
of a technique — that is the project's decision)
{library}

EPISODE
{episode}"""

PLAN = (
    STEP_MARK
    + " "
    + STEP_EDIT
    + """
You edit a finished narrated video. Every scene already has ONE approved clip,
its voice-over and a fixed duration (the "box"). You do NOT reorder scenes, do
NOT change their length, do NOT touch the script or the voice. You decide only
what happens INSIDE each box and at its boundaries.

SCENES: {scene_ids}

MATERIAL (measured, per scene: id | at | box | clip | speech | silent tail | motion | narration)
{sources}

STRUCTURE (the script's retention map: seconds where something changes for the
viewer; script time drifts from MATERIAL by a few seconds — match beats to
scenes by their narration, not by the clock)
{beats}

AVAILABLE
caption styles: {styles}
music tracks: {tracks}
sound effects (id | kind | length | what it is) — the ONLY ids `sfx` may use:
{sfx}

"""
    + _CONTEXT
    + """

Return ONLY a valid JSON object with DEVIATIONS from the default (hard cut
everywhere, captions on, no emphasis, music as configured for the project, no
effects, no sound effects, no slowed tail):
{{"caption_style": null,
  "music": null,
  "scenes": [{{"scene_id": "s07",
     "transition_in": {{"type": "dip_black", "duration_sec": 0.4}},
     "emphasis": ["word"],
     "music_gain_db": -9,
     "why": "one sentence: what this does for the viewer"}}],
  "effects": [{{"at_sec": 12.4, "duration_sec": 0.3, "kind": "zoom_punch",
     "strength": 1.0, "why": "..."}}],
  "sfx": [{{"at_sec": 12.1, "id": "<id from AVAILABLE>", "gain_db": null,
     "why": "..."}}],
  "outro": null}}
Omit scenes you leave as default. Omit fields you do not change; omit empty
lists. `music` may be {{"enabled": false}} or {{"track": "<id>"}} or
{{"gain_db": -14}} or {{"start_sec": -49, "cues": [{{"at_sec": 31.2,
"gain_db": -6, "why": "..."}}]}}. `outro` is null or {{"take_sec": 3,
"speed": 0.5, "pitch_drop": true, "zoom": 0.12, "desaturate": 0.6,
"vignette": true, "why": "..."}}.

Rules:
- Hard cut is the default and the norm. Allowed transitions: cut, crossfade,
  dip_black, fade_in. Use a non-cut transition on at most {max_transitions} of
  {scene_count} scenes, and only where STRUCTURE shows a change of scale, time
  or mood that a cut would make abrupt. Never the same non-cut transition
  {same_run} times in a row. The first scene has no transition.
- crossfade lasts at most min({crossfade_max:g} s, silent tail of the PREVIOUS
  scene): it eats silence, never speech. dip_black and fade_in:
  {transition_min:g}–{dip_max:g} s.
- emphasis: at most {emphasis_max} words per scene, copied verbatim from that
  scene's narration — only what the viewer must not miss (a number, a name,
  the turn).
- music_gain_db: level relative to the voice from this scene on, between
  {gain_lo:g} and {gain_hi:g} (−9 is close, −30 is almost silent); at most
  {max_music_steps} steps in the whole episode (closer toward the climax, back
  under the voice where the narration explains).
- `motion` in MATERIAL is measured: where the clip is quiet, steady or static.
  Put crossfades and dips where both sides are quiet, never across a peak. A
  static clip is a defect of the material — do not hide it with transitions;
  the human decides about it at the gate.
- Every deviation needs `why`. No `why` — no deviation. Do not decorate: if a
  cut works, keep the cut.

TIMELINE — effects, sound effects, music cues, outro. These live on the clock
of the FINISHED video, not inside a scene: seconds are the same as `at` in
MATERIAL (a scene's cut is its `at`; a word is `at`, plus the silence before
the voice where MATERIAL says "speech … from +N s", plus where it falls in the
narration). The default is none of them; every item needs `why`.
- effects: zoom_punch (0.3 s) and shake (0.4 s) are hits — put them ON a cut or
  ON the stressed word; a hit with no cut or word under it is decoration.
  shake at most twice per video. flash (0.15 s) once, on the strongest phrase;
  glitch (0.12 s) once, on a break of thought. desaturate and vignette are
  states, not hits: from the climax to the end. At most {max_effects} effects
  in this {total:.0f} s video ({effects_per_min:g} per minute; {effects_floor}
  are always allowed); an effect longer than {effect_max:g} s is colour
  grading, not an effect. strength 0–2, 1 = as designed.
- sfx: ids from AVAILABLE only — an id that is not listed rejects the plan. An
  impact lands frame-exact WITH a hit or a cut; a sound that builds toward a
  moment ends ON it. gain_db is relative to the voice, {sfx_lo:g}…{sfx_hi:g},
  null → the resource default; louder than the voice is a defect, not an
  accent. At most {max_sfx} sounds ({sfx_per_min:g} per minute; {sfx_floor}
  are always allowed).
- music.cues: level steps at any second, not only at scene boundaries — a drop
  lands inside a phrase. The drop is a cue UP (toward −6…−9) on the climax
  word from STRUCTURE, back under the voice where the narration explains. Only
  when music is on; cues count toward the {max_music_steps} steps. If a track
  lists "drop at N s", `music.start_sec` = climax − N puts the track's own
  drop on the climax (negative = skip that much of the track).
- outro: the slowed tail — the last take_sec seconds at speed
  {speed_lo:g}–{speed_hi:g}, take {take_lo:g}–{take_hi:g} s and never more
  than half the video. Only when the ending asks for it: the last scene holds
  a shot with no speech in its silent tail (see MATERIAL). Otherwise null. It
  is the only place the plan changes the duration.{retry}
"""
)

RETRY = """

YOUR PREVIOUS PLAN WAS REJECTED by the checker. Fix exactly these and return the
whole answer again:
{reasons}"""


def transition_quota(scene_count: int) -> int:
    """Сколько не-cut переходов можно назвать, не задев порог предупреждения
    линта (`TRANSITION_SHARE_WARN`). Хотя бы один — иначе на коротком эпизоде
    промпт запрещает то, что линт разрешает."""
    return max(1, math.floor(scene_count * TRANSITION_SHARE_WARN + 1e-9))


def timeline_quota(total_sec: float, per_min: float, floor: int) -> int:
    """Сколько эффектов (звуков) можно назвать, не задев порог предупреждения
    линта: ставка на минуту хронометража, но не меньше пола — восьмисекундному
    клипу ставка даёт ноль, а линт разрешает `floor`. Та же арифметика, что в
    `edit_lint._lint_timeline`, иначе промпт и линт разойдутся."""
    return max(floor, math.floor(per_min * max(total_sec, 1.0) / 60 + 1e-9))


def sfx_text(sounds: list[Sfx]) -> str:
    """Звуки, которые модель вправе назвать: id, вид, длина, что это. Формат
    построчный и одинаковый — по нему же заглушка LLM берёт id для своего
    ответа, так что менять его значит менять и `providers/mock.py`."""
    lines = []
    for s in sounds:
        what = s.name or s.id
        if s.description.strip():
            what += f" — {s.description.strip()}"
        lines.append(f"{s.id} | {s.kind} | {s.duration_sec:g}s | {what}")
    return "\n".join(lines) or "(none in resources/sfx/ — return no sfx)"


def sources_text(sources: Sources) -> str:
    """Материал по сценам. Секунда начала — сумма коробок: по ней модель
    сопоставляет сцены с отметками смен из STRUCTURE."""
    lines: list[str] = []
    at = 0.0
    for c in sources.clips:
        motion = MotionProfile(step_sec=c.motion_step_sec, energy=c.motion).describe()
        # заход — только где он есть: строка старого материала не меняется
        voice_from = f" from +{c.lead_in_sec:g}s" if c.lead_in_sec > 0 else ""
        lines.append(
            f"{c.scene_id} | at {at:.1f}s | box {c.box_sec:g}s | clip {c.duration_sec:g}s"
            f" | speech {c.speech_sec:g}s{voice_from} | silent tail {c.silent_tail_sec:g}s"
            f' | motion: {motion} | "{c.narration}"'
        )
        at += c.box_sec
    return "\n".join(lines) or "(no clips)"


def beats_text(retention: Retention | None) -> str:
    if retention is None or not retention.beats:
        return "(no retention map — rely on the narration alone)"
    lines = [
        f"{b.t_sec:g}s: {', '.join(b.changes) or 'change'}" + (f" — {b.note}" if b.note else "")
        for b in sorted(retention.beats, key=lambda b: b.t_sec)
    ]
    if retention.weakest:
        lines.append(f"weakest stretch (script critique): {retention.weakest}")
    return "\n".join(lines)


def plan_prompt(
    *,
    sources: Sources,
    retention: Retention | None,
    rules: str,
    blueprint: str,
    episode: str,
    styles: list[str],
    tracks: list[str],
    sfx: list[Sfx] | None = None,
    library: str = "",
    rejected: list[str] | None = None,
) -> str:
    scene_ids = ",".join(c.scene_id for c in sources.clips)
    retry = RETRY.format(reasons="\n".join(f"- {r}" for r in rejected)) if rejected else ""
    gain_lo, gain_hi = MUSIC_GAIN_RANGE_DB
    sfx_lo, sfx_hi = SFX_GAIN_RANGE_DB
    speed_lo, speed_hi = OUTRO_SPEED_RANGE
    take_lo, take_hi = OUTRO_TAKE_RANGE_SEC
    total = sum(c.box_sec for c in sources.clips)  # та же шкала, что `at` в MATERIAL
    return PLAN.format(
        scene_ids=scene_ids,
        sources=sources_text(sources),
        beats=beats_text(retention),
        styles=", ".join(styles) or "(project default only)",
        tracks=", ".join(tracks) or "(project default only)",
        sfx=sfx_text(sfx or []),
        rules=rules.strip() or "(none)",
        blueprint=blueprint.strip() or "(none)",
        library=library.strip() or "(none)",
        episode=episode.strip() or "(none)",
        scene_count=len(sources.clips),
        max_transitions=transition_quota(len(sources.clips)),
        same_run=SAME_TRANSITION_RUN_WARN,
        crossfade_max=CROSSFADE_MAX_SEC,
        transition_min=TRANSITION_MIN_SEC,
        dip_max=DIP_MAX_SEC,
        emphasis_max=EMPHASIS_MAX,
        gain_lo=gain_lo,
        gain_hi=gain_hi,
        max_music_steps=MUSIC_STEPS_WARN,
        total=total,
        max_effects=timeline_quota(total, EFFECTS_PER_MIN_WARN, EFFECTS_FLOOR),
        effects_per_min=EFFECTS_PER_MIN_WARN,
        effects_floor=EFFECTS_FLOOR,
        effect_max=EFFECT_MAX_SEC,
        max_sfx=timeline_quota(total, SFX_PER_MIN_WARN, SFX_FLOOR),
        sfx_per_min=SFX_PER_MIN_WARN,
        sfx_floor=SFX_FLOOR,
        sfx_lo=sfx_lo,
        sfx_hi=sfx_hi,
        speed_lo=speed_lo,
        speed_hi=speed_hi,
        take_lo=take_lo,
        take_hi=take_hi,
        retry=retry,
    )
