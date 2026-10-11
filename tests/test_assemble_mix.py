"""Финальный проход сборки: субтитры, музыка под голосом, нормализация, замеры."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import add_music

from videoforge.core.ffmpeg import probe_duration
from videoforge.core.manifest import load_manifest
from videoforge.core.report import render_report
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import TRUE_PEAK_LIMIT_DBTP, load_storyboard
from videoforge.providers.base import ProviderSet

OPTS = RunOptions(auto_approve=True, raise_errors=True)


@pytest.mark.render
def test_captions_are_burned_and_measured(episode: Path, mock_providers: ProviderSet) -> None:
    run_episode(episode, mock_providers, OPTS)
    sb = load_storyboard(episode)
    final = sb.final
    assert final.captions and (episode / final.captions).exists()
    ass = (episode / final.captions).read_text(encoding="utf-8")
    assert "Dialogue:" in ass and "\\k" in ass  # реплики с караоке-подсветкой
    assert final.caption_events > 0
    assert final.caption_coverage is not None and final.caption_coverage >= 0.95

    s1, s2 = sb.ordered_scenes()
    assert s1.montage.caption_events > 0 and s2.montage.caption_events == 0  # s2 без речи
    # субтитры, fade и финальный проход не меняют длину сцены и не двигают дорожки
    assert s1.measured_sec["clip"] == pytest.approx(s1.duration_sec, abs=0.01)
    assert final.av_drift_sec is not None and abs(final.av_drift_sec) <= 0.1
    assert final.true_peak_dbtp is None or final.true_peak_dbtp <= -1.0
    assert final.cuts == 1 and final.transitions == 1  # план заглушки: один dip_black
    assert len(s1.assets.thumbs) == 3 and all((episode / t).exists() for t in s1.assets.thumbs)
    assert (episode / "05_assemble" / "montage.json").exists()


@pytest.mark.render
def test_ass_is_not_named_like_the_video(episode: Path, mock_providers: ProviderSet) -> None:
    """Плееры сами подхватывают файл субтитров с именем открытого видео и рисуют
    его поверх уже вшитых — человек видит два текста один за другим, а в кадрах,
    извлечённых ffmpeg, всё чисто (внешние субтитры он не подключает). Найдено
    пользователем на готовом шортсе; лечится тем, что имена не совпадают."""
    run_episode(episode, mock_providers, OPTS)
    final = load_storyboard(episode).final
    assert final.video and final.captions
    assert Path(final.captions).stem != Path(final.video).stem
    # и вообще рядом с видео не должно оказаться субтитров с его именем
    video = episode / final.video
    subs = (".ass", ".srt", ".vtt")
    twins = [p for p in video.parent.glob(f"{video.stem}.*") if p.suffix in subs]
    assert not twins, twins


@pytest.mark.render
def test_music_is_mixed_under_the_voice_and_normalized(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    add_music(episode, projects_root)
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    sb = load_storyboard(episode)
    final = sb.final
    assert final.music_track == "tone"
    assert final.music_lufs is not None  # музыка измерена отдельно от микса
    assert final.loudness_lufs is not None
    assert abs(final.loudness_lufs - (-14.0)) <= 1.5  # loudnorm привёл микс к цели площадок
    # Пик здесь — синтетика: голос заглушки молчит, loudnorm поднимает синус с
    # −18 дБ почти на 20 дБ, а удар заглушки монтажа стоит ровно на склейке, и
    # пик зависит от фазы синуса в эту миллисекунду. Замер 16.09.2026: сдвиг
    # склейки на 40–90 мс даёт −1.0 / +0.2 / −0.2 / −0.7 dBTP. Порог площадки
    # (−1 dBTP) держит строка замеров финала на живом голосе; тест держит, что
    # пик измерен и не ушёл дальше перелёта лимитера и AAC
    assert final.true_peak_dbtp is not None
    assert final.true_peak_dbtp <= TRUE_PEAK_LIMIT_DBTP + 1.5
    assert final.av_drift_sec is not None and abs(final.av_drift_sec) <= 0.1
    # длина эпизода — сумма сцен, музыка её не растягивает (4-секундный трек зациклен)
    total = sum(s.measured_sec["clip"] for s in sb.scenes)
    assert probe_duration(episode / final.video) == pytest.approx(total, abs=0.15)
    assert all(s.montage.music for s in sb.scenes)


@pytest.mark.render
def test_music_without_license_is_refused(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    add_music(episode, projects_root, license_text="")
    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True))
    assert not report.ok
    assert report.stopped_at == "assemble"
    assert report.reason and "license" in report.reason
    assert load_manifest(episode).stages["assemble"].status == "failed"


@pytest.mark.render
def test_report_has_a_montage_sheet(episode: Path, mock_providers: ProviderSet) -> None:
    run_episode(episode, mock_providers, OPTS)
    sb = load_storyboard(episode)
    text = render_report(episode, sb, load_manifest(episode)).read_text(encoding="utf-8")
    assert "Монтажный лист" in text
    assert text.count('class="thumbs"') == len(sb.scenes)
    assert "громкость микса" in text and "субтитры" in text  # таблица замеров против порогов


@pytest.mark.render
def test_rerun_after_final_pass_is_idempotent(episode: Path, mock_providers: ProviderSet) -> None:
    run_episode(episode, mock_providers, OPTS)
    first = (episode / "05_assemble" / "final.mp4").stat().st_mtime_ns
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok
    assert (episode / "05_assemble" / "final.mp4").stat().st_mtime_ns == first


def test_music_starts_where_the_plan_says(tmp_path: Path) -> None:
    """Выравнивание трека: `music.start_sec` двигает его по ролику.

    Так дроп трека сажают на кульминацию монтажа («сперва тихо, потом эпично»),
    а не подгоняют монтаж под чужую музыку. Проверяем последствие: до старта
    музыки в миксе тишина, после — нет."""
    from videoforge.core.ffmpeg import MusicMix, finalize, measure_loudness
    from videoforge.core.ffmpeg import run as ffmpeg_run

    video = tmp_path / "silent.mp4"
    ffmpeg_run(["-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=8",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(video)])  # fmt: skip
    track = tmp_path / "tone.wav"
    ffmpeg_run(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "8",
                "-ac", "2", "-c:a", "pcm_s16le", str(track)])  # fmt: skip

    out = tmp_path / "final.mp4"
    finalize(video, out, music=MusicMix(path=track, gain_db=-6.0, start_sec=4.0, fade_in_sec=0.2),
             fade_in_sec=0.0, fade_out_sec=0.0)  # fmt: skip

    def loudness(a: float, b: float) -> float | None:
        piece = tmp_path / f"w{a:.0f}.wav"
        ffmpeg_run(["-ss", f"{a}", "-t", f"{b - a}", "-i", str(out), "-map", "0:a:0",
                    "-c:a", "pcm_s16le", str(piece)])  # fmt: skip
        return measure_loudness(piece)[0]

    assert loudness(0.5, 3.0) is None  # до старта трека музыки нет — тишина
    assert loudness(5.0, 7.5) is not None  # после — играет
