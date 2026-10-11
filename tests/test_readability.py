"""Читаемость без звука: модель описывает кадр вслепую, python сверяет с
репликой; загадки — повтор планнера, потом предупреждение на ворота."""

from __future__ import annotations

from pathlib import Path

from videoforge.core.manifest import load_manifest
from videoforge.core.project import ProjectSpec, create_episode, create_project
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, SceneOverlay, load_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.providers.mock import MockLlm
from videoforge.stages.readability import (
    ReadabilityFinding,
    format_block,
    key_roots,
    related_by_roots,
    scenes_to_check,
)

PLANNER = RunOptions(auto_approve=True, raise_errors=True, only="planner")


def test_roots_match_without_lemmatisation() -> None:
    """«могилы» и «могиле» — один корень; служебные слова корнями не становятся."""
    assert key_roots("Что происходит, когда вас опускают в могилу?") == {"прои", "опус", "моги"}
    assert key_roots("когда тогда если чтобы") == set()
    assert related_by_roots("Его опускают в могилу", ["могила", "земля", "тьма"])
    assert not related_by_roots("Его опускают в могилу", ["лента", "пламя", "книга"])
    # текст плашки — на стороне увиденного: зритель его прочитал
    assert related_by_roots("Его опускают в могилу", ["свет", "тьма"], overlay_text="Могила")
    assert not related_by_roots("Его опускают в могилу", [])


def test_only_spoken_beats_are_checked() -> None:
    scenes = [
        Scene(id="s1", order=1, narration="речь", visual_prompt="a"),
        Scene(id="s2", order=2, narration="речь", visual_prompt="a", continues="s1"),
        Scene(id="s3", order=3, narration="", visual_prompt="a"),
        Scene(id="s4", order=4, narration="речь", visual_prompt=""),
    ]
    assert [s.id for s in scenes_to_check(scenes)] == ["s1"]


def test_block_text_names_the_scene_what_is_heard_and_seen() -> None:
    text = format_block(
        [ReadabilityFinding("s12", "На камне надпись.", ["борозды", "камень"], "покажи надпись")]
    )
    assert "s12" in text and "надпись" in text and "борозды" in text and "NOUN" in text


def _episode(projects_root: Path) -> Path:
    create_project(
        ProjectSpec(
            id="p",
            name="Планнер",
            platforms=["youtube_shorts"],
            character="timur",
            style="pixar3d",
            voice="ru_male",
            target_duration_sec=30,
            scene_duration_sec=6.0,
        )
    )
    return create_episode("p", "e1", title="проба", brief="мальчик находит старую карту")


def _llm(providers: ProviderSet) -> MockLlm:
    assert isinstance(providers.llm, MockLlm)
    return providers.llm


def test_readable_storyboard_passes_with_one_planner_call(
    projects_root: Path, mock_providers: ProviderSet
) -> None:
    edir = _episode(projects_root)
    report = run_episode(edir, mock_providers, PLANNER)
    assert report.ok, report.reason
    llm = _llm(mock_providers)
    assert llm.planner_calls == 1 and llm.readability_calls == 1
    sb = load_storyboard(edir)
    assert not any("readability" in s.checks.notes for s in sb.scenes)
    message = load_manifest(edir).stages["planner"].message
    assert "читаемость: 5/5" in message and "ПРОВЕРЬ" not in message
    # заглушка планнера пишет плашки трёх видов; продолжение длинного бита
    # копирует свою (ярлык живёт на всех сценах плана), отсюда четвёртая
    assert "плашек: 4" in message
    assert sb.scenes[0].overlay == SceneOverlay(text="ПУНКТ 1", kind="label")


def test_riddles_send_the_planner_back_once(
    projects_root: Path, mock_providers: ProviderSet
) -> None:
    """Первая раскадровка — загадка: планнер получает причины и отвечает
    второй раз; вторая читается — заметок нет."""
    edir = _episode(projects_root)
    llm = _llm(mock_providers)
    llm.readability_riddle_calls = 1
    report = run_episode(edir, mock_providers, PLANNER)
    assert report.ok, report.reason
    assert llm.planner_calls == 2 and llm.readability_calls == 2
    sb = load_storyboard(edir)
    assert not any("readability" in s.checks.notes for s in sb.scenes)
    assert "читаемость: 5/5" in load_manifest(edir).stages["planner"].message


def test_a_worse_retry_is_dropped_and_the_rest_goes_to_the_gate(
    projects_root: Path, mock_providers: ProviderSet
) -> None:
    """Повтор читается хуже первой попытки (одна загадка → все сцены) —
    остаётся первая, как на ep001 (6 → 12 загадок); стадия не падает: заметка
    на сцене и «ПРОВЕРЬ» в сообщении ворот, решает человек."""
    edir = _episode(projects_root)
    llm = _llm(mock_providers)
    llm.readability_riddle_calls = 2
    report = run_episode(edir, mock_providers, PLANNER)
    assert report.ok, report.reason
    assert llm.planner_calls == 2 and llm.readability_calls == 2
    sb = load_storyboard(edir)
    flagged = [s for s in sb.scenes if "readability" in s.checks.notes]
    assert [s.id for s in flagged] == ["s01"]  # первая попытка, а не вторая с тремя
    assert "дым, туман, пустота" in flagged[0].checks.notes["readability"]
    message = load_manifest(edir).stages["planner"].message
    assert "ПРОВЕРЬ: без звука не читаются s01" in message
    assert "читаемость: 4/5" in message
