"""Промпт сцены против стиля: по словам — до денег (планнер переспрашивает,
картинки отказываются), спорное — совет модели на ворота."""

from __future__ import annotations

from pathlib import Path

from videoforge.core.manifest import load_manifest
from videoforge.core.project import ProjectSpec, create_episode, create_project
from videoforge.core.resources import Style
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, load_storyboard, save_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.providers.mock import MockLlm
from videoforge.stages.style_conflict import (
    MODEL_RULE,
    Conflict,
    describe,
    find_conflicts,
    retry_reason,
)

PLANNER = RunOptions(auto_approve=True, raise_errors=True, only="planner")
# негатив в духе flat_cartoon_warm: как его пишут в YAML, с регистром и пробелами
FLAT = Style(
    id="flat", negative="photorealistic, 3D render, text, letters, numbers, watermark, logo"
)


def _scene(sid: str, visual: str, motion: str = "slow push in") -> Scene:
    return Scene(
        id=sid, order=int(sid[1:]), narration="речь", visual_prompt=visual, motion_prompt=motion
    )


# ---------------------------------------------------------------- по словам


def test_words_of_the_prompt_are_checked_against_the_negative() -> None:
    """Случаи ep002: Коран с надписью, монета с чеканкой, фотореализм при 2D."""
    scenes = [
        _scene("s1", "an open Quran with an inscription in gold"),
        _scene("s2", "an engraved coin on velvet"),
        _scene("s3", "photorealistic desert at dusk"),
        _scene("s4", "a boy on a hill"),
    ]
    found = find_conflicts(scenes, FLAT)
    assert [(c.scene_id, c.term, c.rule) for c in found] == [
        ("s1", "inscription", "text"),
        ("s2", "engraved", "text"),
        ("s3", "photorealistic", "photorealistic"),
    ]
    assert all(c.severity == "block" for c in found)


def test_whole_words_only() -> None:
    """«script» не ловится в «description», «text» — в «context» и «textured»."""
    scenes = [_scene("s1", "a description of the context, textured wall, scripted lighting")]
    assert find_conflicts(scenes, FLAT) == []


def test_motion_prompt_is_checked_too() -> None:
    """Движение «камера над гравированными буквами» рисует текст так же, как кадр."""
    scenes = [_scene("s1", "a coin on velvet", motion="camera pans over the engraved letters")]
    assert [c.term for c in find_conflicts(scenes, FLAT)] == ["engraved", "letters"]


def test_one_finding_per_term_even_if_several_rules_match() -> None:
    """«inscription» при негативе text, letters, numbers — один конфликт, не три;
    повтор того же слова в сцене — тоже один."""
    scenes = [_scene("s1", "an inscription, and the same inscription again")]
    found = find_conflicts(scenes, FLAT)
    assert [(c.term, c.rule) for c in found] == [("inscription", "text")]


def test_style_without_negative_or_no_style_finds_nothing() -> None:
    scenes = [_scene("s1", "photorealistic engraved text")]
    assert find_conflicts(scenes, Style(id="empty")) == []
    assert find_conflicts(scenes, None) == []


def test_negated_mention_is_agreement_not_conflict() -> None:
    """«an open book with no readable text» — исполнение правила «текст на плашку»."""
    scenes = [
        _scene("s1", "an open book with no readable text"),
        _scene("s2", "a plain wall without any letters, text-free"),
    ]
    assert find_conflicts(scenes, FLAT) == []


def test_hyphenated_and_capitalised_spellings() -> None:
    """Термин называется так, как написан в промпте, — человек ищет его глазами."""
    found = find_conflicts([_scene("s1", "Photo-Realistic shot, DSLR look")], FLAT)
    assert [(c.term, c.rule) for c in found] == [
        ("Photo-Realistic", "photorealistic"),
        ("DSLR", "photorealistic"),
    ]


def test_longest_phrase_wins_over_its_own_word() -> None:
    """«3D render» — один конфликт, а не «3d» плюс «3d render»."""
    found = find_conflicts([_scene("s1", "a 3D render of a mosque")], FLAT)
    assert [(c.term, c.rule) for c in found] == [("3D render", "3D render")]


