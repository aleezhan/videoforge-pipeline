"""Схема замера (`bench.yaml`), запись дубля и вердикт: модели и файлы.

Раскладка `projects/_bench/<id>/`:
  bench.yaml                 схема: вопрос, входы, ветки, сиды, критерии, бюджет
  inputs/<кадр>.png          стартовые кадры сцен (медиа — вне git, отпечаток в записи)
  takes/<take>.json          запись дубля: входы, метод, веса, граф, секунды, $, автозамеры
  takes/<take>.graph.json    граф ComfyUI ровно в том виде, в каком ушёл на под
  takes/<take>.mp4 | .png    результат: клип или кадр (`output` схемы)
  takes/<take>.verdict.json  вердикт человека (пишет пульт, раннер не трогает)
  summary.md                 сводка (`videoforge bench summary`)

Отпечаток дубля — от кадра, промпта, метода, весов и графа: тот же отпечаток
при повторном запуске значит «уже сделано», и деньги не тратятся второй раз.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from videoforge.paths import bench_dir

SPEC_FILE = "bench.yaml"
TAKES_DIR = "takes"
INPUTS_DIR = "inputs"
SUMMARY_FILE = "summary.md"

VerdictValue = Literal["pass", "fail", "na"]
TakeStatus = Literal["done", "failed", "planned"]

# из чего ошибка: не «карта больше», а три разных вывода (Codex, 12.09.2026)
ERROR_KINDS = ("graph", "input", "oom", "network", "unknown")


class BenchError(ValueError):
    """Схема или файлы замера негодны — человеку в текст ошибки, не в трейс."""


class Criterion(BaseModel):
    id: str
    title: str
    hint: str = ""


class BenchScene(BaseModel):
    id: str
    title: str = ""
    frame: str  # относительно папки замера
    prompt: str
    prompts: dict[str, str] = Field(default_factory=dict)  # ветка → свой текст
    negative: str = ""
    video: str = ""  # исходный клип сцены (comfy_upscale), относительно папки замера
    # картинки-референсы сцены (лист героя: лицо, рост) — их берут методы, которые
    # умеют референсы (comfy_keyframe, comfy_h3); относительно папки замера
    refs: list[str] = Field(default_factory=list)

    def prompt_for(self, branch_id: str) -> str:
        return self.prompts.get(branch_id, self.prompt)


class BenchBranch(BaseModel):
    id: str
    method: str = "comfy_i2v"  # comfy_i2v | mock
    preset: str = ""  # comfy: ltx25 | wan480 | wan720
    params: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class StopRule(BaseModel):
    """«Дальше по лестнице, если принято N из M» — правило экономии, не статистика."""

    min_accepted: int = 2
    of: int = 3


class BenchSpec(BaseModel):
    id: str
    question: str
    compare: Literal["cause", "method"] = "cause"
    budget_usd: float
    # что делает дубль: клип (i2v, управление, апскейл) или кадр (панель манги →
    # кадр аниме); от этого зависят расширение результата и автозамеры
    output: Literal["video", "image"] = "video"
    aspect_ratio: str = "9:16"
    duration_sec: int = 5
    seeds: list[int]
    scenes: list[BenchScene]
    branches: list[BenchBranch]
    criteria: list[Criterion]
    stop_rule: StopRule = Field(default_factory=StopRule)
    answer: str = ""
    date: str = ""
    notes: str = ""

    @model_validator(mode="after")
    def _check(self) -> BenchSpec:
        if not self.seeds:
            raise ValueError("seeds пуст: нужен хотя бы один сид")
        if self.budget_usd <= 0:
            raise ValueError("budget_usd должен быть больше нуля — потолок ставится до запуска")
        for name, items in (
            ("scenes", self.scenes),
            ("branches", self.branches),
            ("criteria", self.criteria),
        ):
            ids = [i.id for i in items]
            if not ids:
                raise ValueError(f"{name} пуст")
            dup = sorted({i for i in ids if ids.count(i) > 1})
            if dup:
                raise ValueError(f"{name}: повтор id {dup}")
        recipes = {b.method + ":" + b.preset for b in self.branches}
        if self.compare == "cause" and len(recipes) > 1:
            # общий сид значим только внутри одной реализации; сравнение разных
            # моделей — сравнение методов, и называть его надо прямо
            raise ValueError(
                "compare: cause, но ветки на разных методах/пресетах "
                f"{sorted(recipes)} — это сравнение методов: compare: method"
            )
        return self

    def scene(self, scene_id: str) -> BenchScene:
        for s in self.scenes:
            if s.id == scene_id:
                return s
        raise BenchError(f"нет сцены {scene_id}: {[s.id for s in self.scenes]}")

    def branch(self, branch_id: str) -> BenchBranch:
        for b in self.branches:
            if b.id == branch_id:
                return b
        raise BenchError(f"нет ветки {branch_id}: {[b.id for b in self.branches]}")

    def criteria_ids(self) -> list[str]:
        return [c.id for c in self.criteria]


# ---------------------------------------------------------------- дубль


class TakeInputs(BaseModel):
    frame: str  # относительно папки замера
    frame_sha256: str = ""
    reference: str = ""  # видео на входе: управляющее (comfy_control) или исходное (comfy_upscale)
    reference_sha256: str = ""
    refs: list[str] = Field(default_factory=list)  # картинки-референсы сцены, как ушли
    refs_sha256: list[str] = Field(default_factory=list)
    prompt: str
    negative: str = ""
    aspect_ratio: str
    duration_sec: int
    frames: int = 0
    fps: int = 0
    size: str = ""


class TakeMethod(BaseModel):
    method: str
    preset: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict)  # роль → файл весов
    weights: str = ""  # LoRA персонажа с пода, если есть
    pod_id: str = ""
    rate_usd_hr: float = 0.0


class TakeMetrics(BaseModel):
    """Автозамеры — диагностика, не оценка: энергия движения по окнам 0.5 с."""

    duration_sec: float = 0.0
    motion_mean: float = 0.0
    motion_median: float = 0.0
    motion_peak: float = 0.0
    quiet_share: float = 0.0  # доля окон ниже QUIET_SHARE от пика


class TakeRecord(BaseModel):
    take_id: str
    scene: str
    branch: str
    seed: int
    status: TakeStatus = "planned"
    error_kind: str = ""
    error: str = ""
    inputs: TakeInputs
    method: TakeMethod
    fingerprint: str = ""
    graph: str = ""  # относительно папки замера
    video: str = ""
    image: str = ""  # результат замера с output: image
    started_at: str = ""
    finished_at: str = ""
    gen_sec: float = 0.0
    cost_usd: float = 0.0
    estimate_usd: float = 0.0
    metrics: TakeMetrics | None = None


class Verdict(BaseModel):
    take_id: str
    reviewer: str = ""
    at: str = ""
    criteria: dict[str, VerdictValue] = Field(default_factory=dict)
    defect_times: list[float] = Field(default_factory=list)
    accepted_interval: tuple[float, float] | None = None
    notes: str = ""

    def accepted(self, criteria_ids: list[str]) -> bool | None:
        """False — хоть одно «нет» отклоняет сразу; None — оценён не по всем
        критериям или всё «не применимо»; True — все ответы даны и есть «да»."""
        values = [self.criteria.get(c) for c in criteria_ids]
        if "fail" in values:
            return False
        if any(v is None for v in values):
            return None
        return True if "pass" in values else None


class TakePaths(BaseModel):
    record: Path
    graph: Path
    video: Path
    image: Path
    verdict: Path

    def output(self, kind: str) -> Path:
        return self.image if kind == "image" else self.video


# ---------------------------------------------------------------- файлы


def bench_path(bench_id: str) -> Path:
    if "/" in bench_id or "\\" in bench_id or bench_id in (".", ".."):
        raise BenchError(f"негодный id замера: {bench_id!r}")
    return bench_dir() / bench_id


def list_benches() -> list[Path]:
    root = bench_dir()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / SPEC_FILE).is_file())


def load_spec(folder: Path) -> BenchSpec:
    path = folder / SPEC_FILE
    if not path.is_file():
        raise BenchError(f"нет {SPEC_FILE} в {folder}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise BenchError(f"{path}: YAML не читается: {exc}") from exc
    if not isinstance(data, dict):
        raise BenchError(f"{path}: ожидался словарь")
    data.setdefault("id", folder.name)
    try:
        return BenchSpec.model_validate(data)
    except ValidationError as exc:
        lines = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise BenchError(f"{path}: " + "; ".join(lines)) from exc


def missing_frames(folder: Path, spec: BenchSpec) -> dict[str, str]:
    """Сцена → путь кадра (или исходного клипа), которого нет. Кадр выбирает
    человек — схема без него читается, а запускать такую сцену нельзя."""
    out: dict[str, str] = {}
    for s in spec.scenes:
        for rel in (s.frame, s.video, *s.refs):
            if rel and not (folder / rel).is_file():
                out[s.id] = rel
                break
    return out


def take_id(scene_id: str, branch_id: str, seed: int) -> str:
    return f"{scene_id}_{branch_id}_s{seed}"


def take_paths(folder: Path, tid: str) -> TakePaths:
    d = folder / TAKES_DIR
    return TakePaths(
        record=d / f"{tid}.json",
        graph=d / f"{tid}.graph.json",
        video=d / f"{tid}.mp4",
        image=d / f"{tid}.png",
        verdict=d / f"{tid}.verdict.json",
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _dump(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def write_take(path: Path, take: TakeRecord) -> None:
    _atomic_write(path, _dump(take))


def read_take(path: Path) -> TakeRecord | None:
    if not path.is_file():
        return None
    try:
        return TakeRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError):
        return None


def write_graph(path: Path, graph: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(graph, ensure_ascii=False, indent=2) + "\n")


def write_verdict(path: Path, verdict: Verdict) -> None:
    _atomic_write(path, _dump(verdict))


def read_verdict(path: Path) -> Verdict | None:
    if not path.is_file():
        return None
    try:
        return Verdict.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError):
        return None


def verdict_fingerprint(path: Path) -> str:
    """Отпечаток файла вердикта: пульт шлёт тот, что читал; несовпадение —
    кто-то сохранил раньше, и его оценка не затирается (409)."""
    if not path.is_file():
        return ""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:16]


def load_takes(folder: Path, spec: BenchSpec) -> list[TakeRecord]:
    """Записи дублей в порядке схемы: сцена → ветка → сид."""
    out: list[TakeRecord] = []
    for scene in spec.scenes:
        for branch in spec.branches:
            for seed in spec.seeds:
                rec = read_take(take_paths(folder, take_id(scene.id, branch.id, seed)).record)
                if rec is not None:
                    out.append(rec)
    return out


def load_verdicts(folder: Path, takes: list[TakeRecord]) -> dict[str, Verdict]:
    out: dict[str, Verdict] = {}
    for t in takes:
        v = read_verdict(take_paths(folder, t.take_id).verdict)
        if v is not None:
            out[t.take_id] = v
    return out


# ---------------------------------------------------------------- отпечатки


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(inputs: TakeInputs, method: TakeMethod, graph: dict[str, Any]) -> str:
    """Кадр, промпт, метод, веса и граф целиком — всё, от чего зависит результат."""
    payload = {
        "inputs": inputs.model_dump(mode="json"),
        "method": method.model_dump(mode="json", exclude={"pod_id", "rate_usd_hr"}),
        "graph": graph,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
