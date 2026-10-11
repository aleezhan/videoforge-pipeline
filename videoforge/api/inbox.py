"""Inbox files: `inbox/<day>/<file>` - a drop box between a phone and this PC.

Deliberately kept apart from the pipeline: this is for things to show or pick up
(a video to watch, a PDF to fix), not episode material. Episode material lives in
the episode folder; mixing the two means one day building a video from a random file.

Files are grouped by day rather than in one pile, because the folder is only ever asked
"what did I drop today" and "what was there yesterday". The day uses the machine's local
time (the one a person means by "today"), so day labels are computed here rather than in
the browser - otherwise a phone in another time zone would call today's folder yesterday's.

The file name comes from the phone and cannot be trusted: `_safe_name` keeps only the base
name without paths or characters Windows forbids. A name clash does not overwrite the file
but appends `-2`: a person re-uploads because the first attempt failed, and they, not the
file system, should find out about the duplicate.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from videoforge.api.data import NotFound, file_url, kind_of
from videoforge.paths import inbox_dir

DAY_FMT = "%Y-%m-%d"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# characters forbidden in Windows file names + control chars: the phone may send anything
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_NAME = 120  # characters; the tail of a long name adds nothing
README = "README.md"  # explains the folder to whoever opens it in Explorer

MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _root() -> Path:
    return inbox_dir()


def today() -> str:
    """Today by the machine's local clock - the folder is named after it."""
    return datetime.now().strftime(DAY_FMT)


def _safe_name(name: str) -> str:
    """Turn an uploaded name into a file name that is safe to write to disk.

    No paths survive (`../` and `C:\\`), nor forbidden characters; Windows silently
    strips leading/trailing dots and spaces, so we strip them ourselves - otherwise the
    file would end up with a different name than the one returned to the browser."""
    base = BAD_CHARS.sub("_", name.replace("\\", "/").split("/")[-1]).strip(" .")
    if not base:
        return "file"
    stem, dot, ext = base.rpartition(".")
    if dot and len(ext) <= 10 and stem:
        return stem[: MAX_NAME - len(ext) - 1] + "." + ext
    return base[:MAX_NAME]


def _day_dir(day: str) -> Path:
    if not DAY_RE.match(day):
        raise NotFound(f"это не день: {day}")
    return _root() / day


def reserve(name: str, *, day: str | None = None) -> Path:
    """Reserve a slot for a file: `inbox/<today>/<name>`, never overwriting an existing one.

    The file is created empty right here: the name is taken from this moment on, so two
    concurrent uploads with the same name never write into the same file."""
    safe = _safe_name(name)
    dday = _day_dir(day or today())
    dday.mkdir(parents=True, exist_ok=True)
    stem, dot, ext = safe.rpartition(".")
    if not dot:
        stem, ext = safe, ""
    for n in range(1, 1000):
        candidate = dday / (safe if n == 1 else f"{stem}-{n}{dot}{ext}")
        try:
            candidate.touch(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise NotFound(f"слишком много файлов с именем {safe}")


def entry(path: Path) -> dict[str, Any]:
    """A file for the page: view link, download link and path.

    The path (`rel`) is shown on purpose: drop a video from the phone, read the line, and
    that video can be opened by path in a conversation with Claude."""
    st = path.stat()
    url = file_url(path)
    root = _root().resolve()
    rel = path.resolve().relative_to(root).as_posix()
    return {
        "name": path.name,
        "rel": rel,
        "path": f"inbox/{rel}",
        "day": path.parent.name if path.parent.resolve() != root else "",
        "url": url,
        "download_url": f"{url}&download=1" if url else None,
        "kind": kind_of(path),
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime).astimezone(),
    }


def _files_of(folder: Path, *, root_level: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in folder.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        if root_level and p.name == README:  # the folder's own explanation, not a user file
            continue
        out.append(entry(p))
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out


def day_title(day: str) -> str:
    """Day label: "today", "yesterday" or "10 September"; year only if not the current one."""
    if not day:
        return "без даты"
    try:
        d = datetime.strptime(day, DAY_FMT).date()
    except ValueError:
        return day
    now = datetime.now().date()
    delta = (now - d).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "вчера"
    year = "" if d.year == now.year else f" {d.year}"
    return f"{d.day} {MONTHS[d.month - 1]}{year}"


def overview() -> dict[str, Any]:
    """The whole folder by day: newest day first, newest file first within a day.

    Files placed directly in `inbox/` bypassing the page (dragged in Explorer) are not
    hidden - they form an undated group. An invisible file in a drop box is worse than an
    extra group: the person is sure they put it there, while the page shows nothing."""
    root = _root()
    days: list[dict[str, Any]] = []
    if root.is_dir():
        loose = _files_of(root, root_level=True)
        if loose:
            days.append({"day": "", "title": day_title(""), "files": loose})
        for folder in sorted(root.iterdir(), reverse=True):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            files = _files_of(folder)
            if not files:
                continue
            days.append({"day": folder.name, "title": day_title(folder.name), "files": files})
    for d in days:
        d["count"] = len(d["files"])
        d["size"] = sum(f["size"] for f in d["files"])
    return {
        "days": days,
        "today": today(),
        "total": sum(d["count"] for d in days),
        "size": sum(d["size"] for d in days),
    }


def find(day: str, name: str) -> Path:
    """File by day and name; escaping the folder raises NotFound, not a neighbour's file."""
    root = _root().resolve()
    # "-" means the folder root: the file was placed bypassing the page and has no day.
    # An empty path segment is not routable (`/api/inbox//name`), so a marker is needed
    folder = root if day in ("", "-") else _day_dir(day)
    path = (folder / _safe_name(name)).resolve()
    if root not in path.parents or not path.is_file():
        raise NotFound(f"файла нет: {day}/{name}")
    return path


def delete(day: str, name: str) -> dict[str, Any]:
    """Delete a file; an emptied day folder goes too - empty folders only get in the way."""
    path = find(day, name)
    info = entry(path)
    path.unlink()
    folder = path.parent
    if folder != _root().resolve() and not any(folder.iterdir()):
        folder.rmdir()
    return {"deleted": info["path"]}
