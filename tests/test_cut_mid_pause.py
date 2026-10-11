"""Склейка посреди паузы, а не вплотную к следующему слову (features/cut_mid_pause).

Замер research/_probes/scene_cuts_2026-09-17: у референсного канала A склейка на паузе конца
предложения стоит через ~0.3 с после конца речи и за ~0.3 с до следующего слова —
картинка меняется в тишине. У нас после среза тишины (features/speech_pause_by_class)
вся пауза жила в хвосте уходящей сцены, и новая картинка появлялась за 40 мс до
слова. Здесь — как пауза делится между хвостом и заходом, что сдвигается на заход,
что хронометраж тот же и что готовый эпизод не пересчитывается.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_test_video

from videoforge.api.montage import episode_montage
from videoforge.core.captions import Word, parse_ass_events, parse_srt_words
from videoforge.core.ffmpeg import mux_scene, probe_duration
from videoforge.core.ffmpeg import run as ffmpeg_run
from videoforge.core.pauses import (
    CUT_MID_SHARE,
    LEAD_IN_KEY,
    PAUSE_CLAUSE_SEC,
    TRIM_PAD_SEC,
    ScriptMarks,
    lead_in,
    pause_after,
    scene_lead_in,
)
from videoforge.core.runner import RunReport, run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, Storyboard, load_storyboard, save_storyboard
from videoforge.core.waveform import envelope
from videoforge.providers.base import ProviderSet
from videoforge.stages.audio import RAW_KEY, AudioStage, Span
from videoforge.stages.edit_lint import silent_tail

AUDIO = RunOptions(only="audio", auto_approve=True, raise_errors=True)
TAIL = 0.5  # video.scene_tail_sec проекта из шаблона

LINES = [
    "Первая сцена рассказывает про долгий путь героя.",  # конец предложения
    "А что же было дальше в этой истории?",  # вопрос
    "Третья сцена идёт следом без остановки,",  # запятая: разрез внутри предложения
    "и четвёртая её заканчивает прямо здесь.",  # последняя
]


def _message(report: RunReport, stage: str = "audio") -> str:
    return dict(report.executed)[stage].message


def _words(episode: Path, scene: Scene) -> list[Word]:
    assert scene.assets.captions
    return parse_srt_words((episode / scene.assets.captions).read_text(encoding="utf-8"))


def _four_scenes(episode: Path) -> None:
    sb = load_storyboard(episode)
    sb.scenes = [
        Scene(
            id=f"s{i}",
            order=i,
            narration=text,
            visual_prompt=f"shot {i}",
            motion_prompt=("slow push in", "pan left", "tilt up", "slow pull back")[i - 1],
        )
        for i, text in enumerate(LINES, start=1)
    ]
    save_storyboard(episode, sb)


def _gaps(episode: Path, sb: Storyboard) -> list[tuple[float, float]]:
    """По каждому стыку: (тишина старой картинки после слова, новой — до слова)."""
    scenes = sb.ordered_scenes()
    out = []
    for cur, nxt in zip(scenes, scenes[1:], strict=False):
        after = cur.duration_sec - scene_lead_in(cur) - _words(episode, cur)[-1].end
        before = scene_lead_in(nxt) + _words(episode, nxt)[0].start
        out.append((round(after, 3), round(before, 3)))
    return out


def _uncut(episode: Path) -> None:
    """Раскадровка, как её оставила озвучка 16.09.2026: срезано, но без захода."""
    sb = load_storyboard(episode)
    for scene in sb.scenes:
        scene.measured_sec.pop(LEAD_IN_KEY, None)
    save_storyboard(episode, sb)


# ---------------------------------------------------------------- доля


def test_lead_in_puts_the_cut_in_the_middle_of_a_pause() -> None:
    head = pad = TRIM_PAD_SEC
    # конец предложения, пауза 0.7: до слова 0.35, из них 0.04 — запас файла
    assert lead_in("sentence", 0.7, head=head, tail_pad=pad) == round(0.35 - head, 3)
    assert CUT_MID_SHARE == 0.5
    for cls in ("question", "point", "block"):
        assert lead_in(cls, 0.6, head=head, tail_pad=pad) == round(0.3 - head, 3)  # type: ignore[arg-type]
    # разрез внутри предложения: голос идёт дальше, склейка между словами — на
    # начале файла озвучки, как до 17.09
    assert lead_in("clause", PAUSE_CLAUSE_SEC, head=head, tail_pad=pad) == 0.0
    assert lead_in("clause", 0.5, head=head, tail_pad=pad) == 0.0
    # короткая пауза: заход не залезает на звук уходящей сцены и не бывает < 0
    assert lead_in("sentence", 0.1, head=head, tail_pad=pad) == 0.01
    assert lead_in("sentence", 0.05, head=head, tail_pad=pad) == 0.0


def test_lead_in_gives_way_when_the_scene_would_outgrow_the_longest_clip() -> None:
    """Заход, с которым сцена не влезла бы в самый длинный клип провайдера,
    возвращается хвосту прошлой сцены: речь влезает, а кадр замёрз бы из-за
    тишины перед ней. Сумма коробок та же."""
    ends = {"s1": 3.04, "s2": 5.6, "s3": 2.5}
    scenes = []
    for i, (sid, end) in enumerate(ends.items(), start=1):
        scene = Scene(id=sid, order=i, narration=f"Реплика номер {i}.")
        scene.assets.audio = f"04_audio/{sid}.wav"
        scene.measured_sec = {"audio": end + TRIM_PAD_SEC, RAW_KEY: end + 1.0, LEAD_IN_KEY: 0.0}
        scenes.append(scene)
    words = {sid: [Word("слово", TRIM_PAD_SEC, end)] for sid, end in ends.items()}
    free = AudioStage._spans(scenes, words, ScriptMarks(), tail=TAIL, ceiling=60)
    tight = AudioStage._spans(scenes, words, ScriptMarks(), tail=TAIL, ceiling=6)
    assert free["s2"].lead_in + free["s2"].rest > 6
    assert tight["s2"].lead_in + tight["s2"].rest == pytest.approx(6.0, abs=0.002)
    assert 0 < tight["s2"].lead_in < free["s2"].lead_in
    assert tight["s3"] == free["s3"]

    def total(spans: dict[str, Span]) -> float:
        return sum(s.lead_in + s.rest for s in spans.values())

    assert total(tight) == pytest.approx(total(free), abs=0.002)


# ---------------------------------------------------------------- озвучка


def test_pause_is_split_between_the_tail_and_the_lead_in(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Стык двух реплик: тишина та же, что по классу, а склейка посреди неё.
    Первая сцена без захода, последняя — с хвостом целиком, после запятой —
    склейка вплотную к слову."""
    _four_scenes(episode)
    run_episode(episode, mock_providers, AUDIO)
    sb = load_storyboard(episode)
    s1, s2, s3, s4 = sb.ordered_scenes()
    assert all(LEAD_IN_KEY in s.measured_sec for s in (s1, s2, s3, s4))

    assert scene_lead_in(s1) == 0.0  # первая сцена: заход по fade in, как было
    pauses = [pause_after(a, b, ScriptMarks(), tail_sec=TAIL) for a, b in [(s1, s2), (s2, s3)]]
    for (after, before), pause in zip(_gaps(episode, sb)[:2], pauses, strict=True):
        assert abs(after + before - pause) < 0.011  # тишина между словами — пауза класса
        assert abs(before - pause * CUT_MID_SHARE) < 0.011  # склейка посреди неё
        assert after > 0.2 and before > 0.2  # и картинка до слова не 40 мс
    # после запятой заход нулевой: новая картинка за запас среза до слова
    assert scene_lead_in(s4) == 0.0
    after, before = _gaps(episode, sb)[2]
    assert abs(before - TRIM_PAD_SEC) < 0.002
    # последняя сцена: хвост после слова — вся пауза конца ролика
    tail = s4.duration_sec - scene_lead_in(s4) - _words(episode, s4)[-1].end
    assert abs(tail - pause_after(s4, None, ScriptMarks(), tail_sec=TAIL)) < 0.011
    # хвост тишины для crossfade — после файла озвучки; заход стоит в начале
    # коробки, и съесть его crossfade не вправе
    after_s2 = _gaps(episode, sb)[1][0]
    assert abs(silent_tail(s2) - (after_s2 - TRIM_PAD_SEC)) < 0.011


