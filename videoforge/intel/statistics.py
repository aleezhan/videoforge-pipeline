"""Чистая математика статистики: без сети, без LLM, без файлов.

Выброс считается не против медианы всего канала (она смешивает эпоху «3K
подписчиков» с эпохой «300K» — ранние хиты теряются, старые видео все выглядят
провалами), а против скользящей медианы соседних по дате видео той же группы.
Shorts и длинные видео сравниваются раздельно: у них разные порядки просмотров.

Порог выброса по умолчанию адаптивный: фиксированные ×3 говорят не о канале, а
о том, насколько он ровный. У Kurzgesagt (медиана 1.75M, все ролики похожи)
×3 находит 2 видео из 187, у прыгающего канала — половину ленты. Поэтому порог
берётся из разброса самой группы: медиана ratio плюс три «сигмы», оценённые по
MAD (устойчив к тем самым хитам, которые мы ищем).
"""

from __future__ import annotations

import statistics as st
from datetime import UTC, datetime
from typing import Literal

from videoforge.intel.models import (
    GroupStats,
    OutlierRef,
    StatsReport,
    VideoMeta,
    VideoStats,
)

SHORT_MAX_SEC = 183.0  # лимит YouTube Shorts — 3 минуты (+ зазор на округление)
ROLLING_WINDOW = 10  # соседей с каждой стороны
MIN_NEIGHBORS = 5  # меньше — окно нерепрезентативно, берём всю группу

DEFAULT_THRESHOLD = 3.0  # запасной порог, когда разброс посчитать не из чего
AUTO_SIGMA = 3.0  # сколько «сигм» разброса группы отделяют выброс
MIN_AUTO_THRESHOLD = 1.5  # ниже — это уже не выброс, а обычная удача
MAD_TO_SIGMA = 1.4826  # MAD → σ для нормального распределения
ENGAGEMENT_MULT = 3.0  # во столько раз выше своей нормы = вовлечение чем-то куплено

_EPOCH = datetime.min.replace(tzinfo=UTC)


def video_group(video: VideoMeta) -> str:
    if video.is_short is not None:
        return "shorts" if video.is_short else "long"
    if video.duration_sec is not None and video.duration_sec <= SHORT_MAX_SEC:
        return "shorts"
    return "long"


def rolling_median(views: list[int], index: int, *, window: int = ROLLING_WINDOW) -> float:
    """Медиана до `window` соседей с каждой стороны, не считая самого видео."""
    neighbors = views[max(0, index - window) : index] + views[index + 1 : index + 1 + window]
    if len(neighbors) < MIN_NEIGHBORS:
        neighbors = views[:index] + views[index + 1 :]
    if not neighbors:
        return float(views[index])
    return float(st.median(neighbors))


def auto_threshold(ratios: list[float]) -> float:
    """Порог выброса по разбросу самой группы: медиана ratio + 3σ по MAD.

    Ровный канал получает низкий порог и всё-таки показывает свои лучшие
    ролики; канал с постоянными скачками — высокий, и случайная удача в
    выбросы не попадает."""
    if len(ratios) < MIN_NEIGHBORS:
        return DEFAULT_THRESHOLD
    med = st.median(ratios)
    mad = st.median([abs(r - med) for r in ratios])
    if mad <= 0:  # группа без разброса — адаптировать не к чему
        return DEFAULT_THRESHOLD
    return max(MIN_AUTO_THRESHOLD, round(med + AUTO_SIGMA * MAD_TO_SIGMA * mad, 2))


def engagement(video: VideoMeta) -> float | None:
    """Комментариев на лайк. Измеряется, не судится: смысл цифре придаёт
    сравнение с нормой самого канала, а не абсолютное значение."""
    if video.likes is None or video.comments is None or video.likes <= 0:
        return None
    return round(video.comments / video.likes, 4)


