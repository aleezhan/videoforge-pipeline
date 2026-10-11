"""Текстовые шаблоны (videoforge/templates/). Заполняются через string.Template,
чтобы в YAML/Markdown сохранялись комментарии — их читает человек."""

from __future__ import annotations

import json
import string

from videoforge.paths import templates_dir


def render(name: str, values: dict[str, object]) -> str:
    text = (templates_dir() / name).read_text(encoding="utf-8")
    return string.Template(text).safe_substitute({k: str(v) for k, v in values.items()})


def yaml_str(value: str) -> str:
    """Строка в двойных кавычках, безопасная для YAML (JSON-escape совместим)."""
    return json.dumps(value, ensure_ascii=False)


def yaml_opt(value: str | None) -> str:
    return "null" if value is None else value
