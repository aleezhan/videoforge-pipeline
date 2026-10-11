"""Референс движения у сцены (features/motion_reference_scene): скелет или
видео ведут клип вместо промпта.

Что здесь держится: поле пишет человек и читает стадия video; референс — вход
клипа по СОДЕРЖИМОМУ файла, а без поля отпечаток тот же, что раньше (иначе
обновление устарело бы все готовые клипы); отсутствующий файл и провайдер без
адаптера останавливают стадию ДО первого платного вызова; ComfyUI подгоняет
референс под 8n+1 кадров сцены и шлёт граф управления, fal и remotion
отказывают без фолбэка на клип по промпту.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from conftest import count_frames
from PIL import Image
from pydantic import ValidationError

from videoforge.core.ffmpeg import run as ffmpeg_run
from videoforge.core.manifest import load_manifest
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions, StageContext
from videoforge.core.storyboard import (
    ChainLink,
    MotionReference,
    Scene,
    fingerprint,
    load_storyboard,
    save_storyboard,
)
from videoforge.providers import comfy_http as ch
from videoforge.providers.base import GenResult, MotionGuide, ProviderError, ProviderSet
from videoforge.providers.fal_video import FalVideo
from videoforge.providers.mock import MockLlm, MockVideo
from videoforge.providers.remotion_video import RemotionVideo
from videoforge.stages.planner import _PROMPT as PLANNER_PROMPT
from videoforge.stages.video import VideoStage, motion_reference_path


def _tiny_video(path: Path, *, seconds: float = 0.5, fps: int = 30, size: str = "96x160") -> Path:
    """Крошечное видео lavfi: тестам нужен файл, который ffmpeg читает, а не картинка."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_run(
        [
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={seconds:g}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
        ]
    )  # fmt: skip
    return path


# --- контракт: поле сцены


def test_scene_reads_a_reference_and_the_planner_never_writes_one() -> None:
    scene = Scene.model_validate(
        {"id": "s1", "order": 1, "motion_reference": {"file": "motion/jab.mp4", "signal": "raw"}}
    )
    assert scene.motion_reference == MotionReference(file="motion/jab.mp4", signal="raw")
    assert scene.motion_reference.strength is None  # 1.0 на замерах — умолчание провайдера
    assert Scene(id="s2", order=2).motion_reference is None  # старые раскадровки читаются
    with pytest.raises(ValidationError):
        MotionReference(file="x.mp4", signal="flow")  # сигналов ровно четыре, как у графа
    with pytest.raises(ValidationError):
        MotionReference(file="x.mp4", signal="pose", strength=1.5)
    assert set(MotionReference.model_fields["signal"].annotation.__args__) == set(  # type: ignore[union-attr]
        ch.CONTROL_SIGNALS
    )
    # референс — файл, который надо снять или отрендерить, а не слова: планнер о поле не знает
    assert "motion_reference" not in PLANNER_PROMPT


# --- отпечаток входов клипа


def _ctx(episode_dir: Path, project_root: Path | None = None) -> StageContext:
    ns = SimpleNamespace(
        resources=SimpleNamespace(style=None),
        storyboard=SimpleNamespace(aspect_ratio="9:16"),
        providers=SimpleNamespace(video=SimpleNamespace(chains_camera=False)),
        project_dir=episode_dir,
        project_root=project_root,
    )
    return cast(StageContext, ns)


def _scene() -> Scene:
    scene = Scene(id="s1", order=1, motion_prompt="jab", visual_prompt="cat")
    scene.assets.image = "02_images/s1.png"
    scene.inputs_fingerprint["image"] = "abc"
    return scene


