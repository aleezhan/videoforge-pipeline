"""Data builders for the UI: pipeline folders -> JSON-ready dicts.

No HTTP here: functions take ids and return what the page will show, so they can be
called from a test without a server or from any other layer. They read the disk on every
call - there is deliberately no cache: a person edits YAML in an editor, refreshes the
page, and it must show the new content.

Media (frames, portfolios, final.mp4) is returned as a link rather than content:
`/files/<root>/<path>`, where roots are projects/, resources/, research/, library/,
accounts/. Paths outside these roots get no link.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videoforge.core import queue as q
from videoforge.core.accounts import (
    NETWORKS,
    Account,
    AccountVideos,
    accounts_for_project,
    avatar_path,
    last_published,
    latest_known,
    list_accounts,
    load_account,
    load_days,
    load_history,
    load_videos,
    validate_account,
)
from videoforge.core.features import (
    AREAS,
    STATUSES,
    TOPICS,
    Feature,
    list_features,
    load_feature,
    sort_key,
    validate_feature,
)
from videoforge.core.ffmpeg import FfmpegError, frame_at
from videoforge.core.library import (
    CONDITION_KEYS,
    DECISION_GROUPS,
    DECISIONS,
    Technique,
    find_example,
    load_all,
)
from videoforge.core.links import (
    KINDS as LINK_KINDS,
)
from videoforge.core.links import (
    Link,
    list_links,
    load_link,
    validate_link,
)
from videoforge.core.links import (
    sort_key as link_sort_key,
)
from videoforge.core.manifest import Manifest, load_manifest
from videoforge.core.project import (
    CHECK_FILE,
    PLATFORMS,
    ProjectConfig,
    check_question,
    list_episodes,
    list_projects,
    load_check,
    load_project,
    load_rules_from_dir,
)
from videoforge.core.prompt_translation import (
    SceneTranslation,
    fresh_scenes,
    load_translations,
    stale_scenes,
)
from videoforge.core.resource_examples import RECIPES as EXAMPLE_RECIPES
from videoforge.core.resource_examples import find_example as find_resource_example
from videoforge.core.resource_examples import find_variants as find_example_variants
from videoforge.core.resources import (
    KINDS,
    VISUAL_KINDS,
    MusicTrack,
    Resource,
    Sfx,
    VisualResource,
    list_resources,
    load_resource,
    search_dirs,
)
from videoforge.core.storyboard import Scene, Storyboard, is_filmed, load_storyboard
from videoforge.core.todo import Task
from videoforge.core.workflow import load_workflow
from videoforge.paths import (
    accounts_dir,
    analysis_dir,
    bench_dir,
    blueprint_path,
    episode_dir,
    inbox_dir,
    library_dir,
    project_dir,
    projects_dir,
    research_dir,
    resources_dir,
    sources_dir,
    workflows_dir,
)

log = logging.getLogger("videoforge.api")

# link root -> where it lives on disk. Functions, not paths: tests swap the environment
ROOTS: dict[str, Callable[[], Path]] = {
    "projects": projects_dir,
    "resources": resources_dir,
    "research": research_dir,
    "library": library_dir,
    "accounts": accounts_dir,
    # the benchmark sandbox lives inside projects/ by default and is linked through it;
    # a separate root is needed when VF_BENCH_DIR points elsewhere
    "bench": bench_dir,
    # inbox files (the "Files" section): not part of the pipeline, but viewed and
    # downloaded the same way, so they need the same kind of link
    "inbox": inbox_dir,
}

KIND_TITLES: dict[str, str] = {
    "characters": "Персонажи",
    "locations": "Места",
    "props": "Предметы",
    "styles": "Стили",
    "voices": "Голоса",
    "niches": "Ниши",
    "caption_styles": "Субтитры",
    "music": "Музыка",
    "sfx": "Звуки",
    "backgrounds": "Фоны",
}

# project.yaml slot -> resource kind
SLOT_KIND: dict[str, str] = {
    "character": "characters",
    "style": "styles",
    "voice": "voices",
    "niche": "niches",
    "caption_style": "caption_styles",
    "music": "music",
}

SLOT_TITLES: dict[str, str] = {
    "character": "Персонаж",
    "style": "Стиль",
    "voice": "Голос",
    "niche": "Ниша",
    "caption_style": "Субтитры",
    "music": "Музыка",
}

SCRIPT_FILE = Path("01_script") / "script.md"
QA_FILE = Path("05_assemble") / "qa.json"
PUBLISH_FILE = Path("06_publish") / "publish.json"
REPORT_FILE = "report.html"

# file kind by extension, for folders without a schema (sources, benchmark sandbox)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}
DOC_EXTS = {".md"}
TABLE_EXTS = {".csv"}
HTML_EXTS = {".html", ".htm"}
TEXT_EXTS = {".txt", ".log", ".json", ".yaml", ".yml", ".py", ".sh", ".srt", ".vtt", ".json3"}
MAX_SOURCE_FILES = 300  # files per source in a response; beyond that, open the folder


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in DOC_EXTS:
        return "doc"
    if ext in TABLE_EXTS:
        return "table"
    if ext in HTML_EXTS:
        return "html"
    if ext in TEXT_EXTS:
        return "text"
    return "other"


def file_entry(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {
        "name": path.name,
        "url": file_url(path),
        "kind": kind_of(path),
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, UTC),
    }


class NotFound(LookupError):
    """No such project / episode / resource - a 404 in HTTP terms."""


# ---------------------------------------------------------------- files


def file_url(path: Path | None) -> str | None:
    """Absolute path -> `/files/<root>/<path>?v=<fingerprint>`; outside the roots -> None.

    The fingerprint (mtime and size) in the URL is mandatory: a rebuilt video lands on the
    same path, and the browser would serve the OLD video from cache, leaving the person
    watching yesterday's build and wondering why their edits are missing. When the file
    changes the URL changes and the cache refreshes itself; an unchanged file is still
    served from cache.
    """
    if path is None:
        return None
    p = path.resolve()
    for name, root_fn in ROOTS.items():
        root = root_fn().resolve()
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        url = f"/files/{name}/{rel.as_posix()}"
        try:
            st = p.stat()
        except OSError:
            return url
        return f"{url}?v={int(st.st_mtime)}-{st.st_size}"
    return None


def resolve_file(root: str, rel: str) -> Path:
    """Disk path for a link; escaping the root (`..`) raises NotFound, never returns a file."""
    root_fn = ROOTS.get(root)
    if root_fn is None:
        raise NotFound(f"нет такого корня: {root}")
    base = root_fn().resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise NotFound(f"путь вне корня {root}: {rel}")
    if not target.is_file():
        raise NotFound(f"файла нет: {rel}")
    return target


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("%s не читается: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------- resources


def resource_card(kind: str, obj: Resource, project: str | None = None) -> dict[str, Any]:
    """Resource card for lists: name, description, preview, origin."""
    card: dict[str, Any] = {
        "kind": kind,
        "kind_title": KIND_TITLES.get(kind, kind),
        "id": obj.id,
        "name": obj.name or obj.id,
        "description": obj.description,
        "scope": obj._scope,
        "project": project,
        "preview_url": None,
        "file_url": None,
        "example_url": None,
        "example_kind": None,
        "missing": False,
    }
    if isinstance(obj, VisualResource):
        refs = obj.reference_paths()
        card["preview_url"] = file_url(refs[0]) if refs else None
        card["portfolio_count"] = len(refs)
    if isinstance(obj, MusicTrack | Sfx) and obj.file:
        card["file_url"] = file_url(obj.path())
    # showcase: a clip with the caption style, a voice sample - "how it looks and sounds"
    example = find_resource_example(obj)
    if example is not None:
        card["example_url"] = file_url(example.path)
        card["example_kind"] = example.kind
    return card


def missing_card(kind: str, resource_id: str, slot: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "kind_title": KIND_TITLES.get(kind, kind),
        "id": resource_id,
        "name": resource_id,
        "description": "",
        "scope": None,
        "project": None,
        "preview_url": None,
        "file_url": None,
        "example_url": None,
        "example_kind": None,
        "missing": True,
        "slot": slot,
    }


def _load(kind: str, resource_id: str, dirs: list[Path]) -> Resource:
    return load_resource(kind, resource_id, KINDS[kind], dirs)


def _project_resource_dirs() -> list[tuple[str, Path]]:
    """(project, its resources/) for every project that has the folder."""
    out: list[tuple[str, Path]] = []
    for pid in list_projects():
        rdir = project_dir(pid) / "resources"
        if rdir.is_dir():
            out.append((pid, rdir))
    return out


def resources_overview() -> dict[str, Any]:
    """Resource kinds with counts, global and per-project separately."""
    global_ids = list_resources()
    per_project = [(pid, list_resources(rdir)) for pid, rdir in _project_resource_dirs()]
    kinds = []
    for kind in KINDS:
        project_count = sum(len(ids.get(kind, [])) for _, ids in per_project)
        kinds.append(
            {
                "kind": kind,
                "title": KIND_TITLES.get(kind, kind),
                "visual": kind in VISUAL_KINDS,
                "count_global": len(global_ids.get(kind, [])),
                "count_projects": project_count,
            }
        )
    return {"kinds": kinds}


def resources_list(kind: str) -> dict[str, Any]:
    if kind not in KINDS:
        raise NotFound(f"нет такого вида ресурсов: {kind}")
    items: list[dict[str, Any]] = []
    for rid in list_resources().get(kind, []):
        try:
            items.append(resource_card(kind, _load(kind, rid, [resources_dir()])))
        except Exception as exc:  # noqa: BLE001 - show broken YAML rather than hide it
            card = missing_card(kind, rid)
            card["error"] = str(exc)
            items.append(card)
    for pid, rdir in _project_resource_dirs():
        for rid in list_resources(rdir).get(kind, []):
            try:
                items.append(resource_card(kind, _load(kind, rid, [rdir]), project=pid))
            except Exception as exc:  # noqa: BLE001
                card = missing_card(kind, rid)
                card.update(project=pid, error=str(exc))
                items.append(card)
    return {"kind": kind, "title": KIND_TITLES.get(kind, kind), "items": items}


def resource_used_by(kind: str, resource_id: str) -> list[dict[str, str]]:
    """Where a resource is used: project.yaml slots and episode scene casts."""
    used: list[dict[str, str]] = []
    ref = f"{kind}/{resource_id}"
    for pid in list_projects():
        try:
            cfg = load_project(pid)
        except Exception:  # noqa: BLE001
            continue
        for slot, slot_kind in SLOT_KIND.items():
            if slot_kind == kind and getattr(cfg.resources, slot) == resource_id:
                used.append({"project": pid, "where": f"project.yaml: {slot}", "episode": ""})
        if kind not in VISUAL_KINDS:
            continue
        for ep in list_episodes(pid):
            try:
                sb = load_storyboard(episode_dir(pid, ep))
            except Exception:  # noqa: BLE001
                continue
            scenes = [s.id for s in sb.scenes if ref in s.cast]
            if scenes:
                used.append({"project": pid, "episode": ep, "where": f"сцены: {', '.join(scenes)}"})
    return used


def _technique_refs(refs: list[str]) -> list[dict[str, Any]]:
    """A resource's `techniques:` -> library technique cards. A dangling reference is not
    silent: it shows as "technique missing", otherwise a typo would live forever."""
    known = {f"{t.decision}/{t.id}": t for t in load_all()}
    out: list[dict[str, Any]] = []
    for ref in refs:
        t = known.get(ref)
        decision, _, tid = ref.partition("/")
        out.append(
            {
                "ref": ref,
                "decision": t.decision if t else decision,
                "decision_title": DECISIONS.get(t.decision if t else decision, decision),
                "id": t.id if t else tid,
                "title": t.title if t else ref,
                "status": t.status if t else None,
                "missing": t is None,
            }
        )
    return out


def _all_resources() -> list[tuple[str, str | None, Resource]]:
    """(kind, project or None, resource) across all kinds, global and per-project."""
    out: list[tuple[str, str | None, Resource]] = []
    sources: list[tuple[str | None, Path]] = [(None, resources_dir())]
    sources += [(pid, rdir) for pid, rdir in _project_resource_dirs()]
    for pid, rdir in sources:
        ids = list_resources(rdir)
        for kind in KINDS:
            for rid in ids.get(kind, []):
                try:
                    out.append((kind, pid, _load(kind, rid, [rdir])))
                except Exception as exc:  # noqa: BLE001 - broken YAML must not take down the page
                    log.warning("ресурс %s/%s не разобран: %s", kind, rid, exc)
    return out


def technique_used_by(decision: str, technique_id: str) -> list[dict[str, Any]]:
    """The reverse of `techniques:` - which resources already use a technique."""
    ref = f"{decision}/{technique_id}"
    return [
        {
            "kind": kind,
            "kind_title": KIND_TITLES.get(kind, kind),
            "id": obj.id,
            "name": obj.name or obj.id,
            "project": pid,
        }
        for kind, pid, obj in _all_resources()
        if ref in obj.techniques
    ]


def resource_detail(kind: str, resource_id: str, project: str | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise NotFound(f"нет такого вида ресурсов: {kind}")
    dirs = search_dirs(project_dir(project)) if project else [resources_dir()]
    try:
        obj = _load(kind, resource_id, dirs)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    card = resource_card(kind, obj, project if obj._scope == "project" else None)
    data = obj.model_dump()
    portfolio: list[dict[str, str | None]] = []
    if isinstance(obj, VisualResource):
        portfolio = [{"name": p.stem, "url": file_url(p)} for p in obj.reference_paths()]
    # show the command only where a showcase is possible at all: knowing which kinds
    # have a recipe belongs in core, not in the page markup
    suffix = f" --project {project}" if project and obj._scope == "project" else ""
    command = (
        f"videoforge resource gen-example {kind.rstrip('s')} {resource_id}{suffix}"
        if kind in EXAMPLE_RECIPES
        else None
    )
    return {
        **card,
        "fields": data,
        "path": str(obj._base / f"{obj.id}.yaml"),
        "portfolio": portfolio,
        "techniques": _technique_refs(obj.techniques),
        "example_command": command,
        # voice delivery samples next to the showcase (`gen-example voice <id> --variants`):
        # the same phrase at a different speed and pitch, labelled with what changed
        "examples": [
            {"label": v.label, "url": file_url(v.path)} for v in find_example_variants(obj)
        ],
        "used_by": resource_used_by(kind, resource_id),
    }


# ---------------------------------------------------------------- episodes


def _stage_rows(manifest: Manifest) -> list[dict[str, Any]]:
    """Stages in workflow order; unstarted ones are pending. Stages missing from the
    workflow but present in the manifest (an older run) go at the end."""
    try:
        wf = load_workflow(manifest.workflow)
        order = [(s.name, s.gate_after) for s in wf.stages]
    except (FileNotFoundError, ValueError):
        order = [(name, False) for name in manifest.stages]
    known = {name for name, _ in order}
    order += [(name, False) for name in manifest.stages if name not in known]
    rows = []
    for name, gate in order:
        rec = manifest.stages.get(name)
        rows.append(
            {
                "name": name,
                "status": rec.status if rec else "pending",
                "gate_after": gate,
                "approved": name in manifest.approvals,
                "awaiting": manifest.awaiting_approval == name,
                "message": rec.message if rec else "",
                "error": rec.error if rec else None,
                "elapsed_sec": rec.elapsed_sec if rec else None,
                "cost_usd": manifest.stage_cost(name),
                "started_at": rec.started_at if rec else None,
                "finished_at": rec.finished_at if rec else None,
            }
        )
    return rows


def _duration(sb: Storyboard) -> float | None:
    if sb.final.duration_sec:
        return sb.final.duration_sec
    if sb.scenes:
        return round(sum(s.duration_sec for s in sb.scenes), 2)
    return None


def _episode_paths(pid: str, ep: str) -> tuple[Path, Storyboard, Manifest]:
    edir = episode_dir(pid, ep)
    try:
        return edir, load_storyboard(edir), load_manifest(edir)
    except FileNotFoundError as exc:
        raise NotFound(f"эпизода {pid}/{ep} нет") from exc


def episode_summary(pid: str, ep: str) -> dict[str, Any]:
    edir, sb, m = _episode_paths(pid, ep)
    final_video = edir / sb.final.video if sb.final.video else None
    return {
        "id": ep,
        "project": pid,
        "ref": f"{pid}/{ep}",
        "title": sb.title or sb.publish.title or ep,
        "brief": sb.brief,
        "workflow": sb.workflow,
        "scenes": len(sb.scenes),
        "duration_sec": _duration(sb),
        "spent_usd": m.spent_usd,
        "budget_usd": m.budget_usd,
        "awaiting_approval": m.awaiting_approval,
        "final_video_url": file_url(final_video) if final_video and final_video.exists() else None,
        "created_at": m.created_at,
        "stages": _stage_rows(m),
        # "packaged": the publish stage built the title and description; this is NOT
        # "published". They used to be confused: the publish stage dot was green for a
        # video that was not on the platform
        "published": (edir / PUBLISH_FILE).exists(),
        "upload": _upload_state(m),
    }


def _upload_state(m: Manifest) -> dict[str, Any] | None:
    """Where the video actually went and whether it is visible there.

    `private` is a separate state, not a detail: the video is uploaded and sits on the
    channel visible to nobody until the person switches it. A forgotten private video
    looks like finished work, so the dashboard colours it differently."""
    rec = m.publish
    if rec is None or not rec.platforms:
        return None
    rows = sorted(rec.platforms.items())
    privacy = {p.privacy for _, p in rows if p.privacy}
    return {
        "state": rec.state,
        # one visibility for all platforms, while we upload the same file everywhere
        "privacy": next(iter(privacy)) if len(privacy) == 1 else "",
        "platforms": [
            {"network": net, "url": p.url, "at": p.at, "privacy": p.privacy} for net, p in rows
        ],
    }


def _scene_row(
    edir: Path, scene: Scene, translated: SceneTranslation | None = None
) -> dict[str, Any]:
    def url(rel: str | None) -> str | None:
        return file_url(edir / rel) if rel and (edir / rel).exists() else None

    return {
        "id": scene.id,
        "order": scene.order,
        "narration": scene.narration,
        "visual_prompt": scene.visual_prompt,
        "motion_prompt": scene.motion_prompt,
        # translation for reading (prompts.<lang>.json); None if missing or stale
        "translated": (
            {"visual": translated.visual, "motion": translated.motion} if translated else None
        ),
        "duration_sec": scene.duration_sec,
        "planned_sec": scene.planned_sec,
        "continues": scene.continues,
        # text plate over the frame: what the viewer will read (None if absent)
        "overlay": scene.overlay.model_dump() if scene.overlay else None,
        # motion reference (set by a person): file, signal, strength; None = prompt-driven
        "motion_reference": (
            scene.motion_reference.model_dump() if scene.motion_reference else None
        ),
        "cast": scene.cast,
        # scene composition references (found by the stage from a review verdict or
        # added by a person): path as in the storyboard plus a link to view it
        "references": [{"path": rel, "url": url(rel)} for rel in scene.reference_assets],
        # description fixes from review verdicts, appended to the prompt
        "fixes": scene.fixes,
        # review verdict on the description: what the model saw, what is wrong, what was done
        "review": scene.review["image"].model_dump() if "image" in scene.review else None,
        "checks": scene.checks.model_dump(),
        "attempts": scene.attempts,
        # attempt log where there was more than one: verdicts fluctuate, so the best attempt
        # is kept rather than the last (Scene.attempt_log). `path` as in the storyboard,
        # to pick another attempt by hand; `url` to view it
        "attempt_log": [
            {
                "kind": kind,
                "n": a.n,
                "passed": a.passed,
                "issues": len(a.issues),
                "notes": a.issues,
                "path": a.path,
                "url": url(a.path),
                "kept": a.path == getattr(scene.assets, kind, None),
            }
            for kind, log in scene.retried().items()
            for a in log
        ],
        "cost_usd": round(sum(scene.cost_usd.values()), 4),
        "source": scene.source.model_dump() if scene.source else None,
        "assets": {
            "image": url(scene.assets.image),
            "video": url(scene.assets.video),
            "audio": url(scene.assets.audio),
            "clip": url(scene.assets.clip),
            "thumbs": [u for u in (url(t) for t in scene.assets.thumbs) if u],
        },
    }


def _cast_cards(pid: str, sb: Storyboard) -> list[dict[str, Any]]:
    """Everyone taking part in the episode: scene casts plus the project's main character."""
    refs: list[str] = []
    if sb.resources.character:
        refs.append(f"characters/{sb.resources.character}")
    for scene in sb.scenes:
        for ref in scene.cast:
            if ref not in refs:
                refs.append(ref)
    dirs = search_dirs(project_dir(pid))
    cards = []
    for ref in refs:
        kind, _, rid = ref.partition("/")
        if kind not in KINDS:
            cards.append(missing_card(kind, rid))
            continue
        try:
            obj = _load(kind, rid, dirs)
        except FileNotFoundError:
            cards.append(missing_card(kind, rid))
            continue
        cards.append(resource_card(kind, obj, pid if obj._scope == "project" else None))
    return cards


