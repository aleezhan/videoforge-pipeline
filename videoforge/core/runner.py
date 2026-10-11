"""Runner: walks an episode through its workflow stages, enforcing gates and budget.

Rules:
- a stage is always started (it decides itself what is already done);
- before a stage, its cost forecast is checked against the budget; if it does
  not fit, the run stops;
- after a stage with `gate_after`, the runner stops until the result is approved
  (`videoforge approve`), or approves it itself with `auto_approve`;
- if a stage changed something, its previous approval is revoked;
- only one run works on an episode at a time: a second one waits or refuses
  rather than writing to the same files (`episode_lock`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from videoforge.core.manifest import load_manifest, now, save_manifest
from videoforge.core.project import (
    Blueprint,
    ProjectConfig,
    load_blueprint,
    load_project_from_dir,
    load_rules_from_dir,
    project_blueprint,
)
from videoforge.core.resources import load_resources, search_dirs
from videoforge.core.stage import BudgetExceeded, RunOptions, StageContext, StageResult
from videoforge.core.storyboard import load_storyboard
from videoforge.core.workflow import Workflow, WorkflowStage, load_workflow
from videoforge.paths import PROJECT_FILE
from videoforge.providers.base import ProviderSet
from videoforge.stages import stage_registry

log = logging.getLogger("videoforge.runner")


@dataclass
class RunReport:
    executed: list[tuple[str, StageResult]] = field(default_factory=list)
    stopped_at: str | None = None
    reason: str | None = None  # why the run stopped early (None = reached the end)
    awaiting_approval: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None


def select_stages(workflow: Workflow, options: RunOptions) -> list[WorkflowStage]:
    names = workflow.stage_names()

    def index(name: str) -> int:
        if name not in names:
            raise ValueError(f"стадии '{name}' нет в workflow '{workflow.name}': {names}")
        return names.index(name)

    if options.only:
        return [workflow.stages[index(options.only)]]
    a = index(options.start) if options.start else 0
    b = index(options.stop) if options.stop else len(names) - 1
    return workflow.stages[a : b + 1]


LOCK_FILE = ".run.lock"
# The lock is an OS-level lock on the file itself, not based on the file's age.
# Age-based staleness is wrong both ways: a long local run outlives any threshold
# and loses protection, while a killed process blocks the episode until it expires.
# The OS releases the lock the moment the process dies, which gives honest crash
# recovery without any heartbeat. The locked byte lies far past the end of the file
# so its contents (who is doing what) stay readable to refused callers, who need
# them for the "busy" message.
LOCK_BYTE = 1 << 20

if sys.platform == "win32":  # pragma: no cover - branch chosen by platform
    import msvcrt

    def _take(fd: int) -> bool:
        os.lseek(fd, LOCK_BYTE, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _drop(fd: int) -> None:
        os.lseek(fd, LOCK_BYTE, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - branch chosen by platform
    import fcntl

    def _take(fd: int) -> bool:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, LOCK_BYTE, os.SEEK_SET)
        except OSError:
            return False
        return True

    def _drop(fd: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.lockf(fd, fcntl.LOCK_UN, 1, LOCK_BYTE, os.SEEK_SET)


class Busy(RuntimeError):
    """Another run is already working on the episode."""


def lock_path(episode_dir: Path) -> Path:
    return episode_dir / LOCK_FILE


def _owner(path: Path) -> dict[str, object]:
    """Owner recorded in the lock file. Empty dict: the file exists but is unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def running(episode_dir: Path) -> dict[str, object] | None:
    """Who is currently working on the episode, if anyone.

    Liveness is tested by trying to take the same lock: if it succeeds, the
    previous owner is dead and its leftover file means nothing."""
    path = lock_path(episode_dir)
    if not path.exists():
        return None
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        if _take(fd):
            _drop(fd)
            return None
        return _owner(path) or {"what": "прогон"}
    finally:
        os.close(fd)


