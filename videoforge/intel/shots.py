"""Математика ритма монтажа: таймкоды склеек → статистика планов.

Без сети, без LLM, без файлов — как и statistics.py, только про кадр, а не про
просмотры. Отсюда берётся `scene_duration_sec` для наших проектов: сейчас это
число выбрано на глаз, а должно приходить из замера успешных роликов ниши.

Плотность считается по пяти равным отрезкам ролика: важно не среднее по всему
видео, а растёт ли темп к кульминации — именно это отличает удерживающий
монтаж от ровного.
"""

from __future__ import annotations

import statistics as st

from videoforge.intel.models import ShotStats

SECTIONS = 5  # на сколько равных отрезков делим ролик для профиля плотности
MERGE_GAP_SEC = 0.4  # сработки ближе этого — одна смена картинки, а не несколько


def _percentile(values: list[float], q: float) -> float:
    """q-й процентиль по ближайшему рангу: выборка маленькая, интерполяция лишняя."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def merge_cuts(cuts: list[float], *, gap: float = MERGE_GAP_SEC) -> list[float]:
    """Схлопнуть очередь сработок в одну смену картинки.

    На быстром движении детектор срабатывает несколько раз подряд. Такие
    сработки надо объединять, а не выбрасывать получившиеся короткие планы:
    выброшенный план не исчезает из ролика, он лишь задирает среднюю длину."""
    kept: list[float] = []
    for t in sorted(cuts):
        if not kept or t - kept[-1] >= gap:
            kept.append(t)
    return kept


def shot_lengths(cuts: list[float], duration: float) -> list[float]:
    """Границы планов → их длительности, включая первый и последний план."""
    inside = merge_cuts([t for t in cuts if 0 < t < duration])
    bounds = [0.0, *inside, duration]
    return [b - a for a, b in zip(bounds, bounds[1:], strict=False) if b > a]


def density_profile(cuts: list[float], duration: float, *, sections: int = SECTIONS) -> list[float]:
    """Склеек в минуту по равным отрезкам: ускоряется ли монтаж к финалу."""
    if duration <= 0:
        return []
    span = duration / sections
    buckets = [0] * sections
    for t in merge_cuts([t for t in cuts if 0 <= t < duration]):
        buckets[min(sections - 1, int(t / span))] += 1
    return [round(n / (span / 60), 1) for n in buckets]


def compute_shot_stats(cuts: list[float], duration: float, *, threshold: float = 0.0) -> ShotStats:
    lengths = shot_lengths(cuts, duration)
    if not lengths:
        return ShotStats(duration_sec=round(duration, 1), threshold=threshold)
    return ShotStats(
        duration_sec=round(duration, 1),
        threshold=threshold,
        shot_count=len(lengths),
        mean_shot_sec=round(st.mean(lengths), 2),
        median_shot_sec=round(st.median(lengths), 2),
        p90_shot_sec=round(_percentile(lengths, 0.9), 2),
        cuts_per_min=round(len(lengths) / (duration / 60), 1) if duration > 0 else 0.0,
        density=density_profile(cuts, duration),
    )
