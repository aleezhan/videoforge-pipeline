"""Стадия classify: 01_collect/channel.json → 03_classify/classify.json.

Два прохода через LLM (текст, без картинок — thumbnail-паттерны придут со
стадией deep): один вызов на весь список названий строит таксономию канала
(ниша, аудитория, 3–8 форматов, прикидка RPM для ниши), затем пакетами по 25
видео каждое раскладывается по форматам. В конце — чистая математика:
просмотры по форматам. Стоимость: claude_cli и mock бесплатны; когда появится
платный API-адаптер, его цена ляжет в cost_usd ответов и в журнал расходов.
"""

from __future__ import annotations

import json
import statistics as st
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from videoforge.core.io import read_model, write_model
from videoforge.intel.models import (
    ChannelData,
    ClassifyReport,
    FormatStats,
    Taxonomy,
    VideoClass,
    VideoMeta,
)
from videoforge.intel.stage import IntelContext, IntelStage, StageResult
from videoforge.intel.stages.collect import channel_path
from videoforge.providers.base import LlmProvider

CLASSIFY_DIR = "03_classify"
CLASSIFY_FILE = "classify.json"
BATCH_SIZE = 25
SYSTEM = "You are a content strategist reverse-engineering a YouTube channel."

_TAXONOMY_PROMPT = """Analyze this YouTube channel and derive its content taxonomy.

CHANNEL
{channel}

VIDEOS ({count})
{videos}

Return ONLY valid JSON with exactly this shape (no fences, no comments):
{{
  "niche": "what the channel is about, one sentence",
  "audience": "who watches it, one sentence",
  "formats": [
    {{"id": "snake_case_id", "name": "short name", "description": "one sentence"}}
  ],
  "rpm_suggestion": {{"long": [min, max], "shorts": [min, max]}},
  "notes": "1-3 sentences about the strategy"
}}

Rules: 3-8 formats, each must be recognizable from titles alone.
Write every string value in the language "{language}" (format ids stay snake_case ASCII).
rpm_suggestion is a rough USD-per-1000-views range for THIS niche and the
channel's audience language (advertiser value differs a lot by niche and
geography; be conservative)."""

_VIDEOS_PROMPT = """Classify each video of a YouTube channel into the given formats.

FORMATS
{formats}

VIDEOS
{videos}

Return ONLY a valid JSON array, one item per video, same order:
[{{"video_id": "...", "format": "one of the format ids", "topic": "2-4 words",
"title_pattern": "title structure like 'how/why + extreme fact + place'"}}]

Write topic and title_pattern in the language "{language}"."""


def classify_path(analysis_dir: Path) -> Path:
    return analysis_dir / CLASSIFY_DIR / CLASSIFY_FILE


def _parse_json(text: str) -> Any:
    """JSON из ответа модели; терпит ограды и болтовню вокруг единственного блока."""
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        t = (t[first_nl + 1 :] if first_nl != -1 else "").rstrip()
        t = t[:-3] if t.endswith("```") else t
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start = min(i for i in (t.find("{"), t.find("[")) if i != -1)
        end = max(t.rfind("}"), t.rfind("]"))
        return json.loads(t[start : end + 1])


def _ask_json(
    llm: LlmProvider, prompt: str, ctx: IntelContext, *, images: list[Path] | None = None
) -> Any:
    """Два шанса получить валидный JSON; вторая попытка — с явным укором."""
    last_error = ""
    for attempt in range(2):
        suffix = f"\n\nYour previous answer was not valid JSON ({last_error}). " if attempt else ""
        result = llm.complete(prompt + suffix, system=SYSTEM, json_mode=True, images=images)
        ctx.add_cost(result.cost_usd, llm.name)
        try:
            return _parse_json(result.text)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)[:100]
            ctx.log.warning("невалидный JSON от %s (попытка %d): %s", llm.name, attempt + 1, exc)
    raise ValueError(f"LLM {llm.name} так и не вернул валидный JSON: {last_error}")


def _video_payload(v: VideoMeta, *, key: str, with_description: bool) -> dict[str, Any]:
    item: dict[str, Any] = {key: v.id, "title": v.title, "is_short": v.is_short}
    if with_description and v.description:
        item["description"] = v.description[:200]
    if v.views is not None:
        item["views"] = v.views
    return item