def test_clip_fingerprint_changes_only_when_a_reference_is_set(tmp_path: Path) -> None:
    """Без поля — тот же отпечаток, что до его появления: обновление не должно
    устаревать готовые клипы. С полем — сигнал, сила и файл входят во входы."""
    scene = _scene()
    plain = fingerprint("jab", "cat", "", "9:16", "02_images/s1.png", "abc")
    assert VideoStage._inputs(_ctx(tmp_path), scene) == plain
    _tiny_video(tmp_path / "jab.mp4")
    scene.motion_reference = MotionReference(file="jab.mp4", signal="raw")
    raw = VideoStage._inputs(_ctx(tmp_path), scene)
    scene.motion_reference = MotionReference(file="jab.mp4", signal="pose")
    pose = VideoStage._inputs(_ctx(tmp_path), scene)
    scene.motion_reference = MotionReference(file="jab.mp4", signal="raw", strength=0.7)
    soft = VideoStage._inputs(_ctx(tmp_path), scene)
    assert len({plain, raw, pose, soft}) == 4
    # сила 1.0 явно и None — одно и то же: провайдер получает 1.0 в обоих случаях
    scene.motion_reference = MotionReference(file="jab.mp4", signal="raw", strength=1.0)
    assert VideoStage._inputs(_ctx(tmp_path), scene) == raw


def test_clip_fingerprint_follows_the_file_content_not_its_path_or_mtime(tmp_path: Path) -> None:
    """Файл переезжает из inbox в проект, копируется, выкачивается из git —
    путь и время меняются, движение нет, и перегенерация за деньги не за что.
    Другое содержимое — другой клип. Пропавший файл отпечаток не роняет:
    его считают и прогноз, и пульт, а откажет стадия."""
    episode, project = tmp_path / "e", tmp_path / "p"
    ref = _tiny_video(episode / "jab.mp4")
    scene = _scene()
    scene.motion_reference = MotionReference(file="jab.mp4", signal="raw")
    before = VideoStage._inputs(_ctx(episode, project), scene)

    moved = project / "motion" / "jab_v2.mp4"
    moved.parent.mkdir(parents=True)
    shutil.copy2(ref, moved)
    ref.unlink()
    scene.motion_reference = MotionReference(file="motion/jab_v2.mp4", signal="raw")
    assert motion_reference_path(_ctx(episode, project), scene) == moved  # нашли в проекте
    assert VideoStage._inputs(_ctx(episode, project), scene) == before

    _tiny_video(moved, seconds=1.0)  # тот же путь, другое движение
    assert VideoStage._inputs(_ctx(episode, project), scene) != before

    scene.motion_reference = MotionReference(file="motion/none.mp4", signal="raw")
    assert motion_reference_path(_ctx(episode, project), scene) is None
    assert VideoStage._inputs(_ctx(episode, project), scene) not in (before,)


# --- стадия video на заглушке


class SpyVideo(MockVideo):
    """Заглушка без рендера: клип — копия готового файла, чтобы тест стадии
    шёл миллисекунды; что получила, пишет как MockVideo (`references`)."""

    def __init__(self, clip: Path, *, follows: bool = True) -> None:
        super().__init__()
        self.clip = clip
        self.follows_reference = follows
        self.calls = 0

    def animate(
        self,
        image: Path,
        prompt: str,
        *,
        duration_sec: int,
        aspect_ratio: str,
        out_path: Path,
        chain: ChainLink | None = None,
        motion_reference: MotionGuide | None = None,
    ) -> GenResult:
        self.calls += 1
        self.references.append(motion_reference)
        shutil.copy2(self.clip, out_path)
        return GenResult(out_path, 0.0, {"prompt": prompt})


@pytest.fixture
def spy(tmp_path: Path, mock_providers: ProviderSet) -> SpyVideo:
    # 6 с — длина, которую `pick_duration` выберет для сцены: короче клип
    # стадия сочла бы устаревшим и перегнала на втором прогоне
    clip = _tiny_video(tmp_path / "spy_clip.mp4", seconds=6.0, fps=24)
    provider = SpyVideo(clip)
    mock_providers.video = provider
    return provider


OPTS = RunOptions(start="images", stop="video", auto_approve=True, raise_errors=True)


