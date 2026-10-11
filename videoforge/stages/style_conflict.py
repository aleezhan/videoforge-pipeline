"""Конфликт промпта сцены со стилем — до генерации, а не после проверки кадра.

Зачем. В прогоне ep002 (doc_channel, 09.09.2026) проверяющая модель забраковала
восемь кадров подряд: промпты были написаны в фотореализме при стиле «мягкая
2D-иллюстрация». Сцена «открытый Коран» трижды не прошла по правилу «нет
текста в кадре», монеты — на них чеканят надписи. Каждый провал — платный
кадр плюс платная проверка, а всё это видно в тексте промпта до единого
вызова. Опечатка в касте уже падает до денег (`ImagesStage._resolve_casts`);
здесь то же самое для «промпт против стиля».

Два стража и один совет:
- `find_conflicts` — детерминированная проверка по словам. Источник запретов —
  `negative` стиля (термины через запятую) через карту синонимов ниже, плюс
  явный список `forbid` стиля (фразы, как написаны). Блок: планнер получает
  причины и отвечает второй раз, стадия картинок отказывается генерировать.
- `judge_conflicts` — спорное отдаётся модели одним вызовом на эпизод и ТОЛЬКО
  когда по словам чисто. Её вердикт — совет, не ворота: он плывёт между
  прогонами (проверено на QA первого шортса, приватные дизайн-заметки),
  поэтому уходит в
  сообщение стадии и `checks.notes["style"]`, а не в блок.

Сравнение по словам целиком, не подстрокой: «script» не должен ловиться в
«description», «text» — в «context». Ловим только очевидное: ложный блок
стоит повтора планнера или упавшей стадии, пропуск — одного кадра, который
заметит проверяющая модель.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from videoforge.core.resources import Style
from videoforge.core.stage import StageContext
from videoforge.core.storyboard import Scene
from videoforge.providers.base import ProviderError
from videoforge.stages.llm_json import strip_fences

Severity = Literal["block", "warn"]

STEP_MARK = "VIDEOFORGE-STEP: style_conflict"
SYSTEM = "You are an art director checking frame descriptions against a fixed visual style."
MODEL_RULE = "model"  # `Conflict.rule` у мнения модели: правила по словам у него нет
FIELDS: tuple[str, ...] = ("visual_prompt", "motion_prompt")


@dataclass(frozen=True)
class Conflict:
    scene_id: str
    term: str  # что нашли в промпте («inscription»); у мнения модели — пусто
    rule: str  # термин стиля, который нарушен («text»); у мнения модели — MODEL_RULE
    severity: Severity = "block"
    why: str = ""  # объяснение модели; у проверки по словам — пусто


# Группы слов, которыми промпт сцены нарушает запрет стиля. Ключ карты ниже —
# термин из `negative` стиля, как его пишут в YAML («text», «3D render»,
# «live action photo»); значение — фразы, которые в промпте сцены значат то же.
#
# Почему карта, а не сам `negative`: негатив написан для диффузионной модели и
# держит фразы вроде «added objects the scene does not mention (planets,
# people)» — искать их словами в промпте значило бы блокировать каждую сцену с
# людьми. Термин, которого в карте нет, по словам не проверяется вовсе:
# неочевидное лучше не ловить, чем ловить ложно (спорное судит модель).
#
# Что намеренно НЕ ловим:
# - «sign» голое: «signs of age», «a sign of respect» — не текст; берём только
#   вывески;
# - «letter» в единственном: письмо на столе — предмет, а не надпись;
# - «film still», «cinematic still»: pixar3d сам называет кадр «3D animated
#   film still», у анимации тоже есть стоп-кадры;
# - «blender», «unreal», «octane» голые: кухонный блендер и «unreal glow»;
# - «painting» голое: картина на стене — предмет сцены;
# - «blurry», «low quality», «extra fingers»: так никто не описывает кадр.
_TEXT: tuple[str, ...] = (
    "text",
    "texts",
    "letters",
    "lettering",
    "writing",
    "writings",
    "written",
    "handwriting",
    "handwritten",
    "inscription",
    "inscriptions",
    "inscribed",
    "engraved",
    "engraving",
    "engravings",
    "calligraphy",
    "calligraphic",
    "script",
    "scripts",
    "caption",
    "captions",
    "subtitle",
    "subtitles",
    "label",
    "labels",
    "labeled",
    "labelled",
    "numbers",
    "digits",
    "numerals",
    "typography",
    "words",
    "signage",
    "signboard",
    "neon sign",
    "street sign",
    "road sign",
    "shop sign",
)
_PHOTO: tuple[str, ...] = (
    "photorealistic",
    "photo-realistic",
    "photorealism",
    "photoreal",
    "photograph",
    "photographs",
    "photographed",
    "photography",
    "photographic",
    "photo of",
    "realistic photo",
    "raw photo",
    "stock photo",
    "dslr",
    "35mm",
    "50mm",
    "85mm",
    "live action",
    "live-action",
    "hyperrealistic",
    "hyper-realistic",
    "ultrarealistic",
    "ultra-realistic",
)
_RENDER3D: tuple[str, ...] = (
    "3d",
    "3d render",
    "3d rendered",
    "3d rendering",
    "octane render",
    "unreal engine",
    "blender render",
    "rendered in blender",
    "cinema 4d",
    "c4d",
    "cgi",
    "ray traced",
    "raytraced",
    "ray tracing",
    "raytracing",
    "vray",
    "v-ray",
)
_PAINT: tuple[str, ...] = (
    "painterly",
    "oil painting",
    "oil paint",
    "oil on canvas",
    "watercolor",
    "watercolour",
    "watercolors",
    "watercolours",
    "brush strokes",
    "brushstrokes",
    "brush stroke",
    "brushstroke",
    "impasto",
    "gouache",
    "acrylic painting",
)
_WATERMARK: tuple[str, ...] = ("watermark", "watermarks", "watermarked")
_LOGO: tuple[str, ...] = ("logo", "logos", "brand logo")

SYNONYMS: dict[str, tuple[str, ...]] = {
    "text": _TEXT,
    "letters": _TEXT,
    "lettering": _TEXT,
    "writing": _TEXT,
    "numbers": _TEXT,
    "digits": _TEXT,
    "captions": _TEXT,
    "typography": _TEXT,
    "words": _TEXT,
    "photorealistic": _PHOTO,
    "photo-realistic": _PHOTO,
    "photorealism": _PHOTO,
    "photoreal": _PHOTO,
    "photograph": _PHOTO,
    "photography": _PHOTO,
    "photo": _PHOTO,
    "realistic photo": _PHOTO,
    "real photo": _PHOTO,
    "live action": _PHOTO,
    "live-action": _PHOTO,
    "live action photo": _PHOTO,
    "live action footage": _PHOTO,
    "hyperrealistic": _PHOTO,
    "hyper-realistic": _PHOTO,
    "3d": _RENDER3D,
    "3d render": _RENDER3D,
    "3d rendering": _RENDER3D,
    "3d cgi": _RENDER3D,
    "cgi": _RENDER3D,
    "octane render": _RENDER3D,
    "unreal engine": _RENDER3D,
    "painterly": _PAINT,
    "painterly brush texture": _PAINT,
    "painterly texture": _PAINT,
    "oil painting": _PAINT,
    "watercolor": _PAINT,
    "watercolour": _PAINT,
    "brush strokes": _PAINT,
    "brush texture": _PAINT,
    "watermark": _WATERMARK,
    "logo": _LOGO,
}

# Слова перед термином, которые делают упоминание согласием со стилем, а не
# нарушением: планнеру велено выносить текст на плашку, и «an open book with
# no readable text» — исполнение правила, а не конфликт. Окно в три слова:
# «without any visible text». Пропуск такого промпта стоит кадра, ложный блок
# — повтора планнера
_NEGATION = frozenset({"no", "without", "not", "never", "nor"})
_NEGATION_WINDOW = 3
_WORD = re.compile(r"[a-z0-9]+")


def _norm(term: str) -> str:
    """Термин стиля в форму ключа карты: регистр, дефис, лишние пробелы."""
    return " ".join(_WORD.findall(term.lower().replace("-", " ")))


# ключи карты в той же форме, что термины стиля после `_norm`: «3D render» и
# «photo-realistic» в YAML должны находить свою группу
_BY_KEY: dict[str, tuple[str, ...]] = {_norm(k): v for k, v in SYNONYMS.items()}


def _pattern(phrase: str) -> re.Pattern[str]:
    """Фраза → регулярное выражение по целым словам: между словами фразы —
    пробелы или дефис, по краям — не буква и не цифра. Регистр не важен."""
    words = _WORD.findall(phrase.lower().replace("-", " "))
    body = r"[\s\-]+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.IGNORECASE)


def _negated(text: str, start: int, end: int) -> bool:
    before = _WORD.findall(text[:start].lower())[-_NEGATION_WINDOW:]
    after = _WORD.findall(text[end:].lower())[:1]
    return any(w in _NEGATION for w in before) or after == ["free"]


def style_rules(style: Style) -> list[tuple[str, tuple[str, ...]]]:
    """Правила стиля → (термин правила, фразы-нарушители). Порядок — как в
    стиле: `negative` через карту синонимов (неизвестный термин пропускается),
    потом `forbid` как написано (это явный заказ человека, синонимы у него
    только если термин есть в карте)."""
    rules: list[tuple[str, tuple[str, ...]]] = []
    for raw in style.negative.split(","):
        term = raw.strip()
        if not term:
            continue
        phrases = _BY_KEY.get(_norm(term))
        if phrases:
            rules.append((term, phrases))
    for raw in style.forbid:
        term = raw.strip()
        if not term:
            continue
        rules.append((term, _BY_KEY.get(_norm(term)) or (term,)))
    return rules


def find_conflicts(scenes: list[Scene], style: Style | None) -> list[Conflict]:
    """Слова промпта против правил стиля — детерминированно, $0.

    Проверяются `visual_prompt` и `motion_prompt`: движение «camera pans over
    the engraved letters» рисует текст так же, как описание кадра. Находки
    идут в порядке текста; на одном месте побеждает самая длинная фраза
    («3D render» — один конфликт, а не «3d» плюс «3d render»), и одно слово
    сцены не повторяется по правилам: «inscription» при негативе «text,
    letters, numbers» — один конфликт, а не три."""
    if style is None:
        return []
    rules = [(rule, [_pattern(p) for p in phrases]) for rule, phrases in style_rules(style)]
    if not rules:
        return []
    out: list[Conflict] = []
    for scene in scenes:
        seen: set[str] = set()
        for name in FIELDS:
            text = str(getattr(scene, name, "") or "")
            if not text.strip():
                continue
            hits: list[tuple[int, int, str, str]] = []  # start, end, как в промпте, правило
            for rule, patterns in rules:
                for pattern in patterns:
                    for m in pattern.finditer(text):
                        if not _negated(text, m.start(), m.end()):
                            hits.append((m.start(), m.end(), m.group(0), rule))
            # по месту в тексте, на одном месте — длиннее первой; сортировка
            # устойчива, поэтому при равной длине побеждает правило, которое в
            # стиле стоит раньше
            hits.sort(key=lambda h: (h[0], h[0] - h[1]))
            taken_end = -1
            for start, end, term, rule in hits:
                if start < taken_end or term.lower() in seen:
                    continue
                taken_end = end
                seen.add(term.lower())
                out.append(Conflict(scene_id=scene.id, term=term, rule=rule))
    return out


def describe(conflicts: list[Conflict], limit: int = 12) -> str:
    """Перечисление для человека: «s05: 'inscription' ↔ 'text'; …»."""
    rows = []
    for c in conflicts[:limit]:
        if c.rule == MODEL_RULE:
            rows.append(f"{c.scene_id}: по мнению модели — {c.why}")
        else:
            rows.append(f"{c.scene_id}: '{c.term}' ↔ '{c.rule}'")
    more = f" (+{len(conflicts) - limit})" if len(conflicts) > limit else ""
    return "; ".join(rows) + more


def retry_reason(conflicts: list[Conflict], style: Style | None = None, limit: int = 20) -> str:
    """Причины для повтора планнера — по-английски, полным списком (в текст
    ошибки parse_scenes он бы не влез: там 200 знаков). Со стилем — и его
    запреты целиком: модель до этого стиля не видела, ей не из чего понять,
    чего ещё не писать."""
    rows = [
        f"{c.scene_id}: '{c.term}' violates the style rule '{c.rule}'" for c in conflicts[:limit]
    ]
    more = f" (+{len(conflicts) - limit})" if len(conflicts) > limit else ""
    avoid = ""
    if style is not None:
        forbidden = [t for t in (style.negative.strip(), "; ".join(style.forbid)) if t]
        if forbidden:
            avoid = f" The style forbids: {', '.join(forbidden)}."
    return (
        f"{len(conflicts)} prompts contradict the fixed visual style of this project. "
        "The style prefix is added to every frame automatically, so never name another "
        "rendering technique; anything the viewer must read goes to `overlay`, never "
        f"into the picture.{avoid} Describe the same frames in words that fit the style. "
        + "; ".join(rows)
        + more
    )


_JUDGE = (
    STEP_MARK
    + """
