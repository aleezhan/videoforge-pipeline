"""Stage registry: maps a stage name used in ``workflows/*.yaml`` to its class."""

from __future__ import annotations

from videoforge.core.stage import Stage


def stage_registry() -> dict[str, type[Stage]]:
    """Return ``{stage name: Stage subclass}`` for every built-in stage.

    Imports are local so that loading the registry stays cheap for CLI commands
    that never run a pipeline.
    """
    from videoforge.stages.assemble import AssembleStage
    from videoforge.stages.audio import AudioStage
    from videoforge.stages.cut import CutStage
    from videoforge.stages.director import DirectorStage
    from videoforge.stages.edit import EditStage
    from videoforge.stages.images import ImagesStage
    from videoforge.stages.planner import PlannerStage
    from videoforge.stages.publish import PublishStage
    from videoforge.stages.qa import QaStage
    from videoforge.stages.reframe import ReframeStage
    from videoforge.stages.translate import TranslateStage
    from videoforge.stages.video import VideoStage

    stages: list[type[Stage]] = [
        DirectorStage,
        PlannerStage,
        ImagesStage,
        VideoStage,
        AudioStage,
        CutStage,
        TranslateStage,
        EditStage,
        AssembleStage,
        QaStage,
        ReframeStage,
        PublishStage,
    ]
    return {cls.name: cls for cls in stages}
