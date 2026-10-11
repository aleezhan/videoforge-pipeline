"""Стадия visual: как ролик снят → 06_visual/<video_id>/.

Вторая половина глубокого разбора, та, которой не видно в субтитрах. Делится
на две очень разные по цене части:

1. Ритм монтажа — ffmpeg-детект склеек по скачанному видео. Ни LLM, ни сети:
   средняя длина плана, разброс, ускоряется ли темп к финалу. Для нашего
   конвейера это самое ценное число: `scene_duration_sec` проекта сейчас
   выбран на глаз, а должен приходить отсюда.
2. Что в кадре — vision-LLM по кейфреймам. Кадры берутся не вслепую, а ровно
   на битах, которые стадия deep уже нашла в транскрипте: у нас есть таймкоды
   структуры, значит и смотреть надо туда, где что-то происходит. Все кадры
   ролика уходят одним запросом — так это одна минута на видео, а не десять.

Разбираются те же ролики, что и в deep (визуал без структуры бесполезен).
Видео без скачивания помечается в индексе и стадию не роняет.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from videoforge.core import ffmpeg
from videoforge.core.io import read_model, write_model
from videoforge.intel.models import (
    ChannelData,
    DeepReport,
    DeepVideoAnalysis,
    VideoMeta,
    VisualIndexEntry,
    VisualReport,
    VisualVideoAnalysis,
)
from videoforge.intel.shots import compute_shot_stats
from videoforge.intel.stage import IntelContext, IntelStage, StageResult
from videoforge.intel.stages.classify import _ask_json
from videoforge.intel.stages.collect import channel_path
from videoforge.intel.stages.deep import ANALYSIS_FILE, DEEP_DIR, deep_index_path

VISUAL_DIR = "06_visual"
VISUAL_FILE = "visual.json"
MAX_FRAMES = 10  # больше в один промпт класть незачем: разбор становится поверхностным

_VISUAL_PROMPT = """Analyze HOW this YouTube video is shot and edited — not what it says.

You are given the video's THUMBNAIL ({thumbnail}) — the only image a viewer sees
BEFORE clicking — followed by keyframes taken at the video's own structural
beats, plus the measured editing rhythm. Frame files are named frame_MMSS.jpg
by their timecode.

VIDEO
{meta}

MEASURED EDITING RHYTHM (from cut detection, not from you)
{shots}

BEATS THESE FRAMES WERE TAKEN AT
{beats}

Return ONLY valid JSON with exactly this shape (no fences, no comments):
{{
  "shot_language": "shot sizes and how they alternate — one sentence",
  "camera_moves": ["camera/animation moves you actually see, most frequent first"],
  "staging_patterns": ["how numbers, scale comparisons and abstractions are put on screen"],
  "text_on_screen": "when and how on-screen text/numbers appear — one sentence",
  "palette": "colour strategy — one sentence",
  "thumbnail": {{"elements": "what is on the cover", "text": "cover text, verbatim",
    "why_clickable": "one sentence"}},
  "transferable": ["3-6 techniques reproducible by an image-to-video pipeline"]
}}

