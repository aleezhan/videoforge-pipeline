"""Task endpoints; mutations also use the app's same-origin middleware."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from videoforge.agent_tasks import engine, store

router = APIRouter(prefix="/api/agent-tasks")


class NewTask(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    brief: str = Field(min_length=1, max_length=20000)
    max_rounds: int = Field(default=3, ge=1, le=6)


class Action(BaseModel):
    revision: int = Field(ge=0)
    text: str = Field(default="", max_length=20000)


@router.get("")
def tasks() -> dict[str, Any]:
    return engine.listing()


@router.post("")
def create(body: NewTask) -> dict[str, Any]:
    if not body.title.strip() or not body.brief.strip():
        raise HTTPException(422, "Укажите название и задачу")
    return engine.public(store.create(body.title, body.brief, body.max_rounds))


@router.get("/{task_id}")
def detail(task_id: str) -> dict[str, Any]:
    try:
        return engine.get(task_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{task_id}/{action}")
def act(task_id: str, action: str, body: Action) -> dict[str, Any]:
    try:
        return engine.command(task_id, action, body.revision, body.text)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except store.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
