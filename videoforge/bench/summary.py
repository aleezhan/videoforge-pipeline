"""Сводка замера счётом, не статистикой.

По веткам: дублей, сделано, упало, оценено, принято, доля, деньги, принятые
секунды и цена принятой секунды. По критериям: сколько раз провален в каждой
ветке. Автозамеры против вердикта человека — средние у принятых и отклонённых
с числом примеров, без AUC: на 3–12 дублях он ничего не аттестует (Codex,
12.09.2026). Отрицательный или неопределённый итог — полноценный итог.
"""

from __future__ import annotations

from pathlib import Path
from statistics import mean
from typing import Any

from videoforge.bench.spec import (
    SUMMARY_FILE,
    BenchSpec,
    TakeRecord,
    Verdict,
    load_spec,
    load_takes,
    load_verdicts,
)

METRIC_TITLES = {
    "motion_median": "энергия движения, медиана",
    "motion_mean": "энергия движения, среднее",
    "quiet_share": "доля тихих окон",
}


def _accepted_seconds(take: TakeRecord, verdict: Verdict) -> float:
    if verdict.accepted_interval:
        a, b = verdict.accepted_interval
        return max(0.0, round(b - a, 2))
    if take.metrics and take.metrics.duration_sec:
        return take.metrics.duration_sec
    return float(take.inputs.duration_sec)


def summarize(
    spec: BenchSpec, takes: list[TakeRecord], verdicts: dict[str, Verdict]
) -> dict[str, Any]:
    ids = spec.criteria_ids()
    branches: list[dict[str, Any]] = []
    for b in spec.branches:
        mine = [t for t in takes if t.branch == b.id]
        done = [t for t in mine if t.status == "done"]
        failed = [t for t in mine if t.status == "failed"]
        reviewed: list[tuple[TakeRecord, Verdict, bool | None]] = []
        for t in done:
            v = verdicts.get(t.take_id)
            if v is not None:
                reviewed.append((t, v, v.accepted(ids)))
        accepted = [(t, v) for t, v, ok in reviewed if ok is True]
        rejected = [(t, v) for t, v, ok in reviewed if ok is False]
        partial = [t for t, _v, ok in reviewed if ok is None]
        cost = round(sum(t.cost_usd for t in mine), 4)
        acc_sec = round(sum(_accepted_seconds(t, v) for t, v in accepted), 2)
        fails_by_criterion = {
            c: sum(1 for _t, v, _ok in reviewed if v.criteria.get(c) == "fail") for c in ids
        }
        branches.append(
            {
                "id": b.id,
                "method": b.method,
                "preset": b.preset,
                "takes": len(mine),
                "done": len(done),
                "failed": len(failed),
                "error_kinds": sorted({t.error_kind for t in failed if t.error_kind}),
                "reviewed": len(reviewed),
                "partial": len(partial),
                "accepted": len(accepted),
                "rejected": len(rejected),
                "share": round(len(accepted) / len(reviewed), 2) if reviewed else None,
                "cost_usd": cost,
                "accepted_sec": acc_sec,
                "usd_per_accepted_sec": round(cost / acc_sec, 4) if acc_sec else None,
                "gen_sec_mean": round(mean(t.gen_sec for t in done), 1) if done else None,
                "fails": fails_by_criterion,
                "defect_times": sorted(
                    {round(x, 2) for _t, v, _ok in reviewed for x in v.defect_times}
                ),
            }
        )

    # лестница: сцена → ветка → принято/оценено и держится ли стоп-правило
    ladder: list[dict[str, Any]] = []
    for s in spec.scenes:
        row: dict[str, Any] = {"scene": s.id, "title": s.title, "branches": {}}
        for b in spec.branches:
            mine = [t for t in takes if t.scene == s.id and t.branch == b.id and t.status == "done"]
            oks = [verdicts[t.take_id].accepted(ids) for t in mine if t.take_id in verdicts]
            acc = sum(1 for ok in oks if ok is True)
            row["branches"][b.id] = {
                "done": len(mine),
                "reviewed": len(oks),
                "accepted": acc,
                "passes_rule": acc >= spec.stop_rule.min_accepted,
            }
        ladder.append(row)

    # автозамеры против вердикта: средние у принятых и отклонённых, с n
    metrics: list[dict[str, Any]] = []
    for key, title in METRIC_TITLES.items():
        acc_vals: list[float] = []
        rej_vals: list[float] = []
        for t in takes:
            if t.metrics is None or t.take_id not in verdicts:
                continue
            ok = verdicts[t.take_id].accepted(ids)
            value = float(getattr(t.metrics, key))
            if ok is True:
                acc_vals.append(value)
            elif ok is False:
                rej_vals.append(value)
        metrics.append(
            {
                "metric": key,
                "title": title,
                "accepted_n": len(acc_vals),
                "accepted_mean": round(mean(acc_vals), 3) if acc_vals else None,
                "rejected_n": len(rej_vals),
                "rejected_mean": round(mean(rej_vals), 3) if rej_vals else None,
            }
        )

    spent = round(sum(t.cost_usd for t in takes), 4)
    return {
        "id": spec.id,
        "question": spec.question,
        "answer": spec.answer,
        "compare": spec.compare,
        "budget_usd": spec.budget_usd,
        "spent_usd": spent,
        "takes": len(takes),
        "done": sum(1 for t in takes if t.status == "done"),
        "failed": sum(1 for t in takes if t.status == "failed"),
        "reviewed": sum(1 for t in takes if t.take_id in verdicts),
        "accepted": sum(
            1
            for t in takes
            if verdicts.get(t.take_id, None) is not None
            and verdicts[t.take_id].accepted(ids) is True
        ),
        "branches": branches,
        "ladder": ladder,
        "metrics": metrics,
        "criteria": [{"id": c.id, "title": c.title} for c in spec.criteria],
    }


