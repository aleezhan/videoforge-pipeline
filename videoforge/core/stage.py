"""A stage is a function over the project folder.

It reads earlier stages' artifacts from disk, writes its own and updates the storyboard.
From the outside it is a deterministic step: run it, get files, rerun it safely.
Stages must be idempotent: work that is already done and passed its check is not
redone (unless `options.force` is set).
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel

from videoforge.core.manifest import Manifest, StepRecord, save_manifest
from videoforge.core.project import ProjectConfig
from videoforge.core.resources import ResourceBundle
from videoforge.core.storyboard import Attempt, Scene, Storyboard, best_attempt, save_storyboard
from videoforge.providers.base import (
    CheckResult,
    LlmProvider,
    ProviderError,
    ProviderSet,
    Reference,
)


@dataclass
class RunOptions:
    auto_approve: bool = False  # do not stop at approval gates
    ignore_budget: bool = False  # skip the forecast-vs-budget check
    only: str | None = None  # run a single stage
    start: str | None = None  # first stage to run
    stop: str | None = None  # last stage to run (inclusive)
    max_attempts: int = 3  # regenerations per scene when the check fails
    force: bool = False  # redo even work that already passed its check
    raise_errors: bool = False  # re-raise stage exceptions (tests/debugging)


class BudgetExceeded(RuntimeError):
    """The next paid call does not fit into the episode budget.

    Not a stage error but a refusal to spend: everything the stage has done is
    already on disk (`ctx.save()` after each scene), and the run resumes from
    here once a human raises `budget_usd`."""


class StageResult(BaseModel):
    status: Literal["done", "skipped", "failed"]
    message: str = ""
    changed: bool = False  # stage generated something, so a prior approval is void


class StageContext:
    def __init__(
        self,
        *,
        stage_name: str,
        project_dir: Path,
        storyboard: Storyboard,
        manifest: Manifest,
        providers: ProviderSet,
        resources: ResourceBundle,
        options: RunOptions,
        project: ProjectConfig | None = None,
        project_root: Path | None = None,
        rules: str = "",
        blueprint: str = "",
        resource_dirs: list[Path] | None = None,
    ) -> None:
        self.stage_name = stage_name
        self.project_dir = project_dir  # episode folder: storyboard.json and stage folders
        # Project (channel) folder, for stages that read data shared by all episodes:
        # clip sources live in projects/<p>/sources/ because one long recording
        # yields many shorts. None means the episode is outside a project.
        self.project_root = project_root
        self.storyboard = storyboard
        self.manifest = manifest
        self.providers = providers
        self.resources = resources
        self.options = options
        self.project = project  # project (channel) settings, if the episode is in a project
        self.rules = rules  # rules.md text, for the director and the planner
        # blueprint.md of analysed reference channels plus their niche's playbook rules:
        # external experience for the writer, planner and editor. Reads are tracked so
        # the runner appends the "playbook rules: N of M" note only to stages that
        # actually used the blueprint (`blueprint_read`).
        self._blueprint = blueprint
        self.blueprint_read = False
        # where to look for resources (scene cast): project ones first, then global
        from videoforge.paths import resources_dir

        self.resource_dirs = resource_dirs or [resources_dir()]
        self.log = logging.getLogger(f"videoforge.{stage_name}")

    @property
    def blueprint(self) -> str:
        self.blueprint_read = True
        return self._blueprint

    def stage_dir(self, name: str) -> Path:
        d = self.project_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def rel(self, path: Path) -> str:
        """Path as stored in the storyboard: relative, with forward slashes."""
        return path.resolve().relative_to(self.project_dir.resolve()).as_posix()

    def abs(self, rel: str) -> Path:
        return self.project_dir / rel

    @contextmanager
    def step(self, name: str, note: str = "") -> Iterator[StepRecord]:
        """Time a chunk of work inside a stage: `with ctx.step("download") as s:`.

        The record is written to the manifest as soon as the block exits, so a
        crashed process keeps timings for what it finished. A failed step is timed
        too (its duration is part of the truth about the run), hence `finally`.
        Step names are meant for humans reading `videoforge timings`, not parsers."""
        record = StepRecord(name=name, sec=0.0, note=note)
        started = time.perf_counter()
        try:
            yield record
        finally:
            record.sec = round(time.perf_counter() - started, 3)
            self.manifest.record(self.stage_name).steps.append(record)
            save_manifest(self.project_dir, self.manifest)

    def afford(self, usd: float, note: str = "") -> None:
        """Check the budget covers the next paid call; raise `BudgetExceeded` if not.

        The stage forecast (`estimate_cost`) is an expectation, not a ceiling: it
        counts one generation per scene, while a failed frame check triggers up to
        `max_attempts` regenerations, each followed by another paid check. Checking
        once before the stage let multiples of the budget through, so the check runs
        before every spend instead.

        `usd` is the provider's table price, i.e. an expectation rather than an API
        guarantee. **Zero means "free", not "price unknown".** Where the real price
        is only known from the response (frame checks), the caller must pass an
        upper-bound estimate supplied by the provider (`LlmProvider.check_price_usd`).
        Treating unknown prices as 0 would let paid calls slip past an exhausted
        budget while known-price calls were blocked.

        This is still not a hard spending cap: it relies on reference prices that
        are reconciled with the price list by hand, and on upper-bound estimates."""
        if self.options.ignore_budget:
            return
        spent = self.manifest.spent_usd
        if spent + usd > self.manifest.budget_usd:
            what = f" ({note})" if note else ""
            raise BudgetExceeded(
                f"бюджет кончился{what}: потрачено ${spent:.2f} + "
                f"${usd:.2f} > ${self.manifest.budget_usd:.2f} "
                f"(подними budget_usd в manifest.json или --ignore-budget)"
            )

    def add_cost(
        self, usd: float, provider: str, scene_id: str | None = None, note: str = ""
    ) -> None:
        self.manifest.add_cost(
            stage=self.stage_name, provider=provider, usd=usd, scene_id=scene_id, note=note
        )

    def save(self) -> None:
        """Save progress. Called after every scene so a crashed process loses nothing."""
        save_storyboard(self.project_dir, self.storyboard)
        save_manifest(self.project_dir, self.manifest)


class Stage(ABC):
    name: ClassVar[str]
    # Provider kinds this stage requests work from, i.e. whose keys it needs. The CLI
    # asks only for keys that will actually be used: a subscription-based writer must
    # not demand an image key just because image generation is in the pipeline.
    # Provider properties (prices, supported durations) are read without a key and
    # are not listed here.
    needs: ClassVar[tuple[str, ...]] = ("image", "video", "tts", "llm")
    # Technique-library decision points (`core.library.DECISIONS`) where this stage's
    # MODEL makes the choice: matching techniques go into its prompt
    # (`library.for_stage`), and which/how many into the stage message. Empty for
    # stages without a model or whose model makes no library decisions. A point is
    # declared only when the model has a response field expressing the technique
    # and a measurement to choose by; otherwise the technique is inert in the prompt
    # and merely looks implemented.
    decisions: ClassVar[tuple[str, ...]] = ()

    def estimate_cost(self, ctx: StageContext) -> float:
        """Forecast cost in USD before running. The runner compares it with the budget."""
        return 0.0

    @abstractmethod
    def run(self, ctx: StageContext) -> StageResult: ...


def adopt_old_assets(ctx: StageContext, kind: str, pairs: list[tuple[Scene, str]]) -> int:
    """Mark assets without an input fingerprint as adopted and persist that.

    Called by a stage while it RUNS, not by the readiness predicate, which is also
    read by `estimate`, the report and the dashboard. An asset made before
    fingerprints existed is not redone (no reason to pay for it), but it is not
    claimed to be "made from these inputs" either: the mark carries a prefix, so
    the next edit is detected honestly. Policy:
    `features/asset_migration_policy.yaml`."""
    marked = [scene.id for scene, inputs in pairs if scene.adopt_inputs(kind, inputs)]
    if marked:
        ctx.log.info(
            "%s: принято как есть, соответствие входам не проверялось — %s",
            kind,
            ", ".join(marked),
        )
        ctx.save()
    return len(marked)


def check_or_refusal(
    checker: LlmProvider,
    frames: list[Path],
    *,
    references: list[Reference] | None = None,
    checklist: list[str],
    context: str,
) -> CheckResult:
    """Check frames, treating a SERVICE ERROR as a refusal rather than a stage failure.

    The checker runs on third-party quotas (e.g. a free tier with a daily request
    cap that answers 429 once exhausted). Crashing the stage there wastes paid work
    already done. The frame does not deserve regeneration for someone else's limit,
    and it cannot be marked "checked" either: like `block_reason`, it becomes
    `skipped` with a reason, the stage continues and counts refusals in its
    message."""
    try:
        return checker.check_images(
            frames, references=references or [], checklist=checklist, context=context
        )
    except ProviderError as exc:
        return CheckResult(passed=False, refused=f"{exc}"[:200])


def record_check(scene: Scene, kind: str, check: CheckResult) -> bool:
    """Turn a checker verdict into the asset's status and note. Returns True if the
    checker REFUSED to look.

    A refusal (`CheckResult.refused`, e.g. a safety filter blocking the checklist
    without an HTTP error) is neither a failure nor a pass: there is no reason to
    regenerate and nothing was checked. The asset gets `skipped` with a reason and
    the stage counts refusals in its message; otherwise refusing every scene would
    be indistinguishable from a check that lets everything through."""
    if check.refused:
        setattr(scene.checks, kind, "skipped")
        scene.checks.notes[kind] = f"контролёр отказался ({check.refused})"
        return True
    setattr(scene.checks, kind, "pass" if check.passed else "fail")
    if check.issues:
        scene.checks.notes[kind] = "; ".join(check.issues)
    else:
        scene.checks.notes.pop(kind, None)
    return False


def refused_note(count: int) -> str:
    """Stage-message suffix about checker refusals; empty when there were none."""
    if not count:
        return ""
    return f" · проверок отказано: {count} (контролёр не стал смотреть, см. checks.notes)"


def keep_best_attempt(scene: Scene, kind: str, made: list[Attempt]) -> str:
    """No attempt passed: keep the best one MADE IN THIS RUN, not the last one.
    Returns a line for the stage message.

    Otherwise the storyboard kept whichever attempt exhausted the counter, which
    could be worse than an earlier one. Selection rule: `best_attempt`.

    Only this run's attempts count: earlier ones may come from different inputs
    (a human edited the prompt and reran, the normal way to intervene), and a
    frame from an old prompt must not compete with frames from the new one. They
    stay in `attempt_log` as history, with paths, for manual picking.

    The asset gets the best attempt's path, status `fail` and its issues in
    `checks.notes`; the `attempts` counter, costs and input fingerprint are left
    alone because they describe all attempts, not the chosen one."""
    best = best_attempt(made)
    setattr(scene.assets, kind, best.path)
    setattr(scene.checks, kind, "fail")
    if best.issues:
        scene.checks.notes[kind] = "; ".join(best.issues)
    else:
        scene.checks.notes.pop(kind, None)
    count = len(best.issues)
    word = "замечание" if count % 10 == 1 and count % 100 != 11 else "замечания"
    if count % 10 in (0, 5, 6, 7, 8, 9) or 11 <= count % 100 <= 14:
        word = "замечаний"
    return (
        f"{scene.id}: не прошла после {len(made)} попыток, оставлена v{best.n:02d} ({count} {word})"
    )
