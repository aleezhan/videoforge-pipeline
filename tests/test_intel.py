"""Анализатор на заглушках: создание анализа, сквозной прогон, ворота, бюджет,
идемпотентность и математика выбросов."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from videoforge.core.io import read_model
from videoforge.core.manifest import load_manifest
from videoforge.core.project import approve_stage
from videoforge.core.stage import RunOptions
from videoforge.intel.analysis import AnalysisSpec, create_analysis, load_analysis_from_dir
from videoforge.intel.collectors.mock import MockCollector
from videoforge.intel.mock_llm import MockIntelLlm
from videoforge.intel.models import (
    ClassifyReport,
    DeepReport,
    DeepVideoAnalysis,
    Formula,
    StatsReport,
    VideoMeta,
)
from videoforge.intel.runner import run_analysis
from videoforge.intel.statistics import compute_stats


def _stats(adir: Path) -> StatsReport:
    return read_model(adir / "02_stats" / "stats.json", StatsReport)


def _classify(adir: Path) -> ClassifyReport:
    return read_model(adir / "03_classify" / "classify.json", ClassifyReport)


# ---------------------------------------------------------------- создание


def test_create_analysis(analysis: Path) -> None:
    text = (analysis / "analysis.yaml").read_text(encoding="utf-8")
    assert "#" in text  # шаблон с комментариями, не model_dump
    cfg = load_analysis_from_dir(analysis)
    assert cfg.id == "chan" and cfg.mode == "channel" and cfg.collector == "mock"
    assert cfg.rpm_usd == {}  # пусто = брать оценку LLM из classify
    assert cfg.language == "ru"
    m = load_manifest(analysis)
    assert m.workflow == "analyze_channel" and m.budget_usd == 3.0

    with pytest.raises(FileExistsError):
        create_analysis(AnalysisSpec(id="chan", urls=["x"]))


def test_unimplemented_modes_refused(research_root: Path) -> None:
    with pytest.raises(ValueError, match="фаза 3"):
        create_analysis(AnalysisSpec(id="v", urls=["x"], mode="video"))
    with pytest.raises(ValueError, match="фаза 4"):
        create_analysis(AnalysisSpec(id="c", urls=["x", "y"], mode="competitors"))


# ---------------------------------------------------------------- прогон


def test_end_to_end_mock(analysis: Path) -> None:
    coll, llm = MockCollector(), MockIntelLlm()
    report = run_analysis(analysis, coll, RunOptions(raise_errors=True), llm=llm)
    assert report.awaiting_approval == "stats"  # ворота: человек смотрит выбросы

    m = load_manifest(analysis)
    assert m.stages["collect"].status == "done"
    assert m.stages["stats"].status == "done"
    assert m.spent_usd == 0
    assert m.providers == {"collector": "mock", "llm": "mock"}

    stats = _stats(analysis)
    assert set(stats.groups) == {"long", "shorts"}
    assert stats.counted_videos == 80
    outlier_ids = [o.video_id for o in stats.outliers]
    # посаженные выбросы найдены, самый сильный — первым
    assert outlier_ids[0] == "long040"
    assert "long050" in outlier_ids
    assert "long010" in outlier_ids  # ранний хит: виден только скользящей медиане
    assert "short12" in outlier_ids  # shorts сравниваются со своей группой

    approve_stage(analysis, "stats")
    report = run_analysis(analysis, coll, RunOptions(raise_errors=True), llm=llm)
    assert report.awaiting_approval == "classify"  # вторые ворота: ниша и форматы

    cls = _classify(analysis)
    assert cls.taxonomy.niche == "mock niche"
    assert [f.id for f in cls.taxonomy.formats] == ["mock_a", "mock_b"]
    assert len(cls.videos) == 80  # классифицировано всё
    assert {f.format for f in cls.formats} == {"mock_a", "mock_b"}
    assert cls.taxonomy.rpm_suggestion == {"long": (1.0, 2.0), "shorts": (0.1, 0.2)}

    approve_stage(analysis, "classify")
    report = run_analysis(analysis, coll, RunOptions(raise_errors=True), llm=llm)
    assert report.awaiting_approval == "formula"  # последние ворота — перед продакшеном

    deep = read_model(analysis / "04_deep" / "deep.json", DeepReport)
    done = {v.video_id for v in deep.videos if v.status == "done"}
    assert len(done) == 7  # deep_top=5 хитов + deep_worst=2 контрольных
    roles = {v.video_id: v.role for v in deep.videos}
    assert sum(1 for r in roles.values() if r == "control") == 2
    # топ по ratio: все посаженные выбросы разобраны
    assert {"long040", "short12", "long050", "long010"} <= done
    a = read_model(analysis / "04_deep" / "long040" / "analysis.json", DeepVideoAnalysis)
    assert a.hook.text == "Mock opening question?"
    assert a.llm == "mock" and len(a.structure) == 2
    assert (analysis / "04_deep" / "long040" / "transcript.mock.vtt").exists()

    formula = read_model(analysis / "05_formula" / "formula.json", Formula)
    assert formula.llm == "mock" and formula.video_ideas[0].title == "Mock idea"
    blueprint = (analysis / "05_formula" / "blueprint.md").read_text(encoding="utf-8")
    assert "Production blueprint" in blueprint and "Mock idea" in blueprint

    approve_stage(analysis, "formula")
    report = run_analysis(analysis, coll, RunOptions(raise_errors=True), llm=llm)
    assert report.awaiting_approval == "distill"  # правила уходят в планнер — ещё одни ворота

    approve_stage(analysis, "distill")
    report = run_analysis(analysis, coll, RunOptions(raise_errors=True), llm=llm)
    assert report.ok, report.reason


def test_rerun_is_idempotent(analysis: Path) -> None:
    coll, llm = MockCollector(), MockIntelLlm()
    opts = RunOptions(auto_approve=True, raise_errors=True)
    run_analysis(analysis, coll, opts, llm=llm)
    llm_calls = llm.calls
    # таксономия + 80 видео по 25 + 7 deep + 7 visual + формула + distill
    assert llm_calls == 1 + 4 + 7 + 7 + 1 + 1
    assert coll.transcript_calls == 7
    assert coll.video_calls == 7  # видео качались только ради визуального разбора
    assert llm.image_calls == 7  # и ровно один запрос с кадрами на ролик
    report = run_analysis(analysis, coll, opts, llm=llm)
    assert report.ok
    assert coll.calls == 1  # второй прогон ничего не перекачивал
    assert coll.transcript_calls == 7  # и субтитры не перекачивал
    assert coll.video_calls == 7  # и видео тоже
    assert llm.calls == llm_calls  # и не переспрашивал LLM
    assert [(n, r.status) for n, r in report.executed] == [
        ("collect", "skipped"),
        ("stats", "skipped"),
        ("classify", "skipped"),
        ("deep", "skipped"),
        ("visual", "skipped"),
        ("formula", "skipped"),
        ("distill", "skipped"),
    ]


def test_skipped_stage_keeps_timings_of_the_real_run(analysis: Path) -> None:
    """Повторный прогон не должен стирать, сколько стадия шла на самом деле."""
    opts = RunOptions(auto_approve=True, raise_errors=True)
    run_analysis(analysis, MockCollector(), opts, llm=MockIntelLlm())
    first = {
        name: (r.started_at, r.finished_at) for name, r in load_manifest(analysis).stages.items()
    }
    assert all(started and finished for started, finished in first.values())

    run_analysis(analysis, MockCollector(), opts, llm=MockIntelLlm())
    after = load_manifest(analysis).stages
    assert all(after[name].status == "skipped" for name in first)
    assert {name: (r.started_at, r.finished_at) for name, r in after.items()} == first


def test_visual_stage_measures_shots_and_asks_with_frames(analysis: Path) -> None:
    """Визуальная стадия: реальный ffmpeg по видео + один запрос с кадрами на ролик."""
    from videoforge.intel.models import VisualReport, VisualVideoAnalysis

    coll, llm = MockCollector(), MockIntelLlm()
    run_analysis(analysis, coll, RunOptions(auto_approve=True, raise_errors=True), llm=llm)

    index = read_model(analysis / "06_visual" / "visual.json", VisualReport)
    assert [e.status for e in index.videos] == ["done"] * 7

    vdir = analysis / "06_visual" / "long040"
    a = read_model(vdir / "visual.json", VisualVideoAnalysis)
    assert a.llm == "mock" and a.frames_analyzed > 0
    assert a.shots.duration_sec > 0  # длительность измерена ffmpeg, а не выдумана
    assert a.shots.shot_count >= 1
    assert a.camera_moves and a.transferable
    assert sorted(p.name for p in vdir.glob("frame_*.jpg"))  # кадры лежат рядом
    assert (vdir / "source.mp4").exists()

    # числа монтажа доехали до формулы и до брифа
    formula = read_model(analysis / "05_formula" / "formula.json", Formula)
    assert formula.shot_recipe.scene_duration_sec == 2.0
    blueprint = (analysis / "05_formula" / "blueprint.md").read_text(encoding="utf-8")
    assert "Монтаж: измеренные числа" in blueprint and "scene_duration_sec" in blueprint


def test_visual_stage_survives_a_collector_without_video(analysis: Path) -> None:
    """Коллектор, который не умеет качать видео, не роняет конвейер."""

    class NoVideoCollector(MockCollector):
        def download_video(self, video, *, out_dir, max_height=480):  # type: ignore[no-untyped-def]
            return None

    report = run_analysis(
        analysis,
        NoVideoCollector(),
        RunOptions(auto_approve=True, raise_errors=True),
        llm=MockIntelLlm(),
    )
    statuses = dict((n, r.status) for n, r in report.executed)
    assert statuses["visual"] == "failed"  # разбирать нечего — стадия честно падает
    assert statuses["deep"] == "done"  # но всё, что до неё, сделано


def test_collector_switch_invalidates_snapshot(analysis: Path) -> None:
    opts = RunOptions(auto_approve=True, raise_errors=True)
    run_analysis(analysis, MockCollector(), opts, llm=MockIntelLlm())
    other = MockCollector()
    other.name = "mock2"  # как будто переключились с ytdlp на youtube_api
    report = run_analysis(analysis, other, opts, llm=MockIntelLlm())
    assert report.ok
    assert other.calls == 1  # снапшот чужого коллектора устарел — пересобрано
    # разборы привязаны к видео и не переделываются, формула — да (classify обновился)
    assert [(n, r.status) for n, r in report.executed] == [
        ("collect", "done"),
        ("stats", "done"),
        ("classify", "done"),
        ("deep", "skipped"),
        ("visual", "skipped"),
        ("formula", "done"),
        ("distill", "done"),
    ]


def test_llm_switch_invalidates_classify_and_deep(analysis: Path) -> None:
    opts = RunOptions(auto_approve=True, raise_errors=True)
    run_analysis(analysis, MockCollector(), opts, llm=MockIntelLlm())
    other = MockIntelLlm()
    other.name = "mock2"  # как будто переключились с claude_cli на gemini
    report = run_analysis(analysis, MockCollector(), opts, llm=other)
    assert report.ok
    assert other.calls > 0  # классификация, разборы и формула переделаны новым LLM
    statuses = dict((n, r.status) for n, r in report.executed)
    assert statuses == {
        "collect": "skipped",
        "stats": "skipped",
        "classify": "done",
        "deep": "done",
        "visual": "done",
        "formula": "done",
        "distill": "done",
    }
    assert _classify(analysis).llm == "mock2"
    a = read_model(analysis / "04_deep" / "long040" / "analysis.json", DeepVideoAnalysis)
    assert a.llm == "mock2"
    assert read_model(analysis / "05_formula" / "formula.json", Formula).llm == "mock2"


def test_deep_explicit_video_list(research_root: Path) -> None:
    adir = create_analysis(
        AnalysisSpec(id="pick", urls=["u"], collector="mock", deep_videos=["long000", "short01"])
    )
    run_analysis(
        adir, MockCollector(), RunOptions(auto_approve=True, raise_errors=True), llm=MockIntelLlm()
    )
    deep = read_model(adir / "04_deep" / "deep.json", DeepReport)
    assert [v.video_id for v in deep.videos] == ["long000", "short01"]
    assert (adir / "04_deep" / "long000" / "analysis.json").exists()


def test_budget_guard_blocks_before_collecting(analysis: Path) -> None:
    coll = MockCollector()
    coll.price_per_channel_usd = 10.0  # дороже бюджета анализа ($3)
    report = run_analysis(analysis, coll, RunOptions(auto_approve=True, raise_errors=True))
    assert report.stopped_at == "collect"
    assert report.reason and "бюджет" in report.reason
    m = load_manifest(analysis)
    assert m.stages["collect"].status == "blocked"
    assert m.spent_usd == 0
    assert coll.calls == 0
    assert not (analysis / "01_collect" / "channel.json").exists()


def test_max_videos_limit(research_root: Path) -> None:
    adir = create_analysis(AnalysisSpec(id="lim", urls=["u"], collector="mock", max_videos=10))
    run_analysis(
        adir, MockCollector(), RunOptions(auto_approve=True, raise_errors=True), llm=MockIntelLlm()
    )
    stats = _stats(adir)
    assert stats.counted_videos == 10


# ---------------------------------------------------------------- математика


def _video(
    i: int,
    views: int,
    *,
    duration: float = 600,
    likes: int | None = None,
    comments: int | None = None,
) -> VideoMeta:
    return VideoMeta(
        id=f"v{i:03d}",
        views=views,
        duration_sec=duration,
        likes=likes,
        comments=comments,
        published_at=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(weeks=i),
    )


def test_rolling_median_catches_local_spike() -> None:
    """Ранний хит виден на фоне соседей, поздние большие цифры — не выбросы."""
    videos = [_video(i, 900 if i == 7 else 100) for i in range(15)]
    videos += [_video(i, 10_000) for i in range(15, 30)]
    report = compute_stats(videos, threshold=3.0)
    assert [o.video_id for o in report.outliers] == ["v007"]


def test_groups_are_compared_separately() -> None:
    """Показатели shorts не делают выбросами длинные видео и наоборот."""
    videos = [_video(i, 100, duration=600) for i in range(10)]
    videos += [_video(100 + i, 10_000, duration=45) for i in range(10)]
    report = compute_stats(videos, threshold=3.0)
    assert report.outliers == []
    assert report.groups["long"].count == 10
    assert report.groups["shorts"].count == 10


def test_videos_without_views_are_excluded() -> None:
    videos = [_video(i, 100) for i in range(6)]
    videos.append(VideoMeta(id="noviews"))
    report = compute_stats(videos, threshold=3.0)
    assert report.total_videos == 7
    assert report.counted_videos == 6


def test_control_group_skips_fresh_videos() -> None:
    """Контроль — это слабые ролики, а не свежие.

    Свежее видео проигрывает соседям просто потому, что не успело набрать
    просмотры; без порога возраста «худшие по ratio» — всегда последние
    опубликованные, и контрольная группа мерила бы возраст, а не качество."""
    from videoforge.intel.models import AnalysisConfig
    from videoforge.intel.stages.deep import select_deep_videos

    now = datetime.now(UTC)
    videos = [
        VideoMeta(
            id=f"old{i}", views=1000, duration_sec=600, published_at=now - timedelta(days=400 - i)
        )
        for i in range(12)
    ]
    videos.append(  # старый провал — он и должен попасть в контроль
        VideoMeta(id="flop", views=10, duration_sec=600, published_at=now - timedelta(days=300))
    )
    videos.append(  # вчерашний ролик: просмотров мало, но судить о нём рано
        VideoMeta(id="fresh", views=5, duration_sec=600, published_at=now - timedelta(days=1))
    )
    stats = compute_stats(videos, threshold="auto")

    config = AnalysisConfig(id="x", urls=["u"], deep_top=2, deep_worst=1)
    picked = select_deep_videos(stats, config, {v.id for v in videos})
    controls = [vid for vid, role in picked if role == "control"]
    assert controls == ["flop"]
    assert "fresh" not in [vid for vid, _ in picked]

    # deep_worst=0 — контрольной группы нет вовсе
    no_control = select_deep_videos(
        stats, config.model_copy(update={"deep_worst": 0}), {v.id for v in videos}
    )
    assert all(role == "hit" for _, role in no_control)


def test_control_group_on_young_channel() -> None:
    """Каналу два месяца — контроль всё равно должен найтись.

    Абсолютные 90 дней написаны под взрослый канал: на молодом они отсекают
    всю ленту, и анализ видит только победителей. Порог опускается до
    медианного возраста видео — старшая половина ленты."""
    from videoforge.intel.models import AnalysisConfig
    from videoforge.intel.stages.deep import select_deep_videos

    now = datetime.now(UTC)
    videos = [
        VideoMeta(
            id=f"v{i}",
            views=50_000,
            duration_sec=2000,
            published_at=now - timedelta(days=50 - 2 * i),
        )
        for i in range(20)
    ]
    videos[3] = videos[3].model_copy(update={"id": "flop", "views": 500})  # старый провал
    videos[19] = videos[19].model_copy(update={"id": "fresh", "views": 400})  # вчерашний
    videos[10] = videos[10].model_copy(update={"id": "hit", "views": 2_000_000})
    stats = compute_stats(videos, threshold="auto")

    config = AnalysisConfig(id="x", urls=["u"], deep_top=2, deep_worst=1)
    picked = select_deep_videos(stats, config, {v.id for v in videos})
    assert [vid for vid, role in picked if role == "control"] == ["flop"]
    assert "fresh" not in [vid for vid, _ in picked]


def test_shot_stats_from_cuts() -> None:
    """Склейки → длины планов, включая первый и последний."""
    from videoforge.intel.shots import compute_shot_stats

    stats = compute_shot_stats([2.0, 4.0, 6.0, 8.0], duration=10.0)
    assert stats.shot_count == 5  # 4 склейки режут ролик на 5 планов
    assert stats.mean_shot_sec == 2.0
    assert stats.median_shot_sec == 2.0
    assert stats.cuts_per_min == 30.0
    assert stats.duration_sec == 10.0


def test_shot_density_shows_acceleration() -> None:
    """Профиль плотности ловит ускорение монтажа к финалу — ровное среднее его прячет."""
    from videoforge.intel.shots import compute_shot_stats, density_profile

    cuts = [10.0, 20.0, 30.0, 40.0] + [50.0 + i * 1.0 for i in range(10)]
    density = density_profile(cuts, 60.0)
    assert len(density) == 5
    assert density[-1] > density[0]  # в последнем отрезке склеек больше
    assert compute_shot_stats(cuts, 60.0).shot_count > 0


def test_shot_stats_without_cuts() -> None:
    """Ролик одним планом не роняет математику."""
    from videoforge.intel.shots import compute_shot_stats

    stats = compute_shot_stats([], duration=30.0)
    assert stats.shot_count == 1 and stats.mean_shot_sec == 30.0


def test_cut_bursts_are_merged_not_dropped() -> None:
    """Очередь сработок на быстром движении — одна смена картинки.

    Если такие планы выбрасывать, а не склеивать, средняя длина плана
    завышается: выброшенный план из ролика никуда не девается."""
    from videoforge.intel.shots import compute_shot_stats, merge_cuts

    burst = [10.0, 10.1, 10.2, 10.3]
    assert merge_cuts(burst) == [10.0]

    stats = compute_shot_stats([*burst, 20.0], duration=30.0)
    assert stats.shot_count == 3  # 0–10, 10–20, 20–30, а не два длинных плана
    assert stats.mean_shot_sec == 10.0


def test_visual_frames_are_taken_at_deep_beats() -> None:
    """Кадры берутся на битах структуры, а не вслепую, и первый — всегда хук."""
    from videoforge.intel.models import Beat, DeepVideoAnalysis
    from videoforge.intel.stages.visual import MAX_FRAMES, pick_frame_times

    beats = [Beat(t=f"{m:02d}:30", beat=f"бит {m}") for m in range(20)]
    analysis = DeepVideoAnalysis(video_id="x", structure=beats)
    times = pick_frame_times(analysis, duration=1200.0)
    assert times[0] == 2.0  # кадр хука
    assert len(times) == MAX_FRAMES  # 21 бит проредили до лимита
    assert times == sorted(times)

    # биты за пределами ролика и мусорные таймкоды игнорируются
    short = DeepVideoAnalysis(
        video_id="x", structure=[Beat(t="99:99", beat="за концом"), Beat(t="хук", beat="мусор")]
    )
    assert pick_frame_times(short, duration=60.0) == [2.0]


def test_auto_threshold_finds_hits_on_a_steady_channel() -> None:
    """Ровный канал: фиксированные ×3 не находят ничего, авто-порог — находит.

    Так выглядит Kurzgesagt: все ролики похожи, лучший всего вдвое выше соседей."""
    views = [100, 105, 95, 110, 90, 108, 92, 103, 97, 106, 94, 101]
    videos = [_video(i, v) for i, v in enumerate(views)]
    videos.append(_video(len(views), 210))  # лучший ролик канала — ×2 к соседям

    assert compute_stats(videos, threshold=3.0).outliers == []
    auto = compute_stats(videos, threshold="auto")
    assert [o.video_id for o in auto.outliers] == [f"v{len(views):03d}"]
    assert auto.threshold == "auto"
    assert auto.groups["long"].outlier_threshold < 3.0


def test_auto_threshold_ignores_noise_on_a_jumpy_channel() -> None:
    """Канал со скачками: порог поднимается сам, обычная удача в выбросы не идёт."""
    views = [100, 400, 60, 900, 120, 700, 80, 500, 150, 850, 70, 600]
    report = compute_stats([_video(i, v) for i, v in enumerate(views)], threshold="auto")
    assert report.groups["long"].outlier_threshold > 3.0
    assert report.outliers == []


def test_auto_threshold_is_per_group() -> None:
    """У shorts и длинных своя ровность — значит, и порог свой."""
    videos = [_video(i, 100 + (i % 3)) for i in range(12)]  # длинные: почти без разброса
    videos += [
        _video(100 + i, 1000 * (1 + i % 5), duration=45) for i in range(12)
    ]  # shorts: скачут
    report = compute_stats(videos, threshold="auto")
    assert report.groups["long"].outlier_threshold < report.groups["shorts"].outlier_threshold


# ---------------------------------------------------------------- вовлечение


def test_engagement_is_measured_when_the_snapshot_has_it() -> None:
    videos = [_video(i, 100, likes=1000, comments=20) for i in range(8)]
    report = compute_stats(videos, threshold=3.0)
    assert all(r.engagement == pytest.approx(0.02) for r in report.videos)
    assert report.groups["long"].median_engagement == pytest.approx(0.02)


def test_engagement_stays_none_without_likes_or_comments() -> None:
    """Судить не из чего — значит, и цифры нет: нули врали бы."""
    videos = [_video(i, 100) for i in range(8)]
    report = compute_stats(videos, threshold=3.0)
    assert all(r.engagement is None and not r.engagement_outlier for r in report.videos)
    assert report.groups["long"].median_engagement is None
    assert report.groups["long"].engagement_threshold is None


def test_comment_bait_stands_out_against_the_channels_own_norm() -> None:
    """Ролик с призывом «напиши слово в комментарии» ловится сравнением с нормой
    канала, а не абсолютным порогом: у канала-воронки норма другая."""
    videos = [_video(i, 100, likes=1000, comments=20) for i in range(11)]
    videos[5] = _video(5, 100, likes=1000, comments=900)  # «комментируй CLAUDE»
    report = compute_stats(videos, threshold=3.0)
    flagged = [r.video_id for r in report.videos if r.engagement_outlier]
    assert flagged == ["v005"]


def test_engagement_norm_is_relative_not_absolute() -> None:
    """Тот же самый 0.9 комментария на лайк — норма на канале-воронке."""
    videos = [_video(i, 100, likes=1000, comments=900 + i) for i in range(11)]
    report = compute_stats(videos, threshold=3.0)
    assert not any(r.engagement_outlier for r in report.videos)


# ---------------------------------------------------------------- ytdlp (без сети)


def test_ytdlp_channel_base_url() -> None:
    from videoforge.intel.collectors.ytdlp import channel_base_url

    base = "https://www.youtube.com/@demo"
    assert channel_base_url(base) == base
    assert channel_base_url(base + "/videos") == base
    assert channel_base_url(base + "/shorts/") == base
    assert channel_base_url("https://www.youtube.com/channel/UCabc/featured") == (
        "https://www.youtube.com/channel/UCabc"
    )


def test_ytdlp_video_mapping() -> None:
    from videoforge.intel.collectors.ytdlp import to_video_meta

    full = to_video_meta(
        {
            "id": "abc123",
            "webpage_url": "https://youtu.be/abc123",
            "title": "Заголовок",
            "description": "текст",
            "timestamp": 1717200000,
            "upload_date": "20240531",
            "duration": 615,
            "view_count": 12345,
            "like_count": 100,
            "thumbnail": "https://i.ytimg.com/abc123.jpg",
        },
        is_short=False,
    )
    assert full.id == "abc123" and full.views == 12345 and full.is_short is False
    assert full.published_at == datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
    assert full.duration_sec == 615.0

    # без timestamp дата берётся из upload_date; пустые поля не роняют модель
    bare = to_video_meta({"id": "x", "upload_date": "20240102"}, is_short=True)
    assert bare.published_at == datetime(2024, 1, 2, tzinfo=UTC)
    assert bare.views is None and bare.duration_sec is None and bare.is_short is True


def test_ytdlp_base_lang_strips_track_ids_but_keeps_regions() -> None:
    from videoforge.intel.collectors.ytdlp import base_lang

    assert base_lang("de") == "de"
    assert base_lang("de-CcQ45jRV4-E") == "de"  # мультиязычная дорожка YouTube
    assert base_lang("en-orig") == "en"  # авто-оригинал
    assert base_lang("pt-BR") == "pt-BR"  # настоящий регион сохраняем
    assert base_lang("zh-Hans") == "zh-Hans"  # и письменность тоже


def test_ytdlp_picks_subtitles_by_language_not_by_track_id() -> None:
    """Дорожка «de-CcQ45jRV4-E» — это немецкий, и её надо находить по «de»."""
    from videoforge.intel.collectors.ytdlp import pick_subtitle_lang

    tracks = {"de-CcQ45jRV4-E": [{}], "en-CcQ45jRV4-E": [{}]}
    assert pick_subtitle_lang("de", tracks, {}) == "de-CcQ45jRV4-E"
    assert pick_subtitle_lang(None, tracks, {}) == "en-CcQ45jRV4-E"  # en раньше ru
    assert pick_subtitle_lang("de", {}, {"de-orig": [{}]}) == "de-orig"
    assert pick_subtitle_lang("fr", {}, {}) is None


# ---------------------------------------------------------------- отчёт


def test_report_after_run(analysis: Path) -> None:
    from videoforge.intel.report import render_analysis_report

    run_analysis(
        analysis,
        MockCollector(),
        RunOptions(auto_approve=True, raise_errors=True),
        llm=MockIntelLlm(),
    )
    path = render_analysis_report(analysis)
    assert path == analysis / "report.html"
    text = path.read_text(encoding="utf-8")
    assert "<svg" in text  # график динамики построен
    assert "Mock Channel" in text
    assert "long040" in text  # главный выброс есть в таблице
    assert "скользящая медиана" in text  # легенда двух групп
    assert "доход (оценка)" in text  # вилка по RPM из analysis.yaml
    assert "монетизации YouTube не раскрывает" in text  # дисклеймер на месте
    assert "Ниша и форматы" in text and "mock niche" in text  # секция classify
    assert "Формат А" in text  # таблица форматов
    assert "RPM ниши по оценке LLM" in text  # rpm_usd пуст → берётся оценка из classify
    assert "оценка LLM для этой ниши (classify)" in text  # источник в дисклеймере дохода
    assert "Разбор лучших видео" in text  # секция deep
    assert "Mock opening question?" in text  # хук цитатой
    assert "Формула контента" in text and "Mock idea" in text  # секция formula
    assert "blueprint.md" in text  # ссылка на производственный бриф


def test_vtt_to_text_dedups_and_strips_tags(tmp_path: Path) -> None:
    from videoforge.intel.transcript import vtt_to_text

    vtt = tmp_path / "t.vtt"
    vtt.write_text(
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "00:00:01.000 --> 00:00:03.000 align:start position:0%\n"
        "first<00:00:01.599><c> line</c>\n\n"
        "00:00:03.000 --> 00:00:05.000\n"
        "first line\n"  # автосубтитры дублируют предыдущую строку
        "second line\n\n"
        "01:02:03.000 --> 01:02:04.000\n"
        "third line\n",
        encoding="utf-8",
    )
    text = vtt_to_text(vtt)
    assert text.splitlines() == ["00:01 first line", "00:03 second line", "1:02:03 third line"]


def test_strip_fences() -> None:
    from videoforge.providers.claude_cli import strip_fences

    assert strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_fences('{"a": 1}') == '{"a": 1}'
    assert strip_fences("```\ntext\n```") == "text"


def test_report_before_any_stage(analysis: Path) -> None:
    from videoforge.intel.report import render_analysis_report

    path = render_analysis_report(analysis)
    text = path.read_text(encoding="utf-8")
    assert "данных ещё нет" in text  # отчёт не падает на пустом анализе
