"""Эффекты кадра, звуковые эффекты и замедленный хвост: план монтажа → рендер.

Всё это живёт не в сцене, а на таймлайне готового ролика, и проверяется тем же
способом, что и остальная сборка: замерами готового файла, а не тем, что мы
собирались сделать.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import count_frames

from videoforge.core.editplan import EditPlan, EffectCue, Outro, SfxCue, load_plan
from videoforge.core.ffmpeg import Effect, SlowTail, effect_filters, probe_duration, slow_tail
from videoforge.core.ffmpeg import run as ffmpeg_run
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.stages.edit_lint import EFFECTS_PER_MIN_BLOCK, lint_plan

OPTS = RunOptions(auto_approve=True, raise_errors=True)


def add_sfx(projects_root: Path, project: str = "t") -> None:
    """Звук в ресурсах проекта: короткий щелчок через ffmpeg. Права заполнены —
    без них сборка звук не возьмёт, и это отдельная проверка ниже."""
    sdir = projects_root / project / "resources" / "sfx"
    sdir.mkdir(parents=True, exist_ok=True)
    ffmpeg_run(["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100", "-t", "0.2",
                "-ac", "1", "-c:a", "pcm_s16le", str(sdir / "tick.wav")])  # fmt: skip
    (sdir / "tick.yaml").write_text(
        'id: tick\nfile: tick.wav\nlicense: "test"\nsource: "ffmpeg lavfi"\n'
        "kind: click\ngain_db: -8\n",
        encoding="utf-8",
    )


def test_effect_filters_are_empty_without_effects() -> None:
    """Ролик без эффектов не должен платить за них ни одним фильтром: `zoompan`
    пересэмплирует каждый кадр, и включать его «на всякий случай» нельзя."""
    assert effect_filters([], size=(1080, 1920)) == []


def test_geometry_effects_share_one_zoompan() -> None:
    """Наезд и тряска — это одно окно кадра, и считать его дважды незачем."""
    chain = effect_filters(
        [Effect("zoom_punch", 2.0, 0.3), Effect("shake", 5.0, 0.4)], size=(1080, 1920)
    )
    zoompans = [f for f in chain if f.startswith("zoompan")]
    assert len(zoompans) == 1
    graph = zoompans[0]
    assert "between(it,2.000,2.300)" in graph and "between(it,5.000,5.400)" in graph
    assert "sin(2*PI*" in graph  # тряска — синусы по обеим осям


def test_colour_effects_share_one_eq_and_ramp() -> None:
    chain = effect_filters(
        [Effect("flash", 1.0, 0.15), Effect("desaturate", 4.0, 2.0)], size=(1080, 1920)
    )
    eq = next(f for f in chain if f.startswith("eq="))
    assert "brightness=" in eq and "saturation=" in eq and "eval=frame" in eq
    # вспышка бьёт сразу и спадает, состояние входит трапецией — иначе на входе
    # и выходе слышен щелчок глазами
    assert "max(0,1-(t-1.000)" in eq and "min(min(" in eq


def test_slow_tail_keeps_the_head_and_the_tracks_together(tmp_path: Path) -> None:
    """Замедление хвоста — единственное намеренное изменение хронометража.

    Проверяем ровно то, что может сломаться: длина выросла на замедление, а
    дорожки остались вместе. Расхождение дорожек замедление умножает, поэтому
    обе в конце принудительно доводятся до одной длины."""
    from videoforge.core.ffmpeg import probe_stream_duration

    src = tmp_path / "src.mp4"
    ffmpeg_run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100:duration=6",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(src)])  # fmt: skip
    out = tmp_path / "slow.mp4"
    total = slow_tail(src, out, SlowTail(take_sec=2.0, speed=0.5, zoom=0.1, vignette=True))
    assert total == pytest.approx(8.0, abs=0.3)  # 4 обычных + 2 вдвое медленнее
    video = probe_stream_duration(out, "0:v:0")
    audio = probe_stream_duration(out, "0:a:0")
    assert video is not None and audio is not None
    assert abs(audio - video) <= 0.1


def test_slowed_tail_stays_constant_frame_rate(tmp_path: Path) -> None:
    """Замедленный хвост обязан остаться с постоянной частотой кадров.

    Без `fps` после `setpts` в хвосте остаётся исходное число кадров на вдвое
    большем интервале, то есть файл становится VFR. Следующий фильтр, который
    переставляет метки времени (`zoompan` в эффектах), пересчитает такой хвост
    обратно — и картинка окажется на длину замедления короче звука. Поймано
    замером av_drift на живом шортсе: +2.354 с при хвосте 2.3 с.

    С 14.09.2026 `effect_filters` сам ставит `fps` перед `zoompan` (склейка
    сцен тоже приходит с дырами в метках), и проход через эффекты VFR уже не
    выдаст, поэтому `slow_tail` проверяется прямо по счёту кадров: их столько,
    сколько велит длительность дорожки."""
    from videoforge.core.ffmpeg import probe_stream_duration

    src = tmp_path / "src.mp4"
    ffmpeg_run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=4",
                "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100:duration=4",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(src)])  # fmt: skip
    slow = tmp_path / "slow.mp4"
    slow_tail(src, slow, SlowTail(take_sec=2.0, speed=0.5))
    slow_v = probe_stream_duration(slow, "0:v:0")
    assert slow_v is not None
    assert abs(count_frames(slow) - round(slow_v * 24)) <= 1
    zoomed = tmp_path / "zoomed.mp4"
    graph = ",".join(effect_filters([Effect("zoom_punch", 1.0, 0.3)], size=(320, 180)))
    ffmpeg_run(["-i", str(slow), "-vf", graph, "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "copy", str(zoomed)])  # fmt: skip
    assert probe_duration(zoomed) == pytest.approx(probe_duration(slow), abs=0.15)


@pytest.mark.render
def test_plan_effects_reach_the_final_file(
    episode: Path, projects_root: Path, mock_providers: ProviderSet
) -> None:
    """Ручной план с эффектами, звуками и замедленным хвостом → готовый файл.

    Проверяются последствия, а не намерения: хронометраж вырос ровно на
    замедление, дорожки не разъехались, монтажный лист помнит, что применили."""
    add_sfx(projects_root)
    (episode / "05_edit").mkdir(exist_ok=True)
    plan = EditPlan(
        author="manual",
        effects=[
            EffectCue(at_sec=0.5, duration_sec=0.3, kind="zoom_punch", why="проверка наезда"),
            EffectCue(at_sec=1.5, duration_sec=0.15, kind="flash", why="проверка вспышки"),
            EffectCue(at_sec=2.0, duration_sec=0.12, kind="glitch", why="проверка глитча"),
        ],
        sfx=[SfxCue(at_sec=0.5, id="tick", why="кадр в кадр с наездом")],
        outro=Outro(take_sec=1.0, speed=0.5, pitch_drop=True, zoom=0.1, why="проверка хвоста"),
        notes="тест",
    )
    (episode / "05_edit" / "edit_plan.json").write_text(
        plan.model_dump_json(indent=2, exclude_none=False), encoding="utf-8"
    )

    run_episode(episode, mock_providers, OPTS)
    sb = load_storyboard(episode)
    final = sb.final
    assert final.effects == 3 and final.sfx == 1
    assert final.outro_added_sec == pytest.approx(1.0, abs=0.25)  # 1 с × 0.5 → +1 с
    assert final.av_drift_sec is not None and abs(final.av_drift_sec) <= 0.1
    assert load_plan(episode) is not None and load_plan(episode).author == "manual"  # type: ignore[union-attr]
    montage = json.loads((episode / "05_assemble" / "montage.json").read_text(encoding="utf-8"))
    assert len(montage["effects"]) == 3 and len(montage["sfx"]) == 1
    assert montage["outro"]["take_sec"] == 1.0


@pytest.mark.render
def test_broken_manual_plan_does_not_overwrite_a_good_final(
    episode: Path, projects_root: Path, mock_providers: ProviderSet
) -> None:
    """План человека с блокирующей находкой роняет сборку и НЕ трогает ролик.

    Раньше такой план молча подменялся дефолтным, и ролик пересобирался без
    правок: файл на месте, играет, а того, ради чего его правили, в нём нет.
    Пока план правили в редакторе, это стоило одного прогона; когда его правят
    кнопкой в браузере, это стало бы обычным делом."""
    run_episode(episode, mock_providers, OPTS)
    final = episode / "05_assemble" / "final.mp4"
    made = final.stat().st_mtime_ns
    assert final.exists()

    (episode / "05_edit" / "edit_plan.json").write_text(
        EditPlan(author="manual", caption_style="нет-такого-стиля").model_dump_json(indent=2),
        encoding="utf-8",
    )
    # именно так пересобирает кнопка в пульте: одна стадия, без стадии edit,
    # которая тот же план проверила бы раньше
    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True, only="assemble"))
    assert report.stopped_at == "assemble"
    assert report.reason is not None and "не прошёл проверку" in report.reason
    assert final.stat().st_mtime_ns == made  # ролик не тронут


@pytest.mark.render
def test_sfx_without_rights_stops_the_render(
    episode: Path, projects_root: Path, mock_providers: ProviderSet
) -> None:
    """Права на звук обязательны так же, как на музыку: Content ID ловит и
    трёхсекундный свист."""
    add_sfx(projects_root)
    sdir = projects_root / "t" / "resources" / "sfx"
    (sdir / "tick.yaml").write_text(
        'id: tick\nfile: tick.wav\nlicense: ""\nsource: ""\n', encoding="utf-8"
    )
    (episode / "05_edit").mkdir(exist_ok=True)
    plan = EditPlan(author="manual", sfx=[SfxCue(at_sec=0.5, id="tick")])
    (episode / "05_edit" / "edit_plan.json").write_text(
        plan.model_dump_json(indent=2), encoding="utf-8"
    )
    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True))
    assert report.stopped_at == "assemble"
    assert report.reason is not None and "license" in report.reason


def test_lint_holds_the_quotas_on_effects(episode: Path) -> None:
    """Модель, которой дали вспышку, поставит её везде — считать это должен
    python. Порог на минуту хронометража, потому что эффекты живут на
    таймлайне, а не в сценах."""
    sb = load_storyboard(episode)
    total = sum(s.duration_sec for s in sb.ordered_scenes())
    many = int(EFFECTS_PER_MIN_BLOCK * total / 60) + 2
    plan = EditPlan(
        author="manual",
        effects=[
            EffectCue(at_sec=total * i / (many + 1), kind="flash") for i in range(1, many + 1)
        ],
    )
    result = lint_plan(
        plan, sb, duration_of=lambda _: total, style_exists=lambda _: True,
        track_exists=lambda _: True, sfx_exists=lambda _: True,
    )  # fmt: skip
    assert any(f.code == "too_many_effects" for f in result.blocking)


def test_lint_drops_cues_outside_the_episode_and_clamps_the_outro(episode: Path) -> None:
    """Эффект за концом ролика молча не сработает, и человек будет искать его
    глазами; замедлять больше половины ролика — это уже не хвост."""
    sb = load_storyboard(episode)
    total = sum(s.duration_sec for s in sb.ordered_scenes())
    plan = EditPlan(
        author="manual",
        effects=[EffectCue(at_sec=total + 30, kind="flash")],
        sfx=[SfxCue(at_sec=1.0, id="nope")],
        outro=Outro(take_sec=total, speed=0.05),
    )
    result = lint_plan(
        plan, sb, duration_of=lambda _: total, style_exists=lambda _: True,
        track_exists=lambda _: True, sfx_exists=lambda _: False,
    )  # fmt: skip
    assert result.plan.effects == []  # эффект вне ролика убран автопочинкой
    assert any(f.code == "unknown_sfx" for f in result.blocking)  # а выдуманный звук — блок
    assert result.plan.outro is not None
    assert result.plan.outro.take_sec <= total / 2 and result.plan.outro.speed >= 0.25
