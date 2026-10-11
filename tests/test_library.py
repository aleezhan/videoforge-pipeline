"""Библиотека приёмов: отбор по условиям, правила против гнили, оглавление."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from videoforge.core.library import (
    Technique,
    add_example,
    brief,
    find_example,
    for_decision,
    load_all,
    validate_technique,
    write_index,
)


def _write(base: Path, decision: str, data: dict) -> Path:
    folder = base / decision
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{data['id']}.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture()
def lib(tmp_path: Path) -> Path:
    base = tmp_path / "library"
    _write(
        base,
        "ending_audio",
        {
            "id": "fade_out_soft",
            "title": "Затухание",
            "status": "implemented",
            "implemented_by": "assemble",
            "applies_when": {"music": True},
            "evidence": [{"source": "ролик", "measured": "0.5 с"}],
        },
    )
    _write(
        base,
        "ending_audio",
        {
            "id": "cut_on_beat",
            "title": "Обрыв на доле",
            "applies_when": {"music": True},
            "never_when": {"has_speech": True},
            "evidence": [{"source": "идея", "verdict": "n/a"}],
        },
    )
    _write(
        base,
        "captions",
        {
            "id": "karaoke",
            "title": "Караоке",
            "status": "implemented",
            "implemented_by": "captions.py",
            "applies_when": {"has_speech": True},
            "evidence": [{"source": "шортсы"}],
        },
    )
    return base


def test_conditions_select_and_reject(lib: Path) -> None:
    """Ролик с речью не получает приём, у которого речь в never_when."""
    ctx = {"music": True, "has_speech": True}
    rows = {t.id: verdict for t, verdict, _ in for_decision("ending_audio", ctx, lib)}
    assert rows["fade_out_soft"] == "подходит"
    assert rows["cut_on_beat"] == "не подходит"


def test_unknown_condition_is_shown_not_hidden(lib: Path) -> None:
    """Непроверенное условие не прячет приём — показываем с пометкой."""
    rows = dict(
        (t.id, (verdict, unknown)) for t, verdict, unknown in for_decision("ending_audio", {}, lib)
    )
    verdict, unknown = rows["fade_out_soft"]
    assert verdict == "не проверено"
    assert unknown == ["music"]


def test_brief_skips_unsuitable(lib: Path) -> None:
    """В промпт стадии идут только подходящие: задача — сузить выбор."""
    text = brief("ending_audio", {"music": True, "has_speech": True}, lib)
    assert "Затухание" in text
    assert "Обрыв на доле" not in text


def test_recorded_marked_as_not_implemented(lib: Path) -> None:
    text = brief("ending_audio", {"music": True, "has_speech": False}, lib)
    assert "[не реализовано]" in text


def test_empty_conditions_are_rejected() -> None:
    """«Всегда» должно быть решением, а не забывчивостью."""
    t = Technique(id="x", decision="grade", title="X", evidence=[{"source": "s"}])
    problems = validate_technique(t)
    assert any("нет ни одного условия" in p for p in problems)
    t.always = True
    assert not validate_technique(t)


def test_technique_without_evidence_is_rejected() -> None:
    t = Technique(id="x", decision="grade", title="X", always=True)
    assert any("evidence" in p for p in validate_technique(t))


def test_unknown_decision_and_condition_are_rejected() -> None:
    t = Technique(
        id="x",
        decision="выдуманная",
        title="X",
        applies_when={"погода": "ясно"},
        evidence=[{"source": "s"}],
    )
    problems = validate_technique(t)
    assert any("точка решения" in p for p in problems)
    assert any("вне словаря" in p for p in problems)


def test_implemented_must_say_by_what() -> None:
    t = Technique(
        id="x",
        decision="grade",
        title="X",
        always=True,
        status="implemented",
        evidence=[{"source": "s"}],
    )
    assert any("implemented_by" in p for p in validate_technique(t))


def test_index_lists_everything(lib: Path) -> None:
    path = write_index(lib)
    text = path.read_text(encoding="utf-8")
    assert "Приёмов: 3 — умеем 2" in text
    assert "fade_out_soft" in text and "cut_on_beat" in text


def test_example_before_after(lib: Path, tmp_path: Path) -> None:
    """Пример «было → стало» лежит рядом с приёмом под именами before/after;
    новый пример заменяет старый — пример у приёма один."""
    t = next(t for t in load_all(lib) if t.id == "karaoke")
    assert find_example(t) is None
    before, after = tmp_path / "b.png", tmp_path / "a.png"
    before.write_bytes(b"\x89PNG")
    after.write_bytes(b"\x89PNG")
    ex = add_example(t, after, before)
    folder = lib / "captions" / "karaoke"
    assert (
        ex.kind == "image"
        and ex.after == folder / "after.png"
        and ex.before == folder / "before.png"
    )
    assert find_example(t) == ex

    after2 = tmp_path / "a.jpg"
    after2.write_bytes(b"\xff\xd8")
    ex2 = add_example(t, after2)  # без before: старое «было» остаётся
    assert ex2.after.name == "after.jpg" and not (folder / "after.png").exists()
    assert ex2.before is not None and ex2.before.name == "before.png"

    bad = tmp_path / "x.txt"
    bad.write_text("no", encoding="utf-8")
    with pytest.raises(ValueError):
        add_example(t, bad)
    with pytest.raises(FileNotFoundError):
        add_example(t, tmp_path / "nope.png")
    # одинокий before без after — не пример
    (folder / "after.jpg").unlink()
    assert find_example(t) is None


def test_every_decision_in_exactly_one_group() -> None:
    """Группы для пульта покрывают словарь точек без дыр и без дублей."""
    from videoforge.core.library import DECISION_GROUPS, DECISIONS

    seen: list[str] = [d for _, keys in DECISION_GROUPS.values() for d in keys]
    assert sorted(seen) == sorted(DECISIONS), "точка вне групп или в двух группах"


def test_recipes_point_at_real_techniques() -> None:
    """Рецепт примера без приёма — мусор; приём с рецептом обязан существовать."""
    from videoforge.core.library_examples import RECIPES

    ids = {t.id for t in load_all()}
    missing = sorted(set(RECIPES) - ids)
    assert not missing, f"рецепты без приёма: {missing}"


def test_render_caption_examples(tmp_path: Path) -> None:
    """Рецепты на своих материалах (шрифты из репозитория) собирают файлы:
    шрифт — картинка, анимация — клип."""
    from videoforge.core.library_examples import render

    font = render("pt_sans", tmp_path / "font")
    assert font.after.suffix == ".png" and font.after.stat().st_size > 1000 and font.before is None
    clip = render("pop", tmp_path / "pop")
    assert clip.after.suffix == ".mp4" and clip.after.stat().st_size > 1000


def test_repo_library_is_valid() -> None:
    """Живая библиотека репозитория обязана проходить свои же правила."""
    items = load_all()
    assert items, "library/ пуста — приёмы не загрузились"
    for t in items:
        assert not validate_technique(t), f"{t.decision}/{t.id}"


# ------------------------------------------------------- условия ролика и бриф стадии


def test_conditions_come_from_the_episode_files() -> None:
    """Условия ролика считает python из раскадровки, project.yaml и workflow —
    одни и те же у CLI, у стадии и у пульта."""
    from videoforge.core.library import conditions
    from videoforge.core.project import ChannelConfig, ProjectConfig, VideoConfig
    from videoforge.core.storyboard import Scene, SourceSegment, Storyboard

    sb = Storyboard(
        project_id="t", scenes=[Scene(id="s1", order=1, narration="речь", duration_sec=6)]
    )
    project = ProjectConfig(
        id="t",
        channel=ChannelConfig(platforms=["youtube_shorts", "tiktok"]),
        video=VideoConfig(music=True),
    )
    assert conditions(sb, project, "ai_video") == {
        "format": "ai_video",
        "aspect": "9:16",
        "footage": "generated",
        "has_speech": True,
        "duration_band": "short",
        "music": True,
        "platform": ["youtube", "tiktok"],
    }  # mood не выводится: чувство ролика нигде не записано

    clip = Scene(
        id="c1",
        order=1,
        narration="слова из транскрипта",
        duration_sec=70,
        source=SourceSegment(path="sources/x/full.mp4", start_sec=10, end_sec=80),
    )
    filmed = conditions(Storyboard(project_id="t", scenes=[clip]), None, "clips_from_long")
    assert filmed["footage"] == "filmed" and filmed["format"] == "clips_from_long"
    assert filmed["duration_band"] == "mid" and filmed["music"] is False


def test_list_valued_condition_hits_any_platform() -> None:
    """Ролик на несколько площадок: never_when по одной из них срабатывает."""
    t = Technique(
        id="x",
        decision="captions",
        title="X",
        applies_when={"aspect": "9:16"},
        never_when={"platform": "tiktok"},
        evidence=[{"source": "s"}],
    )
    assert t.matches({"aspect": "9:16", "platform": ["youtube", "tiktok"]})[0] == "не подходит"
    assert t.matches({"aspect": "9:16", "platform": ["youtube"]})[0] == "подходит"


def test_for_stage_offers_only_implemented_and_fitting(lib: Path) -> None:
    """В промпт стадии идёт только то, что рендер умеет и что подходит ролику;
    сообщение стадии называет, сколько и по каким точкам."""
    from videoforge.core.library import for_stage

    got = for_stage(("ending_audio", "captions"), {"music": True, "has_speech": False}, lib)
    assert got.offered == {"ending_audio": ["fade_out_soft"], "captions": []}
    assert "Затухание" in got.text and "Обрыв на доле" not in got.text  # recorded — не модели
    assert "исполнение: assemble" in got.text
    assert got.describe() == "приёмов из библиотеки: 1 (ending_audio 1)"
    empty = for_stage(("ending_audio",), {"music": False}, lib)
    assert empty.text == "" and empty.describe() == "приёмов из библиотеки: 0"
