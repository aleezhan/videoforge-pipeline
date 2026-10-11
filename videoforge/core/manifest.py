"""Episode manifest: stage statuses, approvals, budget and the cost ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from videoforge.core.io import read_model, write_model

MANIFEST_FILE = "manifest.json"

StageStatus = Literal["pending", "running", "done", "failed", "skipped", "blocked"]


def now() -> datetime:
    return datetime.now(UTC)


class CostEntry(BaseModel):
    at: datetime
    stage: str
    provider: str
    usd: float
    scene_id: str | None = None
    note: str = ""


class StepRecord(BaseModel):
    """Timing of one step inside a stage: how many seconds it took and on what.

    A stage is a black box from outside, but time inside it is spent very
    differently: clipping mostly downloads source segments, assembly is dominated
    by the final ffmpeg pass, QA by a single vision-model call. Without timings
    people optimise what is visible rather than what is slow."""

    name: str
    sec: float
    note: str = ""  # what exactly was done: scene name, file size, model


class StageRecord(BaseModel):
    status: StageStatus = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str = ""
    error: str | None = None
    steps: list[StepRecord] = Field(default_factory=list)

    @property
    def elapsed_sec(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 3)


class PlatformPublish(BaseModel):
    """Where the video landed on one platform. If recorded, it is not uploaded again."""

    video_id: str = ""
    url: str = ""
    at: datetime | None = None
    provider: str = ""
    # Visibility the video was uploaded with. A private upload is "sent but seen by
    # nobody": without this field the dashboard shows it like a published video and
    # a forgotten private upload goes unnoticed.
    privacy: str = ""
    # Fingerprint of the thumbnail the platform already accepted
    # (`core.thumbnail.fingerprint`). The platform only returns a re-encoded copy, so
    # we remember what we sent: an edit re-uploads a changed image but not the same one.
    thumbnail: str = ""
    warnings: list[str] = Field(default_factory=list)


class PublishRecord(BaseModel):
    """Episode publishing state. Kept in the manifest rather than a separate file,
    since the manifest already answers "what has been done with this episode".

    `state`: dry_run - computed what would be sent, sent nothing; done - all
    requested platforms accepted; failed - at least one refused (text in `error`;
    the other platforms remain recorded in `platforms`)."""

    state: Literal["pending", "dry_run", "done", "failed"] = "pending"
    at: datetime | None = None
    platforms: dict[str, PlatformPublish] = Field(default_factory=dict)
    error: str = ""
    # Platforms that received the file but never answered (process killed, network
    # dropped). This is NOT "not sent": the video may already be live. Recording the
    # uncertainty beats guessing either way: a blind retry uploads a duplicate, while
    # silence hides a successful upload.
    in_flight: dict[str, datetime] = Field(default_factory=dict)


class CleanupRecord(BaseModel):
    """Record of the last intermediate-file cleanup (`videoforge episode cleanup`).

    Lets a missing scene clip read as intentional rather than broken: the dashboard
    and the user see when it was deleted and how much space was freed. Old manifests
    without this field still load (no cleanup happened)."""

    at: datetime
    classes: list[str] = Field(default_factory=list)  # file classes that were deleted
    freed_mb: float = 0.0
    files: int = 0


class Manifest(BaseModel):
    project_id: str
    episode_id: str = ""
    workflow: str
    created_at: datetime
    budget_usd: float = 5.0
    providers: dict[str, str] = Field(default_factory=dict)  # providers of the last run
    approvals: list[str] = Field(default_factory=list)  # stages whose result is approved
    awaiting_approval: str | None = None  # stage whose result awaits review
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    ledger: list[CostEntry] = Field(default_factory=list)
    # publishing: where the video already lives. None means never published
    publish: PublishRecord | None = None
    # last intermediate-file cleanup. None means never cleaned
    cleanup: CleanupRecord | None = None

    @property
    def spent_usd(self) -> float:
        return round(sum(e.usd for e in self.ledger), 6)

    def stage_cost(self, stage: str) -> float:
        return round(sum(e.usd for e in self.ledger if e.stage == stage), 6)

    def add_cost(
        self,
        *,
        stage: str,
        provider: str,
        usd: float,
        scene_id: str | None = None,
        note: str = "",
    ) -> None:
        if usd <= 0:
            return
        self.ledger.append(
            CostEntry(
                at=now(), stage=stage, provider=provider, usd=usd, scene_id=scene_id, note=note
            )
        )

    def record(self, stage: str) -> StageRecord:
        return self.stages.setdefault(stage, StageRecord())


def manifest_path(project_dir: Path) -> Path:
    return project_dir / MANIFEST_FILE


def load_manifest(project_dir: Path) -> Manifest:
    return read_model(manifest_path(project_dir), Manifest)


def save_manifest(project_dir: Path, manifest: Manifest) -> None:
    write_model(manifest_path(project_dir), manifest)