Rules: describe only what is visible in the images — do not infer from the
title. If the thumbnail was not supplied, leave the "thumbnail" fields empty
instead of guessing from a keyframe. "transferable" must exclude anything that
depends on this channel's specific art style; keep staging, rhythm and camera
work. Write all values in the language "{language}" (cover text stays verbatim)."""


def visual_index_path(analysis_dir: Path) -> Path:
    return analysis_dir / VISUAL_DIR / VISUAL_FILE


def pick_frame_times(analysis: DeepVideoAnalysis, duration: float) -> list[float]:
    """Секунды для кейфреймов: биты структуры плюс обязательный кадр хука."""
    times: list[float] = [2.0]
    for beat in analysis.structure:
        parts = beat.t.split(":")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            continue
        seconds = int(parts[0]) * 60 + int(parts[1])
        if 0 < seconds < duration:
            times.append(float(seconds))
    unique = sorted(set(times))
    if len(unique) <= MAX_FRAMES:
        return unique
    step = (len(unique) - 1) / (MAX_FRAMES - 1)  # прореживаем равномерно по ролику
    return [unique[round(i * step)] for i in range(MAX_FRAMES)]


def _frame_name(seconds: float) -> str:
    return f"frame_{int(seconds) // 60:02d}{int(seconds) % 60:02d}.jpg"


class VisualStage(IntelStage):
    name = "visual"

    def run(self, ctx: IntelContext) -> StageResult:
        src = channel_path(ctx.analysis_dir)
        deep_index = deep_index_path(ctx.analysis_dir)
        for path, stage in ((src, "collect"), (deep_index, "deep")):
            if not path.exists():
                return StageResult(status="failed", message=f"нет {path} — сначала {stage}")
        if ctx.llm is None:
            return StageResult(status="failed", message="нет LLM-провайдера (--llm или --mock)")

        by_id: dict[str, VideoMeta] = {v.id: v for v in read_model(src, ChannelData).videos}
        selected = [
            (e.video_id, e.role)
            for e in read_model(deep_index, DeepReport).videos
            if e.status == "done" and e.video_id in by_id
        ]
        if not selected:
            return StageResult(status="failed", message="нечего разбирать: deep не дал ни одного")

        entries: list[VisualIndexEntry] = []
        analyzed = reused = 0
        for vid, role in selected:
            video = by_id[vid]
            vdir = ctx.stage_dir(VISUAL_DIR) / vid
            vdir.mkdir(exist_ok=True)
            out = vdir / "visual.json"
            if out.exists() and not ctx.options.force:
                if read_model(out, VisualVideoAnalysis).llm == ctx.llm.name:
                    reused += 1
                    entries.append(
                        VisualIndexEntry(
                            video_id=vid, status="done", role=role, note="готово ранее"
                        )
                    )
                    continue

            ctx.log.info("[%s] скачиваю видео для разбора монтажа…", vid)
            downloaded = ctx.collector.download_video(video, out_dir=vdir)
            if downloaded is None:
                ctx.log.warning("[%s] видео не скачалось — пропускаю", vid)
                entries.append(VisualIndexEntry(video_id=vid, status="no_video", role=role))
                continue
            ctx.add_cost(downloaded.cost_usd, ctx.collector.name, note=vid)

            try:
                duration = ffmpeg.probe_duration(downloaded.path)
                cuts = ffmpeg.detect_cuts(downloaded.path, threshold=ctx.config.scene_threshold)
            except ffmpeg.FfmpegError as exc:
                ctx.log.warning("[%s] ffmpeg не справился: %s", vid, exc)
                entries.append(
                    VisualIndexEntry(video_id=vid, status="failed", role=role, note=str(exc)[:200])
                )
                continue
            shots = compute_shot_stats(cuts, duration, threshold=ctx.config.scene_threshold)
            ctx.log.info(
                "[%s] планов: %d, средний %.1f с, склеек в минуту %.1f",
                vid, shots.shot_count, shots.mean_shot_sec, shots.cuts_per_min,
            )  # fmt: skip

            deep_path = ctx.analysis_dir / DEEP_DIR / vid / ANALYSIS_FILE
            deep_analysis = read_model(deep_path, DeepVideoAnalysis)
            times = pick_frame_times(deep_analysis, duration)
            frames = [ffmpeg.frame_at(downloaded.path, t, vdir / _frame_name(t)) for t in times]
            # обложка идёт первой и отдельно: это единственный кадр, который зритель
            # видит ДО клика, и в кейфреймах видео её нет
            thumbnail = ctx.collector.fetch_thumbnail(video, out_dir=vdir)
            images = [thumbnail, *frames] if thumbnail else frames

            beats = {beat.t: beat.beat for beat in deep_analysis.structure}
            meta = {
                "title": video.title,
                "duration_sec": video.duration_sec,
                "views": video.views,
            }
            ctx.log.info(
                "[%s] разбор %d кадров%s одним запросом…",
                vid, len(frames), " и обложки" if thumbnail else " (обложки нет)",
            )  # fmt: skip
            try:
                raw = _ask_json(
                    ctx.llm,
                    _VISUAL_PROMPT.format(
                        meta=json.dumps(meta, ensure_ascii=False),
                        thumbnail=thumbnail.name if thumbnail else "не удалось скачать",
                        shots=json.dumps(shots.model_dump(), ensure_ascii=False),
                        beats=json.dumps(beats, ensure_ascii=False),
                        language=ctx.config.language,
                    ),
                    ctx,
                    images=images,
                )
                analysis = VisualVideoAnalysis.model_validate(
                    {
                        **raw,
                        "video_id": vid,
                        "llm": ctx.llm.name,
                        "shots": shots.model_dump(),
                        "frames_analyzed": len(frames),
                        "thumbnail_seen": thumbnail is not None,
                        "role": role,
                    }
                )
            except (ValidationError, ValueError) as exc:
                ctx.log.warning("[%s] визуальный разбор не удался: %s", vid, exc)
                entries.append(VisualIndexEntry(video_id=vid, status="failed", note=str(exc)[:200]))
                continue
            write_model(out, analysis)
            ctx.save()  # прогресс после каждого видео
            analyzed += 1
            entries.append(VisualIndexEntry(video_id=vid, status="done", role=role))

        if analyzed or not visual_index_path(ctx.analysis_dir).exists():
            write_model(
                visual_index_path(ctx.analysis_dir),
                VisualReport(computed_at=datetime.now(UTC), llm=ctx.llm.name, videos=entries),
            )
        done = analyzed + reused
        if done == 0:
            return StageResult(
                status="failed",
                message=f"ни одно из {len(selected)} видео не разобралось (см. {VISUAL_FILE})",
            )
        missed = f", без видео/ошибок: {len(selected) - done}" if done < len(selected) else ""
        if analyzed == 0:
            return StageResult(
                status="skipped",
                message=f"все {done} видео уже разобраны визуально{missed}; --force — переделать",
            )
        return StageResult(
            status="done",
            message=f"разобрано визуально {done} из {len(selected)}{missed}",
            changed=True,
        )
