"""Your own YouTube upload as a clip source: metadata, chapters and a word-level
transcript via yt-dlp, without API keys.

Intended for videos you own (your channel's long-form uploads). The transcript is
YouTube's auto-captions in json3 format, which carries per-word timing: more precise
than manual captions (timed per line) for karaoke captions and cheaper than Whisper.
Manual captions in the spoken language are the fallback (words spread evenly
across the line).

The full source is never downloaded: `download_proxy` fetches a 480p preview and
only the selected segments are fetched in full resolution.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from videoforge.clips.source import Chapter, HeatPoint, SourceInfo
from videoforge.core.captions import Word, words_to_srt
from videoforge.core.ffmpeg import ffmpeg_exe, merge_tracks, probe_stream_duration
from videoforge.providers.base import ProviderError, TranscriptProvider, TranscriptResult

log = logging.getLogger("videoforge.youtube")

MAX_WORD_SEC = 1.5  # последнее слово реплики не тянется до следующей реплики
MIN_WORD_SEC = 0.08
SPEAKER_MARK = ">>"  # так YouTube помечает смену говорящего в авто-дорожке
# YouTube помечает звуки квадратными скобками: [смех], [откашливается],
# [тяжело вздыхает]. Это не речь, и в караоке им делать нечего — на первом же
# шортсе «[откашливается]» висело поверх лица. Выкидываем при разборе дорожки,
# а не при сборке: иначе тот же мусор всплыл бы и в выборе моментов
SOUND_EVENT_MAX_WORDS = 4  # незакрытая скобка не должна съесть следующую реплику
_REAL_SUBTAG = re.compile(r"^([A-Za-z]{2}|[0-9]{3}|[A-Za-z]{4})$")


def base_lang(code: str) -> str:
    """Код дорожки → код языка: ru-orig → ru, de-CcQ45jRV4-E → de, pt-BR → pt-BR.
    (Та же логика, что у коллектора анализатора; провайдер от intel не зависит.)"""
    primary, _, rest = code.partition("-")
    if not rest:
        return code
    subtag = rest.split("-")[0]
    if subtag.lower() == "orig" or not _REAL_SUBTAG.match(subtag):
        return primary
    return f"{primary}-{subtag}"


def use_our_ffmpeg() -> str:
    """Сказать yt-dlp, каким ffmpeg пользоваться, и вернуть путь к нему.

    Две ловушки сразу, обе видны только на живой докачке отрезка:

    1. Путь — к ФАЙЛУ, не к папке. В папке yt-dlp ищет файл ровно с именем
       `ffmpeg`, а бинарь из imageio-ffmpeg называется
       `ffmpeg-win-x86_64-v7.1.exe`; по файлу же программа опознаётся по
       подстроке в имени.
    2. Опции `ffmpeg_location` мало. Проверка «а можно ли скачать кусок»
       (`FFmpegFD.available()`) создаёт постпроцессор без загрузчика, параметров
       не видит и читает контекстную переменную, которую выставляет CLI
       (`--ffmpeg-location`). Не выставить её — «ffmpeg is not installed» при
       установленном ffmpeg, которым работает весь остальной конвейер.
    """
    exe = ffmpeg_exe()
    try:
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

        FFmpegPostProcessor._ffmpeg_location.set(exe)
    except (ImportError, AttributeError):  # yt-dlp переименовал внутренность
        log.warning("не удалось указать yt-dlp путь к ffmpeg — докачка отрезков может упасть")
    return exe


def _sound_event(token: str, inside: int) -> tuple[bool, int]:
    """(выбросить ли токен, сколько токенов уже внутри скобок).

    Пометка живёт не в одном токене: у авто-дорожки каждое слово приходит своим
    сегментом, поэтому «[тяжело вздыхает]» — это `[тяжело` и `вздыхает]`, а
    рядом встречается слипшееся `[вздыхает][тяжело`. Одной регуляркой такое не
    поймать, нужен разбор со состоянием."""
    if inside:
        return True, 0 if token.endswith("]") or inside >= SOUND_EVENT_MAX_WORDS else inside + 1
    if token.startswith("["):
        return True, 0 if token.endswith("]") else 1
    return False, 0


def _ydl_opts(**extra: Any) -> dict[str, Any]:
    return {"quiet": True, "no_warnings": True, "noprogress": True, **extra}


YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def is_youtube_url(target: str) -> bool:
    """A YouTube link is the only source with ready word-level captions; a local
    file is transcribed by Whisper. A Windows drive path parses as a URL scheme
    without a host, so it is not mistaken for a link."""
    try:
        host = (urlparse(target).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


def fetch_info(url: str) -> dict[str, Any]:
    """Сырые метаданные видео (yt-dlp extract_info без скачивания)."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True)) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except yt_dlp.utils.DownloadError as exc:
        raise ProviderError(f"видео не открылось: {url} — {exc}") from exc


