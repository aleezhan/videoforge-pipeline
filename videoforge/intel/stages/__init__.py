"""Реестр стадий анализа: имя из workflow → класс."""

from __future__ import annotations

from videoforge.intel.stage import IntelStage


def intel_stage_registry() -> dict[str, type[IntelStage]]:
    from videoforge.intel.stages.classify import ClassifyStage
    from videoforge.intel.stages.collect import CollectStage
    from videoforge.intel.stages.deep import DeepStage
    from videoforge.intel.stages.distill import DistillStage
    from videoforge.intel.stages.formula import FormulaStage
    from videoforge.intel.stages.stats import StatsStage
    from videoforge.intel.stages.visual import VisualStage

    stages: list[type[IntelStage]] = [
        CollectStage,
        StatsStage,
        ClassifyStage,
        DeepStage,
        VisualStage,
        FormulaStage,
        DistillStage,
    ]
    return {cls.name: cls for cls in stages}
