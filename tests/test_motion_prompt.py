"""Структура i2v-промпта: Subject lock → Action → [Tempo] → Camera → Constraints.

Форма промпта — производственное знание (замеры 31.08.2026): вольная склейка
дорисовывала в кадр выдуманные элементы. Тест держит форму, чтобы её не
упростили обратно «для краткости».

Блок темпа — замер 09.09.2026 (tests/t02_cat_realtime): промпт с одним
событием давал слоумо (медиана энергии движения 8.8, 24% тихих окон), с
непрерывной серией действий и словами про темп — 14.5 и 6%. Тесты держат и
формулировку, и выключатель: выдох и пейзаж блок темпа не получают.
"""

from types import SimpleNamespace

import pytest

from videoforge.core.resources import ResourceBundle, Style
from videoforge.core.storyboard import Scene, SceneTempo, fingerprint
from videoforge.providers.remotion_video import STRENGTH_DEFAULT, parse_motion
from videoforge.stages.planner import _PROMPT, parse_scenes
from videoforge.stages.prompts import (
    MOTION_CONSTRAINTS,
    TEMPO_ACTION,
    motion_prompt,
    scene_tempo,
)
from videoforge.stages.video import VideoStage


def _scene() -> Scene:
    return Scene(
        id="s1",
        order=1,
        narration="текст",
        visual_prompt="a small planet wrapped in a glowing shield",
        motion_prompt="camera slowly pushes in toward the planet",
    )


def _bundle(motion_hint: str = "") -> ResourceBundle:
    style = Style(
        id="test_style",
        name="test",
        prompt_prefix="flat vector",
        prompt_suffix="poster-like",
        motion_hint=motion_hint,
    )
    return ResourceBundle(style=style)


def test_motion_prompt_is_structured() -> None:
    prompt = motion_prompt(_scene(), _bundle("one single slow continuous camera move"))
    blocks = prompt.split("\n\n")
    assert blocks[0].startswith("Image-to-video animation")
    assert blocks[1].startswith("Subject: a small planet")
    assert "exactly as in the input image" in blocks[1]
    assert blocks[2].startswith("Action: camera slowly pushes in")
    assert blocks[3].startswith("Camera: one single slow continuous")
    assert blocks[4] == MOTION_CONSTRAINTS


def test_constraints_are_positive_not_a_negation_tail() -> None:
    # позитивная формулировка стабильности — до перечисления запретов
    assert MOTION_CONSTRAINTS.index("must remain visually unchanged") < MOTION_CONSTRAINTS.index(
        "No new elements"
    )


def test_camera_block_skipped_without_hint() -> None:
    prompt = motion_prompt(_scene(), _bundle(""))
    assert "Camera:" not in prompt
    assert MOTION_CONSTRAINTS in prompt


def test_structured_prompt_still_parses_for_remotion() -> None:
    """remotion-провайдер читает тот же промпт: структура не должна ломать разбор."""
    prompt = motion_prompt(_scene(), _bundle("one single slow continuous camera move"))
    parsed = parse_motion(prompt)
    assert parsed.motion == "push_in"


# --- темп: непрерывное действие вместо одного события ------------------------


def _action_scene(tempo: SceneTempo | None = None) -> Scene:
    return Scene(
        id="s2",
        order=2,
        narration="",
        visual_prompt="a white persian cat in MMA gloves over a downed fighter in a UFC octagon",
        motion_prompt=(
            "the cat throws a flurry of punches, the fighter covers up and slides down the fence"
        ),
        tempo=tempo,
    )


def test_action_scene_gets_the_tempo_block_between_action_and_camera() -> None:
    prompt = motion_prompt(_action_scene(), _bundle("handheld broadcast camera, slight shake"))
    blocks = prompt.split("\n\n")
    assert blocks[2].startswith("Action: the cat throws a flurry")
    assert blocks[3] == TEMPO_ACTION
    assert blocks[4].startswith("Camera: handheld")
    assert blocks[5] == MOTION_CONSTRAINTS


def test_tempo_wording_is_the_one_that_won_the_bench() -> None:
    """Слова про темп — из промпта B замера 09.09.2026 (медиана 14.5 против 8.8):
    их не переписывать «покрасивее» без нового замера."""
    assert TEMPO_ACTION.startswith("Tempo: continuous real-time action for the whole clip")
    assert "one movement after another without pause" in TEMPO_ACTION
    assert (
        "Real-time speed, normal playback speed, fast sharp movements, no slow motion"
        in TEMPO_ACTION
    )


