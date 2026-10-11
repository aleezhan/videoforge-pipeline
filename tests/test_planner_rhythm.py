"""Картинка несёт одно-два предложения по ритму проекта, а не ровно одно.

Замер research/_probes/scene_cuts_2026-09-17: у референсного канала A картинка держит
12–18 слов — в роликах-списках одно предложение (5.4–6.6 с), в истории «7
стадий Барзаха» два (9.5 с); длинная фраза делится на две картинки посреди
фразы примерно через 5 с; длинные планы 15–24 с бывают только в истории и
всегда со словами. У ep001 78% сцен было ровно одно предложение по 8 слов.

Что здесь держится: ориентир слов считает python из `scene_duration_sec`, а
модель получает число; длинные фразы сценария приходят в промпт с готовым
разрезом; картинка из нескольких предложений и длинный план со словами проходят
линты и не дают немых половинок; сообщение стадии показывает ритм против
ориентира.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core.manifest import load_manifest
from videoforge.core.project import SCRIPT_DIR, ProjectSpec, create_episode, create_project
from videoforge.core.runner import run_episode
from videoforge.core.script import WORDS_PER_SEC
from videoforge.core.stage import RunOptions
from videoforge.providers.base import ProviderSet
from videoforge.providers.mock import MockLlm
from videoforge.stages.planner import (
    LONG_PICTURE_SEC,
    cut_long_sentence,
    long_plan_sec,
    long_sentences_text,
    parse_scenes,
    picture_rhythm,
    picture_text,
    rhythm_stats,
    silence_findings,
)
from videoforge.stages.variation import check_variation

PLANNER = RunOptions(auto_approve=True, raise_errors=True, only="planner")


def _row(n: int, narration: str, seconds: float) -> dict[str, object]:
    return {
        "narration": narration,
        "visual_prompt": f"a boy on hill {n}",
        "motion_prompt": f"pan {n}",
        "duration_sec": seconds,
    }


def _sentence(n: int, words: int, end: str = ".") -> str:
    return " ".join(f"слово{n}x{k}" for k in range(words)) + end


# ---------------------------------------------------------------- ориентир


def test_words_per_picture_come_from_the_scene_orientation() -> None:
    story = picture_rhythm(9.5)
    assert story.words == 19 == round(9.5 * WORDS_PER_SEC)
    assert story.long_sentence_words == 30  # 1.6 картинки
    assert story.cut_words == 10  # 5 с речи
    listing = picture_rhythm(6.0)
    assert (listing.words, listing.long_sentence_words) == (12, 19)
    # короткий ориентир не режет фразу короче 8 с
    assert picture_rhythm(3.0).long_sentence_words == 16
    assert story.sentences == 0  # без текста предложений не посчитать
    # длинный план раскладки — в измеренных 15–24 с, а не 2.5 ориентира (24 у истории)
    assert (long_plan_sec(9.5), long_plan_sec(6.0), long_plan_sec(30)) == (19, 15, 24)


def test_sentences_per_picture_are_counted_on_this_script() -> None:
    """Предложения считаются по слою «Текст» сценария: кадр и эмоция — не речь."""
    text = " ".join(_sentence(n, 8) for n in range(6))
    script = (
        f"## b1 · Проба · 30s\n\n**Текст:** {text}\n\n"
        "**Кадр:** Пустая земля. Тени.\n\n**Эмоция:** Тревога. Узнавание. Страх."
    )
    story = picture_rhythm(9.5, script)
    assert story.sentence_words == 8 and story.sentences == 2.5  # 19 / 8
    assert picture_rhythm(6.0, script).sentences == 1.5  # 12 / 8
    line = picture_text(story)
    assert "about 19 words" in line and "runs 9.5 s" in line and "about 2.5 of them" in line
    # brief без слоёв — сам текст без заголовков
    assert picture_rhythm(6.0, "# Заголовок\nРаз два три. Четыре пять шесть.").sentence_words == 3


# ---------------------------------------------------------------- длинная фраза


def test_a_long_sentence_is_cut_after_about_five_seconds_of_voice() -> None:
    text = (
        "Он взял два верных факта и сам вынес приговор, чья природа выше, "
        "и поставил своё суждение выше приказа Того, Кто их создал."
    )
    head, tail = cut_long_sentence(text.split(), 10)
    assert " ".join(head) == "Он взял два верных факта и сам вынес приговор,"  # 9 слов
    # без знаков — ровно после десятого слова
    head, tail = cut_long_sentence(_sentence(1, 24).split(), 10)
    assert (len(head), len(tail)) == (10, 14)
    # запятая дальше трёх слов от места не годится: картинка вышла бы на 2 с
    far = "раз два три четыре, " + _sentence(2, 13)
    assert len(cut_long_sentence(far.split(), 10)[0]) == 10
    # тире — тоже место; хвост короче 2 с (4 слова) фразу не режет
    dash = _sentence(3, 8, "") + " — " + _sentence(4, 10)
    assert cut_long_sentence(dash.split(), 10)[1][0] == "—"
    assert cut_long_sentence(_sentence(5, 13).split(), 10)[1] == []
    assert len(cut_long_sentence(_sentence(6, 14).split(), 10)[1]) == 4


def test_long_sentences_of_the_script_reach_the_prompt_with_the_cut() -> None:
    long = (
        "Он взял два верных факта и сам вынес приговор, чья природа выше, "
        "и поставил своё суждение выше приказа Того, Кто их создал."
    )
    script = f"## b1 · Проба · 30s\n\n**Текст:** Ложь в двух словах. {long} Вот это и есть кибр."
    listing = picture_rhythm(6.0, script)
    text = long_sentences_text(script, listing)
    assert "«Он взял два верных факта и сам вынес приговор, | чья природа выше, и…»" in text
    assert "(22 words)" in text and "Ложь" not in text
    # у истории та же фраза держится одной картинкой: порог 30 слов
    assert "No sentence" in long_sentences_text(script, picture_rhythm(9.5, script))


# ---------------------------------------------------------------- линты


def test_short_sentences_share_a_picture_and_no_part_goes_mute() -> None:
    raw = [
        _row(1, "Весь зал склонился. Один остался стоять.", 5.0),
        _row(2, "Он не забыл. Он не испугался. Он не перепутал.", 9.0),
        _row(3, "И сказал одну фразу.", 4.0),
        _row(4, "Всего одну. Короткую.", 3.0),
    ]
    scenes = parse_scenes(raw, known_cast=set(), default_duration=6.0, ceiling=6.0, target_sec=21)
    # картинка на 9 с длиннее клипа: python режет её на продолжения по предложениям
    assert [s.id for s in scenes] == ["s01", "s02", "s02_2", "s03", "s04"]
    assert scenes[1].narration == "Он не забыл. Он не испугался."
    assert all(s.narration for s in scenes) and silence_findings(scenes).ok
    stats = rhythm_stats(scenes, picture_rhythm(6.0))
    assert stats.pictures == 4  # продолжение — та же картинка
    assert stats.sentences == 2  # 2, 3, 1, 2
    assert stats.one_sentence_share == 0.25


def test_a_story_with_long_pictures_and_a_cut_mid_phrase_passes_the_lints() -> None:
    """Ритм «Барзаха»: картинки по два предложения, три длинных плана по 18 с
    под четырьмя предложениями и одна длинная фраза на двух картинках — на
    потолке клипа LTX (6 с). Ни одного блока и ни одной немой половинки."""
    raw: list[dict[str, object]] = []
    n = 0

    def add(narration: str, seconds: float) -> None:
        nonlocal n
        n += 1
        raw.append(_row(n, narration, seconds))

    for chapter in range(3):
        for k in range(3):
            add(_sentence(10 * chapter + k, 10) + " " + _sentence(50 + 10 * chapter + k, 9), 9.5)
        add(" ".join(_sentence(100 + 10 * chapter + k, 9) for k in range(4)), 18.0)
    long = _sentence(900, 34).split()
    add(" ".join(long[:10]).rstrip(".") + ",", 5.0)  # разрез посреди фразы — после ~5 с
    add(" ".join(long[10:]), 12.0)
    total = sum(float(str(r["duration_sec"])) for r in raw)
    scenes = parse_scenes(
        raw, known_cast=set(), default_duration=9.5, ceiling=6.0, target_sec=total
    )
    assert all(s.narration for s in scenes) and all(s.duration_sec <= 6.0 for s in scenes)
    assert silence_findings(scenes).ok
    assert not check_variation(scenes)

    stats = rhythm_stats(scenes, picture_rhythm(9.5))
    assert stats.pictures == 14
    # 9.6, а не 9.5: продолжения округляются до 0.1 с (4.8 + 4.8)
    assert stats.median_sec == pytest.approx(9.5, abs=0.15)
    assert stats.long_pictures == 3 and stats.long_time_share == pytest.approx(54 / total, 0.02)
    assert stats.one_sentence_share == 0  # ни одной картинки ровно в одно предложение
    assert stats.long_sentences_whole == 0  # 34 слова разнесены на две картинки
    assert stats.sentences == 2
    message = stats.describe(picture_rhythm(9.5))
    assert f"длинных планов ≥ {LONG_PICTURE_SEC:g} с: 3" in message


def test_a_long_sentence_left_on_one_picture_is_counted() -> None:
    raw = [_row(1, _sentence(1, 34), 17.0), _row(2, _sentence(2, 8), 4.0)]
    scenes = parse_scenes(raw, known_cast=set(), default_duration=9.5, ceiling=6.0)
    stats = rhythm_stats(scenes, picture_rhythm(9.5))
    assert stats.long_sentences_whole == 1 and stats.pictures == 2
    assert "фраз длиннее 30 слов на одной картинке: 1" in stats.describe(picture_rhythm(9.5))


# ---------------------------------------------------------------- стадия


def _project(scene_sec: float, target: int) -> Path:
    create_project(
        ProjectSpec(
            id="p",
            platforms=["youtube"],
            character="timur",
            style="pixar3d",
            voice="ru_male",
            target_duration_sec=target,
            scene_duration_sec=scene_sec,
        )
    )
    return create_episode("p", "e1", title="проба", brief="мальчик находит старую карту")


def _llm(providers: ProviderSet) -> MockLlm:
    assert isinstance(providers.llm, MockLlm)
    return providers.llm


def test_the_planner_message_shows_the_rhythm_against_the_orientation(
    projects_root: Path, mock_providers: ProviderSet
) -> None:
    edir = _project(6.0, 30)
    report = run_episode(edir, mock_providers, PLANNER)
    assert report.ok, report.reason
    prompt = _llm(mock_providers).planner_prompts[0]
    assert "a picture holds about 12 words" in prompt and "more than 19 words" in prompt
    message = load_manifest(edir).stages["planner"].message
    # заглушка: 3.2, 5.5, 12.6 (два предложения, разрезан по клипу), 3.2, 5.5
    assert "картинок: 5" in message
    assert "ритм: медиана картинки 5.5 с (ориентир 6)" in message
    assert "предложений на картинку 1" in message and "ровно одно предложение 80%" in message
    assert "длинных планов ≥ 15 с: 0 (0% времени)" in message


def test_a_story_project_gets_its_numbers_and_the_script_cuts(
    projects_root: Path, mock_providers: ProviderSet
) -> None:
    edir = _project(9.5, 240)
    long = (
        "Он взял два верных факта и сам вынес приговор, чья природа выше, и поставил своё "
        "суждение выше приказа Того, Кто их создал, и не спросил ни разу, зачем ему этот "
        "приказ и что за ним стоит."
    )
    script = edir / SCRIPT_DIR / "script.md"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f"## b1 · Кибр · 240s\n\n**Текст:** Ложь в двух словах. {long} Вот это и есть кибр.\n",
        encoding="utf-8",
    )
    report = run_episode(edir, mock_providers, PLANNER)
    assert report.ok, report.reason
    prompt = _llm(mock_providers).planner_prompts[0]
    assert "a picture holds about 19 words" in prompt and "runs 9.5 s" in prompt
    # длинный план ~19 с под ~38 словами, а не 24 с
    assert "3 LONG scenes of ~19 s = 57 s, each held under ~38 words" in prompt
    assert "«Он взял два верных факта и сам вынес приговор, | чья природа выше, и…» (36 words)" in (
        prompt
    )
    message = load_manifest(edir).stages["planner"].message
    assert "(ориентир 9.5)" in message and "длинных планов ≥ 15 с:" in message
