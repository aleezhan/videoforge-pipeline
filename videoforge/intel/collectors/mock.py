"""Заглушка коллектора: детерминированный канал с посаженными выбросами.

Данные формульные (без random) — тесты проверяют конкретные видео:
- long040 — большой хит (×20 к своей округе);
- long050 — хит поменьше (×6) рядом с long040: медиана соседей его не прячет;
- long010 — ранний хит (×5): против медианы ВСЕГО канала он бы потерялся,
  скользящая медиана соседей его находит;
- short12 — выброс среди shorts (×8), в свою группу, не к длинным видео.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from videoforge.intel.collectors.base import (
    Collector,
    CollectResult,
    TranscriptResult,
    VideoFileResult,
)
from videoforge.intel.models import ChannelData, ChannelSnapshot, VideoMeta

_START = datetime(2024, 1, 1, tzinfo=UTC)
_BOOSTS_LONG = {10: 5, 40: 20, 50: 6}
_BOOSTS_SHORT = {12: 8}


def _jitter(i: int) -> float:
    return 1.0 + 0.2 * math.sin(1.7 * i)


class MockCollector(Collector):
    name = "mock"

    def __init__(self) -> None:
        self.calls = 0  # тесты идемпотентности считают реальные обращения
        self.transcript_calls = 0
        self.video_calls = 0

    def download_video(
        self, video: VideoMeta, *, out_dir: Path, max_height: int = 480
    ) -> VideoFileResult | None:
        """Настоящий короткий mp4 из трёх цветных кусков — детектору склеек есть что найти."""
        from videoforge.core import ffmpeg

        self.video_calls += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "source.mp4"
        if path.exists():
            return VideoFileResult(path=path)
        inputs: list[str] = []
        for color in ("red", "green", "blue"):
            inputs += ["-f", "lavfi", "-t", "2", "-i", f"color=c={color}:s=160x120:r=10"]
        streams = "".join(f"[{i}:v]" for i in range(3))
        ffmpeg.run(
            [*inputs, "-filter_complex", f"{streams}concat=n=3:v=1:a=0[v]",
             "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", str(path)]
        )  # fmt: skip
        return VideoFileResult(path=path)

    def fetch_transcript(self, video: VideoMeta, *, out_dir: Path) -> TranscriptResult | None:
        self.transcript_calls += 1
        path = out_dir / "transcript.mock.vtt"
        path.write_text(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:03.000\n"
            f"Mock opening question about {video.id}?\n\n"
            "00:00:03.000 --> 00:00:08.000\n"
            "Mock context line.\n\n"
            "00:00:08.000 --> 00:00:15.000\n"
            "Mock escalation with details.\n",
            encoding="utf-8",
        )
        return TranscriptResult(path=path, language="mock")

    def collect_channel(self, url: str, *, limit: int = 0) -> CollectResult:
        self.calls += 1
        videos: list[VideoMeta] = []
        for i in range(60):  # длинные видео, раз в неделю, канал растёт
            views = int((1000 + 120 * i) * _jitter(i) * _BOOSTS_LONG.get(i, 1))
            videos.append(
                VideoMeta(
                    id=f"long{i:03d}",
                    url=f"{url}/long{i:03d}",
                    title=f"Длинное видео №{i}",
                    published_at=_START + timedelta(weeks=i),
                    duration_sec=900 + (i % 5) * 120,
                    views=views,
                    is_short=False,
                )
            )
        for i in range(20):  # shorts, раз в ~10 дней, свой масштаб просмотров
            views = int((5000 + 500 * i) * _jitter(i) * _BOOSTS_SHORT.get(i, 1))
            videos.append(
                VideoMeta(
                    id=f"short{i:02d}",
                    url=f"{url}/short{i:02d}",
                    title=f"Шортс №{i}",
                    published_at=_START + timedelta(days=3 + 10 * i),
                    duration_sec=45,
                    views=views,
                    is_short=True,
                )
            )
        videos.sort(key=lambda v: v.published_at or _START)
        total = len(videos)
        if limit > 0:
            videos = videos[-limit:]
        channel = ChannelSnapshot(
            id="mock_channel",
            url=url,
            title="Mock Channel",
            subscribers=15000,
            video_count=total,
            created_at=_START,
            collected_at=datetime.now(UTC),
            collector=self.name,
        )
        return CollectResult(data=ChannelData(channel=channel, videos=videos), cost_usd=0.0)
