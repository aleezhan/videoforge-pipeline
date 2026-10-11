"""Стадия stats: 01_collect/channel.json → 02_stats/stats.json.

Чистый python, бесплатно: динамика канала, частота публикаций и выбросы
по скользящей медиане (см. intel/statistics.py). После этой стадии — ворота:
человек смотрит выбросы и решает, какие видео разбирать за деньги.
"""

from __future__ import annotations

from videoforge.core.io import read_model, write_model
from videoforge.intel.models import ChannelData
from videoforge.intel.stage import IntelContext, IntelStage, StageResult
from videoforge.intel.stages.collect import channel_path
from videoforge.intel.statistics import compute_stats

STATS_DIR = "02_stats"
STATS_FILE = "stats.json"


class StatsStage(IntelStage):
    name = "stats"

    def run(self, ctx: IntelContext) -> StageResult:
        src = channel_path(ctx.analysis_dir)
        if not src.exists():
            return StageResult(status="failed", message=f"нет {src} — сначала стадия collect")
        out = ctx.stage_dir(STATS_DIR) / STATS_FILE
        if out.exists() and not ctx.options.force and out.stat().st_mtime >= src.stat().st_mtime:
            return StageResult(
                status="skipped", message=f"статистика актуальна ({out}); --force — пересчитать"
            )
        data = read_model(src, ChannelData)
        report = compute_stats(data.videos, threshold=ctx.config.outlier_threshold)
        write_model(out, report)
        groups = " · ".join(
            f"{name}: {gs.count} видео, медиана {gs.median_views:.0f}, "
            f"порог ×{gs.outlier_threshold:g}"
            for name, gs in sorted(report.groups.items())
        )
        how = "авто" if report.threshold == "auto" else f"×{report.threshold:g}"
        return StageResult(
            status="done",
            message=f"выбросов: {len(report.outliers)} ({how}) · {groups}",
            changed=True,
        )
