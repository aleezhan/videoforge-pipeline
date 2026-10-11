"""VTT-субтитры → плоский текст с таймкодами: «MM:SS реплика» на строку.

Автосубтитры YouTube дублируют строки (каждый кий показывает предыдущую и
новую строку, слова размечены inline-тегами <c>/<00:00:01.599>) — теги
режутся, повторы схлопываются.
"""

from __future__ import annotations

import re
from pathlib import Path

_TAG = re.compile(r"<[^>]+>")
_TIMING = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,]\d{3}\s+-->")


def _short_time(h: str, m: str, s: str) -> str:
    hours = int(h)
    return f"{hours}:{m}:{s}" if hours else f"{m}:{s}"


def vtt_to_text(path: Path, *, max_chars: int = 40_000) -> str:
    lines: list[str] = []
    seen_tail = ""  # последняя добавленная реплика — для схлопывания повторов
    current_time = ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        timing = _TIMING.match(line)
        if timing:
            current_time = _short_time(*timing.groups())
            continue
        if not line or line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        text = _TAG.sub("", line).strip()
        if not text or text == seen_tail:
            continue
        lines.append(f"{current_time} {text}" if current_time else text)
        seen_tail = text
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n[transcript truncated]"
    return result
