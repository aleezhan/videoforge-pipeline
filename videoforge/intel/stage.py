"""Стадия анализа = функция над папкой research/<slug>/.

Тот же контракт, что у стадий эпизода (StageResult, RunOptions — общие),
но контекст легче: вместо раскадровки и четвёрки провайдеров — конфиг анализа,
коллектор и LLM (для classify и дальше; None — стадии без LLM).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from videoforge.core.manifest import Manifest, save_manifest
from videoforge.core.stage import RunOptions, StageResult
from videoforge.intel.collectors.base import Collector
from videoforge.intel.models import AnalysisConfig
from videoforge.providers.base import LlmProvider

__all__ = ["IntelContext", "IntelStage", "RunOptions", "StageResult"]


class IntelContext:
    def __init__(
        self,
        *,
        stage_name: str,
        analysis_dir: Path,
        config: AnalysisConfig,
        manifest: Manifest,
        collector: Collector,
        options: RunOptions,
        llm: LlmProvider | None = None,
    ) -> None:
        self.stage_name = stage_name
        self.analysis_dir = analysis_dir
        self.config = config
        self.manifest = manifest
        self.collector = collector
        self.options = options
        self.llm = llm
        self.log = logging.getLogger(f"videoforge.intel.{stage_name}")

    def stage_dir(self, name: str) -> Path:
        d = self.analysis_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add_cost(self, usd: float, provider: str, note: str = "") -> None:
        self.manifest.add_cost(stage=self.stage_name, provider=provider, usd=usd, note=note)

    def save(self) -> None:
        save_manifest(self.analysis_dir, self.manifest)


class IntelStage(ABC):
    name: ClassVar[str]

    def estimate_cost(self, ctx: IntelContext) -> float:
        """Прогноз расходов в USD до запуска. Раннер сверяет его с бюджетом."""
        return 0.0

    @abstractmethod
    def run(self, ctx: IntelContext) -> StageResult: ...
