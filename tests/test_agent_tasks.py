"""Approval, questions, races and interrupted workers. Never call paid agents."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videoforge.agent_tasks import engine, store
from videoforge.api.app import create_app


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("VF_AGENT_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(engine, "capabilities", lambda: {"claude": True, "codex": True})
    monkeypatch.setattr(engine, "launch", lambda *_: None)
    return TestClient(create_app(ui_dist=tmp_path / "none"))


def reply(decision: str = "continue", plan: str = "", questions: list[str] | None = None):
    return engine.Reply.model_validate(
        {"decision": decision, "text": "Проверено", "plan": plan, "questions": questions or []}
    )


def task():
    return store.create("Проверка", "Сделай согласованное изменение", 2)


def start(t):
    engine.command(t["id"], "start", t["revision"])
    return store.read(t["id"])


def test_discussion_cannot_enter_work_without_human_approval(setup, monkeypatch):
    phases = []

    def call(t, token):
        phases.append(t["phase"])
        return reply("agree" if t["phase"] == "codex_plan" else "continue", "План A")

    monkeypatch.setattr(engine, "call", call)
    t = start(task())
    engine.run(t["id"], t["worker_token"])
    current = store.read(t["id"])
    assert phases == ["claude_understand", "codex_understand", "claude_plan", "codex_plan"]
    assert current["status"] == "awaiting_approval"
    assert current["approved_plan"] == ""
    approved = engine.command(t["id"], "approve", current["revision"])
    assert approved["phase"] == "claude_work"
    assert approved["approved_plan"] == "План A"


def test_question_stops_and_answer_resumes_same_step(setup, monkeypatch):
    monkeypatch.setattr(engine, "call", lambda *_: reply("question", questions=["Какой формат?"]))
    t = start(task())
    engine.run(t["id"], t["worker_token"])
    current = store.read(t["id"])
    assert current["status"] == "awaiting_answer"
    assert current["phase"] == "claude_understand"
    answered = engine.command(t["id"], "message", current["revision"], "Вертикальный")
    assert answered["status"] == "discussing"
    assert answered["question"] == ""
    assert answered["messages"][-1]["text"] == "Вертикальный"


def test_only_one_of_two_concurrent_approvals_succeeds(setup):
    t = task()
    with store.locked():
        t.update(status="awaiting_approval", plan="План", phase="codex_plan")
        store.save(t)

    def approve(_):
        try:
            engine.command(t["id"], "approve", t["revision"])
            return "success"
        except store.Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(approve, range(2))) == ["conflict", "success"]


def test_replan_invalidates_approval_and_stale_revision(setup):
    t = task()
    with store.locked():
        t.update(status="awaiting_approval", plan="Старый план", phase="codex_plan")
        store.save(t)
    updated = engine.command(t["id"], "message", t["revision"], "Измени цель")
    assert updated["status"] == "discussing" and not updated["approved_plan"]
    with pytest.raises(store.Conflict):
        engine.command(t["id"], "approve", t["revision"])


def test_worker_never_completes_on_stale_answer_after_user_steering(setup, monkeypatch):
    t = start(task())
    with store.locked():
        t.update(phase="codex_review", approved_plan="План", status="working", last_work_done=True)
        store.save(t)
    calls = []

    def call(current, token):
        calls.append(current)
        if len(calls) == 1:
            engine.command(t["id"], "message", current["revision"], "Есть важное уточнение")
            return reply("agree")
        return reply("question", questions=["Уточни изменение"])

    monkeypatch.setattr(engine, "call", call)
    engine.run(t["id"], t["worker_token"])
    assert len(calls) == 2
    assert store.read(t["id"])["status"] == "awaiting_answer"


def test_disagreement_is_bounded(setup, monkeypatch):
    monkeypatch.setattr(engine, "call", lambda *_: reply("changes", "План"))
    t = start(task())
    engine.run(t["id"], t["worker_token"])
    current = store.read(t["id"])
    assert current["status"] == "paused" and current["round"] == 2


def test_review_requires_claude_completion_too(setup):
    t = task()
    t.update(phase="codex_review", status="working", approved_plan="План", last_work_done=False)
    engine.advance(t, reply("agree"))
    assert t["status"] == "working" and t["phase"] == "claude_work"
    t.update(phase="codex_review", last_work_done=True)
    engine.advance(t, reply("agree"))
    assert t["status"] == "completed"


def test_stop_prevents_even_first_agent_call(setup, monkeypatch):
    def fail(*_):
        pytest.fail("agent called after stop")

    monkeypatch.setattr(engine, "call", fail)
    t = start(task())
    engine.command(t["id"], "stop", t["revision"])
    engine.run(t["id"], t["worker_token"])
    assert store.read(t["id"])["status"] == "paused"


def test_dead_worker_pauses_without_automatic_retry(setup):
    t = start(task())
    with store.locked():
        t["heartbeat"] = time.time() - 100
        store.save(t)
    assert engine.get(t["id"])["status"] == "paused"
    assert not store.read(t["id"])["worker_token"]


def test_second_task_cannot_start_in_same_checkout(setup):
    start(task())
    second = task()
    with pytest.raises(store.Conflict):
        start(second)
    assert store.read(second["id"])["status"] == "draft"


def test_api_blocks_cross_origin_and_invalid_state(setup):
    response = setup.post(
        "/api/agent-tasks",
        json={"title": "T", "brief": "B"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    t = setup.post("/api/agent-tasks", json={"title": "T", "brief": "B"}).json()
    assert (
        setup.post(
            f"/api/agent-tasks/{t['id']}/approve", json={"revision": t["revision"]}
        ).status_code
        == 409
    )
    assert setup.get("/api/agent-tasks/unknown").status_code == 404
    assert "worker_token" not in setup.get(f"/api/agent-tasks/{t['id']}").json()


def test_planning_cli_has_no_write_tools(setup, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "executable", lambda name: name)
    args = engine.arguments("claude", "claude_plan", tmp_path)
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
    assert "--safe-mode" in args
    args = engine.arguments("codex", "codex_review", tmp_path)
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert not any("bypass" in a for a in args)
    assert json.loads((tmp_path / "reply.schema.json").read_text())["type"] == "object"


def test_independent_codex_interpretation_does_not_see_claude(setup):
    t = task()
    t["phase"] = "codex_understand"
    store.message(t, "claude", "SECRET_FIRST_OPINION")
    assert "SECRET_FIRST_OPINION" not in engine.prompt(t)
