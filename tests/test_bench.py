"""Замеры по схеме: bench.yaml → дубли с полным происхождением → вердикт → сводка."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from videoforge.api.app import create_app
from videoforge.bench import spec as bs
from videoforge.bench.run import MockMethod, TakePlan, error_kind, run_bench
from videoforge.bench.summary import render_markdown, summarize, write_summary
from videoforge.providers.base import ProviderError


@pytest.fixture
def bench_root(projects_root: Path) -> Path:
    root = projects_root / "_bench"
    root.mkdir()
    return root


def make_bench(
    root: Path,
    bench_id: str = "b1",
    *,
    branches: list[dict[str, object]] | None = None,
    seeds: tuple[int, ...] = (11,),
    budget: float = 1.0,
    frame: bool = True,
    compare: str = "method",
) -> Path:
    folder = root / bench_id
    (folder / "inputs").mkdir(parents=True)
    if frame:
        Image.new("RGB", (72, 128), (200, 120, 40)).save(folder / "inputs" / "s1.png")
    spec = {
        "id": bench_id,
        "question": "кто держит морду",
        "compare": compare,
        "budget_usd": budget,
        "seeds": list(seeds),
        "scenes": [
            {"id": "S1", "title": "поворот", "frame": "inputs/s1.png", "prompt": "cat turns head"}
        ],
        "branches": branches or [{"id": "a", "method": "mock"}, {"id": "b", "method": "mock"}],
        "criteria": [
            {"id": "identity", "title": "тот же кот"},
            {"id": "anatomy", "title": "целое тело"},
        ],
    }
    (folder / "bench.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    return folder


def _done_take(
    folder: Path, scene: str, branch: str, seed: int, cost: float = 0.02
) -> bs.TakeRecord:
    tid = bs.take_id(scene, branch, seed)
    rec = bs.TakeRecord(
        take_id=tid,
        scene=scene,
        branch=branch,
        seed=seed,
        status="done",
        inputs=bs.TakeInputs(
            frame="inputs/s1.png", prompt="cat turns head", aspect_ratio="9:16", duration_sec=5
        ),
        method=bs.TakeMethod(method="mock", preset="ltx25"),
        fingerprint="abc",
        cost_usd=cost,
        gen_sec=40.0,
    )
    bs.write_take(bs.take_paths(folder, tid).record, rec)
    return rec


# ---------------------------------------------------------------- схема


def test_spec_loads_and_validates(bench_root: Path) -> None:
    folder = make_bench(bench_root)
    spec = bs.load_spec(folder)
    assert spec.id == "b1" and [b.id for b in spec.branches] == ["a", "b"]
    assert bs.missing_frames(folder, spec) == {}

    (folder / "inputs" / "s1.png").unlink()
    assert bs.missing_frames(folder, spec) == {"S1": "inputs/s1.png"}

    # одна причина при разных моделях — не сравнение причины, схема это называет вслух
    bad = make_bench(
        bench_root,
        "b2",
        branches=[
            {"id": "x", "method": "comfy_i2v", "preset": "ltx25"},
            {"id": "y", "method": "comfy_i2v", "preset": "wan480"},
        ],
        compare="cause",
    )
    with pytest.raises(bs.BenchError, match="compare: method"):
        bs.load_spec(bad)

    dup = make_bench(bench_root, "b3", branches=[{"id": "a", "method": "mock"}, {"id": "a"}])
    with pytest.raises(bs.BenchError, match="повтор id"):
        bs.load_spec(dup)

    assert bs.list_benches() == sorted([folder, bad, dup])


def test_plan_fingerprint_is_stable_and_seed_sensitive(bench_root: Path) -> None:
    folder = make_bench(bench_root)
    spec = bs.load_spec(folder)
    m = MockMethod()
    frame = folder / "inputs" / "s1.png"
    p1 = m.plan(spec, spec.scenes[0], spec.branches[0], 11, frame=frame)
    p2 = m.plan(spec, spec.scenes[0], spec.branches[0], 11, frame=frame)
    p3 = m.plan(spec, spec.scenes[0], spec.branches[0], 42, frame=frame)
    f = bs.fingerprint
    assert f(p1.inputs, p1.method, p1.graph) == f(p2.inputs, p2.method, p2.graph)
    assert f(p1.inputs, p1.method, p1.graph) != f(p3.inputs, p3.method, p3.graph)
    assert p1.inputs.frame_sha256 == bs.sha256_file(frame) and p1.inputs.prompt == "cat turns head"


def test_branch_can_set_its_own_clip_length(bench_root: Path) -> None:
    """Длина клипа — переменная ветки: короткий клип против длинного при том же
    промпте. Пресет, который такой длины не умеет, останавливает план до вызова."""
    from videoforge.bench.run import ComfyI2V

    folder = make_bench(
        bench_root,
        branches=[
            {"id": "long", "method": "comfy_i2v", "preset": "ltx25"},
            {
                "id": "short",
                "method": "comfy_i2v",
                "preset": "ltx25",
                "params": {"duration_sec": 3},
            },
            {"id": "bad", "method": "comfy_i2v", "preset": "wan480", "params": {"duration_sec": 9}},
        ],  # fmt: skip
    )
    spec = bs.load_spec(folder)
    frame = folder / "inputs" / "s1.png"
    m = ComfyI2V()
    long = m.plan(spec, spec.scenes[0], spec.branches[0], 11, frame=frame)
    short = m.plan(spec, spec.scenes[0], spec.branches[1], 11, frame=frame)
    assert (long.inputs.duration_sec, long.inputs.frames) == (5, 121)
    assert (short.inputs.duration_sec, short.inputs.frames) == (3, 73)
    assert short.estimate_usd < long.estimate_usd
    assert short.graph["lat1"]["inputs"]["length"] == 73  # граф собран на ту же длину
    with pytest.raises(bs.BenchError, match="не умеет 9 с"):
        m.plan(spec, spec.scenes[0], spec.branches[2], 11, frame=frame)


def test_frame_rate_cond_changes_model_condition_not_output_fps(bench_root: Path) -> None:
    """Условие темпа — переменная ветки: в граф LTX идёт 16 в LTXVConditioning и
    в аудио-латент, а выходной файл остаётся на 24 fps; у Wan такого рычага нет."""
    from videoforge.bench.run import ComfyI2V
    from videoforge.providers.base import ProviderError

    folder = make_bench(
        bench_root,
        branches=[
            {
                "id": "fr16",
                "method": "comfy_i2v",
                "preset": "ltx25",
                "params": {"frame_rate_cond": 16},
            },
            {"id": "s2", "method": "comfy_i2v", "preset": "ltx25", "params": {"duration_sec": 2}},
            {
                "id": "wan",
                "method": "comfy_i2v",
                "preset": "wan480",
                "params": {"frame_rate_cond": 16},
            },
        ],  # fmt: skip
    )
    spec = bs.load_spec(folder)
    frame = folder / "inputs" / "s1.png"
    m = ComfyI2V()
    fr16 = m.plan(spec, spec.scenes[0], spec.branches[0], 11, frame=frame)
    assert fr16.graph["cond"]["inputs"]["frame_rate"] == 16.0
    assert fr16.graph["aud"]["inputs"]["frame_rate"] == 16.0
    assert fr16.graph["vid"]["inputs"]["fps"] == 24.0 and fr16.inputs.fps == 24
    two = m.plan(spec, spec.scenes[0], spec.branches[1], 11, frame=frame)
    assert (two.inputs.duration_sec, two.inputs.frames) == (2, 49)
    with pytest.raises(ProviderError, match="только у LTX"):
        m.plan(spec, spec.scenes[0], spec.branches[2], 11, frame=frame)


def test_control_plan_wires_reference_into_ic_lora_guide(bench_root: Path) -> None:
    """comfy_control: референсное видео → аннотатор → LTXAddVideoICLoRAGuide поверх
    стартового кадра, после первого прохода — LTXVCropGuides; кадров 8n+1,
    референс в записи дубля с отпечатком."""
    from videoforge.bench.run import ComfyControl

    folder = make_bench(
        bench_root,
        branches=[
            {
                "id": "pose",
                "method": "comfy_control",
                "preset": "ltx25",
                "params": {"reference": "inputs/ref.mp4", "signal": "pose", "ic_strength": 0.6},
            },
            {
                "id": "raw",
                "method": "comfy_control",
                "preset": "ltx25",
                "params": {"reference": "inputs/ref.mp4", "signal": "raw", "two_stage": True},
            },
            {"id": "none", "method": "comfy_control", "preset": "ltx25"},
        ],  # fmt: skip
    )
    (folder / "inputs" / "ref.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    spec = bs.load_spec(folder)
    frame = folder / "inputs" / "s1.png"
    m = ComfyControl()
    pose = m.plan(spec, spec.scenes[0], spec.branches[0], 11, frame=frame)
    g = pose.graph
    assert g["ann"]["class_type"] == "DWPreprocessor" and g["ctrl"]["inputs"]["image"] == ["ann", 0]
    assert g["guide"]["inputs"]["strength"] == 0.6 and g["guide"]["inputs"]["latent"] == ["i2v1", 0]
    assert g["guide"]["inputs"]["latent_downscale_factor"] == ["ic", 1]
    assert g["crop"]["inputs"]["latent"] == ["sep1", 0] and g["dec"]["inputs"]["samples"] == [
        "crop",
        2,
    ]
    assert g["ref"]["inputs"]["file"] == "ref.mp4" and g["lat1"]["inputs"]["length"] == 121
    assert (pose.inputs.size, pose.inputs.frames) == ("576x1024", 121)
    assert pose.inputs.reference == "inputs/ref.mp4" and len(pose.inputs.reference_sha256) == 64
    assert pose.method.models["ic_lora"].endswith("union-control-ref0.5.safetensors")

    raw = m.plan(spec, spec.scenes[0], spec.branches[1], 11, frame=frame)
    assert "ann" not in raw.graph and raw.graph["ctrl"]["inputs"]["image"] == ["refs", 0]
    assert raw.graph["dec"]["inputs"]["samples"] == ["sep2", 0]  # два прохода: апскейл и досэмпл
    assert raw.graph["imgs2"]["inputs"]["width"] == 1152

    with pytest.raises(bs.BenchError, match="params.reference"):
        m.plan(spec, spec.scenes[0], spec.branches[2], 11, frame=frame)


def test_error_kind_names_the_cause() -> None:
    assert error_kind(ProviderError("ComfyUI отверг граф: Value not in list")) == "graph"
    assert error_kind(ProviderError("CUDA error: out of memory")) == "oom"
    assert error_kind(ProviderError("ComfyUI не поднят: запусти gpu up")) == "network"
    assert error_kind(ProviderError("что-то странное")) == "unknown"


# ---------------------------------------------------------------- прогон


@pytest.mark.render
def test_run_keeps_sources_stops_dead_branch_and_is_idempotent(bench_root: Path) -> None:
    folder = make_bench(
        bench_root,
        branches=[
            {"id": "a", "method": "mock"},
            {"id": "b", "method": "mock", "params": {"fail": True}},
        ],
        seeds=(11, 42),
    )
    said: list[str] = []
    report = run_bench(folder, say=said.append)
    assert report.done == ["S1_a_s11", "S1_a_s42"]
    assert report.failed == ["S1_b_s11"]  # второй сид упавшей ветки не жгётся
    assert not bs.take_paths(folder, "S1_b_s42").record.exists()

    paths = bs.take_paths(folder, "S1_a_s11")
    assert paths.video.is_file() and paths.graph.is_file()
    rec = bs.read_take(paths.record)
    assert rec is not None and rec.status == "done"
    # происхождение: кадр с отпечатком, точный промпт, метод, граф как ушёл
    assert rec.inputs.prompt == "cat turns head" and len(rec.inputs.frame_sha256) == 64
    assert rec.method.method == "mock" and rec.graph == "takes/S1_a_s11.graph.json"
    assert json.loads(paths.graph.read_text(encoding="utf-8"))["mock"]["seed"] == 11
    assert rec.metrics is not None and rec.metrics.duration_sec > 0
    failed = bs.read_take(bs.take_paths(folder, "S1_b_s11").record)
    assert failed is not None and failed.status == "failed" and failed.error_kind == "graph"

    again = run_bench(folder, say=said.append)
    assert again.done == [] and again.skipped == ["S1_a_s11", "S1_a_s42"]
    forced = run_bench(folder, branches=["a"], seeds=[11], force=True, say=said.append)
    assert forced.done == ["S1_a_s11"]

    path, summary = write_summary(folder)
    # упавший дубль при повторе перезаписывает СВОЮ запись, второго файла не появляется
    assert path.is_file() and summary["done"] == 2 and summary["failed"] == 1
    assert summary["branches"][1]["error_kinds"] == ["graph"]


def test_budget_stops_before_spending(bench_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = make_bench(bench_root, budget=0.5, branches=[{"id": "a", "method": "mock"}])
    original = MockMethod.plan

    def pricey(self: MockMethod, *args: object, **kwargs: object) -> TakePlan:
        plan = original(self, *args, **kwargs)  # type: ignore[arg-type]
        plan.estimate_usd = 0.6
        return plan

    monkeypatch.setattr(MockMethod, "plan", pricey)
    report = run_bench(folder)
    assert report.done == [] and "бюджет $0.50" in report.stopped
    assert not bs.take_paths(folder, "S1_a_s11").record.exists()


def test_missing_frame_skips_scene(bench_root: Path) -> None:
    folder = make_bench(bench_root, frame=False)
    said: list[str] = []
    report = run_bench(folder, say=said.append)
    assert report.done == [] and any("кадра inputs/s1.png нет" in s for s in said)


def test_image_bench_takes_refs_from_shared_material(bench_root: Path, projects_root: Path) -> None:
    """Замер кадров: панель и лист героя лежат в общей папке материала рядом
    (путь из замера наружу), результат — png, референсы с отпечатками в записи,
    пульт отдаёт кадр и референсы."""
    material = bench_root / "anime_material"
    (material / "refs").mkdir(parents=True)
    Image.new("RGB", (96, 64), (20, 20, 20)).save(material / "panel.png")
    Image.new("RGB", (64, 64), (200, 40, 160)).save(material / "refs" / "hero_a.png")
    folder = make_bench(bench_root, "kf", branches=[{"id": "a", "method": "mock"}], frame=False)
    spec = yaml.safe_load((folder / "bench.yaml").read_text(encoding="utf-8"))
    spec["output"] = "image"
    spec["scenes"][0].update(
        {"frame": "../anime_material/panel.png", "refs": ["../anime_material/refs/hero_a.png"]}
    )
    (folder / "bench.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")

    report = run_bench(folder)
    assert report.done == ["S1_a_s11"]
    paths = bs.take_paths(folder, "S1_a_s11")
    assert paths.image.is_file() and not paths.video.exists()
    rec = bs.read_take(paths.record)
    assert rec is not None and rec.image == "takes/S1_a_s11.png" and rec.video == ""
    assert rec.inputs.refs == ["../anime_material/refs/hero_a.png"]
    assert len(rec.inputs.refs_sha256[0]) == 64 and rec.inputs.duration_sec == 0
    assert rec.metrics is None  # движение у кадра не меряется
    assert run_bench(folder).skipped == ["S1_a_s11"]

    # пропал референс — сцену не запускать, как без кадра
    (material / "refs" / "hero_a.png").unlink()
    assert bs.missing_frames(folder, bs.load_spec(folder)) == {
        "S1": "../anime_material/refs/hero_a.png"
    }
    Image.new("RGB", (64, 64), (200, 40, 160)).save(material / "refs" / "hero_a.png")

    client = TestClient(create_app())
    take = client.get("/api/bench/kf").json()["takes"][0]
    assert take["image_url"].startswith("/files/projects/_bench/kf/takes/S1_a_s11.png")
    assert take["video_url"] is None
    assert take["ref_urls"][0].startswith("/files/projects/_bench/anime_material/refs/hero_a.png")


def _anime_bench(
    bench_root: Path, bench_id: str, output: str, branches: list[dict[str, object]]
) -> Path:
    """Два героя с одинаковыми именами листов в общей папке материала."""
    material = bench_root / "anime_material"
    for hero in ("hero_a", "hero_b"):
        (material / "refs" / hero).mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 96), (200, 40, 160)).save(
            material / "refs" / hero / "full_body_turnaround.png"
        )
    (material / "panels").mkdir(exist_ok=True)
    Image.new("RGB", (96, 64), (20, 20, 20)).save(material / "panels" / "p5_arena.png")
    folder = make_bench(bench_root, bench_id, branches=branches, frame=False)
    spec = yaml.safe_load((folder / "bench.yaml").read_text(encoding="utf-8"))
    spec.update({"output": output, "aspect_ratio": "16:9"})
    spec["scenes"][0].update(
        {
            "frame": "../anime_material/panels/p5_arena.png",
            "refs": [
                "../anime_material/refs/hero_a/full_body_turnaround.png",
                "../anime_material/refs/hero_b/full_body_turnaround.png",
            ],
            "prompts": {"qwen": "Turn this manga panel (image 1) into an anime screenshot"},
        }
    )
    (folder / "bench.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    return folder


def test_keyframe_plans_send_panel_and_both_heroes_under_distinct_names(bench_root: Path) -> None:
    """Панель → кадр аниме: klein получает панель и листы через ReferenceLatent,
    Qwen-Image-Edit — тремя картинками в TextEncodeQwenImageEditPlus с пустым
    латентом 16:9; листы двух героев зовутся одинаково, а на под уходят под
    разными именами. Кадровый метод на замере клипов — ошибка схемы."""
    from videoforge.bench.run import ComfyKeyframe

    folder = _anime_bench(
        bench_root,
        "kf",
        "image",
        [
            {"id": "klein", "method": "comfy_keyframe", "preset": "klein"},
            {"id": "qwen", "method": "comfy_keyframe", "preset": "qwen_edit"},
        ],
    )
    spec = bs.load_spec(folder)
    scene = spec.scenes[0]
    frame = folder / scene.frame
    m = ComfyKeyframe()
    klein = m.plan(spec, scene, spec.branches[0], 11, frame=frame)
    names = [n for _, n in klein.uploads]
    assert names == [
        "panels_p5_arena.png",
        "hero_a_full_body_turnaround.png",
        "hero_b_full_body_turnaround.png",
    ]
    assert [klein.graph[f"ref_img{i}"]["inputs"]["image"] for i in (1, 2, 3)] == names
    assert klein.graph["guider"]["inputs"]["positive"] == ["pos_ref3", 0]
    assert klein.graph["latent"]["inputs"]["width"] == 1280 and klein.inputs.size == "1280x720"
    assert len(klein.inputs.refs_sha256) == 2 and klein.inputs.duration_sec == 0

    qwen = m.plan(spec, scene, spec.branches[1], 11, frame=frame)
    pos = qwen.graph["pos"]["inputs"]
    assert pos["prompt"].startswith("Turn this manga panel")  # у ветки своя инструкция
    assert pos["image1"] == ["img1s", 0] and pos["image3"] == ["img3", 0]
    assert qwen.graph["img2"]["inputs"]["image"] == "hero_a_full_body_turnaround.png"
    assert qwen.graph["lat"]["inputs"] == {"width": 1344, "height": 768, "batch_size": 1}
    assert qwen.graph["ks"]["inputs"]["steps"] == 4 and "lora" in qwen.method.models
    assert qwen.graph["neg_m"]["inputs"]["reference_latents_method"] == "index_timestep_zero"

    dev = m.plan(
        spec, scene, spec.branches[0].model_copy(update={"preset": "flux2_dev"}), 11, frame=frame
    )
    assert dev.graph["pos"]["class_type"] == "FluxGuidance"
    assert dev.graph["guider"]["inputs"]["conditioning"] == ["pos_ref3", 0]
    assert dev.graph["sigmas"]["inputs"]["steps"] == 20
    assert dev.method.models["unet"] == "flux2_dev_fp8mixed.safetensors"

    clip_folder = _anime_bench(
        bench_root,
        "kf_bad",
        "video",
        [{"id": "klein", "method": "comfy_keyframe", "preset": "klein"}],
    )
    bad = bs.load_spec(clip_folder)
    with pytest.raises(bs.BenchError, match="output: image"):
        m.plan(bad, bad.scenes[0], bad.branches[0], 11, frame=clip_folder / bad.scenes[0].frame)


def test_keyframe_panel_only_keeps_panel_proportions(bench_root: Path) -> None:
    """`refs: false` — на под уходит одна панель, листы героев сцены не прикладываются
    (лист с несколькими ракурсами клонировал героя на толпу, anime_keyframe_holdout);
    `size: panel` — кадр в пропорции панели около мегапикселя, кратно 16."""
    from videoforge.bench.run import ComfyKeyframe

    params = {"refs": False, "size": "panel"}
    folder = _anime_bench(
        bench_root,
        "kf_panel",
        "image",
        [
            {"id": "dev", "method": "comfy_keyframe", "preset": "flux2_dev", "params": params},
            {"id": "qwen", "method": "comfy_keyframe", "preset": "qwen_edit", "params": params},
        ],
    )
    spec = bs.load_spec(folder)
    scene = spec.scenes[0]
    frame = folder / scene.frame  # 96×64 — 3:2
    m = ComfyKeyframe()
    dev = m.plan(spec, scene, spec.branches[0], 11, frame=frame)
    assert [n for _, n in dev.uploads] == ["panels_p5_arena.png"]
    assert dev.inputs.refs == [] and dev.inputs.refs_sha256 == []
    assert "ref_img2" not in dev.graph and dev.graph["guider"]["inputs"]["conditioning"] == [
        "pos_ref1",
        0,
    ]
    assert (
        dev.graph["latent"]["inputs"]["width"] == 1232
        and dev.graph["latent"]["inputs"]["height"] == 816
    )
    assert dev.inputs.size == "1232x816" and dev.inputs.aspect_ratio == "panel"

    qwen = m.plan(spec, scene, spec.branches[1], 11, frame=frame)
    assert "image2" not in qwen.graph["pos"]["inputs"]
    assert qwen.graph["lat"]["inputs"]["width"] == 1232


def test_anime_clip_plans_anisora_recipe_and_h3_modes(bench_root: Path) -> None:
    """Клип из кадра: AniSora — граф Wan без lightx2v, 8 шагов 3 + 5; H3 i2v —
    первый кадр, H3 ref — кадр <Picture 1> и листы героев за ним, 124 кадра на 5 с,
    размер кратен 32."""
    from videoforge.bench.run import ComfyH3, ComfyI2V

    folder = _anime_bench(
        bench_root,
        "clip",
        "video",
        [
            {"id": "anisora", "method": "comfy_i2v", "preset": "anisora720"},
            {"id": "h3_i2v", "method": "comfy_h3", "preset": "i2v"},
            {"id": "h3_ref", "method": "comfy_h3", "preset": "ref"},
            {"id": "h3_bad", "method": "comfy_h3", "preset": "ref", "params": {"width": 850}},
            {
                "id": "h3_768",
                "method": "comfy_h3",
                "preset": "i2v",
                "params": {"width": 1344, "height": 768, "turbo_768": True},
            },
        ],
    )
    spec = bs.load_spec(folder)
    scene = spec.scenes[0]
    frame = folder / scene.frame

    anisora = ComfyI2V().plan(spec, scene, spec.branches[0], 11, frame=frame)
    g = anisora.graph
    assert "lh" not in g and g["mh"]["inputs"]["model"] == ["uh", 0]
    assert g["uh"]["inputs"]["unet_name"].startswith("Wan2_2-I2V_AniSoraV3_2_HIGH")
    assert (g["s1"]["inputs"]["steps"], g["s1"]["inputs"]["end_at_step"]) == (8, 3)
    assert (g["s2"]["inputs"]["start_at_step"], g["s2"]["inputs"]["end_at_step"]) == (3, 8)
    assert "lora_high" not in anisora.method.models and anisora.inputs.size == "1280x720"

    h3 = ComfyH3()
    i2v = h3.plan(spec, scene, spec.branches[1], 11, frame=frame)
    assert i2v.graph["cond"]["class_type"] == "MiniMaxH3ImageToVideo"
    assert i2v.graph["first"]["inputs"]["image"] == "panels_p5_arena.png"
    assert i2v.graph["cond"]["inputs"]["length"] == 124 and i2v.inputs.frames == 124
    assert i2v.graph["sch"]["inputs"]["steps"] == 8 and len(i2v.uploads) == 1
    assert i2v.inputs.refs == [] and i2v.inputs.size == "864x480"

    ref = h3.plan(spec, scene, spec.branches[2], 11, frame=frame)
    cond = ref.graph["cond"]["inputs"]
    assert ref.graph["cond"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert [
        ref.graph[cond[f"ref_images.ref_image_{i}"][0]]["inputs"]["image"] for i in range(3)
    ] == [
        "panels_p5_arena.png",
        "hero_a_full_body_turnaround.png",
        "hero_b_full_body_turnaround.png",
    ]
    assert ref.graph["sch"]["inputs"]["steps"] == 4 and len(ref.uploads) == 3
    assert ref.graph["vid"]["inputs"]["audio"] == ["adec", 0]
    fp = bs.fingerprint
    assert fp(i2v.inputs, i2v.method, i2v.graph) != fp(ref.inputs, ref.method, ref.graph)
    with pytest.raises(bs.BenchError, match="кратен 32"):
        h3.plan(spec, scene, spec.branches[3], 11, frame=frame)

    # мелкие лица общего плана: турбо 768p обучена при сдвиге 6/3 — узел сдвига после LoRA
    big = h3.plan(spec, scene, spec.branches[4], 11, frame=frame)
    assert big.graph["lora"]["inputs"]["lora_name"].endswith("768p_comfyui_bf16.safetensors")
    assert big.graph["shift"]["inputs"] == {
        "model": ["lora", 0],
        "shift_video": 6.0,
        "shift_audio": 3.0,
    }
    assert big.graph["sch"]["inputs"]["model"] == ["shift", 0]
    assert big.graph["sch"]["inputs"]["steps"] == 4 and big.inputs.size == "1344x768"
    assert "shift" not in i2v.graph


def test_dry_run_writes_plan_only(bench_root: Path) -> None:
    folder = make_bench(bench_root, branches=[{"id": "a", "method": "mock"}])
    report = run_bench(folder, dry=True)
    assert report.planned == ["S1_a_s11"]
    paths = bs.take_paths(folder, "S1_a_s11")
    rec = bs.read_take(paths.record)
    assert rec is not None and rec.status == "planned" and paths.graph.is_file()
    assert not paths.video.exists()


# ---------------------------------------------------------------- сводка


def test_summary_counts_not_statistics(bench_root: Path) -> None:
    folder = make_bench(bench_root, seeds=(11, 42))
    spec = bs.load_spec(folder)
    for branch in ("a", "b"):
        for seed in (11, 42):
            _done_take(folder, "S1", branch, seed)
    verdicts = {
        "S1_a_s11": bs.Verdict(
            take_id="S1_a_s11", criteria={"identity": "pass", "anatomy": "pass"}
        ),
        "S1_a_s42": bs.Verdict(
            take_id="S1_a_s42",
            criteria={"identity": "pass", "anatomy": "fail"},
            defect_times=[3.4],
        ),
        "S1_b_s11": bs.Verdict(
            take_id="S1_b_s11",
            criteria={"identity": "pass", "anatomy": "pass"},
            accepted_interval=(0.0, 2.5),
        ),
        "S1_b_s42": bs.Verdict(take_id="S1_b_s42", criteria={"identity": "pass"}),  # не до конца
    }
    for v in verdicts.values():
        bs.write_verdict(bs.take_paths(folder, v.take_id).verdict, v)
    takes = bs.load_takes(folder, spec)
    s = summarize(spec, takes, bs.load_verdicts(folder, takes))
    a, b = s["branches"]
    assert a["reviewed"] == 2 and a["accepted"] == 1 and a["rejected"] == 1 and a["share"] == 0.5
    assert a["fails"] == {"identity": 0, "anatomy": 1} and a["defect_times"] == [3.4]
    assert a["accepted_sec"] == 5.0 and a["usd_per_accepted_sec"] == round(0.04 / 5.0, 4)
    assert b["reviewed"] == 2 and b["partial"] == 1 and b["accepted"] == 1
    assert b["accepted_sec"] == 2.5  # принятый отрезок, а не весь клип
    assert s["ladder"][0]["branches"]["a"] == {
        "done": 2,
        "reviewed": 2,
        "accepted": 1,
        "passes_rule": False,
    }
    md = render_markdown(s)
    assert "| a | mock" in md and "Лестница" in md and "не аттестат" in md


# ---------------------------------------------------------------- пульт


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(ui_dist=Path("nonexistent")))


def test_api_bench_detail_and_verdict(bench_root: Path, client: TestClient) -> None:
    folder = make_bench(bench_root)
    _done_take(folder, "S1", "a", 11)

    over = client.get("/api/bench").json()
    card = over["benches"][0]
    assert card["id"] == "b1" and card["takes"] == 1 and card["budget_usd"] == 1.0
    assert over["totals"]["bench_usd"] == 0.02 and over["totals"]["benches"] == 1

    body = client.get("/api/bench/b1").json()
    take = body["takes"][0]
    assert take["inputs"]["prompt"] == "cat turns head" and take["verdict"] is None
    assert take["video_url"] is None and body["scenes"][0]["frame_url"]
    assert body["summary"]["reviewed"] == 0
    assert client.get("/api/bench/nope").status_code == 404

    url = "/api/bench/b1/takes/S1_a_s11/verdict"
    ok = client.post(
        url,
        json={"criteria": {"identity": "pass", "anatomy": "pass"}, "reviewer": "я", "base": ""},
    )
    assert ok.status_code == 200 and ok.json()["accepted"] is True
    saved = bs.read_verdict(bs.take_paths(folder, "S1_a_s11").verdict)
    assert saved is not None and saved.reviewer == "я" and saved.at

    # чужая вкладка сохранила раньше: старый отпечаток → 409, свежий → 200
    stale = client.post(url, json={"criteria": {"identity": "fail"}, "base": ""})
    assert stale.status_code == 409
    fresh = client.post(url, json={"criteria": {"identity": "fail"}, "base": ok.json()["base"]})
    assert fresh.status_code == 200 and fresh.json()["accepted"] is False

    bad = client.post(url, json={"criteria": {"gloves": "pass"}, "base": fresh.json()["base"]})
    assert bad.status_code == 422
    assert client.post("/api/bench/b1/takes/nope/verdict", json={"base": ""}).status_code == 404

    # чужая страница в браузере: POST с другим Origin не проходит
    foreign = client.post(url, json={"base": ""}, headers={"Origin": "http://evil.example"})
    assert foreign.status_code == 403

    detail = client.get("/api/bench/b1").json()["takes"][0]
    assert detail["accepted"] is False and detail["verdict"]["criteria"] == {"identity": "fail"}


def test_upscale_plan_takes_size_and_length_from_the_clip(
    bench_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """comfy_upscale: исходный клип сцены (`video`) → lanczos до ×factor → VAE в
    стартовый латент и те же кадры направляющими в LTXAddVideoICLoRAGuide; размер
    и кадры читаются с клипа, адаптер по factor, верхние сигмы срезаются по
    skip_sigmas; без video сцена не планируется, чужой factor — ошибка."""
    from videoforge.bench.run import ComfyUpscale

    folder = make_bench(
        bench_root,
        branches=[
            {
                "id": "x2",
                "method": "comfy_upscale",
                "preset": "ltx25",
                "params": {"factor": 2, "skip_sigmas": 2, "ic_strength": 0.8},
            },
            {"id": "x4", "method": "comfy_upscale", "preset": "ltx25", "params": {"factor": 4}},
            {"id": "x3", "method": "comfy_upscale", "preset": "ltx25", "params": {"factor": 3}},
        ],
        compare="cause",
    )
    data = yaml.safe_load((folder / "bench.yaml").read_text(encoding="utf-8"))
    data["scenes"][0]["video"] = "inputs/s1_lo.mp4"
    (folder / "bench.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    spec = bs.load_spec(folder)
    assert bs.missing_frames(folder, spec) == {"S1": "inputs/s1_lo.mp4"}  # клипа ещё нет
    (folder / "inputs" / "s1_lo.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    assert bs.missing_frames(folder, spec) == {}
    monkeypatch.setattr("videoforge.core.ffmpeg.probe_size", lambda p: (576, 1024))
    monkeypatch.setattr("videoforge.core.ffmpeg.probe_duration", lambda p: 3.04)

    frame = folder / "inputs" / "s1.png"
    m = ComfyUpscale()
    x2 = m.plan(spec, spec.scenes[0], spec.branches[0], 11, frame=frame)
    g = x2.graph
    assert g["ic"]["inputs"]["lora_name"].endswith("pixel-spatial-upscaler-x2-1.0.safetensors")
    assert (g["refs"]["inputs"]["width"], g["refs"]["inputs"]["height"]) == (1152, 2048)
    assert g["enc"]["inputs"]["pixels"] == ["refs", 0] and g["guide"]["inputs"]["latent"] == [
        "enc",
        0,
    ]
    assert g["guide"]["inputs"]["image"] == ["refs", 0] and g["guide"]["inputs"]["strength"] == 0.8
    assert g["guide"]["inputs"]["latent_downscale_factor"] == ["ic", 1]
    assert (
        g["sig1"]["inputs"]["sigmas"].startswith("0.9875, 0.98125")
        and "1.0," not in g["sig1"]["inputs"]["sigmas"]
    )
    assert (
        g["dec"]["inputs"]["samples"] == ["crop", 2] and g["ref"]["inputs"]["file"] == "s1_lo.mp4"
    )
    assert "img" not in g  # кадр в граф не идёт: вход — клип
    assert (x2.inputs.size, x2.inputs.frames, x2.inputs.duration_sec) == (
        "576x1024→×2=1152x2048",
        73,
        3,
    )
    assert x2.inputs.reference == "inputs/s1_lo.mp4" and len(x2.inputs.reference_sha256) == 64
    assert (
        x2.method.method == "comfy_upscale"
        and x2.method.models["ic_lora"] == g["ic"]["inputs"]["lora_name"]
    )

    x4 = m.plan(spec, spec.scenes[0], spec.branches[1], 11, frame=frame)
    assert x4.graph["ic"]["inputs"]["lora_name"].endswith("x4-0.9.safetensors")
    assert x4.graph["refs"]["inputs"]["width"] == 2304 and x4.inputs.size.endswith("=2304x4096")

    with pytest.raises(bs.BenchError, match="factor 3"):
        m.plan(spec, spec.scenes[0], spec.branches[2], 11, frame=frame)

    plain = make_bench(
        bench_root, "b2", branches=[{"id": "x2", "method": "comfy_upscale", "preset": "ltx25"}]
    )
    plain_spec = bs.load_spec(plain)
    with pytest.raises(bs.BenchError, match="video у сцены"):
        m.plan(
            plain_spec,
            plain_spec.scenes[0],
            plain_spec.branches[0],
            11,
            frame=plain / "inputs" / "s1.png",
        )


def test_animate_plan_builds_segment_chain_from_the_clip(
    bench_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """comfy_animate: картинка персонажа + исходный клип → Wan 2.2 Animate в режиме
    замены: маска человека SAM2 по точкам первого кадра, скелет и лицо DWPose,
    фон — кадры с вырезанным человеком; проходов по 77 кадров столько, сколько
    нужно на длину клипа, продолжение берёт смещение и хвост прошлого прохода;
    выход обрезан до длины клипа, звук исходника; без точек — ошибка."""
    from videoforge.bench.run import ComfyAnimate
    from videoforge.providers import comfy_http as ch

    assert [ch.animate_segments(n) for n in (77, 78, 149, 150, 313)] == [1, 2, 2, 3, 5]

    folder = make_bench(
        bench_root,
        branches=[
            {
                "id": "mix",
                "method": "comfy_animate",
                "preset": "wan_animate",
                "params": {
                    "points_positive": [[0.25, 0.5], [0.5, 0.75]],
                    "points_negative": [[0.75, 0.25]],
                },
            },
            {"id": "nopts", "method": "comfy_animate", "preset": "wan_animate"},
            {
                "id": "px",
                "method": "comfy_animate",
                "preset": "wan_animate",
                "params": {"points_positive": [[470, 520]]},
            },
        ],
        compare="cause",
    )
    data = yaml.safe_load((folder / "bench.yaml").read_text(encoding="utf-8"))
    data["aspect_ratio"] = "16:9"
    data["scenes"][0]["video"] = "inputs/meme.mp4"
    (folder / "bench.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (folder / "inputs" / "meme.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    spec = bs.load_spec(folder)
    monkeypatch.setattr("videoforge.core.ffmpeg.probe_size", lambda p: (1920, 1080))
    monkeypatch.setattr("videoforge.core.ffmpeg.probe_fps", lambda p: 25.0)
    monkeypatch.setattr("videoforge.core.ffmpeg.probe_duration", lambda p: 12.52)

    frame = folder / "inputs" / "s1.png"
    m = ComfyAnimate()
    p = m.plan(spec, spec.scenes[0], spec.branches[0], 11, frame=frame)
    g = p.graph
    assert (p.inputs.frames, p.inputs.fps, p.inputs.duration_sec) == (313, 25, 13)
    assert p.inputs.size == "1920x1080→1280x720 · 5 прох."
    assert g["u"]["inputs"]["unet_name"] == ch.ANIMATE_UNET and g["ms"]["inputs"]["shift"] == 8.0
    assert g["rl"]["inputs"]["lora_name"] == ch.ANIMATE_RELIGHT
    assert g["frames"]["inputs"]["width"] == 1280 and g["ref"]["inputs"]["file"] == "meme.mp4"
    assert (
        g["face"]["inputs"]["detect_face"] == "enable"
        and g["face"]["inputs"]["detect_body"] == "disable"
    )
    assert (
        g["pose"]["inputs"]["detect_body"] == "enable"
        and g["pose"]["inputs"]["detect_face"] == "disable"
    )
    # точки схемы — доли кадра, в граф идут пикселями рабочего размера 1280×720
    assert json.loads(g["pts"]["inputs"]["value"]) == [{"x": 320, "y": 360}, {"x": 640, "y": 540}]
    assert json.loads(g["npts"]["inputs"]["value"]) == [{"x": 960, "y": 180}]
    assert g["seg"]["inputs"]["coordinates_negative"] == ["npts", 0] and g["seg"]["inputs"][
        "image"
    ] == ["frames", 0]
    assert g["bg"]["inputs"]["mask"] == ["mask", 0] and g["mask"]["inputs"]["masks"] == ["grow", 0]
    a0, a4 = g["a0"]["inputs"], g["a4"]["inputs"]
    assert a0["video_frame_offset"] == 0 and "continue_motion" not in a0 and a0["length"] == 77
    assert a0["background_video"] == ["bg", 0] and a0["character_mask"] == ["mask", 0]
    assert a4["video_frame_offset"] == ["a3", 5] and a4["continue_motion"] == ["d3", 0]
    assert "a5" not in g and g["k4"]["inputs"]["seed"] == 15 and g["k0"]["inputs"]["steps"] == 6
    assert g["t2"]["inputs"]["trim_amount"] == ["a2", 3] and g["b2"]["inputs"]["batch_index"] == [
        "a2",
        4,
    ]
    assert g["cat4"]["inputs"]["image2"] == ["b4", 0] and g["fit"]["inputs"] == {
        "image": ["cat4", 0],
        "batch_index": 0,
        "length": 313,
    }
    assert g["vid"]["inputs"]["audio"] == ["refc", 1] and g["vid"]["inputs"]["fps"] == 25.0
    assert p.method.method == "comfy_animate" and p.method.models["unet"] == ch.ANIMATE_UNET

    with pytest.raises(bs.BenchError, match="points_positive"):
        m.plan(spec, spec.scenes[0], spec.branches[1], 11, frame=frame)
    with pytest.raises(bs.BenchError, match="доли кадра"):
        m.plan(spec, spec.scenes[0], spec.branches[2], 11, frame=frame)
