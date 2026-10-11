"""Стадия formula: всё собранное → формула контента → 05_formula/.

Кросс-анализ одним вызовом LLM: статистика и выбросы + таксономия форматов с
просмотрами + паттерны заголовков + глубокие разборы топ-видео → что общего у
успешных роликов, обобщённая структура, правила хука, формулы заголовков,
советы по форматам и несколько идей по формуле (не копий). Рядом с
formula.json пишется человекочитаемый blueprint.md — производственный бриф,
который читают /new-project и будущий планнер.

Выборка маленькая (обычно 20–200 видео, 3–5 разборов) — промпт требует
формулировок «N из M», а не процентов, и честного списка ограничений.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from videoforge.core.io import read_model, write_model
from videoforge.intel.models import (
    AnalysisConfig,
    ChannelData,
    ClassifyReport,
    DeepReport,
    DeepVideoAnalysis,
    Formula,
    StatsReport,
    VideoStats,
    VisualReport,
    VisualVideoAnalysis,
)
from videoforge.intel.stage import IntelContext, IntelStage, StageResult
from videoforge.intel.stages.classify import _ask_json, classify_path
from videoforge.intel.stages.collect import channel_path
from videoforge.intel.stages.deep import ANALYSIS_FILE, DEEP_DIR, deep_index_path
from videoforge.intel.stages.stats import STATS_DIR, STATS_FILE
from videoforge.intel.stages.visual import VISUAL_DIR, visual_index_path
from videoforge.paths import BLUEPRINT_FILE, FORMULA_DIR

FORMULA_FILE = "formula.json"

_FORMULA_PROMPT = """Derive the CONTENT FORMULA of this YouTube channel.

Below is everything the analysis pipeline extracted: channel stats, the format
taxonomy with per-format performance, outlier videos (views vs the channel's
rolling median), per-video title patterns, and deep structural analyses of the
top videos. Find what the successful videos share and turn it into a formula a
production team can follow.

CHANNEL
{channel}

FORMATS (performance per format)
{formats}

OUTLIERS (unusually successful videos)
{outliers}

TITLE PATTERNS (per video; outliers marked)
{title_patterns}

DEEP ANALYSES (role "hit" = channel's best, role "control" = a weak video of the
same channel, old enough to have collected its views; each carries its own views
and ratio vs neighbours, so roles can be checked against numbers)
{deep}

VISUAL ANALYSES OF THE SAME VIDEOS (measured editing rhythm + what is on screen)
{visual}