Below are the rules of the visual style every frame of this video is rendered
in, and the frame descriptions (prompts) of its scenes. Find the scenes whose
prompt CONTRADICTS the style: it asks for a rendering technique the style
forbids (photographic realism in a flat 2D style, a 3D render in a hand-drawn
one), or it puts into the picture something the style's avoid-list or
checklist rules out (readable text, letters or numbers inside the frame,
logos, the listed forbidden subjects). Judge the words of the prompt as
written; do not guess what the image model might add on its own.

STYLE
prefix: {prefix}
avoid: {negative}
checklist (what the reviewer verifies on every frame):
{checklist}

SCENES (id | prompt)
{scenes}

Return ONLY a valid JSON object, no fences, no commentary:
{{"conflicts": [{{"scene_id": "s05", "why": "one sentence: which words break which rule"}}]}}

Rules:
- List a scene only when the contradiction is in the words of its prompt. A
  prompt that merely does not mention the style is not a conflict: the style
  prefix is added to every prompt automatically.
- An empty list is a valid and common answer.
- Write `why` in {language}."""
)


def _scenes_text(scenes: list[Scene]) -> str:
    lines = []
    for s in scenes:
        prompt = s.visual_prompt.strip()
        if s.motion_prompt.strip():
            prompt += f" | motion: {s.motion_prompt.strip()}"
        lines.append(f"{s.id} | {prompt}")
    return "\n".join(lines)


def _parse_judgement(raw: Any, ids: list[str]) -> list[Conflict]:
    data = raw.get("conflicts") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        raise ValueError("ответ модели без списка conflicts")
    wanted = set(ids)
    out: list[Conflict] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("scene_id") or item.get("id") or "")
        if sid not in wanted or sid in seen:
            continue
        seen.add(sid)
        why = str(item.get("why") or "").strip()[:300] or "модель не назвала причину"
        out.append(Conflict(scene_id=sid, term="", rule=MODEL_RULE, severity="warn", why=why))
    return out


def judge_conflicts(ctx: StageContext, scenes: list[Scene], style: Style | None) -> list[Conflict]:
    """Спорное — модели, одним вызовом на эпизод. Совет, не ворота: упавший
    вызов или ответ не по форме — предупреждение в лог и пустой список.

    Берёт контекст, а не провайдера: вызов на платном провайдере стоит денег,
    и записать их в манифест умеет только `ctx.add_cost` (как у `ask_json`)."""
    if style is None or not scenes:
        return []
    llm = ctx.providers.llm
    prompt = _JUDGE.format(
        prefix=style.prompt_prefix.strip() or "—",
        negative=style.negative.strip() or "—",
        checklist="\n".join(f"- {c}" for c in style.checklist) or "—",
        scenes=_scenes_text(scenes),
        language=ctx.storyboard.language,
    )
    try:
        result = llm.complete(prompt, system=SYSTEM, json_mode=True)
    except ProviderError as exc:
        ctx.log.warning("мнение модели о стиле не получено: %s", exc)
        return []
    ctx.add_cost(result.cost_usd, llm.name)
    try:
        return _parse_judgement(json.loads(strip_fences(result.text)), [s.id for s in scenes])
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        ctx.log.warning("мнение модели о стиле не разобрано: %s", exc)
        return []
