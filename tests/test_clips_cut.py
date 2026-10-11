"""Стадия cut: отрезок исходника → клип со звуком речи и словами отрезка.

Сеть не трогаем: источник — локальный файл (`SourceInfo.file`), тот же путь,
что у нарезок из локального видео. Докачка отрезка с YouTube проверяется
живьём, руками, — за неё отвечает `providers/youtube.py: download_section`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import make_test_video

from videoforge.core.captions import Word, parse_srt_words, slice_words
from videoforge.core.ffmpeg import cut_segment, probe_duration, probe_stream_duration
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.stages.cut import EDGE_PAD_SEC, CutStage


def test_slice_words_shifts_to_zero_and_trims_the_edges() -> None:
    words = [Word("до", 0.0, 1.0), Word("край", 1.5, 2.5), Word("внутри", 3.0, 4.0, turn=True)]
    sliced = slice_words(words, 2.0, 3.5)
    assert [w.text for w in sliced] == ["край", "внутри"]
    assert (sliced[0].start, sliced[0].end) == (0.0, 0.5)  # слово на границе подрезано
    assert (sliced[1].start, sliced[1].end) == (1.0, 1.5) and sliced[1].turn


def test_cut_segment_keeps_sound_and_length(tmp_path: Path) -> None:
    src = make_test_video(tmp_path / "src.mp4")
    out = tmp_path / "piece.mp4"
    length = cut_segment(src, out, start_sec=10.0, duration_sec=5.0)
    assert 4.8 <= length <= 5.2
    assert (probe_stream_duration(out, "0:a:0") or 0) > 4.0  # звук исходника остался


def test_cut_fills_the_scene_from_the_source(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    ctx_scene = load_storyboard(clip_episode).scenes[0]
    assert ctx_scene.assets.video is None

    report = run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    assert report.ok, report.reason
    sb = load_storyboard(clip_episode)
    (scene,) = sb.scenes
    assert scene.assets.video == "03_video/s01.mp4"
    assert scene.assets.captions == "04_audio/s01.srt"
    assert scene.assets.audio is None  # речь в клипе, синтезировать нечего
    assert scene.provenance["video"] == "cut" and scene.checks.video == "pass"
    assert scene.checks.audio == "skipped"

    clip = clip_episode / scene.assets.video
    # отрезок шире момента на запас с обеих сторон: автосубтитры опережают звук
    wanted = 45.0 - 20.0 + 2 * EDGE_PAD_SEC
    assert abs(probe_duration(clip) - wanted) < 0.3
    assert abs(scene.duration_sec - wanted) < 0.3
    assert (probe_stream_duration(clip, "0:a:0") or 0) > wanted - 1  # звук на месте

    words = parse_srt_words((clip_episode / scene.assets.captions).read_text(encoding="utf-8"))
    assert words and words[0].start < 1.0  # слова отрезка сдвинуты к нулю
    assert words[-1].end <= wanted + 0.1
    assert scene.measured_sec["audio"] == pytest.approx(words[-1].end, abs=0.01)
    # запас по краям — для звука: слово соседней мысли, целиком лежащее в нём,
    # в субтитры не попадает (источник даёт слово в секунду: 19-е и 45-е —
    # соседи момента 20–45, оба в запасе)
    assert words[0].start >= EDGE_PAD_SEC - 0.05
    assert words[-1].end <= wanted - EDGE_PAD_SEC + 0.05
    assert [w.text for w in words][0] == "слово20" and [w.text for w in words][-1] == "слово44"


def test_cut_is_idempotent_and_force_redoes_it(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    clip = clip_episode / "03_video" / "s01.mp4"
    stamp = clip.stat().st_mtime_ns

    again = run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    assert again.executed[0][1].message.startswith("все отрезки уже нарезаны")
    assert clip.stat().st_mtime_ns == stamp  # файл не тронут

    forced = run_episode(clip_episode, mock_providers, RunOptions(only="cut", force=True))
    assert forced.ok and clip.stat().st_mtime_ns != stamp


def test_new_transcript_makes_the_cut_stale(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    """Транскрипт источника перечитали — слова отрезка устарели вместе с ним,
    иначе шортс остался бы со старыми субтитрами и никто бы не заметил."""
    from videoforge.clips.source import WORDS_FILE
    from videoforge.paths import source_dir

    run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    srt = clip_episode / "04_audio" / "s01.srt"
    stamp = srt.stat().st_mtime_ns

    words = source_dir("c", "vid1") / WORDS_FILE
    os.utime(words, (words.stat().st_atime, words.stat().st_mtime + 100))
    assert run_episode(clip_episode, mock_providers, RunOptions(only="cut")).ok
    assert srt.stat().st_mtime_ns != stamp


def test_cut_says_what_is_missing(clip_episode: Path, mock_providers: ProviderSet) -> None:
    sb = load_storyboard(clip_episode)
    sb.scenes[0].source = None
    from videoforge.core.storyboard import save_storyboard

    save_storyboard(clip_episode, sb)
    report = run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    assert not report.ok and "нет source" in (report.reason or "")


@pytest.mark.render
def test_shorts_assembles_with_sound_from_the_clip(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    """Ради этого и правился `mux_scene`: у нарезки нет отдельной озвучки, и
    сцена без `assets.audio` раньше получала тишину — шортс собирался немым."""
    assert run_episode(clip_episode, mock_providers, RunOptions(only="cut")).ok
    report = run_episode(
        clip_episode, mock_providers, RunOptions(start="edit", stop="assemble", auto_approve=True)
    )
    assert report.ok, report.reason

    sb = load_storyboard(clip_episode)
    assert sb.final.video
    final = clip_episode / sb.final.video
    assert (probe_stream_duration(final, "0:a:0") or 0) > 20.0  # звук исходника дошёл до финала
    assert sb.final.caption_events > 0  # субтитры собрались из транскрипта источника
    assert sb.final.frozen_sec == 0.0  # коробка равна клипу — замораживать нечего

    # снятый материал: неподвижность в кадре — это оператор, а не заморозка
    # рендера, и замечанием она быть не должна (иначе QA краснеет на каждом шортсе)
    rows = {
        c.label: c
        for c in sb.final.checks(captions_expected=True, music_expected=False, filmed=True)
    }
    assert rows["застывший кадр (детектор)"].ok


def test_cut_needs_no_provider_keys() -> None:
    """Стадия качает и режет, а не генерирует: ключей у неё быть не должно."""
    assert CutStage.needs == ()
    assert CutStage().estimate_cost(None) == 0.0  # type: ignore[arg-type]


def test_section_download_prefers_a_file_over_hls() -> None:
    """Отрезок из HLS-плейлиста ffmpeg скачивает пустым (0 пакетов, 261 байт) —
    так 17.09.2026 упали четыре шортса студийного подкаста. Файловый формат должен
    побеждать, даже когда сортировка yt-dlp ставит HLS выше."""
    import yt_dlp

    from videoforge.providers.youtube import _section_video_format

    formats = [  # yt-dlp отдаёт форматы от худшего к лучшему
        {"format_id": "399", "ext": "mp4", "height": 1080, "vcodec": "av01",
         "acodec": "none", "protocol": "https", "url": "https://x/399"},
        {"format_id": "616", "ext": "mp4", "height": 1080, "vcodec": "vp09",
         "acodec": "none", "protocol": "m3u8_native", "url": "https://x/616.m3u8"},
    ]  # fmt: skip
    ydl = yt_dlp.YoutubeDL({"quiet": True})
    ctx = {"formats": formats, "has_merged_format": False, "incomplete_formats": True}
    select = ydl.build_format_selector(_section_video_format(2160))
    assert [f["format_id"] for f in select(ctx)] == ["399"]

    only_hls = {**ctx, "formats": formats[1:]}  # файла нет — HLS остаётся запасным
    assert [f["format_id"] for f in select(only_hls)] == ["616"]


def test_blur_graph_puts_even_boxes_back_in_place() -> None:
    """Прямоугольник в долях кадра → пиксели чётные и не за краем, радиус влезает
    в плоскость цвета (она вдвое меньше): иначе boxblur не встаёт."""
    from videoforge.core.ffmpeg import blur_graph

    graph = blur_graph([(0.15, 0.72, 0.11, 0.14), (0.95, 0.9, 0.2, 0.3)], (1920, 1080))
    assert graph.startswith("[0:v:0]split=3[base][b0][b1]")
    assert "[b0]crop=210:150:288:776,boxblur=37:3:18:3[k0]" in graph
    assert "[base][k0]overlay=288:776[t0]" in graph
    # второй прямоугольник упирается в край кадра — обрезан по нему, а не за ним
    assert "[b1]crop=96:108:1824:972" in graph
    assert graph.endswith("[t1]null[v]")


@pytest.mark.render
def test_cut_blurs_the_boxes_of_the_source_and_recuts_when_they_change(
    clip_episode: Path, mock_providers: ProviderSet, tmp_path: Path
) -> None:
    """Вывеска казино в комнате стримера размывается на каждом отрезке
    (`SourceInfo.blur`); поменяли прямоугольники — отрезок режется заново."""
    from PIL import Image, ImageStat

    from videoforge.clips.source import BlurBox, load_source, save_source
    from videoforge.core.ffmpeg import frame_at
    from videoforge.core.io import read_model
    from videoforge.paths import source_dir
    from videoforge.stages.cut import SourceCut

    def spread(frame: Path, box: tuple[int, int, int, int]) -> float:
        return ImageStat.Stat(Image.open(frame).convert("L").crop(box)).stddev[0]

    assert run_episode(clip_episode, mock_providers, RunOptions(only="cut")).ok
    clip = clip_episode / "03_video" / "s01.mp4"
    sharp = frame_at(clip, 2.0, tmp_path / "sharp.jpg")

    sdir = source_dir("c", "vid1")
    info = load_source(sdir)
    assert info is not None
    box = BlurBox(x=0.0, y=0.0, w=0.5, h=0.5, note="вывеска")
    save_source(sdir, info.model_copy(update={"blur": [box]}))
    report = run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    assert report.ok and "уже нарезаны" not in report.executed[0][1].message
    soft = frame_at(clip, 2.0, tmp_path / "soft.jpg")

    inside, outside = (20, 20, 300, 160), (340, 200, 620, 340)  # кадр 640×360
    assert spread(soft, inside) < spread(sharp, inside) * 0.6  # детали внутри съедены
    assert abs(spread(soft, outside) - spread(sharp, outside)) < 5  # снаружи как было
    assert read_model(clip_episode / "03_video" / "s01_src.json", SourceCut).blur == [box]
    again = run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    assert again.executed[0][1].message.startswith("все отрезки уже нарезаны")
