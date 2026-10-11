"""Сборка промптов и каста сцены. Одно место — чтобы стиль, персонажи, места и
предметы подмешивались одинаково в стадии картинок и видео."""

from __future__ import annotations

import re

from videoforge.core.resources import CastMember, ResourceBundle, VisualResource, load_cast
from videoforge.core.stage import StageContext
from videoforge.core.storyboard import Scene, SceneTempo
from videoforge.providers.base import Reference

SINGULAR = {"characters": "character", "props": "prop", "locations": "location"}
_ORDER = {"characters": 0, "props": 1, "locations": 2}


def scene_cast(ctx: StageContext, scene: Scene) -> list[CastMember]:
    """Кто и что в кадре — ровно то, что перечислено в `scene.cast`. Пустой список
    = в кадре никого: пейзаж, предмет, символ. Раньше пусто означало «главный
    персонаж проекта», и планнер, которому это объяснили в промпте, всё равно
    писал `[]` у пустых пейзажей и явно называл героя там, где он есть (105 сцен,
    08.09.2026) — модель читает пустое как пустое, и герой попадал в каждый
    пейзаж. Порядок в промпте: персонажи → предметы → места."""
    cast = load_cast(scene.cast, ctx.resource_dirs) if scene.cast else []
    return sorted(cast, key=lambda member: _ORDER[member[0]])


def resource_references(kind_singular: str, res: VisualResource) -> list[Reference]:
    """Портфолио одного ресурса с подписями. Имя файла — часть подписи:
    `face_front`, `body_full_back` говорят модели, что именно на кадре."""
    label = f"{kind_singular} '{res.id}'"
    return [Reference(label=f"{label} ({p.stem})", path=p) for p in res.reference_paths()]


def cast_references(cast: list[CastMember]) -> list[Reference]:
    """Портфолио каста с подписями — модель должна знать, чей референс чей."""
    refs: list[Reference] = []
    for kind, res in cast:
        refs.extend(resource_references(SINGULAR[kind], res))
    return refs


def cast_checklist(cast: list[CastMember], res: ResourceBundle) -> list[str]:
    items: list[str] = []
    for _, member in cast:
        items.extend(member.checklist)
    if res.style:
        items.extend(res.style.checklist)
    return items


def join_parts(parts: list[str]) -> str:
    # хвостовые точки и запятые снимаются: префикс стиля в YAML кончается
    # запятой, и без этого в промпте выходило «mood,. Warm living room»
    cleaned = [p.strip().rstrip(".,").rstrip() for p in parts if p and p.strip()]
    return ". ".join(cleaned) + "." if cleaned else ""


def image_prompt(
    scene: Scene,
    res: ResourceBundle,
    cast: list[CastMember],
    *,
    has_references: bool,
    lora_mode: bool = False,
    fixes: list[str] | None = None,
) -> str:
    """`lora_mode` — художник держит лицо LoRA (ComfyUI на своей карте): у
    персонажа с LoRA в промпт идёт триггер вместо паспорта, референсы и «Avoid:»
    не нужны — диффузионная модель без негатива читает их как позитив. Паспорт в
    промпте при LoRA превращал «со спины» в анфас (замер 08.09.2026).

    `fixes` — фразы из вердикта проверки по описанию (`Scene.fixes`): что в
    кадре должно быть иначе, описанием, не инструкцией — «замени / убери»
    klein читает как существительное и теряет раскладку (test8b, 14.09.2026).
    Идут сразу после описания сцены, до суффикса стиля."""
    parts: list[str] = []
    triggers: list[str] = []
    if lora_mode:
        for _, member in cast:
            lora = getattr(member, "lora", None)
            if lora and lora.trigger:
                triggers.append(lora.trigger)
    if triggers:
        parts.append(", ".join(triggers))
    if res.style:
        parts.append(res.style.prompt_prefix)
    for _, member in cast:
        lora = getattr(member, "lora", None)
        if lora_mode and lora and lora.trigger:
            continue
        parts.append(member.prompt_block)
    if has_references and not lora_mode:
        parts.append(
            "Every referenced character must be the exact same person as in their "
            "reference images (face, hair, body); wardrobe follows the scene "
            "description. Props and locations must match their reference images exactly"
        )
    parts.append(scene.visual_prompt)
    parts.extend(fixes or [])
    if res.style:
        parts.append(res.style.prompt_suffix)
    prompt = join_parts(parts)
    if res.style and res.style.negative.strip() and not lora_mode:
        prompt += f" Avoid: {res.style.negative.strip()}."
    return prompt


