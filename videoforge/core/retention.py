"""Кривая удержания ролика → где уходят зрители, в секундах готового файла.

YouTube отдаёт кривую долями ролика (100 точек), а монтажный лист и сценарий
живут секундами. Здесь только перевод и арифметика: сколько зрителей
досмотрело до 30-й секунды, до четвертей, и какие окна теряют больше всего.
Что в эти секунды звучит и видно — дело того, кто читает (`videoforge account
video` кладёт рядом слова монтажного листа), а вывод «почему ушли» — человека
или модели: число говорит где, а не почему.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from videoforge.core.accounts import RetentionPoint
from videoforge.paths import projects_dir

HOOK_SEC = 30.0  # «досмотрели до 30 с» — первый вопрос к любому ролику
DROP_WINDOW = 3  # окно спада, в точках кривой (1 точка = 1% ролика)


@dataclass(frozen=True)
class Drop:
    """Окно, где кривая падает круче всего: секунды и сколько зрителей потеряно
    (доля от начавших смотреть, а не от оставшихся)."""

    start_sec: float
    end_sec: float
    lost: float


def watching_at(curve: list[RetentionPoint], ratio: float) -> float | None:
    """Доля зрителей в точке ролика: линейно между соседними точками."""
    if not curve:
        return None
    points = sorted(curve, key=lambda p: p.at)
    if ratio <= points[0].at:
        return points[0].watching
    for a, b in zip(points, points[1:], strict=False):
        if a.at <= ratio <= b.at:
            span = b.at - a.at or 1.0
            return a.watching + (b.watching - a.watching) * (ratio - a.at) / span
    return points[-1].watching


def drops(curve: list[RetentionPoint], duration_sec: float, *, top: int = 5) -> list[Drop]:
    """Самые крутые спады без пересечений. Первое окно не исключается: уход на
    первых секундах — самый дорогой, и это вопрос к началу ролика, а не шум.
    Подъёмы (пересмотры) спадом не считаются."""
    points = sorted(curve, key=lambda p: p.at)
    windows: list[Drop] = []
    for i in range(len(points) - DROP_WINDOW):
        a, b = points[i], points[i + DROP_WINDOW]
        lost = a.watching - b.watching
        if lost > 0:
            windows.append(Drop(a.at * duration_sec, b.at * duration_sec, lost))
    chosen: list[Drop] = []
    for d in sorted(windows, key=lambda d: -d.lost):
        if any(d.start_sec < c.end_sec and c.start_sec < d.end_sec for c in chosen):
            continue
        chosen.append(d)
        if len(chosen) == top:
            break
    # окна, вставшие встык, — один спад: у короткого ролика окно 1.5 с, и
    # «7–9, 9–10, 10–12» читается как три беды там, где она одна
    merged: list[Drop] = []
    for d in sorted(chosen, key=lambda d: d.start_sec):
        if merged and d.start_sec - merged[-1].end_sec < 1e-6:
            last = merged.pop()
            d = Drop(last.start_sec, d.end_sec, last.lost + d.lost)
        merged.append(d)
    return merged


def episode_for_video(video_id: str, base: Path | None = None) -> str | None:
    """`<проект>/<эпизод>`, откуда уехал ролик: по `manifest.publish` всех
    эпизодов. Своей связи «ролик → эпизод» у аккаунта нет, и заводить её
    незачем — публикация её уже записала."""
    root = base or projects_dir()
    for manifest in sorted(root.glob("*/episodes/*/manifest.json")):
        try:
            publish = json.loads(manifest.read_text(encoding="utf-8")).get("publish") or {}
        except (OSError, ValueError):
            continue
        for record in (publish.get("platforms") or {}).values():
            if isinstance(record, dict) and record.get("video_id") == video_id:
                edir = manifest.parent
                return f"{edir.parent.parent.name}/{edir.name}"
    return None