def test_split_keeps_the_running_time_and_the_word_gaps(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Хронометраж не меняется: сколько получил заход новой сцены, столько отдал
    хвост прошлой. Тишина между словами на каждом стыке та же."""
    _four_scenes(episode)
    run_episode(episode, mock_providers, AUDIO)
    split = load_storyboard(episode)
    _uncut(episode)
    run_episode(episode, mock_providers, AUDIO)  # только пересчёт: озвучка на месте
    whole = load_storyboard(episode)
    assert all(scene_lead_in(s) == 0.0 for s in whole.scenes)

    total = sum(s.duration_sec for s in split.scenes)
    assert abs(total - sum(s.duration_sec for s in whole.scenes)) < 0.03
    assert [s.duration_sec for s in split.ordered_scenes()] != [
        s.duration_sec for s in whole.ordered_scenes()
    ]  # коробки другие — склейка переехала
    for (a1, b1), (a2, b2) in zip(_gaps(episode, split), _gaps(episode, whole), strict=True):
        assert abs((a1 + b1) - (a2 + b2)) < 0.011


def test_ready_episode_is_not_retimed_by_the_split(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Эпизод, озвученный до 17.09 и с готовыми клипами: коробки не двигаются,
    клипы не перегенерируются. Переозвученная сцена заход получает только на
    стыке с такой же; `--force` переводит эпизод целиком."""
    _four_scenes(episode)
    run_episode(episode, mock_providers, AUDIO)
    _uncut(episode)
    run_episode(episode, mock_providers, AUDIO)
    sb = load_storyboard(episode)
    vdir = episode / "03_video"
    vdir.mkdir(exist_ok=True)
    for scene in sb.scenes:
        clip = vdir / f"{scene.id}_v01.mp4"
        clip.write_bytes(b"clip")
        scene.assets.image = f"02_images/{scene.id}.png"
        scene.assets.video = f"03_video/{clip.name}"
        scene.provenance["video"] = "mock"
        scene.checks.video = "pass"
        scene.attempts["video"] = 1
        scene.measured_sec["video"] = float(mock_providers.video.pick_duration(scene.duration_sec))
    save_storyboard(episode, sb)
    before = {s.id: s.duration_sec for s in sb.scenes}

    report = run_episode(episode, mock_providers, AUDIO)
    assert "до склейки посреди паузы сцен: 4" in _message(report)
    sb = load_storyboard(episode)
    assert {s.id: s.duration_sec for s in sb.scenes} == before
    assert all(LEAD_IN_KEY not in s.measured_sec for s in sb.scenes)
    video = RunOptions(only="video", auto_approve=True, raise_errors=True)
    report = run_episode(episode, mock_providers, video)
    assert "все клипы уже на месте" in _message(report, "video")
    assert all(s.attempts["video"] == 1 for s in load_storyboard(episode).scenes)

    # правка реплики: сцена озвучена заново и помечена, но соседи прежние —
    # стыки с ними не делятся, их коробки и клипы не тронуты
    sb.scene("s2").narration = "А что же случилось дальше в этой истории?"
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, AUDIO)
    sb = load_storyboard(episode)
    assert sb.scene("s2").measured_sec[LEAD_IN_KEY] == 0.0
    assert {s.id: s.duration_sec for s in sb.scenes if s.id != "s2"} == {
        k: v for k, v in before.items() if k != "s2"
    }

    # перевод эпизода целиком — переозвучка: заходы на всех стыках предложений
    force = RunOptions(only="audio", auto_approve=True, raise_errors=True, force=True)
    run_episode(episode, mock_providers, force)
    sb = load_storyboard(episode)
    assert scene_lead_in(sb.scene("s2")) > 0.2 and scene_lead_in(sb.scene("s3")) > 0.2