def _qa_summary(edir: Path) -> dict[str, Any] | None:
    data = _read_json(edir / QA_FILE)
    if data is None:
        return None
    findings = data.get("findings") or []
    return {
        "llm": data.get("llm", ""),
        "generated_at": data.get("generated_at"),
        "findings": findings,
        "cuts": len(data.get("cuts") or []),
        "frames": len(data.get("frames") or []),
        "url": file_url(edir / QA_FILE),
    }


def episode_detail(pid: str, ep: str) -> dict[str, Any]:
    edir, sb, m = _episode_paths(pid, ep)
    summary = episode_summary(pid, ep)
    cfg: ProjectConfig | None
    try:
        cfg = load_project(pid)
    except (FileNotFoundError, ValueError):
        cfg = None
    checks: list[dict[str, Any]] = []
    if sb.final.video:
        rows = sb.final.checks(
            captions_expected=cfg.video.captions if cfg else True,
            music_expected=cfg.video.music if cfg else False,
            filmed=is_filmed(sb.scenes),
            fps_expected=sb.fps,
        )
        checks = [r._asdict() for r in rows]
    ledger = [
        {
            "at": e.at,
            "stage": e.stage,
            "provider": e.provider,
            "usd": e.usd,
            "scene_id": e.scene_id,
            "note": e.note,
        }
        for e in m.ledger
    ]
    # prompt translation into the project language, for reading, if present and fresh
    lang = sb.language
    translations = load_translations(edir, lang) if lang != "en" else None
    fresh = fresh_scenes(translations, sb) if translations else {}
    translation_info = (
        {
            "language": translations.language,
            "llm": translations.llm,
            "generated_at": translations.generated_at,
            "fresh": len(fresh),
            "stale": len(stale_scenes(translations, sb)),
        }
        if translations
        else None
    )
    return {
        **summary,
        "language": sb.language,
        "captions_language": sb.captions_language,
        "aspect_ratio": sb.aspect_ratio,
        "resources": sb.resources.model_dump(),
        "publish": sb.publish.model_dump(),
        "cast": _cast_cards(pid, sb),
        "scenes": [_scene_row(edir, s, fresh.get(s.id)) for s in sb.ordered_scenes()],
        "translations": translation_info,
        "final": sb.final.model_dump(),
        "final_checks": checks,
        "script_md": _text(edir / SCRIPT_FILE),
        "qa": _qa_summary(edir),
        "publish_result": _read_json(edir / PUBLISH_FILE),
        # where the video has already gone (manifest.publish); None if never published
        "publish_state": m.publish.model_dump() if m.publish else None,
        "providers": m.providers,
        "approvals": m.approvals,
        "ledger": ledger,
        "report_url": f"/api/projects/{pid}/episodes/{ep}/report",
        "path": str(edir),
    }


