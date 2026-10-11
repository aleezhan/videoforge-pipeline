"""Перевод промптов для чтения: файл рядом с раскадровкой, свежесть по отпечатку."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videoforge.api.app import create_app
from videoforge.core.prompt_translation import (
    load_translations,
    stale_scenes,
    translation_path,
)
from videoforge.core.storyboard import load_storyboard, save_storyboard
from videoforge.stages.prompt_translate import translate_prompts


@dataclass
class _Result:
    text: str
    cost_usd: float = 0.0


class FakeLlm:
    """Переводит «RU: <оригинал>»; считает вызовы и помнит, сколько сцен просили."""

    name = "fake"

    def __init__(self, *, cost: float = 0.0, broken_first: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.cost = cost
        self.broken_first = broken_first

    def complete(
        self, prompt: str, *, system: str | None = None, json_mode: bool = False, images=None
    ) -> _Result:
        payload = prompt.split("INPUT", 1)[1].strip()
        # у повтора после JSON идёт хвост «Your previous answer…» — читаем только объект
        items, _ = json.JSONDecoder().raw_decode(payload)
        self.calls.append(list(items))
        if self.broken_first and len(self.calls) == 1:
            return _Result("not json at all", self.cost)
        out = {
            k: {"visual": f"RU: {v['visual']}", "motion": f"RU: {v['motion']}"}
            for k, v in items.items()
        }
        return _Result(json.dumps(out, ensure_ascii=False), self.cost)


def test_translate_is_idempotent_and_tracks_changes(episode: Path) -> None:
    llm = FakeLlm()
    data, done, cost = translate_prompts(episode, llm=llm, lang="ru")
    assert done == 2 and cost == 0.0 and len(llm.calls) == 1
    assert translation_path(episode, "ru").exists()
    assert data.scenes["s1"].visual == "RU: boy at the forest edge"
    assert data.scenes["s2"].motion == "RU: tilt up"

    # второй прогон — ничего не переводит
    _, done, _ = translate_prompts(episode, llm=llm, lang="ru")
    assert done == 0 and len(llm.calls) == 1

    # изменился промпт одной сцены → устарел только её перевод
    sb = load_storyboard(episode)
    sb.scenes[0].visual_prompt = "boy at the river"
    save_storyboard(episode, sb)
    stored = load_translations(episode, "ru")
    assert stored is not None and stale_scenes(stored, sb) == ["s1"]
    _, done, _ = translate_prompts(episode, llm=llm, lang="ru")
    assert done == 1 and llm.calls[-1] == ["s1"]
    assert load_translations(episode, "ru").scenes["s1"].visual == "RU: boy at the river"  # type: ignore[union-attr]

    # --force переводит все заново
    _, done, _ = translate_prompts(episode, llm=llm, lang="ru", force=True)
    assert done == 2


def test_translate_batches_and_retries(episode: Path) -> None:
    sb = load_storyboard(episode)
    sb.scenes[0].visual_prompt = ""
    sb.scenes[0].motion_prompt = ""  # пустая сцена не переводится
    save_storyboard(episode, sb)
    llm = FakeLlm(broken_first=True, cost=0.001)
    data, done, cost = translate_prompts(episode, llm=llm, lang="ru", batch=1)
    assert done == 1 and len(llm.calls) == 2  # первый ответ битый → повтор
    assert cost == pytest.approx(0.002)
    assert list(data.scenes) == ["s2"]
    from videoforge.core.manifest import load_manifest

    assert load_manifest(episode).stage_cost("translate_prompts") == pytest.approx(0.002)


def test_api_shows_fresh_translation_only(episode: Path, accounts_root: Path) -> None:
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    body = client.get("/api/projects/t/episodes/e1").json()
    assert body["translations"] is None and body["scenes"][0]["translated"] is None

    translate_prompts(episode, llm=FakeLlm(), lang="ru")
    body = client.get("/api/projects/t/episodes/e1").json()
    assert body["translations"]["fresh"] == 2 and body["translations"]["stale"] == 0
    assert body["scenes"][0]["translated"]["visual"] == "RU: boy at the forest edge"

    sb = load_storyboard(episode)
    sb.scenes[1].motion_prompt = "pan left"
    save_storyboard(episode, sb)
    body = client.get("/api/projects/t/episodes/e1").json()
    assert body["translations"]["stale"] == 1
    assert body["scenes"][0]["translated"] is not None and body["scenes"][1]["translated"] is None