def test_unmapped_negative_terms_are_not_matched_by_words() -> None:
    """Негатив пишется для художника: «people» там значит «лишних людей», и по
    словам его не ловим — иначе блок на каждой сцене с людьми."""
    style = Style(
        id="s", negative="added objects the scene does not mention (planets, people), cross"
    )
    scenes = [_scene("s1", "people crossing a square, planets above")]
    assert find_conflicts(scenes, style) == []


def test_forbid_terms_are_literal_phrases() -> None:
    """`forbid` — заказ человека: фраза как написана, словом целиком."""
    style = Style(id="s", forbid=["coins", "wine glass"])
    scenes = [
        _scene("s1", "a pile of gold coins"),
        _scene("s2", "a glass of water and a coin"),
        _scene("s3", "a wine-glass on the table"),
    ]
    assert [(c.scene_id, c.term, c.rule) for c in find_conflicts(scenes, style)] == [
        ("s1", "coins", "coins"),
        ("s3", "wine-glass", "wine glass"),
    ]


def test_messages_name_scene_term_and_rule() -> None:
    conflicts = [Conflict("s05", "inscription", "text")]
    assert describe(conflicts) == "s05: 'inscription' ↔ 'text'"
    assert describe([Conflict("s02", "", MODEL_RULE, "warn", "фото при 2D")]) == (
        "s02: по мнению модели — фото при 2D"
    )
    text = retry_reason(conflicts, FLAT)
    assert "s05" in text and "inscription" in text and "overlay" in text
    assert "The style forbids: photorealistic, 3D render" in text  # запреты стиля — модели целиком


# ---------------------------------------------------------------- планнер


def _episode(projects_root: Path, *, style: str | None = "pixar3d") -> Path:
    create_project(
        ProjectSpec(
            id="p",
            name="Планнер",
            platforms=["youtube_shorts"],
            character="timur",
            style=style,
            voice="ru_male",
            target_duration_sec=30,
            scene_duration_sec=6.0,
        )
    )
    return create_episode("p", "e1", title="проба", brief="мальчик находит старую карту")


def _llm(providers: ProviderSet) -> MockLlm:
    assert isinstance(providers.llm, MockLlm)
    return providers.llm


def test_a_clean_storyboard_asks_the_model_once_and_reports_zero(
    projects_root: Path, mock_providers: ProviderSet
) -> None:
    edir = _episode(projects_root)
    report = run_episode(edir, mock_providers, PLANNER)
    assert report.ok, report.reason
    llm = _llm(mock_providers)
    assert llm.planner_calls == 1 and llm.style_calls == 1
    message = load_manifest(edir).stages["planner"].message
    assert "конфликтов со стилем: 0 (по словам 0, по мнению модели 0)" in message
    assert not any("style" in s.checks.notes for s in load_storyboard(edir).scenes)


def test_an_off_style_answer_sends_the_planner_back_once(
    projects_root: Path, mock_providers: ProviderSet
) -> None:
    """Первая раскадровка — «photorealistic» при pixar3d: планнер получает
    причины и отвечает второй раз; до читаемости первый ответ не доходит —
    конфликт виден по словам, вызов не нужен."""
    edir = _episode(projects_root)
    llm = _llm(mock_providers)
    llm.planner_offstyle_calls = 1
    report = run_episode(edir, mock_providers, PLANNER)
    assert report.ok, report.reason
    assert llm.planner_calls == 2 and llm.readability_calls == 1 and llm.style_calls == 1
    sb = load_storyboard(edir)
    assert not any("photorealistic" in s.visual_prompt for s in sb.scenes)
    message = load_manifest(edir).stages["planner"].message
    assert "конфликтов со стилем: 1 (по словам 1 → повтор, по мнению модели 0)" in message


def test_planner_fails_when_the_retry_is_off_style_too(
    projects_root: Path, mock_providers: ProviderSet
) -> None:
    """Два ответа против стиля — стадия падает с перечислением; ни читаемость,
    ни мнение модели не спрашиваются, раскадровка не пишется."""
    edir = _episode(projects_root)
    llm = _llm(mock_providers)
    llm.planner_offstyle_calls = 2
    report = run_episode(edir, mock_providers, RunOptions(auto_approve=True, only="planner"))
    assert not report.ok
    reason = report.reason or ""
    assert "промпт против стиля" in reason and "s01: 'photorealistic' ↔ 'photorealistic'" in reason
    assert llm.planner_calls == 2 and llm.readability_calls == 0 and llm.style_calls == 0
    assert load_manifest(edir).stages["planner"].status == "failed"
    assert load_storyboard(edir).scenes == []