# ---------------------------------------------------------------- projects


def _project_spent(pid: str) -> float:
    total = 0.0
    for ep in list_episodes(pid):
        try:
            total += load_manifest(episode_dir(pid, ep)).spent_usd
        except (FileNotFoundError, ValueError):
            continue
    return round(total, 4)


def _account_brief(acc: Account) -> dict[str, Any]:
    return {
        "id": acc.id,
        "network": acc.network,
        "network_title": acc.network_title,
        "handle": acc.handle,
        "url": acc.url,
        "title": acc.title,
    }


def projects_overview() -> dict[str, Any]:
    """All projects: with accounts (live) and without (demo).

    Projects with `hidden: true` in project.yaml (samples, sandboxes) are excluded from
    the list and only reported in `hidden`, so the page can say they exist; their project
    page still works via a direct link.

    `kind` is included for every project: the page shows quality test projects (`test`)
    as a separate category, and they never count as live even with an account - their
    videos are reviewed by eye, not published."""
    accounts = list_accounts()
    items: list[dict[str, Any]] = []
    hidden: list[str] = []
    for pid in list_projects():
        try:
            cfg = load_project(pid)
        except (FileNotFoundError, ValueError) as exc:
            items.append({"id": pid, "name": pid, "error": str(exc), "status": "demo"})
            continue
        if cfg.hidden:
            hidden.append(pid)
            continue
        mine = [a for a in accounts if a.project == pid]
        items.append(
            {
                "id": pid,
                "name": cfg.name or pid,
                "description": cfg.description,
                "workflow": cfg.workflow,
                # channel | test: a test project is never live even with an account -
                # its videos are reviewed by eye, not published
                "kind": cfg.kind,
                "platforms": cfg.channel.platforms,
                "platform_titles": [
                    str(PLATFORMS[p]["title"]) for p in cfg.channel.platforms if p in PLATFORMS
                ],
                "language": cfg.channel.language,
                "aspect_ratio": cfg.video.aspect_ratio,
                "target_duration_sec": cfg.video.target_duration_sec,
                "status": "live" if mine and cfg.kind != TEST_KIND else "demo",
                "accounts": [_account_brief(a) for a in mine],
                "episodes": len(list_episodes(pid)),
                "spent_usd": _project_spent(pid),
                "budget_per_episode_usd": cfg.budget_per_episode_usd,
            }
        )
    # folders inside projects/ without project.yaml: the pipeline ignores them, but the
    # person should know they exist (benchmark sandbox, material without a project)
    known = set(list_projects())
    base = projects_dir()
    bench = bench_dir().resolve()
    others = (
        sorted(
            p.name
            for p in base.iterdir()
            if p.is_dir() and p.name not in known and p.resolve() != bench
        )
        if base.is_dir()
        else []
    )
    return {
        "items": items,
        "hidden": hidden,
        "other_folders": others,
        "bench_folder": bench.name,
    }


