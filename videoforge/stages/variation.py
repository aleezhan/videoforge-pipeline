"""Проверка раскадровки на однообразие — арифметика, а не мнение.

Ворота перед самой дорогой стадией. Раскадровка, где половина сцен описана
одной фразой, а камера везде «slow push in», превращается в слайдшоу с
озвучкой — и это видно по самому тексту раскадровки, до единого платного
вызова. Считать это должен python: перечитывать собственный ответ модель умеет
плохо, а лишний вызов стоит денег и ничего не добавляет.

Находки делятся так же, как уже устроен планнер: `block` уходит в текст ошибки
и модель отвечает второй раз (один вызов LLM дешевле пачки одинаковых кадров),
`warn` доходит до человека на воротах, который и решает.

Считаем по БИТАМ, а не по кадрам. Длинный бит planner режет на несколько сцен
одного плана (`Scene.continues`), и они копируют промпт законно — это один
удержанный кадр, а не повтор.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel

from videoforge.core.storyboard import Scene

Severity = Literal["block", "warn"]

# Слова, которыми модель заполняет пустоту вместо описания кадра. Каждое из них
# ничего не добавляет к картинке: художник по такой строке не нарисует ничего
# конкретного, и генератор тоже — он вернёт свою среднюю картинку по больнице.
GENERIC_PHRASES: frozenset[str] = frozenset(
    {
        "a person",
        "a professional",
        "a beautiful",
        "modern",
        "futuristic",
        "cutting-edge",
        "state-of-the-art",
        "next-generation",
        "revolutionary",
        "innovative",
        "sleek design",
        "elegant solution",
        "in today's world",
        "dynamic",
        "vibrant",
        "stunning",
        "breathtaking",
        "amazing",
        "incredible",
        "powerful",
        "seamless",
    }
)

# доля неповторяющихся описаний, ниже которой это уже не раскадровка
UNIQUE_VISUALS_BLOCK = 0.5
UNIQUE_VISUALS_WARN = 0.8
# доля битов с одним и тем же движением камеры
SAME_MOTION_BLOCK = 0.9
SAME_MOTION_WARN = 0.6
# доля описаний, опирающихся на пустые слова
GENERIC_BLOCK = 0.4
GENERIC_WARN = 0.15
# столько одинаковых кадров подряд — уже заметно зрителю
IDENTICAL_RUN_WARN = 3
# меньше этого числа битов судить не о чем: три сцены похожи по случайности
MIN_BEATS = 4


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str


def _norm(text: str) -> str:
    return re.sub(r"[^a-zа-яё0-9 ]+", " ", text.lower()).strip()


def _norm_words(text: str) -> str:
    return " ".join(_norm(text).split())


def beat_scenes(scenes: list[Scene]) -> list[Scene]:
    """Первые сцены каждого бита: продолжения одного плана — не повтор."""
    return [s for s in scenes if not s.continues]


def _generic_hits(text: str) -> list[str]:
    low = f" {_norm_words(text)} "
    return sorted(p for p in GENERIC_PHRASES if f" {_norm_words(p)} " in low)


def _longest_run(values: list[str]) -> int:
    """Длина самой длинной серии подряд идущих одинаковых значений.

    Именно серии: сумма одинаковых пар по всему списку посчитала бы
    wide,wide,cu,cu,med,med как «три подряд», хотя подряд там только по два."""
    longest = current = 1 if values else 0
    for i in range(1, len(values)):
        current = current + 1 if values[i] and values[i] == values[i - 1] else 1
        longest = max(longest, current)
    return longest


def check_variation(scenes: list[Scene]) -> list[Finding]:
    """Находки об однообразии раскадровки, самые тяжёлые первыми."""
    beats = beat_scenes(scenes)
    if len(beats) < MIN_BEATS:
        return []

    findings: list[Finding] = []
    total = len(beats)

    visuals = [_norm_words(s.visual_prompt) for s in beats]
    unique_share = len({v for v in visuals if v}) / total
    if unique_share < UNIQUE_VISUALS_BLOCK:
        findings.append(
            Finding(
                code="repeated_visuals",
                severity="block",
                message=(
                    f"разных описаний кадра всего {unique_share:.0%} от {total} битов — "
                    "остальное повторы; каждый повтор это отдельный оплаченный кадр, "
                    "который зритель уже видел"
                ),
            )
        )
    elif unique_share < UNIQUE_VISUALS_WARN:
        findings.append(
            Finding(
                code="repeated_visuals",
                severity="warn",
                message=f"разных описаний кадра {unique_share:.0%} от {total} битов",
            )
        )

    motions = [_norm_words(s.motion_prompt) for s in beats]
    named = [m for m in motions if m]
    if named:
        top, count = Counter(named).most_common(1)[0]
        share = count / total
        if share >= SAME_MOTION_BLOCK:
            findings.append(
                Finding(
                    code="same_motion",
                    severity="block",
                    message=(
                        f"движение «{top}» стоит в {count} битах из {total} ({share:.0%}) — "
                        "одинаковое движение во всех кадрах читается как слайдшоу, "
                        "а не как снятое видео"
                    ),
                )
            )
        elif share >= SAME_MOTION_WARN:
            findings.append(
                Finding(
                    code="same_motion",
                    severity="warn",
                    message=f"движение «{top}» повторяется в {count} битах из {total}",
                )
            )

    hits = {s.id: _generic_hits(s.visual_prompt) for s in beats}
    lazy = {sid: words for sid, words in hits.items() if words}
    if lazy:
        share = len(lazy) / total
        sample = ", ".join(sorted({w for words in lazy.values() for w in words})[:6])
        severity: Severity = "block" if share >= GENERIC_BLOCK else "warn"
        if share >= GENERIC_WARN or severity == "block":
            findings.append(
                Finding(
                    code="generic_language",
                    severity=severity,
                    message=(
                        f"{len(lazy)} из {total} описаний кадра держатся на пустых словах "
                        f"({sample}) — по ним не нарисовать конкретную картинку"
                    ),
                )
            )

    run = _longest_run(visuals)
    if run >= IDENTICAL_RUN_WARN:
        findings.append(
            Finding(
                code="identical_run",
                severity="warn",
                message=f"{run} битов подряд с одним описанием кадра",
            )
        )

    order = {"block": 0, "warn": 1}
    return sorted(findings, key=lambda f: order[f.severity])


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "block"]