def engagement_threshold(values: list[float]) -> float | None:
    """Порог ненормального вовлечения — по норме самой группы. Абсолютной шкалы
    тут нет и быть не может: у канала-воронки нормой будет то, что у сериала
    аномалия.

    Берём максимум из двух правил. Разброс по MAD, как у порога выбросов, —
    и кратность медиане, потому что на ровном канале MAD равен нулю, а это как
    раз тот случай, где одинокий «напиши слово в комментарии» виден лучше
    всего: отдать его молча было бы худшим из возможных ответов."""
    if len(values) < MIN_NEIGHBORS:
        return None
    med = st.median(values)
    if med <= 0:  # комментариев нет ни у кого — сравнивать не с чем
        return None
    mad = st.median([abs(v - med) for v in values])
    by_spread = med + AUTO_SIGMA * MAD_TO_SIGMA * mad
    return round(max(by_spread, med * ENGAGEMENT_MULT), 4)


def _sort_key(video: VideoMeta) -> tuple[datetime, str]:
    return (video.published_at or _EPOCH, video.id)


def _cadence_per_week(videos: list[VideoMeta]) -> float | None:
    dates = sorted(v.published_at for v in videos if v.published_at is not None)
    if len(dates) < 2:
        return None
    weeks = (dates[-1] - dates[0]).total_seconds() / (7 * 24 * 3600)
    if weeks <= 0:
        return None
    return round((len(dates) - 1) / weeks, 2)


def compute_stats(
    videos: list[VideoMeta], *, threshold: float | Literal["auto"] = "auto"
) -> StatsReport:
    counted = [v for v in videos if v.views is not None]
    grouped: dict[str, list[VideoMeta]] = {}
    for v in counted:
        grouped.setdefault(video_group(v), []).append(v)

    rows: list[VideoStats] = []
    groups: dict[str, GroupStats] = {}
    for group, items in grouped.items():
        items.sort(key=_sort_key)
        views = [v.views for v in items if v.views is not None]
        group_rows: list[VideoStats] = []
        for i, v in enumerate(items):
            med = rolling_median(views, i)
            ratio = round(views[i] / max(med, 1.0), 2)
            group_rows.append(
                VideoStats(
                    video_id=v.id,
                    title=v.title,
                    group=group,
                    published_at=v.published_at,
                    duration_sec=v.duration_sec,
                    views=views[i],
                    rolling_median_views=round(med, 1),
                    ratio=ratio,
                    outlier=False,  # порог группы известен только после всех ratio
                    engagement=engagement(v),
                )
            )
        # порог считается по своей группе: у shorts и длинных разная ровность
        limit = auto_threshold([r.ratio for r in group_rows]) if threshold == "auto" else threshold
        for r in group_rows:
            r.outlier = r.ratio >= limit
        # то же самое для вовлечения — по своей группе и по своей норме
        engagements = [r.engagement for r in group_rows if r.engagement is not None]
        eng_limit = engagement_threshold(engagements)
        if eng_limit is not None:
            for r in group_rows:
                r.engagement_outlier = r.engagement is not None and r.engagement >= eng_limit
        rows.extend(group_rows)
        top = max(items, key=lambda v: v.views or 0)
        groups[group] = GroupStats(
            count=len(items),
            median_views=float(st.median(views)),
            mean_views=round(st.mean(views), 1),
            max_views=top.views or 0,
            top_video_id=top.id,
            outlier_threshold=limit,
            median_engagement=round(st.median(engagements), 4) if engagements else None,
            engagement_threshold=eng_limit,
        )

    rows.sort(key=lambda r: (r.published_at or _EPOCH, r.video_id))
    outliers = sorted(
        (
            OutlierRef(
                video_id=r.video_id, title=r.title, group=r.group, views=r.views, ratio=r.ratio
            )
            for r in rows
            if r.outlier
        ),
        key=lambda o: o.ratio,
        reverse=True,
    )
    return StatsReport(
        computed_at=datetime.now(UTC),
        threshold=threshold,
        total_videos=len(videos),
        counted_videos=len(counted),
        cadence_per_week=_cadence_per_week(counted),
        groups=groups,
        videos=rows,
        outliers=outliers,
    )
