"""Наезд продолжается через стык продолжений плана (features/zoom_chain_continues).

Длинный план режется на сцены-продолжения (`Scene.continues`), и на стыке наезд
начинался заново с исходного масштаба: у remotion — каждое звено шло по всему
пути камеры, у i2v — каждая генерация стартует с кадра как есть. Сшивается
двумя путями: программная камера получает место в цепочке (`ChainLink`) и
рисует свой отрезок пути, генеративный клип кропает сборка по
`video.zoom_per_sec` проекта. Стык меряется разностью соседних кадров, а не
предполагается.
"""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from videoforge.core.ffmpeg import frame_at, probe_duration
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions, StageContext
from videoforge.core.storyboard import (
    ChainLink,
    Scene,
    Storyboard,
    chain_links,
    load_storyboard,
    save_storyboard,
)
from videoforge.providers.base import ProviderSet
from videoforge.providers.remotion_video import STRENGTH_MAX, STRENGTH_SLOW, motion_props
from videoforge.stages.assemble import chain_zoom
from videoforge.stages.video import VideoStage


def _chain(*lengths: float, tail: float | None = 4.0) -> list[Scene]:
    """s01 и его продолжения заданной длины; `tail` — обычная сцена после."""
    scenes = [Scene(id="s01", order=1, duration_sec=lengths[0])]
    for n, sec in enumerate(lengths[1:], start=2):
        scenes.append(Scene(id=f"s01_{n}", order=n, duration_sec=sec, continues=scenes[-1].id))
    if tail is not None:
        scenes.append(Scene(id="s02", order=len(scenes) + 1, duration_sec=tail))
    return scenes


# --- чистая арифметика цепочки


def test_chain_links_place_every_link_of_the_plan() -> None:
    links = chain_links(_chain(6.0, 6.0, 5.0))
    assert set(links) == {"s01", "s01_2", "s01_3"}  # s02 — не цепочка, стыка нет
    assert links["s01"] == ChainLink("s01", 0.0, 6.0, 17.0, 6.0)
    assert links["s01_2"] == ChainLink("s01", 6.0, 6.0, 17.0, 6.0)
    assert links["s01_3"] == ChainLink("s01", 12.0, 5.0, 17.0, 6.0)


def test_chain_links_follow_order_not_list_position() -> None:
    scenes = _chain(6.0, 6.0)
    scenes.reverse()
    assert chain_links(scenes)["s01_2"].before_sec == 6.0


def test_chain_zoom_is_a_product_of_previous_links() -> None:
    """Масштабы перемножаются: (1 + k·d₁)(1 + k·d₂), не 1 + k·(d₁ + d₂)."""
    zoom = chain_zoom(_chain(6.0, 6.0, 6.0), 0.02)
    assert zoom["s01"] == 1.0  # голова начинает с исходного масштаба
    assert zoom["s01_2"] == pytest.approx(1.12)
    assert zoom["s01_3"] == pytest.approx(1.12 * 1.12)  # 1.2544, а не 1.24
    assert zoom["s02"] == 1.0


def test_chain_zoom_is_off_without_the_setting_or_when_provider_chained() -> None:
    assert set(chain_zoom(_chain(6.0, 6.0), 0.0).values()) == {1.0}
    scenes = _chain(6.0, 6.0)
    scenes[1].chained = True  # remotion уже начал клип с места хода
    assert set(chain_zoom(scenes, 0.02).values()) == {1.0}


# --- программная камера: отрезок пути и скорость головы


def test_remotion_props_continue_the_path_at_the_head_speed() -> None:
    size = (1080, 1920)
    link = ChainLink("s01", before_sec=6.0, own_sec=6.0, total_sec=18.0, head_sec=6.0)
    props = motion_props("slow push in", duration_sec=6, size=size, chain=link)
    assert props["strength"] == pytest.approx(STRENGTH_SLOW * 3)  # ход на весь план
    assert props["pathFrom"] == pytest.approx(1 / 3)
    assert props["pathTo"] == pytest.approx(2 / 3)
    # клип головы: путь с нуля, в точке среза (6 с) ровно 1 + strength — как один
    head = motion_props(
        "slow push in", duration_sec=6, size=size, chain=ChainLink("s01", 0.0, 6.0, 18.0, 6.0)
    )
    assert head["pathFrom"] == 0.0 and head["pathTo"] == pytest.approx(1 / 3)
    # последнее звено: клип на секунду длиннее звена (ceil) — путь выходит за 1.0
    last = motion_props(
        "slow push in", duration_sec=7, size=size, chain=ChainLink("s01", 12.0, 6.0, 18.0, 6.0)
    )
    assert last["pathTo"] == pytest.approx(19 / 18)