def _format_stats(
    classes: list[VideoClass], videos: list[VideoMeta], taxonomy: Taxonomy
) -> list[FormatStats]:
    views: dict[str, int] = {v.id: v.views for v in videos if v.views is not None}
    names = {f.id: f.name for f in taxonomy.formats}
    by_format: dict[str, list[str]] = {}
    for c in classes:
        by_format.setdefault(c.format, []).append(c.video_id)
    stats = []
    for fmt, ids in by_format.items():
        counted = [(vid, views[vid]) for vid in ids if vid in views]
        top_id, _ = max(counted, key=lambda p: p[1], default=("", 0))
        numbers = [n for _, n in counted]
        stats.append(
            FormatStats(
                format=fmt,
                name=names.get(fmt, fmt),
                count=len(ids),
                median_views=float(st.median(numbers)) if numbers else 0.0,
                mean_views=round(st.mean(numbers), 1) if numbers else 0.0,
                top_video_id=top_id,
            )
        )
    return sorted(stats, key=lambda s: -s.count)


class ClassifyStage(IntelStage):
    name = "classify"

    def run(self, ctx: IntelContext) -> StageResult:
        src = channel_path(ctx.analysis_dir)
        if not src.exists():
            return StageResult(status="failed", message=f"нет {src} — сначала стадия collect")
        if ctx.llm is None:
            return StageResult(status="failed", message="нет LLM-провайдера (--llm или --mock)")
        out = classify_path(ctx.analysis_dir)
        if out.exists() and not ctx.options.force:
            existing = read_model(out, ClassifyReport)
            if existing.llm == ctx.llm.name and out.stat().st_mtime >= src.stat().st_mtime:
                return StageResult(
                    status="skipped",
                    message=f"уже классифицировано ({out}); --force — переделать",
                )

        data = read_model(src, ChannelData)
        if not data.videos:
            return StageResult(status="failed", message="в снапшоте нет видео")

        ctx.log.info("таксономия: %d видео одним запросом…", len(data.videos))
        channel_desc = {
            "title": data.channel.title,
            "description": data.channel.description[:500],
            "subscribers": data.channel.subscribers,
        }
        raw = _ask_json(
            ctx.llm,
            _TAXONOMY_PROMPT.format(
                channel=json.dumps(channel_desc, ensure_ascii=False),
                count=len(data.videos),
                language=ctx.config.language,
                videos=json.dumps(
                    [_video_payload(v, key="id", with_description=False) for v in data.videos],
                    ensure_ascii=False,
                ),
            ),
            ctx,
        )
        taxonomy = Taxonomy.model_validate(raw)
        if not taxonomy.formats:
            return StageResult(status="failed", message="LLM не выделил ни одного формата")

        formats_json = json.dumps([f.model_dump() for f in taxonomy.formats], ensure_ascii=False)
        classes: list[VideoClass] = []
        batches = [data.videos[i : i + BATCH_SIZE] for i in range(0, len(data.videos), BATCH_SIZE)]
        for n, batch in enumerate(batches, start=1):
            ctx.log.info("классификация: пакет %d/%d…", n, len(batches))
            raw = _ask_json(
                ctx.llm,
                _VIDEOS_PROMPT.format(
                    formats=formats_json,
                    language=ctx.config.language,
                    videos=json.dumps(
                        [_video_payload(v, key="video_id", with_description=True) for v in batch],
                        ensure_ascii=False,
                    ),
                ),
                ctx,
            )
            known = {v.id for v in batch}
            try:
                for item in raw:
                    vc = VideoClass.model_validate(item)
                    if vc.video_id in known:
                        classes.append(vc)
            except (ValidationError, TypeError) as exc:
                return StageResult(
                    status="failed", message=f"пакет {n}: ответ LLM не разобрался: {exc}"
                )

        report = ClassifyReport(
            computed_at=datetime.now(UTC),
            llm=ctx.llm.name,
            taxonomy=taxonomy,
            videos=classes,
            formats=_format_stats(classes, data.videos, taxonomy),
        )
        ctx.stage_dir(CLASSIFY_DIR)
        write_model(out, report)
        return StageResult(
            status="done",
            message=(
                f"ниша: {taxonomy.niche[:60]} · форматов: {len(taxonomy.formats)} · "
                f"классифицировано {len(classes)} из {len(data.videos)}"
            ),
            changed=True,
        )
