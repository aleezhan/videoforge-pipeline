"""Снимок статистики аккаунта через коллектор анализатора.

Тот же yt-dlp, что собирает чужие каналы для research/, умеет собрать и свой:
подписчики (`channel_follower_count`), просмотры и лайки по каждому ролику —
без ключей и за $0. Здесь только связка: коллектор → снимок + список роликов
в папке аккаунта. Сети без бесплатного доступа к числам (TikTok, Instagram)
сюда не заходят — для них `account sync --manual`.

С токеном YouTube (`videoforge account login`) к снимку yt-dlp добавляется
второй — официальный, из YouTube Analytics API (`source: youtube_api`): окно
последних 28 дней (просмотры, минуты, удержание, подписки, лайки, доход) и
итоги канала. Тот же stats.jsonl, два ряда с разными `source` — они несравнимы
между собой, и это записано в каждой строке. Ряд по дням уходит в
`accounts/<id>/analytics.jsonl`, только новыми датами. Без токена — как
раньше; ошибка API — предупреждение, ряд yt-dlp при этом уже на диске.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime

from videoforge.core.accounts import (
    API_SOURCE,
    Account,
    AccountVideo,
    AccountVideos,
    DayStats,
    RetentionPoint,
    StatsSnapshot,
    TrafficRow,
    VideoStats,
    add_days,
    add_snapshot,
    remember_channel_id,
    save_avatar,
    save_video_stats,
    save_videos,
)
from videoforge.core.manifest import now
from videoforge.intel.collectors import build_collector
from videoforge.intel.models import ChannelData
from videoforge.providers.base import ProviderError
from videoforge.providers.youtube_api import (
    ANALYTICS_DAYS,
    AnalyticsSnapshot,
    VideoAnalytics,
    YoutubeUpload,
    token_path,
)

log = logging.getLogger("videoforge.account_sync")

SYNCABLE: tuple[str, ...] = ("youtube",)


def channel_url(acc: Account) -> str:
    if acc.url:
        return acc.url
    if acc.handle:
        handle = acc.handle if acc.handle.startswith("@") else f"@{acc.handle}"
        return f"https://www.youtube.com/{handle}"
    if acc.channel_id:
        return f"https://www.youtube.com/channel/{acc.channel_id}"
    raise ValueError(f"у аккаунта {acc.id} нет ни url, ни handle, ни channel_id")


def _total(values: list[int | None]) -> int | None:
    """Сумма по роликам; None, если площадка не отдала ни одного числа."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def snapshot_from_channel(data: ChannelData, *, source: str, partial: bool) -> StatsSnapshot:
    """Снимок из данных коллектора. При `partial` (собрана только часть роликов)
    суммы просмотров и лайков не пишутся: частичная сумма в графике выглядит
    как падение, а это не падение — это усечённый сбор."""
    videos = data.videos
    return StatsSnapshot(
        at=data.channel.collected_at,
        source=source,
        subscribers=data.channel.subscribers,
        views_total=None if partial else _total([v.views for v in videos]),
        likes_total=None if partial else _total([v.likes for v in videos]),
        video_count=data.channel.video_count if not partial else None,
        note=f"собрано роликов: {len(videos)}" + (" (частично)" if partial else ""),
    )


