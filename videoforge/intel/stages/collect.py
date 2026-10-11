"""Стадия collect: коллектор → 01_collect/channel.json.

Снапшот помнит, кто его собирал: сменился коллектор — данные устарели,
пересобираем (как provenance у активов эпизода). Существующий свежий снапшот
не перекачивается — сеть и квоты не тратятся зря.
"""

from __future__ import annotations

from pathlib import Path

from videoforge.core.io import read_model, write_model
from videoforge.intel.models import ChannelData
from videoforge.intel.stage import IntelContext, IntelStage, StageResult

COLLECT_DIR = "01_collect"
CHANNEL_FILE = "channel.json"


def channel_path(analysis_dir: Path) -> Path:
    return analysis_dir / COLLECT_DIR / CHANNEL_FILE


def _existing(ctx: IntelContext) -> ChannelData | None:
    """Снапшот, который можно не пересобирать (есть, тем же коллектором, без --force)."""
    path = channel_path(ctx.analysis_dir)
    if ctx.options.force or not path.exists():
        return None
    data = read_model(path, ChannelData)
    return data if data.channel.collector == ctx.collector.name else None


class CollectStage(IntelStage):
    name = "collect"

    def estimate_cost(self, ctx: IntelContext) -> float:
        if _existing(ctx) is not None:
            return 0.0
        return ctx.collector.price_per_channel_usd

    def run(self, ctx: IntelContext) -> StageResult:
        cfg = ctx.config
        if cfg.mode != "channel":
            return StageResult(
                status="failed", message=f"режим '{cfg.mode}' ещё не реализован; пока channel"
            )
        existing = _existing(ctx)
        path = channel_path(ctx.analysis_dir)
        if existing is not None:
            return StageResult(
                status="skipped",
                message=f"уже собрано: {len(existing.videos)} видео ({path}); --force — перекачать",
            )
        result = ctx.collector.collect_channel(cfg.urls[0], limit=cfg.max_videos)
        ctx.stage_dir(COLLECT_DIR)
        write_model(path, result.data)
        ctx.add_cost(result.cost_usd, ctx.collector.name, note=cfg.urls[0])
        return StageResult(
            status="done",
            message=f"{len(result.data.videos)} видео · канал «{result.data.channel.title}»",
            changed=True,
        )
