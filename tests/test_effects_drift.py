"""Дрейф дорожек от эффектов кадра: склейка сцен с дырами в метках → `zoompan`.

Живой случай 14.09.2026, `doc_channel/ep001` с планом модели (2 zoom_punch,
5 звуков): `scenes.mp4` — картинка 506.958 с при звуке 506.974; после финального
прохода картинка 506.708, звук 506.975 — минус 6 кадров, av_drift +0.267 при
пороге 0.1. Прежняя сборка того же эпизода без эффектов давала +0.034.
Тесты эффектов этого не ловили: у эпизода заглушки две сцены — один стык, а
дыра в дорожке появляется, когда сдвиг звука над картинкой накопится больше
0.6 кадра, то есть раз в несколько стыков; на двух сценах дрейфу неоткуда взяться.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import count_frames

from videoforge.core.ffmpeg import (
    FPS,
    Effect,
    Sound,
    concat,
    effect_filters,
    finalize,
    probe_stream_duration,
)
from videoforge.core.ffmpeg import run as ffmpeg_run

SCENES = 12
SIZE = (480, 854)  # тест про метки времени, а не про пиксели — мельче и быстрее


def gappy_concat(tmp_path: Path) -> Path:
    """Склейка сцен, у которых звук на доли кадра длиннее картинки — как у
    живых сцен (s02 ep001: 115 кадров = 4.7917 с при звуке 4.8065). Concat-
    демуксер ставит следующую сцену на длительность контейнера, и ffmpeg,
    накопив больше 0.6 кадра сдвига, оставляет в дорожке дыру в целый кадр."""
    clips: list[Path] = []
    for i in range(SCENES):
        clip = tmp_path / f"s{i:02d}.mp4"
        ffmpeg_run(["-f", "lavfi", "-i", f"testsrc=size={SIZE[0]}x{SIZE[1]}:rate={FPS}:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1.02",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(clip)])  # fmt: skip
        clips.append(clip)
    scenes = tmp_path / "scenes.mp4"
    concat(clips, scenes)
    return scenes


def test_geometry_chain_starts_with_fps_from_zero() -> None:
    """`zoompan` кладёт кадры на свою сетку по счёту, с нуля: дыры в метках
    входа исчезают, и картинка становится короче на их сумму. Поэтому перед
    ним — `fps` с сеткой от нуля; без `start_time=0` стартовая дыра остаётся."""
    chain = effect_filters([Effect("zoom_punch", 2.0, 0.3)], size=SIZE)
    zoompan = next(i for i, f in enumerate(chain) if f.startswith("zoompan"))
    assert zoompan > 0
    assert chain[zoompan - 1] == f"fps={FPS}:start_time=0"
    # цвет и глитч работают по `t` входа и метки не переставляют — им fps не нужен
    assert not any(f.startswith("fps=") for f in effect_filters([Effect("flash", 1.0)], size=SIZE))


@pytest.mark.render
def test_effects_keep_every_frame_of_a_gappy_concat(tmp_path: Path) -> None:
    """Два наезда и пять звуков на склейке с дырами → дорожки вместе, кадров
    столько, сколько велит длительность.

    До починки (14.09.2026) на этой же склейке: картинка 12.000 с при звуке
    12.26 (288 кадров вместо 294), дрейф +0.26; без эффектов +0.03."""
    scenes = gappy_concat(tmp_path)
    v_in = probe_stream_duration(scenes, "0:v:0")
    assert v_in is not None
    expected = round(v_in * FPS)
    # склейка обязана быть VFR с дырами — иначе тест ничего не проверяет
    assert count_frames(scenes) <= expected - 3, (count_frames(scenes), expected)

    tick = tmp_path / "tick.wav"
    ffmpeg_run(["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100", "-t", "0.2",
                "-ac", "1", "-c:a", "pcm_s16le", str(tick)])  # fmt: skip
    out = tmp_path / "final.mp4"
    finalize(
        scenes,
        out,
        size=SIZE,
        effects=[Effect("zoom_punch", 4.3, 0.3, 0.6), Effect("zoom_punch", 9.0, 0.3, 0.8)],
        sounds=[Sound(tick, at) for at in (1.5, 3.7, 4.3, 8.0, 9.0)],
    )
    video = probe_stream_duration(out, "0:v:0")
    audio = probe_stream_duration(out, "0:a:0")
    assert video is not None and audio is not None
    assert abs(audio - video) <= 0.05, (video, audio)
    assert abs(count_frames(out) - expected) <= 1, (count_frames(out), expected)


def _onsets(path: Path) -> list[float]:
    """Секунды, где на дорожке начинается звук (порог тишины, 8 кГц моно)."""
    import subprocess

    from videoforge.core.ffmpeg import ffmpeg_exe

    raw = subprocess.run(
        [ffmpeg_exe(), "-v", "quiet", "-i", str(path), "-ac", "1", "-ar", "8000",
         "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    samples = memoryview(raw).cast("h")
    starts: list[float] = []
    last = -10.0
    for i in range(0, len(samples), 40):  # шаг 5 мс
        if abs(samples[i]) > 100:
            t = i / 8000
            if t - last > 0.5:
                starts.append(round(t, 2))
            last = t
    return starts


def test_sfx_bus_puts_every_sound_in_its_second(tmp_path: Path) -> None:
    """Восемь звуков, в том числе поздние и два внахлёст, — каждый в своей
    секунде и ничего в начале. ep003 (18.09.2026): при входе на каждый звук со
    своим `adelay` ffmpeg положил последний (1051.8 с) в начало ролика."""
    from videoforge.core.ffmpeg import sfx_bus

    tone = tmp_path / "tone.wav"
    ffmpeg_run(["-f", "lavfi", "-i", "sine=frequency=880:duration=0.4",
                "-ac", "2", "-ar", "44100", str(tone)])  # fmt: skip
    at = [3.0, 7.5, 12.0, 20.0, 20.1, 31.0, 44.4, 58.2]  # 20.0 и 20.1 — внахлёст
    bus = sfx_bus([Sound(tone, t, -6.0) for t in at], 60.0, tmp_path / "bus.wav")
    assert abs(probe_stream_duration(bus, "0:a:0") - 60.0) < 0.05
    got = _onsets(bus)
    assert got[0] > 1.0, f"в начале дорожки звук, которого нет в плане: {got}"
    expected = [3.0, 7.5, 12.0, 20.0, 31.0, 44.4, 58.2]  # 20.1 сливается с 20.0
    assert len(got) == len(expected)
    assert all(abs(g - e) < 0.02 for g, e in zip(got, expected, strict=True))
