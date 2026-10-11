"""Подложка: ровный эмбиенс под всей дорожкой, слой отдельный от музыки.

Трек с развитием начинается тихо, и его вступление — это и есть тишина между
репликами; подложка не зависит от трека, зациклена на всю длину и держит один
уровень. Здесь: модель и линт, ровность и петля синтезированного ресурса,
сборка с подложкой (паузы перестают быть тишиной по детектору, отпечаток
финала без подложки не меняется).
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from videoforge.core.audio_synth import PRESETS, build_preset
from videoforge.core.editplan import BedPlan, EditPlan, MusicPlan, SceneEdit
from videoforge.core.ffmpeg import BedMix, measure_loudness, momentary_loudness
from videoforge.core.manifest import load_manifest
from videoforge.core.project import AudioConfig, ProjectSpec, create_project, load_project
from videoforge.core.report import render_report
from videoforge.core.resources import MusicTrack, load_resource
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import FinalOutput, Scene, SceneAssets, Storyboard, load_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.stages.edit_lint import BED_GAIN_RANGE_DB, lint_plan

OPTS = RunOptions(auto_approve=True, raise_errors=True)


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
        track_exists=lambda rid: rid in ("pad", "tone"),
    )


# ------------------------------------------------------------------ модель и линт


def test_bed_plan_is_off_by_null_track_and_blocks_unknown_track() -> None:
    """`bed` в плане отсутствует — как в проекте; `{track: null}` — выключено;
    выдуманный id — блок, как у музыки и звуков."""
    assert EditPlan().bed is None
    assert BedPlan().track is None and BedPlan().gain_db is None
    off = EditPlan.model_validate({"bed": {"track": None}})
    assert off.bed is not None and off.bed.track is None
    assert not _lint(off).blocking

    plan = EditPlan(bed=BedPlan(track="missing"), music=MusicPlan(track="missing"))
    codes = {f.code for f in _lint(plan).blocking}
    assert codes == {"unknown_bed", "unknown_track"}

    ok = EditPlan(bed=BedPlan(track="pad", gain_db=-5.0), scenes=[SceneEdit(scene_id="s1")])
    result = _lint(ok)
    assert not result.blocking
    assert result.plan.bed is not None and result.plan.bed.gain_db == BED_GAIN_RANGE_DB[1]
    assert any("bed.gain_db" in fix for fix in result.fixes)


def test_project_bed_is_off_by_default_and_the_template_carries_the_block(
    projects_root: Path,
) -> None:
    assert AudioConfig().bed is None and AudioConfig().bed_gain_db == -28.0
    pdir, _ = create_project(ProjectSpec(id="p"))
    text = (pdir / "project.yaml").read_text(encoding="utf-8")
    assert "audio:" in text and "bed_gain_db" in text
    cfg = load_project("p")
    assert cfg.audio.bed is None and cfg.audio.bed_gain_db == -28.0
    (pdir / "project.yaml").write_text(text.replace("  bed: null", "  bed: pad"), encoding="utf-8")
    assert load_project("p").audio.bed == "pad"


def test_music_resource_bed_flag(tmp_path: Path) -> None:
    mdir = tmp_path / "music"
    mdir.mkdir()
    (mdir / "a.yaml").write_text("id: a\nfile: a.wav\n", encoding="utf-8")
    (mdir / "b.yaml").write_text("id: b\nfile: b.wav\nbed: true\n", encoding="utf-8")
    assert load_resource("music", "a", MusicTrack, [tmp_path]).bed is False
    assert load_resource("music", "b", MusicTrack, [tmp_path]).bed is True


def test_final_output_checks_the_bed_only_when_it_is_there() -> None:
    labels = [c.label for c in FinalOutput().checks(captions_expected=False, music_expected=False)]
    assert "подложка под голосом" not in labels
    final = FinalOutput(bed_track="pad", voice_lufs=-20.0, bed_lufs=-48.0)
    row = next(
        c
        for c in final.checks(captions_expected=False, music_expected=False)
        if c.label == "подложка под голосом"
    )
    assert row.ok and "подложка -48.0" in row.value


# ------------------------------------------------------------------ синтез


def _samples(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as w:
        n, ch = w.getnframes(), w.getnchannels()
        return list(struct.unpack(f"<{n * ch}h", w.readframes(n))[::ch])


def test_flat_pad_is_flat_and_loops_without_a_seam(tmp_path: Path) -> None:
    """Ровность — разброс МОМЕНТАЛЬНОЙ громкости (окно 400 мс): интеграл у пада с
    провалами и у ровного один и тот же. Замер 14.09.2026: flat_pad 0.4 дБ,
    soft_pad из resources/music — 20.7 дБ (−38.1…−17.4 LUFS).

    Петля — по построению: частоты кратны 1/16 Гц, каждый синус и LFO тембра
    укладываются в 16 с целым числом периодов, поэтому сэмпл после последнего
    равен первому. Проверяется шаг на стыке против шага между соседями внутри."""
    rid, note = build_preset(tmp_path, "flat_pad", "pad")
    assert rid == "pad" and note.endswith(" с")
    wav, yaml_path = tmp_path / "music" / "pad.wav", tmp_path / "music" / "pad.yaml"
    assert wav.exists() and yaml_path.exists()
    track = load_resource("music", "pad", MusicTrack, [tmp_path])
    assert track.bed and track.loop_ok and track.license and track.source

    points = momentary_loudness(wav)
    assert len(points) > 100  # 16 с по 100 мс
    levels = [m for _, m in points]
    assert max(levels) - min(levels) <= 1.0, (min(levels), max(levels))

    samples = _samples(wav)
    assert len(samples) == int(PRESETS["flat_pad"].duration * 44100)
    step = max(abs(b - a) for a, b in zip(samples[:-1], samples[1:], strict=True))
    seam = abs(samples[0] - samples[-1])
    assert seam <= 1.5 * step, (seam, step)
    assert build_preset(tmp_path, "flat_pad", "pad") == ("pad", "уже есть")
    with pytest.raises(KeyError):
        build_preset(tmp_path, "nope")


# ------------------------------------------------------------------ сборка


def _set_bed(projects_root: Path, value: str | None) -> None:
    cfg = projects_root / "t" / "project.yaml"
    text = cfg.read_text(encoding="utf-8")
    if value is None:
        text = text.replace("  bed: pad", "  bed: null")
    else:
        text = text.replace("  bed: null", f"  bed: {value}")
    cfg.write_text(text, encoding="utf-8")


@pytest.mark.render
def test_bed_fills_the_pauses_and_leaves_old_finals_alone(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    """Озвучка заглушки — цифровая тишина, поэтому без подложки весь ролик
    тишина по детектору; с подложкой доля тишины падает до нуля, слой измерен
    отдельно (`bed_lufs`) и назван в отчёте. Финал, собранный без подложки,
    после её удаления снова «уже собран»: слой входит в отпечаток только когда
    он есть, иначе все готовые ролики разом устарели бы."""
    run_episode(episode, mock_providers, OPTS)
    before = load_storyboard(episode).final
    assert before.bed_track is None and before.bed_lufs is None
    assert before.silence_share > 0.5  # заглушка молчит
    fingerprint = before.inputs_fingerprint

    build_preset(projects_root / "t" / "resources", "flat_pad", "pad")
    _set_bed(projects_root, "pad")
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    sb = load_storyboard(episode)
    final = sb.final
    assert final.bed_track == "pad"
    assert final.bed_lufs is not None
    assert final.silence_sec == 0 and final.silence_share == 0
    assert final.music_track is None  # подложка — не музыка
    assert final.inputs_fingerprint != fingerprint
    # слышна на всём протяжении, а не только в измерителе: середина ролика не тишина
    assert final.video and final.duration_sec
    middle = measure_loudness(episode / final.video)
    assert middle[0] is not None
    rows = {c.label: c for c in final.checks(captions_expected=True, music_expected=False)}
    assert rows["подложка под голосом"].ok
    html = render_report(episode, sb, load_manifest(episode)).read_text(encoding="utf-8")
    assert "подложка: pad" in html

    _set_bed(projects_root, None)
    run_episode(episode, mock_providers, OPTS)
    after = load_storyboard(episode).final
    assert after.bed_track is None and after.inputs_fingerprint == fingerprint


@pytest.mark.render
def test_plan_turns_the_project_bed_off_and_refuses_a_track_without_the_flag(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    from conftest import add_music

    from videoforge.core.editplan import default_plan, save_plan

    build_preset(projects_root / "t" / "resources", "flat_pad", "pad")
    _set_bed(projects_root, "pad")
    plan = default_plan(load_storyboard(episode))
    plan.author = "manual"
    plan.bed = BedPlan(track=None)
    save_plan(episode, plan)
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    assert load_storyboard(episode).final.bed_track is None

    # трек без bed: true подложкой не берётся — его тихое вступление и есть та
    # тишина, которую слой должен закрывать
    add_music(episode, projects_root)  # tone.yaml без bed
    plan.bed = BedPlan(track="tone")
    save_plan(episode, plan)
    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True))
    assert not report.ok and report.stopped_at == "assemble"
    assert report.reason and "bed: true" in report.reason


def test_bed_mix_defaults_match_the_project() -> None:
    assert BedMix(path=Path("x")).gain_db == AudioConfig().bed_gain_db
