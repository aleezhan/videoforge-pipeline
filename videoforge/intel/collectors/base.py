"""Интерфейс коллекторов. Стадии анализа зависят только от него.

Коллектор — тот же провайдер, что в providers/base: адаптер внешнего сервиса,
который возвращает данные и стоимость вызова (0 для бесплатных вроде yt-dlp).
Сменить yt-dlp на YouTube Data API или платный сервис — один файл в этой папке.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from videoforge.intel.models import ChannelData, VideoMeta


class CollectorError(RuntimeError):
    pass


@dataclass
class CollectResult:
    data: ChannelData
    cost_usd: float = 0.0


@dataclass
class TranscriptResult:
    path: Path  # субтитры (vtt) на диске
    language: str = ""
    cost_usd: float = 0.0


@dataclass
class VideoFileResult:
    path: Path  # сам файл видео на диске
    cost_usd: float = 0.0


class Collector(ABC):
    name: ClassVar[str] = "?"
    price_per_channel_usd: float = 0.0  # прогноз для бюджетного стража

    @abstractmethod
    def collect_channel(self, url: str, *, limit: int = 0) -> CollectResult:
        """Снапшот канала и метаданные видео. limit > 0 — только N последних."""
        ...

    @abstractmethod
    def fetch_transcript(self, video: VideoMeta, *, out_dir: Path) -> TranscriptResult | None:
        """Субтитры видео (ручные или авто) в out_dir; None — субтитров нет."""
        ...

    def download_video(
        self, video: VideoMeta, *, out_dir: Path, max_height: int = 480
    ) -> VideoFileResult | None:
        """Сам файл видео в out_dir; None — коллектор не умеет или видео недоступно.

        Нужен только визуальному разбору: ритм монтажа и кейфреймы иначе не
        достать. Низкое разрешение намеренно — считаем склейки, не любуемся."""
        return None

    def fetch_thumbnail(self, video: VideoMeta, *, out_dir: Path) -> Path | None:
        """Обложка видео в out_dir; None — ссылки нет или не скачалась.

        Реализация одна на всех: thumbnail_url уже лежит в метаданных, качать
        картинку по https незачем поручать конкретному коллектору."""
        import urllib.error
        import urllib.request

        if not video.thumbnail_url:
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "thumbnail.jpg"
        if path.exists():
            return path
        try:
            urllib.request.urlretrieve(video.thumbnail_url, path)  # noqa: S310 — https из метаданных
        except (urllib.error.URLError, OSError):
            return None
        return path