def test_calm_scene_has_no_tempo_block() -> None:
    """«slowly pushes in» на планете — выдох: от «fast sharp movements» он задёргается."""
    assert scene_tempo(_scene()) == "calm"
    assert "Tempo:" not in motion_prompt(_scene(), _bundle(""))


def test_doubt_is_action() -> None:
    """Ни слов покоя, ни слов действия — action: по замеру вялость — дефект по умолчанию."""
    scene = _scene()
    scene.motion_prompt = "camera pans across the market"
    assert scene_tempo(scene) == "action"


def test_an_action_word_beats_a_calm_word() -> None:
    """«no slow motion» в промпте про драку содержит slow — и всё равно action."""
    scene = _action_scene()
    scene.motion_prompt += "; real-time, no slow motion"
    assert scene_tempo(scene) == "action"


def test_calm_is_read_from_word_stems() -> None:
    """slowly / breathing / gently / silence — те же слова покоя, что slow / breathe."""
    for motion in (
        "mist breathing over the valley",
        "gently drifting clouds",
        "silence, empty sky",
    ):
        scene = _scene()
        scene.motion_prompt = motion
        assert scene_tempo(scene) == "calm", motion


def test_explicit_calm_switches_the_rule_off() -> None:
    """Планнер или человек написал `tempo: calm` — эвристика не спорит."""
    prompt = motion_prompt(_action_scene(tempo="calm"), _bundle(""))
    assert "Tempo:" not in prompt


def test_explicit_action_forces_the_rule() -> None:
    scene = _scene()  # по словам — выдох
    scene.tempo = "action"
    assert TEMPO_ACTION in motion_prompt(scene, _bundle(""))


def test_tempo_enters_the_clip_fingerprint_only_when_set() -> None:
    """Пустое tempo не меняет отпечаток клипа: иначе обновление с правилом темпа
    сделало бы устаревшими все готовые клипы во всех проектах. Заданное — меняет:
    это правка входа, как правка motion_prompt."""
    ctx = SimpleNamespace(
        resources=_bundle("hint"),
        storyboard=SimpleNamespace(aspect_ratio="9:16"),
        # генеративный провайдер: место в цепочке продолжений во входы не идёт
        providers=SimpleNamespace(video=SimpleNamespace(chains_camera=False)),
    )
    scene = _action_scene()
    scene.assets.image = "02_images/s2.png"
    scene.inputs_fingerprint["image"] = "abc"
    before_tempo_existed = fingerprint(
        scene.motion_prompt, scene.visual_prompt, "hint", "9:16", "02_images/s2.png", "abc"
    )
    assert VideoStage._inputs(ctx, scene) == before_tempo_existed
    scene.tempo = "calm"
    calm = VideoStage._inputs(ctx, scene)
    scene.tempo = "action"
    action = VideoStage._inputs(ctx, scene)
    assert len({before_tempo_existed, calm, action}) == 3


def test_remotion_ignores_the_tempo_block() -> None:
    """Блок темпа адресован i2v-модели: программная камера Ken Burns не должна
    читать из него «fast» и ускорять наезд на каждой сцене с действием."""
    scene = _action_scene()
    scene.motion_prompt = "camera pushes in as the cat throws punch after punch"
    prompt = motion_prompt(scene, _bundle(""))
    assert TEMPO_ACTION in prompt
    parsed = parse_motion(prompt)
    assert parsed.motion == "push_in"
    assert parsed.strength == STRENGTH_DEFAULT


def test_planner_asks_for_tempo_and_a_sequence_of_actions() -> None:
    assert '"tempo": "action | calm"' in _PROMPT
    assert "never a single event" in _PROMPT


def test_planner_writes_tempo_and_rejects_a_third_value() -> None:
    raw: list[dict[str, object]] = [
        {"narration": "раз", "visual_prompt": "a", "motion_prompt": "m1", "tempo": "action"},
        {"narration": "два", "visual_prompt": "b", "motion_prompt": "m2", "tempo": "calm"},
        {"narration": "три", "visual_prompt": "c", "motion_prompt": "m3"},
    ]
    scenes = parse_scenes(raw, known_cast=set(), default_duration=6.0)
    assert [s.tempo for s in scenes] == ["action", "calm", None]
    raw[0]["tempo"] = "medium"
    with pytest.raises(ValueError, match="tempo 'medium'"):
        parse_scenes(raw, known_cast=set(), default_duration=6.0)
