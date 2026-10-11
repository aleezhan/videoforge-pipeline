"""Persistent task state. Every read/compare/write uses the same OS lock."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videoforge.paths import ROOT

ACTIVE = {"discussing", "working"}
_mutex = threading.RLock()


class Conflict(ValueError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def execution_lock() -> Iterator[None]:
    """A second worker cannot edit the shared checkout, even after a stale heartbeat."""
    root().mkdir(parents=True, exist_ok=True)
    with (root() / ".execution.lock").open("a+b") as stream:
        busy = "Предыдущий исполнитель ещё работает. Дождитесь остановки."
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Conflict(busy) from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # type: ignore[import-not-found,unused-ignore]

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined,unused-ignore]
            except OSError:
                raise Conflict(busy) from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)  # type: ignore[attr-defined,unused-ignore]


def root() -> Path:
    return Path(os.environ.get("VF_AGENT_TASKS_DIR", ROOT / ".agent-tasks"))


def directory(task_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise FileNotFoundError("Задача не найдена")
    return root() / task_id


@contextmanager
def locked() -> Iterator[None]:
    with _mutex:
        root().mkdir(parents=True, exist_ok=True)
        # Never unlink a lock file: another process may already have it open.
        with (root() / ".state.lock").open("a+b") as stream:
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + 10
                while True:
                    try:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise Conflict("Хранилище занято, повторите запрос") from None
                        time.sleep(0.02)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # type: ignore[import-not-found,unused-ignore]

                fcntl.flock(stream, fcntl.LOCK_EX)  # type: ignore[attr-defined,unused-ignore]
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)  # type: ignore[attr-defined,unused-ignore]


def read(task_id: str) -> dict[str, Any]:
    path = directory(task_id) / "task.json"
    if not path.is_file():
        raise FileNotFoundError("Задача не найдена")
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def save(task: dict[str, Any]) -> None:
    folder = directory(task["id"])
    folder.mkdir(parents=True, exist_ok=True)
    task["revision"] += 1
    task["updated_at"] = now()
    temporary = folder / f".{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, folder / "task.json")


def message(task: dict[str, Any], role: str, text: str, kind: str = "message") -> None:
    task["messages"].append(
        {"id": uuid.uuid4().hex, "at": now(), "role": role, "text": text, "kind": kind}
    )


def check_revision(task: dict[str, Any], revision: int) -> None:
    if task["revision"] != revision:
        raise Conflict("Задача обновилась. Обновите страницу и повторите действие.")


def create(title: str, brief: str, max_rounds: int) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "title": title.strip(),
        "brief": brief.strip(),
        "status": "draft",
        "revision": 0,
        "phase": "claude_understand",
        "plan": "",
        "messages": [],
        "question": "",
        "error": "",
        "round": 0,
        "max_rounds": max_rounds,
        "created_at": now(),
        "updated_at": now(),
        "approved_plan": "",
        "worker_token": "",
        "heartbeat": 0,
        "stop_requested": False,
        "last_work_done": False,
    }
    message(task, "user", brief.strip(), "brief")
    with locked():
        save(task)
    return task


def all_tasks() -> list[dict[str, Any]]:
    tasks = []
    if root().exists():
        for path in root().glob("*/task.json"):
            try:
                tasks.append(read(path.parent.name))
            except (ValueError, OSError):
                continue
    return sorted(tasks, key=lambda t: t["created_at"], reverse=True)


def recover(task: dict[str, Any]) -> None:
    if task["status"] in ACTIVE and time.time() - task.get("heartbeat", 0) > 90:
        task["status"] = "paused"
        task["worker_token"] = ""
        task["error"] = "Связь с исполнителем потеряна. Проверьте изменения перед продолжением."
        message(task, "system", task["error"], "interrupted")
        save(task)
