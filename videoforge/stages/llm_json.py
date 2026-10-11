"""Ответ модели → JSON. Общее для стадий, которые просят структуру, а не текст.

Тот же приём, что в анализаторе (`intel/stages/classify.py:_ask_json`): второй
заход с текстом ошибки. Больше двух попыток не делаем — если модель дважды не
смогла вернуть объявленную форму, дело не в удаче.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from videoforge.core.stage import StageContext


def strip_fences(text: str) -> str:
    """```json … ``` → …  Модели заворачивают ответ, даже когда просят не заворачивать."""
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        t = (t[first_nl + 1 :] if first_nl != -1 else "").rstrip()
        t = t[:-3] if t.endswith("```") else t
    return t.strip()


def ask_json(
    ctx: StageContext, prompt: str, *, system: str, images: list[Path] | None = None
) -> Any:
    """Два шанса получить валидный JSON; второй — с текстом ошибки.
    `images` — кадры на вход vision-модели («посмотри и ответь»)."""
    llm = ctx.providers.llm
    last_error = ""
    for attempt in range(2):
        suffix = f"\n\nYour previous answer was not valid JSON ({last_error})." if attempt else ""
        result = llm.complete(prompt + suffix, system=system, json_mode=True, images=images)
        ctx.add_cost(result.cost_usd, llm.name)
        try:
            return json.loads(strip_fences(result.text))
        except json.JSONDecodeError as exc:
            last_error = str(exc)[:100]
            ctx.log.warning("невалидный JSON от %s (попытка %d): %s", llm.name, attempt + 1, exc)
    raise ValueError(f"LLM {llm.name} так и не вернул валидный JSON: {last_error}")


def ask_text(ctx: StageContext, prompt: str, *, system: str) -> str:
    """Свободный текст (сценарий): форму проверять нечем, поэтому один заход."""
    llm = ctx.providers.llm
    result = llm.complete(prompt, system=system)
    ctx.add_cost(result.cost_usd, llm.name)
    return strip_fences(result.text)
