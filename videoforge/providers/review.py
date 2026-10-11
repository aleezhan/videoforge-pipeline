"""Проверка кадров ПАЧКОЙ по описанию: промпт и разбор ответа, общие для
провайдеров (`LlmProvider.review_frames`).

Один вызов на десятки кадров вместо вызова на кадр — время уходит на чтение
каждого файла (≈7 с у claude_cli независимо от размера пачки и модели), а
запуск процесса платится один раз: 105 кадров за ≈12 минут вместо ≈28
(замер 14.09.2026, `projects/_bench/test9`). Модель отвечает не битом, а
диагнозом: что видно, что не так, как чинить и какой запрос дать поиску
референса — вторая попытка идёт с картинкой, а не со слепым перекатом сида.

Вердикт двухступенчатый. `block` — кадр не про то (предмет не тот или не
узнаётся, композиция противоречит описанию, лишние люди или предметы меняют
смысл, псевдотекст на священной странице, заметные артефакты) — переделывать.
`note` — расхождение в деталях: человеку на ворота, перегенерации нет. Без
этого opus браковал половину эпизода (10 из 20, 19 из 40 в test9), и большая
часть — детали вроде размера тени, которые слепой повтор не чинит.
"""

from __future__ import annotations

import json
import re

from videoforge.providers.base import FrameToReview, FrameVerdict, ProviderError

BATCH = 20  # кадров на вызов: пачка 40 не дешевле 20 (7.0 против 6.6 с/кадр), test9

REVIEW_INSTRUCTION = """You are a strict quality-control reviewer for frames of a narrated video.
Each frame must show what its scene description says, so that a viewer understands the
subject within one second without sound. Below is a list of scenes; each has an image
file path, the visual description the artist was given, and the narration line.

Open and look at EVERY image file listed. For each scene answer:
- "match": true if the frame shows the described subject AND composition; false otherwise.
- "severity": for match=false — "block" if the subject is missing or unrecognizable, the
  composition contradicts the description (e.g. "an endless row receding into haze" drawn
  as a heap), extra people or objects change the meaning, there is pseudo-text on a
  sacred page, garbled text or visible artifacts; "note" if the subject and composition
  are right and only details differ (lighting color, size of a shadow, a count that
  does not change the meaning). For match=true — "ok".
- "see": what is actually in the frame, 5-12 words.
- "issue": for match=false — what contradicts the description, one sentence; else "".
- "fix": for match=false — the shortest change to the DESCRIPTION that would fix it, one
  sentence written as a description of the wanted frame (not an instruction); else "".
- "search": for severity="block" — a 3-6 word English image-search query describing the
  COMPOSITION only (arrangement, perspective, framing) that a reference photo should
  have, e.g. "row of identical statues receding into distance"; NOT the subject of the
  scene. Else "".

Return ONLY JSON: {"scenes": [{"id": "...", "match": true, "severity": "ok", "see": "...",
"issue": "", "fix": "", "search": ""}, ...]} — one entry per scene, same ids, same order."""

SEVERITIES = ("ok", "note", "block")


# Правила, которые кадр нарушает независимо от описания. Сюда — только то, что
# брак везде; правила канала (что можно рисовать в его теме) идут из проекта
DEFECTS = (
    'Always severity "block", even if the frame matches its description: broken anatomy '
    "(extra, missing or fused fingers, hands or limbs; twisted or melted faces; two heads; "
    "limbs passing through objects), duplicated figures that should be one, objects "
    "merging into each other, visible watermarks or UI, any garbled or pseudo text."
)


def review_prompt(frames: list[FrameToReview]) -> str:
    lines = [
        f"- id: {f.id}\n  file: {f.path.resolve()}\n  description: {f.description}"
        + (f"\n  narration: {f.narration}" if f.narration else "")
        for f in frames
    ]
    rules = sorted({r for f in frames for r in f.rules})
    extra = "\n\n" + DEFECTS
    if rules:
        extra += (
            '\n\nChannel rules — a frame that breaks ANY of them is severity "block" and '
            "the issue must name the rule:\n" + "\n".join(f"- {r}" for r in rules)
        )
    return REVIEW_INSTRUCTION + extra + "\n\nScenes:\n" + "\n".join(lines)


def _loads(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\n?", "", t).rstrip("`").strip()
    try:
        data = json.loads(t)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"проверка кадров: ответ не JSON ({exc}): {t[:200]!r}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
        raise ProviderError(f"проверка кадров: в ответе нет списка scenes: {t[:200]!r}")
    return data


def parse_review(text: str, frames: list[FrameToReview]) -> list[FrameVerdict]:
    """Ответ модели → вердикты в порядке `frames`. Сцены, о которых модель не
    сказала, в списке отсутствуют — стадия считает их непроверенными, а не
    прошедшими. Чужие id пропускаются."""
    data = _loads(text)
    known = {f.id for f in frames}
    by_id: dict[str, FrameVerdict] = {}
    for row in data["scenes"]:
        if not isinstance(row, dict) or str(row.get("id", "")) not in known:
            continue
        match = bool(row.get("match"))
        severity = str(row.get("severity") or "").strip().lower()
        if match:
            severity = "ok"
        elif severity not in SEVERITIES or severity == "ok":
            # модель сказала «не совпал», но не сказала насколько: это её
            # вердикт о несовпадении, и он весит как блок
            severity = "block"
        by_id[str(row["id"])] = FrameVerdict(
            id=str(row["id"]),
            match=match,
            severity=severity,
            see=str(row.get("see") or "").strip(),
            issue=str(row.get("issue") or "").strip(),
            fix=str(row.get("fix") or "").strip(),
            search=str(row.get("search") or "").strip(),
        )
    return [by_id[f.id] for f in frames if f.id in by_id]