def test_video_stage_hands_the_reference_to_the_provider_and_warns_about_the_first_pose(
    episode: Path, mock_providers: ProviderSet, projects_root: Path, spy: SpyVideo
) -> None:
    ref = _tiny_video(projects_root / "t" / "motion" / "jab.mp4")  # библиотека проекта
    sb = load_storyboard(episode)
    sb.scenes[1].motion_reference = MotionReference(file="motion/jab.mp4", signal="raw")
    save_storyboard(episode, sb)

    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    assert spy.references == [None, MotionGuide(path=ref, signal="raw", strength=1.0)]
    message = load_manifest(episode).record("video").message
    assert "по референсу движения: 1 сцен ['s2']" in message
    assert "первой позе референса" in message and "mocap_follow_8s" in message

    # идемпотентность: без правок ничего не перегоняется
    run_episode(episode, mock_providers, OPTS)
    assert spy.calls == 2
    # другое движение в том же файле — устарел только клип с референсом
    _tiny_video(ref, seconds=1.0)
    run_episode(episode, mock_providers, OPTS)
    assert spy.calls == 3 and spy.references[-1] is not None


def test_the_first_pose_reminder_survives_a_failed_clip_check(
    episode: Path, mock_providers: ProviderSet, projects_root: Path, spy: SpyVideo
) -> None:
    """Прогон t07_lora_motion_ref (16.09.2026): проверка клипа не прошла, стадия
    вышла с провалом раньше, и «ПРОВЕРЬ: первая поза» пропало — а при провале
    оно нужнее всего."""
    _tiny_video(projects_root / "t" / "motion" / "jab.mp4")
    sb = load_storyboard(episode)
    sb.scenes = sb.scenes[:1]
    sb.scenes[0].cast = ["characters/timur"]  # чеклист непустой → клип проверяется
    sb.scenes[0].motion_reference = MotionReference(file="motion/jab.mp4", signal="raw")
    save_storyboard(episode, sb)
    llm = MockLlm()
    llm.check_issues = [[], ["призрак в первые полсекунды"]]  # кадр проходит, клип нет
    mock_providers.llm = llm

    report = run_episode(
        episode,
        mock_providers,
        RunOptions(start="images", stop="video", auto_approve=True, max_attempts=1),
    )
    assert not report.ok and report.stopped_at == "video"
    rec = load_manifest(episode).record("video")
    assert rec.status == "failed" and "не прошли проверку" in rec.message
    assert "по референсу движения: 1 сцен ['s1']" in rec.message
    assert "первой позе референса" in rec.message and "mocap_follow_8s" in rec.message


def test_a_missing_reference_fails_before_any_paid_call(
    episode: Path, mock_providers: ProviderSet, spy: SpyVideo
) -> None:
    sb = load_storyboard(episode)
    sb.scenes[0].motion_reference = MotionReference(file="motion/nope.mp4", signal="raw")
    save_storyboard(episode, sb)

    report = run_episode(episode, mock_providers, OPTS)
    assert not report.ok and report.stopped_at == "video"
    rec = load_manifest(episode).record("video")
    assert rec.status == "failed" and "s1 (motion/nope.mp4)" in rec.message
    assert spy.calls == 0  # ни одной сцены не оплатили, включая s2 без референса
    sb = load_storyboard(episode)
    assert all(s.attempts.get("video", 0) == 0 for s in sb.scenes)


def test_a_provider_without_control_stops_the_stage_before_spending(
    episode: Path, mock_providers: ProviderSet, projects_root: Path, spy: SpyVideo
) -> None:
    spy.follows_reference = False
    _tiny_video(projects_root / "t" / "motion" / "jab.mp4")
    sb = load_storyboard(episode)
    sb.scenes[1].motion_reference = MotionReference(file="motion/jab.mp4", signal="raw")
    save_storyboard(episode, sb)

    report = run_episode(episode, mock_providers, OPTS)
    assert not report.ok
    rec = load_manifest(episode).record("video")
    assert rec.status == "failed" and "['s2']" in rec.message and "mock" in rec.message
    assert spy.calls == 0


# --- провайдеры


def test_mock_accepts_a_reference_and_says_what_it_got(tmp_path: Path) -> None:
    img = tmp_path / "a.png"
    Image.new("RGB", (64, 64), (20, 90, 160)).save(img)
    guide = MotionGuide(tmp_path / "jab.mp4", "pose", 0.8)
    v = MockVideo()
    res = v.animate(
        img, "p", duration_sec=1, aspect_ratio="9:16", out_path=tmp_path / "o.mp4",
        motion_reference=guide,
    )  # fmt: skip
    assert v.follows_reference and v.references == [guide]
    assert res.meta["motion_reference"] == str(guide.path) and res.meta["signal"] == "pose"
    assert (tmp_path / "o.mp4").exists()


