"""Аккаунты на площадках: файл из шаблона, привязка к проекту, снимки статистики."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from videoforge.core.accounts import (
    StatsSnapshot,
    accounts_for_project,
    add_snapshot,
    create_account,
    latest_known,
    latest_snapshot,
    list_accounts,
    load_account,
    load_days,
    load_history,
    load_video_stats,
    load_videos,
    manual_snapshot,
    parse_manual,
    platforms_of,
    project_status,
    remember_channel_id,
    validate_account,
)
from videoforge.core.project import ProjectSpec, create_project
from videoforge.intel import account_sync
from videoforge.intel.account_sync import channel_url, sync_account
from videoforge.providers.base import ProviderError
from videoforge.providers.youtube_api import (
    AnalyticsRow,
    AnalyticsSnapshot,
    VideoAnalytics,
    save_token,
)


def test_account_scaffold_and_project_binding(projects_root: Path, accounts_root: Path) -> None:
    create_project(ProjectSpec(id="chan", platforms=["youtube"]))
    path, problems = create_account(
        "chan_yt", network="youtube", project="chan", handle="@chan", title="Канал"
    )
    assert path == accounts_root / "chan_yt.yaml" and problems == []
    assert "# Аккаунт на площадке" in path.read_text(encoding="utf-8")
    assert (accounts_root / "chan_yt").is_dir()

    acc = load_account("chan_yt")
    assert acc.network == "youtube" and acc.project == "chan" and acc.title == "Канал"
    assert acc.network_title == "YouTube"
    assert [a.id for a in list_accounts()] == ["chan_yt"]
    assert [a.id for a in accounts_for_project("chan")] == ["chan_yt"]

    # статус проекта выводится из наличия аккаунта, а не хранится
    create_project(ProjectSpec(id="draft"))
    assert project_status("chan") == "live" and project_status("draft") == "demo"

    with pytest.raises(FileExistsError):
        create_account("chan_yt", network="youtube", project="chan")
    with pytest.raises(ValueError):
        create_account("x", network="vk", project="chan")
    with pytest.raises(ValueError):  # проект проверяется в момент создания
        create_account("x", network="tiktok", project="nobody")


def test_validate_reports_missing_project(projects_root: Path, accounts_root: Path) -> None:
    create_project(ProjectSpec(id="chan"))
    create_account("a", network="tiktok", project="chan", handle="@a")
    (accounts_root / "a.yaml").write_text(
        "id: a\nnetwork: tiktok\nproject: gone\n", encoding="utf-8"
    )
    acc = load_account("a")
    problems = validate_account(acc)
    assert any("gone" in p for p in problems) and any("url" in p for p in problems)
    # список не падает из-за одного битого файла
    (accounts_root / "broken.yaml").write_text("network: mars\nproject: chan\n", encoding="utf-8")
    assert [a.id for a in list_accounts()] == ["a"]


def test_platforms_of_network() -> None:
    assert platforms_of("youtube") == ["youtube", "youtube_shorts"]
    assert platforms_of("instagram") == ["instagram_reels"]


def test_snapshots_append_only(projects_root: Path, accounts_root: Path) -> None:
    create_project(ProjectSpec(id="chan"))
    create_account("a", network="youtube", project="chan", url="https://youtube.com/@a")
    acc = load_account("a")
    assert latest_snapshot(acc) is None
    first = StatsSnapshot(at=datetime(2026, 9, 1, tzinfo=UTC), source="manual", subscribers=10)
    second = StatsSnapshot(at=datetime(2026, 9, 8, tzinfo=UTC), source="manual", subscribers=25)
    add_snapshot(acc, second)  # порядок в файле не важен — история сортируется по дате
    path = add_snapshot(acc, first)
    assert path == accounts_root / "a" / "stats.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    history = load_history(acc)
    assert [s.subscribers for s in history] == [10, 25]
    last = latest_snapshot(acc)
    assert last is not None and last.subscribers == 25


def test_manual_snapshot_parsing() -> None:
    values = parse_manual(["subscribers=1_200", "views_total=34_000"])
    assert values == {"subscribers": 1200, "views_total": 34000}
    # одним аргументом через пробел или запятую — тоже
    assert parse_manual(["subscribers=5 likes_total=7,video_count=2"]) == {
        "subscribers": 5,
        "likes_total": 7,
        "video_count": 2,
    }
    snap = manual_snapshot(values, note="со скриншота")
    assert snap.source == "manual" and snap.likes_total is None and snap.note == "со скриншота"
    with pytest.raises(ValueError):
        parse_manual(["subscribers"])
    with pytest.raises(ValueError):
        parse_manual(["subscribers=many"])
    with pytest.raises(ValueError):
        manual_snapshot({"followers": 1})
    with pytest.raises(ValueError):
        manual_snapshot({})


def test_sync_via_collector(projects_root: Path, accounts_root: Path) -> None:
    create_project(ProjectSpec(id="chan"))
    create_account("a", network="youtube", project="chan", handle="demo")
    acc = load_account("a")
    assert channel_url(acc) == "https://www.youtube.com/@demo"

    snap = sync_account(acc, collector="mock")
    assert snap.source == "mock" and snap.subscribers == 15000
    assert snap.views_total and snap.views_total > 0 and snap.video_count
    assert latest_snapshot(acc) == load_history(acc)[-1]
    videos = load_videos(acc)
    assert videos is not None and len(videos.videos) == snap.video_count
    # новые сверху: страница аккаунта показывает свежее первым
    dates = [v.published_at for v in videos.videos if v.published_at]
    assert dates == sorted(dates, reverse=True)

    # частичный сбор не пишет суммы — иначе в графике это выглядит как падение
    partial = sync_account(acc, collector="mock", limit=5)
    assert partial.views_total is None and partial.video_count is None
    assert partial.subscribers == 15000 and len(load_history(acc)) == 2


def test_sync_writes_down_the_channel_id_once(projects_root: Path, accounts_root: Path) -> None:
    """Шаблон аккаунта обещает «пусто — определится при sync», и до 15.09.2026
    это было неправдой: поле оставалось пустым у канала, который синхронизировали
    десяток раз. Уже записанный id не переписывается — ссылка и хэндл у канала
    меняются, id нет, и молчаливая подмена означала бы другой аккаунт."""
    create_project(ProjectSpec(id="chan"))
    create_account("a", network="youtube", project="chan", handle="demo")
    acc = load_account("a")
    assert acc.channel_id == ""

    sync_account(acc, collector="mock")
    written = load_account("a").channel_id
    assert written and load_account("a").handle == "demo"  # остальное не тронуто
    text = (accounts_root / "a.yaml").read_text(encoding="utf-8")
    assert "#" in text and text.count("channel_id:") == 1  # комментарии шаблона целы

    assert remember_channel_id(load_account("a"), "UCдругой") is False
    assert load_account("a").channel_id == written
    assert remember_channel_id(load_account("a"), "") is False


def test_sync_refuses_networks_without_numbers(projects_root: Path, accounts_root: Path) -> None:
    create_project(ProjectSpec(id="chan"))
    create_account("t", network="tiktok", project="chan", handle="@t")
    with pytest.raises(ValueError, match="manual"):
        sync_account(load_account("t"), collector="mock")


# ---------------------------------------------------------------- Analytics API


def test_analytics_report_is_parsed_by_column_names() -> None:
    """Колонки идут в порядке запроса, но читаются по именам: перепутанный
    порядок не должен поменять лайки с комментариями местами."""
    from videoforge.providers.youtube_api import analytics_row, parse_report

    payload = {
        "columnHeaders": [
            {"name": "comments"},
            {"name": "views"},
            {"name": "estimatedMinutesWatched"},
            {"name": "estimatedRevenue"},
        ],
        "rows": [[3, 120, 45.5, 0.42]],
    }
    rows = parse_report(payload)
    assert rows == [
        {"comments": 3, "views": 120, "estimatedMinutesWatched": 45.5, "estimatedRevenue": 0.42}
    ]
    row = analytics_row(rows[0])
    assert row.views == 120 and row.comments == 3 and row.watch_minutes == 45.5
    assert row.revenue_usd == 0.42
    assert row.likes is None and row.avg_view_pct is None  # чего нет — None, не нуль
    assert parse_report({}) == []


class _FakeGoogle:
    """Ответы Google без сети: окно, окно по дням, итоги канала.

    `money`: ok — доход отдаётся; 403 — отказ на любой запрос с ним.
    `refuse` — отказ по правам на всё."""

    def __init__(self, *, money: str = "ok", refuse: bool = False) -> None:
        self.money = money
        self.refuse = refuse
        self.calls: list[dict[str, str]] = []

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str], timeout: float):  # type: ignore[no-untyped-def]
        import httpx

        assert headers["Authorization"] == "Bearer токен"
        self.calls.append({"url": url, **params})
        scope_error = {"error": {"message": "Request had insufficient authentication scopes."}}
        if self.refuse:
            return httpx.Response(403, json=scope_error)
        if url.endswith("/channels"):
            stats = {"subscriberCount": "18", "viewCount": "20", "videoCount": "4"}
            return httpx.Response(200, json={"items": [{"statistics": stats}]})
        metrics = params["metrics"].split(",")
        if "estimatedRevenue" in metrics and self.money == "403":
            return httpx.Response(403, json={"error": {"message": "Insufficient Permission"}})
        totals: list[Any] = [120, 45.5, 30.2, 41.0, 2, 1, 5, 3, 1]
        if "estimatedRevenue" in metrics:
            totals.append(0.42)
        if params.get("dimensions") == "day":
            names = ["day", *metrics]
            rows = [["2026-09-13", *totals], ["2026-09-11", *totals], ["2026-09-10", *totals]]
        else:
            names, rows = metrics, [totals]
        return httpx.Response(
            200, json={"columnHeaders": [{"name": n} for n in names], "rows": rows}
        )


def test_analytics_snapshot_survives_money_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoforge.providers import youtube_api
    from videoforge.providers.youtube_api import SCOPE_MONEY, SCOPE_STATS, analytics_snapshot

    today = date(2026, 9, 14)
    both = frozenset({SCOPE_STATS, SCOPE_MONEY})

    fake = _FakeGoogle(money="403")
    monkeypatch.setattr(youtube_api.httpx, "get", fake.get)
    snap = analytics_snapshot("токен", granted=both, days=28, today=today)
    assert (snap.start, snap.end, snap.window_days) == (date(2026, 8, 17), date(2026, 9, 13), 28)
    assert snap.totals.views == 120 and snap.totals.watch_minutes == 45.5
    assert snap.totals.avg_view_pct == 41.0 and snap.totals.subs_gained == 2
    # отказ по доходу — не отказ снимка: поле пусто, причина записана
    assert snap.totals.revenue_usd is None and any("доход" in w for w in snap.warnings)
    assert (snap.subscribers, snap.views_total, snap.video_count) == (18, 20, 4)
    assert snap.settled_until == date(2026, 9, 11)
    assert [d for d, _ in snap.days] == [date(2026, 9, 13), date(2026, 9, 11), date(2026, 9, 10)]
    # окно с доходом (403) → окно без → дни без → итоги канала
    with_money = [c for c in fake.calls if "estimatedRevenue" in c.get("metrics", "")]
    assert len(with_money) == 1 and len(fake.calls) == 4

    # без права на доход его и не спрашивают
    fake = _FakeGoogle()
    monkeypatch.setattr(youtube_api.httpx, "get", fake.get)
    snap = analytics_snapshot("токен", granted=frozenset({SCOPE_STATS}), today=today)
    assert snap.totals.revenue_usd is None and snap.warnings == []
    assert not any("estimatedRevenue" in c.get("metrics", "") for c in fake.calls)

    # с правом и без отказа — доход в снимке, три запроса
    fake = _FakeGoogle()
    monkeypatch.setattr(youtube_api.httpx, "get", fake.get)
    snap = analytics_snapshot("токен", granted=both, today=today)
    assert snap.totals.revenue_usd == 0.42 and len(fake.calls) == 3
    assert snap.days[0][1].revenue_usd == 0.42

    # отказ не про доход — ошибка со словами, что делать, а не пустой снимок
    fake = _FakeGoogle(refuse=True)
    monkeypatch.setattr(youtube_api.httpx, "get", fake.get)
    with pytest.raises(ProviderError, match="повторный вход"):
        analytics_snapshot("токен", granted=both, today=today)


def _api_stub(today: date, days: int = 28) -> AnalyticsSnapshot:
    """Снимок, как его вернул бы провайдер: окно, три дня (один ещё не
    устоялся), итоги канала, доход не отдан."""
    row = AnalyticsRow(
        views=120,
        watch_minutes=45.5,
        avg_view_sec=30.2,
        avg_view_pct=41.0,
        subs_gained=2,
        subs_lost=1,
        likes=5,
        comments=3,
        shares=1,
    )
    return AnalyticsSnapshot(
        start=today - timedelta(days=days),
        end=today - timedelta(days=1),
        totals=row,
        days=[
            (today - timedelta(days=1), AnalyticsRow(views=10)),
            (today - timedelta(days=3), AnalyticsRow(views=20)),
            (today - timedelta(days=4), AnalyticsRow(views=30)),
        ],
        settled_until=today - timedelta(days=3),
        subscribers=18,
        views_total=20,
        video_count=4,
        warnings=["доход не отдан (403: Insufficient Permission) — revenue_usd пуст"],
    )


def _video_stub(video_id: str) -> VideoAnalytics:
    """Ролик, как его отдал бы провайдер: кривая из пяти точек со спадом
    посередине, два источника."""
    return VideoAnalytics(
        video_id=video_id,
        start=date(2020, 1, 1),
        end=date(2026, 9, 28),
        totals=AnalyticsRow(views=100, avg_view_sec=14.0, avg_view_pct=30.0),
        retention=[(0.0, 1.0, 0.5), (0.25, 0.9, 0.5), (0.5, 0.4, 0.4), (0.75, 0.35, 0.4)]
        if video_id.endswith("1")
        else [],
        traffic={"YT_SEARCH": (16, 3.0), "SHORTS": (4, 0.5)},
    )


def test_sync_video_stats_writes_one_file_per_video_and_survives_a_failure(
    yt_account: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def api(acc: Any, vid: str) -> VideoAnalytics:
        if vid == "bad":
            raise ProviderError("Analytics API отказал по ролику: 500")
        return _video_stub(vid)

    monkeypatch.setattr(account_sync, "api_video", api)
    line = account_sync.sync_video_stats(yt_account, ["v1", "bad", "v2"])
    assert line == "API по роликам: 2 из 3, кривая удержания у 1 · не вышло: bad"
    stats = load_video_stats(yt_account, "v1")
    assert stats is not None and stats.views == 100 and len(stats.retention) == 4
    assert stats.traffic["YT_SEARCH"].views == 16
    assert load_video_stats(yt_account, "bad") is None


@pytest.fixture
def yt_account(
    projects_root: Path, accounts_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Аккаунт youtube с изолированным .secrets/: токена нет, пока тест не положит.

    `channel_id` проставлен заранее: иначе первая же синхронизация допишет его в
    файл и своей строкой в лог, а тесты ниже проверяют строки про API дословно.
    Как id попадает в файл — проверяет `test_sync_writes_down_the_channel_id_once`."""
    monkeypatch.setenv("VF_SECRETS_DIR", str(tmp_path / ".secrets"))
    create_project(ProjectSpec(id="chan"))
    create_account("a", network="youtube", project="chan", handle="demo")
    acc = load_account("a")
    remember_channel_id(acc, "mock_channel")
    return load_account("a")


