"""Benchmark sandbox (projects/_bench) for the UI: documents, tables, test outputs.

The sandbox is not a pipeline: it has no contract, scripts and files change from test to
test, and any result schema would be stale by the next measurement. So the UI shows the
folder as is: markdown documents are rendered, CSVs are read as tables, and images and
clips from `out/<test>/` are shown as a gallery per subfolder.
Two "smart" parts. A summary of `runs.csv` (hosted calls: count, cost, time, models per
test) - every sandbox script writes this file in the same format. And `tests.yaml` - the
test card a person writes when closing a test: question, answer, GPU (pod, minutes, $),
which models took part. Sandbox spending is hosted cost from the CSV plus GPU cost from
the cards: either sum without the other is misleading (198 calls for $6.23 while pods
ate another $7).
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from videoforge.api.data import NotFound, file_entry, file_url, kind_of
from videoforge.bench import spec as bspec
from videoforge.bench.summary import summarize
from videoforge.paths import bench_dir

log = logging.getLogger("videoforge.api.bench")

MAX_ROWS = 2000  # table rows per response; for more, the person opens the file
MAX_TEXT = 2_000_000  # bytes of text per response
OUT_DIR = "out"
RUNS_FILE = "runs.csv"
TESTS_FILE = "tests.yaml"
FIRST_DOCS = ("README.md", "LOG.md")  # list order: the map first, then the log


def _root() -> Path:
    return bench_dir()


def _rel(path: Path) -> str:
    return path.resolve().relative_to(_root().resolve()).as_posix()


def file_info(path: Path) -> dict[str, Any]:
    return {**file_entry(path), "rel": _rel(path)}


def resolve_inside(rel: str) -> Path:
    """A path inside the sandbox; escaping it raises NotFound, never returns a file."""
    base = _root().resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise NotFound(f"путь вне полигона: {rel}")
    if not target.exists():
        raise NotFound(f"нет такого файла: {rel}")
    return target


# ---------------------------------------------------------------- tables


def read_csv(path: Path, limit: int = MAX_ROWS) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            columns = next(reader)
        except StopIteration:
            return {"columns": [], "rows": [], "total": 0, "truncated": False}
        rows: list[list[str]] = []
        total = 0
        for row in reader:
            total += 1
            if len(rows) < limit:
                rows.append(row)
    return {"columns": columns, "rows": rows, "total": total, "truncated": total > len(rows)}


def _count_rows(path: Path) -> int:
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            return max(0, sum(1 for _ in csv.reader(fh)) - 1)
    except OSError:
        return 0


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def runs_summary(path: Path) -> list[dict[str, Any]]:
    """runs.csv -> per test: calls, completed, total $, time, dates and how many times
    each endpoint (model) was called."""
    if not path.exists():
        return []
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "completed": 0,
            "cost_usd": 0.0,
            "wall": [],
            "first": "",
            "last": "",
            "endpoints": defaultdict(lambda: {"calls": 0, "cost_usd": 0.0}),
        }
    )
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                test = (row.get("test") or "").strip() or "—"
                g = groups[test]
                g["calls"] += 1
                if (row.get("status") or "").upper() == "COMPLETED":
                    g["completed"] += 1
                cost = _float(row.get("cost_usd")) or 0.0
                g["cost_usd"] += cost
                wall = _float(row.get("wall_s"))
                if wall is not None:
                    g["wall"].append(wall)
                ts = (row.get("ts") or "").strip()
                if ts:
                    g["first"] = min(g["first"] or ts, ts)
                    g["last"] = max(g["last"], ts)
                endpoint = (row.get("endpoint") or "").strip()
                if endpoint:
                    g["endpoints"][endpoint]["calls"] += 1
                    g["endpoints"][endpoint]["cost_usd"] += cost
    except OSError as exc:
        log.warning("%s не читается: %s", path, exc)
        return []
    out = []
    for test, g in groups.items():
        wall = g["wall"]
        endpoints = sorted(
            (
                {"id": eid, "calls": e["calls"], "cost_usd": round(e["cost_usd"], 4)}
                for eid, e in g["endpoints"].items()
            ),
            key=lambda e: (-e["calls"], e["id"]),
        )
        out.append(
            {
                "test": test,
                "calls": g["calls"],
                "completed": g["completed"],
                "cost_usd": round(g["cost_usd"], 4),
                "wall_s": round(sum(wall), 1),
                "wall_mean_s": round(sum(wall) / len(wall), 1) if wall else None,
                "first": g["first"] or None,
                "last": g["last"] or None,
                "endpoints": endpoints,
            }
        )
    return sorted(out, key=lambda r: r["test"])


def tests_meta(path: Path) -> dict[str, dict[str, Any]]:
    """tests.yaml -> {test id: card}. Missing or broken file -> empty: the dashboard
    shows folders as is, without question and answer."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("%s не разобран: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _gpu_totals(meta: dict[str, dict[str, Any]]) -> tuple[float, float]:
    """GPU minutes and cost across all cards."""
    minutes = 0.0
    cost = 0.0
    for card in meta.values():
        gpu = card.get("gpu")
        if not isinstance(gpu, dict):
            continue
        minutes += _float(str(gpu.get("minutes", ""))) or 0.0
        cost += _float(str(gpu.get("cost_usd", ""))) or 0.0
    return round(minutes, 1), round(cost, 2)


