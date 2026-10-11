"""Build the provider set from names. Imports are lazy: an unused SDK is never loaded.

Providers are constructed without keys (their clients are lazy), so `estimate` can
compute prices before keys exist. Before generating, the CLI calls `missing_keys`
to fail early and clearly rather than in the middle of a stage.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from videoforge.config import ProviderNames
from videoforge.providers.base import (
    ImageProvider,
    ImageSearchProvider,
    LlmProvider,
    LyricsProvider,
    ProviderSet,
    PublishProvider,
    TranscriptProvider,
    TtsProvider,
    VideoProvider,
)

# environment key each provider needs for generation (not for pricing)
REQUIRED_KEYS: dict[str, str] = {"gemini": "GEMINI_API_KEY", "fal": "FAL_KEY"}


def missing_keys(provider_names: Iterable[str]) -> list[str]:
    """Environment keys missing for the selected providers."""
    need = {REQUIRED_KEYS.get(name) for name in provider_names}
    return sorted(key for key in need if key and not os.environ.get(key))


def _image(name: str) -> ImageProvider:
    if name == "mock":
        from videoforge.providers.mock import MockImage

        return MockImage()
    if name == "gemini":
        from videoforge.providers.gemini import GeminiImage

        return GeminiImage()
    if name == "comfy":
        from videoforge.providers.comfy_http import ComfyImage

        return ComfyImage()
    raise ValueError(f"неизвестный провайдер картинок: {name}")


def _video(name: str) -> VideoProvider:
    if name == "mock":
        from videoforge.providers.mock import MockVideo

        return MockVideo()
    if name == "fal":
        from videoforge.providers.fal_video import FalVideo

        return FalVideo()
    if name == "remotion":
        from videoforge.providers.remotion_video import RemotionVideo

        return RemotionVideo()
    if name == "comfy":
        from videoforge.providers.comfy_http import ComfyVideo

        return ComfyVideo()
    raise ValueError(f"неизвестный провайдер видео: {name}")


def _still(name: str) -> VideoProvider:
    """Still-scene provider: the mock has its own (different move, own chain log);
    real ones reuse the clip adapters (remotion by default)."""
    if name == "mock":
        from videoforge.providers.mock import MockStill

        return MockStill()
    return _video(name)


def _tts(name: str) -> TtsProvider:
    if name == "mock":
        from videoforge.providers.mock import MockTts

        return MockTts()
    if name == "edge":
        from videoforge.providers.edge_tts_provider import EdgeTts

        return EdgeTts()
    if name == "elevenlabs":
        from videoforge.providers.elevenlabs import ElevenLabsTts

        return ElevenLabsTts()
    raise ValueError(f"неизвестный провайдер озвучки: {name}")


def _llm(name: str) -> LlmProvider:
    if name == "mock":
        from videoforge.providers.mock import MockLlm

        return MockLlm()
    if name == "gemini":
        from videoforge.providers.gemini import GeminiLlm

        return GeminiLlm()
    if name == "claude_cli":
        from videoforge.providers.claude_cli import ClaudeCli

        return ClaudeCli()
    raise ValueError(f"неизвестный LLM-провайдер: {name}")


def _search(name: str) -> ImageSearchProvider | None:
    """Reference-image search; `none` (or empty) disables search."""
    if name in ("", "none"):
        return None
    if name == "mock":
        from videoforge.providers.mock import MockSearch

        return MockSearch()
    if name == "commons":
        from videoforge.providers.commons import CommonsSearch

        return CommonsSearch()
    raise ValueError(f"неизвестный поиск картинок: {name} (commons | mock | none)")


def build_image(name: str) -> ImageProvider:
    """A single image provider, for portfolio generation outside an episode pipeline."""
    return _image(name)


def build_still(name: str) -> VideoProvider:
    """The still-scene provider, for `doctor` to check readiness on this machine."""
    return _still(name)


def build_llm(name: str) -> LlmProvider:
    """A single LLM provider, for analysis stages (classify and onwards)."""
    return _llm(name)


def build_transcriber(name: str) -> TranscriptProvider:
    """Speech to timed words, used to prepare clip sources."""
    if name == "mock":
        from videoforge.providers.mock import MockTranscriber

        return MockTranscriber()
    if name == "youtube":
        from videoforge.providers.youtube import YoutubeCaptions

        return YoutubeCaptions()
    if name == "whisper":
        from videoforge.providers.whisper_local import WhisperLocal

        return WhisperLocal(speech=True)  # clip sources are speech, not singing
    raise ValueError(f"unknown transcriber: {name} (youtube | whisper | mock)")


def transcriber_for(target: str) -> str:
    """Pick a transcriber for a source: a YouTube link (your own upload) uses its
    word-level captions; anything else (a local file) is transcribed by Whisper."""
    from videoforge.providers.youtube import is_youtube_url

    return "youtube" if is_youtube_url(target) else "whisper"


def build_lyrics(name: str = "lrclib") -> LyricsProvider:
    """Song title to lyrics. Words come from here, timings from our own audio."""
    if name == "lrclib":
        from videoforge.providers.lrclib import LrcLib

        return LrcLib()
    raise ValueError(f"неизвестный источник текстов: {name} (lrclib)")


# default upload provider per network. YouTube uses its API; TikTok and Instagram
# use the browser because their APIs are gated behind app review
PUBLISH_DEFAULTS: dict[str, str] = {
    "youtube": "youtube_api",
    "tiktok": "browser",
    "instagram": "browser",
}


def build_publisher(
    platform: str, account: str = "", *, name: str | None = None
) -> PublishProvider:
    """Publish provider for a network on behalf of an account (id from accounts/).
    YouTube keeps the token with the account and cannot be built without it;
    browser sessions take the account from PublishMeta. `name` (or
    `VF_PUBLISH_PROVIDER`) overrides the default, which lets tests and dry runs
    use `mock`."""
    chosen = name or os.environ.get("VF_PUBLISH_PROVIDER") or PUBLISH_DEFAULTS.get(platform)
    if chosen is None:
        raise ValueError(f"нет провайдера публикации для площадки {platform}")
    if chosen == "mock":
        from videoforge.providers.mock import MockPublish

        return MockPublish()
    if chosen == "youtube_api":
        from videoforge.providers.youtube_api import YoutubeUpload

        return YoutubeUpload(account)
    if chosen == "browser":
        from videoforge.providers.browser_publish import BrowserPublish

        return BrowserPublish()
    raise ValueError(f"неизвестный провайдер публикации: {chosen}")


def build_providers(names: ProviderNames) -> ProviderSet:
    return ProviderSet(
        image=_image(names.image),
        video=_video(names.video),
        tts=_tts(names.tts),
        llm=_llm(names.llm),
        # the checker is built separately only when named; otherwise the slot is empty
        # and `ProviderSet.check` returns llm (the same instance, not a second one)
        checker=_llm(names.check) if names.check else None,
        search=_search(names.search),
        # always built, even if every scene is a clip: the remotion constructor checks
        # and downloads nothing, and a storyboard may carry a hand-set `still` even
        # at a 100% clip share
        still=_still(names.still),
    )
