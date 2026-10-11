"""Планнер: сценарий → сцены раскадровки, на заглушках и без денег."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core.pauses import PAUSE_BLOCK_SEC, PAUSE_JITTER_SEC
from videoforge.core.project import (
    SCRIPT_DIR,
    ProjectSpec,
    create_episode,
    create_project,
    load_blueprint,
    load_project,
)
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard, save_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.stages.audio import MAX_PAD_SEC, MIN_SCENE_SEC
from videoforge.stages.planner import available_cast, parse_scenes


@pytest.fixture
def empty_episode(projects_root: Path) -> Path:
    """Эпизод без сцен: их должен построить планнер."""
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


@pytest.mark.render
def test_planner_builds_scenes_from_brief(empty_episode: Path, mock_providers: ProviderSet) -> None:
    report = run_episode(
        empty_episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True)
    )
    assert report.ok, report.reason

    sb = load_storyboard(empty_episode)
    # заглушка планирует 5 планов, из них один длиннее клипа — его разбивает python
    ordered = sb.ordered_scenes()
    assert len(ordered) == 6
    assert [s.order for s in ordered] == [1, 2, 3, 4, 5, 6]
    assert [s.id for s in ordered][:3] == ["s01", "s02", "s03"]
    first = ordered[0]
    assert first.narration and first.visual_prompt and first.motion_prompt
    # планнер ставит длительность по содержанию, но стадия озвучки перемеряет сцену
    # по готовому голосу — в раскадровке остаётся факт, а не догадка: речь плюс
    # пауза до следующей реплики, и пол планнера добавляет к ней не больше MAX_PAD_SEC
    speech = first.measured_sec["audio"]
    longest_pause = PAUSE_BLOCK_SEC + PAUSE_JITTER_SEC
    assert speech < first.duration_sec <= max(MIN_SCENE_SEC, speech + longest_pause + MAX_PAD_SEC)
    # раскадровка доехала до конца конвейера
    assert sb.final.video and (empty_episode / sb.final.video).exists()


def test_long_beat_is_split_into_several_shots(
    empty_episode: Path, mock_providers: ProviderSet
) -> None:
    """Бит длиннее самого длинного клипа провайдера — несколько сцен одного плана."""
    run_episode(
        empty_episode,
        mock_providers,
        RunOptions(auto_approve=True, raise_errors=True, stop="planner"),
    )
    scenes = load_storyboard(empty_episode).ordered_scenes()
    ceiling = max(mock_providers.video.supported_durations)
    assert all(s.duration_sec <= ceiling for s in scenes)

    parts = [s for s in scenes if s.continues]
    assert parts, "длинный бит должен был разбиться"
    for part in parts:
        assert part.continues in {s.id for s in scenes}
        head = next(s for s in scenes if s.id == part.continues)
        assert head.visual_prompt == part.visual_prompt  # тот же план, продолжение движения


def test_scene_durations_are_not_all_equal(
    empty_episode: Path, mock_providers: ProviderSet
) -> None:
    """Ровный ритм — то, от чего мы уходим: одна проектная длительность на всех
    сценах больше не ставится."""
    run_episode(
        empty_episode,
        mock_providers,
        RunOptions(auto_approve=True, raise_errors=True, stop="planner"),
    )
    scenes = load_storyboard(empty_episode).ordered_scenes()
    assert len({s.duration_sec for s in scenes}) > 1
    assert all(s.planned_sec for s in scenes)  # пол для стадии озвучки проставлен


def test_planner_prefers_script_over_brief(
    empty_episode: Path, mock_providers: ProviderSet
) -> None:
    """Есть сценарий — планируем по нему; brief нужен, только когда сценария нет."""
    script = empty_episode / SCRIPT_DIR / "script.md"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# Сценарий\n\nМальчик находит карту и уходит в лес.\n", encoding="utf-8")

    run_episode(
        empty_episode,
        mock_providers,
        RunOptions(auto_approve=True, raise_errors=True, only="planner"),
    )
    scenes = load_storyboard(empty_episode).scenes
    assert scenes
    # длительность назначает планнер по содержанию, а не проектный дефолт на всех
    assert scenes[0].planned_sec == scenes[0].duration_sec


def test_planner_is_skipped_when_scenes_exist(episode: Path, mock_providers: ProviderSet) -> None:
    """Раскадровка, написанная руками, не перезаписывается планнером."""
    before = [s.id for s in load_storyboard(episode).scenes]
    run_episode(
        episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True, only="planner")
    )
    assert [s.id for s in load_storyboard(episode).scenes] == before


def test_planner_fails_without_script_or_brief(
    empty_episode: Path, mock_providers: ProviderSet
) -> None:
    sb = load_storyboard(empty_episode)
    sb.brief = ""
    save_storyboard(empty_episode, sb)
    report = run_episode(
        empty_episode, mock_providers, RunOptions(auto_approve=True, only="planner")
    )
    assert not report.ok
    assert "не из чего планировать" in (report.reason or "")


# ---------------------------------------------------------------- разбор ответа


def test_parse_scenes_numbers_scenes_itself() -> None:
    """Порядок и id задаёт планнер, а не модель: так они всегда уникальны."""
    scenes = parse_scenes(
        [
            {"narration": "раз", "visual_prompt": "a", "motion_prompt": "m", "cast": []},
            {"narration": "два", "visual_prompt": "b", "motion_prompt": "m", "cast": []},
        ],
        known_cast=set(),
        default_duration=6.0,
    )
    assert [s.id for s in scenes] == ["s01", "s02"]
    assert [s.order for s in scenes] == [1, 2]
    assert all(s.duration_sec == 6.0 for s in scenes)


def test_parse_scenes_rejects_unknown_cast() -> None:
    """Опечатка в id каста должна падать здесь, а не на платной генерации."""
    with pytest.raises(ValueError, match="characters/nobody"):
        parse_scenes(
            [{"narration": "x", "visual_prompt": "a", "cast": ["characters/nobody"]}],
            known_cast={"characters/timur"},
            default_duration=6.0,
        )


def test_parse_scenes_rejects_a_storyboard_longer_than_the_episode() -> None:
    """Лишние секунды раскадровки становятся полом для озвучки — то есть тишиной."""
    scenes = [
        {
            "narration": f"реплика {i}",
            "visual_prompt": f"a boy on hill {i}",
            "motion_prompt": f"pan {i}",
            "duration_sec": 20.0,
        }
        for i in range(5)
    ]
    with pytest.raises(ValueError, match="сумма длительностей"):
        parse_scenes(scenes, known_cast=set(), default_duration=6.0, target_sec=60.0)
    # в пределах допуска — принимается
    assert parse_scenes(scenes, known_cast=set(), default_duration=6.0, target_sec=95.0)


def test_scene_count_has_no_ceiling() -> None:
    """24 минуты сценами по 5 с — 290 сцен, и это не брак ответа: потолок в 200
    был взят без замера, деньги держит бюджет, а раздутый ответ — сумма
    длительностей. Короткие сцены подряд тоже не «дробность»."""
    raw = [{**row, "duration_sec": 5.0} for row in _raw(290)]
    scenes = parse_scenes(raw, known_cast=set(), default_duration=6.0, ceiling=6.0, target_sec=1450)
    assert len(scenes) == 290
    short = [{**row, "duration_sec": 2.0} for row in _raw(30)]
    assert len(parse_scenes(short, known_cast=set(), default_duration=6.0, target_sec=60)) == 30


def test_a_scene_is_never_shorter_than_the_eye_reads() -> None:
    """Короче MIN_SCENE_SEC сцена не читается, и озвучка всё равно поднимет её до
    пола — поэтому планнер ставит пол сразу, и сумма против цели считает его:
    тысяча сцен по полсекунды на 500 с — это 2000 с на экране, а не 500."""
    raw = [{**row, "duration_sec": 0.5} for row in _raw(8)]
    scenes = parse_scenes(raw, known_cast=set(), default_duration=6.0)
    assert all(s.duration_sec == MIN_SCENE_SEC == s.planned_sec for s in scenes)
    with pytest.raises(ValueError, match="сумма длительностей"):
        parse_scenes(
            [{**row, "duration_sec": 0.5} for row in _raw(1000)],
            known_cast=set(),
            default_duration=6.0,
            target_sec=500,
        )


def test_the_planner_prompt_names_no_scene_count() -> None:
    """Медиана длины сцены — ориентир, а не квота: «about N scenes» из неё делало
    норму, и короткие сцены подряд модель укрупняла под счёт."""
    from videoforge.stages.planner import _PROMPT, time_budget

    budget = time_budget(target_sec=1440, long_sec=14)
    # длинных сцен — число (четверть хронометража), всех сцен — нет
    assert "about" not in budget and "shorter scenes" not in budget
    assert "1440 seconds in total" in budget and "25 LONG scenes of ~14 s" in budget
    assert "About {scene_count}" not in _PROMPT and "not a norm" in _PROMPT


def test_parse_scenes_rejects_garbage() -> None:
    for raw in ([], {"scenes": []}, ["не объект"]):
        with pytest.raises(ValueError):
            parse_scenes(raw, known_cast=set(), default_duration=6.0)


def test_available_cast_lists_only_visual_resources(
    empty_episode: Path, mock_providers: ProviderSet
) -> None:
    """Планнеру предлагаются персонажи, места и предметы — но не стили и голоса."""
    from videoforge.core.manifest import load_manifest
    from videoforge.core.resources import load_resources, search_dirs
    from videoforge.core.runner import project_context
    from videoforge.core.stage import StageContext

    project, rules = project_context(empty_episode)
    dirs = search_dirs(empty_episode.resolve().parent.parent)
    sb = load_storyboard(empty_episode)
    ctx = StageContext(
        stage_name="planner",
        project_dir=empty_episode,
        storyboard=sb,
        manifest=load_manifest(empty_episode),
        providers=mock_providers,
        resources=load_resources(sb.resources, dirs),
        options=RunOptions(),
        project=project,
        rules=rules,
        resource_dirs=dirs,
    )
    cast = available_cast(ctx)
    assert "characters/timur" in cast
    assert not any(ref.startswith(("styles/", "voices/", "niches/")) for ref in cast)


# ---------------------------------------------------------------- связь с research


def test_blueprint_is_optional_and_missing_analysis_is_not_an_error(
    projects_root: Path, research_root: Path
) -> None:
    create_project(ProjectSpec(id="q", platforms=["youtube_shorts"], research=["нет_такого"]))
    cfg = load_project("q")
    assert cfg.research == ["нет_такого"]
    assert load_blueprint(cfg) == ""  # проект создан раньше анализа — это нормально


def test_blueprint_reaches_the_planner(projects_root: Path, research_root: Path) -> None:
    from videoforge.paths import blueprint_path

    path = blueprint_path("chan")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Production blueprint\n\n- клип 7 секунд\n", encoding="utf-8")

    create_project(ProjectSpec(id="r", platforms=["youtube_shorts"], research=["chan"]))
    assert "клип 7 секунд" in load_blueprint(load_project("r"))


# ---------------------------------------------------------------- однообразие


def _raw(n: int, *, visual: str | None = None, motion: str | None = None) -> list[dict]:
    return [
        {
            "narration": f"реплика {i}",
            "visual_prompt": visual or f"a boy on hill {i}",
            "motion_prompt": motion or f"pan {i}",
        }
        for i in range(n)
    ]


def test_storyboard_of_copies_is_rejected_before_the_money() -> None:
    """Одно описание на всю раскадровку — это пачка одинаковых оплаченных кадров."""
    with pytest.raises(ValueError, match="разных описаний кадра"):
        parse_scenes(_raw(8, visual="a boy on a hill"), known_cast=set(), default_duration=6.0)


def test_one_movement_everywhere_is_rejected() -> None:
    """Одинаковое движение во всех кадрах читается как слайдшоу."""
    with pytest.raises(ValueError, match="слайдшоу"):
        parse_scenes(_raw(8, motion="slow push in"), known_cast=set(), default_duration=6.0)


def test_empty_words_instead_of_a_frame_are_rejected() -> None:
    raw = _raw(8, visual="a beautiful modern futuristic scene, stunning")
    with pytest.raises(ValueError, match="пустых словах|разных описаний"):
        parse_scenes(raw, known_cast=set(), default_duration=6.0)


def test_a_varied_storyboard_passes() -> None:
    scenes = parse_scenes(_raw(8), known_cast=set(), default_duration=6.0)
    assert len(scenes) == 8


def test_continuations_of_one_beat_are_not_a_repeat() -> None:
    """Длинный бит planner режет на сцены одного плана — они копируют промпт
    законно, это один удержанный кадр, а не повтор."""
    from videoforge.stages.variation import check_variation

    # 4 бита по 24 с при потолке 6 с → 16 кадров, у каждого бита 4 копии
    scenes = parse_scenes(
        [
            {
                "narration": f"реплика {i}",
                "visual_prompt": f"a boy on hill {i}",
                "motion_prompt": f"pan {i}",
                "duration_sec": 24.0,
            }
            for i in range(4)
        ],
        known_cast=set(),
        default_duration=6.0,
        ceiling=6.0,
    )
    assert len(scenes) == 16  # копии есть
    assert check_variation(scenes) == []  # но однообразием они не считаются


def test_too_few_beats_to_judge() -> None:
    """Три сцены похожи по случайности — судить не о чем."""
    from videoforge.stages.variation import check_variation

    assert (
        check_variation(
            parse_scenes(_raw(3, visual="same"), known_cast=set(), default_duration=6.0)
        )
        == []
    )


def test_longest_run_counts_the_run_not_the_pairs() -> None:
    """wide,wide,cu,cu,med,med — это по два подряд, а не три."""
    from videoforge.stages.variation import _longest_run

    assert _longest_run(["w", "w", "c", "c", "m", "m"]) == 2
    assert _longest_run(["w", "w", "w", "c", "m"]) == 3