def source_info_from(info: dict[str, Any], *, language: str | None = None) -> SourceInfo:
    chapters = [
        Chapter(
            title=str(ch.get("title") or ""),
            start_sec=float(ch.get("start_time") or 0.0),
            end_sec=float(ch.get("end_time") or 0.0),
        )
        for ch in info.get("chapters") or []
        if isinstance(ch, dict)
    ]
    heatmap = [
        HeatPoint(
            start_sec=float(h.get("start_time") or 0.0),
            end_sec=float(h.get("end_time") or 0.0),
            value=float(h.get("value") or 0.0),
        )
        for h in info.get("heatmap") or []
        if isinstance(h, dict)
    ]
    return SourceInfo(
        id=str(info.get("id") or ""),
        url=str(info.get("webpage_url") or info.get("original_url") or ""),
        title=str(info.get("title") or ""),
        channel=str(info.get("channel") or info.get("uploader") or ""),
        duration_sec=float(info.get("duration") or 0.0),
        width=info.get("width"),
        height=info.get("height"),
        language=language or base_lang(str(info.get("language") or "ru")),
        upload_date=str(info.get("upload_date") or ""),
        views=info.get("view_count"),
        chapters=chapters,
        heatmap=heatmap,
        fetched_at=datetime.now(UTC),
    )


def pick_caption_track(
    language: str, subtitles: dict[str, Any], autos: dict[str, Any]
) -> tuple[str, str] | None:
    """(код дорожки, auto | manual) для языка речи.

    Сначала авто-дорожка на языке речи — только у неё тайминги слов; `-orig`
    предпочтительнее (это распознанный оригинал, а не авто-перевод). Потом
    ручные субтитры на том же языке. Другой язык не берём никогда."""
    wanted = base_lang(language)
    auto = [k for k in autos if base_lang(k) == wanted]
    if auto:
        auto.sort(key=lambda k: (not k.endswith("-orig"), k))
        return auto[0], "auto"
    manual = [k for k in subtitles if base_lang(k) == wanted]
    if manual:
        return manual[0], "manual"
    return None