# ---------------------------------------------------------------- сборка


@pytest.mark.render
def test_mux_scene_delays_the_voice_by_the_lead_in(tmp_path: Path) -> None:
    """Звук сцены начинается через заход: первые секунды картинки — тишина,
    длина сцены — коробка, озвучка целиком."""
    video = make_test_video(tmp_path / "clip.mp4", seconds=3.0, size="320x568")
    voice = tmp_path / "voice.wav"
    ffmpeg_run(["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=24000:duration=1.5",
                "-c:a", "pcm_s16le", str(voice)])  # fmt: skip
    out = tmp_path / "scene.mp4"
    muxed = mux_scene(video, voice, out, size=(320, 568), target_sec=2.5, audio_delay=0.4)
    assert muxed.duration_sec == 2.5 and muxed.frozen_sec == 0.0
    assert abs(probe_duration(out) - 2.5) < 0.05
    loud = [i for i, v in enumerate(envelope(out, per_sec=100).values) if v > 0.05]
    assert abs(loud[0] / 100 - 0.4) < 0.04  # голос — через заход, а не с первого кадра
    assert abs((loud[-1] + 1) / 100 - 1.9) < 0.05  # и целиком: 0.4 + 1.5

    # звук длиннее коробки вместе с заходом: коробка растёт, озвучку не режем
    muxed = mux_scene(video, voice, out, size=(320, 568), target_sec=1.6, audio_delay=0.4)
    assert abs(muxed.duration_sec - 1.9) < 0.01


