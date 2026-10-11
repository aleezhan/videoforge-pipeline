"""Реестр коллекторов: имя из analysis.yaml → адаптер."""

from __future__ import annotations

from videoforge.intel.collectors.base import Collector


def build_collector(name: str) -> Collector:
    if name == "mock":
        from videoforge.intel.collectors.mock import MockCollector

        return MockCollector()
    if name == "ytdlp":
        from videoforge.intel.collectors.ytdlp import YtDlpCollector

        return YtDlpCollector()
    raise ValueError(f"неизвестный коллектор {name!r}; доступны: ytdlp, mock")
