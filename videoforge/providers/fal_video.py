"""fal.ai image-to-video. Пресеты — endpoint + длительности + цены.

Цены здесь — ориентир для бюджетного стража, не прайс. Перед первым платным
прогоном сверь с https://fal.ai/models/<endpoint> и поправь таблицу.
FAL_KEY читается самим fal_client из окружения.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from videoforge.core.storyboard import ChainLink
from videoforge.providers.base import GenResult, MotionGuide, ProviderError, VideoProvider

log = logging.getLogger("videoforge.fal")


@dataclass(frozen=True)
class FalPreset:
    endpoint: str
    durations: tuple[int, ...]
    prices: dict[int, float]  # USD за клип по длительности
    extra: dict[str, Any] = field(default_factory=dict)


PRESETS: dict[str, FalPreset] = {
    "hailuo-02-fast": FalPreset(
        endpoint="fal-ai/minimax/hailuo-02-fast/image-to-video",
        durations=(6, 10),
        prices={6: 0.10, 10: 0.17},
        extra={"prompt_optimizer": True},
    ),
    "hailuo-02-standard": FalPreset(
        endpoint="fal-ai/minimax/hailuo-02/standard/image-to-video",
        durations=(6, 10),
        prices={6: 0.28, 10: 0.48},
        extra={"prompt_optimizer": True, "resolution": "768P"},
    ),
}


class FalVideo(VideoProvider):
    name = "fal"

    def __init__(self, preset: str | None = None) -> None:
        # ключ проверяется в animate(): без него провайдер строится и отдаёт
        # цены (estimate), генерация — нет
        preset_name = preset or os.environ.get("FAL_VIDEO_PRESET", "hailuo-02-fast")
        if preset_name not in PRESETS:
            raise ProviderError(f"неизвестный пресет fal '{preset_name}': {sorted(PRESETS)}")
        self.preset = PRESETS[preset_name]
        self.endpoint = os.environ.get("FAL_VIDEO_ENDPOINT") or self.preset.endpoint
        self.supported_durations = self.preset.durations
        self.name = f"fal:{preset_name}"

    def price_for(self, duration: int, *, guided: bool = False) -> float:
        return self.preset.prices.get(duration, max(self.preset.prices.values()))

    def animate(
        self,
        image: Path,
        prompt: str,
        *,
        duration_sec: int,
        aspect_ratio: str,
        out_path: Path,
        chain: ChainLink | None = None,  # движение задаёт модель: место в цепочке не применимо
        motion_reference: MotionGuide | None = None,
    ) -> GenResult:
        if motion_reference is not None:
            # референса у hosted i2v нет; рисовать вместо него по промпту нельзя —
            # клип без удара, на который человек ставил референс, это брак, и
            # решать, чем рендерить, должен человек (VF_VIDEO_PROVIDER=comfy)
            raise ProviderError(
                f"{self.name}: референс движения не поддерживается "
                f"({motion_reference.path.name}) — нужен провайдер comfy"
            )
        if not os.environ.get("FAL_KEY"):
            raise ProviderError("FAL_KEY не задан (см. .env.example)")
        import fal_client

        image_url = fal_client.upload_file(image)
        arguments: dict[str, Any] = {
            "prompt": prompt,
            "image_url": image_url,
            "duration": str(duration_sec),
            **self.preset.extra,
        }

        def on_update(update: Any) -> None:
            if isinstance(update, fal_client.InProgress):
                for entry in update.logs or []:
                    log.debug("fal: %s", entry.get("message", ""))

        result = fal_client.subscribe(
            self.endpoint, arguments=arguments, with_logs=True, on_queue_update=on_update
        )
        video = result.get("video") if isinstance(result, dict) else None
        url = video.get("url") if isinstance(video, dict) else None
        if not url:
            raise ProviderError(f"fal не вернул video.url: {str(result)[:300]}")

        with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        return GenResult(
            out_path,
            self.price_for(duration_sec),
            {"endpoint": self.endpoint, "duration": duration_sec, "prompt": prompt},
        )
