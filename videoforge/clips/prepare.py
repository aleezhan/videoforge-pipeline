"""Подготовка источника: метаданные → пословный транскрипт → предложения.

Каждый шаг оставляет файл и пропускается, если файл уже есть и сделан тем же
транскрибером (`force` переделывает). Сеть — только через провайдеры: кто
достаёт транскрипт, стадии не знают. Стоимость вызова возвращается, как у
любого провайдера: YouTube-субтитры бесплатны, Whisper на RunPod — нет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from videoforge.clips.source import (
    TRANSCRIPT_FILE,
    WORDS_FILE,
    SourceInfo,
    Transcript,
    load_source,
    load_transcript,
    load_words,
    save_source,
    sentences_from_words,
)
from videoforge.core.io import write_model
from videoforge.providers.base import ProviderError, TranscriptProvider


@dataclass
class PrepareReport:
    source_dir: Path
    info: SourceInfo
    transcript: Transcript
    steps: list[str] = field(default_factory=list)  # что сделано в этот раз
    cost_usd: float = 0.0


def prepare_source(
    sdir: Path, info: SourceInfo, transcriber: TranscriptProvider, *, force: bool = False
) -> PrepareReport:
    """source.json → transcript.words.srt → transcript.json. Идемпотентно."""
    sdir.mkdir(parents=True, exist_ok=True)
    steps: list[str] = []
    cost = 0.0
    previous = load_source(sdir)
    if previous is not None and not info.blur:
        # размытие ставит человек по кадру — перечитанные метаданные его не знают
        info.blur = previous.blur
    if previous is not None and not force:
        # транскрипт уже есть — его метаданные важнее свежих просмотров
        info.transcriber = previous.transcriber
        info.transcript_language = previous.transcript_language
        info.word_count = previous.word_count
        info.proxy = previous.proxy
    info.fetched_at = info.fetched_at or datetime.now(UTC)
    save_source(sdir, info)

    words_path = sdir / WORDS_FILE
    if force or not words_path.exists() or info.transcriber != transcriber.name:
        video = Path(info.file) if info.file else None
        result = transcriber.transcribe(
            url=info.url,
            video=video,
            language=info.language,
            duration_sec=info.duration_sec,
            out_path=words_path,
        )
        cost += result.cost_usd
        info.transcriber = transcriber.name
        info.transcript_language = result.language or info.language
        steps.append("transcript")

    words = load_words(sdir)
    if not words:
        raise ProviderError(f"транскрипт пустой: {words_path}")
    info.word_count = len(words)
    transcript = None if (force or "transcript" in steps) else load_transcript(sdir)
    if transcript is None:
        transcript = Transcript(
            provider=info.transcriber,
            language=info.transcript_language,
            word_count=len(words),
            sentences=sentences_from_words(words, info.chapters),
        )
        write_model(sdir / TRANSCRIPT_FILE, transcript)
        steps.append("sentences")
    save_source(sdir, info)
    return PrepareReport(sdir, info, transcript, steps, cost)
