"""Раннер анализа: те же правила, что у эпизодов, — ворота и бюджет.

Логика повторяет core.runner.run_episode без раскадровки и ProviderSet;
эпизодный раннер сознательно не обобщён — у него свой контекст и своя жизнь.
RunReport и select_stages общие.
"""

from __future__ import annotations

import logging
from pathlib import Path

from videoforge.core.manifest import load_manifest, now, save_manifest
from videoforge.core.runner import RunReport, select_stages
from videoforge.core.stage import RunOptions
from videoforge.core.workflow import load_workflow
from videoforge.intel.analysis import load_analysis_from_dir
from videoforge.intel.collectors.base import Collector
from videoforge.intel.stage import IntelContext
from videoforge.intel.stages import intel_stage_registry
from videoforge.providers.base import LlmProvider

log = logging.getLogger("videoforge.intel.runner")


def run_analysis(
    analysis_dir: Path,
    collector: Collector,
    options: RunOptions,
    llm: LlmProvider | None = None,
) -> RunReport:
    manifest = load_manifest(analysis_dir)
    config = load_analysis_from_dir(analysis_dir)
    workflow = load_workflow(manifest.workflow)
    registry = intel_stage_registry()
    report = RunReport()

    manifest.providers = {"collector": collector.name}
    if llm is not None:
        manifest.providers["llm"] = llm.name
    save_manifest(analysis_dir, manifest)

    for ws in select_stages(workflow, options):
        rec = manifest.record(ws.name)
        stage_cls = registry.get(ws.name)
        if stage_cls is None:
            rec.status = "failed"
            rec.error = f"стадия '{ws.name}' не реализована"
            save_manifest(analysis_dir, manifest)
            report.stopped_at, report.reason = ws.name, rec.error
            break

        stage = stage_cls()
        ctx = IntelContext(
            stage_name=ws.name,
            analysis_dir=analysis_dir,
            config=config,
            manifest=manifest,
            collector=collector,
            options=options,
            llm=llm,
        )

        estimate = stage.estimate_cost(ctx)
        if not options.ignore_budget and manifest.spent_usd + estimate > manifest.budget_usd:
            rec.status = "blocked"
            rec.message = (
                f"прогноз ${estimate:.2f} + потрачено ${manifest.spent_usd:.2f} "
                f"> бюджет ${manifest.budget_usd:.2f} (подними budget_usd в manifest.json "
                f"или --ignore-budget)"
            )
            save_manifest(analysis_dir, manifest)
            report.stopped_at, report.reason = ws.name, f"бюджет: {rec.message}"
            log.warning("[%s] заблокирована: %s", ws.name, rec.message)
            break

        log.info("[%s] старт (прогноз $%.2f)", ws.name, estimate)
        # тайминги настоящего прогона: пропуск их не затирает, иначе после
        # повторного run в манифесте не остаётся следов, сколько стадия шла
        was_started, was_finished = rec.started_at, rec.finished_at
        rec.status, rec.started_at, rec.finished_at, rec.error = "running", now(), None, None
        save_manifest(analysis_dir, manifest)
        try:
            result = stage.run(ctx)
        except Exception as exc:  # noqa: BLE001 — стадия упала, фиксируем и останавливаемся
            rec.status, rec.error, rec.finished_at = "failed", f"{type(exc).__name__}: {exc}", now()
            ctx.save()
            report.stopped_at, report.reason = ws.name, rec.error
            log.exception("[%s] упала", ws.name)
            if options.raise_errors:
                raise
            break

        rec.status, rec.message, rec.finished_at = result.status, result.message, now()
        if result.status == "skipped":
            rec.started_at, rec.finished_at = was_started, was_finished
        if result.changed and ws.name in manifest.approvals:
            manifest.approvals.remove(ws.name)
        ctx.save()
        report.executed.append((ws.name, result))
        log.info("[%s] %s — %s", ws.name, result.status, result.message)

        if result.status == "failed":
            report.stopped_at, report.reason = ws.name, result.message
            break

        if ws.gate_after and result.status == "done" and ws.name not in manifest.approvals:
            if options.auto_approve:
                manifest.approvals.append(ws.name)
                save_manifest(analysis_dir, manifest)
                continue
            manifest.awaiting_approval = ws.name
            save_manifest(analysis_dir, manifest)
            report.stopped_at = ws.name
            report.awaiting_approval = ws.name
            report.reason = f"результат стадии '{ws.name}' ждёт одобрения"
            break

    return report