def test_remotion_props_cap_the_strength_but_keep_the_chain_seamless() -> None:
    size = (1920, 1080)
    long = [ChainLink("s01", 6.0 * i, 6.0, 30.0, 6.0) for i in range(5)]
    strengths = {
        motion_props("fast push in", duration_sec=6, size=size, chain=link)["strength"]
        for link in long
    }
    assert strengths == {STRENGTH_MAX}  # 0.16 × 5 = 0.8 → потолок, один на все звенья


def test_remotion_props_without_chain_walk_the_whole_path() -> None:
    props = motion_props("slow push in", duration_sec=6, size=(1080, 1920))
    assert "pathFrom" not in props and "pathTo" not in props  # умолчания схемы: 0 → 1
    assert props["strength"] == STRENGTH_SLOW


# --- отпечаток входов клипа


def _ctx(scenes: list[Scene], *, chains_camera: bool) -> StageContext:
    sb = Storyboard(project_id="t", scenes=scenes)
    ns = SimpleNamespace(
        resources=SimpleNamespace(style=None),
        storyboard=sb,
        providers=SimpleNamespace(video=SimpleNamespace(chains_camera=chains_camera)),
    )
    return cast(StageContext, ns)


def test_video_inputs_follow_the_chain_only_for_programmatic_camera() -> None:
    """Правка длины соседнего звена сдвигает место в цепочке: у программной
    камеры это перегенерирует продолжение ($0), у генеративной — нет: там
    стык сшивает сборка, а перегенерация стоила бы денег."""
    before = _chain(6.0, 6.0)
    after = _chain(8.0, 6.0)  # голова стала длиннее → s01_2 стартует позже по пути
    link = lambda scenes: next(s for s in scenes if s.id == "s01_2")  # noqa: E731
    assert VideoStage._inputs(_ctx(before, chains_camera=True), link(before)) != (
        VideoStage._inputs(_ctx(after, chains_camera=True), link(after))
    )
    assert VideoStage._inputs(_ctx(before, chains_camera=False), link(before)) == (
        VideoStage._inputs(_ctx(after, chains_camera=False), link(after))
    )
    # сцена вне цепочки — отпечаток тот же, что до появления цепочек
    lone = Scene(id="x", order=1)
    assert VideoStage._inputs(_ctx([lone], chains_camera=True), lone) == (
        VideoStage._inputs(_ctx([lone], chains_camera=False), lone)
    )


# --- рендер: стык меряется


def _textured(path: Path, size: tuple[int, int], seed: int) -> None:
    """Кадр из цветных клеток: на плоском цвете заглушки скачок масштаба не
    виден никаким замером, а клетки 60 px при наезде 12% уезжают на краях на
    полклетки и больше."""
    rng = random.Random(seed)
    w, h = size
    cell = 60
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(0, h, cell):
        for x in range(0, w, cell):
            color = (rng.randrange(40, 220), rng.randrange(40, 220), rng.randrange(40, 220))
            draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=color)
    img.save(path)


def _seam(prev_clip: Path, next_clip: Path, tmp: Path) -> float:
    """Средняя разность яркости (0..255) последнего кадра одного клипа и
    первого кадра следующего: то, что глаз видит на стыке."""
    last = probe_duration(prev_clip) - 1.5 / 24  # первый кадр не раньше этой секунды — последний
    a = Image.open(frame_at(prev_clip, last, tmp / "a.jpg")).convert("L")
    b = Image.open(frame_at(next_clip, 0.0, tmp / "b.jpg")).convert("L")
    return float(ImageStat.Stat(ImageChops.difference(a, b)).mean[0])