# ---------------------------------------------------------------- overview


def _sorted_docs(paths: list[Path]) -> list[Path]:
    order = {name: i for i, name in enumerate(FIRST_DOCS)}
    return sorted(paths, key=lambda p: (order.get(p.name, len(order)), p.name.lower()))


def _counts(folder: Path) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for p in folder.rglob("*"):
        if p.is_file():
            counts[kind_of(p)] += 1
    return dict(counts)


def _latest_mtime(folder: Path) -> datetime | None:
    latest: float | None = None
    for p in folder.rglob("*"):
        if p.is_file():
            m = p.stat().st_mtime
            latest = m if latest is None or m > latest else latest
    return datetime.fromtimestamp(latest, UTC) if latest is not None else None


def bench_overview() -> dict[str, Any]:
    root = _root()
    if not root.is_dir():
        return {
            "exists": False,
            "path": str(root),
            "docs": [],
            "tables": [],
            "reports": [],
            "tests": [],
            "runs": [],
            "totals": {
                "calls": 0,
                "hosted_usd": 0.0,
                "gpu_minutes": 0.0,
                "gpu_usd": 0.0,
                "cost_usd": 0.0,
                "tests": 0,
            },
        }
    docs = [file_info(p) for p in _sorted_docs([p for p in root.glob("*.md") if p.is_file()])]
    tables = []
    for p in sorted(root.glob("*.csv")):
        info = file_info(p)
        info["rows"] = _count_rows(p)
        tables.append(info)
    reports = [file_info(p) for p in sorted(root.glob("*.html"))]
    out = root / OUT_DIR
    if out.is_dir():
        reports += [file_info(p) for p in sorted(out.glob("*.html"))]
    runs = runs_summary(root / RUNS_FILE)
    hosted_by_test = {r["test"]: r for r in runs}
    meta = tests_meta(root / TESTS_FILE)
    tests = []
    if out.is_dir():
        for d in sorted(p for p in out.iterdir() if p.is_dir()):
            tests.append(
                {
                    "id": d.name,
                    "rel": _rel(d),
                    "counts": _counts(d),
                    "mtime": _latest_mtime(d),
                    "docs": [file_info(p) for p in _sorted_docs(sorted(d.glob("*.md")))],
                    "reports": [file_info(p) for p in sorted(d.glob("*.html"))],
                    "meta": meta.get(d.name),
                    "hosted": hosted_by_test.get(d.name),
                }
            )
    hosted_usd = round(sum(r["cost_usd"] for r in runs), 2)
    gpu_minutes, gpu_usd = _gpu_totals(meta)
    benches = benches_overview()
    bench_usd = round(sum(float(b.get("spent_usd") or 0.0) for b in benches), 2)
    return {
        "exists": True,
        "path": str(root),
        "docs": docs,
        "tables": tables,
        "reports": reports,
        "tests": tests,
        "benches": benches,
        "runs": runs,
        "totals": {
            "calls": sum(r["calls"] for r in runs),
            "hosted_usd": hosted_usd,
            "gpu_minutes": gpu_minutes,
            "gpu_usd": gpu_usd,
            "bench_usd": bench_usd,
            "cost_usd": round(hosted_usd + gpu_usd + bench_usd, 2),
            "tests": len(tests),
            "benches": len(benches),
        },
    }


