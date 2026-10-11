"""План монтажа: заготовка стадии edit, линт с автопочинкой, исполнение в сборке."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoforge.core.editplan import (
    EditPlan,
    MusicPlan,
    SceneEdit,
    Segment,
    Transition,
    default_plan,
    load_plan,
    plan_path,
    save_plan,
    sources_path,
)
from videoforge.core.ffmpeg import probe_duration
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, SceneAssets, Storyboard, load_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.stages.edit_lint import lint_plan

OPTS = RunOptions(auto_approve=True, raise_errors=True)


# ------------------------------------------------------------------ линт, без ffmpeg


def _storyboard() -> Storyboard:
    scenes = [
        Scene(
            id=f"s{i}",
            order=i,
            duration_sec=5.0,
            narration=f"слово{i} ещё немного текста.",
            assets=SceneAssets(video=f"03_video/s{i}.mp4"),
            measured_sec={"audio": 4.0},
        )
        for i in range(1, 5)
    ]
    return Storyboard(project_id="t", scenes=scenes)


def _lint(plan: EditPlan):  # noqa: ANN202 — тип из модуля линта
    return lint_plan(
        plan,
        _storyboard(),
        duration_of=lambda rel: 6.0 if rel.startswith("03_video/") else None,
        style_exists=lambda rid: rid == "default",
        track_exists=lambda rid: rid == "pad",
    )


def test_default_plan_lists_every_scene_with_its_clip() -> None:
    plan = default_plan(_storyboard())
    assert [e.scene_id for e in plan.scenes] == ["s1", "s2", "s3", "s4"]
    assert plan.scenes[0].segments[0].source == "03_video/s1.mp4"
    assert all(e.transition_in.type == "cut" for e in plan.scenes)
    assert plan.author == "default"


def test_lint_fixes_the_obvious_and_expands_defaults() -> None:
    plan = EditPlan(
        fade_in_sec=10.0,
        scenes=[
            SceneEdit(
                scene_id="s2",
                segments=[Segment(in_sec=-1.0, out_sec=9.0)],
                transition_in=Transition(type="crossfade", duration_sec=2.0),
                emphasis=["слово2", "чужое", "ещё", "немного"],
            ),
            SceneEdit(scene_id="s1", transition_in=Transition(type="dip_black", duration_sec=0.4)),
        ],
    )
    result = _lint(plan)
    assert not result.blocking
    s2 = result.plan.scene("s2")
    assert s2 is not None
    seg = s2.segments[0]
    assert seg.source == "03_video/s2.mp4"  # источник по умолчанию раскрыт
    assert seg.in_sec == 0.0  # отрицательный in
    assert seg.out_sec == 5.0  # out за концом клипа и длиннее коробки → коробка
    # crossfade не длиннее хвоста тишины предыдущей сцены (5 − 4 = 1) и потолка 0.5
    assert s2.transition_in.duration_sec == 0.5
    assert s2.emphasis == ["слово2", "ещё"]  # чужое слово убрано, лишние обрезаны до двух
    s1 = result.plan.scene("s1")
    assert s1 is not None and s1.transition_in.type == "cut"  # на первой сцене переходов нет
    assert result.plan.fade_in_sec == 3.0
    assert len(result.fixes) >= 6


def test_lint_blocks_unknown_scene_source_style_and_track() -> None:
    plan = EditPlan(
        caption_style="nope",
        music=MusicPlan(track="missing"),
        scenes=[
            SceneEdit(scene_id="s9"),
            SceneEdit(scene_id="s1", segments=[Segment(source="elsewhere/x.mp4")]),
            SceneEdit(scene_id="s2", segments=[Segment(in_sec=3.0, out_sec=3.0)]),
        ],
    )
    result = _lint(plan)
    codes = {f.code for f in result.blocking}
    assert codes == {"unknown_scene", "bad_segment", "unknown_style", "unknown_track"}


def test_lint_quotas_keep_hard_cut_the_default() -> None:
    plan = EditPlan(
        scenes=[
            SceneEdit(
                scene_id=f"s{i}", transition_in=Transition(type="dip_black", duration_sec=0.4)
            )
            for i in range(2, 5)
        ]
    )
    result = _lint(plan)
    assert any(f.code == "too_many_transitions" for f in result.blocking)  # 3 из 4
    assert any(f.code == "same_transition_run" for f in result.warnings)

    modest = EditPlan(
        scenes=[
            SceneEdit(scene_id="s3", transition_in=Transition(type="dip_black", duration_sec=0.4))
        ]
    )
    assert not _lint(modest).blocking  # 1 из 4 — норма


def test_lint_warns_when_segments_do_not_fill_the_box() -> None:
    plan = EditPlan(scenes=[SceneEdit(scene_id="s1", segments=[Segment(in_sec=0.0, out_sec=2.0)])])
    result = _lint(plan)
    assert any(f.code == "underfilled" for f in result.warnings)


# ------------------------------------------------------------------ конвейер на заглушках


@pytest.mark.render
def test_edit_stage_writes_sources_and_a_plan_from_the_model(
    episode: Path, mock_providers: ProviderSet
) -> None:
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    plan = load_plan(episode)
    assert plan is not None and plan.author == "llm"
    assert [e.scene_id for e in plan.scenes] == ["s1", "s2"]  # полный план, не только отклонения
    sources = json.loads(sources_path(episode).read_text(encoding="utf-8"))
    by_id = {c["scene_id"]: c for c in sources["clips"]}
    assert by_id["s1"]["duration_sec"] >= by_id["s1"]["box_sec"]
    assert by_id["s1"]["silent_tail_sec"] == pytest.approx(0.5, abs=0.06)
    final = load_storyboard(episode).final
    assert final.edit_author == "llm" and final.transitions == 1  # заглушка ставит один dip_black
    assert (episode / "05_assemble" / "timeline.json").exists()


@pytest.mark.render
def test_manual_plan_is_executed(episode: Path, mock_providers: ProviderSet) -> None:
    """Два отрезка в сцене, dip-to-black на входе, акцент в субтитрах — всё
    исполняется, а длина эпизода остаётся суммой коробок."""
    run_episode(
        episode, mock_providers, RunOptions(stop="video", auto_approve=True, raise_errors=True)
    )
    plan = default_plan(load_storyboard(episode))
    plan.author = "manual"
    s1 = plan.scene("s1")
    s2 = plan.scene("s2")
    assert s1 and s2
    s1.emphasis = ["короткая"]
    s2.segments = [Segment(in_sec=0.0, out_sec=3.0), Segment(in_sec=2.0, out_sec=5.0)]
    s2.transition_in = Transition(type="dip_black", duration_sec=0.4)
    save_plan(episode, plan)

    report = run_episode(
        episode, mock_providers, RunOptions(start="edit", auto_approve=True, raise_errors=True)
    )
    assert report.ok, report.reason
    sb = load_storyboard(episode)
    a, b = sb.ordered_scenes()
    assert b.montage.shots == 2 and b.montage.transition_in == "dip_black"
    assert sb.final.transitions == 1 and sb.final.edit_author == "manual"
    assert b.measured_sec["clip"] == pytest.approx(b.duration_sec, abs=0.05)  # dip длину не меняет
    assert a.measured_sec["clip"] == pytest.approx(a.duration_sec, abs=0.05)
    ass = (episode / sb.final.captions).read_text(encoding="utf-8")
    assert "\\fscx112" in ass  # акцент дошёл до ASS
    assert sb.final.av_drift_sec is not None and abs(sb.final.av_drift_sec) <= 0.1
    assert load_plan(episode).author == "manual"  # стадия edit ручной план не переписала


@pytest.mark.render
def test_crossfade_eats_only_the_silent_tail(episode: Path, mock_providers: ProviderSet) -> None:
    run_episode(
        episode, mock_providers, RunOptions(stop="video", auto_approve=True, raise_errors=True)
    )
    plan = default_plan(load_storyboard(episode))
    plan.author = "manual"
    s2 = plan.scene("s2")
    assert s2
    s2.transition_in = Transition(type="crossfade", duration_sec=0.3)
    save_plan(episode, plan)

    report = run_episode(
        episode, mock_providers, RunOptions(start="edit", auto_approve=True, raise_errors=True)
    )
    assert report.ok, report.reason
    sb = load_storyboard(episode)
    a, b = sb.ordered_scenes()
    boxes = a.duration_sec + b.duration_sec
    total = probe_duration(episode / sb.final.video)
    assert total == pytest.approx(boxes - 0.3, abs=0.12)  # ровно на crossfade короче
    assert b.montage.lead_sec > 0
    assert sb.final.av_drift_sec is not None and abs(sb.final.av_drift_sec) <= 0.1
    assert sb.final.caption_coverage is not None and sb.final.caption_coverage >= 0.95


@pytest.mark.render
def test_invalid_plan_falls_back_to_default_render(
    episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(
        episode, mock_providers, RunOptions(stop="video", auto_approve=True, raise_errors=True)
    )
    plan = default_plan(load_storyboard(episode))
    plan.author = "manual"
    plan.scenes.append(SceneEdit(scene_id="ghost"))
    save_plan(episode, plan)

    edit = run_episode(episode, mock_providers, RunOptions(only="edit", auto_approve=True))
    assert not edit.ok and "ghost" in (edit.reason or "")  # стадия edit честно падает

    report = run_episode(
        episode, mock_providers, RunOptions(only="assemble", auto_approve=True, raise_errors=True)
    )
    assert report.ok, report.reason
    _, result = report.executed[0]
    assert "ПЛАН" in result.message and "ghost" in result.message  # собрано, но громко
    assert load_storyboard(episode).final.edit_author == "default"
    assert plan_path(episode).exists()