def test_the_models_opinion_is_a_note_not_a_block(
    projects_root: Path, mock_providers: ProviderSet
) -> None:
    """Вердикт модели плывёт между прогонами — заметка на сцене и «ПРОВЕРЬ»
    на воротах, а не повтор и не провал."""
    edir = _episode(projects_root)
    llm = _llm(mock_providers)
    llm.style_flag_calls = 1
    report = run_episode(edir, mock_providers, PLANNER)
    assert report.ok, report.reason
    assert llm.planner_calls == 1 and llm.style_calls == 1
    flagged = [s for s in load_storyboard(edir).scenes if "style" in s.checks.notes]
    assert [s.id for s in flagged] == ["s01"]
    assert "мок" in flagged[0].checks.notes["style"]
    message = load_manifest(edir).stages["planner"].message
    assert "по мнению модели 1" in message
    assert "ПРОВЕРЬ: против стиля по мнению модели s01" in message


def test_no_style_means_no_check(projects_root: Path, mock_providers: ProviderSet) -> None:
    """Стиля нет — противоречить нечему: ни блока по словам, ни вызова модели."""
    edir = _episode(projects_root, style=None)
    llm = _llm(mock_providers)
    llm.planner_offstyle_calls = 1
    report = run_episode(edir, mock_providers, PLANNER)
    assert report.ok, report.reason
    assert llm.planner_calls == 1 and llm.style_calls == 0
    assert "конфликтов со стилем" not in load_manifest(edir).stages["planner"].message


# ---------------------------------------------------------------- картинки


def test_images_refuse_an_off_style_prompt_before_spending(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Второй страж: раскадровку поправили руками — и правка не стоит кадра.
    Ни попытки, ни расхода, ни файла; мнение модели здесь не спрашивается."""
    sb = load_storyboard(episode)
    sb.scenes[0].visual_prompt = "engraved inscription on a coin"
    save_storyboard(episode, sb)
    mock_providers.image.price_usd = 1.0  # платный вызов, случись он, был бы виден в расходах

    report = run_episode(episode, mock_providers, RunOptions(only="images", auto_approve=True))
    assert not report.ok
    reason = report.reason or ""
    assert "промпт против стиля" in reason
    assert "s1: 'engraved' ↔ 'text'; s1: 'inscription' ↔ 'text'" in reason
    manifest = load_manifest(episode)
    assert manifest.stages["images"].status == "failed"
    assert manifest.spent_usd == 0
    assert load_storyboard(episode).scenes[0].attempts.get("image", 0) == 0
    images_dir = episode / "02_images"
    assert not images_dir.exists() or not list(images_dir.glob("*.png"))
    assert _llm(mock_providers).style_calls == 0


def test_images_run_once_the_prompt_is_fixed(episode: Path, mock_providers: ProviderSet) -> None:
    """Тот же эпизод после правки промпта руками — кадры рендерятся."""
    sb = load_storyboard(episode)
    sb.scenes[0].visual_prompt = "engraved inscription on a coin"
    save_storyboard(episode, sb)
    assert not run_episode(episode, mock_providers, RunOptions(only="images", auto_approve=True)).ok

    sb = load_storyboard(episode)
    sb.scenes[0].visual_prompt = "a plain golden coin on velvet, no markings"
    save_storyboard(episode, sb)
    report = run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, raise_errors=True)
    )
    assert report.ok, report.reason
    assert load_storyboard(episode).scenes[0].attempts["image"] == 1


def test_planner_prompt_shows_the_style_before_the_first_answer() -> None:
    """До 14.09.2026 планнер стиля не видел и узнавал о нём из причин повтора:
    ep002 написал восемь кадров в фотореализме при стиле 2D. Стиль должен
    стоять в промпте с первого захода — переспрашивать дороже."""
    from videoforge.core.resources import Style
    from videoforge.stages.planner import style_text

    style = Style(
        id="s",
        name="s",
        prompt_prefix="flat 2D vector illustration",
        negative="photorealistic, text, letters",
        forbid=["wine glass"],
    )
    text = style_text(style)
    assert "flat 2D vector illustration" in text
    assert "photorealistic, text, letters" in text
    assert "wine glass" in text
    assert style_text(None) == "—"