@contextlib.contextmanager
def episode_lock(episode_dir: Path, *, what: str = "") -> Iterator[Path]:
    """One run per episode.

    This guards a mundane case: someone clicks "rebuild" twice, or clicks it in
    the browser while `videoforge run` is going in a terminal. Two ffmpeg
    processes writing the same `final.mp4` produce a file nobody intended. For
    publishing the stakes are higher: two processes uploading the same video put a
    duplicate on the channel, which no local fix can undo.

    A file rather than an in-memory lock, because the dashboard and the CLI are
    separate processes with no shared memory. Acquisition is atomic: the kernel
    picks exactly one winner, unlike a "file missing" check followed by a write.
    We only release our own lock: another process's lock cannot be released by
    construction, and the file is removed only by the process that took it."""
    episode_dir.mkdir(parents=True, exist_ok=True)
    path = lock_path(episode_dir)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    if not _take(fd):
        busy = _owner(path)
        os.close(fd)
        raise Busy(
            f"над эпизодом уже идёт работа: {busy.get('what') or 'прогон'} "
            f"(pid {busy.get('pid')}, начат {busy.get('started')})"
        )
    try:
        body = json.dumps({"pid": os.getpid(), "started": now().isoformat(), "what": what}).encode(
            "utf-8"
        )
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, body)
        os.ftruncate(fd, len(body))
        yield path
    finally:
        _drop(fd)
        os.close(fd)
        # The file is not proof of work (the lock is), so a leftover file is harmless:
        # another process's `running()` holding it open blocks deletion on Windows.
        with contextlib.suppress(OSError):
            path.unlink()


def required_providers(episode_dir: Path, options: RunOptions) -> list[str]:
    """Provider kinds the selected stages will call; the CLI asks keys only for these.

    Running a single free stage (e.g. a subscription-based writer) must not require
    an image key just because image generation is in the episode workflow."""
    workflow = load_workflow(load_manifest(episode_dir).workflow)
    registry = stage_registry()
    needed: list[str] = []
    for ws in select_stages(workflow, options):
        stage_cls = registry.get(ws.name)
        for provider in stage_cls.needs if stage_cls else ():
            if provider not in needed:
                needed.append(provider)
    return needed


def project_context(episode_dir: Path) -> tuple[ProjectConfig | None, str]:
    """Settings and rules of the project containing the episode (episodes/<ep>)."""
    pdir = episode_dir.resolve().parent.parent
    if not (pdir / PROJECT_FILE).exists():
        return None, ""
    cfg = load_project_from_dir(pdir)
    return cfg, load_rules_from_dir(pdir, cfg)


@dataclass
class StageEstimate:
    stage: str
    cost_usd: float | None  # None: stage is not implemented
    gate_after: bool = False


def estimate_episode(episode_dir: Path, providers: ProviderSet) -> list[StageEstimate]:
    """Cost forecast for every workflow stage, without running or writing to disk.

    The same figure the runner checks against the budget before a stage: only
    work not yet done is counted (idempotency).
    """
    manifest = load_manifest(episode_dir)
    storyboard = load_storyboard(episode_dir)
    workflow = load_workflow(manifest.workflow)
    project, rules = project_context(episode_dir)
    blueprint = load_blueprint(project) if project is not None else ""
    project_root = episode_dir.resolve().parent.parent if project is not None else None
    resource_dirs = search_dirs(project_root)
    resources = load_resources(storyboard.resources, resource_dirs)
    registry = stage_registry()
    options = RunOptions()

    estimates: list[StageEstimate] = []
    for ws in workflow.stages:
        stage_cls = registry.get(ws.name)
        if stage_cls is None:
            estimates.append(StageEstimate(ws.name, None, ws.gate_after))
            continue
        ctx = StageContext(
            stage_name=ws.name,
            project_dir=episode_dir,
            storyboard=storyboard,
            manifest=manifest,
            providers=providers,
            resources=resources,
            options=options,
            project=project,
            project_root=project_root,
            rules=rules,
            blueprint=blueprint,
            resource_dirs=resource_dirs,
        )
        estimates.append(StageEstimate(ws.name, stage_cls().estimate_cost(ctx), ws.gate_after))
    return estimates


def run_episode(episode_dir: Path, providers: ProviderSet, options: RunOptions) -> RunReport:
    """Run the episode under a lock so a second run of it cannot start."""
    with episode_lock(episode_dir, what=options.only or "прогон"):
        return _run_episode(episode_dir, providers, options)


