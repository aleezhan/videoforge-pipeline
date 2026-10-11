"""Часть сцен клипом, остальные — картинкой с наездом
(`features/mixed_still_and_clip_scenes`, 16.09.2026).

Что здесь держится: долю клипов по времени считает python (`plan_render`) —
первые 10% хронометража клипами, остаток доли рассыпан по хвосту, а не кучей,
с предпочтением действию и участникам; план продолжений получает одно решение;
поставленное руками побеждает; решение пишется в раскадровку один раз и не
перетасовывается сменой длительностей; старая раскадровка без поля не
устаревает; картинка-сцена идёт вторым провайдером без проверки по кадрам, а
неготовый провайдер останавливает стадию до первого платного клипа.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from videoforge.cli import still_status
from videoforge.config import ProviderNames
from videoforge.core.ffmpeg import run as ffmpeg_run
from videoforge.core.manifest import load_manifest
from videoforge.core.project import load_project
from videoforge.core.runner import estimate_episode, required_providers, run_episode
from videoforge.core.stage import RunOptions, StageContext
from videoforge.core.storyboard import (
    ChainLink,
    MotionReference,
    Scene,
    SceneRender,
    clip_share_of,
    fingerprint,
    load_storyboard,
    plan_render,
    save_storyboard,
    shots,
)
from videoforge.providers.base import GenResult, MotionGuide, ProviderSet
from videoforge.providers.mock import MockStill, MockVideo
from videoforge.providers.registry import build_providers
from videoforge.stages.video import VideoStage

# ---------------------------------------------------------------- раскладка


def _storyboard(
    total: float = 1440.0, *, seed: int = 7, action_band: tuple[float, float] | None = None
) -> list[Scene]:
    """Раскадровка длинного ролика: сцены 2–12 с, часть — продолжения плана,
    у части герой в кадре. `action_band` — действие только в этой доле
    хронометража (битва посередине), иначе вперемешку."""
    rng = random.Random(seed)
    scenes: list[Scene] = []
    clock = 0.0
    while clock < total:
        n = len(scenes) + 1
        seconds = rng.choice([2, 2.5, 3, 4, 5, 5.5, 6, 7, 9, 12])
        if action_band is not None:
            lo, hi = action_band
            tempo = "action" if lo * total <= clock < hi * total else "calm"
        else:
            tempo = rng.choice(["action", "calm", "calm"])
        scene = Scene(
            id=f"s{n:03d}",
            order=n,
            duration_sec=seconds,
            tempo=tempo,  # type: ignore[arg-type]
            cast=["characters/amir"] if rng.random() < 0.4 else [],
        )
        if scenes and rng.random() < 0.08:
            scene.continues = scenes[-1].id
        scenes.append(scene)
        clock += seconds
    return scenes


def _timeline(scenes: list[Scene]) -> list[tuple[Scene, float]]:
    out, clock = [], 0.0
    for scene in sorted(scenes, key=lambda s: s.order):
        out.append((scene, clock))
        clock += scene.duration_sec
    return out


def test_all_clips_by_default() -> None:
    scenes = _storyboard(300)
    modes = plan_render(scenes, clip_share=1.0)
    assert set(modes.values()) == {"clip"}


def test_a_scene_longer_than_the_provider_clip_is_not_a_clip() -> None:
    """Клип у LTX не длиннее 6 с, а сцена бывает 7–10: остаток доигрывал бы
    замороженный кадр. На ep003 (18.09.2026) так замёрзло 25 с."""
    scenes = _storyboard(seed=3)
    modes = plan_render(scenes, clip_share=0.2, max_clip_sec=6)
    long_ones = [s for s in scenes if s.duration_sec > 6.3]
    assert long_ones, "в раскладке должны быть сцены длиннее клипа"
    assert {modes[s.id] for s in long_ones} == {"still"}
    # без потолка правило не действует: провайдер мог уметь любую длину
    free = plan_render(scenes, clip_share=0.2)
    assert any(modes[s.id] != free[s.id] for s in long_ones)
    # рука человека сильнее правила: поставил клип — значит клип
    scenes[0].duration_sec = 10.0
    scenes[0].render = "clip"
    assert plan_render(scenes, clip_share=0.2, max_clip_sec=6)[scenes[0].id] == "clip"


def test_opening_tenth_is_clips_and_the_share_lands_near_the_goal() -> None:
    for seed in range(4):
        scenes = _storyboard(seed=seed)
        total = sum(s.duration_sec for s in scenes)
        modes = plan_render(scenes, clip_share=0.2)
        # первые 10% хронометража — клипами целиком: зритель видит движение сразу
        assert all(modes[s.id] == "clip" for s, start in _timeline(scenes) if start < 0.1 * total)
        # допуск владельца ±2–4 п.п.; план не режется, но на 24 минутах попадание плотное
        assert clip_share_of(scenes, modes) == pytest.approx(0.2, abs=0.02)
        assert "still" in modes.values()


def test_tail_clips_are_spread_not_heaped_where_the_action_is() -> None:
    """Всё действие — в середине ролика. Лучшие по баллу легли бы туда все;
    окна по времени рассыпают клипы по хвосту, действие выигрывает внутри окна."""
    scenes = _storyboard(action_band=(0.45, 0.6))
    total = sum(s.duration_sec for s in scenes)
    modes = plan_render(scenes, clip_share=0.2)
    tail = [(s, start) for s, start in _timeline(scenes) if start >= 0.1 * total]
    tail_len = total - tail[0][1]
    parts = [[0.0, 0.0] for _ in range(5)]
    mids: list[float] = []
    for scene, start in tail:
        mid = start + scene.duration_sec / 2
        k = min(4, int((mid - tail[0][1]) / (tail_len / 5)))
        parts[k][1] += scene.duration_sec
        if modes[scene.id] == "clip":
            parts[k][0] += scene.duration_sec
            mids.append(mid)
    # в хвосте цель ~11% (10 п.п. доли на 90% хронометража): ни одна пятая не пуста
    # и ни одна не забирает клипы себе
    shares = [clip / length for clip, length in parts]
    assert all(0.04 <= s <= 0.25 for s in shares), shares
    spacing = tail_len / len(mids)
    gaps = [b - a for a, b in zip([tail[0][1], *mids], [*mids, total], strict=True)]
    assert max(gaps) <= 3 * spacing, (max(gaps), spacing)
    # действие всё же в предпочтении: в полосе действия клипов больше средней
    band = [s for s, start in tail if 0.45 * total <= start < 0.6 * total]
    band_share = sum(s.duration_sec for s in band if modes[s.id] == "clip") / sum(
        s.duration_sec for s in band
    )
    assert band_share >= 0.1


def _row(n: int, seconds: float = 10.0, **fields: object) -> Scene:
    return Scene(id=f"s{n}", order=n, duration_sec=seconds, **fields)  # type: ignore[arg-type]


def test_action_then_cast_win_inside_a_window() -> None:
    """100 с, доля 20%: первые 10 с — клип, на хвост остаётся один план в одном
    окне; при равной длине выигрывает действие, без действия — герой в кадре."""
    scenes = [_row(1), _row(2), _row(3, cast=["characters/amir"]), _row(4, tempo="action")]
    scenes += [_row(n) for n in range(5, 11)]
    modes = plan_render(scenes, clip_share=0.2)
    assert [s.id for s in scenes if modes[s.id] == "clip"] == ["s1", "s4"]

    scenes[3].tempo = "calm"
    modes = plan_render(scenes, clip_share=0.2)
    assert [s.id for s in scenes if modes[s.id] == "clip"] == ["s1", "s3"]


def test_continuations_of_one_shot_get_one_decision() -> None:
    scenes = _storyboard(seed=3)
    modes = plan_render(scenes, clip_share=0.2)
    chained = [group for group in shots(scenes) if len(group) > 1]
    assert chained
    assert all(len({modes[s.id] for s in group}) == 1 for group in chained)


def test_a_hand_set_field_wins_and_the_shot_follows_it() -> None:
    scenes = [_row(1), _row(2, continues="s1"), *[_row(n) for n in range(3, 11)]]
    scenes[0].render = "still"  # в окне начала, но человек сказал «картинкой»
    scenes[6].render = "clip"  # в хвосте человек сказал «клипом»
    modes = plan_render(scenes, clip_share=0.2)
    assert modes["s1"] == "still" and modes["s2"] == "still"  # продолжение — за головой
    assert modes["s7"] == "clip"
    # доля 20% из 100 с = 20 с: клип человека в счёт, остальное добрано до цели
    assert clip_share_of(scenes, modes) == pytest.approx(0.2, abs=0.1)

    # и при доле 100% поставленное руками — тоже закон
    assert plan_render(scenes, clip_share=1.0)["s1"] == "still"


def test_a_shot_with_a_motion_reference_is_a_clip() -> None:
    scenes = [_row(n) for n in range(1, 11)]
    scenes[8].motion_reference = MotionReference(file="motion/jab.mp4", signal="raw")
    modes = plan_render(scenes, clip_share=0.2)
    assert modes["s9"] == "clip"


def test_written_decision_survives_new_durations() -> None:
    """Озвучка перемеряет сцены — назначение, раз записанное, не перетасовывается:
    иначе сменятся отпечатки, и клипы перегенерируются за деньги."""
    scenes = _storyboard(seed=5)
    first = plan_render(scenes, clip_share=0.2)
    for scene in scenes:
        scene.render = first[scene.id]
    rng = random.Random(1)
    for scene in scenes:
        scene.duration_sec = round(scene.duration_sec * rng.uniform(0.5, 1.8), 1)
    assert plan_render(scenes, clip_share=0.2) == first
    assert plan_render(scenes, clip_share=0.5) == first  # и смена доли тоже


# ---------------------------------------------------------------- отпечаток


def _ctx() -> StageContext:
    ns = SimpleNamespace(
        resources=SimpleNamespace(style=None),
        storyboard=SimpleNamespace(aspect_ratio="9:16", ordered_scenes=lambda: []),
        providers=SimpleNamespace(
            video=SimpleNamespace(chains_camera=False), still=SimpleNamespace(chains_camera=True)
        ),
    )
    return cast(StageContext, ns)


def test_an_old_storyboard_without_the_field_is_not_stale() -> None:
    scene = Scene(id="s1", order=1, motion_prompt="pan left", visual_prompt="a mosque")
    scene.assets.image = "02_images/s1.png"
    scene.inputs_fingerprint["image"] = "abc"
    before_the_field = fingerprint("pan left", "a mosque", "", "9:16", "02_images/s1.png", "abc")
    assert VideoStage._inputs(_ctx(), scene) == before_the_field
    # записанное «клип» — тоже прежний отпечаток: запись решения клипы не старит
    scene.render = "clip"
    assert VideoStage._inputs(_ctx(), scene) == before_the_field
    scene.render = "still"
    assert VideoStage._inputs(_ctx(), scene) != before_the_field


# ---------------------------------------------------------------- стадия


def _tiny_video(path: Path, *, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_run(
        [
            "-f", "lavfi", "-i", f"testsrc=size=96x160:rate=24:duration={seconds:g}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
        ]
    )  # fmt: skip
    return path


class SpyClip(MockVideo):
    """Клип без рендера и с ценой: копия готового файла, $1 за клип."""

    def __init__(self, clip: Path) -> None:
        super().__init__()
        self.clip = clip
        self.calls: list[str] = []

    def price_for(self, duration: int, *, guided: bool = False) -> float:
        return 1.0

    def animate(  # type: ignore[override]
        self, image: Path, prompt: str, *, out_path: Path, **kwargs: object
    ) -> GenResult:
        self.calls.append(out_path.name)
        shutil.copy2(self.clip, out_path)
        return GenResult(out_path, 0.0)


class SpyStill(MockStill):
    def __init__(self, clip: Path) -> None:
        super().__init__()
        self.clip = clip
        self.calls: list[str] = []

    def animate(  # type: ignore[override]
        self,
        image: Path,
        prompt: str,
        *,
        out_path: Path,
        chain: ChainLink | None = None,
        motion_reference: MotionGuide | None = None,
        **kwargs: object,
    ) -> GenResult:
        self.calls.append(out_path.name)
        self.chains.append(chain)
        shutil.copy2(self.clip, out_path)
        return GenResult(out_path, 0.0)


@pytest.fixture
def spies(tmp_path: Path, mock_providers: ProviderSet) -> tuple[SpyClip, SpyStill]:
    # 6 с — длина, которую выберут оба для сцены 6 с: короче стадия сочла бы
    # клип устаревшим и перегнала на втором прогоне
    clip = _tiny_video(tmp_path / "spy.mp4", seconds=6.0)
    video, still = SpyClip(clip), SpyStill(clip)
    mock_providers.video, mock_providers.still = video, still
    return video, still


def _share(projects_root: Path, share: float) -> None:
    cfg = projects_root / "t" / "project.yaml"
    text = cfg.read_text(encoding="utf-8")
    assert "clip_share: 1.0" in text  # поле в шаблоне с комментарием
    cfg.write_text(text.replace("clip_share: 1.0", f"clip_share: {share}"), encoding="utf-8")
    assert load_project("t").video.clip_share == share


OPTS = RunOptions(start="images", stop="video", auto_approve=True, raise_errors=True)


def test_default_project_renders_clips_and_writes_no_field(
    episode: Path, mock_providers: ProviderSet, spies: tuple[SpyClip, SpyStill]
) -> None:
    video, still = spies
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    assert len(video.calls) == 2 and still.calls == []
    assert all(s.render is None for s in load_storyboard(episode).scenes)
    assert "картинками" not in load_manifest(episode).record("video").message


def test_mixed_episode_goes_through_two_providers_once(
    episode: Path,
    mock_providers: ProviderSet,
    spies: tuple[SpyClip, SpyStill],
    projects_root: Path,
) -> None:
    """Две сцены по 6 с, доля 50%: первая — окно начала, клипом; вторая —
    картинкой. Решение записано, клипа по кадрам у картинки нет, прогноз
    считает только клип, повтор ничего не перегоняет."""
    video, still = spies
    _share(projects_root, 0.5)
    run_episode(episode, mock_providers, RunOptions(only="images", raise_errors=True))
    estimate = {r.stage: r.cost_usd for r in estimate_episode(episode, mock_providers)}
    assert estimate["video"] == 1.0  # только клип: картинка-сцена у remotion и заглушки $0

    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    sb = load_storyboard(episode)
    s1, s2 = sb.ordered_scenes()
    assert (s1.render, s2.render) == ("clip", "still")
    assert video.calls == ["s1_v01.mp4"] and still.calls == ["s2_v01.mp4"]
    assert s2.checks.video == "skipped" and "video" not in s2.checks.notes
    assert s2.inputs_fingerprint["video"] != s1.inputs_fingerprint["video"]
    message = load_manifest(episode).record("video").message
    assert "клипами 1 сцен" in message and "картинками 1" in message
    assert "доля клипов по времени 50%" in message

    # повтор: всё на месте, и перемер длительностей решения не трогает
    sb.scenes[0].duration_sec = 5.6
    sb.scenes[1].duration_sec = 6.4
    save_storyboard(episode, sb)
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    assert len(video.calls) == 1 and len(still.calls) == 1
    sb = load_storyboard(episode)
    assert [s.render for s in sb.ordered_scenes()] == ["clip", "still"]

    # человек перевёл картинку в клип — перегоняется ровно она, основным провайдером
    sb.scenes[1].render = "clip"
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, OPTS)
    assert video.calls == ["s1_v01.mp4", "s2_v02.mp4"] and len(still.calls) == 1


def test_a_still_provider_that_is_not_ready_stops_before_any_paid_clip(
    episode: Path,
    mock_providers: ProviderSet,
    spies: tuple[SpyClip, SpyStill],
    projects_root: Path,
) -> None:
    video, still = spies
    still.problem = "Node.js не найден (нужен npx)"
    _share(projects_root, 0.5)
    report = run_episode(episode, mock_providers, OPTS)
    assert not report.ok and report.stopped_at == "video"
    rec = load_manifest(episode).record("video")
    assert rec.status == "failed" and "Node.js" in rec.message and "s2" in rec.message
    assert video.calls == []  # клип s1 стоит раньше по порядку — и не оплачен


def test_a_hand_marked_still_scene_needs_no_project_share(
    episode: Path, mock_providers: ProviderSet, spies: tuple[SpyClip, SpyStill]
) -> None:
    video, still = spies
    sb = load_storyboard(episode)
    sb.scenes[1].render = "still"
    save_storyboard(episode, sb)
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    assert video.calls == ["s1_v01.mp4"] and still.calls == ["s2_v01.mp4"]
    assert load_storyboard(episode).scenes[0].render is None  # доля 100%: не пишется


def test_still_continuations_get_their_place_in_the_chain(
    episode: Path,
    mock_providers: ProviderSet,
    spies: tuple[SpyClip, SpyStill],
    projects_root: Path,
) -> None:
    video, still = spies
    sb = load_storyboard(episode)
    sb.scenes[1].continues = "s1"
    for scene in sb.scenes:
        scene.render = "still"
    save_storyboard(episode, sb)
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    assert video.calls == [] and len(still.chains) == 2
    assert all(link is not None for link in still.chains)
    assert all(s.chained for s in load_storyboard(episode).scenes)


@pytest.mark.render
def test_mixed_episode_is_assembled_end_to_end(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    """Сквозной прогон на заглушках с долей 50%: озвучка ставит длительности,
    video назначает и рендерит двумя провайдерами, сборка склеивает оба вида."""
    _share(projects_root, 0.5)
    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    assert report.ok, report.reason
    sb = load_storyboard(episode)
    s1, s2 = sb.ordered_scenes()
    assert (s1.render, s2.render) == ("clip", "still")
    assert s1.checks.video == "pass" and s2.checks.video == "skipped"
    assert sb.final.video and (episode / sb.final.video).exists()
    assert s2.measured_sec["clip"] == pytest.approx(s2.duration_sec, abs=0.05)
    assert load_manifest(episode).providers["still"] == "mock"


def test_video_needs_the_still_slot(episode: Path) -> None:
    assert required_providers(episode, RunOptions(only="video")) == ["video", "still", "check"]


# ---------------------------------------------------------------- провайдеры и doctor


def test_still_provider_is_its_own_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VF_STILL_PROVIDER", raising=False)
    assert ProviderNames.from_env().still == "remotion"
    monkeypatch.setenv("VF_STILL_PROVIDER", "mock")
    assert ProviderNames.from_env().as_dict()["still"] == "mock"
    providers = build_providers(ProviderNames.mock())
    assert isinstance(providers.still, MockStill) and providers.still is not providers.video
    assert providers.names()["still"] == "mock"


def test_doctor_names_projects_that_need_stills(
    episode: Path, projects_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ProviderNames.mock()
    assert "не нужны" in still_status(names)[0]

    _share(projects_root, 0.2)
    lines = still_status(names)
    assert "t (20% клипами)" in lines[0] and "заглушка mock" in lines[1]

    # remotion без node_modules — честное «не готов», а не «ok»
    empty = tmp_path / "remotion"
    empty.mkdir()
    (empty / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VF_REMOTION_DIR", str(empty))
    lines = still_status(ProviderNames(image="mock", video="mock", tts="mock", llm="mock"))
    assert "НЕ готов" in lines[1]


def test_modes_type_is_the_contract_literal() -> None:
    mode: SceneRender = "still"
    assert Scene(id="s", order=1, render=mode).render == "still"
