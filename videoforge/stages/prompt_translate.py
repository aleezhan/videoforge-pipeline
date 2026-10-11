"""Перевод промптов сцен моделью — вне конвейера, как `portfolio`.

`videoforge episode translate-prompts <проект>/<эпизод>` → `prompts.<lang>.json`.
Идемпотентно: переводятся только сцены, у которых перевода нет или промпт
изменился (отпечаток в `core/prompt_translation.py`). Сцены идут пачками в
одном вызове — на claude_cli время растёт с выводом, а не с числом вызовов,
поэтому 105 сцен = три вызова, не сто пять. Прогресс сохраняется после каждой
пачки: упавший вызов не теряет уже переведённое.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from videoforge.core.manifest import load_manifest, now, save_manifest
from videoforge.core.prompt_translation import (
    LANGUAGE_NAMES,
    PromptTranslations,
    SceneTranslation,
    load_translations,
    save_translations,
    scene_hash,
    translation_path,
)
from videoforge.core.storyboard import Scene, load_storyboard
from videoforge.providers.base import LlmProvider
from videoforge.stages.llm_json import strip_fences

BATCH = 40  # сцен в одном вызове: ~4 КБ вывода, модель не теряет строки

SYSTEM = (
    "You translate storyboard prompts for a video producer who reads the storyboard. "
    "Faithful, plain, concrete. No embellishment, no additions."
)

PROMPT = (
    "VIDEOFORGE-STEP: translate-prompts\n"
    """
Translate the storyboard prompts below into {language}. The reader is the producer:
they need to understand what is in the frame and how the camera moves, not to
generate anything. Each item has an id, a `visual` prompt (what is in the frame)
and a `motion` prompt (camera or subject movement).

Rules:
- translate meaning faithfully; keep it as short as the original;
- camera terms (push in, pan, tilt, close-up, dolly) — natural {language} wording
  a video editor uses;
- an empty string stays an empty string;
- do not add, judge or explain anything.

Return ONLY a JSON object with EVERY id from the input:
{{"<id>": {{"visual": "...", "motion": "..."}}, ...}}

INPUT
{items}
"""
)


def _chunks(items: list[Scene], size: int) -> list[list[Scene]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _ask(llm: LlmProvider, prompt: str) -> tuple[dict[str, object], float]:
    """Два шанса получить валидный JSON, как в `llm_json.ask_json`."""
    cost = 0.0
    last_error = ""
    for attempt in range(2):
        suffix = f"\n\nYour previous answer was not valid JSON ({last_error})." if attempt else ""
        result = llm.complete(prompt + suffix, system=SYSTEM, json_mode=True)
        cost += result.cost_usd
        try:
            parsed = json.loads(strip_fences(result.text))
        except json.JSONDecodeError as exc:
            last_error = str(exc)[:100]
            continue
        if isinstance(parsed, dict):
            return parsed, cost
        last_error = "top level is not an object"
    raise ValueError(f"LLM {llm.name} так и не вернул валидный JSON: {last_error}")


def translate_prompts(
    episode_dir: Path,
    *,
    llm: LlmProvider,
    lang: str = "ru",
    force: bool = False,
    batch: int = BATCH,
    log: Callable[[str], None] | None = None,
) -> tuple[PromptTranslations, int, float]:
    """Перевести устаревшие/отсутствующие переводы. Возвращает файл, число
    переведённых сцен и стоимость."""
    say = log or (lambda _msg: None)
    storyboard = load_storyboard(episode_dir)
    data = load_translations(episode_dir, lang) or PromptTranslations(language=lang)
    todo = [
        s
        for s in storyboard.ordered_scenes()
        if (s.visual_prompt.strip() or s.motion_prompt.strip())
        and (force or s.id not in data.scenes or data.scenes[s.id].source != scene_hash(s))
    ]
    if not todo:
        say("переводы свежие — переводить нечего")
        return data, 0, 0.0

    language = LANGUAGE_NAMES.get(lang, lang)
    total_cost = 0.0
    chunks = _chunks(todo, max(1, batch))
    for n, chunk in enumerate(chunks, start=1):
        say(f"перевод: пачка {n}/{len(chunks)} ({len(chunk)} сцен) → {llm.name}")
        items = json.dumps(
            {s.id: {"visual": s.visual_prompt, "motion": s.motion_prompt} for s in chunk},
            ensure_ascii=False,
            indent=1,
        )
        parsed, cost = _ask(llm, PROMPT.format(language=language, items=items))
        total_cost += cost
        missing = [s.id for s in chunk if not isinstance(parsed.get(s.id), dict)]
        if missing:
            raise ValueError(f"модель не вернула перевод для сцен: {', '.join(missing)}")
        for s in chunk:
            entry = parsed[s.id]
            assert isinstance(entry, dict)
            data.scenes[s.id] = SceneTranslation(
                visual=str(entry.get("visual") or ""),
                motion=str(entry.get("motion") or ""),
                source=scene_hash(s),
            )
        data.llm = llm.name
        data.generated_at = now()
        save_translations(episode_dir, data)  # прогресс после каждой пачки

    if total_cost > 0:
        manifest = load_manifest(episode_dir)
        manifest.add_cost(stage="translate_prompts", provider=llm.name, usd=total_cost)
        save_manifest(episode_dir, manifest)
    say(f"записан {translation_path(episode_dir, lang)}")
    return data, len(todo), total_cost
