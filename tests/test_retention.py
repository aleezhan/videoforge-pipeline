"""Кривая удержания в секунды и чтение текста YouTube Studio — без сети."""

from __future__ import annotations

import json
from pathlib import Path

from videoforge.core.accounts import RetentionPoint
from videoforge.core.retention import drops, episode_for_video, watching_at
from videoforge.providers.youtube_studio import parse_duration, parse_number, parse_studio


def _curve(values: list[float]) -> list[RetentionPoint]:
    n = len(values) - 1
    return [RetentionPoint(at=i / n, watching=v) for i, v in enumerate(values)]


def test_watching_at_interpolates_between_points() -> None:
    curve = _curve([1.0, 0.5, 0.3])
    assert watching_at(curve, 0.25) == 0.75
    assert watching_at(curve, 1.0) == 0.3
    assert watching_at([], 0.5) is None


def test_drops_are_in_seconds_and_touching_windows_merge() -> None:
    # 11 точек по 10%: крутой спад на 10–30%, пологий хвост, подъём (пересмотр)
    curve = _curve([1.0, 0.95, 0.6, 0.4, 0.38, 0.36, 0.36, 0.45, 0.44, 0.43, 0.43])
    found = drops(curve, 100.0, top=2)
    # первое окно 0–30 с (−0.6), второе встык 30–60 с (−0.04) — один спад
    assert len(found) == 1
    assert (found[0].start_sec, found[0].end_sec) == (0.0, 60.0)
    assert abs(found[0].lost - 0.64) < 1e-9


def test_rise_is_not_a_drop() -> None:
    assert drops(_curve([0.5, 0.6, 0.7, 0.8, 0.9]), 50.0) == []


def test_episode_for_video_reads_publish_record(tmp_path: Path) -> None:
    edir = tmp_path / "chan" / "episodes" / "ep1"
    edir.mkdir(parents=True)
    publish = {"platforms": {"youtube": {"video_id": "abc"}}}
    (edir / "manifest.json").write_text(json.dumps({"publish": publish}), encoding="utf-8")
    assert episode_for_video("abc", tmp_path) == "chan/ep1"
    assert episode_for_video("zzz", tmp_path) is None


def test_numbers_as_studio_prints_them() -> None:
    assert parse_number("138") == 138
    assert parse_number("3,0 %") == 3.0
    assert parse_number("1,2 тыс.") == 1200
    assert parse_number("1\u00a0234") == 1234
    assert parse_number("4\u202f612") == 4612  # узкий пробел Studio
    assert parse_number("12.5K") == 12500
    assert parse_number("Просмотры") is None
    assert parse_duration("3:51") == 231
    assert parse_duration("1:02:05") == 3725


def test_parse_studio_takes_the_value_after_the_label() -> None:
    # как печатает вкладка «Новый контент» на скриншоте канала 28.09.2026
    overview = "\n".join(
        [
            "Просмотры",
            "138",
            "CTR для значков видео",
            "3,0 %",
            "Средняя продолжительность просмотра",
            "3:51",
        ]
    )
    reach = "Показы\n4 612\nCTR для значков видео\n3,0 %"
    stats = parse_studio("vid", {"overview": overview, "reach": reach})
    assert (stats.views, stats.ctr_pct, stats.avg_view_sec) == (138, 3.0, 231.0)
    assert stats.impressions == 4612
    assert stats.text["reach"] == reach


def test_parse_studio_missing_numbers_stay_none() -> None:
    stats = parse_studio("vid", {"overview": "Страница не загрузилась"})
    assert stats.impressions is None and stats.ctr_pct is None and stats.views is None
