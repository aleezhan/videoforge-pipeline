"""Бюджет тишины в готовом ролике (features/silence_budget).

Числа — из замера measure_final по doc_channel/ep001: 267.5 с тишины
из 506.6 (53%), 92 паузы, самая длинная 17.3 с, последние 15.6 с ролик молчит.
Ворота его пропустили: прежняя строка «тишина дольше секунды ≤ 1.5 с» горела
на каждом длинном ролике и сигналом быть перестала. Здесь — доля, самая длинная
пауза и хвост против порогов, и имена сцен, где дыры.
"""

from __future__ import annotations

from videoforge.core.storyboard import (
    SILENCE_GAPS_KEPT,
    SILENCE_LONGEST_LIMIT_SEC,
    SILENCE_SHARE_LIMIT,
    SILENCE_TAIL_LIMIT_SEC,
    FinalCheck,
    FinalOutput,
    Scene,
    SilenceGap,
)
from videoforge.stages.assemble import (
    TimelineEntry,
    check_places,
    format_final,
    silence_gaps,
    silence_tail,
)

EP001 = FinalOutput(
    video="05_assemble/final.mp4",
    duration_sec=506.6,
    silence_sec=267.5,
    silence_tail_sec=15.6,
    silence_gaps=[
        SilenceGap(scene_id="s07", at_sec=31.0, sec=4.1),
        SilenceGap(scene_id="s12", at_sec=60.2, sec=17.3),
        SilenceGap(scene_id="s105", at_sec=491.0, sec=15.6),
    ],
)


def _rows(final: FinalOutput, *, filmed: bool = False) -> dict[str, FinalCheck]:
    return {
        c.label: c
        for c in final.checks(captions_expected=False, music_expected=False, filmed=filmed)
    }


def test_silence_is_judged_by_share_and_holes_not_by_the_sum() -> None:
    rows = _rows(EP001)
    share = rows["доля тишины"]
    assert not share.ok and share.value == "53% (267.5s)"
    assert f"{SILENCE_SHARE_LIMIT:.0%}" in share.target
    assert not rows["самая длинная пауза"].ok and rows["самая длинная пауза"].value == "17.3s"
    assert not rows["тишина в конце"].ok and rows["тишина в конце"].value == "15.6s"
    # старой строки нет: сумма против 1.5 с горела на каждом ролике длиннее минуты
    assert not any(label.startswith("тишина дольше") for label in rows)

    # десять минут с редкими паузами: сумма за 1.5 с давно ушла, а ролик в норме
    calm = FinalOutput(
        video="f.mp4",
        duration_sec=600.0,
        silence_sec=30.0,
        silence_tail_sec=SILENCE_TAIL_LIMIT_SEC,
        silence_gaps=[SilenceGap(scene_id="s03", at_sec=10.0, sec=SILENCE_LONGEST_LIMIT_SEC)],
    )
    rows = _rows(calm)
    assert rows["доля тишины"].ok and rows["доля тишины"].value == "5% (30.0s)"
    assert rows["самая длинная пауза"].ok and rows["тишина в конце"].ok


def test_filmed_cut_keeps_the_silence_thresholds() -> None:
    """У нарезки неподвижность в кадре — не дефект, а тишина — дефект: звук
    приходит из исходника, и молчащий отрезок значит, что момент вырезан в
    паузу или дорожка не дошла до финала."""
    rows = _rows(EP001, filmed=True)
    assert not rows["доля тишины"].ok
    assert not rows["самая длинная пауза"].ok
    assert not rows["тишина в конце"].ok
    assert rows["застывший кадр (детектор)"].ok  # а это для снятого материала прощается


def test_unmeasured_silence_is_named_not_faked() -> None:
    """Финал, собранный до этой проверки: сумма в файле есть, дыр и хвоста нет.
    Доля выводится из суммы и краснеет сразу — без пересборки; «самая длинная»
    и «хвост» честно говорят «не измерено», а не «0.0s»."""
    old = FinalOutput(video="f.mp4", duration_sec=506.6, silence_sec=267.5)
    assert old.silence_longest_sec is None and old.silence_tail_sec is None
    rows = _rows(old)
    assert not rows["доля тишины"].ok
    assert rows["самая длинная пауза"].ok and rows["самая длинная пауза"].value == "не измерено"
    assert rows["тишина в конце"].ok and rows["тишина в конце"].value == "не измерено"


def test_gaps_are_named_by_the_scene_that_holds_most_of_the_pause() -> None:
    timeline = [
        TimelineEntry("s1", 0.0, 10.0),
        TimelineEntry("s2", 10.0, 20.0),
        TimelineEntry("s3", 20.0, 30.0),
    ]
    intervals = [
        (8.0, 12.5),  # на стыке: 2.0 с в s1, 2.5 с в s2 → s2
        (21.0, 22.2),
        (25.0, 27.0),
        (1.0, 2.1),
    ]
    gaps = silence_gaps(intervals, timeline)
    assert [g.scene_id for g in gaps] == ["s1", "s2", "s3", "s3"]  # по времени, не по длине
    assert [g.sec for g in gaps] == [1.1, 4.5, 1.2, 2.0]
    assert gaps[1].at_sec == 8.0

    # хранятся только самые длинные — у ep001 пауз 92, и список из 92 не читают
    top = silence_gaps(intervals, timeline, keep=2)
    assert [(g.scene_id, g.sec) for g in top] == [("s2", 4.5), ("s3", 2.0)]
    assert SILENCE_GAPS_KEPT == 5
    nameless = silence_gaps(intervals, [])  # без таймлайна дыры есть, сцен у них нет
    assert len(nameless) == 4 and all(g.scene_id == "" for g in nameless)


def test_tail_is_the_pause_that_reaches_the_end() -> None:
    intervals = [(3.0, 5.0), (24.4, 40.0)]
    assert silence_tail(intervals, end=40.0) == 15.6
    assert silence_tail(intervals, end=40.04) == 15.6  # зазор округления окна — ещё хвост
    assert silence_tail(intervals, end=41.0) == 0.0  # последняя пауза кончилась раньше конца
    assert silence_tail([], end=40.0) == 0.0


def test_final_message_names_the_holes() -> None:
    """«ПРОВЕРЬ» на воротах говорит, куда смотреть: дыры с длиной каждой, сцена
    самой длинной, последняя сцена у хвоста."""
    scenes = [Scene(id=f"s{n:02d}", order=n) for n in range(1, 106)]
    message = format_final(EP001, scenes, captions=False, music=False)
    assert "доля тишины 53% (267.5s) (s07 4.1s, s12 17.3s, s105 15.6s)" in message
    assert "самая длинная пауза 17.3s (s12)" in message
    assert "тишина в конце 15.6s (s105)" in message

    where = check_places(EP001, scenes)
    assert [p.scene_id for p in where["доля тишины"]] == ["s07", "s12", "s105"]
    assert where["самая длинная"][0].scene_id == "s12"
    assert where["тишина в конце"][0].scene_id == "s105"
    # хвоста нет — и сцены для него нет
    quiet = EP001.model_copy(update={"silence_tail_sec": 0.0})
    assert check_places(quiet, scenes)["тишина в конце"] == []