def _num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(s: dict[str, Any]) -> str:
    lines = [
        f"# {s['id']} — сводка",
        "",
        f"**Вопрос.** {s['question']}",
        "",
        f"Дублей {s['takes']}: сделано {s['done']}, упало {s['failed']}, оценено {s['reviewed']}, "
        f"принято {s['accepted']}. Потрачено ${s['spent_usd']:.3f} из ${s['budget_usd']:.2f}. "
        f"Сравнение: {'одна причина' if s['compare'] == 'cause' else 'метод против метода'}.",
        "",
        "## По веткам",
        "",
        "| ветка | метод | дублей | сделано | упало | оценено | принято | доля | $ "
        "| принятых с | $/принятая с | с/дубль |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for b in s["branches"]:
        fails = ", ".join(f"{k} {v}" for k, v in b["fails"].items() if v)
        lines.append(
            f"| {b['id']} | {b['method']} {b['preset']}".rstrip()
            + f" | {b['takes']} | {b['done']} | {b['failed']}"
            + (f" ({', '.join(b['error_kinds'])})" if b["error_kinds"] else "")
            + f" | {b['reviewed']}"
            + (f" (+{b['partial']} не до конца)" if b["partial"] else "")
            + f" | {b['accepted']} | {_num(b['share'])} | {b['cost_usd']:.3f}"
            + f" | {b['accepted_sec']:.1f} | {_num(b['usd_per_accepted_sec'], 4)}"
            + f" | {_num(b['gen_sec_mean'], 1)} |"
        )
        if fails:
            lines.append(f"|  | провалы по критериям: {fails} | | | | | | | | | | |")
    lines += [
        "",
        "## Лестница",
        "",
        "| сцена | " + " | ".join(b["id"] for b in s["branches"]) + " |",
        "|---|" + "---|" * len(s["branches"]),
    ]
    for row in s["ladder"]:
        cells = []
        for b in s["branches"]:
            r = row["branches"][b["id"]]
            mark = "✓" if r["passes_rule"] else ""
            cells.append(f"{r['accepted']}/{r['reviewed']} из {r['done']} {mark}".strip())
        lines.append(f"| {row['scene']} {row['title']}".rstrip() + " | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Автозамеры против вердикта человека",
        "",
        "| замер | принятые (n) | отклонённые (n) |",
        "|---|---|---|",
    ]
    for m in s["metrics"]:
        lines.append(
            f"| {m['title']} | {_num(m['accepted_mean'], 3)} ({m['accepted_n']}) "
            f"| {_num(m['rejected_mean'], 3)} ({m['rejected_n']}) |"
        )
    lines += [
        "",
        "Числа — счёт, не аттестат: порог для детектора подбирается на одной сцене и "
        "проверяется на другой; энергия движения — диагностика, не качество.",
    ]
    if s["answer"]:
        lines += ["", f"**Ответ.** {s['answer']}"]
    return "\n".join(lines) + "\n"


def write_summary(folder: Path) -> tuple[Path, dict[str, Any]]:
    spec = load_spec(folder)
    takes = load_takes(folder, spec)
    verdicts = load_verdicts(folder, takes)
    summary = summarize(spec, takes, verdicts)
    path = folder / SUMMARY_FILE
    path.write_text(render_markdown(summary), encoding="utf-8")
    return path, summary