def download_json3(url: str, track: str, out_dir: Path, *, automatic: bool) -> dict[str, Any]:
    """Скачать одну дорожку субтитров как json3 и вернуть разобранный JSON."""
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    opts = _ydl_opts(
        skip_download=True,
        writesubtitles=not automatic,
        writeautomaticsub=automatic,
        subtitleslangs=[track],
        subtitlesformat="json3",
        outtmpl={"default": str(out_dir / "captions")},
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        raise ProviderError(f"субтитры не скачались ({track}): {exc}") from exc
    found = sorted(out_dir.glob("captions.*.json3"))
    if not found:
        raise ProviderError(f"yt-dlp не оставил файла субтитров для дорожки {track}")
    return json.loads(found[0].read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def json3_to_words(payload: dict[str, Any]) -> list[Word]:
    """json3 → слова. У авто-дорожки каждый сегмент — слово со своим смещением;
    у ручной — реплика целиком, тогда слова делят её время поровну. Маркер
    смены говорящего `>>` в слова не идёт (он попал бы в субтитры), а
    становится признаком `turn` у следующего слова."""
    events = [e for e in payload.get("events") or [] if isinstance(e, dict)]
    words: list[Word] = []
    turn = False
    in_event = 0  # сколько токенов уже пропущено внутри незакрытой [пометки]
    for i, ev in enumerate(events):
        t0 = ev.get("tStartMs")
        segs = ev.get("segs")
        if t0 is None or not isinstance(segs, list):
            continue
        dur = ev.get("dDurationMs") or 0
        ev_end = (t0 + dur) / 1000 if dur else None
        nxt = events[i + 1].get("tStartMs") if i + 1 < len(events) else None
        next_start = nxt / 1000 if isinstance(nxt, int | float) else None
        items: list[tuple[float, str]] = []
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("utf8") or "").strip()
            if not text:
                continue
            items.append(((t0 + (seg.get("tOffsetMs") or 0)) / 1000, text))
        for j, (start, text) in enumerate(items):
            if j + 1 < len(items):
                end = items[j + 1][0]
            else:
                limits = [x for x in (ev_end, next_start) if x is not None]
                end = min([*limits, start + MAX_WORD_SEC])
            end = max(end, start + MIN_WORD_SEC)
            raw = text.split()
            if SPEAKER_MARK in raw:
                turn = True
            tokens = [t for t in raw if t != SPEAKER_MARK]
            if not tokens:
                continue
            step = (end - start) / len(tokens)
            for k, token in enumerate(tokens):
                skipped, in_event = _sound_event(token, in_event)
                if skipped:  # звук, а не слово: время его остаётся пустым
                    continue
                words.append(
                    Word(
                        token,
                        round(start + k * step, 3),
                        round(start + (k + 1) * step, 3),
                        turn=turn,
                    )
                )
                turn = False
    words.sort(key=lambda w: w.start)
    return words


class YoutubeCaptions(TranscriptProvider):
    name = "youtube"

    def transcribe(
        self,
        *,
        url: str,
        video: Path | None,
        language: str,
        duration_sec: float,
        out_path: Path,
    ) -> TranscriptResult:
        if not url or not is_youtube_url(url):
            raise ProviderError(
                "транскрибер youtube читает только субтитры YouTube; файл на диске и "
                "другие сайты слушает whisper (--transcriber whisper, auto выберет сам)"
            )
        info = fetch_info(url)
        picked = pick_caption_track(
            language, info.get("subtitles") or {}, info.get("automatic_captions") or {}
        )
        if picked is None:
            raise ProviderError(
                f"у видео нет субтитров на языке речи «{language}» — ни авто, ни ручных; "
                "нужен whisper или другой --language"
            )
        track, kind = picked
        log.info("субтитры: дорожка %s (%s)", track, kind)
        words = json3_to_words(
            download_json3(url, track, out_path.parent, automatic=kind == "auto")
        )
        if not words:
            raise ProviderError(f"дорожка {track} пустая")
        out_path.write_text(words_to_srt(words), encoding="utf-8")
        return TranscriptResult(
            words_srt=out_path,
            language=base_lang(track),
            cost_usd=0.0,
            meta={"track": track, "kind": kind, "words": len(words)},
        )


AUDIO_GAP_LIMIT_SEC = 0.5  # звук короче картинки на столько — брак докачки, а не округление


def _out_template(out_path: Path) -> tuple[Path, dict[str, str]]:
    """(основа имени, outtmpl для yt-dlp) с явным `%(ext)s`.

    Шаблон без `%(ext)s` yt-dlp понимает буквально и кладёт файл вообще без
    расширения — на живом прогоне вышел `s01_src.video`, которого никакой поиск
    по `*.mp4` не находит. Расширение он выбирает сам, поэтому результат потом
    ищется по основе имени."""
    stem = out_path.with_suffix("")
    for stale in stem.parent.glob(stem.name + ".*"):
        if stale.is_file():
            stale.unlink()
    return stem, {"default": f"{stem}.%(ext)s"}


def _produced(stem: Path, out_path: Path) -> Path:
    found = sorted(p for p in stem.parent.glob(stem.name + ".*") if p.is_file())
    if not found:
        raise ProviderError(f"yt-dlp не оставил файла: {stem.name}.*")
    if found[0] != out_path:
        found[0].replace(out_path)
    return out_path


def _download_range(
    url: str, out_path: Path, *, fmt: str, start_sec: float, end_sec: float, force_keyframes: bool
) -> Path:
    """Одна дорожка одного отрезка."""
    import yt_dlp

    out_path.parent.mkdir(parents=True, exist_ok=True)
    stem, outtmpl = _out_template(out_path)
    opts = _ydl_opts(
        format=fmt,
        outtmpl=outtmpl,
        ffmpeg_location=use_our_ffmpeg(),
        download_ranges=yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)]),
        force_keyframes_at_cuts=force_keyframes,
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        raise ProviderError(
            f"отрезок не скачался ({start_sec:.0f}–{end_sec:.0f} с, формат {fmt}): {exc}"
        ) from exc
    return _produced(stem, out_path)


