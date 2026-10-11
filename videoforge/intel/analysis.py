"""Анализ как единица работы: research/<slug>/ с analysis.yaml и manifest.json.

Аналог эпизода: манифест (тот же Manifest — статусы, ворота, бюджет, журнал
расходов) плюс конфиг, который правит человек. Артефакты стадий лежат рядом.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from videoforge.core.ffmpeg import SCENE_THRESHOLD
from videoforge.core.manifest import Manifest, now, save_manifest
from videoforge.core.templates import render, yaml_str
from videoforge.core.workflow import load_workflow
from videoforge.intel.models import AnalysisConfig, AnalysisMode
from videoforge.paths import analysis_dir, research_dir

ANALYSIS_FILE = "analysis.yaml"

# Режим → workflow. Отсутствие в карте = режим ещё не реализован.
WORKFLOW_BY_MODE: dict[str, str] = {"channel": "analyze_channel"}
_MODE_PLAN = {"video": "фаза 3", "competitors": "фаза 4"}


@dataclass
class AnalysisSpec:
    """Ответы опроса /analyze-* — ровно то, что нужно шаблону analysis.yaml."""

    id: str
    urls: list[str] = field(default_factory=list)
    mode: AnalysisMode = "channel"
    collector: str = "ytdlp"
    goal: str = ""
    language: str = "ru"
    max_videos: int = 0
    outlier_threshold: float | Literal["auto"] = "auto"
    deep_top: int = 5
    deep_worst: int = 2
    deep_videos: list[str] = field(default_factory=list)
    scene_threshold: float = SCENE_THRESHOLD
    budget_usd: float = 3.0  # уходит в manifest.json — его читает бюджетный страж


def create_analysis(spec: AnalysisSpec) -> Path:
    adir = analysis_dir(spec.id)
    if (adir / ANALYSIS_FILE).exists():
        raise FileExistsError(f"анализ уже существует: {adir / ANALYSIS_FILE}")
    workflow = WORKFLOW_BY_MODE.get(spec.mode)
    if workflow is None:
        raise ValueError(
            f"режим '{spec.mode}' ещё не реализован ({_MODE_PLAN[spec.mode]}); пока channel"
        )
    load_workflow(workflow)  # проверка, что такой workflow есть
    values = {
        "id": spec.id,
        "mode": spec.mode,
        "collector": spec.collector,
        "urls": "\n".join(f"  - {yaml_str(u)}" for u in spec.urls),
        "goal": yaml_str(spec.goal),
        "language": spec.language,
        "max_videos": spec.max_videos,
        "outlier_threshold": spec.outlier_threshold,
        "deep_top": spec.deep_top,
        "deep_worst": spec.deep_worst,
        "deep_videos": json.dumps(spec.deep_videos),  # JSON-список — валидный inline-YAML
        "scene_threshold": spec.scene_threshold,
    }
    adir.mkdir(parents=True, exist_ok=True)
    (adir / ANALYSIS_FILE).write_text(render("analysis.yaml", values), encoding="utf-8")
    load_analysis_from_dir(adir)  # проверка: шаблон отрендерился в валидный YAML
    save_manifest(
        adir,
        Manifest(
            project_id=spec.id,
            workflow=workflow,
            created_at=now(),
            budget_usd=spec.budget_usd,
        ),
    )
    return adir


def load_analysis_from_dir(adir: Path) -> AnalysisConfig:
    path = adir / ANALYSIS_FILE
    if not path.exists():
        raise FileNotFoundError(f"анализ не найден: нет {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("id", adir.name)
    return AnalysisConfig.model_validate(data)


def resolve_analysis(slug: str) -> Path:
    adir = analysis_dir(slug)
    if not (adir / ANALYSIS_FILE).exists():
        raise FileNotFoundError(f"анализа '{slug}' нет: {adir}")
    return adir


def list_analyses() -> list[str]:
    base = research_dir()
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if (p / ANALYSIS_FILE).exists())