def videos_from_channel(data: ChannelData, *, source: str) -> AccountVideos:
    return AccountVideos(
        collected_at=data.channel.collected_at,
        source=source,
        videos=[
            AccountVideo(
                id=v.id,
                url=v.url,
                title=v.title,
                published_at=v.published_at,
                duration_sec=v.duration_sec,
                views=v.views,
                likes=v.likes,
                comments=v.comments,
                thumbnail_url=v.thumbnail_url,
                is_short=v.is_short,
            )
            for v in sorted(
                data.videos,
                key=lambda v: v.published_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
        ],
    )


def sync_account(
    acc: Account,
    *,
    collector: str = "ytdlp",
    limit: int = 0,
    days: int = ANALYTICS_DAYS,
    log_fn: Callable[[str], None] = log.info,
) -> StatsSnapshot:
    """Собрать канал, дописать снимок, перезаписать список роликов; с токеном —
    дописать и официальный снимок. Возвращается снимок коллектора: он есть
    всегда, а строка про API уходит в `log_fn` — есть токен или нет, вышло или
    нет, человек видит это в том же выводе."""
    if acc.network not in SYNCABLE:
        raise ValueError(
            f"{acc.network_title}: числа автоматически не собираются — "
            f"videoforge account sync {acc.id} --manual subscribers=…"
        )
    result = build_collector(collector).collect_channel(channel_url(acc), limit=limit)
    # технический id канала — то, что у площадки не меняется, в отличие от
    # хэндла и ссылки; шаблон аккаунта обещает записать его здесь
    if remember_channel_id(acc, result.data.channel.id):
        log_fn(f"записан id канала: {acc.channel_id}")
    snapshot = snapshot_from_channel(result.data, source=collector, partial=limit > 0)
    add_snapshot(acc, snapshot)
    save_videos(acc, videos_from_channel(result.data, source=collector))
    save_avatar(acc, result.data.channel.avatar_url)
    if not has_token(acc):
        log_fn(f"API: токена нет — videoforge account login {acc.id}")
        return snapshot
    try:
        api, added = sync_api(acc, days=days)
    except (ProviderError, ValueError) as exc:
        # ряд коллектора уже на диске; официального не будет, и это сказано
        log.warning("Analytics API для %s: %s", acc.id, exc)
        log_fn(f"API: не вышло — {exc}")
        return snapshot
    log_fn(api_line(api, added))
    log_fn(sync_video_stats(acc, [v.id for v in result.data.videos]))
    return snapshot


# ---------------------------------------------------------------- Analytics API


def has_token(acc: Account) -> bool:
    """Есть ли у аккаунта токен YouTube — тот же файл, что ищут `doctor` и
    публикация. Только факт наличия: что он умеет, скажет Google."""
    return acc.network == "youtube" and token_path(acc.id).exists()


def api_snapshot(acc: Account, *, days: int = ANALYTICS_DAYS) -> AnalyticsSnapshot:
    """Снимок Analytics API токеном аккаунта. Отдельной функцией, чтобы тест
    подменял её и до сети не доходил."""
    return YoutubeUpload(acc.id).analytics(days=days)


def snapshot_from_analytics(api: AnalyticsSnapshot, *, at: datetime) -> StatsSnapshot:
    """Строка stats.jsonl из ответа API: итоги канала — в те же поля, что у
    yt-dlp, окно — в свои. Предупреждения API (доход не отдан, итогов нет)
    остаются в `note`: пустое поле без причины читается как «не было»."""
    t = api.totals
    note = f"окно {api.start}…{api.end}"
    if api.warnings:
        note += "; " + "; ".join(api.warnings)
    return StatsSnapshot(
        at=at,
        source=API_SOURCE,
        subscribers=api.subscribers,
        views_total=api.views_total,
        video_count=api.video_count,
        window_days=api.window_days,
        views=t.views,
        watch_minutes=t.watch_minutes,
        avg_view_sec=t.avg_view_sec,
        avg_view_pct=t.avg_view_pct,
        subs_gained=t.subs_gained,
        subs_lost=t.subs_lost,
        likes=t.likes,
        comments=t.comments,
        shares=t.shares,
        revenue_usd=t.revenue_usd,
        note=note,
    )


def days_from_analytics(api: AnalyticsSnapshot) -> list[DayStats]:
    """Дни, которые площадка досчитала: свежие в append-only файл не идут —
    их первое, частичное число легло бы навсегда."""
    return [DayStats(day=d, **asdict(row)) for d, row in api.days if d <= api.settled_until]


def sync_api(acc: Account, *, days: int = ANALYTICS_DAYS) -> tuple[StatsSnapshot, list[DayStats]]:
    """Официальный снимок: строка в stats.jsonl и новые дни в analytics.jsonl."""
    api = api_snapshot(acc, days=days)
    snapshot = snapshot_from_analytics(api, at=now())
    add_snapshot(acc, snapshot)
    return snapshot, add_days(acc, days_from_analytics(api))


def api_video(acc: Account, video_id: str) -> VideoAnalytics:
    """Один ролик через API. Отдельной функцией — тест подменяет её."""
    return YoutubeUpload(acc.id).video_analytics(video_id)


def video_stats_from_api(api: VideoAnalytics, *, at: datetime) -> VideoStats:
    t = api.totals
    return VideoStats(
        video_id=api.video_id,
        at=at,
        views=t.views,
        watch_minutes=t.watch_minutes,
        avg_view_sec=t.avg_view_sec,
        avg_view_pct=t.avg_view_pct,
        likes=t.likes,
        comments=t.comments,
        shares=t.shares,
        subs_gained=t.subs_gained,
        retention=[RetentionPoint(at=a, watching=w, relative=r) for a, w, r in api.retention],
        traffic={k: TrafficRow(views=v, minutes=m) for k, (v, m) in api.traffic.items()},
    )


def sync_video_stats(acc: Account, video_ids: list[str]) -> str:
    """Каждый ролик канала → `video_stats/<id>.api.json`. Ошибка по одному
    ролику остальные не роняет — строка для человека называет, сколько вышло
    и у скольких кривая уже есть."""
    done = curves = 0
    failed: list[str] = []
    for vid in video_ids:
        try:
            stats = video_stats_from_api(api_video(acc, vid), at=now())
        except (ProviderError, ValueError, KeyError) as exc:
            log.warning("статистика ролика %s: %s", vid, exc)
            failed.append(vid)
            continue
        save_video_stats(acc, stats)
        done += 1
        curves += bool(stats.retention)
    line = f"API по роликам: {done} из {len(video_ids)}, кривая удержания у {curves}"
    if failed:
        line += f" · не вышло: {', '.join(failed[:5])}"
    return line


def api_line(snapshot: StatsSnapshot, added: list[DayStats]) -> str:
    """Одна строка для человека: окно, часы, удержание, сколько дней дописано."""

    def n(value: int | None) -> str:
        return "?" if value is None else f"{value:,}".replace(",", " ")

    hours = "?" if snapshot.watch_minutes is None else f"{snapshot.watch_minutes / 60:.1f}"
    hold = "?" if snapshot.avg_view_pct is None else f"{snapshot.avg_view_pct:.0f}%"
    line = (
        f"API: просмотров за {snapshot.window_days} дн. {n(snapshot.views)} · часов {hours}"
        f" · удержание {hold} · подписчиков +{n(snapshot.subs_gained)}/−{n(snapshot.subs_lost)}"
        f" · дней дописано {len(added)}"
    )
    if snapshot.revenue_usd is not None:
        line += f" · доход ${snapshot.revenue_usd:.2f}"
    return line