def _research_rows(cfg: ProjectConfig) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for slug in cfg.research:
        adir = analysis_dir(slug)
        channel = _read_json(adir / "01_collect" / "channel.json") or {}
        info = channel.get("channel") or {}
        blueprint = blueprint_path(slug)
        report = adir / REPORT_FILE
        rows.append(
            {
                "slug": slug,
                "exists": (adir / "analysis.yaml").exists(),
                "title": info.get("title") or slug,
                "url": info.get("url") or "",
                "subscribers": info.get("subscribers"),
                "blueprint_url": file_url(blueprint) if blueprint.exists() else None,
                "report_url": file_url(report) if report.exists() else None,
            }
        )
    return rows


def _source_groups(sdir: Path) -> list[dict[str, Any]]:
    """Source files by subfolder: no schema, as they are on disk. README is returned apart."""
    groups: list[dict[str, Any]] = []
    shown = 0
    folders = [sdir, *sorted(p for p in sdir.rglob("*") if p.is_dir())]
    for d in folders:
        files = sorted(
            (p for p in d.iterdir() if p.is_file() and not (d == sdir and p.name == "README.md")),
            key=lambda p: p.name.lower(),
        )
        if not files:
            continue
        entries = []
        for p in files:
            if shown >= MAX_SOURCE_FILES:
                break
            entries.append(file_entry(p))
            shown += 1
        groups.append(
            {"dir": d.relative_to(sdir).as_posix() if d != sdir else "", "files": entries}
        )
    return groups


