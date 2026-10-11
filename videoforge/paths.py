"""Filesystem layout. All paths derive from here and can be overridden via env.

Functions rather than constants, because tests change the environment on the fly.

projects/<project>/project.yaml      project (channel/series) settings
projects/<project>/rules.md          rules for the director and the planner
projects/<project>/episodes/<ep>/    episode: storyboard.json, manifest.json, stage folders
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROJECT_FILE = "project.yaml"
EPISODES_DIR = "episodes"

# Episode folders and files read by more than their own stage: names live here so
# different layers never end up with two spellings.
ASSEMBLE_DIR = "05_assemble"
FINAL_FILE = "final.mp4"
PUBLISH_DIR = "06_publish"
PUBLISH_DATA_FILE = "publish.json"


def resources_dir() -> Path:
    return Path(os.environ.get("VF_RESOURCES_DIR", ROOT / "resources"))


def workflows_dir() -> Path:
    return Path(os.environ.get("VF_WORKFLOWS_DIR", ROOT / "workflows"))


def projects_dir() -> Path:
    return Path(os.environ.get("VF_PROJECTS_DIR", ROOT / "projects"))


def research_dir() -> Path:
    """Analyses of external content: research/<slug>/, one per channel or collection."""
    return Path(os.environ.get("VF_RESEARCH_DIR", ROOT / "research"))


def analysis_dir(slug: str) -> Path:
    return research_dir() / slug


FORMULA_DIR = "05_formula"
BLUEPRINT_FILE = "blueprint.md"


def blueprint_path(slug: str) -> Path:
    """Production brief of an analysis, read by both the planner and /new-project.

    The names live here rather than in intel/ because the core references the brief
    and must not depend on the analyser."""
    return analysis_dir(slug) / FORMULA_DIR / BLUEPRINT_FILE


def playbook_path() -> Path:
    """Collection of rules that held up across several channels."""
    return research_dir() / "playbook.md"


def playbook_json_path() -> Path:
    """The same rules as data (counters and checks). playbook.md is generated from
    it, and the core selects channel rules for prompts from it (core/playbook.py)."""
    return research_dir() / "playbook.json"


def library_dir() -> Path:
    """Technique library: library/<decision point>/<id>.yaml.

    Next to research/, not inside it: the playbook collects content rules for the
    writer, while the library covers execution and feeds the render stages."""
    return Path(os.environ.get("VF_LIBRARY_DIR", ROOT / "library"))


def research_index_path() -> Path:
    """Index of all analyses: the starting point when looking for a suitable niche."""
    return research_dir() / "index.md"


def bench_dir() -> Path:
    """Benchmark area: projects/_bench. It has no project.yaml: it is a test folder
    (scripts, logs, CSVs of prices and timings, outputs in out/<test>/), not a
    project. The UI shows it as-is; there is deliberately no results schema."""
    return Path(os.environ.get("VF_BENCH_DIR", projects_dir() / "_bench"))


def ui_dir() -> Path:
    """UI frontend (Vite + React): sources in ui/, build in ui/dist."""
    return ROOT / "ui"


def ui_dist_dir() -> Path:
    return ui_dir() / "dist"


def accounts_dir() -> Path:
    """Platform accounts: accounts/<id>.yaml plus accounts/<id>/ with stats snapshots.
    Separate from resources/: a resource is what a video is made from, an account
    is where it goes; a project without an account is treated as a demo."""
    return Path(os.environ.get("VF_ACCOUNTS_DIR", ROOT / "accounts"))


def features_dir() -> Path:
    """Plans and features: features/<id>.yaml, one record per tool capability from
    idea to done. The dashboard shows unfinished ones as plans and finished ones as
    features; it is one format, not two lists."""
    return Path(os.environ.get("VF_FEATURES_DIR", ROOT / "features"))


def links_dir() -> Path:
    """Useful external links: links/<id>.yaml (a site, service, channel or article).
    A separate folder rather than part of features/, which holds the tool's own
    capabilities; these are third-party things we only look at."""
    return Path(os.environ.get("VF_LINKS_DIR", ROOT / "links"))


def inbox_dir() -> Path:
    """Incoming files: `inbox/<day>/<file>`, an exchange folder between a phone and
    this PC. Unrelated to the pipeline and deliberately kept beside it: it holds
    things to view or pick up, not episode material. Organised by day because
    "what did I drop here yesterday" is the only question asked of such a folder."""
    return Path(os.environ.get("VF_INBOX_DIR", ROOT / "inbox"))


def secrets_dir() -> Path:
    """Secrets that do not fit in a `.env` line: the YouTube OAuth token (an object
    with client_id/refresh_token), session cookies. The folder is git-ignored; its
    contents are never printed, only whether a file exists."""
    path = Path(os.environ.get("VF_SECRETS_DIR", ROOT / ".secrets"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_dir(project_id: str) -> Path:
    return projects_dir() / project_id


def project_config_path(project_id: str) -> Path:
    return project_dir(project_id) / PROJECT_FILE


def episodes_dir(project_id: str) -> Path:
    return project_dir(project_id) / EPISODES_DIR


def episode_dir(project_id: str, episode_id: str) -> Path:
    return episodes_dir(project_id) / episode_id


SOURCES_DIR = "sources"


def sources_dir(project_id: str) -> Path:
    """Clip sources: a long video and its transcript live at the project level,
    because one long recording yields many short episodes."""
    return project_dir(project_id) / SOURCES_DIR


def source_dir(project_id: str, source_id: str) -> Path:
    return sources_dir(project_id) / source_id


def templates_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def models_dir() -> Path:
    """Models bundled in the repository: currently only the YuNet face detector (MIT,
    ~230 KB) for clip framing. Weights are vendored rather than downloaded on demand
    so the stage depends on neither the network nor a third-party link staying alive."""
    return Path(__file__).resolve().parent / "assets" / "models"


def fonts_dir() -> Path:
    """Subtitle fonts bundled in the repository (OFL, with Cyrillic support).

    The ffmpeg build lacks fontconfig, so libass gets the folder explicitly
    (`fontsdir=`): rendering is identical on any machine regardless of the fonts
    installed on the system."""
    return Path(__file__).resolve().parent / "assets" / "fonts"