@pytest.mark.render
def test_assembled_words_and_captions_follow_the_lead_in(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """В готовом ролике слова, субтитры и замер пауз стоят там, где звучит
    голос: после захода, а не с первого кадра сцены. Хронометраж — сумма коробок."""
    _four_scenes(episode)
    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    assert report.ok, report.reason
    sb = load_storyboard(episode)
    s1, s2, s3, s4 = sb.ordered_scenes()
    assert scene_lead_in(s2) > 0.2

    # готовый файл: столько, сколько коробки (заход внутри них, а не сверху)
    assert sb.final.duration_sec is not None
    assert abs(sb.final.duration_sec - sum(s.duration_sec for s in sb.scenes)) < 0.1
    # медиана паузы на конце предложения меряется по словам на шкале финала: без
    # сдвига на заход вышла бы половина паузы
    expected = sorted(
        pause_after(a, b, ScriptMarks(), tail_sec=TAIL) for a, b in [(s1, s2), (s2, s3)]
    )
    assert sb.final.sentence_pause_sec is not None
    assert abs(sb.final.sentence_pause_sec - sum(expected) / 2) < 0.03

    # субтитры s2 начинаются с её первым словом, а не со склейкой
    rows = json.loads((episode / "05_assemble" / "timeline.json").read_text(encoding="utf-8"))
    cut = next(float(r["start"]) for r in rows if r["scene_id"] == "s2")
    speech = cut + scene_lead_in(s2) + _words(episode, s2)[0].start
    assert sb.final.captions
    events = parse_ass_events((episode / sb.final.captions).read_text(encoding="utf-8"))
    first = min((e.start for e in events if e.start > cut - 0.01), default=None)
    assert first is not None and abs(first - speech) < 0.02
    # монтажный лист пульта кладёт речь туда же
    tracks = {t["id"]: t for t in episode_montage("t", "e1")["tracks"]}
    spoken = [w["start"] for w in tracks["speech"]["items"] if w["start"] > cut - 0.01]
    assert abs(spoken[0] - speech) < 0.011
    # клип сцены — коробка целиком: заход внутри неё
    assert s2.measured_sec["clip"] == pytest.approx(s2.duration_sec, abs=0.05)