def _sources_rows(pid: str) -> list[dict[str, Any]]:
    """Project sources: clip sources get metadata and moments, others the folder as is."""
    from videoforge.clips.highlights import load_highlights
    from videoforge.clips.source import load_source

    root = sources_dir(pid)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for sdir in sorted(p for p in root.iterdir() if p.is_dir()):
        row: dict[str, Any] = {
            "id": sdir.name,
            "kind": "folder",
            "path": str(sdir),
            "readme_md": _text(sdir / "README.md"),
            "groups": _source_groups(sdir),
        }
        try:
            info = load_source(sdir)
        except Exception as exc:  # noqa: BLE001 - show a broken source.json as a plain folder
            info = None
            row["error"] = str(exc)
        if info is not None:
            proxy = sdir / info.proxy if info.proxy else None
            row.update(
                kind="clips",
                title=info.title,
                url=info.url,
                file=info.file,
                channel=info.channel,
                duration_sec=info.duration_sec,
                language=info.language,
                word_count=info.word_count,
                transcriber=info.transcriber,
                chapters=len(info.chapters),
                heatmap=bool(info.heatmap),
                proxy_url=file_url(proxy) if proxy and proxy.exists() else None,
            )
            try:
                hl = load_highlights(sdir)
            except Exception:  # noqa: BLE001
                hl = None
            report = sdir / "highlights.html"
            row["highlights"] = (
                {
                    "candidates": len(hl.candidates),
                    "rejected": len(hl.rejected),
                    "generated_at": hl.generated_at,
                    "report_url": file_url(report) if report.exists() else None,
                }
                if hl
                else None
            )
        rows.append(row)
    return rows


CastIndex = dict[tuple[str, str], dict[str, dict[str, int]]]


def _cast_index() -> CastIndex:
    """(kind, id) -> {project -> {episode -> scenes}} from the casts of all storyboards.

    One pass over all episodes of all projects instead of a walk per card: the project
    page asks about each of its resources, and the answer is the same for all of them."""
    index: CastIndex = {}
    for pid in list_projects():
        for ep in list_episodes(pid):
            try:
                sb = load_storyboard(episode_dir(pid, ep))
            except Exception:  # noqa: BLE001 - a broken storyboard does not hide the others
                continue
            for scene in sb.scenes:
                for ref in scene.cast:
                    kind, _, rid = ref.partition("/")
                    per_project = index.setdefault((kind, rid), {})
                    per_episode = per_project.setdefault(pid, {})
                    per_episode[ep] = per_episode.get(ep, 0) + 1
    return index


def _slot_users() -> dict[tuple[str, str], set[str]]:
    """(kind, id) -> projects that have this resource in a project.yaml slot."""
    users: dict[tuple[str, str], set[str]] = {}
    for pid in list_projects():
        try:
            cfg = load_project(pid)
        except Exception:  # noqa: BLE001
            continue
        for slot, kind in SLOT_KIND.items():
            rid = getattr(cfg.resources, slot)
            if rid:
                users.setdefault((kind, rid), set()).add(pid)
    return users


def _has_own(pid: str, kind: str, rid: str) -> bool:
    """The project has its own resource with this id: its link resolves locally, not globally."""
    return (project_dir(pid) / "resources" / kind / f"{rid}.yaml").exists()


def _origin(
    card: dict[str, Any], pid: str, cast: CastIndex, slots: dict[tuple[str, str], set[str]]
) -> None:
    """Whether a resource belongs to or is used by the project, and where exactly.

    A global resource from `resources/` and a project one from `projects/<p>/resources/`
    in a project.yaml slot used to look the same ("project resources"), but the difference
    matters in practice: editing a global voice changes every project that uses it. So a
    global resource lists the other projects using it (`shared_with`; empty means "nowhere
    else"), and a visual one lists the episodes and number of scenes it is cast in
    (`used_in`; empty means defined but never on screen). A project with its OWN resource
    of the same id does not use the global one - its link resolves to its own file, and it
    is not listed in `shared_with`."""
    kind, rid = card["kind"], card["id"]
    if card["scope"] == "global":
        others = set(slots.get((kind, rid), ())) | set(cast.get((kind, rid), {}))
        card["shared_with"] = sorted(p for p in others if p != pid and not _has_own(p, kind, rid))
    else:
        card["shared_with"] = []
    if kind in VISUAL_KINDS:
        episodes = cast.get((kind, rid), {}).get(pid, {})
        card["used_in"] = [{"episode": ep, "scenes": n} for ep, n in sorted(episodes.items())]


