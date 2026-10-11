"""Editing the edit plan from the dashboard: what may be changed with the mouse and what may not.

Until now the dashboard was read-only. The first write from the browser is not "one more
endpoint" but three new ways to ruin work, and each is closed here:

1. **Changing the wrong thing.** The request body is the WHOLE plan, not a path DSL
   (`{"path": "music.gain_db", "value": -16}`): pydantic validates the schema, so an
   invented field cannot exist. The server then builds the allowed edit itself - it loads
   the plan from disk, copies over only whitelisted fields - and compares the result with
   what was sent. Any difference means the client changed something forbidden, and the
   whole edit is rejected. Scene order, durations, voice-over and prompts are protected
   by construction rather than by a check: the plan schema has no such fields at all.

2. **Overwriting someone else's edit.** The plan has a fingerprint; the client sends back
   the one it read. On mismatch: 409 and no write, because the file changed while the
   person was looking at the screen.

3. **Writing something the build cannot digest.** The edit goes through THE SAME lint as
   the stage (`stages/edit.lint_with_material`). A block is a rejection; lint fixes
   (gain out of range, a step past the end of the video) are applied and returned as a
   list so the person sees exactly what was corrected in their edit.

Before writing, the previous plan is saved alongside (`edit_plan.<fingerprint>.json`) -
a single copy for "restore previous version". No deeper history: that is what git is for.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from videoforge.api.data import NotFound, file_url
from videoforge.core.editplan import EditPlan, EffectKind, load_plan, plan_path, save_plan
from videoforge.core.resources import Sfx, load_resource, search_dirs
from videoforge.core.storyboard import load_storyboard
from videoforge.paths import episode_dir, project_dir

# What a person may change with the mouse. One selection rule: **costs no money and does
# not touch timing**. Everything that moves time (scene order, durations, voice-over) lives
# in the storyboard and is governed by the measured voice - audio drift, frozen frames and
# caption coverage of speech all depend on it.
ALLOWED: frozenset[str] = frozenset(
    {
        "caption_style",  # the whole caption style (resource id)
        "music",  # track, levels, drop placement, steps, on/off
        "bed",  # background bed: track and level, {track: null} turns it off
        "fade_in_sec",
        "fade_out_sec",
        "effects",  # frame effects: kind, second, duration, strength
        "sfx",  # sound effects: id, second, level
        "outro",  # slowed-down tail
        "notes",
        # the "do not rewrite automatically" lock. Whitelisted on purpose: otherwise it
        # could only be removed by hand-editing the file, yet set by accident with one
        # mouse movement
        "pinned",
        # per scene: only what is inside its box and at its boundaries
        "scenes[].transition_in",
        "scenes[].captions",
        "scenes[].emphasis",
        "scenes[].music_gain_db",
        "scenes[].segments",  # segments: source and trim inside the box
        "scenes[].why",
    }
)

BACKUP_SUFFIX = ".json"


class PlanConflict(RuntimeError):
    """The file changed since the dashboard read it."""


class PlanForbidden(RuntimeError):
    """The edit went beyond the whitelist; nothing to accept."""


class PlanBlocked(RuntimeError):
    """Lint rejected the edit: the plan cannot be built."""


def fingerprint(plan: EditPlan | None) -> str:
    """Plan fingerprint, based on content rather than file time: two edits within one
    second differ, while a file rewritten with the same content does not."""
    if plan is None:
        return ""
    body = plan.model_dump_json(exclude={"generated_at"})
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _merge(current: Any, incoming: Any, prefix: str = "") -> Any:
    """Build the allowed edit: take the current plan and copy over only what the
    whitelist names."""
    if not isinstance(current, dict) or not isinstance(incoming, dict):
        return current
    out: dict[str, Any] = {}
    for key, value in current.items():
        path = f"{prefix}{key}"
        if path in ALLOWED:
            out[key] = incoming.get(key, value)
        elif key == "scenes" and isinstance(value, list) and isinstance(incoming.get(key), list):
            out[key] = _merge_scenes(value, incoming[key])
        else:
            out[key] = value
    return out


def _merge_scenes(current: list[Any], incoming: list[Any]) -> list[Any]:
    """Scenes are matched by `scene_id`, not by list position: the plan does not define
    scene order, and a reordered list must not mean a reordered video."""
    by_id = {s.get("scene_id"): s for s in incoming if isinstance(s, dict) and s.get("scene_id")}
    out: list[Any] = []
    for scene in current:
        if not isinstance(scene, dict):
            out.append(scene)
            continue
        given = by_id.get(scene.get("scene_id"))
        if given is None:
            out.append(scene)
            continue
        merged = dict(scene)
        for key in scene:
            if f"scenes[].{key}" in ALLOWED and key in given:
                merged[key] = given[key]
        out.append(merged)
    return out


def _differences(a: Any, b: Any, prefix: str = "") -> list[str]:
    """How the submitted plan differs from the allowed one, as a list of paths."""
    if isinstance(a, dict) and isinstance(b, dict):
        paths: list[str] = []
        for key in sorted(set(a) | set(b)):
            paths.extend(_differences(a.get(key), b.get(key), f"{prefix}{key}."))
        return paths
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{prefix.rstrip('.')} (было {len(a)}, прислано {len(b)})"]
        paths = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            paths.extend(_differences(x, y, f"{prefix.rstrip('.')}[{i}]."))
        return paths
    return [] if a == b else [prefix.rstrip(".")]


class PlanUpdate:
    """Edit result: what was written and what lint fixed along the way."""

    def __init__(self, plan: EditPlan, fixes: list[str], warnings: list[str], backup: Path | None):
        self.plan = plan
        self.fixes = fixes
        self.warnings = warnings
        self.backup = backup

    def dict(self) -> dict[str, Any]:
        return {
            "fingerprint": fingerprint(self.plan),
            "author": self.plan.author,
            "pinned": self.plan.pinned,
            "fixes": self.fixes,
            "warnings": self.warnings,
            "backup": self.backup.name if self.backup else None,
        }


def sfx_choices(pid: str) -> list[dict[str, Any]]:
    """Sound effects the lint will accept, returned together with the plan.

    Not decoration: lint BLOCKS an unknown sfx id (`unknown_sfx`), so one typo in an
    input field rejects the whole edit. Hence a choice rather than free text, and the list
    must come from the same place the judge uses: the same `search_dirs`, with a project
    sound overriding a global one by id.

    A file link is included for the same reason caption styles have a preview: you cannot
    hear "choir_hit" from its name, and picking a sound by name alone is guesswork.
    """
    seen: dict[str, dict[str, Any]] = {}
    for base in search_dirs(project_dir(pid)):
        for path in sorted((base / "sfx").glob("*.yaml")):
            if path.stem in seen:  # project sound already taken; global does not override
                continue
            try:
                sfx = load_resource("sfx", path.stem, Sfx, [base])
            except (FileNotFoundError, ValueError):
                continue  # broken YAML must not take down the whole edit form
            seen[path.stem] = {
                "id": sfx.id,
                "kind": sfx.kind,
                "gain_db": sfx.gain_db,
                "url": file_url(sfx.path()) if sfx.file else None,
            }
    return [seen[k] for k in sorted(seen)]


def read_plan(pid: str, ep: str) -> dict[str, Any]:
    """The episode plan for the dashboard: the plan, its fingerprint and what is editable."""
    edir = episode_dir(pid, ep)
    if not edir.is_dir():
        raise NotFound(f"эпизода {pid}/{ep} нет")
    plan = load_plan(edir)
    return {
        "ref": f"{pid}/{ep}",
        "exists": plan is not None,
        "plan": plan.model_dump(mode="json") if plan else None,
        "fingerprint": fingerprint(plan),
        "editable": sorted(ALLOWED),
        # Effect kinds come from the plan schema itself rather than being duplicated in the
        # UI: a list kept in two places silently diverges, and the person picks a kind the
        # renderer does not know
        "effect_kinds": list(get_args(EffectKind)),
        "sfx": sfx_choices(pid),
        "path": str(plan_path(edir)),
    }


def write_plan(pid: str, ep: str, incoming: dict[str, Any], base: str) -> PlanUpdate:
    """Write a plan edit from the dashboard. Rejection raises; nothing is silently swapped."""
    edir = episode_dir(pid, ep)
    if not edir.is_dir():
        raise NotFound(f"эпизода {pid}/{ep} нет")
    current = load_plan(edir)
    if current is None:
        raise NotFound(
            "плана монтажа нет — его делает стадия edit (videoforge run <ref> --only edit)"
        )
    if base != fingerprint(current):
        raise PlanConflict(
            "план на диске изменился с тех пор, как пульт его прочитал — обнови страницу"
        )

    given = EditPlan.model_validate(incoming).model_dump(mode="json")
    allowed = _merge(current.model_dump(mode="json"), given)
    forbidden = _differences(allowed, given)
    if forbidden:
        raise PlanForbidden(
            "эти поля пульт менять не может: " + ", ".join(forbidden[:8]) +
            ("…" if len(forbidden) > 8 else "")
        )  # fmt: skip

    plan = EditPlan.model_validate(allowed)
    result = _lint(edir, plan)
    blocking = [f.message for f in result.blocking]
    if blocking:
        raise PlanBlocked("правка не проходит проверку: " + "; ".join(blocking))

    plan = result.plan
    # A human edit wins over the model: the edit stage does not rewrite such a plan.
    # The lock is the EXPLICIT `pinned` field rather than authorship: they used to be the
    # same, so the first mouse edit disabled the model editor for good and could only be
    # undone by hand in the file. Now the lock is visible in the API response and can be
    # removed by the same kind of edit: send `pinned: false`
    plan.author = "manual"
    # The dashboard sends the WHOLE plan, so the lock field is always present, and
    # "untouched" can only be detected by whether the value changed. Unchanged means no
    # decision was made, so the edit gets pinned: losing manual work is worse than asking
    # once more. Changed means we obey, including unpinning
    if plan.pinned == current.pinned:
        plan.pinned = True
    plan.generated_at = datetime.now(UTC)
    backup = _backup(edir, current)
    save_plan(edir, plan)
    return PlanUpdate(plan, result.fixes, [f.message for f in result.warnings], backup)


def _lint(edir: Path, plan: EditPlan) -> Any:
    """The same lint as the stage: the browser cannot be a more lenient judge."""
    from videoforge.stages.edit import lint_with_material

    pid = edir.parent.parent.name
    return lint_with_material(
        plan,
        load_storyboard(edir),
        episode_dir=edir,
        resource_dirs=search_dirs(project_dir(pid)),
    )


def _backup(edir: Path, plan: EditPlan) -> Path | None:
    """Copy of the previous plan alongside, for "restore previous version". One copy, not a
    history: beyond that is git, and a second history mechanism would overstate its scope."""
    path = plan_path(edir).with_name(f"edit_plan.{fingerprint(plan)}{BACKUP_SUFFIX}")
    try:
        path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    except OSError:
        return None
    return path