def _set_zoom_per_sec(projects_root: Path, value: float) -> None:
    cfg = projects_root / "t" / "project.yaml"
    text = cfg.read_text(encoding="utf-8")
    assert "zoom_per_sec:" in text  # поле в шаблоне: новое поле → строка с комментарием
    lines = [
        f"  zoom_per_sec: {value:g}" if line.startswith("  zoom_per_sec:") else line
        for line in text.splitlines()
    ]
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.render
def test_continuation_starts_where_the_previous_link_ended(
    episode: Path, mock_providers: ProviderSet, projects_root: Path, tmp_path: Path
) -> None:
    """Цепочка из трёх звеньев по 6 с на заглушке (i2v: клип едет 12% с
    исходного масштаба). Без `zoom_per_sec` стык отпрыгивает, с ним — второе
    звено стартует на масштабе, где кончилось первое, третье — где второе."""
    sb = load_storyboard(episode)
    sb.scenes = _chain(6.0, 6.0, 6.0, tail=None)
    for s in sb.scenes:  # без речи: длина звена остаётся плановой, 6.0 ровно
        s.visual_prompt, s.motion_prompt = "night sky over the sea", "slow push in"
    save_storyboard(episode, sb)

    def run(**kw: str) -> None:
        report = run_episode(episode, mock_providers, RunOptions(auto_approve=True, **kw))
        assert report.ok, report.reason

    run(stop="images")
    sb = load_storyboard(episode)
    for s in sb.scenes:  # один и тот же текстурный кадр на весь план, как у продолжений
        assert s.assets.image
        _textured(episode / s.assets.image, (1080, 1920), seed=7)

    def seams() -> tuple[float, float]:
        out = episode / "05_assemble"
        return (
            _seam(out / "s01.mp4", out / "s01_2.mp4", tmp_path / "seam1"),
            _seam(out / "s01_2.mp4", out / "s01_3.mp4", tmp_path / "seam2"),
        )

    # --- до: наезд каждого звена с нуля
    run(start="video")
    sb = load_storyboard(episode)
    assert all(s.montage.zoom == 1.0 for s in sb.scenes) and not any(s.chained for s in sb.scenes)
    jump1, jump2 = seams()

    # --- после: кроп продолжения до масштаба конца предыдущего звена
    _set_zoom_per_sec(projects_root, 0.02)  # 12% за 6 с — скорость заглушки
    run(only="assemble")
    sb = load_storyboard(episode)
    by_id = {s.id: s for s in sb.scenes}
    assert by_id["s01"].montage.zoom == 1.0
    assert by_id["s01_2"].montage.zoom == pytest.approx(1.12)
    assert by_id["s01_3"].montage.zoom == pytest.approx(1.2544)
    seam1, seam2 = seams()

    # замер 14.09.2026 на этом кадре (яркость 0..255): скачок 33.2 / 33.2 →
    # шов 2.1 / 2.3, при том что соседние кадры ВНУТРИ звена расходятся на 1.3
    # (сам наезд за кадр плюс кодек): шов — это два кадра одного хода через
    # перекодирование. Порог — треть скачка: шов меньше трети скачка глазом за
    # скачок не читается, а ошибка в знаке или в порядке множителей держала бы
    # его на уровне самого скачка
    assert jump1 > 25 and jump2 > 25, (jump1, jump2)
    assert seam1 < jump1 / 3 and seam2 < jump2 / 3, (jump1, seam1, jump2, seam2)
    # длины звеньев кроп не тронул, дорожки не разъехались
    assert all(s.measured_sec["clip"] == pytest.approx(6.0, abs=0.05) for s in sb.scenes)
    assert sb.final.av_drift_sec is not None and abs(sb.final.av_drift_sec) <= 0.1

    # смена скорости — новый отпечаток сборки: без неё правка k ничего бы не пересобрала
    first = sb.final.inputs_fingerprint
    _set_zoom_per_sec(projects_root, 0.01)
    run(only="assemble")
    assert load_storyboard(episode).final.inputs_fingerprint != first