def project_detail(pid: str) -> dict[str, Any]:
    try:
        cfg = load_project(pid)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    pdir = project_dir(pid)
    dirs = search_dirs(pdir)
    cast_index = _cast_index()
    slot_users = _slot_users()
    slots = []
    for slot, kind in SLOT_KIND.items():
        rid = getattr(cfg.resources, slot)
        if not rid:
            continue
        try:
            obj = _load(kind, rid, dirs)
            card = resource_card(kind, obj, pid if obj._scope == "project" else None)
            _origin(card, pid, cast_index, slot_users)
        except Exception:  # noqa: BLE001 - show a missing resource as missing
            card = missing_card(kind, rid, slot)
        card.update(slot=slot, slot_title=SLOT_TITLES[slot])
        slots.append(card)

    local: list[dict[str, Any]] = []
    rdir = pdir / "resources"
    if rdir.is_dir():
        for kind, ids in list_resources(rdir).items():
            for rid in ids:
                try:
                    card = resource_card(kind, _load(kind, rid, [rdir]), project=pid)
                    _origin(card, pid, cast_index, slot_users)
                    # a project resource that fills a slot: its card in the project's list says
                    # which role it plays, not just that it exists
                    for slot, slot_kind in SLOT_KIND.items():
                        if slot_kind == kind and getattr(cfg.resources, slot) == rid:
                            card.update(slot=slot, slot_title=SLOT_TITLES[slot])
                    local.append(card)
                except Exception as exc:  # noqa: BLE001
                    card = missing_card(kind, rid)
                    card.update(project=pid, error=str(exc))
                    local.append(card)

    mine = accounts_for_project(pid)
    episodes = []
    for ep in list_episodes(pid):
        try:
            episodes.append(episode_summary(pid, ep))
        except Exception as exc:  # noqa: BLE001 - a broken episode does not hide the others
            episodes.append({"id": ep, "project": pid, "ref": f"{pid}/{ep}", "error": str(exc)})

    return {
        "id": pid,
        "name": cfg.name or pid,
        "description": cfg.description,
        "workflow": cfg.workflow,
        "kind": cfg.kind,
        "status": "live" if mine and cfg.kind != TEST_KIND else "demo",
        "accounts": [_account_brief(a) for a in mine],
        "channel": cfg.channel.model_dump(),
        "platform_titles": [
            str(PLATFORMS[p]["title"]) for p in cfg.channel.platforms if p in PLATFORMS
        ],
        "video": cfg.video.model_dump(),
        "budget_per_episode_usd": cfg.budget_per_episode_usd,
        "scenes_estimate": cfg.scenes_estimate,
        "resources": slots,
        "local_resources": local,
        "research": _research_rows(cfg),
        "rules_md": load_rules_from_dir(pdir, cfg),
        "episodes": episodes,
        "sources": _sources_rows(pid),
        "workflow_exists": (workflows_dir() / f"{cfg.workflow}.yaml").exists(),
        "spent_usd": _project_spent(pid),
        "queue": queue_view(pid, live=bool(mine)),
        "path": str(pdir),
    }


# ---------------------------------------------------------------- queue and schedule


def _runway_dict(rw: q.Runway) -> dict[str, Any]:
    return {
        "ready": rw.ready,
        "waiting": rw.waiting,
        "unfinished": rw.unfinished,
        "per_day": rw.per_day,
        "ends": rw.ends,
        "days": rw.days,
        "level": rw.level,
    }


def queue_view(pid: str, *, live: bool | None = None) -> dict[str, Any]:
    """Queue, schedule, the "when -> what" plan, stock and warnings - all from
    `queue.yaml`, episode manifests and the tick trace (`queue.tick.json`).
    Recomputed on every request: the person edits the file, the page shows it."""
    if live is None:
        live = bool(accounts_for_project(pid))
    now = q.local_now()
    schedule = q.load_schedule(pid)
    last = q.load_tick(pid)
    return {
        "schedule": {
            "times": list(schedule.times),
            "paused": schedule.paused,
            "per_day": schedule.per_day,
            "active": schedule.active,
            "spread_min": schedule.spread_min,
        },
        "items": [
            {
                "episode": r.episode,
                "ref": f"{pid}/{r.episode}",
                "state": r.state,
                "state_title": q.STATE_TITLE[r.state],
                "title": r.title,
                "note": r.note,
            }
            for r in q.queue(pid)
        ],
        "plan": [
            {
                "at": s.at,
                "episode": s.item.episode if s.item else None,
                "ref": f"{pid}/{s.item.episode}" if s.item else None,
                "title": s.item.title if s.item else "",
                "pending": s.pending,
            }
            for s in q.plan(pid, now)
        ],
        "runway": _runway_dict(q.runway(pid, now)),
        "notices": [{"level": n.level, "text": n.text} for n in q.notices(pid, now, live=live)],
        "tick": None
        if last is None
        else {
            "at": last.at,
            "due": last.due,
            "slot": last.slot,
            "target": last.target,
            "handled": last.handled,
            "handled_at": last.handled_at,
            "attempts": last.attempts,
            "failed": last.failed,
            "item": last.item,
            "why": last.why,
            "stale": last.stale(now),
        },
        # tick step from the last trace: that is what the task itself uses, not the code
        "tick_every_min": last.every_min if last else q.TICK_EVERY_MIN,
        "live": live,
        "now": now,
        "path": str(q.queue_path(pid)),
    }


def update_schedule(pid: str, times: list[str], paused: bool) -> dict[str, Any]:
    """Dashboard schedule buttons - the same write to `queue.yaml` as
    `videoforge queue --times/--pause`. Episode order and comments in the file are left
    untouched. A bad time raises ValueError, which the route turns into 400."""
    try:
        load_project(pid)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    current = q.load_schedule(pid)  # the dashboard does not edit jitter; it stays as in the file
    q.save_schedule(
        pid, q.Schedule(times=q.parse_times(times), paused=paused, spread_min=current.spread_min)
    )
    return queue_view(pid)


def _runway_brief(project_id: str) -> dict[str, Any] | None:
    """Project stock for the account card, only when a schedule is set - for an account
    without slots there is no "how long will it last" to report."""
    if not project_id:
        return None
    try:
        schedule = q.load_schedule(project_id)
        if not schedule.times:
            return None
        rw = q.runway(project_id)
    except Exception:  # noqa: BLE001 - a broken project does not hide the account card
        return None
    return {**_runway_dict(rw), "paused": schedule.paused}


# ---------------------------------------------------------------- tests

TEST_KIND = "test"  # project.yaml kind: a quality test project, not a channel


def poster_path(video: Path) -> Path | None:
    """Video poster: `final.poster.jpg` next to the video.

    A gallery without images is useless, while the pipeline does not need a poster, so
    the frame is grabbed here, once: a file older than the video is regenerated, and no
    ffmpeg (or a broken file) just means no poster - the page carries on."""
    if not video.exists():
        return None
    poster = video.with_suffix(".poster.jpg")
    if poster.exists() and poster.stat().st_mtime >= video.stat().st_mtime:
        return poster
    for at in (1.0, 0.0):  # for videos shorter than a second, take the first frame at zero
        try:
            frame_at(video, at, poster)
        except (FfmpegError, OSError) as exc:
            log.warning("обложка для %s не снята: %s", video, exc)
            continue
        if poster.exists():
            return poster
    return None