# Стабильность формулируется позитивно: отрицания в хвосте («no morphing»)
# i2v-модели читают плохо — замерено 31.08.2026, вольный промпт дорисовывал
# в кадр выдуманные элементы, структурный с этим блоком — нет.
MOTION_CONSTRAINTS = (
    "Constraints: everything must remain visually unchanged from the input "
    "image — same subjects, same layout, same colors, crisp edges held. The "
    "camera makes one single continuous move. No new elements appear, no "
    "morphing, no shape deformation, no cuts, no on-screen text."
)


# Темп. Замер 09.09.2026 (projects/_bench/test7_realtime.py, эпизод
# tests/t02_cat_realtime; один сид, один кадр, LTX-2.5, 8 с): промпт с ОДНИМ
# событием («замахнулся и ударил») модель растягивает на всю длину клипа, и
# выходит слоумо — медиана энергии движения 8.8, тихих окон 24%. Тот же кадр с
# непрерывной серией действий и словами про темп — 14.5 и 6%, на клипе 5 с —
# 0% (эталон, чужой рилс: медиана 6.2, тихих 0%). Заниженное условие темпа
# (frame_rate 16 вместо 24) помогало слабее — 12.2, значит главный рычаг —
# текст, и просить непрерывное действие должна сама стадия, а не планнер по
# настроению. Формулировка — из победившего промпта B: «one after another
# without pause» и фраза про темп оттуда дословно.
TEMPO_ACTION = (
    "Tempo: continuous real-time action for the whole clip — the action above "
    "keeps going one movement after another without pause, from the first frame "
    "to the last. Real-time speed, normal playback speed, fast sharp movements, "
    "no slow motion."
)

# Слова покоя: сцена — выдох, пейзаж, статичный предмет. Ищутся как начала
# слов, чтобы «slowly pushes» читалось как «slow push», а «breathing» — как
# «breathe»; поэтому стемы («silen», «gentl»), а не слова
CALM_WORDS = (
    "still",
    "static",
    "calm",
    "slow",
    "drift",
    "breath",
    "landscape",
    "empty",
    "silen",
    "gentl",
    "quiet",
    "motionless",
)
# Слова действия: одно такое — и сцена не выдох, что бы ни стояло рядом
# («no slow motion» в промпте про драку содержит slow). «spin» намеренно нет:
# начало слова «spine», а корешок книги в кадре — не действие
ACTION_WORDS = (
    "run",
    "jump",
    "hit",
    "punch",
    "throw",
    "fight",
    "fall",
    "dance",
    "chase",
    "explo",
    "kick",
    "strike",
    "sprint",
    "leap",
    "smash",
    "crash",
    "flurry",
    "slam",
    "swing",
    "grab",
    "rush",
    "dash",
    "whip",
    "burst",
    "attack",
    "dodge",
    "race",
    "flee",
    "charge",
    "gallop",
)
_CALM_RE = re.compile(r"\b(?:" + "|".join(CALM_WORDS) + r")\w*")
_ACTION_RE = re.compile(r"\b(?:" + "|".join(ACTION_WORDS) + r")\w*")


def scene_tempo(scene: Scene) -> SceneTempo:
    """Темп сцены: явное поле `tempo` побеждает, иначе — по словам промптов.

    Сцена спокойная, только если в motion_prompt или visual_prompt есть слово
    покоя и нет ни одного слова действия. Сомнение — action: по замеру вялость
    это дефект по умолчанию, а лишний темп на спокойной сцене человек выключает
    одним полем `tempo: calm`."""
    if scene.tempo is not None:
        return scene.tempo
    text = f"{scene.motion_prompt} {scene.visual_prompt}".lower()
    if _ACTION_RE.search(text):
        return "action"
    return "calm" if _CALM_RE.search(text) else "action"


def motion_prompt(scene: Scene, res: ResourceBundle) -> str:
    """i2v-промпт структурой Subject lock → Action → [Tempo] → Camera → Constraints
    (по docs/reference/arcads-prompting, подтверждено живыми тестами):
    модель должна знать, что в кадре уже есть, что из этого движется и как
    ведёт себя камера — тремя раздельными блоками, а не одной фразой. Блок
    темпа — только у сцен с действием (`scene_tempo`): выдох и пейзаж от
    «fast sharp movements» задёргаются."""
    parts = [
        "Image-to-video animation of the scene in the input image.",
        f"Subject: {scene.visual_prompt.strip().rstrip('.')} — exactly as in the input image.",
        f"Action: {scene.motion_prompt.strip().rstrip('.')}.",
    ]
    if scene_tempo(scene) == "action":
        parts.append(TEMPO_ACTION)
    if res.style and res.style.motion_hint.strip():
        parts.append(f"Camera: {res.style.motion_hint.strip().rstrip('.')}.")
    parts.append(MOTION_CONSTRAINTS)
    return "\n\n".join(parts)