def _run_episode(episode_dir: Path, providers: ProviderSet, options: RunOptions) -> RunReport:
    manifest = load_manifest(episode_dir)
    storyboard = load_storyboard(episode_dir)
    workflow = load_workflow(manifest.workflow)
    project, rules = project_context(episode_dir)
    blueprint = project_blueprint(project) if project is not None else Blueprint("")
    if blueprint.note:
        log.info("blueprint: %d знаков, %s", len(blueprint.text), blueprint.note)
    project_root = episode_dir.resolve().parent.parent if project is not None else None
    resource_dirs = search_dirs(project_root)
    resources = load_resources(storyboard.resources, resource_dirs)
    registry = stage_registry()
    report = RunReport()

    # `providers` in the manifest records WHAT the episode was made with. A stage that
    # calls no provider (assembly is ffmpeg only) leaves it alone; otherwise a
    # rebuild would erase the names of the models that generated the visuals.
    if required_providers(episode_dir, options):
        manifest.providers = providers.names()
        save_manifest(episode_dir, manifest)

    for ws in select_stages(workflow, options):
        rec = manifest.record(ws.name)
        stage_cls = registry.get(ws.name)
        if stage_cls is None:
            rec.status = "failed"
            rec.error = f"стадия '{ws.name}' не реализована"
            save_manifest(episode_dir, manifest)
            report.stopped_at, report.reason = ws.name, rec.error
            break

        stage = stage_cls()
        ctx = StageContext(
            stage_name=ws.name,
            project_dir=episode_dir,
            storyboard=storyboard,
            manifest=manifest,
            providers=providers,
            resources=resources,
            options=options,
            project=project,
            project_root=project_root,
            rules=rules,
            blueprint=blueprint.text,
            resource_dirs=resource_dirs,
        )

        estimate = stage.estimate_cost(ctx)
        if not options.ignore_budget and manifest.spent_usd + estimate > manifest.budget_usd:
            rec.status = "blocked"
            rec.message = (
                f"прогноз ${estimate:.2f} + потрачено ${manifest.spent_usd:.2f} "
                f"> бюджет ${manifest.budget_usd:.2f} (подними budget_usd в manifest.json "
                f"или --ignore-budget)"
            )
            save_manifest(episode_dir, manifest)
            report.stopped_at, report.reason = ws.name, f"бюджет: {rec.message}"
            log.warning("[%s] заблокирована: %s", ws.name, rec.message)
            break

        log.info("[%s] старт (прогноз $%.2f)", ws.name, estimate)
        # keep timings of the real run: a skip must not overwrite them, or a repeated
        # run leaves no record of how long the stage took
        was_started, was_finished, was_steps = rec.started_at, rec.finished_at, rec.steps
        rec.status, rec.started_at, rec.finished_at, rec.error = "running", now(), None, None
        rec.steps = []  # timings of this run, not a sum of all previous ones
        save_manifest(episode_dir, manifest)
        ctx.blueprint_read = False  # track reads by run(), not by the estimate
        try:
            result = stage.run(ctx)
        except BudgetExceeded as exc:
            # a refusal to spend, not a failure: the stage's work is already on disk
            # and the run resumes here once a human raises the budget
            rec.status, rec.message, rec.finished_at = "blocked", str(exc), now()
            ctx.save()
            report.stopped_at, report.reason = ws.name, f"бюджет: {exc}"
            log.warning("[%s] остановлена: %s", ws.name, exc)
            break
        except Exception as exc:  # noqa: BLE001 - stage crashed: record it and stop
            rec.status, rec.error, rec.finished_at = "failed", f"{type(exc).__name__}: {exc}", now()
            ctx.save()
            report.stopped_at, report.reason = ws.name, rec.error
            log.exception("[%s] упала", ws.name)
            if options.raise_errors:
                raise
            break

        # report how many playbook rules went into the prompt, only for stages that
        # read the blueprint and did work (changed something or failed); a skip had
        # no prompt
        if blueprint.note and ctx.blueprint_read and (result.changed or result.status == "failed"):
            result.message = " · ".join(part for part in (result.message, blueprint.note) if part)
        rec.status, rec.message, rec.finished_at = result.status, result.message, now()
        # A run in which the stage did nothing (already done or skipped) must not
        # overwrite the timings of the run that did the work; otherwise
        # `videoforge timings` shows half a second instead of minutes.
        if result.status == "skipped" or (not result.changed and not rec.steps):
            rec.started_at, rec.finished_at, rec.steps = was_started, was_finished, was_steps
        if result.changed and ws.name in manifest.approvals:
            manifest.approvals.remove(ws.name)
        ctx.save()
        report.executed.append((ws.name, result))
        log.info("[%s] %s — %s", ws.name, result.status, result.message)

        if result.status == "failed":
            report.stopped_at, report.reason = ws.name, result.message
            break

        if ws.gate_after and result.status == "done" and ws.name not in manifest.approvals:
            if options.auto_approve:
                manifest.approvals.append(ws.name)
                save_manifest(episode_dir, manifest)
                continue
            manifest.awaiting_approval = ws.name
            save_manifest(episode_dir, manifest)
            report.stopped_at = ws.name
            report.awaiting_approval = ws.name
            report.reason = f"результат стадии '{ws.name}' ждёт одобрения"
            break

    return report
