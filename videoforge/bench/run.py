"""Раннер замера: сцена × ветка × сид → дубль с полным происхождением.

Метод ветки сначала СТРОИТ ПЛАН (граф, входы, веса, прогноз цены) без единого
вызова — по нему считается отпечаток и сверяется бюджет, — и только потом
выполняет его теми же классами, что провайдер `comfy_http`. Правила:
  - дубль с тем же отпечатком и готовым файлом не перегоняется (`--force`);
  - `потрачено + прогноз > budget_usd` — стоп до вызова, не после;
  - техническая ошибка ветки (граф отвергнут, вход негоден, OOM, под молчит)
    записывается с `error_kind`, и остальные сиды этой ветки не жгутся —
    сначала чинится причина;
  - прогресс — файлами после каждого дубля, второго прогона можно не бояться.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median
from typing import Any, Protocol

from videoforge.bench.spec import (
    BenchBranch,
    BenchError,
    BenchScene,
    BenchSpec,
    TakeInputs,
    TakeMethod,
    TakeMetrics,
    TakeRecord,
    fingerprint,
    load_spec,
    load_takes,
    missing_frames,
    now_iso,
    read_take,
    sha256_file,
    take_id,
    take_paths,
    write_graph,
    write_take,
)
from videoforge.providers.base import ProviderError

log = logging.getLogger("videoforge.bench")


@dataclass
class TakePlan:
    graph: dict[str, Any]
    inputs: TakeInputs
    method: TakeMethod
    estimate_usd: float
    # файлы на под: (локальный путь, имя на поде) — у методов с референсами
    uploads: list[tuple[Path, str]] = field(default_factory=list)


@dataclass
class Executed:
    gen_sec: float
    cost_usd: float
    graph: dict[str, Any]  # граф как ушёл на под (имя загруженного кадра может отличаться)


class Method(Protocol):
    def plan(
        self, spec: BenchSpec, scene: BenchScene, branch: BenchBranch, seed: int, *, frame: Path
    ) -> TakePlan: ...

    def execute(self, plan: TakePlan, *, frame: Path, out_path: Path) -> Executed: ...


def bench_folder(frame: Path, scene: BenchScene) -> Path:
    """Папка замера по пути кадра: `frame` = папка / `scene.frame`, и путь кадра
    может выходить из папки (`../anime_anime/panels/p1.png` — материал, общий для
    двух замеров), поэтому отрезается ровно столько частей, сколько их в схеме."""
    return frame.parents[len(Path(scene.frame).parts) - 1]


def panel_size(path: Path, megapixels: float = 1.0, step: int = 16) -> tuple[int, int]:
    """Размер кадра в пропорции картинки около `megapixels`, кратно 16 (латент Flux.2
    и Qwen): панель манги бывает и квадратной, и полосой 4:1 — кадр её не обрезает."""
    from PIL import Image

    with Image.open(path) as im:
        w, h = im.size
    scale = (megapixels * 1_000_000 / (w * h)) ** 0.5
    return max(step, round(w * scale / step) * step), max(step, round(h * scale / step) * step)


def scene_refs(folder: Path, scene: BenchScene) -> tuple[list[tuple[str, Path]], list[str]]:
    """Референсы сцены: (путь из схемы, файл) и отпечатки — в запись дубля."""
    refs: list[tuple[str, Path]] = []
    for rel in scene.refs:
        path = folder / rel
        if not path.is_file():
            raise BenchError(f"сцена {scene.id}: референса нет: {path}")
        refs.append((rel, path))
    return refs, [sha256_file(p) for _, p in refs]


# ---------------------------------------------------------------- методы


class MockMethod:
    """Заглушка: Ken Burns по кадру ffmpeg-ом, $0. `params.fail: true` роняет
    ветку «граф отвергнут» — так проверяется, что раннер не жжёт сиды."""

    def plan(
        self, spec: BenchSpec, scene: BenchScene, branch: BenchBranch, seed: int, *, frame: Path
    ) -> TakePlan:
        prompt = scene.prompt_for(branch.id)
        duration = int(branch.params.get("duration_sec") or spec.duration_sec)
        if spec.output == "image":
            duration = 0
        refs, refs_sha = scene_refs(bench_folder(frame, scene), scene)
        inputs = TakeInputs(
            frame=scene.frame,
            frame_sha256=sha256_file(frame),
            refs=[r for r, _ in refs],
            refs_sha256=refs_sha,
            prompt=prompt,
            negative=scene.negative,
            aspect_ratio=spec.aspect_ratio,
            duration_sec=duration,
            frames=duration * 24,
            fps=24,
            size="mock",
        )
        method = TakeMethod(method="mock", preset=branch.preset, params=dict(branch.params))
        graph = {"mock": {"prompt": prompt, "seed": seed, "duration_sec": duration}}
        return TakePlan(graph=graph, inputs=inputs, method=method, estimate_usd=0.0)

    def execute(self, plan: TakePlan, *, frame: Path, out_path: Path) -> Executed:
        if plan.method.params.get("fail"):
            raise ProviderError("ComfyUI отверг граф: mock")
        from videoforge.core.ffmpeg import aspect_size, image_to_video

        t0 = time.perf_counter()
        if out_path.suffix == ".png":
            # замер кадров: результат — сам кадр, перерисованный «моделью»
            from PIL import Image

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(frame) as im:
                im.convert("RGB").save(out_path)
            return Executed(gen_sec=time.perf_counter() - t0, cost_usd=0.0, graph=plan.graph)
        image_to_video(
            frame,
            out_path,
            duration=plan.inputs.duration_sec,
            size=aspect_size(plan.inputs.aspect_ratio),
        )
        return Executed(gen_sec=time.perf_counter() - t0, cost_usd=0.0, graph=plan.graph)


class ComfyI2V:
    """Картинка → клип на поде ComfyUI: граф собирают те же функции, что у
    провайдера (`comfy_http.i2v_plan`), сид — явный из схемы, а не от промпта."""

    def __init__(self) -> None:
        from videoforge.providers import comfy_http as ch

        self.ch = ch
        self._state: Any = None
        self._freed = False

    def _pod(self) -> Any:
        if self._state is None:
            self._state = self.ch.PodState.load()
        return self._state

    def _pod_meta(self) -> tuple[str, str, float]:
        """Веса персонажа, id пода и ставка — с поднятой карты; без неё план
        строится всё равно (--dry), а выполнение упадёт как network."""
        try:
            st = self._pod()
        except ProviderError:
            return "", "", self.ch.DEFAULT_RATE_USD_HR
        weights = f"{st.lora}@{st.lora_strength:g}" if st.lora else ""
        return weights, st.pod_id, st.rate_usd_hr

    def plan(
        self, spec: BenchSpec, scene: BenchScene, branch: BenchBranch, seed: int, *, frame: Path
    ) -> TakePlan:
        ch = self.ch
        if branch.preset not in ch.VIDEO_PRESETS:
            raise BenchError(
                f"ветка {branch.id}: неизвестный пресет '{branch.preset}': "
                f"{sorted(ch.VIDEO_PRESETS)}"
            )
        preset = ch.VIDEO_PRESETS[branch.preset]
        prompt = scene.prompt_for(branch.id)
        params = dict(branch.params)
        # длина клипа — переменная ветки: одно событие модель растягивает на весь
        # клип (t02), и короткий клип — способ получить один чёткий удар
        duration = int(params.get("duration_sec") or spec.duration_sec)
        if duration not in preset.durations:
            raise BenchError(
                f"ветка {branch.id}: пресет {preset.id} не умеет {duration} с, "
                f"только {list(preset.durations)}"
            )
        graph, meta = ch.i2v_plan(
            preset,
            frame.name,
            prompt,
            aspect_ratio=spec.aspect_ratio,
            duration_sec=duration,
            seed=seed,
            prefix=f"bench/{spec.id}/{take_id(scene.id, branch.id, seed)}",
            audio=bool(params.get("audio", False)),
            negative=scene.negative or None,
            # условие темпа LTX (16 при выводе 24) — переменная ветки, см. i2v_plan
            frame_rate_cond=(
                float(params["frame_rate_cond"]) if params.get("frame_rate_cond") else None
            ),
        )
        inputs = TakeInputs(
            frame=scene.frame,
            frame_sha256=sha256_file(frame),
            prompt=prompt,
            negative=str(meta.get("negative") or ""),
            aspect_ratio=spec.aspect_ratio,
            duration_sec=duration,
            frames=int(meta["frames"]),
            fps=int(meta["fps"]),
            size=str(meta["size"]),
        )
        weights, pod_id, rate = self._pod_meta()
        method = TakeMethod(
            method="comfy_i2v",
            preset=preset.id,
            params=params,
            models=ch.model_files(preset),
            weights=weights,
            pod_id=pod_id,
            rate_usd_hr=rate,
        )
        estimate = round(inputs.frames * preset.sec_per_frame * rate / 3600, 4)
        return TakePlan(graph=graph, inputs=inputs, method=method, estimate_usd=estimate)

    def execute(self, plan: TakePlan, *, frame: Path, out_path: Path) -> Executed:
        ch = self.ch
        st = self._pod()
        client = ch.ComfyClient(st.base_url)
        if not self._freed:
            client.free()
            self._freed = True
        name = client.upload_image(frame)
        graph = plan.graph
        if name != frame.name:
            # ComfyUI переименовал загрузку — в граф идёт настоящее имя, и оно же в запись
            graph = ch.with_image_name(graph, name)
        t0 = time.perf_counter()
        hist = client.wait(client.queue(graph))
        item = client.find_output(hist, (".mp4", ".webm", ".mov"))
        if not item:
            raise ProviderError("ComfyUI не вернул видео (нет SaveVideo в history)")
        client.download(item, out_path)
        sec = time.perf_counter() - t0
        return Executed(gen_sec=sec, cost_usd=round(sec * st.rate_usd_hr / 3600, 6), graph=graph)


class ComfyControl(ComfyI2V):
    wait_timeout_s = 1800.0  # один дубль до 8 с — минуты; цепочка Animate переопределяет

    """Картинка + референсное видео → клип: LTX-2.5 с IC-LoRA Union Control
    (`comfy_http.control_graph`). Параметры ветки: `reference` — путь к видео
    относительно папки замера (8n+1 кадров, то же разрешение, 24 fps), `signal`
    — pose | canny | raw, `ic_strength`, `lora`, `two_stage`, `width`/`height`
    (по умолчанию 544×960 для 9:16 — короткая сторона шаблона Lightricks)."""

    def plan(
        self, spec: BenchSpec, scene: BenchScene, branch: BenchBranch, seed: int, *, frame: Path
    ) -> TakePlan:
        ch = self.ch
        params = dict(branch.params)
        ref_rel = str(params.get("reference") or "")
        if not ref_rel:
            raise BenchError(f"ветка {branch.id}: comfy_control требует params.reference")
        folder = bench_folder(frame, scene)
        ref = folder / ref_rel if not Path(ref_rel).is_absolute() else Path(ref_rel)
        if not ref.is_file():
            raise BenchError(f"ветка {branch.id}: референса нет: {ref}")
        prompt = scene.prompt_for(branch.id)
        duration = int(params.get("duration_sec") or spec.duration_sec)
        frames = ch.frames_for(duration, ch.LTX_FPS)
        # кратно 64, а не 32: адаптер ref0.5 делит латент пополам, и 544×960 (17×30
        # латентных клеток) он отверг на поде 13.09.2026; 1024×576 — ровно 16:9
        w, h = ch.size_for(
            spec.aspect_ratio, int(params.get("width") or 1024), int(params.get("height") or 576)
        )
        graph = ch.control_graph(
            frame.name,
            ref.name,
            prompt,
            width=w,
            height=h,
            frames=frames,
            seed=seed,
            prefix=f"bench/{spec.id}/{take_id(scene.id, branch.id, seed)}",
            signal=str(params.get("signal") or "pose"),
            lora=str(params.get("lora") or ch.IC_LORA_UNION),
            strength=float(params.get("ic_strength", 1.0)),
            two_stage=bool(params.get("two_stage", False)),
        )
        inputs = TakeInputs(
            frame=scene.frame,
            frame_sha256=sha256_file(frame),
            reference=ref_rel,
            reference_sha256=sha256_file(ref),
            prompt=prompt,
            negative="",
            aspect_ratio=spec.aspect_ratio,
            duration_sec=duration,
            frames=frames,
            fps=ch.LTX_FPS,
            size=f"{w}x{h}" + ("→×2" if params.get("two_stage") else ""),
        )
        weights, pod_id, rate = self._pod_meta()
        models = dict(ch.model_files(ch.VIDEO_PRESETS["ltx25"]))
        models["ic_lora"] = str(params.get("lora") or ch.IC_LORA_UNION)
        method = TakeMethod(
            method="comfy_control",
            preset="ltx25",
            params=params,
            models=models,
            weights=weights,
            pod_id=pod_id,
            rate_usd_hr=rate,
        )
        # прогноз: обычный проход плюс аннотатор и управляющие токены — ×2, замер уточнит
        estimate = round(frames * ch.MEASURED_SEC["ltx25_704p_per_frame"] * 2 * rate / 3600, 4)
        plan = TakePlan(graph=graph, inputs=inputs, method=method, estimate_usd=estimate)
        self._refs[plan.inputs.reference_sha256] = ref
        return plan

    def __init__(self) -> None:
        super().__init__()
        self._refs: dict[str, Path] = {}

    def execute(self, plan: TakePlan, *, frame: Path, out_path: Path) -> Executed:
        ch = self.ch
        st = self._pod()
        client = ch.ComfyClient(st.base_url)
        if not self._freed:
            client.free()
            self._freed = True
        ref = self._refs[plan.inputs.reference_sha256]
        name = client.upload_image(frame)
        ref_name = client.upload_image(ref)
        graph = plan.graph
        if name != frame.name:
            graph = ch.with_image_name(graph, name)
        if ref_name != ref.name:
            graph = json.loads(json.dumps(graph))
            graph["ref"]["inputs"]["file"] = ref_name
        t0 = time.perf_counter()
        hist = client.wait(client.queue(graph), timeout_s=self.wait_timeout_s)
        item = client.find_output(hist, (".mp4", ".webm", ".mov"))
        if not item:
            raise ProviderError("ComfyUI не вернул видео (нет SaveVideo в history)")
        client.download(item, out_path)
        sec = time.perf_counter() - t0
        return Executed(gen_sec=sec, cost_usd=round(sec * st.rate_usd_hr / 3600, 6), graph=graph)


class ComfyUpscale(ComfyControl):
    """Клип → тот же клип крупнее: LTX-2.5 с IC-LoRA Pixel Spatial Upscaler
    (`comfy_http.upscale_graph`). Вход — `video` сцены: исходный клип относительно
    папки замера; `frame` сцены — кадр, из которого клип сделан, он идёт в запись
    и в пульт, граф его не читает. Размер и длина берутся с самого клипа: выход
    = вход × `factor` (2 | 4). Параметры ветки: `factor`, `ic_strength`,
    `skip_sigmas` (верхние сигмы долой — ближе к исходнику), `lora`."""

    def plan(
        self, spec: BenchSpec, scene: BenchScene, branch: BenchBranch, seed: int, *, frame: Path
    ) -> TakePlan:
        from videoforge.core.ffmpeg import probe_duration, probe_size

        ch = self.ch
        params = dict(branch.params)
        if not scene.video:
            raise BenchError(
                f"сцена {scene.id}: comfy_upscale требует video у сцены (исходный клип)"
            )
        src = bench_folder(frame, scene) / scene.video
        if not src.is_file():
            raise BenchError(f"сцена {scene.id}: исходного клипа нет: {src}")
        factor = int(params.get("factor", 2))
        if factor not in ch.IC_LORA_UPSCALE:
            raise BenchError(
                f"ветка {branch.id}: factor {factor}, "
                f"апскейлер есть только {sorted(ch.IC_LORA_UPSCALE)}"
            )
        size = probe_size(src)
        if not size:
            raise BenchError(f"сцена {scene.id}: не читается размер клипа {src.name}")
        sw, sh = size
        frames = round(probe_duration(src) * ch.LTX_FPS)
        if (frames - 1) % 8:
            raise BenchError(f"сцена {scene.id}: в клипе {frames} кадров, апскейлеру нужно 8n+1")
        w, h = sw * factor, sh * factor
        prompt = scene.prompt_for(branch.id)
        lora = str(params.get("lora") or ch.IC_LORA_UPSCALE[factor])
        graph = ch.upscale_graph(
            src.name,
            prompt,
            factor=factor,
            width=w,
            height=h,
            frames=frames,
            seed=seed,
            prefix=f"bench/{spec.id}/{take_id(scene.id, branch.id, seed)}",
            lora=lora,
            strength=float(params.get("ic_strength", 1.0)),
            skip_sigmas=int(params.get("skip_sigmas", 0)),
        )
        inputs = TakeInputs(
            frame=scene.frame,
            frame_sha256=sha256_file(frame),
            reference=scene.video,
            reference_sha256=sha256_file(src),
            prompt=prompt,
            negative="",
            aspect_ratio=spec.aspect_ratio,
            duration_sec=round(frames / ch.LTX_FPS),
            frames=frames,
            fps=ch.LTX_FPS,
            size=f"{sw}x{sh}→×{factor}={w}x{h}",
        )
        weights, pod_id, rate = self._pod_meta()
        models = dict(ch.model_files(ch.VIDEO_PRESETS["ltx25"]))
        models["ic_lora"] = lora
        method = TakeMethod(
            method="comfy_upscale",
            preset="ltx25",
            params=params,
            models=models,
            weights=weights,
            pod_id=pod_id,
            rate_usd_hr=rate,
        )
        # прогноз: время растёт с числом токенов — площадь выхода против 704p замера,
        # плюс направляющие; замер уточнит
        area = (w * h) / (1280 * 704)
        estimate = round(
            frames * ch.MEASURED_SEC["ltx25_704p_per_frame"] * area * 1.5 * rate / 3600, 4
        )
        plan = TakePlan(graph=graph, inputs=inputs, method=method, estimate_usd=estimate)
        self._refs[plan.inputs.reference_sha256] = src
        return plan


class ComfyAnimate(ComfyControl):
    """Персонаж с картинки на место человека в чужом видео: Wan 2.2 Animate,
    режим замены (`comfy_http.animate_graph`). Вход — `frame` сцены (картинка
    персонажа) и `video` сцены (исходный клип); размер, длина и fps читаются с
    клипа, выход — клип той же длины со звуком исходника. Параметры ветки:
    `points_positive` / `points_negative` — точки маски человека на ПЕРВОМ кадре
    долями ширины и высоты (0..1), `width`/`height` (кратно 16; по умолчанию
    1280×720 для 16:9), `steps`, `relight`, `mask_grow`, `mask_block`."""

    wait_timeout_s = 5400.0  # пять проходов по 77 кадров на 720p — десятки минут

    def plan(
        self, spec: BenchSpec, scene: BenchScene, branch: BenchBranch, seed: int, *, frame: Path
    ) -> TakePlan:
        from videoforge.core.ffmpeg import probe_duration, probe_fps, probe_size

        ch = self.ch
        params = dict(branch.params)
        if not scene.video:
            raise BenchError(
                f"сцена {scene.id}: comfy_animate требует video у сцены (исходный клип)"
            )
        src = bench_folder(frame, scene) / scene.video
        if not src.is_file():
            raise BenchError(f"сцена {scene.id}: исходного клипа нет: {src}")
        pts = [tuple(p) for p in params.get("points_positive") or []]
        if not pts:
            raise BenchError(f"ветка {branch.id}: нужны points_positive — точки на человеке")
        npts = [tuple(p) for p in params.get("points_negative") or []]
        size = probe_size(src)
        fps = probe_fps(src)
        if not size or not fps:
            raise BenchError(f"сцена {scene.id}: не читается размер или fps клипа {src.name}")
        sw, sh = size
        frames = round(probe_duration(src) * fps)
        w, h = ch.size_for(
            spec.aspect_ratio, int(params.get("width") or 1280), int(params.get("height") or 720)
        )
        # точки в схеме — доли ширины и высоты кадра (0..1): один набор годится
        # исходнику любого размера и ветке любого рабочего размера; граф
        # получает пиксели рабочего размера
        scale = (float(w), float(h))
        if any(v > 1.0 for pt in [*pts, *npts] for v in pt):
            raise BenchError(
                f"ветка {branch.id}: точки маски — доли кадра 0..1, а не пиксели: {pts[:2]}…"
            )
        prompt = scene.prompt_for(branch.id)
        graph = ch.animate_graph(
            frame.name,
            src.name,
            prompt,
            width=w,
            height=h,
            frames=frames,
            fps=fps,
            seed=seed,
            prefix=f"bench/{spec.id}/{take_id(scene.id, branch.id, seed)}",
            points_positive=[(round(x * scale[0]), round(y * scale[1])) for x, y in pts],
            points_negative=[(round(x * scale[0]), round(y * scale[1])) for x, y in npts],
            steps=int(params.get("steps", 6)),
            negative=scene.negative or ch.WAN_ANIMATE_NEGATIVE,
            mask_grow=int(params.get("mask_grow", 10)),
            mask_block=int(params.get("mask_block", 32)),
            relight=float(params.get("relight", 1.0)),
        )
        segments = ch.animate_segments(frames)
        inputs = TakeInputs(
            frame=scene.frame,
            frame_sha256=sha256_file(frame),
            reference=scene.video,
            reference_sha256=sha256_file(src),
            prompt=prompt,
            negative=scene.negative or ch.WAN_ANIMATE_NEGATIVE,
            aspect_ratio=spec.aspect_ratio,
            duration_sec=round(frames / fps),
            frames=frames,
            fps=round(fps),
            size=f"{sw}x{sh}→{w}x{h} · {segments} прох.",
        )
        weights, pod_id, rate = self._pod_meta()
        method = TakeMethod(
            method="comfy_animate",
            preset="wan_animate",
            params=params,
            models={
                "unet": ch.ANIMATE_UNET,
                "lora_distill": ch.ANIMATE_LIGHTX2V,
                "lora_relight": ch.ANIMATE_RELIGHT,
                "clip": ch.WAN_CLIP,
                "clip_vision": ch.WAN_CLIP_VISION,
                "vae": ch.WAN_VAE,
            },
            weights=weights,
            pod_id=pod_id,
            rate_usd_hr=rate,
        )
        # прогноз: проход 77 кадров ≈ клип Wan 480p (52 с) × площадь против 832×480,
        # плюс аннотаторы и маска; замер уточнит
        area = (w * h) / (832 * 480)
        estimate = round(segments * 52.5 * area * 1.3 * rate / 3600, 4)
        plan = TakePlan(graph=graph, inputs=inputs, method=method, estimate_usd=estimate)
        self._refs[plan.inputs.reference_sha256] = src
        return plan


def pod_name(rel: str) -> str:
    """Имя картинки на поде из пути в схеме: папка + файл. Листы разных героев
    зовутся одинаково (`hero_a/full_body_turnaround.png`, `hero_b/…`), а ComfyUI
    кладёт загрузки в одну папку; имя стабильное — отпечаток дубля от него зависит."""
    parts = Path(rel).parts
    return "_".join(p for p in parts[-2:] if p not in ("..", "."))


class _WithRefs(ComfyI2V):
    """Общее у методов с картинками-референсами: кадр и референсы уходят на под
    под стабильными именами, результат ищется по расширению."""

    wait_timeout_s = 1800.0
    suffixes: tuple[str, ...] = (".mp4", ".webm", ".mov")

    def execute(self, plan: TakePlan, *, frame: Path, out_path: Path) -> Executed:
        ch = self.ch
        st = self._pod()
        client = ch.ComfyClient(st.base_url)
        if not self._freed:
            client.free()
            self._freed = True
        for path, name in plan.uploads:
            got = client.upload_image(path, name)
            if got != name:
                raise ProviderError(f"ComfyUI загрузил {name} как {got}: вход граф не найдёт")
        t0 = time.perf_counter()
        hist = client.wait(client.queue(plan.graph), timeout_s=self.wait_timeout_s)
        item = client.find_output(hist, self.suffixes)
        if not item:
            raise ProviderError(f"ComfyUI не вернул результат {self.suffixes} (history пуста)")
        client.download(item, out_path)
        sec = time.perf_counter() - t0
        return Executed(
            gen_sec=sec, cost_usd=round(sec * st.rate_usd_hr / 3600, 6), graph=plan.graph
        )


class ComfyKeyframe(_WithRefs):
    """Панель манги (+ листы героев) → кадр аниме, замер с `output: image`.
    Пресеты: `klein` — FLUX.2 klein 4B, панель и референсы через ReferenceLatent
    (`comfy_http.klein_reference_graph`), промпт — описание кадра; `qwen_edit` —
    Qwen-Image-Edit-2511 + Lightning 4 шага, до трёх картинок, промпт — инструкция;
    `flux2_dev` — FLUX.2 [dev] 32B тем же ReferenceLatent, 20 шагов.
    Параметры: `width`/`height` (по умолчанию 1280×720 у klein и flux2_dev и 1344×768 у
    Qwen для 16:9), `size: panel` (пропорция панели около мегапикселя — как у кадра,
    с которым сравниваем), `refs: false` (только панель, без листов героев сцены),
    `lightning` (Qwen, по умолчанию true), `steps` (flux2_dev, по умолчанию 20)."""

    suffixes = (".png", ".jpg", ".webp")
    SIZES = {"klein": (1280, 720), "qwen_edit": (1344, 768), "flux2_dev": (1280, 720)}

    def plan(
        self, spec: BenchSpec, scene: BenchScene, branch: BenchBranch, seed: int, *, frame: Path
    ) -> TakePlan:
        ch = self.ch
        if spec.output != "image":
            raise BenchError(f"ветка {branch.id}: comfy_keyframe делает кадр — нужен output: image")
        if branch.preset not in self.SIZES:
            raise BenchError(f"ветка {branch.id}: пресет '{branch.preset}': {sorted(self.SIZES)}")
        params = dict(branch.params)
        folder = bench_folder(frame, scene)
        refs, refs_sha = scene_refs(folder, scene) if params.get("refs", True) else ([], [])
        aspect = spec.aspect_ratio
        if params.get("size") == "panel":
            w, h = panel_size(frame)
            aspect = "panel"
        else:
            w16, h16 = self.SIZES[branch.preset]
            w, h = ch.size_for(
                aspect, int(params.get("width") or w16), int(params.get("height") or h16)
            )
        prompt = scene.prompt_for(branch.id)
        prefix = f"bench/{spec.id}/{take_id(scene.id, branch.id, seed)}"
        names = [pod_name(scene.frame), *(pod_name(r) for r, _ in refs)]
        if branch.preset == "klein":
            base = ch.klein_graph(
                prompt, width=w, height=h, seed=seed, lora="", lora_strength=1.0, prefix=prefix
            )
            graph = ch.klein_reference_graph(base, names)
            models = {"unet": ch.KLEIN_UNET, "clip": ch.KLEIN_CLIP, "vae": ch.KLEIN_VAE}
            estimate_sec = ch.MEASURED_SEC["klein4b_1280x720"] * (1 + len(names))
        elif branch.preset == "flux2_dev":
            graph = ch.flux2_dev_graph(
                names,
                prompt,
                width=w,
                height=h,
                seed=seed,
                prefix=prefix,
                steps=int(params.get("steps", 20)),
            )
            models = {"unet": ch.FLUX2_DEV_UNET, "clip": ch.FLUX2_DEV_CLIP, "vae": ch.KLEIN_VAE}
            estimate_sec = ch.MEASURED_SEC["flux2dev_1mp"]
        else:
            lightning = bool(params.get("lightning", True))
            graph = ch.qwen_edit_graph(
                names, prompt, width=w, height=h, seed=seed, prefix=prefix, lightning=lightning
            )
            models = {
                "unet": ch.QWEN_EDIT_UNET,
                "clip": ch.QWEN_EDIT_CLIP,
                "vae": ch.QWEN_EDIT_VAE,
            }
            if lightning:
                models["lora"] = ch.QWEN_EDIT_LIGHTNING
            estimate_sec = ch.MEASURED_SEC["qwen_edit_1mp"] * (1 if lightning else 10)
        inputs = TakeInputs(
            frame=scene.frame,
            frame_sha256=sha256_file(frame),
            refs=[r for r, _ in refs],
            refs_sha256=refs_sha,
            prompt=prompt,
            negative="",
            aspect_ratio=aspect,
            duration_sec=0,
            size=f"{w}x{h}",
        )
        _, pod_id, rate = self._pod_meta()
        method = TakeMethod(
            method="comfy_keyframe",
            preset=branch.preset,
            params=params,
            models=models,
            pod_id=pod_id,
            rate_usd_hr=rate,
        )
        uploads = [(frame, names[0]), *((p, n) for (_, p), n in zip(refs, names[1:], strict=True))]
        estimate = round(estimate_sec * rate / 3600, 5)
        return TakePlan(
            graph=graph, inputs=inputs, method=method, estimate_usd=estimate, uploads=uploads
        )


class ComfyH3(_WithRefs):
    """Кадр → клип MiniMax H3 (`comfy_http.h3_graph`), 24 fps со звуком модели.
    Пресеты: `i2v` — FL2VA от первого кадра (кадр сцены), турбо 8 шагов; `ref` —
    Ref2VA, кадр сцены идёт референсом <Picture 1>, листы героев `refs` сцены —
    <Picture 2…>, турбо 4 шага. Параметры: `width`/`height` (по умолчанию 864×480
    для 16:9, кратно 32), `turbo` (true), `turbo_768` (i2v: турбо 768p, 4 шага, сдвиг
    6/3 — для 1344×768), `ref_image_size` (match | max)."""

    wait_timeout_s = 3600.0

    def plan(
        self, spec: BenchSpec, scene: BenchScene, branch: BenchBranch, seed: int, *, frame: Path
    ) -> TakePlan:
        ch = self.ch
        if branch.preset not in ch.H3_UNET:
            raise BenchError(f"ветка {branch.id}: пресет '{branch.preset}': {sorted(ch.H3_UNET)}")
        params = dict(branch.params)
        folder = bench_folder(frame, scene)
        refs, refs_sha = scene_refs(folder, scene)
        duration = int(params.get("duration_sec") or spec.duration_sec)
        frames = ch.h3_frames(duration)
        w, h = ch.size_for(
            spec.aspect_ratio, int(params.get("width") or 864), int(params.get("height") or 480)
        )
        if w % 32 or h % 32:
            raise BenchError(f"ветка {branch.id}: размер H3 кратен 32, а не {w}x{h}")
        prompt = scene.prompt_for(branch.id)
        turbo = bool(params.get("turbo", True))
        turbo_768 = bool(params.get("turbo_768", False))
        frame_name = pod_name(scene.frame)
        ref_names = [pod_name(r) for r, _ in refs]
        graph = ch.h3_graph(
            branch.preset,
            prompt,
            width=w,
            height=h,
            frames=frames,
            seed=seed,
            prefix=f"bench/{spec.id}/{take_id(scene.id, branch.id, seed)}",
            first_frame=frame_name if branch.preset == "i2v" else "",
            refs=[frame_name, *ref_names] if branch.preset == "ref" else None,
            turbo=turbo,
            ref_image_size=str(params.get("ref_image_size") or "match"),
            turbo_768=turbo_768,
        )
        uploads = [(frame, frame_name)]
        if branch.preset == "ref":
            uploads += [(p, n) for (_, p), n in zip(refs, ref_names, strict=True)]
        inputs = TakeInputs(
            frame=scene.frame,
            frame_sha256=sha256_file(frame),
            refs=[r for r, _ in refs] if branch.preset == "ref" else [],
            refs_sha256=refs_sha if branch.preset == "ref" else [],
            prompt=prompt,
            negative="",
            aspect_ratio=spec.aspect_ratio,
            duration_sec=duration,
            frames=frames,
            fps=ch.H3_FPS,
            size=f"{w}x{h}",
        )
        _, pod_id, rate = self._pod_meta()
        lora, _steps = ch.H3_TURBO_768 if turbo_768 else ch.H3_TURBO[branch.preset]
        models = {"unet": ch.H3_UNET[branch.preset], "clip": ch.H3_CLIP, "vae": ch.H3_VAE}
        models["audio_vae"] = ch.H3_AVAE
        if turbo:
            models["lora"] = lora
        method = TakeMethod(
            method="comfy_h3",
            preset=branch.preset,
            params=params,
            models=models,
            pod_id=pod_id,
            rate_usd_hr=rate,
        )
        area = (w * h) / (864 * 480)
        per_frame = ch.MEASURED_SEC["h3_480p_per_frame"] * area * (1 if turbo else 3)
        estimate = round(frames * per_frame * rate / 3600, 4)
        return TakePlan(
            graph=graph, inputs=inputs, method=method, estimate_usd=estimate, uploads=uploads
        )


METHODS: dict[str, Callable[[], Method]] = {
    "comfy_i2v": ComfyI2V,
    "comfy_control": ComfyControl,
    "comfy_upscale": ComfyUpscale,
    "comfy_animate": ComfyAnimate,
    "comfy_keyframe": ComfyKeyframe,
    "comfy_h3": ComfyH3,
    "mock": MockMethod,
}


def error_kind(exc: Exception) -> str:
    text = str(exc).lower()
    if "out of memory" in text or "oom" in text or "cuda error" in text or "allocat" in text:
        return "oom"
    if "отверг граф" in text or "node_errors" in text or "value not in list" in text:
        return "graph"
    if "class_type" in text or "required input is missing" in text:
        return "graph"
    if "upload" in text or "загруз" in text or "input" in text or "вход" in text:
        return "input"
    if (
        "не поднят" in text
        or "перестал отвечать" in text
        or "не отвечает" in text
        or "не завершился" in text
        or "http" in text
        or "connect" in text
        or "потерял задачу" in text
    ):
        return "network"
    return "unknown"


# ---------------------------------------------------------------- прогон


@dataclass
class RunReport:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # уже сделаны
    failed: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)  # --dry
    stopped: str = ""  # почему остановились раньше конца матрицы
    spent_usd: float = 0.0

    def line(self) -> str:
        parts = [f"сделано {len(self.done)}"]
        if self.skipped:
            parts.append(f"уже было {len(self.skipped)}")
        if self.failed:
            parts.append(f"упало {len(self.failed)}")
        if self.planned:
            parts.append(f"спланировано {len(self.planned)}")
        parts.append(f"потрачено ${self.spent_usd:.3f}")
        if self.stopped:
            parts.append(f"стоп: {self.stopped}")
        return " · ".join(parts)


def _metrics(video: Path) -> TakeMetrics | None:
    from videoforge.core.motion import QUIET_SHARE, load_or_measure

    try:
        prof = load_or_measure(video)
    except Exception as exc:  # noqa: BLE001 — замер не должен ронять записанный дубль
        log.warning("профиль движения %s: %s", video.name, exc)
        return None
    if not prof.energy:
        return TakeMetrics(duration_sec=0.0)
    peak = prof.peak
    quiet = sum(1 for e in prof.energy if e < QUIET_SHARE * peak) / len(prof.energy)
    return TakeMetrics(
        duration_sec=prof.duration_sec,
        motion_mean=round(mean(prof.energy), 3),
        motion_median=round(median(prof.energy), 3),
        motion_peak=round(peak, 3),
        quiet_share=round(quiet, 3),
    )


def run_bench(
    folder: Path,
    *,
    scenes: list[str] | None = None,
    branches: list[str] | None = None,
    seeds: list[int] | None = None,
    mock: bool = False,
    dry: bool = False,
    force: bool = False,
    metrics: bool = True,
    say: Callable[[str], None] = log.info,
) -> RunReport:
    spec = load_spec(folder)
    for sid in scenes or []:
        spec.scene(sid)
    for bid in branches or []:
        spec.branch(bid)
    bad_seeds = [s for s in seeds or [] if s not in spec.seeds]
    if bad_seeds:
        raise BenchError(f"сидов {bad_seeds} нет в схеме: {spec.seeds}")
    absent = missing_frames(folder, spec)
    report = RunReport()
    report.spent_usd = sum(
        t.cost_usd for t in load_takes(folder, spec) if t.status in ("done", "failed")
    )
    methods: dict[str, Method] = {}
    dead: set[str] = set()  # ветки, у которых упал дубль: остальные сиды не жжём

    for scene in spec.scenes:
        if scenes and scene.id not in scenes:
            continue
        if scene.id in absent:
            say(f"{scene.id}: кадра {absent[scene.id]} нет — сцену выбирает человек, пропуск")
            continue
        frame = folder / scene.frame
        for branch in spec.branches:
            if branches and branch.id not in branches:
                continue
            if branch.id in dead:
                continue
            method_name = "mock" if mock else branch.method
            if method_name not in METHODS:
                raise BenchError(f"ветка {branch.id}: неизвестный метод '{method_name}'")
            method = methods.get(method_name) or METHODS[method_name]()
            methods[method_name] = method
            for seed in spec.seeds:
                if seeds and seed not in seeds:
                    continue
                tid = take_id(scene.id, branch.id, seed)
                paths = take_paths(folder, tid)
                out_path = paths.output(spec.output)
                plan = method.plan(spec, scene, branch, seed, frame=frame)
                fp = fingerprint(plan.inputs, plan.method, plan.graph)
                existing = read_take(paths.record)
                if (
                    existing is not None
                    and existing.status == "done"
                    and existing.fingerprint == fp
                    and out_path.is_file()
                    and not force
                ):
                    report.skipped.append(tid)
                    continue
                record = TakeRecord(
                    take_id=tid,
                    scene=scene.id,
                    branch=branch.id,
                    seed=seed,
                    inputs=plan.inputs,
                    method=plan.method,
                    fingerprint=fp,
                    graph=paths.graph.relative_to(folder).as_posix(),
                    estimate_usd=plan.estimate_usd,
                )
                if dry:
                    write_graph(paths.graph, plan.graph)
                    write_take(paths.record, record)
                    report.planned.append(tid)
                    say(f"{tid}: план записан, прогноз ${plan.estimate_usd:.3f}")
                    continue
                if report.spent_usd + plan.estimate_usd > spec.budget_usd:
                    report.stopped = (
                        f"бюджет ${spec.budget_usd:.2f}: потрачено ${report.spent_usd:.3f}, "
                        f"следующий дубль {tid} ≈ ${plan.estimate_usd:.3f}"
                    )
                    say(report.stopped)
                    return report
                record.started_at = now_iso()
                write_graph(paths.graph, plan.graph)
                write_take(paths.record, record)
                say(f"{tid}: {plan.method.method} {plan.method.preset} …")
                try:
                    done = method.execute(plan, frame=frame, out_path=out_path)
                except ProviderError as exc:
                    record.status = "failed"
                    record.error_kind = error_kind(exc)
                    record.error = str(exc)[:600]
                    record.finished_at = now_iso()
                    write_take(paths.record, record)
                    report.failed.append(tid)
                    dead.add(branch.id)
                    say(
                        f"{tid}: {record.error_kind}: {record.error[:120]} — ветка {branch.id} стоп"
                    )
                    break
                record.status = "done"
                record.finished_at = now_iso()
                record.gen_sec = round(done.gen_sec, 1)
                record.cost_usd = round(done.cost_usd, 6)
                rel = out_path.relative_to(folder).as_posix()
                if spec.output == "image":
                    record.image = rel
                else:
                    record.video = rel
                if done.graph is not plan.graph:
                    write_graph(paths.graph, done.graph)
                    record.fingerprint = fingerprint(plan.inputs, plan.method, done.graph)
                if metrics and spec.output == "video":
                    record.metrics = _metrics(out_path)
                write_take(paths.record, record)
                report.done.append(tid)
                report.spent_usd = round(report.spent_usd + record.cost_usd, 6)
                say(f"{tid}: {record.gen_sec:.0f} с, ${record.cost_usd:.4f}")
    return report
