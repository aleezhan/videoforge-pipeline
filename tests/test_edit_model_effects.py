"""Модель сама расставляет эффекты, звуки, ступени музыки и замедленный хвост
(`features/edit_model_effects`, 14.09.2026).

Что здесь проверяется и почему именно это:
- промпт называет звуки списком и квоты числами — модель не вправе выдумать id,
  а квоты в промпте обязаны совпадать с линтом, иначе план вечно на переделке;
- ответ с четырьмя списками ложится в план так же, как сцены и музыка;
- выдуманный id звука — блок линта → один повтор с причиной → после второго
  `failed`, план сохранён как вернула модель (решает человек);
- эффект за концом ролика линт снимает, а не пропускает молча.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from videoforge.core.editplan import SourceClip, Sources, default_plan, load_plan
from videoforge.core.resources import Sfx
from videoforge.core.runner import RunReport, run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, SceneAssets, Storyboard
from videoforge.providers.base import ProviderSet
from videoforge.stages import edit as edit_stage
from videoforge.stages.edit import merge_deviations
from videoforge.stages.edit_lint import (
    EFFECT_MAX_SEC,
    EFFECTS_FLOOR,
    EFFECTS_PER_MIN_WARN,
    OUTRO_SPEED_RANGE,
    SFX_FLOOR,
    SFX_GAIN_RANGE_DB,
    SFX_PER_MIN_WARN,
    lint_plan,
)
from videoforge.stages.edit_prompts import plan_prompt, sfx_text, timeline_quota

SOUNDS = [
    Sfx(id="whoosh", name="Пролёт", kind="whoosh", duration_sec=0.7),
    Sfx(id="impact_deep", name="Удар (низкий)", kind="impact", duration_sec=0.9),
]


def _stage_message(report: RunReport, stage: str) -> str:
    return next(r.message for n, r in report.executed if n == stage)


def _sources(n: int) -> Sources:
    """n сцен по 5 с — хронометраж промпта считается суммой коробок."""
    return Sources(
        clips=[
            SourceClip(
                scene_id=f"s{i:02d}",
                path=f"03_video/s{i:02d}.mp4",
                duration_sec=6.0,
                box_sec=5.0,
                speech_sec=4.0,
                silent_tail_sec=1.0,
                narration=f"Реплика {i} про число {i * 10}.",
            )
            for i in range(1, n + 1)
        ]
    )


# ------------------------------------------------------------------------ промпт


def test_prompt_lists_the_sounds_and_the_timeline_quotas_as_numbers() -> None:
    """Звуки — построчно с видом и длиной (пролёт ставят ДО склейки на его
    длину), квоты — те же константы, что в линте, с полом на коротких роликах."""
    sources = _sources(12)  # 12 × 5 с = 60 с
    prompt = plan_prompt(
        sources=sources, retention=None, rules="", blueprint="", episode="",
        styles=[], tracks=["black_vortex (drop at 45s)"], sfx=SOUNDS,
    )  # fmt: skip
    assert "sound effects (id | kind | length | what it is)" in prompt
    assert "whoosh | whoosh | 0.7s | Пролёт" in prompt
    assert "impact_deep | impact | 0.9s | Удар (низкий)" in prompt
    assert "TIMELINE" in prompt and '"effects": [' in prompt and '"outro": null' in prompt
    assert f"At most {timeline_quota(60, EFFECTS_PER_MIN_WARN, EFFECTS_FLOOR)} effects" in prompt
    assert f"({EFFECTS_PER_MIN_WARN:g} per minute; {EFFECTS_FLOOR}" in prompt
    assert f"At most {timeline_quota(60, SFX_PER_MIN_WARN, SFX_FLOOR)} sounds" in prompt
    assert f"({SFX_PER_MIN_WARN:g} per minute; {SFX_FLOOR}" in prompt
    lo, hi = SFX_GAIN_RANGE_DB
    assert f"{lo:g}…{hi:g}" in prompt
    # текст промпта переносится по строкам — сверяем числа, а не фразы целиком
    assert f"longer than {EFFECT_MAX_SEC:g} s is colour" in prompt
    assert f"{OUTRO_SPEED_RANGE[0]:g}–{OUTRO_SPEED_RANGE[1]:g}, take" in prompt
    assert "drop at 45s" in prompt and "`music.start_sec` = climax − N" in prompt
    assert "in this 60 s video" in prompt  # шкала — сумма коробок, как `at` в MATERIAL

    # без звуков в ресурсах модели прямо сказано их не возвращать
    empty = plan_prompt(
        sources=sources, retention=None, rules="", blueprint="", episode="", styles=[], tracks=[]
    )
    assert "(none in resources/sfx/ — return no sfx)" in empty
    assert sfx_text([]) in empty


def test_timeline_quota_has_a_floor_for_short_videos() -> None:
    """Восьмисекундный клип по ставке «в минуту» не имел бы права ни на что,
    а линт ему разрешает пол — промпт обязан говорить то же самое."""
    assert timeline_quota(8.0, EFFECTS_PER_MIN_WARN, EFFECTS_FLOOR) == EFFECTS_FLOOR
    assert timeline_quota(600.0, EFFECTS_PER_MIN_WARN, EFFECTS_FLOOR) == int(
        EFFECTS_PER_MIN_WARN * 10
    )
    assert timeline_quota(0.0, SFX_PER_MIN_WARN, SFX_FLOOR) == SFX_FLOOR


# ---------------------------------------------------------------- слияние и линт


def _storyboard(n: int = 3) -> Storyboard:
    scenes = [
        Scene(
            id=f"s{i}",
            order=i,
            duration_sec=5.0,
            narration=f"слово{i} текст.",
            assets=SceneAssets(video=f"03_video/s{i}.mp4"),
            measured_sec={"audio": 4.0},
        )
        for i in range(1, n + 1)
    ]
    return Storyboard(project_id="t", scenes=scenes)


def test_merge_takes_the_four_timeline_lists() -> None:
    raw = {
        "scenes": [],
        "music": {"start_sec": -20, "cues": [{"at_sec": 7.5, "gain_db": -6, "why": "дроп"}]},
        "effects": [{"at_sec": 5.0, "kind": "zoom_punch", "why": "склейка"}],
        "sfx": [{"at_sec": 4.7, "id": "whoosh", "why": "пролёт до склейки"}],
        "outro": {"take_sec": 3, "speed": 0.5, "why": "последний кадр держится"},
    }
    plan = merge_deviations(default_plan(_storyboard()), raw, llm="mock")
    assert plan.author == "llm" and plan.llm == "mock"
    assert [e.scene_id for e in plan.scenes] == ["s1", "s2", "s3"]  # заготовка сохранена
    assert plan.effects[0].kind == "zoom_punch" and plan.effects[0].duration_sec == 0.4
    assert plan.sfx[0].id == "whoosh" and plan.sfx[0].gain_db is None
    assert plan.music is not None and plan.music.start_sec == -20
    assert plan.music.cues[0].gain_db == -6 and plan.music.cues[0].why == "дроп"
    assert plan.outro is not None and plan.outro.take_sec == 3 and plan.outro.pitch_drop

    # пропущенное — «ничего», как и у сцен
    bare = merge_deviations(default_plan(_storyboard()), {"scenes": []}, llm="mock")
    assert bare.effects == [] and bare.sfx == [] and bare.outro is None


def test_merge_names_the_broken_field_for_the_retry() -> None:
    base = default_plan(_storyboard())
    with pytest.raises(ValueError, match="kind"):
        merge_deviations(base, {"effects": [{"at_sec": 1, "kind": "explode"}]}, llm="mock")
    with pytest.raises(ValueError, match="at_sec"):
        merge_deviations(base, {"sfx": [{"id": "whoosh"}]}, llm="mock")
    with pytest.raises(ValueError, match="outro"):
        merge_deviations(base, {"outro": "yes"}, llm="mock")
    with pytest.raises(ValueError, match="effects"):
        merge_deviations(base, {"effects": {"at_sec": 1}}, llm="mock")


def test_lint_drops_a_model_effect_outside_the_video_and_orders_by_time() -> None:
    """Эффект за концом ролика молча не сработал бы; неизвестный звук — блок,
    и это причина повтора. Порядок по времени — для глаза, без записи в починки."""
    sb = _storyboard()  # 15 с
    raw = {
        "effects": [
            {"at_sec": 40.0, "kind": "flash", "why": "за концом"},
            {"at_sec": 10.0, "kind": "shake", "why": "второй"},
            {"at_sec": 5.0, "kind": "zoom_punch", "why": "первый"},
        ],
        "sfx": [
            {"at_sec": 9.7, "id": "whoosh", "why": "есть"},
            {"at_sec": 1.0, "id": "no_such_sound", "why": "нет"},
        ],
        "music": {"cues": [{"at_sec": 12, "gain_db": -6}, {"at_sec": 3, "gain_db": -20}]},
    }
    plan = merge_deviations(default_plan(sb), raw, llm="mock")
    result = lint_plan(
        plan, sb, duration_of=lambda _: 5.0, style_exists=lambda _: True,
        track_exists=lambda _: True, sfx_exists=lambda rid: rid == "whoosh",
    )  # fmt: skip
    assert [e.kind for e in result.plan.effects] == ["zoom_punch", "shake"]
    assert any("flash" in f and "вне ролика" in f for f in result.fixes)
    blocks = [f for f in result.blocking if f.code == "unknown_sfx"]
    assert len(blocks) == 1 and "no_such_sound" in blocks[0].message
    assert result.plan.music is not None
    assert [c.at_sec for c in result.plan.music.cues] == [3, 12]
    assert not any("порядок" in f for f in result.fixes)


# ------------------------------------------------------------ конвейер на заглушках

STOP_AT_EDIT = RunOptions(auto_approve=True, raise_errors=True, stop="edit")


def _first_sound_id(prompt: str) -> str:
    """Тот же разбор блока AVAILABLE, что у заглушки: id, вид, длина."""
    kinds = "impact|whoosh|riser|sub_drop|click|glitch|other"
    m = re.search(rf"^(\S+) \| (?:{kinds}) \| ", prompt, re.M)
    assert m, "в промпте нет блока звуков"
    return m.group(1)


@pytest.mark.render
def test_unknown_sfx_earns_one_retry_with_the_reason(
    episode: Path, mock_providers: ProviderSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake(ctx, prompt, **kw):  # type: ignore[no-untyped-def]
        prompts.append(prompt)
        sound = "no_such_sound" if len(prompts) == 1 else _first_sound_id(prompt)
        return {"sfx": [{"at_sec": 0.2, "id": sound, "why": "тест"}]}

    monkeypatch.setattr(edit_stage, "ask_json", fake)
    report = run_episode(episode, mock_providers, STOP_AT_EDIT)
    assert report.ok, report.reason
    assert len(prompts) == 2
    assert "REJECTED" in prompts[1] and "no_such_sound" in prompts[1]
    assert "resources/sfx/" in prompts[1]  # причина — текст блока линта, как есть
    plan = load_plan(episode)
    assert plan is not None and plan.author == "llm" and plan.llm == "mock"
    assert [c.id for c in plan.sfx] == [_first_sound_id(prompts[1])]
    assert "вызовов модели 2" in _stage_message(report, "edit")
    assert "звуков 1" in _stage_message(report, "edit")


@pytest.mark.render
def test_unknown_sfx_twice_fails_and_keeps_the_answer(
    episode: Path, mock_providers: ProviderSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fake(ctx, prompt, **kw):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return {"sfx": [{"at_sec": 0.2, "id": "no_such_sound", "why": "упрямо"}]}

    monkeypatch.setattr(edit_stage, "ask_json", fake)
    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True, stop="edit"))
    assert not report.ok and report.stopped_at == "edit"
    assert "no_such_sound" in (report.reason or "") and "author: manual" in (report.reason or "")
    assert calls == 2  # третьего захода нет — дальше решает человек
    plan = load_plan(episode)
    assert plan is not None and plan.author == "llm"
    assert [c.id for c in plan.sfx] == ["no_such_sound"]  # как вернула модель, без починок
