"""Прогноз стоимости без запуска и HTML-отчёт эпизода."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.config import ProviderNames
from videoforge.core.manifest import load_manifest
from videoforge.core.report import REPORT_FILE, render_report
from videoforge.core.runner import estimate_episode, run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard
from videoforge.core.workflow import load_workflow
from videoforge.providers.base import ProviderSet
from videoforge.providers.registry import build_providers, missing_keys


def test_estimate_covers_workflow_and_writes_nothing(
    episode: Path, mock_providers: ProviderSet
) -> None:
    mock_providers.image.price_usd = 0.5
    before = (episode / "manifest.json").read_bytes()
    rows = estimate_episode(episode, mock_providers)
    assert [r.stage for r in rows] == load_workflow("ai_video").stage_names()
    by = {r.stage: r.cost_usd for r in rows}
    assert by["images"] == 1.0  # 2 сцены × $0.5
    assert by["video"] == 0.0  # mock
    assert (episode / "manifest.json").read_bytes() == before  # ничего не записано


def test_estimate_counts_only_remaining(episode: Path, mock_providers: ProviderSet) -> None:
    run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, raise_errors=True)
    )
    mock_providers.image.price_usd = 0.5
    by = {r.stage: r.cost_usd for r in estimate_episode(episode, mock_providers)}
    assert by["images"] == 0.0  # картинки готовы — переделывать нечего


def test_real_providers_build_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ключей провайдеры строятся и отдают цены — прогноз работает до .env."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("FAL_KEY", raising=False)
    names = ProviderNames(image="gemini", video="fal", tts="edge", llm="gemini")
    providers = build_providers(names)
    assert providers.image.price_usd > 0
    assert providers.video.price_for(6) > 0
    assert missing_keys(names.as_dict().values()) == ["FAL_KEY", "GEMINI_API_KEY"]


@pytest.mark.render
def test_report_html(episode: Path, mock_providers: ProviderSet) -> None:
    run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    sb = load_storyboard(episode)
    path = render_report(
        episode, sb, load_manifest(episode), estimates={"video": 0.2, "publish": None}
    )
    assert path == episode / REPORT_FILE
    text = path.read_text(encoding="utf-8")
    assert "s1" in text and "boy at the forest edge" in text  # сцена и её промпт
    assert sb.final.video and sb.final.video in text  # ссылка на итоговое видео
    assert "не реализована" in text  # publish без стадии
    assert "$0.200" in text  # прогноз попал в таблицу стадий


@pytest.mark.render
def test_report_has_a_storyboard_sheet(episode: Path, mock_providers: ProviderSet) -> None:
    """Ворота проходят глазами по листу, а не вычитыванием JSON."""
    run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    sb = load_storyboard(episode)
    text = render_report(episode, sb, load_manifest(episode)).read_text(encoding="utf-8")

    assert "Лист раскадровки" in text
    assert text.count('class="cell"') == len(sb.scenes)  # по ячейке на бит
    assert "хронометраж" in text and "длинных планов" in text
    # замеры готового файла доехали до отчёта
    assert "звук минус картинка" in text


def test_sheet_shows_the_shape_before_any_image_exists(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """До стадии картинок лист всё равно показывает форму эпизода — по тексту кадра."""
    run_episode(
        episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True, only="audio")
    )
    sb = load_storyboard(episode)
    text = render_report(episode, sb, load_manifest(episode)).read_text(encoding="utf-8")
    assert "Лист раскадровки" in text
    assert "boy at the forest edge" in text


def test_sheet_joins_the_parts_of_one_beat(projects_root: Path) -> None:
    """Один план, разрезанный по потолку провайдера, — одна ячейка, не три."""
    from videoforge.core.report import _beats
    from videoforge.core.storyboard import Scene

    scenes = [
        Scene(id="s01", order=1, duration_sec=6.0),
        Scene(id="s01_2", order=2, duration_sec=6.0, continues="s01"),
        Scene(id="s01_3", order=3, duration_sec=6.0, continues="s01_2"),
        Scene(id="s02", order=4, duration_sec=4.0),
    ]
    beats = _beats(scenes)
    assert [len(b) for b in beats] == [3, 1]
