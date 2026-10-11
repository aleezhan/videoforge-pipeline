"""edge-tts: бесплатная озвучка голосами Microsoft Edge. Рядом с mp3 кладёт .srt
с пословными таймингами — пригодится стадии субтитров.

Сервис иногда молча вешает соединение (замечено на живом прогоне 01.09.2026:
два зависания за четыре реплики) — поэтому весь синтез одной реплики обёрнут
в таймаут и повторяется до ATTEMPTS раз."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from videoforge.providers.base import GenResult, ProviderError, TtsProvider

log = logging.getLogger("videoforge.edge_tts")

TIMEOUT_SEC = 60  # на синтез одной реплики целиком
ATTEMPTS = 3


def list_voices(lang: str = "") -> list[Any]:
    """Голоса edge-tts (словари с ShortName, Gender, Locale, VoiceTag);
    `lang` — префикс локали ('ru', 'en-US'). Нужен интернет."""
    import edge_tts

    voices: list[Any] = list(asyncio.run(edge_tts.list_voices()))
    prefix = lang.lower()
    rows = [v for v in voices if v["Locale"].lower().startswith(prefix)] if prefix else voices
    return sorted(rows, key=lambda v: v["ShortName"])


class EdgeTts(TtsProvider):
    name = "edge"
    extension = ".mp3"

    def synthesize(
        self,
        text: str,
        *,
        voice: str,
        rate: str,
        pitch: str,
        out_path: Path,
        volume: str = "+0%",
        context: tuple[str, str] | None = None,
    ) -> GenResult:
        last: Exception | None = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return self._synthesize_once(
                    text, voice=voice, rate=rate, pitch=pitch, volume=volume, out_path=out_path
                )
            except ProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 — таймаут или сетевой сбой сервиса
                last = exc
                log.warning(
                    "edge-tts: попытка %d/%d не удалась (%s), повторяю", attempt, ATTEMPTS, exc
                )
        raise ProviderError(f"edge-tts не ответил за {ATTEMPTS} попыток: {last}")

    @staticmethod
    def _synthesize_once(
        text: str, *, voice: str, rate: str, pitch: str, volume: str, out_path: Path
    ) -> GenResult:
        import edge_tts

        submaker = edge_tts.SubMaker()

        async def _stream() -> bool:
            # rate/pitch/volume — подача в формате самого сервиса ("+10%", "-4Hz"),
            # формат проверен раньше (`core.storyboard.check_prosody`)
            communicate = edge_tts.Communicate(
                text, voice, rate=rate, pitch=pitch, volume=volume, boundary="WordBoundary"
            )
            got = False
            with open(out_path, "wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                        got = True
                    elif chunk["type"] == "WordBoundary":
                        submaker.feed(chunk)
            return got

        got_audio = asyncio.run(asyncio.wait_for(_stream(), timeout=TIMEOUT_SEC))
        if not got_audio:
            raise ProviderError(f"edge-tts не вернул аудио для голоса {voice}")
        srt_path = out_path.with_suffix(".srt")
        srt_path.write_text(submaker.get_srt(), encoding="utf-8")
        return GenResult(out_path, 0.0, {"voice": voice, "srt": srt_path.name})
