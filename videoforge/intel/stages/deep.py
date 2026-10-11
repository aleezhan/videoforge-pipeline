"""Стадия deep: разбор устройства лучших видео → 04_deep/<video_id>/.

Текстовая половина глубокого разбора: субтитры через коллектор (бесплатно) +
LLM по транскрипту — хук, структура по таймкодам, стиль наррации, крючки
любопытства, CTA, переносимые приёмы. Как это снято — следующая стадия
(visual), она работает по тем же роликам.

Какие видео разбирать: явный список deep_videos из analysis.yaml, иначе
топ-`deep_top` по ratio плюс `deep_worst` худших как контрольная группа.
Контроль обязателен для честных выводов: приём, который есть и в хитах, и в
провалах того же канала, объясняет стиль, а не успех. Видео без субтитров
помечается в индексе, стадию не роняет.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from videoforge.core.io import read_model, write_model
from videoforge.intel.models import (
    AnalysisConfig,
    ChannelData,
    ClassifyReport,
    DeepIndexEntry,
    DeepReport,
    DeepVideoAnalysis,
    StatsReport,
    VideoMeta,
    VideoRole,
    VideoStats,
)
from videoforge.intel.stage import IntelContext, IntelStage, StageResult
from videoforge.intel.stages.classify import _ask_json, classify_path
from videoforge.intel.stages.collect import channel_path
from videoforge.intel.stages.stats import STATS_DIR, STATS_FILE
from videoforge.intel.transcript import vtt_to_text

DEEP_DIR = "04_deep"
DEEP_FILE = "deep.json"
ANALYSIS_FILE = "analysis.json"

_DEEP_PROMPT = """Analyze how this successful YouTube video is constructed.

VIDEO
{meta}

TRANSCRIPT (each line: MM:SS text)
{transcript}

Return ONLY valid JSON with exactly this shape (no fences, no comments):
{{
  "hook": {{"type": "question|shock_fact|story|claim|visual|other",
    "text": "quote the actual opening line(s) from the transcript",
    "why_it_works": "one sentence"}},
  "structure": [{{"t": "MM:SS", "beat": "short beat name",
    "purpose": "what it does for retention"}}],
  "narration_style": "pace, tone, sentence style — one sentence",
  "curiosity_gaps": ["questions the video opens and closes later"],
  "cta": "what the viewer is asked to do, or empty string",
  "takeaways": ["3-6 transferable techniques that make this video work"]
}}

Rules: 5-12 structure beats covering the whole runtime. Write all values in
the language "{language}"; only hook.text stays a verbatim quote in the
transcript's original language.
If comments_per_like_abnormal is true, this video's engagement is bought by a
call to comment, not earned by the content: say so in cta, and do not credit
the takeaways with an audience response the CTA produced."""


def deep_index_path(analysis_dir: Path) -> Path:
    return analysis_dir / DEEP_DIR / DEEP_FILE


CONTROL_MIN_AGE_DAYS = 90
"""Контрольные ролики берём только из тех, что успели набрать просмотры.