def test_sync_without_token_does_not_call_api(
    yt_account: Any, accounts_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(acc: Any, *, days: int) -> AnalyticsSnapshot:
        raise AssertionError("без токена к API ходить нельзя")

    monkeypatch.setattr(account_sync, "api_snapshot", boom)
    lines: list[str] = []
    snap = sync_account(yt_account, collector="mock", log_fn=lines.append)
    assert snap.source == "mock" and [s.source for s in load_history(yt_account)] == ["mock"]
    assert lines == ["API: токена нет — videoforge account login a"]
    assert not (accounts_root / "a" / "analytics.jsonl").exists()


def test_sync_with_token_adds_api_row_and_settled_days(
    yt_account: Any, accounts_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_token("a", "cid", "secret", "refresh")  # значения не читаются — важен факт файла
    today = datetime.now(UTC).date()
    monkeypatch.setattr(account_sync, "api_snapshot", lambda acc, *, days: _api_stub(today, days))
    monkeypatch.setattr(account_sync, "api_video", lambda acc, vid: _video_stub(vid))
    lines: list[str] = []
    sync_account(yt_account, collector="mock", log_fn=lines.append)
    assert lines[-1].startswith("API по роликам:")
    lines.pop()

    history = load_history(yt_account)
    assert [s.source for s in history] == ["mock", "youtube_api"]
    api = history[-1]
    assert api.window_days == 28 and api.views == 120 and api.watch_minutes == 45.5
    assert api.subs_gained == 2 and api.subs_lost == 1 and api.revenue_usd is None
    assert (api.subscribers, api.views_total, api.video_count) == (18, 20, 4)
    assert "доход не отдан" in api.note
    # из двух снимков одного дня число берёт официальный ряд, хотя mock дал 15000
    assert latest_known(history)["subscribers"] == {
        "value": 18,
        "at": api.at,
        "source": "youtube_api",
    }
    assert lines == [
        "API: просмотров за 28 дн. 120 · часов 0.8 · удержание 41%"
        " · подписчиков +2/−1 · дней дописано 2"
    ]
    # по дням — только устоявшиеся: вчерашний день ещё досчитывается
    days = load_days(yt_account)
    assert [d.day for d in days] == [today - timedelta(days=4), today - timedelta(days=3)]
    assert [d.views for d in days] == [30, 20] and days[0].source == "youtube_api"
    path = accounts_root / "a" / "analytics.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2

    # повторный sync за тот же день дни не дублирует, снимки — дописывает
    sync_account(yt_account, collector="mock", log_fn=lines.append)
    assert load_days(yt_account) == days and lines[-2].endswith("дней дописано 0")
    assert [s.source for s in load_history(yt_account)] == ["mock", "youtube_api"] * 2


def test_sync_api_failure_keeps_collector_row(
    yt_account: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_token("a", "cid", "secret", "refresh")

    def refused(acc: Any, *, days: int) -> AnalyticsSnapshot:
        raise ProviderError("Analytics API отказал: 403: прав у токена не хватает")

    monkeypatch.setattr(account_sync, "api_snapshot", refused)
    lines: list[str] = []
    snap = sync_account(yt_account, collector="mock", log_fn=lines.append)
    assert snap.source == "mock" and [s.source for s in load_history(yt_account)] == ["mock"]
    assert lines == ["API: не вышло — Analytics API отказал: 403: прав у токена не хватает"]


def test_latest_known_prefers_api_row_on_the_same_day() -> None:
    """yt-dlp читает «15,4 тыс.» со страницы, API отдаёт точное; по секунде
    записи число прыгало бы между рядами."""
    history = [
        StatsSnapshot(
            at=datetime(2026, 9, 13, 10, 0, tzinfo=UTC), source="youtube_api", subscribers=15387
        ),
        StatsSnapshot(
            at=datetime(2026, 9, 13, 10, 5, tzinfo=UTC),
            source="ytdlp",
            subscribers=15400,
            video_count=4,
        ),
    ]
    known = latest_known(history)
    assert known["subscribers"] is not None and known["subscribers"]["value"] == 15387
    assert known["subscribers"]["source"] == "youtube_api"
    assert known["video_count"] is not None and known["video_count"]["value"] == 4  # у API нет
    # на следующий день без токена — свежее число yt-dlp, а не вчерашнее API
    history.append(
        StatsSnapshot(
            at=datetime(2026, 9, 14, 10, 0, tzinfo=UTC), source="ytdlp", subscribers=15500
        )
    )
    assert latest_known(history)["subscribers"] == {
        "value": 15500,
        "at": history[-1].at,
        "source": "ytdlp",
    }
