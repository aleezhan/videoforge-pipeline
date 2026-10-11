"""Источник для нарезок: длинное видео и его транскрипт.

projects/<project>/sources/<id>/
  source.json            метаданные: ссылка, название, канал, длительность,
                         размер кадра, язык, главы; кем сделан транскрипт
  transcript.words.srt   пословные тайминги — тот же формат, что пишет озвучка
                         (edge-tts), поэтому субтитры-караоке работают без изменений
  transcript.json        предложения с таймкодами и главами: вход для выбора
                         моментов и для человека
  proxy.mp4              (по желанию) 480p для превью кадров; полный исходник
                         не качается — полное разрешение докачивают только
                         выбранные отрезки, а не 4K на два с половиной часа

Предложения строит python, не модель: у автосубтитров нет пунктуации, поэтому
граница — пауза в речи, длина и точка, если она всё-таки есть.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from videoforge.core.captions import Word, parse_srt_words
from videoforge.core.io import read_model, write_model

SOURCE_FILE = "source.json"
WORDS_FILE = "transcript.words.srt"
TRANSCRIPT_FILE = "transcript.json"
PROXY_FILE = "proxy.mp4"

SENTENCE_GAP_SEC = 0.8  # пауза длиннее этой — граница предложения
SENTENCE_MAX_WORDS = 28  # автосубтитры без пунктуации: режем и по длине
SENTENCE_MAX_SEC = 12.0


class Chapter(BaseModel):
    title: str
    start_sec: float
    end_sec: float


class HeatPoint(BaseModel):
    """One window of YouTube's "most replayed" graph, as a share of the maximum.

    The only measured signal of what viewers found interesting, as opposed to a
    "virality score" a model would make up. Available for your own uploads."""

    start_sec: float
    end_sec: float
    value: float  # 0..1


HEAT_SKIP_HEAD_SEC = 30.0  # старт у YouTube всегда 1.0 — его смотрят все, это не момент


def top_peaks(
    heatmap: list[HeatPoint], n: int = 8, *, skip_head_sec: float = HEAT_SKIP_HEAD_SEC
) -> list[HeatPoint]:
    """Самые пересматриваемые окна, по убыванию, без стартового пика."""
    body = [h for h in heatmap if h.start_sec >= skip_head_sec]
    return sorted(body, key=lambda h: h.value, reverse=True)[:n]


class BlurBox(BaseModel):
    """A rectangle of the source frame blurred on every segment, in frame shares.

    For something that stays in shot for the whole recording and must not reach
    the clips (a sponsor sign on the wall, a whiteboard with private notes).
    Written by hand into `source.json`."""

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)
    note: str = ""  # what is blurred: "sponsor logo on the wall"


class SourceInfo(BaseModel):
    id: str
    url: str = ""  # пусто — локальный файл
    file: str = ""  # локальный исходник (абсолютный путь), если не по ссылке
    title: str = ""
    channel: str = ""
    duration_sec: float = 0.0
    width: int | None = None
    height: int | None = None
    language: str = "ru"  # язык речи — на нём ищем субтитры
    upload_date: str = ""
    views: int | None = None
    chapters: list[Chapter] = Field(default_factory=list)
    heatmap: list[HeatPoint] = Field(default_factory=list)  # пусто — YouTube не отдал
    # recording of your own live stream: a small face in a corner is the facecam,
    # which enables the automatic "screen on top, facecam below" reframe layout
    stream: bool = False
    fetched_at: datetime | None = None
    transcriber: str = ""  # кем сделан транскрипт; пусто — ещё нет
    transcript_language: str = ""
    word_count: int = 0
    proxy: str | None = None  # превью относительно папки источника
    blur: list[BlurBox] = Field(default_factory=list)  # что размыть на каждом отрезке


class Sentence(BaseModel):
    start_sec: float
    end_sec: float
    text: str
    chapter: str = ""
    speaker_turn: bool = False  # с этого предложения говорит другой человек


class Transcript(BaseModel):
    provider: str = ""
    language: str = ""
    word_count: int = 0
    sentences: list[Sentence] = Field(default_factory=list)


def chapter_at(chapters: list[Chapter], t: float) -> str:
    for ch in chapters:
        if ch.start_sec <= t < ch.end_sec:
            return ch.title
    return ""


def sentences_from_words(
    words: list[Word],
    chapters: list[Chapter] | None = None,
    *,
    gap_sec: float = SENTENCE_GAP_SEC,
    max_words: int = SENTENCE_MAX_WORDS,
    max_sec: float = SENTENCE_MAX_SEC,
) -> list[Sentence]:
    """Слова → предложения: граница на смене говорящего, паузе, точке и по длине."""
    out: list[Sentence] = []
    buf: list[Word] = []

    def flush() -> None:
        if not buf:
            return
        out.append(
            Sentence(
                start_sec=round(buf[0].start, 2),
                end_sec=round(buf[-1].end, 2),
                text=" ".join(w.text for w in buf),
                chapter=chapter_at(chapters or [], buf[0].start),
                speaker_turn=buf[0].turn,
            )
        )
        buf.clear()

    for w in words:
        if buf and (
            w.turn
            or w.start - buf[-1].end > gap_sec
            or len(buf) >= max_words
            or w.end - buf[0].start > max_sec
            or buf[-1].text.endswith((".", "?", "!"))
        ):
            flush()
        buf.append(w)
    flush()
    return out


# ---------------------------------------------------------------- файлы


def source_path(sdir: Path) -> Path:
    return sdir / SOURCE_FILE


def load_source(sdir: Path) -> SourceInfo | None:
    path = source_path(sdir)
    return read_model(path, SourceInfo) if path.exists() else None


def save_source(sdir: Path, info: SourceInfo) -> None:
    sdir.mkdir(parents=True, exist_ok=True)
    write_model(source_path(sdir), info)


def load_words(sdir: Path) -> list[Word]:
    path = sdir / WORDS_FILE
    return parse_srt_words(path.read_text(encoding="utf-8")) if path.exists() else []


def load_transcript(sdir: Path) -> Transcript | None:
    path = sdir / TRANSCRIPT_FILE
    return read_model(path, Transcript) if path.exists() else None


def list_sources(sources_root: Path) -> list[SourceInfo]:
    if not sources_root.exists():
        return []
    found = [load_source(p) for p in sorted(sources_root.iterdir()) if p.is_dir()]
    return [s for s in found if s is not None]