def test_hosted_and_programmatic_providers_refuse_a_reference_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAL_KEY", "x")  # отказ должен быть из-за референса, а не ключа
    guide = MotionGuide(tmp_path / "jab.mp4", "raw")
    for provider in (FalVideo("hailuo-02-fast"), RemotionVideo(tmp_path)):
        assert not provider.follows_reference
        with pytest.raises(ProviderError, match="референс движения не поддерживается"):
            provider.animate(
                tmp_path / "a.png", "p", duration_sec=6, aspect_ratio="9:16",
                out_path=tmp_path / "o.mp4", motion_reference=guide,
            )  # fmt: skip
        assert not (tmp_path / "o.mp4").exists()


def test_control_plan_builds_the_union_control_graph_for_the_scene_length() -> None:
    preset = ch.VIDEO_PRESETS["ltx25"]

    def plan(**kw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        args: dict[str, Any] = dict(
            aspect_ratio="9:16", duration_sec=6, seed=7, prefix="videoforge/s2_v01", signal="raw"
        )
        return ch.control_plan(preset, "A01.png", "jab.mp4", "a white cat", **{**args, **kw})

    g, meta = plan(strength=0.8)
    assert meta["frames"] == 145 == ch.frames_for(6, 24) and meta["fps"] == 24
    assert g["lat1"]["inputs"] == {"width": 384, "height": 704, "length": 145, "batch_size": 1}
    assert g["aud"]["inputs"]["frames_number"] == 145
    assert g["ic"]["inputs"]["lora_name"] == ch.IC_LORA_UNION == meta["ic_lora"]
    assert g["img"]["inputs"]["image"] == "A01.png" and g["ref"]["inputs"]["file"] == "jab.mp4"
    assert g["guide"]["inputs"]["strength"] == 0.8 and g["guide"]["inputs"]["image"] == ["ctrl", 0]
    assert g["ctrl"]["inputs"]["image"] == ["refs", 0] and "ann" not in g  # raw: файл и есть сигнал
    assert "up" in g and g["dec"]["inputs"]["samples"] == ["sep2", 0]  # рецепт двухпроходный
    assert meta["size"] == "384x704→×2"

    g2, _ = plan(signal="pose")  # видео человека: скелет снимает аннотатор
    assert g2["ann"]["class_type"] == "DWPreprocessor"
    assert g2["ctrl"]["inputs"]["image"] == ["ann", 0]

    g3, meta3 = plan(aspect_ratio="16:9", duration_sec=4)
    assert meta3["frames"] == 97 and g3["lat1"]["inputs"]["width"] == 704

    with pytest.raises(ProviderError, match="LTX-2.5"):
        ch.control_plan(
            ch.VIDEO_PRESETS["wan480"], "A01.png", "jab.mp4", "p",
            aspect_ratio="9:16", duration_sec=4, seed=1, prefix="x", signal="raw",
        )  # fmt: skip


def test_conform_reference_trims_or_holds_the_last_frame(tmp_path: Path) -> None:
    """Скелет из замера — 193 кадра, видео с телефона — 30 fps: под сцену
    режется с конца или дотягивается последним кадром, первый кадр не трогается."""
    long = _tiny_video(tmp_path / "long.mp4", seconds=8.0, fps=24)
    assert count_frames(ch.conform_reference(long, tmp_path / "long.ref.mp4", frames=73)) == 73
    short = _tiny_video(tmp_path / "short.mp4", seconds=0.5, fps=30)
    assert count_frames(ch.conform_reference(short, tmp_path / "short.ref.mp4", frames=73)) == 73
    with pytest.raises(ProviderError, match=r"8n\+1"):
        ch.conform_reference(short, tmp_path / "x.mp4", frames=72)


class FakeComfy:
    """Достаточно ComfyUI для пути целиком; загрузки называются своими именами —
    так видно, что на под ушёл подогнанный референс, а не исходный."""

    def __init__(self) -> None:
        self.graphs: list[dict[str, Any]] = []
        self.uploads: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/upload/image":
            m = re.search(rb'filename="([^"]+)"', request.content)
            assert m
            name = m.group(1).decode()
            self.uploads.append(name)
            return httpx.Response(200, json={"name": name, "subfolder": "", "type": "input"})
        if path == "/prompt":
            self.graphs.append(json.loads(request.content)["prompt"])
            return httpx.Response(200, json={"prompt_id": "p1", "number": 1, "node_errors": {}})
        if path.startswith("/history/"):
            out = {"save": {"images": [{"filename": "x.mp4", "subfolder": "", "type": "output"}]}}
            status = {"status_str": "success", "completed": True}
            return httpx.Response(200, json={"p1": {"status": status, "outputs": out}})
        if path == "/view":
            return httpx.Response(200, content=b"fake mp4")
        if path == "/free":
            return httpx.Response(200)
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeComfy:
    f = FakeComfy()
    transport = httpx.MockTransport(f.handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        ch.httpx, "Client", lambda *a, **k: real_client(*a, **{**k, "transport": transport})
    )
    return f


def test_comfy_conforms_the_reference_next_to_the_clip_and_sends_the_control_graph(
    fake: FakeComfy, tmp_path: Path
) -> None:
    img = tmp_path / "s2.png"
    img.write_bytes(b"\x89PNG")
    ref = _tiny_video(tmp_path / "motion" / "jab.mp4", seconds=2.0, fps=30)  # 60 кадров, 30 fps
    v = ch.ComfyVideo(ch.PodState(base_url="http://comfy", video="ltx25"))
    out = tmp_path / "03_video" / "s2_v01.mp4"
    out.parent.mkdir()
    res = v.animate(
        img, "a white cat", duration_sec=6, aspect_ratio="9:16", out_path=out,
        motion_reference=MotionGuide(ref, "raw", 0.9),
    )  # fmt: skip
    conformed = tmp_path / "03_video" / "s2_v01.ref.mp4"
    assert conformed.exists() and count_frames(conformed) == 145  # 8n+1 под 6 с при 24 fps
    assert fake.uploads == ["s2.png", "s2_v01.ref.mp4"]  # ушёл подогнанный, не исходный
    g = fake.graphs[-1]
    assert g["ref"]["inputs"]["file"] == "s2_v01.ref.mp4"
    assert g["ic"]["inputs"]["lora_name"] == ch.IC_LORA_UNION
    assert g["lat1"]["inputs"]["length"] == 145 and g["guide"]["inputs"]["strength"] == 0.9
    assert res.meta["frames"] == 145 and res.meta["signal"] == "raw"
    assert res.meta["reference_source"] == str(ref) and out.read_bytes() == b"fake mp4"
    # прогноз клипа по референсу — дороже обычного (аннотатор и управляющие токены)
    assert v.price_for(6, guided=True) == pytest.approx(v.price_for(6) * ch.CONTROL_COST_FACTOR)

    # без референса — обычный i2v, граф без адаптера
    v.animate(img, "walks", duration_sec=6, aspect_ratio="9:16", out_path=out.with_name("s3.mp4"))
    assert "ic" not in fake.graphs[-1] and "ref" not in fake.graphs[-1]

    # пресет Wan референс не умеет: отказ до подгонки и до единого вызова пода
    w = ch.ComfyVideo(ch.PodState(base_url="http://comfy", video="wan480"))
    calls = len(fake.uploads)
    with pytest.raises(ProviderError, match="LTX-2.5"):
        w.animate(
            img, "p", duration_sec=4, aspect_ratio="9:16", out_path=out.with_name("s4_v01.mp4"),
            motion_reference=MotionGuide(ref, "raw"),
        )  # fmt: skip
    assert len(fake.uploads) == calls and not out.with_name("s4_v01.ref.mp4").exists()


# --- пульт


def test_scene_row_shows_the_reference(tmp_path: Path) -> None:
    from videoforge.api.data import _scene_row

    scene = Scene(id="s1", order=1)
    assert _scene_row(tmp_path, scene)["motion_reference"] is None
    scene.motion_reference = MotionReference(file="motion/jab.mp4", signal="raw", strength=0.8)
    assert _scene_row(tmp_path, scene)["motion_reference"] == {
        "file": "motion/jab.mp4",
        "signal": "raw",
        "strength": 0.8,
    }
