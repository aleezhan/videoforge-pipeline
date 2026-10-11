"""Коллектор на yt-dlp: метаданные YouTube-канала без API-ключей. Бесплатно.

Два прохода: плоский список вкладок /videos и /shorts (быстро, даёт id и
принадлежность к shorts), затем полные метаданные по каждому видео (даты,
просмотры, лайки, описание) — примерно секунда на видео, поэтому у больших
каналов ставь max_videos. Прогресс пишется в лог каждые 10 видео. Приватные
и удалённые видео пропускаются с предупреждением, стадию не роняют.

Вкладка /streams (записи трансляций) сознательно не собирается: у стримов
другая механика просмотров, в статистике канала они только шумят.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videoforge.intel.collectors.base import (
    Collector,
    CollectorError,
    CollectResult,
    TranscriptResult,
    VideoFileResult,
)
from videoforge.intel.models import ChannelData, ChannelSnapshot, VideoMeta

log = logging.getLogger("videoforge.intel.ytdlp")

_TAB_SUFFIXES = (
    "/videos",
    "/shorts",
    "/streams",
    "/featured",
    "/playlists",
    "/community",
    "/about",
)

# Настоящий подтег BCP-47: регион (de-AT, 419) или письменность (zh-Hans).
# Всё остальное после дефиса — служебный хвост YouTube, см. base_lang.
_REAL_SUBTAG = re.compile(r"^([A-Za-z]{2}|[0-9]{3}|[A-Za-z]{4})$")


class _QuietLogger:
    """Глушитель вывода yt-dlp: свои сообщения он печатает мимо logging.

    Ошибки мы и так поднимаем как CollectorError с текстом исключения, а
    штатные ситуации (у канала нет вкладки /shorts) не должны выглядеть как
    ERROR в логе прогона.
    """

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)


def _avatar_url(thumbnails: list[dict[str, Any]]) -> str:
    """Аватар из `thumbnails` страницы канала: там же лежит и баннер, поэтому
    берём запись с id вида avatar_*, без обрезки предпочтительнее."""
    avatars = [t for t in thumbnails if "avatar" in str(t.get("id") or "")]
    if not avatars:
        # корневая страница канала отдаёт аватар без имён (id 0, 1, 2…), баннер
        # там помечен словом banner — берём последний не-баннер, он крупнее
        avatars = [t for t in thumbnails if "banner" not in str(t.get("id") or "")]
    if not avatars:
        return ""
    best = next((t for t in avatars if t.get("id") == "avatar_uncropped"), avatars[-1])
    return str(best.get("url") or "")


def _eta(started: float, done: int, total: int) -> str:
    """Осталось времени по фактической скорости: «3 мин», «40 с»."""
    if done <= 0 or done >= total:
        return "0 с"
    left = (time.monotonic() - started) / done * (total - done)
    return f"{left / 60:.0f} мин" if left >= 90 else f"{left:.0f} с"


def base_lang(code: str) -> str:
    """Код дорожки субтитров → код языка: de-CcQ45jRV4-E → de, en-orig → en.

    YouTube помечает авто-переведённые и мультиязычные дорожки хвостом с id
    исходной дорожки, а авто-оригинал — суффиксом -orig. Ни то, ни другое не
    язык. Настоящие региональные коды (pt-BR, zh-Hans) сохраняются.
    """
    primary, _, rest = code.partition("-")
    if not rest:
        return code
    subtag = rest.split("-")[0]
    if subtag.lower() == "orig" or not _REAL_SUBTAG.match(subtag):
        return primary
    return f"{primary}-{subtag}"


def channel_base_url(url: str) -> str:
    """Ссылка на канал без вкладки: .../@handle/videos → .../@handle."""
    base = url.strip().rstrip("/")
    for suffix in _TAB_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base


def parse_published(info: dict[str, Any]) -> datetime | None:
    """timestamp точнее; upload_date (YYYYMMDD) — запасной вариант."""
    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts:
        return datetime.fromtimestamp(int(ts), UTC)
    raw = info.get("upload_date")
    if raw:
        try:
            return datetime.strptime(str(raw), "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _match_lang(tracks: dict[str, Any], wanted: str) -> str | None:
    """Код дорожки для нужного языка: сначала точный, потом по базовому коду."""
    if wanted in tracks:
        return wanted
    return next((key for key in tracks if base_lang(key) == wanted), None)


def pick_subtitle_lang(
    language: str | None, subtitles: dict[str, Any], autos: dict[str, Any]
) -> str | None:
    """Ручные субтитры на языке видео → ручные en/ru → авто-оригинал → авто en/ru.

    Сравнение идёт по базовому коду языка: YouTube отдаёт дорожки вида
    "de-CcQ45jRV4-E" (мультиязычная озвучка) и "en-orig" (авто-оригинал), и
    точное совпадение с "de"/"en" в таких каналах не находится никогда."""
    candidates: list[str] = []
    if language:
        candidates.append(base_lang(language))
    candidates += ["en", "ru"]
    for lang in candidates:
        found = _match_lang(subtitles, lang)
        if found:
            return found
    if subtitles:
        return next(iter(subtitles))
    for lang in candidates:
        found = _match_lang(autos, lang)
        if found:
            return found
    for lang in autos:  # авто-дорожка оригинала, когда язык видео не указан
        if lang.endswith("-orig"):
            return lang
    return next(iter(autos)) if autos else None


def to_video_meta(info: dict[str, Any], *, is_short: bool | None) -> VideoMeta:
    duration = info.get("duration")
    return VideoMeta(
        id=str(info.get("id", "")),
        url=str(info.get("webpage_url") or info.get("url") or ""),
        title=str(info.get("title") or ""),
        description=str(info.get("description") or ""),
        published_at=parse_published(info),
        duration_sec=float(duration) if duration is not None else None,
        views=info.get("view_count"),
        likes=info.get("like_count"),
        comments=info.get("comment_count"),
        thumbnail_url=str(info.get("thumbnail") or ""),
        is_short=is_short,
    )


class YtDlpCollector(Collector):
    name = "ytdlp"

    def fetch_transcript(self, video: VideoMeta, *, out_dir: Path) -> TranscriptResult | None:
        import yt_dlp

        url = video.url or f"https://www.youtube.com/watch?v={video.id}"
        probe_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        try:
            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                info = ydl.extract_info(url, download=False) or {}
        except yt_dlp.utils.DownloadError as exc:
            raise CollectorError(f"видео {video.id} не открылось: {exc}") from exc
        lang = pick_subtitle_lang(
            info.get("language"),
            info.get("subtitles") or {},
            info.get("automatic_captions") or {},
        )
        if lang is None:
            return None
        dl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [lang],
            "subtitlesformat": "vtt",
            "outtmpl": {"default": str(out_dir / "transcript")},
        }
        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as exc:
            raise CollectorError(f"субтитры {video.id} не скачались: {exc}") from exc
        path = out_dir / f"transcript.{lang}.vtt"
        if not path.exists():
            found = sorted(out_dir.glob("transcript.*.vtt"))
            if not found:
                return None
            path = found[0]
        # имя файла — источник языка для стадии deep, поэтому служебный хвост
        # дорожки ("de-CcQ45jRV4-E") в него попасть не должен
        code = base_lang(lang)
        if code != lang:
            normalized = out_dir / f"transcript.{code}.vtt"
            path = path.replace(normalized)
        return TranscriptResult(path=path, language=code)

    def download_video(
        self, video: VideoMeta, *, out_dir: Path, max_height: int = 480
    ) -> VideoFileResult | None:
        import yt_dlp

        existing = sorted(out_dir.glob("source.*"))
        if existing:  # уже скачано прошлым прогоном — качать заново незачем
            return VideoFileResult(path=existing[0])
        url = video.url or f"https://www.youtube.com/watch?v={video.id}"
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _QuietLogger(),
            # только видеодорожка: звук визуальному разбору не нужен, а вес втрое меньше
            "format": f"bv*[height<={max_height}][ext=mp4]/bv*[height<={max_height}]/worst",
            "outtmpl": {"default": str(out_dir / "source.%(ext)s")},
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as exc:
            log.warning("видео %s не скачалось: %s", video.id, exc)
            return None
        found = sorted(out_dir.glob("source.*"))
        return VideoFileResult(path=found[0]) if found else None

    def collect_channel(self, url: str, *, limit: int = 0) -> CollectResult:
        import yt_dlp

        base = channel_base_url(url)
        flat_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "logger": _QuietLogger(),
        }
        if limit > 0:
            flat_opts["playlistend"] = limit

        channel_info: dict[str, Any] = {}
        tab_entries: list[tuple[dict[str, Any], bool]] = []  # (entry, is_short)
        errors: list[str] = []
        for tab, is_short in (("/videos", False), ("/shorts", True)):
            try:
                with yt_dlp.YoutubeDL(flat_opts) as ydl:
                    info = ydl.extract_info(base + tab, download=False) or {}
            except yt_dlp.utils.DownloadError as exc:
                # у канала может не быть любой из вкладок: канал из одних shorts
                # без /videos — норма (свой канал), ошибка — когда нет обеих
                if "does not have a" in str(exc):
                    log.info("вкладки %s нет — у канала таких роликов нет", tab)
                else:
                    errors.append(f"{base + tab} — {exc}")
                continue
            if not channel_info:
                channel_info = info
            tab_entries.extend((e, is_short) for e in info.get("entries") or [] if e)
        if not tab_entries and not errors:
            # публичных роликов нет (все скрыты или ещё не выложены) — канал всё
            # равно существует: подписчики и аватар берутся с корневой страницы
            try:
                with yt_dlp.YoutubeDL(flat_opts) as ydl:
                    channel_info = ydl.extract_info(base, download=False) or {}
            except yt_dlp.utils.DownloadError as exc:
                raise CollectorError(f"канал не открылся: {base} — {exc}") from exc
            log.warning("канал «%s»: публичных роликов нет", channel_info.get("channel") or base)
        elif not tab_entries:
            raise CollectorError(f"на канале не нашлось видео: {base} — {'; '.join(errors)}")

        log.info(
            "канал «%s»: %d видео в списке, собираю метаданные…",
            channel_info.get("channel") or base,
            len(tab_entries),
        )
        videos: list[VideoMeta] = []
        skipped = 0
        total = len(tab_entries)
        started = time.monotonic()
        meta_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(meta_opts) as ydl:
            for n, (entry, is_short) in enumerate(tab_entries, start=1):
                video_url = entry.get("url") or entry.get("webpage_url")
                try:
                    full = ydl.extract_info(video_url, download=False) or {}
                except yt_dlp.utils.DownloadError as exc:
                    skipped += 1
                    log.warning("видео %s пропущено: %s", entry.get("id"), exc)
                    continue
                videos.append(to_video_meta(full, is_short=is_short))
                if n % 10 == 0 or n == total:
                    log.info("  %d/%d, осталось ~%s", n, total, _eta(started, n, total))

        videos.sort(key=lambda v: v.published_at or datetime.min.replace(tzinfo=UTC))
        if limit > 0:
            videos = videos[-limit:]  # N последних по дате среди обеих вкладок

        channel = ChannelSnapshot(
            id=str(channel_info.get("channel_id") or channel_info.get("id") or base),
            url=str(channel_info.get("channel_url") or base),
            title=str(channel_info.get("channel") or channel_info.get("uploader") or ""),
            description=str(channel_info.get("description") or ""),
            subscribers=channel_info.get("channel_follower_count"),
            video_count=channel_info.get("playlist_count") if limit == 0 else None,
            avatar_url=_avatar_url(channel_info.get("thumbnails") or []),
            collected_at=datetime.now(UTC),
            collector=self.name,
        )
        if skipped:
            log.warning("пропущено видео (приватные/удалённые): %d", skipped)
        return CollectResult(data=ChannelData(channel=channel, videos=videos), cost_usd=0.0)