def _test_card(pid: str, project_name: str, ep: str) -> dict[str, Any]:
    """Test video card: poster, what was tested, duration, when."""
    edir, sb, m = _episode_paths(pid, ep)
    video = edir / sb.final.video if sb.final.video else None
    ready = video is not None and video.exists()
    check = load_check(edir)
    poster = poster_path(video) if ready and video else None
    stages = _stage_rows(m)
    failed = [s["name"] for s in stages if s["status"] == "failed"]
    question = check_question(check)
    if not question and sb.brief.strip():  # no check.md: use the first line of the brief
        question = sb.brief.strip().splitlines()[0]
    return {
        "ref": f"{pid}/{ep}",
        "project": pid,
        "project_name": project_name,
        "episode": ep,
        "title": sb.title or sb.publish.title or ep,
        # one-line "what was tested": from check.md, otherwise from the episode brief
        "question": question,
        "check_md": check,
        "check_path": str(edir / CHECK_FILE),
        "duration_sec": _duration(sb),
        "made_at": (
            datetime.fromtimestamp(video.stat().st_mtime, UTC) if ready and video else m.created_at
        ),
        "video_url": file_url(video) if ready and video else None,
        "poster_url": file_url(poster) if poster else None,
        "aspect_ratio": sb.aspect_ratio,
        "scenes": len(sb.scenes),
        "spent_usd": m.spent_usd,
        "workflow": sb.workflow,
        "awaiting_approval": m.awaiting_approval,
        "failed_stages": failed,
        "stages": stages,
        "report_url": f"/api/projects/{pid}/episodes/{ep}/report",
        "path": str(edir),
    }


def tests_overview() -> dict[str, Any]:
    """Gallery of test videos: everything built in projects with `kind: test`.

    The page exists to open it and immediately see which videos there are and what each
    one tested. Finished ones (with a final) come first, newest on top; unfinished ones
    are listed separately so they are not lost."""
    projects: list[dict[str, Any]] = []
    ready: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for pid in list_projects():
        try:
            cfg = load_project(pid)
        except (FileNotFoundError, ValueError) as exc:
            log.warning("проект %s не прочитан: %s", pid, exc)
            continue
        if cfg.kind != TEST_KIND:
            continue
        name = cfg.name or pid
        projects.append(
            {
                "id": pid,
                "name": name,
                "description": cfg.description,
                "workflow": cfg.workflow,
                "episodes": len(list_episodes(pid)),
            }
        )
        for ep in list_episodes(pid):
            try:
                card = _test_card(pid, name, ep)
            except NotFound as exc:  # an episode without a storyboard must not break the page
                log.warning("эпизод %s/%s пропущен: %s", pid, ep, exc)
                continue
            (ready if card["video_url"] else pending).append(card)
    ready.sort(key=lambda c: c["made_at"], reverse=True)
    pending.sort(key=lambda c: c["made_at"], reverse=True)
    return {
        "projects": projects,
        "items": ready,
        "pending": pending,
        "total": len(ready) + len(pending),
    }


# ---------------------------------------------------------------- accounts


def _snapshot_dict(snapshot: Any) -> dict[str, Any] | None:
    return snapshot.model_dump() if snapshot is not None else None


def _account_face(acc: Account, videos: AccountVideos | None) -> dict[str, Any]:
    """What catches the eye first: the avatar and when something was last published."""
    avatar = avatar_path(acc)
    last = last_published(videos)
    days = (datetime.now(UTC) - last).days if last else None
    return {
        "avatar_url": file_url(avatar) if avatar.exists() else None,
        "last_published_at": last,
        "days_since_publish": days,
    }


def accounts_overview() -> dict[str, Any]:
    projects = list_projects()
    names: dict[str, str] = {}
    for pid in projects:
        try:
            names[pid] = load_project(pid).name or pid
        except Exception:  # noqa: BLE001
            names[pid] = pid
    items = []
    for acc in list_accounts():
        history = load_history(acc)
        items.append(
            {
                **_account_brief(acc),
                **_account_face(acc, load_videos(acc)),
                "project": acc.project,
                "project_name": names.get(acc.project, acc.project),
                "latest": _snapshot_dict(history[-1] if history else None),
                "known": latest_known(history),
                "snapshots": len(history),
                "problems": validate_account(acc, projects),
                # how many days the ready videos last at the project's slots: running out of stock
                # is visible on the first page, not deep inside the project
                "runway": _runway_brief(acc.project),
            }
        )
    networks = [
        {"id": key, "title": info.title, "platforms": list(info.platforms)}
        for key, info in NETWORKS.items()
    ]
    return {"networks": networks, "items": items}


SHORT_PLATFORMS = {"youtube_shorts", "tiktok", "instagram_reels"}
THUMBS_DIR = Path("05_assemble") / "thumbs"


def _published_episodes(pid: str) -> list[dict[str, Any]]:
    """Episodes that reached the publish stage: file, title and description are ready.
    This is "packaged", not "uploaded": where the video actually went is known by the
    manifest (`manifest.publish`), and `upload` is taken from there by the same builder
    as the project's episode list. Previously this expected a video_id in publish.json,
    which is never written there, so a channel's first video showed as "not uploaded"
    while it was publicly live."""
    rows: list[dict[str, Any]] = []
    if not pid:  # account without a project: nothing to show, no episode list to query
        return rows
    for ep in list_episodes(pid):
        edir = episode_dir(pid, ep)
        data = _read_json(edir / PUBLISH_FILE)
        if data is None:
            continue
        try:
            upload = _upload_state(load_manifest(edir))
        except Exception:  # noqa: BLE001 - a broken manifest does not hide the packaging
            upload = None
        video = data.get("video")
        platforms = data.get("platforms", [])
        duration = data.get("duration_sec")
        thumbs = sorted((edir / THUMBS_DIR).glob("*.jpg")) if (edir / THUMBS_DIR).is_dir() else []
        rows.append(
            {
                "episode": ep,
                "ref": f"{pid}/{ep}",
                "title": data.get("title", ""),
                "platforms": platforms,
                "duration_sec": duration,
                "generated_at": data.get("generated_at"),
                "video_url": file_url(edir / video) if video and (edir / video).exists() else None,
                "thumbnail_url": file_url(thumbs[0]) if thumbs else None,
                # short vs long by platform; without platforms judge by length, as YouTube does
                "is_short": bool(set(platforms) & SHORT_PLATFORMS)
                or (not platforms and duration is not None and duration <= 60),
                "uploaded": bool(upload and upload.get("state") == "done"),
                "upload": upload,
                "checks_failed": sum(1 for c in data.get("checks") or [] if not c.get("ok", True)),
            }
        )
    return rows