Свежее видео проигрывает соседям просто потому, что оно свежее: без этого
порога «худшие по ratio» — это всегда последние опубликованные, и контрольная
группа превращается в замер возраста, а не качества."""


def control_cutoff(rows: list[VideoStats], now: datetime) -> datetime:
    """Граница «видео успело набрать просмотры»: 90 дней или возраст ленты.

    Абсолютные 90 дней написаны под взрослый канал. Каналу, которому два
    месяца, они отсекают всё — контрольной группы не остаётся вовсе, хотя
    провал там виден и без выдержки: сосед по той же неделе набрал 400K, этот
    3K. Поэтому порог опускается до медианного возраста видео канала —
    контроль всегда берётся из старшей половины ленты, где у роликов было
    сравнимое время. У канала с многолетней историей медиана заведомо больше
    90 дней, и правило остаётся прежним."""
    ages = sorted(now - r.published_at for r in rows if r.published_at is not None)
    if not ages:
        return now - timedelta(days=CONTROL_MIN_AGE_DAYS)
    return now - min(timedelta(days=CONTROL_MIN_AGE_DAYS), ages[len(ages) // 2])


def select_deep_videos(
    stats: StatsReport, config: AnalysisConfig, available: set[str]
) -> list[tuple[str, VideoRole]]:
    """Кого разбирать: лучшие ролики канала плюс контрольная группа худших.

    Контроль нужен, чтобы отличить причины успеха от общих черт стиля: приём,
    который есть и в хитах, и в провалах, ничего не объясняет."""
    if config.deep_videos:
        return [(vid, "hit") for vid in config.deep_videos if vid in available]
    rows = [r for r in stats.videos if r.video_id in available]
    ranked = sorted(rows, key=lambda r: -r.ratio)
    picked: list[tuple[str, VideoRole]] = [(r.video_id, "hit") for r in ranked[: config.deep_top]]
    if config.deep_worst <= 0:
        return picked

    chosen = {vid for vid, _ in picked}
    cutoff = control_cutoff(rows, datetime.now(UTC))
    mature = [
        r
        for r in ranked
        if r.video_id not in chosen and r.published_at is not None and r.published_at <= cutoff
    ]
    for row in reversed(mature[-config.deep_worst :]):  # от самого слабого вверх
        picked.append((row.video_id, "control"))
    return picked


class DeepStage(IntelStage):
    name = "deep"

    def run(self, ctx: IntelContext) -> StageResult:
        src = channel_path(ctx.analysis_dir)
        stats_path = ctx.analysis_dir / STATS_DIR / STATS_FILE
        for path, stage in ((src, "collect"), (stats_path, "stats")):
            if not path.exists():
                return StageResult(status="failed", message=f"нет {path} — сначала {stage}")
        if ctx.llm is None:
            return StageResult(status="failed", message="нет LLM-провайдера (--llm или --mock)")

        data = read_model(src, ChannelData)
        stats = read_model(stats_path, StatsReport)
        by_id: dict[str, VideoMeta] = {v.id: v for v in data.videos}
        selected = select_deep_videos(stats, ctx.config, set(by_id))
        if not selected:
            return StageResult(status="failed", message="нечего разбирать: список видео пуст")
        rows = {r.video_id: r for r in stats.videos}
        formats: dict[str, str] = {}
        if classify_path(ctx.analysis_dir).exists():
            cls = read_model(classify_path(ctx.analysis_dir), ClassifyReport)
            formats = {c.video_id: c.format for c in cls.videos}

        entries: list[DeepIndexEntry] = []
        analyzed = reused = 0
        for vid, role in selected:
            video = by_id[vid]
            vdir = ctx.stage_dir(DEEP_DIR) / vid
            vdir.mkdir(exist_ok=True)
            out = vdir / ANALYSIS_FILE
            if out.exists() and not ctx.options.force:
                if read_model(out, DeepVideoAnalysis).llm == ctx.llm.name:
                    reused += 1
                    entries.append(
                        DeepIndexEntry(video_id=vid, status="done", role=role, note="готово ранее")
                    )
                    continue

            transcripts = sorted(vdir.glob("transcript.*"))
            if not transcripts:
                result = ctx.collector.fetch_transcript(video, out_dir=vdir)
                if result is None:
                    ctx.log.warning("[%s] субтитров нет — пропускаю", vid)
                    entries.append(DeepIndexEntry(video_id=vid, status="no_transcript", role=role))
                    continue
                ctx.add_cost(result.cost_usd, ctx.collector.name, note=vid)
                transcripts = [result.path]
            transcript_path = transcripts[0]
            language = transcript_path.suffixes[0].lstrip(".") if transcript_path.suffixes else ""

            row = rows.get(vid)
            meta = {
                "title": video.title,
                "description": video.description[:300],
                "duration_sec": video.duration_sec,
                "views": video.views,
                "ratio_vs_neighbors": row.ratio if row else None,
                "format": formats.get(vid, ""),
                # комментариев на лайк и норма канала: разбирая хит, модель должна
                # видеть, не куплено ли вовлечение призывом писать в комментарии
                "comments_per_like": row.engagement if row else None,
                "comments_per_like_abnormal": bool(row and row.engagement_outlier),
            }
            ctx.log.info(
                "[%s] разбор по транскрипту (%s)…", vid, "хит" if role == "hit" else "контроль"
            )
            try:
                raw = _ask_json(
                    ctx.llm,
                    _DEEP_PROMPT.format(
                        meta=json.dumps(meta, ensure_ascii=False),
                        language=ctx.config.language,
                        transcript=vtt_to_text(transcript_path),
                    ),
                    ctx,
                )
                analysis = DeepVideoAnalysis.model_validate(
                    {
                        **raw,
                        "video_id": vid,
                        "llm": ctx.llm.name,
                        "role": role,
                        "transcript_language": language,
                    }
                )
            except (ValidationError, ValueError) as exc:
                ctx.log.warning("[%s] разбор не удался: %s", vid, exc)
                entries.append(
                    DeepIndexEntry(video_id=vid, status="failed", role=role, note=str(exc)[:200])
                )
                continue
            write_model(out, analysis)
            ctx.save()  # прогресс после каждого видео — упавший процесс ничего не теряет
            analyzed += 1
            entries.append(DeepIndexEntry(video_id=vid, status="done", role=role))

        # индекс не перезаписывается вхолостую: его mtime — сигнал свежести для formula
        if analyzed or not deep_index_path(ctx.analysis_dir).exists():
            write_model(
                deep_index_path(ctx.analysis_dir),
                DeepReport(computed_at=datetime.now(UTC), llm=ctx.llm.name, videos=entries),
            )
        done = analyzed + reused
        if done == 0:
            return StageResult(
                status="failed",
                message=f"ни одно из {len(selected)} видео не разобралось (см. {DEEP_FILE})",
            )
        skipped_note = (
            f", без субтитров/ошибок: {len(selected) - done}" if done < len(selected) else ""
        )
        if analyzed == 0:
            return StageResult(
                status="skipped",
                message=f"все {done} видео уже разобраны{skipped_note}; --force — переделать",
            )
        return StageResult(
            status="done",
            message=(
                f"разобрано {done} из {len(selected)} видео{skipped_note} "
                f"(контрольных: {sum(1 for _, r in selected if r == 'control')})"
            ),
            changed=True,
        )