# ---------------------------------------------------------------- spec-driven benchmarks


class Conflict(ValueError):
    """A verdict was already saved from another tab; we never overwrite it (409)."""


def benches_overview() -> list[dict[str, Any]]:
    """Spec-driven benchmark cards: question, budget and spend, takes/rated/accepted."""
    items: list[dict[str, Any]] = []
    for folder in bspec.list_benches():
        try:
            spec = bspec.load_spec(folder)
        except bspec.BenchError as exc:
            items.append({"id": folder.name, "rel": _rel(folder), "error": str(exc)})
            continue
        takes = bspec.load_takes(folder, spec)
        verdicts = bspec.load_verdicts(folder, takes)
        s = summarize(spec, takes, verdicts)
        items.append(
            {
                "id": spec.id,
                "rel": _rel(folder),
                "error": "",
                "question": spec.question,
                "answer": spec.answer,
                "date": spec.date,
                "compare": spec.compare,
                "budget_usd": spec.budget_usd,
                "spent_usd": s["spent_usd"],
                "takes": s["takes"],
                "done": s["done"],
                "failed": s["failed"],
                "reviewed": s["reviewed"],
                "accepted": s["accepted"],
                "branches": [b.id for b in spec.branches],
                "scenes": len(spec.scenes),
                "missing_frames": sorted(bspec.missing_frames(folder, spec)),
                "mtime": _latest_mtime(folder),
            }
        )
    return items


def _bench_folder(bench_id: str) -> Path:
    try:
        folder = bspec.bench_path(bench_id)
    except bspec.BenchError as exc:
        raise NotFound(str(exc)) from exc
    if not (folder / bspec.SPEC_FILE).is_file():
        raise NotFound(f"нет такого замера: {bench_id}")
    return folder


def bench_detail(bench_id: str) -> dict[str, Any]:
    """The whole benchmark: spec, scenes with frames, takes with FULL provenance
    (frame, prompt, graph, weights, seconds, cost, auto-metrics), verdicts, summary."""
    folder = _bench_folder(bench_id)
    try:
        spec = bspec.load_spec(folder)
    except bspec.BenchError as exc:
        raise NotFound(str(exc)) from exc
    takes = bspec.load_takes(folder, spec)
    verdicts = bspec.load_verdicts(folder, takes)
    ids = spec.criteria_ids()
    absent = bspec.missing_frames(folder, spec)
    scenes = []
    for s in spec.scenes:
        frame = folder / s.frame
        video = folder / s.video if s.video else None
        scenes.append(
            {
                **s.model_dump(mode="json"),
                "frame_url": file_url(frame) if frame.is_file() else None,
                "video_url": file_url(video) if video and video.is_file() else None,
                "missing": s.id in absent,
            }
        )
    out_takes = []
    for t in takes:
        paths = bspec.take_paths(folder, t.take_id)
        v = verdicts.get(t.take_id)
        frame = folder / t.inputs.frame
        ref = folder / t.inputs.reference if t.inputs.reference else None
        out_takes.append(
            {
                **t.model_dump(mode="json"),
                "video_url": file_url(paths.video) if paths.video.is_file() else None,
                "image_url": file_url(paths.image) if paths.image.is_file() else None,
                "frame_url": file_url(frame) if frame.is_file() else None,
                "ref_urls": [file_url(folder / r) for r in t.inputs.refs if (folder / r).is_file()],
                # input video (control or source): without it "how it was made" is incomplete
                "reference_url": file_url(ref) if ref and ref.is_file() else None,
                "graph_url": file_url(paths.graph) if paths.graph.is_file() else None,
                "graph_rel": _rel(paths.graph) if paths.graph.is_file() else None,
                "verdict": v.model_dump(mode="json") if v else None,
                "verdict_base": bspec.verdict_fingerprint(paths.verdict),
                "accepted": v.accepted(ids) if v else None,
            }
        )
    summary_path = folder / bspec.SUMMARY_FILE
    return {
        "id": spec.id,
        "rel": _rel(folder),
        "spec": spec.model_dump(mode="json"),
        "scenes": scenes,
        "takes": out_takes,
        "summary": summarize(spec, takes, verdicts),
        "summary_md": summary_path.read_text(encoding="utf-8") if summary_path.is_file() else "",
    }


