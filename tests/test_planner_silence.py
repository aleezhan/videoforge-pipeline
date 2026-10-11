"""Тишина рождается в раскадровке — и пресекается там же, до денег.

Замер research/_probes/speech_pauses_2026-09-16: у ep001 52% тишины, у
конкурентов 0–1.8%. 112 с из неё дала раскадровка: промпт планнера разрешал
немые сцены и звал длинные сцены «выдохами» (четыре немые сцены по 15 с), а
разбиение бита из одного предложения оставляло второй клип немым (15 половинок,
52 с). Что здесь держится: промпт не приглашает немых сцен, немая сцена — брак
ответа с одним повтором (кроме вспышки до первого слова и конечной заставки),
разбиение не оставляет немых половинок, а немые серии видны на воротах.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core.manifest import load_manifest
from videoforge.core.project import ProjectSpec, create_episode, create_project
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, load_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.providers.mock import MockLlm
from videoforge.stages.planner import (
    _PROMPT,
    SILENT_RUN_LIMIT_SEC,
    parse_scenes,
    silence_findings,
    split_narration,
    time_budget,
)


def _row(n: int, narration: str, seconds: float) -> dict[str, object]:
    return {
        "narration": narration,
        "visual_prompt": f"a boy on hill {n}",
        "motion_prompt": f"pan {n}",
        "duration_sec": seconds,
    }


# ---------------------------------------------------------------- промпт


def test_the_prompt_no_longer_invites_mute_scenes() -> None:
    assert "may have empty narration" not in _PROMPT
    assert "The long scenes are usually these" not in _PROMPT
    assert "breaths" not in _PROMPT and "payoff holds" not in _PROMPT
    # длинная сцена — кадр под речью, и слова названы числом
    assert "the voice does not wait for the picture" in _PROMPT
    assert "words of narration" in time_budget(target_sec=600, long_sec=15)


# ---------------------------------------------------------------- разбор ответа


def test_a_mute_scene_in_the_middle_is_a_rejected_answer() -> None:
    raw = [_row(1, "раз два три", 5), _row(2, "", 15), _row(3, "четыре пять", 5)]
    with pytest.raises(ValueError, match=r"сцены без текста: 2 "):
        parse_scenes(raw, known_cast=set(), default_duration=6.0)


def test_the_opening_flash_and_the_end_screen_may_be_mute() -> None:
    raw = [_row(1, "", 0.8), _row(2, "раз два три", 5), _row(3, "", 4.5)]
    scenes = parse_scenes(raw, known_cast=set(), default_duration=6.0)
    # вспышка не поднимается до пола читаемости: пол — для сцен с речью
    assert [s.duration_sec for s in scenes] == [0.8, 5, 4.5]
    assert silence_findings(scenes).ok

    for too_long in ([_row(1, "", 2.0), raw[1], raw[2]], [raw[0], raw[1], _row(3, "", 6.0)]):
        with pytest.raises(ValueError, match="сцены без текста"):
            parse_scenes(too_long, known_cast=set(), default_duration=6.0)


# ---------------------------------------------------------------- разбиение бита


def test_a_one_sentence_beat_is_cut_at_the_comma_not_left_mute() -> None:
    """ep001 s36: «Весь зал склонился. Один остался стоять.» — два предложения;
    бит из одного предложения до 16.09 отдавал весь текст первому клипу."""
    text = "Весь зал склонился перед ним, и только один остался стоять."
    assert split_narration(text, 2) == [
        "Весь зал склонился перед ним,",
        "и только один остался стоять.",
    ]
    scenes = parse_scenes(
        [_row(1, text, 12.0), _row(2, "дальше", 4.0), _row(3, "ещё", 4.0)],
        known_cast=set(),
        default_duration=6.0,
        ceiling=6.0,
    )
    assert [s.id for s in scenes][:2] == ["s01", "s01_2"]
    assert all(s.narration for s in scenes)
    assert silence_findings(scenes).ok


def test_a_dash_or_colon_is_a_cut_too() -> None:
    assert split_narration("Ответ простой: он не знал", 2) == ["Ответ простой:", "он не знал"]
    assert split_narration("Он встал — и ушёл навсегда", 2) == ["Он встал", "— и ушёл навсегда"]


def test_without_punctuation_the_cut_goes_between_words() -> None:
    parts = split_narration("один остался стоять перед троном царя", 3)
    assert len(parts) == 3 and all(parts)
    assert " ".join(parts) == "один остался стоять перед троном царя"


def test_sentences_win_over_commas_and_no_part_is_ever_empty() -> None:
    assert split_narration("Первое, короткое. Второе тоже.", 2) == [
        "Первое, короткое.",
        "Второе тоже.",
    ]
    # короткие куски в начале и длинный в конце: жадная раскладка оставляла пустые части
    parts = split_narration("А, б, " + "в" * 100, 3)
    assert parts == ["А,", "б,", "в" * 100]


def test_a_single_word_cannot_be_cut_and_the_mute_part_is_found() -> None:
    scenes = parse_scenes(
        [_row(1, "Тишина.", 12.0), _row(2, "раз два", 4.0), _row(3, "три четыре", 4.0)],
        known_cast=set(),
        default_duration=6.0,
        ceiling=6.0,
    )
    silence = silence_findings(scenes)
    assert not silence.ok
    assert silence.runs == [(["s01_2"], 6.0)]


# ---------------------------------------------------------------- немые серии


def _scene(n: int, narration: str, seconds: float) -> Scene:
    return Scene(id=f"s{n}", order=n, narration=narration, duration_sec=seconds)


def test_silence_counts_mute_seconds_and_runs_but_not_the_allowed_places() -> None:
    scenes = [
        _scene(1, "", 0.8),  # вспышка до первого слова — законна
        _scene(2, "текст", 100.0),
        _scene(3, "", 1.0),  # короткая немая — в доле, но не серия
        _scene(4, "текст", 5.0),
        _scene(5, "", 1.0),
        _scene(6, "", 1.0),  # две подряд — серия 2 с
        _scene(7, "текст", 90.0),
        _scene(8, "", 4.0),  # конечная заставка — законна
    ]
    silence = silence_findings(scenes)
    assert silence.seconds == 3.0
    assert silence.runs == [(["s5", "s6"], 2.0)]
    assert 2.0 > SILENT_RUN_LIMIT_SEC and not silence.ok
    assert "s5…s6 2.0 с" in silence.describe()

    scenes[5].narration = "текст"
    assert silence_findings(scenes).ok  # 2 с из 202.8 — меньше 1%, серий нет


def test_the_share_limit_catches_many_short_mute_scenes() -> None:
    scenes = [_scene(n, "" if n % 2 else "текст", 1.4) for n in range(2, 42)]
    scenes.insert(0, _scene(1, "текст", 5))
    silence = silence_findings(scenes)
    assert not silence.runs and not silence.ok  # серий нет, но немых 28 с из 61 — 46%


# ---------------------------------------------------------------- стадия


PLANNER = RunOptions(auto_approve=True, raise_errors=True, only="planner")


@pytest.fixture
def briefed(projects_root: Path) -> Path:
    create_project(
        ProjectSpec(
            id="p",
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


def test_a_mute_answer_sends_the_planner_back_once_with_the_reason(
    briefed: Path, mock_providers: ProviderSet
) -> None:
    llm = _llm(mock_providers)
    llm.planner_mute_calls = 1
    report = run_episode(briefed, mock_providers, PLANNER)
    assert report.ok, report.reason
    assert llm.planner_calls == 2
    assert "сцены без текста: 2" in llm.planner_prompts[1]
    scenes = load_storyboard(briefed).scenes
    assert scenes and all(s.narration for s in scenes)


def test_two_mute_answers_fail_the_planner_before_any_money(
    briefed: Path, mock_providers: ProviderSet
) -> None:
    llm = _llm(mock_providers)
    llm.planner_mute_calls = 2
    report = run_episode(briefed, mock_providers, RunOptions(auto_approve=True, only="planner"))
    assert not report.ok and "сцены без текста" in (report.reason or "")
    assert load_manifest(briefed).stages["planner"].status == "failed"
    assert load_storyboard(briefed).scenes == []
