"""Стадия translate: субтитры на языке зрителя поверх речи на языке источника.

Речь остаётся, переводится текст. Проверяется на нарезке с локальным
источником и заглушке модели: реплики 1:1 по номерам, свежесть, сборка
рисует перевод фразой без караоке и честно говорит, когда перевода нет.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from videoforge.core.captions import (
    SceneWords,
    TranslatedCaptions,
    TranslatedLine,
    Word,
    group_words,
    parse_srt_words,
    translated_events,
)
from videoforge.core.io import read_model
from videoforge.core.resources import CaptionStyle
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard, save_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.stages.translate import parse_answer


def _set_captions_language(edir: Path, lang: str | None) -> None:
    sb = load_storyboard(edir)
    sb.captions_language = lang
    save_storyboard(edir, sb)


def test_translate_is_skipped_when_captions_speak_the_same_language(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    report = run_episode(clip_episode, mock_providers, RunOptions(only="translate"))
    assert report.ok
    assert report.executed[0][1].status == "skipped"
    assert "переводить нечего" in report.executed[0][1].message
    assert mock_providers.llm.translate_calls == 0  # type: ignore[attr-defined]


def test_translate_writes_one_line_per_group_and_is_idempotent(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    _set_captions_language(clip_episode, "en")

    report = run_episode(clip_episode, mock_providers, RunOptions(only="translate"))
    assert report.ok, report.reason
    sb = load_storyboard(clip_episode)
    (scene,) = sb.scenes
    assert scene.assets.translation == "04_audio/s01.captions.en.json"
    made = read_model(clip_episode / scene.assets.translation, TranslatedCaptions)
    assert made.language == "en" and made.source_language == "ru" and made.llm == "mock"

    # реплик ровно столько, сколько групп даёт стиль по словам речи; каждая — 1:1
    words = parse_srt_words((clip_episode / scene.assets.captions).read_text(encoding="utf-8"))
    style = CaptionStyle(id="default")
    groups = group_words(
        words, max_words=style.max_words, max_chars=style.max_chars, max_gap_sec=style.max_gap_sec
    )
    assert len(made.lines) == len(groups) > 0
    for i, (line, g) in enumerate(zip(made.lines, groups, strict=True)):
        assert line.text == f"EN {i + 1}: {' '.join(w.text for w in g)}"
        assert (line.start, line.end) == (g[0].start, g[-1].end)
    llm = mock_providers.llm
    assert llm.translate_calls == 1  # type: ignore[attr-defined]

    # свежий перевод не переделывается, --force — переделывается
    again = run_episode(clip_episode, mock_providers, RunOptions(only="translate"))
    assert again.executed[0][1].status == "skipped"
    assert llm.translate_calls == 1  # type: ignore[attr-defined]
    forced = run_episode(clip_episode, mock_providers, RunOptions(only="translate", force=True))
    assert forced.ok and llm.translate_calls == 2  # type: ignore[attr-defined]


@pytest.mark.render
def test_assemble_draws_translation_as_whole_phrases(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    _set_captions_language(clip_episode, "en")
    assert run_episode(clip_episode, mock_providers, RunOptions(only="translate")).ok

    report = run_episode(clip_episode, mock_providers, RunOptions(only="assemble"))
    assert report.ok, report.reason
    message = report.executed[0][1].message
    assert "ПЕРЕВОД" not in message
    ass = (clip_episode / "05_assemble" / "captions.ass").read_text(encoding="utf-8")
    dialogues = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    # каждая реплика — перевод целиком (заглушка пишет «EN n: …»), а не слова речи
    assert dialogues and all(re.search(r"\}EN \d+: ", ln) for ln in dialogues)
    assert "\\k" not in ass  # фраза целиком: подсветка по слову перевода невозможна
    sb = load_storyboard(clip_episode)
    made = read_model(clip_episode / "04_audio" / "s01.captions.en.json", TranslatedCaptions)
    assert sb.final.caption_events == len(made.lines)
    assert sb.final.caption_coverage and sb.final.caption_coverage > 0.9

    # язык субтитров вернули к языку речи (канал стал русским): файл перевода
    # остался у сцены, но финал обязан пересобраться со словами речи
    _set_captions_language(clip_episode, None)
    again = run_episode(clip_episode, mock_providers, RunOptions(only="assemble"))
    assert again.ok and "уже собрано" not in again.executed[0][1].message
    ass = (clip_episode / "05_assemble" / "captions.ass").read_text(encoding="utf-8")
    assert "слово" in ass and "EN " not in ass


@pytest.mark.render
def test_assemble_says_when_translation_is_missing(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    """Язык субтитров задан, стадию translate не запускали: сборка не падает,
    субтитры выходят на языке речи, и в сообщении это названо по сцене."""
    run_episode(clip_episode, mock_providers, RunOptions(only="cut"))
    _set_captions_language(clip_episode, "en")
    report = run_episode(clip_episode, mock_providers, RunOptions(only="assemble"))
    assert report.ok, report.reason
    assert "ПЕРЕВОД: у сцен s01" in report.executed[0][1].message
    ass = (clip_episode / "05_assemble" / "captions.ass").read_text(encoding="utf-8")
    assert "слово" in ass and "EN " not in ass


def test_parse_answer_blocks_gaps_and_duplicates_but_only_warns_on_length() -> None:
    ok, warnings = parse_answer(
        {"lines": [{"n": 1, "text": "one"}, {"n": 2, "text": "x" * 40}]}, 2, max_chars=20
    )
    assert ok == ["one", "x" * 40] and warnings and "longer" in warnings[0]

    texts, problems = parse_answer({"lines": [{"n": 1, "text": "one"}]}, 2, max_chars=20)
    assert texts == [] and any("missing" in p for p in problems)

    texts, problems = parse_answer(
        {"lines": [{"n": 1, "text": "a"}, {"n": 1, "text": "b"}, {"n": 2, "text": " "}]},
        2,
        max_chars=20,
    )
    assert (
        texts == [] and any("twice" in p for p in problems) and any("empty" in p for p in problems)
    )

    assert parse_answer(["not", "an", "object"], 1, max_chars=20)[0] == []


def test_translated_events_keep_speech_words_and_skip_windows_without_speech() -> None:
    words = [Word("раз", 0.0, 0.4), Word("два", 0.5, 0.9), Word("три", 3.0, 3.4)]
    scene = SceneWords("s01", offset_sec=10.0, duration_sec=5.0, words=words)
    captions = TranslatedCaptions(
        language="en",
        source_language="ru",
        llm="mock",
        style_id="default",
        lines=[
            TranslatedLine(start=0.0, end=0.9, source="раз два", text="one two"),
            TranslatedLine(start=1.5, end=2.5, source="", text="nothing said here"),
            TranslatedLine(start=3.0, end=3.4, source="три", text="three"),
        ],
    )
    events = translated_events(scene, captions)
    assert [e.text for e in events] == ["one two", "three"]
    assert [w.text for w in events[0].words] == ["раз", "два"]  # тайминги и покрытие — по речи
    assert events[0].start == 10.0 and events[1].start == 13.0