def account_detail(account_id: str) -> dict[str, Any]:
    try:
        acc = load_account(account_id)
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    videos = load_videos(acc)
    history = load_history(acc)
    project_name = acc.project
    if acc.project:  # empty means the account is not linked to anything, which is fine
        try:
            project_name = load_project(acc.project).name or acc.project
        except Exception:  # noqa: BLE001 - the project may be gone, the page is still needed
            pass
    return {
        **_account_brief(acc),
        **_account_face(acc, videos),
        "project": acc.project,
        "project_name": project_name,
        "channel_id": acc.channel_id,
        "notes": acc.notes,
        "problems": validate_account(acc),
        "history": [s.model_dump() for s in history],
        "known": latest_known(history),
        # official per-day stats (analytics.jsonl); empty without a token
        "analytics": [d.model_dump() for d in load_days(acc)],
        "videos": videos.model_dump() if videos else None,
        "published": _published_episodes(acc.project),
        # the project schedule lives here too: the account is opened first, and "when is
        # the next video" is asked of the account, not the project. An account without a
        # project has no schedule
        "queue": queue_view(acc.project, live=True) if acc.project else None,
        "path": str(acc._path),
    }


# ---------------------------------------------------------------- library


def _technique_dict(t: Technique) -> dict[str, Any]:
    data = t.model_dump()
    data["decision_title"] = DECISIONS.get(t.decision, t.decision)
    data["path"] = str(t._path)
    # a "before -> after" example: techniques are picked by eye, not by parameters
    ex = find_example(t)
    data["example"] = (
        {"kind": ex.kind, "after_url": file_url(ex.after), "before_url": file_url(ex.before)}
        if ex
        else None
    )
    return data


def library_overview() -> dict[str, Any]:
    items = load_all()
    decisions = []
    for key, title in DECISIONS.items():
        group = [t for t in items if t.decision == key]
        decisions.append(
            {
                "id": key,
                "title": title,
                "implemented": sum(1 for t in group if t.status == "implemented"),
                "recorded": sum(1 for t in group if t.status == "recorded"),
            }
        )
    unknown = sorted({t.decision for t in items} - set(DECISIONS))
    groups = [
        {"id": key, "title": title, "decisions": list(keys)}
        for key, (title, keys) in DECISION_GROUPS.items()
    ]
    return {
        "decisions": decisions,
        "groups": groups,
        "conditions": [{"key": k, "hint": v} for k, v in CONDITION_KEYS.items()],
        "unknown_decisions": unknown,
        "total": len(items),
    }


def library_list(decision: str) -> dict[str, Any]:
    if decision not in DECISIONS:
        raise NotFound(f"нет такой точки решения: {decision}")
    items = sorted(
        (t for t in load_all() if t.decision == decision),
        key=lambda t: (t.status != "implemented", t.id),
    )
    return {
        "id": decision,
        "title": DECISIONS[decision],
        "items": [_technique_dict(t) for t in items],
    }


def library_technique(decision: str, technique_id: str) -> dict[str, Any]:
    from videoforge.core.library import validate_technique

    for t in load_all():
        if t.decision == decision and t.id == technique_id:
            data = _technique_dict(t)
            data["problems"] = validate_technique(t)
            data["used_by_resources"] = technique_used_by(decision, technique_id)
            return data
    raise NotFound(f"приёма {decision}/{technique_id} нет")


# ---------------------------------------------------------------- plans and capabilities


def _feature_dict(f: Feature) -> dict[str, Any]:
    data = f.model_dump()
    data["status_title"] = f.status_title
    data["area_title"] = f.area_title
    data["topic_title"] = f.topic_title
    data["problems"] = validate_feature(f)
    data["path"] = str(f._path)
    return data


def todo_overview() -> dict[str, Any]:
    """Human tasks for the "Plans" page, as its first section.

    Kept separate from `features/`: those are tool capabilities, these are actions with an
    end ("sign in", "click", "review"). What they share is that a person looks at both
    from a phone, so they are shown side by side."""
    from videoforge.core.todo import load_tasks, todo_path

    tasks = load_tasks()
    # entry titles in one pass over features/: a task's link must read as words, not an
    # id; a deleted entry yields an empty title, not a 500
    titles = {f.id: f.title for f in list_features()} if any(t.feature for t in tasks) else {}
    rows = [_todo_row(t, titles) for t in tasks]
    return {
        "open": [r for r in rows if not r["done"]],
        "done": [r for r in rows if r["done"]],
        "path": str(todo_path()),
    }


def _todo_row(task: Task, titles: dict[str, str]) -> dict[str, Any]:
    return {
        "code": task.code,
        "text": task.text,
        "done": task.done,
        "tag": task.tag,
        "done_at": task.done_at,
        "feature": task.feature,
        "feature_title": titles.get(task.feature, "") if task.feature else "",
    }


def close_todo(code: str, *, done: bool = True) -> dict[str, Any]:
    """The dashboard "done" button - the same function as `videoforge todo done`.

    `hint` is the same hint the CLI prints: which features/ entry was waiting for this
    step. The dashboard only displays it and does not touch the entry."""
    from videoforge.core.todo import close_task, feature_hint

    task = close_task(code, done=done)
    if task is None:
        raise NotFound(f"задачи {code} нет")
    titles = {f.id: f.title for f in list_features()} if task.feature else {}
    return {**_todo_row(task, titles), "hint": feature_hint(task)}


def features_overview() -> dict[str, Any]:
    """All entries at once: the "Plans" and "Capabilities" pages split them by status
    themselves - the list is short, and both need the status and area dictionaries."""
    items = sorted(list_features(), key=sort_key)
    counts = {key: sum(1 for f in items if f.status == key) for key in STATUSES}
    return {
        "items": [_feature_dict(f) for f in items],
        "statuses": [{"id": k, "title": v} for k, v in STATUSES.items()],
        "areas": [{"id": k, "title": v} for k, v in AREAS.items()],
        "topics": [{"id": k, "title": v} for k, v in TOPICS.items()],
        "counts": counts,
        "total": len(items),
    }


def feature_detail(feature_id: str) -> dict[str, Any]:
    try:
        f = load_feature(feature_id)
    except (FileNotFoundError, ValueError) as exc:
        raise NotFound(str(exc)) from exc
    return _feature_dict(f)


# ---------------------------------------------------------------- useful sources


def _link_dict(link: Link) -> dict[str, Any]:
    data = link.model_dump()
    data["kind_title"] = link.kind_title
    data["area_title"] = link.area_title
    data["host"] = link.host
    data["problems"] = validate_link(link)
    data["path"] = str(link._path)
    return data


def links_overview() -> dict[str, Any]:
    """All bookmarks at once: the list is short, the page groups them by kind itself."""
    items = sorted(list_links(), key=link_sort_key)
    counts = {key: sum(1 for link in items if link.kind == key) for key in LINK_KINDS}
    return {
        "items": [_link_dict(link) for link in items],
        "kinds": [{"id": k, "title": v} for k, v in LINK_KINDS.items()],
        "areas": [{"id": k, "title": v} for k, v in AREAS.items()],
        "counts": counts,
        "total": len(items),
    }


def link_detail(link_id: str) -> dict[str, Any]:
    try:
        link = load_link(link_id)
    except (FileNotFoundError, ValueError) as exc:
        raise NotFound(str(exc)) from exc
    return _link_dict(link)