def write_verdict(bench_id: str, take_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Human verdict -> `takes/<take>.verdict.json`. `base` is the fingerprint of the file
    the dashboard read: a mismatch means someone saved first -> Conflict."""
    folder = _bench_folder(bench_id)
    try:
        spec = bspec.load_spec(folder)
    except bspec.BenchError as exc:
        raise NotFound(str(exc)) from exc
    if "/" in take_id or "\\" in take_id:
        raise NotFound(f"негодный id дубля: {take_id}")
    paths = bspec.take_paths(folder, take_id)
    if bspec.read_take(paths.record) is None:
        raise NotFound(f"нет такого дубля: {take_id}")
    current = bspec.verdict_fingerprint(paths.verdict)
    if str(payload.get("base") or "") != current:
        raise Conflict("оценку этого дубля уже сохранили с другой вкладки — обнови страницу")
    ids = spec.criteria_ids()
    criteria: dict[str, str] = {}
    for key, value in (payload.get("criteria") or {}).items():
        if key not in ids:
            raise ValueError(f"нет критерия {key}: {ids}")
        if value not in ("pass", "fail", "na"):
            raise ValueError(f"{key}: оценка pass | fail | na, не {value!r}")
        criteria[key] = value
    interval = payload.get("accepted_interval")
    verdict = bspec.Verdict(
        take_id=take_id,
        reviewer=str(payload.get("reviewer") or ""),
        at=bspec.now_iso(),
        criteria=criteria,  # type: ignore[arg-type]
        defect_times=[float(x) for x in payload.get("defect_times") or []],
        accepted_interval=(float(interval[0]), float(interval[1])) if interval else None,
        notes=str(payload.get("notes") or ""),
    )
    bspec.write_verdict(paths.verdict, verdict)
    return {
        "verdict": verdict.model_dump(mode="json"),
        "base": bspec.verdict_fingerprint(paths.verdict),
        "accepted": verdict.accepted(ids),
    }


def bench_test(test_id: str) -> dict[str, Any]:
    if "/" in test_id or "\\" in test_id or test_id in (".", ".."):
        raise NotFound(f"нет такого теста: {test_id}")
    folder = _root() / OUT_DIR / test_id
    if not folder.is_dir():
        raise NotFound(f"нет такого теста: {test_id}")
    groups: list[dict[str, Any]] = []
    for d in sorted([folder, *(p for p in folder.rglob("*") if p.is_dir())]):
        files = sorted((p for p in d.iterdir() if p.is_file()), key=lambda p: p.name.lower())
        if not files:
            continue
        group: dict[str, Any] = {
            "dir": d.relative_to(folder).as_posix() if d != folder else "",
            "images": [],
            "videos": [],
            "audio": [],
            "docs": [],
            "tables": [],
            "other": [],
        }
        for p in files:
            info = file_info(p)
            kind = info["kind"]
            if kind == "image":
                group["images"].append(info)
            elif kind == "video":
                group["videos"].append(info)
            elif kind == "audio":
                group["audio"].append(info)
            elif kind == "doc":
                # short documents are sent inline: a test summary is read in place
                if p.stat().st_size <= 200_000:
                    info["text"] = p.read_text(encoding="utf-8", errors="replace")
                group["docs"].append(info)
            elif kind == "table":
                info["table"] = read_csv(p, limit=500)
                group["tables"].append(info)
            else:
                group["other"].append(info)
        groups.append(group)
    return {"id": test_id, "rel": _rel(folder), "groups": groups, "counts": _counts(folder)}


def bench_text(rel: str) -> dict[str, Any]:
    """A sandbox document, table or text file by relative path."""
    path = resolve_inside(rel)
    if not path.is_file():
        raise NotFound(f"не файл: {rel}")
    info = file_info(path)
    kind = info["kind"]
    if kind == "table":
        info["table"] = read_csv(path)
        return info
    if kind in ("doc", "text"):
        if path.stat().st_size > MAX_TEXT:
            raise NotFound(f"файл слишком большой для показа: {rel}")
        info["text"] = path.read_text(encoding="utf-8", errors="replace")
        return info
    raise NotFound(f"не текстовый файл: {rel} — открой по ссылке")
