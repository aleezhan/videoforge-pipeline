"""ElevenLabs: озвучка с интонацией живого рассказчика.

edge-tts читает ровно и ставит паузы по знакам; ElevenLabs держит интонацию
фразы и сам решает, где вдохнуть. Замечание владельца 28.09.2026: «половину
предложения говоришь, потом без остановки полтора продолжаешь» — это и голос, и
то, что сцену озвучивали отдельно от соседей (`stages/audio.py` теперь
озвучивает фразами).

Время слов — из ответа `/with-timestamps`: он отдаёт время КАЖДОГО знака
(`alignment`), слова собираются из него по пробелам. Отдельной распознавалки
не нужно, как и у edge-tts, `.srt` кладётся рядом с mp3 тем же форматом.

`previous_text` / `next_text` — соседний текст, который модель слышит, но не
произносит: интонация фразы тогда продолжает предыдущую, а не начинается
заново. Передаётся из стадии через `context` синтеза.

Ключ — `ELEVENLABS_API_KEY` в `.env`. Голос — `voice_id` ElevenLabs в поле
`voice` ресурса. Скорость ресурса («+10%») переводится в `speed` (0.7–1.2),
высоту модель не умеет — игнорируется.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from videoforge.core.captions import Word, words_to_srt
from videoforge.providers.base import GenResult, ProviderError, TtsProvider

log = logging.getLogger("videoforge.elevenlabs")

API = "https://api.elevenlabs.io/v1"
KEY_ENV = "ELEVENLABS_API_KEY"
MODEL_ENV = "VF_ELEVENLABS_MODEL"
# multilingual v2 — русский без акцента и с таймингами; v3 выразительнее, но
# знаки времени у неё не везде — сменить можно переменной, не кодом
DEFAULT_MODEL = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"
TIMEOUT_SEC = 180.0
# Ориентир, сверять с прайсом: Creator $22 за 100 тыс. знаков. Бюджетному
# стражу нужна цена до вызова; на бесплатном тарифе денег нет, но знаки те же
PRICE_PER_CHAR = 0.00022
SPEED_RANGE = (0.7, 1.2)
# подача по умолчанию: стабильность ниже середины — живее, но без срывов
SETTINGS: dict[str, Any] = {
    "stability": 0.45,
    "similarity_boost": 0.8,
    "style": 0.15,
    "use_speaker_boost": True,
}


def api_key() -> str:
    key = os.environ.get(KEY_ENV, "")
    if not key:
        raise ProviderError(f"нет ключа ElevenLabs: {KEY_ENV}=… в .env (elevenlabs.io → API Keys)")
    return key


def speed_from_rate(rate: str) -> float:
    """«+10%» → 1.1 в пределах, которые принимает модель."""
    try:
        value = 1.0 + float(rate.strip().rstrip("%")) / 100
    except ValueError:
        value = 1.0
    low, high = SPEED_RANGE
    return round(min(high, max(low, value)), 2)


def words_from_alignment(alignment: dict[str, Any]) -> list[Word]:
    """Время знаков → слова: подряд идущие непробельные знаки. Токен без букв и
    цифр («—», «…») словом не считается — в субтитрах ему нечего подсвечивать,
    и edge-tts таких не отдаёт."""
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    words: list[Word] = []
    buf: list[str] = []
    first = last = 0.0
    for ch, s, e in zip(chars, starts, ends, strict=False):
        if ch.isspace():
            if buf:
                words.append(Word("".join(buf), first, last))
                buf = []
            continue
        if not buf:
            first = float(s)
        buf.append(ch)
        last = float(e)
    if buf:
        words.append(Word("".join(buf), first, last))
    return [w for w in words if any(c.isalnum() for c in w.text)]


class ElevenLabsTts(TtsProvider):
    name = "elevenlabs"
    extension = ".mp3"
    price_per_char = PRICE_PER_CHAR
    takes_context = True  # понимает previous_text / next_text

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get(MODEL_ENV, DEFAULT_MODEL)

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
        body: dict[str, Any] = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {**SETTINGS, "speed": speed_from_rate(rate)},
        }
        if context:
            before, after = context
            if before:
                body["previous_text"] = before
            if after:
                body["next_text"] = after
        try:
            resp = httpx.post(
                f"{API}/text-to-speech/{voice}/with-timestamps",
                params={"output_format": OUTPUT_FORMAT},
                headers={"xi-api-key": api_key()},
                json=body,
                timeout=TIMEOUT_SEC,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"ElevenLabs не ответил: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"ElevenLabs {resp.status_code}: {_error(resp)}")
        data = resp.json()
        audio = data.get("audio_base64") or ""
        if not audio:
            raise ProviderError("ElevenLabs вернул ответ без звука")
        out_path.write_bytes(base64.b64decode(audio))
        words = words_from_alignment(data.get("alignment") or {})
        srt_path = out_path.with_suffix(".srt")
        srt_path.write_text(words_to_srt(words), encoding="utf-8")
        return GenResult(
            out_path,
            round(len(text) * PRICE_PER_CHAR, 6),
            {"voice": voice, "model": self.model, "srt": srt_path.name, "chars": len(text)},
        )


def list_voices(language: str = "ru") -> list[dict[str, Any]]:
    """Голоса аккаунта (премейд и добавленные) и общей библиотеки на языке.
    Библиотека через API на бесплатном тарифе может быть закрыта — тогда только
    свои."""
    headers = {"xi-api-key": api_key()}
    out: list[dict[str, Any]] = []
    try:
        mine = httpx.get(f"{API}/voices", headers=headers, timeout=60.0)
        if mine.status_code < 400:
            for v in mine.json().get("voices") or []:
                out.append(
                    {
                        "id": v.get("voice_id"),
                        "name": v.get("name"),
                        "source": v.get("category") or "account",
                        "labels": v.get("labels") or {},
                        "preview": v.get("preview_url") or "",
                    }
                )
        shared = httpx.get(
            f"{API}/shared-voices",
            params={"language": language, "page_size": 50, "use_cases": "narrative_story"},
            headers=headers,
            timeout=60.0,
        )
        if shared.status_code < 400:
            for v in shared.json().get("voices") or []:
                out.append(
                    {
                        "id": v.get("voice_id"),
                        "name": v.get("name"),
                        "source": "library",
                        "labels": {
                            k: v.get(k) for k in ("gender", "age", "accent", "use_case") if v.get(k)
                        },
                        "preview": v.get("preview_url") or "",
                        "owner": v.get("public_owner_id"),
                    }
                )
    except httpx.HTTPError as exc:
        raise ProviderError(f"ElevenLabs не ответил: {exc}") from exc
    return out


def _error(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return resp.text[:300]
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("status") or detail)[:300]
    return str(detail)[:300]
