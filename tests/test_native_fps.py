"""Частота кадров сборки — из исходника, а не константа
(`features/assemble_native_fps`).

До 14.09.2026 всё шло в 24: у 25-кадрового подкаста (podcast_clips/h13)
отрезок терял кадр в секунду ещё на нарезке — 1055 кадров вместо 1099 на
43.96 с, и движение рук и губ спотыкалось раз в секунду. Теперь `cut` пишет
`Storyboard.fps` по исходнику, кадрирование и сборка рендерят в неё, а
готовый файл меряется: строка «частота кадров» в замерах сравнивает его с
частотой эпизода. У ai_video поле пусто — его клипы идут в 24, как раньше.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import count_frames, make_test_video

from videoforge.clips.highlights import Candidate, episode_from_candidate
from videoforge.clips.source import WORDS_FILE, SourceInfo, save_source
from videoforge.core.captions import Word, words_to_srt
from videoforge.core.editplan import default_plan
from videoforge.core.ffmpeg import FPS, fps_expr, probe_duration, probe_fps, probe_stream_duration
from videoforge.core.manifest import load_manifest
from videoforge.core.project import ProjectSpec, create_project
from videoforge.core.project import load_project as load_project_cfg
from videoforge.core.resources import load_resources, search_dirs
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions, StageContext
from videoforge.core.storyboard import (
    FinalOutput,
    Scene,
    SourceSegment,
    load_storyboard,
    save_storyboard,
)
from videoforge.paths import SOURCES_DIR, source_dir
from videoforge.providers.base import ProviderSet
from videoforge.stages.assemble import inputs_fingerprint
from videoforge.stages.cut import episode_fps

MOMENT = (5.0, 15.0)  # секунды «подкаста», из которых режется шортс


def test_fps_expr_keeps_integers_and_names_ntsc_fractions() -> None:
    assert fps_expr(24) == "24" and fps_expr(30.0) == "30" and fps_expr(60) == "60"
    assert fps_expr(25.0) == "25"
    # дробные NTSC — точной дробью: по округлённой сетка кадров расходится
    assert fps_expr(29.97) == "30000/1001"
    assert fps_expr(23.976) == "24000/1001"
    assert fps_expr(23.98) == "24000/1001"  # так их печатает `ffmpeg -i`
    assert fps_expr(59.94) == "60000/1001"
    assert fps_expr(12.5) == "12.5"  # прочая дробь — как есть, без хвоста нулей


def test_final_check_compares_the_measured_rate_with_the_episode() -> None:
    """Строка «частота кадров» есть только у финала, у которого частота
    измерена; сравнивается с частотой эпизода, пусто — 24."""
    kw = {"captions_expected": False, "music_expected": False}
    assert "частота кадров" not in {c.label for c in FinalOutput().checks(**kw)}

    rows = {c.label: c for c in FinalOutput(fps=24.0).checks(**kw)}
    assert rows["частота кадров"].ok and rows["частота кадров"].value == "24 (эпизод 24)"

    rows = {c.label: c for c in FinalOutput(fps=24.0).checks(fps_expected=25.0, **kw)}
    assert not rows["частота кадров"].ok  # по дороге кадры пересчитали
    assert rows["частота кадров"].value == "24 (эпизод 25)"

    # `ffmpeg -i` печатает 24000/1001 как «23.98» — это та же частота
    rows = {c.label: c for c in FinalOutput(fps=23.98).checks(fps_expected=23.976, **kw)}
    assert rows["частота кадров"].ok


def test_episode_fps_takes_the_longest_material_and_names_the_rest() -> None:
    assert episode_fps([]) == (float(FPS), None)
    assert episode_fps([("s01", 25.0, 44.0)]) == (25.0, None)
    # по секундам, не по числу сцен: две короткие в 30 против одной длинной в 24
    rate, warning = episode_fps([("s01", 30.0, 4.0), ("s02", 24.0, 12.0), ("s03", 30.0, 5.0)])
    assert rate == 24.0
    assert warning is not None and "s01 (30)" in warning and "s03 (30)" in warning
    assert "s02" not in warning


def _context(episode: Path, providers: ProviderSet, projects_root: Path) -> StageContext:
    sb = load_storyboard(episode)
    pdir = projects_root / "t"
    return StageContext(
        stage_name="assemble",
        project_dir=episode,
        storyboard=sb,
        manifest=load_manifest(episode),
        providers=providers,
        resources=load_resources(sb.resources, search_dirs(pdir)),
        options=RunOptions(),
        project=load_project_cfg("t"),
        project_root=pdir,
        resource_dirs=search_dirs(pdir),
    )


def test_ai_video_has_no_fps_and_the_fingerprint_does_not_change(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    """Пустое поле значит 24, и в отпечаток сборки оно не идёт: иначе все
    собранные до него финалы разом стали бы «устаревшими»."""
    ctx = _context(episode, mock_providers, projects_root)
    assert ctx.storyboard.fps is None and (ctx.storyboard.fps or FPS) == 24
    scenes = ctx.storyboard.ordered_scenes()
    plan = default_plan(ctx.storyboard)

    def fp() -> str:
        assert ctx.project is not None
        return inputs_fingerprint(
            ctx, scenes, plan=plan, style=None, track=None, cfg=ctx.project.video
        )

    plain = fp()
    ctx.storyboard.fps = 25.0
    assert fp() != plain  # заданная частота — вход сборки
    ctx.storyboard.fps = None
    assert fp() == plain


def _clips_project(projects_root: Path, tmp_path: Path, *, rates: dict[str, int]) -> None:
    """Проект нарезок с локальными «подкастами» разной частоты кадров."""
    create_project(ProjectSpec(id="c", name="Нарезки", workflow="clips_from_long"))
    seconds = 30.0
    for sid, rate in rates.items():
        video = make_test_video(tmp_path / f"{sid}.mp4", seconds=seconds, rate=rate)
        sdir = source_dir("c", sid)
        save_source(sdir, SourceInfo(id=sid, file=str(video), title=sid, duration_sec=seconds))
        words = [Word(f"слово{i}", float(i), float(i) + 0.6) for i in range(int(seconds))]
        (sdir / WORDS_FILE).write_text(words_to_srt(words), encoding="utf-8")


def _episode(source_id: str) -> Path:
    start, end = MOMENT
    return episode_from_candidate(
        "c", source_id, Candidate(id="h01", start_sec=start, end_sec=end, title="Момент", hook="х")
    )


def test_cut_keeps_the_source_frame_rate(
    projects_root: Path, tmp_path: Path, mock_providers: ProviderSet
) -> None:
    _clips_project(projects_root, tmp_path, rates={"vid30": 30})
    edir = _episode("vid30")
    assert load_storyboard(edir).fps is None  # до нарезки частоты нет

    report = run_episode(edir, mock_providers, RunOptions(only="cut"))
    assert report.ok, report.reason
    assert "30 к/с" in report.executed[0][1].message
    sb = load_storyboard(edir)
    assert sb.fps == 30.0
    (scene,) = sb.scenes
    assert scene.assets.video
    clip = edir / scene.assets.video
    assert probe_fps(clip) == 30.0
    # кадров ровно длительность × 30 — ни один не выброшен (при 24 их было бы 240)
    assert abs(count_frames(clip) - probe_duration(clip) * 30) <= 1


def test_mixed_sources_take_the_longest_and_the_stage_warns(
    projects_root: Path, tmp_path: Path, mock_providers: ProviderSet
) -> None:
    _clips_project(projects_root, tmp_path, rates={"vid30": 30, "vid24": 24})
    edir = _episode("vid30")  # 10 с в 30 к/с
    sb = load_storyboard(edir)
    sb.scenes.append(
        Scene(
            id="s02",
            order=2,
            duration_sec=12.0,
            planned_sec=12.0,
            source=SourceSegment(path=f"{SOURCES_DIR}/vid24", start_sec=10.0, end_sec=22.0),
        )
    )
    save_storyboard(edir, sb)

    report = run_episode(edir, mock_providers, RunOptions(only="cut"))
    assert report.ok, report.reason
    message = report.executed[0][1].message
    assert "24 к/с" in message and "ПРОВЕРЬ" in message and "s01 (30)" in message
    sb = load_storyboard(edir)
    assert sb.fps == 24.0
    # каждый отрезок остался в своей частоте: пересчёт — забота сборки, один раз
    rates = {s.id: probe_fps(edir / (s.assets.video or "")) for s in sb.scenes}
    assert rates == {"s01": 30.0, "s02": 24.0}


@pytest.mark.render
def test_shorts_is_rendered_in_the_source_frame_rate(
    projects_root: Path, tmp_path: Path, mock_providers: ProviderSet
) -> None:
    """Исходник 30 → кадрирование 30 → финал 30, и замер это подтверждает."""
    _clips_project(projects_root, tmp_path, rates={"vid30": 30})
    edir = _episode("vid30")
    assert run_episode(edir, mock_providers, RunOptions(only="cut")).ok
    report = run_episode(
        edir, mock_providers, RunOptions(start="reframe", stop="assemble", auto_approve=True)
    )
    assert report.ok, report.reason

    sb = load_storyboard(edir)
    (scene,) = sb.scenes
    assert scene.assets.video and scene.assets.video.endswith("_reframed.mp4")
    assert probe_fps(edir / scene.assets.video) == 30.0
    assert sb.final.video and sb.final.fps == 30.0
    final = edir / sb.final.video
    assert probe_fps(final) == 30.0
    # кадров — длина ДОРОЖКИ картинки × 30: контейнер длиннее на хвост звука
    # (318 кадров = 10.60 с при контейнере 10.66), и по нему счёт не сходится
    video_sec = probe_stream_duration(final, "0:v:0") or 0.0
    assert abs(count_frames(final) - video_sec * 30) <= 1
    rows = {
        c.label: c
        for c in sb.final.checks(
            captions_expected=True, music_expected=False, filmed=True, fps_expected=sb.fps
        )
    }
    assert rows["частота кадров"].ok and rows["частота кадров"].value == "30 (эпизод 30)"
    assert rows["дорожки (звук − картинка)"].ok

    # человек сменил частоту эпизода — кадрирование устарело и перерисовалось
    # в новую без --force, чтобы сборка не пересчитывала кадры дважды
    sb.fps = 24.0
    save_storyboard(edir, sb)
    assert run_episode(edir, mock_providers, RunOptions(only="reframe")).ok
    assert probe_fps(edir / scene.assets.video) == 24.0
