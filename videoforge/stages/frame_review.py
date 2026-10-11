"""Проверка кадров ПО ОПИСАНИЮ пачкой — сторона стадии
(`features/scene_reference_image`, замер `_bench/test9`).

Стадия картинок после каждого круга генерации отдаёт готовые кадры llm по
двадцать одним вызовом (`LlmProvider.review_frames`): показывает ли кадр то,
что просила сцена. Вердикт двух ступеней: `block` — кадр не про то, идёт на
повтор с фразой `fix` в описании и, если нашёлся, с референсом композиции;
`note` — детали расходятся, человеку на ворота. Это не сверка с референсами
каста (`check_images` у контролёра) — та про «тот ли человек», эта про «то ли
нарисовано».
"""

from __future__ import annotations

from videoforge.core.stage import StageContext
from videoforge.core.storyboard import FrameReview, Scene
from videoforge.providers.base import FrameToReview, FrameVerdict
from videoforge.providers.review import BATCH


def review_scenes(ctx: StageContext, scenes: list[Scene]) -> dict[str, FrameVerdict]:
    """Кадры сцен → вердикты по id. Сцены без кадра пропускаются; о ком модель
    не сказала — того в ответе нет (стадия читает это как «не проверен»)."""
    llm = ctx.providers.llm
    rules = tuple(ctx.project.video.frame_rules) if ctx.project else ()
    frames = [
        FrameToReview(
            id=s.id,
            path=ctx.abs(s.assets.image),
            description=s.visual_prompt,
            narration=s.narration,
            rules=rules,
        )
        for s in scenes
        if s.assets.image and ctx.abs(s.assets.image).exists()
    ]
    verdicts: dict[str, FrameVerdict] = {}
    for start in range(0, len(frames), BATCH):
        batch = frames[start : start + BATCH]
        ctx.afford(llm.check_price_usd * len(batch), f"проверка кадров по описанию ×{len(batch)}")
        with ctx.step(f"проверка по описанию ×{len(batch)}"):
            result = llm.review_frames(batch)
        ctx.add_cost(result.cost_usd, llm.name, None, f"frame review ×{len(batch)}")
        for v in result.verdicts:
            verdicts[v.id] = v
    return verdicts


def apply_verdict(scene: Scene, verdict: FrameVerdict, model: str) -> None:
    """Вердикт → запись у сцены. Блок переводит кадр в `fail` с причиной в
    заметках (как непройденная сверка каста); заметка статус не трогает."""
    scene.review["image"] = FrameReview(
        severity=verdict.severity,
        see=verdict.see,
        issue=verdict.issue,
        fix=verdict.fix,
        search=verdict.search,
        model=model,
    )
    if verdict.severity == "block":
        scene.checks.image = "fail"
        note = verdict.issue or "кадр не совпал с описанием"
        scene.checks.notes["image"] = f"не про то: {note}"
    elif verdict.severity == "note" and verdict.issue:
        # деталь, не брак: статус остаётся, замечание — человеку
        scene.checks.notes["image"] = f"деталь: {verdict.issue}"