def _section_video_format(max_height: int) -> str:
    """Селектор картинки для отрезка: сначала форматы, которые отдаются файлом
    по http(s), и только потом HLS.

    Сортировка yt-dlp ставит выше всех «премиальный» VP9 по m3u8 (616 у
    1080p), а ffmpeg, открывая такой плейлист с перемоткой на час с лишним,
    читает 0 пакетов и оставляет файл в 261 байт без единой дорожки — без
    ошибки. Замерено 17.09.2026 на студийном подкасте: четыре шортса упали на
    склейке «Stream map '0:v:0' matches no streams»; тот же отрезок форматом
    399 (https) — 7 с 1080p."""
    h = f"[height<={max_height}]"
    return f"bv*{h}[ext=mp4][protocol^=http]/bv*{h}[protocol^=http]/bv*{h}[ext=mp4]/bv*{h}/bv*"


def download_section(
    url: str, out_path: Path, *, start_sec: float, end_sec: float, max_height: int = 2160
) -> Path:
    """Один отрезок видео в полном разрешении — то, ради чего исходник целиком не
    качается: из двух с половиной часов 4K нужны сорок секунд.

    **Картинка и звук качаются РАЗНЫМИ вызовами и сшиваются здесь.** Одним
    вызовом (`bv+ba`) yt-dlp отдаёт оба URL одному процессу ffmpeg, и на 4K с
    `--force-keyframes-at-cuts` перекодирование картинки успевает уронить
    соединение со звуком: замерено на живом прогоне — 32 секунды картинки против
    6.6 секунды звука, при этом на 1080p и на 4K без перекодирования всё цело.
    Немой шортс — брак, который заметен только на просмотре, поэтому звук
    проверяется здесь же.

    Перекодирование краёв (`force_keyframes`) нужно только картинке: у звука
    каждый кадр опорный, и копия и так режется точно. Точную границу всё равно
    ставит `cut_segment` — здесь берут с запасом. Файл уже на месте — не качаем."""
    if out_path.exists():
        return out_path
    video = _download_range(
        url,
        out_path.with_name(out_path.stem + "_v.mp4"),
        fmt=_section_video_format(max_height),
        start_sec=start_sec,
        end_sec=end_sec,
        force_keyframes=True,
    )
    if not probe_stream_duration(video, "0:v:0"):
        raise ProviderError(
            f"отрезок {start_sec:.0f}–{end_sec:.0f} с скачался без картинки "
            f"({video.stat().st_size} байт): формат не режется по времени"
        )
    audio = _download_range(
        url,
        out_path.with_name(out_path.stem + "_a.m4a"),
        fmt="ba[ext=m4a]/ba",
        start_sec=start_sec,
        end_sec=end_sec,
        force_keyframes=False,
    )
    merge_tracks(video, audio, out_path)
    video.unlink(missing_ok=True)
    audio.unlink(missing_ok=True)

    vdur = probe_stream_duration(out_path, "0:v:0") or 0.0
    adur = probe_stream_duration(out_path, "0:a:0") or 0.0
    if adur < vdur - AUDIO_GAP_LIMIT_SEC:
        raise ProviderError(
            f"в скачанном отрезке звук короче картинки ({adur:.1f} с против {vdur:.1f} с) — "
            f"из такого куска вышел бы немой шортс; повтори стадию"
        )
    return out_path


def download_audio(url: str, out_path: Path) -> Path:
    """Только звук, лучшая дорожка — для распознавания там, где субтитров нет
    (своё видео по ссылке без автосубтитров). Файл уже на месте —
    не качаем: двухчасовой подкаст тянуть второй раз незачем. Контейнер yt-dlp
    выбирает сам; имя приводится к `out_path`, декодеру whisper это всё равно."""
    import yt_dlp

    if out_path.exists():
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stem, outtmpl = _out_template(out_path)
    opts = _ydl_opts(format="ba[ext=m4a]/ba", outtmpl=outtmpl, ffmpeg_location=use_our_ffmpeg())
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        raise ProviderError(f"звук не скачался: {exc}") from exc
    return _produced(stem, out_path)


def download_proxy(url: str, out_path: Path, *, max_height: int = 480) -> Path:
    """480p со звуком — превью для выбора моментов, не материал для рендера."""
    import yt_dlp

    if out_path.exists():
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stem, outtmpl = _out_template(out_path)
    opts = _ydl_opts(
        format=(
            f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={max_height}][ext=mp4]/b[height<={max_height}]"
        ),
        merge_output_format="mp4",
        outtmpl=outtmpl,
        ffmpeg_location=use_our_ffmpeg(),
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        raise ProviderError(f"превью не скачалось: {exc}") from exc
    return _produced(stem, out_path)