Return ONLY valid JSON with exactly this shape (no fences, no comments):
{{
  "what_works": ["5-10 observations about what actually separates the hits"],
  "content_formula": [{{"step": "beat name", "purpose": "why it is there"}}],
  "title_formulas": [{{"pattern": "title structure", "example": "a NEW example"}}],
  "hook_rules": ["3-6 rules for the first seconds"],
  "format_advice": [{{"format": "format id or name",
    "verdict": "делать | масштабировать | избегать", "why": "one sentence"}}],
  "shot_recipe": {{"scene_duration_sec": 0.0, "shot_range_sec": "min-max",
    "cuts_per_min": 0.0, "rhythm": "how the pace changes across the runtime",
    "camera_moves": ["moves worth reproducing, most useful first"],
    "staging": ["how to put numbers and scale comparisons on screen"]}},
  "production_notes": "length, pace, narration, cadence — one paragraph",
  "video_ideas": [{{"title": "...", "format": "...", "hook": "...",
    "outline": "3-5 beats"}}],
  "hit_vs_control": ["what the hits do that the control videos do not — or an
    honest statement that no difference is visible in this data"],
  "caveats": ["honest limits of this analysis"]
}}

Rules: base every claim on the data above — cite counts as "N of M", never
made-up percentages. Where control videos are present, a technique counts as a
cause of success only if the hits have it and the controls do not; a technique
present in both is the channel's style, not its engine — say so plainly in
hit_vs_control instead of dressing it up as a finding. With no controls, leave
hit_vs_control empty. content_formula generalizes the deep structures, it is
not a copy of one video. video_ideas (exactly 5) must follow the formula
without copying existing videos. shot_recipe must come from the MEASURED
numbers in the visual analyses, not from impressions — scene_duration_sec is
the length one generated clip should be, and it must exclude anything that
depends on the channel's own art style; if there are no visual analyses, leave
shot_recipe empty. Write all values in the language "{language}"
(verdicts too)."""


def formula_path(analysis_dir: Path) -> Path:
    return analysis_dir / FORMULA_DIR / FORMULA_FILE


def blueprint_path(analysis_dir: Path) -> Path:
    return analysis_dir / FORMULA_DIR / BLUEPRINT_FILE


def _deep_payload(
    analyses: list[DeepVideoAnalysis], titles: dict[str, str], rows: dict[str, VideoStats]
) -> list[dict]:
    """Разборы + их результат в цифрах.

    Без просмотров и ratio роли «hit»/«control» — просто слова: кросс-анализ
    видит цифры только по выбросам, а хит, не дотянувший до порога выброса,
    выглядит утверждением без основания (и модель честно жалуется на это в
    ограничениях)."""
    return [
        {
            "title": titles.get(a.video_id, a.video_id),
            "role": a.role,
            "views": rows[a.video_id].views if a.video_id in rows else None,
            "ratio_vs_neighbors": rows[a.video_id].ratio if a.video_id in rows else None,
            "hook": a.hook.model_dump(),
            "structure": [{"t": b.t, "beat": b.beat, "purpose": b.purpose} for b in a.structure],
            "narration_style": a.narration_style,
            "curiosity_gaps": a.curiosity_gaps,
            "takeaways": a.takeaways,
        }
        for a in analyses
    ]


def _visual_payload(analyses: list[VisualVideoAnalysis], titles: dict[str, str]) -> list[dict]:
    return [
        {
            "title": titles.get(a.video_id, a.video_id),
            "role": a.role,
            "shots": a.shots.model_dump(),
            "shot_language": a.shot_language,
            "camera_moves": a.camera_moves,
            "staging_patterns": a.staging_patterns,
            "text_on_screen": a.text_on_screen,
            "palette": a.palette,
            "thumbnail": a.thumbnail.model_dump(),
            "transferable": a.transferable,
        }
        for a in analyses
    ]


def render_blueprint(formula: Formula, config: AnalysisConfig, channel_title: str) -> str:
    """formula.json → blueprint.md: производственный бриф для людей и /new-project."""

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) or "—"

    steps = "\n".join(
        f"{i}. **{s.step}** — {s.purpose}" for i, s in enumerate(formula.content_formula, 1)
    )
    titles = "\n".join(f"- `{t.pattern}` — например: «{t.example}»" for t in formula.title_formulas)
    advice = "\n".join(f"- **{a.format}**: {a.verdict} — {a.why}" for a in formula.format_advice)
    ideas = "\n".join(
        f"{i}. **{v.title}** ({v.format})\n   - хук: {v.hook}\n   - план: {v.outline}"
        for i, v in enumerate(formula.video_ideas, 1)
    )
    shot = formula.shot_recipe
    shots_section = "—"
    if shot.scene_duration_sec or shot.cuts_per_min or shot.rhythm:
        shots_section = "\n".join(
            line
            for line in (
                f"- **длина клипа: {shot.scene_duration_sec:g} с** "
                f"(`scene_duration_sec` в project.yaml)"
                if shot.scene_duration_sec
                else "",
                f"- разброс планов: {shot.shot_range_sec}" if shot.shot_range_sec else "",
                f"- склеек в минуту: {shot.cuts_per_min:g}" if shot.cuts_per_min else "",
                f"- ритм: {shot.rhythm}" if shot.rhythm else "",
                f"- движение камеры: {', '.join(shot.camera_moves)}" if shot.camera_moves else "",
                f"- подача чисел и сравнений: {'; '.join(shot.staging)}" if shot.staging else "",
            )
            if line
        )
    return f"""# Production blueprint: {channel_title or config.id}

> Сгенерировано анализатором videoforge {formula.computed_at:%d.%m.%Y} по
> research/{config.id}/ (LLM: {formula.llm}). Это выжимка закономерностей
> чужого успешного канала — основа для своих видео, не для копий.

## Что работает

{bullets(formula.what_works)}

## Формула контента

{steps or "—"}

## Правила хука

{bullets(formula.hook_rules)}

## Формулы заголовков

{titles or "—"}

## Форматы: что делать

{advice or "—"}

## Монтаж: измеренные числа

{shots_section}

## Производственные заметки

{formula.production_notes or "—"}

## Идеи по формуле (не копии)

{ideas or "—"}

## Чем хиты отличаются от провалов

{bullets(formula.hit_vs_control)}

## Ограничения анализа

{bullets(formula.caveats)}
"""


class FormulaStage(IntelStage):
    name = "formula"

    def run(self, ctx: IntelContext) -> StageResult:
        adir = ctx.analysis_dir
        required = (
            (channel_path(adir), "collect"),
            (adir / STATS_DIR / STATS_FILE, "stats"),
            (classify_path(adir), "classify"),
        )
        for path, stage in required:
            if not path.exists():
                return StageResult(status="failed", message=f"нет {path} — сначала {stage}")
        if ctx.llm is None:
            return StageResult(status="failed", message="нет LLM-провайдера (--llm или --mock)")

        sources = [classify_path(adir)]
        for index in (deep_index_path(adir), visual_index_path(adir)):
            if index.exists():
                sources.append(index)
        out = formula_path(adir)
        if out.exists() and not ctx.options.force:
            fresh = all(out.stat().st_mtime >= s.stat().st_mtime for s in sources)
            if fresh and read_model(out, Formula).llm == ctx.llm.name:
                return StageResult(
                    status="skipped", message=f"формула актуальна ({out}); --force — переделать"
                )

        data = read_model(channel_path(adir), ChannelData)
        stats = read_model(adir / STATS_DIR / STATS_FILE, StatsReport)
        classify = read_model(classify_path(adir), ClassifyReport)
        titles = {v.id: v.title for v in data.videos}
        analyses: list[DeepVideoAnalysis] = []
        if deep_index_path(adir).exists():
            for entry in read_model(deep_index_path(adir), DeepReport).videos:
                path = adir / DEEP_DIR / entry.video_id / ANALYSIS_FILE
                if entry.status == "done" and path.exists():
                    analyses.append(read_model(path, DeepVideoAnalysis))
        visuals: list[VisualVideoAnalysis] = []
        if visual_index_path(adir).exists():
            for shot_entry in read_model(visual_index_path(adir), VisualReport).videos:
                shot_path = adir / VISUAL_DIR / shot_entry.video_id / "visual.json"
                if shot_entry.status == "done" and shot_path.exists():
                    visuals.append(read_model(shot_path, VisualVideoAnalysis))

        outlier_ids = {o.video_id for o in stats.outliers}
        channel_desc = {
            "title": data.channel.title,
            "subscribers": data.channel.subscribers,
            "videos_analyzed": stats.counted_videos,
            "cadence_per_week": stats.cadence_per_week,
            "niche": classify.taxonomy.niche,
            "audience": classify.taxonomy.audience,
            "strategy_notes": classify.taxonomy.notes,
        }
        formats_payload = [
            {
                "id": f.format,
                "name": f.name,
                "count": f.count,
                "median_views": f.median_views,
                "top_video": titles.get(f.top_video_id, ""),
            }
            for f in classify.formats
        ]
        outliers_payload = [
            {"title": o.title, "group": o.group, "views": o.views, "ratio": o.ratio}
            for o in stats.outliers
        ]
        patterns_payload = [
            {
                "title": titles.get(c.video_id, ""),
                "pattern": c.title_pattern,
                "outlier": c.video_id in outlier_ids,
            }
            for c in classify.videos
            if c.title_pattern
        ]

        ctx.log.info(
            "формула: %d форматов, %d выбросов, %d разборов, %d визуальных…",
            len(formats_payload),
            len(outliers_payload),
            len(analyses),
            len(visuals),
        )
        raw = _ask_json(
            ctx.llm,
            _FORMULA_PROMPT.format(
                channel=json.dumps(channel_desc, ensure_ascii=False),
                formats=json.dumps(formats_payload, ensure_ascii=False),
                outliers=json.dumps(outliers_payload, ensure_ascii=False),
                title_patterns=json.dumps(patterns_payload, ensure_ascii=False),
                deep=json.dumps(
                    _deep_payload(analyses, titles, {r.video_id: r for r in stats.videos}),
                    ensure_ascii=False,
                ),
                visual=json.dumps(_visual_payload(visuals, titles), ensure_ascii=False),
                language=ctx.config.language,
            ),
            ctx,
        )
        try:
            formula = Formula.model_validate(
                {**raw, "computed_at": datetime.now(UTC), "llm": ctx.llm.name}
            )
        except ValidationError as exc:
            return StageResult(status="failed", message=f"ответ LLM не разобрался: {exc}")

        ctx.stage_dir(FORMULA_DIR)
        write_model(out, formula)
        blueprint_path(adir).write_text(
            render_blueprint(formula, ctx.config, data.channel.title), encoding="utf-8"
        )
        deep_note = f", разборов: {len(analyses)}" if analyses else ", без deep-разборов"
        return StageResult(
            status="done",
            message=(
                f"формула из {stats.counted_videos} видео{deep_note} → {blueprint_path(adir)}"
            ),
            changed=True,
        )
