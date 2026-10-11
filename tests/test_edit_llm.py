"""Стадия edit с моделью: план от заглушки разнообразной формы, свежесть по
входам и модели, повтор по блоку линта, ручной план неприкосновенен."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import add_music

from videoforge.core.editplan import (
    EffectCue,
    SourceClip,
    Sources,
    default_plan,
    load_plan,
    plan_path,
    save_plan,
    sources_path,
)
from videoforge.core.io import write_model
from videoforge.core.manifest import load_manifest
from videoforge.core.project import SCRIPT_DIR
from videoforge.core.report import render_report
from videoforge.core.resources import list_resources
from videoforge.core.runner import RunReport, run_episode
from videoforge.core.script import RETENTION_FILE, Beat, Retention
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, SceneAssets, Storyboard, load_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.providers.mock import MockLlm
from videoforge.stages.edit import merge_deviations
from videoforge.stages.edit_lint import CROSSFADE_MAX_SEC, EMPHASIS_MAX, MUSIC_STEPS_WARN
from videoforge.stages.edit_prompts import STEP_EDIT, STEP_MARK, plan_prompt, transition_quota

OPTS = RunOptions(auto_approve=True, raise_errors=True)


def _llm(providers: ProviderSet) -> MockLlm:
    assert isinstance(providers.llm, MockLlm)
    return providers.llm


def _stage_message(report: RunReport, stage: str) -> str:
    return next(r.message for n, r in report.executed if n == stage)


# ------------------------------------------------------------------ конвейер на заглушках


@pytest.mark.render
def test_model_plan_is_varied_and_executed(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    """Заглушка отдаёт отклонения разной формы (переход, акцент, ступень музыки),
    стадия сливает их в полный план, сборка исполняет — и каждое обосновано."""
    add_music(episode, projects_root)
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    plan = load_plan(episode)
    assert plan is not None and plan.author == "llm" and plan.llm == "mock"
    assert [e.scene_id for e in plan.scenes] == ["s1", "s2"]  # полный план, не только отклонения
    deviations = [
        e
        for e in plan.scenes
        if e.transition_in.type != "cut" or e.emphasis or e.music_gain_db is not None
    ]
    assert len(deviations) == 2 and all(e.why for e in deviations)

    sb = load_storyboard(episode)
    s1, s2 = sb.ordered_scenes()
    assert sb.final.edit_author == "llm" and sb.final.transitions == 1
    assert s2.montage.transition_in == "dip_black"
    assert s1.measured_sec["clip"] == pytest.approx(s1.duration_sec, abs=0.05)  # длины не тронуты
    assert sb.final.captions
    ass = (episode / sb.final.captions).read_text(encoding="utf-8")
    assert "\\fscx" in ass  # акцент дошёл до субтитров
    montage = json.loads((episode / "05_assemble" / "montage.json").read_text(encoding="utf-8"))
    # ступень сцены и точечная ступень плана (cue) — в одном списке: дроп
    # случается внутри реплики, а не на границе сцены
    step, cue = montage["music_steps"]
    assert step["scene_id"] == "s2" and step["gain_db"] == -9
    assert step["at_sec"] == pytest.approx(s1.measured_sec["clip"], abs=0.05)
    assert cue["scene_id"] is None and cue["gain_db"] == -12
    assert step["at_sec"] < cue["at_sec"] < s1.duration_sec + s2.duration_sec
    # таймлайн от модели: удар на склейке во вторую сцену и звук кадр в кадр с ним,
    # id звука — настоящий (из AVAILABLE), хвоста нет — и всё это дошло до файла
    (effect,) = plan.effects
    assert effect.kind == "zoom_punch" and effect.why
    assert effect.at_sec == pytest.approx(s1.duration_sec, abs=0.06)  # `at` в MATERIAL с 0.1 с
    (sound,) = plan.sfx
    assert sound.at_sec == effect.at_sec and sound.why
    assert sound.id in list_resources()["sfx"]
    assert plan.outro is None
    assert sb.final.effects == 1 and sb.final.sfx == 1 and sb.final.outro_added_sec == 0
    assert montage["effects"][0]["kind"] == "zoom_punch" and montage["sfx"][0]["id"] == sound.id
    message = _stage_message(report, "edit")
    assert "ступеней музыки 2, эффектов 1, звуков 1, хвоста нет" in message
    assert _llm(mock_providers).edit_calls == 1
    assert load_manifest(episode).spent_usd == 0


@pytest.mark.render
def test_rerun_keeps_the_plan_and_does_not_call_the_model(
    episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(episode, mock_providers, OPTS)
    llm = _llm(mock_providers)
    plan_mtime = plan_path(episode).stat().st_mtime_ns
    sources_mtime = sources_path(episode).stat().st_mtime_ns
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    assert llm.edit_calls == 1
    assert plan_path(episode).stat().st_mtime_ns == plan_mtime
    # материал не менялся — sources.json не переписан, иначе план вечно «устаревал» бы
    assert sources_path(episode).stat().st_mtime_ns == sources_mtime
    assert "актуален" in _stage_message(report, "edit")
    assert load_manifest(episode).spent_usd == 0


@pytest.mark.render
def test_plan_is_redone_when_model_or_inputs_change(
    episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(episode, mock_providers, OPTS)
    llm = _llm(mock_providers)
    llm.name = "other"  # как будто переключились с mock на claude_cli
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    plan = load_plan(episode)
    assert llm.edit_calls == 2 and plan is not None and plan.llm == "other"
    # карта смен сценариста обновилась → план устарел
    write_model(
        episode / SCRIPT_DIR / RETENTION_FILE,
        Retention(llm="other", beats=[Beat(t_sec=0.0, changes=["вопрос"])]),
    )
    run_episode(episode, mock_providers, OPTS)
    assert llm.edit_calls == 3
    run_episode(
        episode,
        mock_providers,
        RunOptions(auto_approve=True, raise_errors=True, only="edit", force=True),
    )
    assert llm.edit_calls == 4


@pytest.mark.render
def test_manual_plan_is_not_touched_by_the_model(
    episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(
        episode, mock_providers, RunOptions(stop="video", auto_approve=True, raise_errors=True)
    )
    plan = default_plan(load_storyboard(episode))
    plan.author = "manual"
    # таймлайн человека: модель теперь пишет эти списки сама, но ручной план
    # она не трогает — ни сцены, ни эффекты
    plan.effects = [EffectCue(at_sec=1.0, duration_sec=0.15, kind="flash", why="рука")]
    save_plan(episode, plan)
    report = run_episode(
        episode, mock_providers, RunOptions(start="edit", auto_approve=True, raise_errors=True)
    )
    assert report.ok, report.reason
    assert _llm(mock_providers).edit_calls == 0
    kept = load_plan(episode)
    assert kept is not None and kept.author == "manual"
    assert [e.kind for e in kept.effects] == ["flash"] and kept.sfx == []
    assert "эффектов 1, звуков 0, хвоста нет" in _stage_message(report, "edit")


@pytest.mark.render
def test_lint_block_gets_exactly_one_retry(episode: Path, mock_providers: ProviderSet) -> None:
    llm = _llm(mock_providers)
    llm.reject_edit_calls = 1  # первый ответ — со сценой, которой нет
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    assert llm.edit_calls == 2
    plan = load_plan(episode)
    assert plan is not None and plan.author == "llm" and plan.scene("ghost") is None
    assert "вызовов модели 2" in _stage_message(report, "edit")


@pytest.mark.render
def test_still_blocked_after_retry_fails_and_keeps_the_answer(
    episode: Path, mock_providers: ProviderSet
) -> None:
    llm = _llm(mock_providers)
    llm.reject_edit_calls = 2
    report = run_episode(episode, mock_providers, OPTS)
    assert not report.ok and report.stopped_at == "edit"
    assert "ghost" in (report.reason or "") and "author: manual" in (report.reason or "")
    assert llm.edit_calls == 2  # третьего захода нет — дальше решает человек
    plan = load_plan(episode)
    assert plan is not None and plan.author == "llm" and plan.scene("ghost") is not None

    # повторный прогон упирается в тот же план, а не зовёт модель снова
    again = run_episode(episode, mock_providers, OPTS)
    assert not again.ok and again.stopped_at == "edit" and llm.edit_calls == 2

    # сборка при этом не встаёт: собирает по умолчанию и говорит об этом громко
    build = run_episode(
        episode, mock_providers, RunOptions(only="assemble", auto_approve=True, raise_errors=True)
    )
    assert build.ok, build.reason
    assert "ПЛАН" in _stage_message(build, "assemble")
    assert load_storyboard(episode).final.edit_author == "default"

    # --force: модель зовут заново, чистый ответ проходит
    fixed = run_episode(
        episode,
        mock_providers,
        RunOptions(only="edit", auto_approve=True, raise_errors=True, force=True),
    )
    assert fixed.ok, fixed.reason
    assert llm.edit_calls == 3
    plan = load_plan(episode)
    assert plan is not None and plan.scene("ghost") is None


@pytest.mark.render
def test_report_shows_why_next_to_each_deviation(
    episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(episode, mock_providers, OPTS)
    sb = load_storyboard(episode)
    text = render_report(episode, sb, load_manifest(episode)).read_text(encoding="utf-8")
    assert "план монтажа / почему" in text
    assert "dip_black 0.4s" in text and "мок: смена масштаба" in text
    assert "план: llm (mock)" in text


# ------------------------------------------------------------------ промпт и слияние


def _sources(n: int) -> Sources:
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


def test_prompt_quotas_come_from_the_lint_constants() -> None:
    """Числа в промпте — те же константы, что проверяет линт: модель, которой
    сказали «не больше N», реже возвращается на переделку."""
    prompt = plan_prompt(
        sources=_sources(12),
        retention=None,
        rules="",
        blueprint="",
        episode="",
        styles=["default (current)"],
        tracks=["pad"],
    )
    assert prompt.startswith(f"{STEP_MARK} {STEP_EDIT}")
    assert "SCENES: s01,s02,s03" in prompt
    assert "s02 | at 5.0s | box 5s" in prompt and '"Реплика 2 про число 20."' in prompt
    assert "| motion: not measured |" in prompt  # без профиля движения — честно «не измерено»
    assert f"at most {transition_quota(12)} of" in prompt and "12 scenes" in prompt
    assert f"at most {EMPHASIS_MAX} words" in prompt
    assert f"{MUSIC_STEPS_WARN} steps in the whole episode" in prompt
    assert f"min({CROSSFADE_MAX_SEC:g} s" in prompt
    assert "REJECTED" not in prompt
    assert (transition_quota(12), transition_quota(15), transition_quota(2)) == (2, 3, 1)

    retry = plan_prompt(
        sources=_sources(2),
        retention=None,
        rules="",
        blueprint="",
        episode="",
        styles=[],
        tracks=[],
        rejected=["сцена ghost не существует"],
    )
    assert "REJECTED" in retry and "- сцена ghost не существует" in retry


def _storyboard() -> Storyboard:
    scenes = [
        Scene(
            id=f"s{i}",
            order=i,
            duration_sec=5.0,
            narration=f"слово{i} текст.",
            assets=SceneAssets(video=f"03_video/s{i}.mp4"),
            measured_sec={"audio": 4.0},
        )
        for i in range(1, 4)
    ]
    return Storyboard(project_id="t", scenes=scenes)


def test_merge_keeps_the_full_plan_and_tolerates_loose_shapes() -> None:
    base = default_plan(_storyboard())
    raw = {
        "caption_style": "kinetic",
        "music": {"gain_db": -14},
        "scenes": [
            {"scene_id": "s2", "transition_in": "dip_black", "emphasis": "слово2", "why": "a"},
            {"scene_id": "s2", "music_gain_db": "-9", "why": "b"},  # сцена названа дважды
            {"scene_id": "ghost", "why": "нет такой — пусть назовёт линт"},
        ],
    }
    plan = merge_deviations(base, raw, llm="mock")
    assert plan.author == "llm" and plan.llm == "mock" and plan.generated_at is not None
    assert [e.scene_id for e in plan.scenes] == ["s1", "s2", "s3", "ghost"]
    s2 = plan.scene("s2")
    assert s2 is not None
    assert s2.transition_in.type == "dip_black" and s2.emphasis == ["слово2"]
    assert s2.music_gain_db == -9 and s2.why == "a; b"
    assert s2.segments[0].source == "03_video/s2.mp4"  # отрезки заготовки сохранены
    assert plan.caption_style == "kinetic" and plan.music is not None
    assert plan.music.gain_db == -14


def test_merge_rejects_a_wrong_shape_with_a_readable_reason() -> None:
    base = default_plan(_storyboard())
    with pytest.raises(ValueError, match="transition_in"):
        merge_deviations(
            base, {"scenes": [{"scene_id": "s1", "transition_in": {"type": "wipe"}}]}, llm="mock"
        )
    with pytest.raises(ValueError, match="объект"):
        merge_deviations(base, [1, 2], llm="mock")


# ------------------------------------------------------------ библиотека приёмов в промпте


def test_prompt_carries_the_library_block() -> None:
    """Приёмы библиотеки — третий вход промпта после правил и blueprint."""
    kwargs: dict = dict(
        sources=_sources(3),
        retention=None,
        rules="",
        blueprint="",
        episode="",
        styles=[],
        tracks=[],
    )
    with_lib = plan_prompt(**kwargs, library="Приёмы из библиотеки — Звук:\n- Ступень: на повороте")
    assert "HOUSE TECHNIQUES" in with_lib and "- Ступень: на повороте" in with_lib
    without = plan_prompt(**kwargs)
    assert "HOUSE TECHNIQUES" in without and "\n(none)\n\nEPISODE" in without


@pytest.mark.render
def test_edit_stage_puts_fitting_techniques_into_the_prompt(
    episode: Path, mock_providers: ProviderSet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стадия сама считает условия ролика и кладёт в промпт подходящие
    реализованные приёмы своих точек; сообщение стадии называет, сколько."""
    import yaml

    from videoforge.stages import edit as edit_stage

    lib = tmp_path / "library" / "audio_layers"
    lib.mkdir(parents=True)
    rows = {
        "steps": {
            "title": "Ступень на повороте",
            "status": "implemented",
            "implemented_by": "план: music_gain_db",
            "applies_when": {"has_speech": True},
        },
        "dream": {
            "title": "Выдох музыкой",
            "status": "recorded",
            "applies_when": {"has_speech": True},
        },
        "clips_only": {
            "title": "Звук исходника",
            "status": "implemented",
            "implemented_by": "amix",
            "applies_when": {"format": "clips_from_long"},
        },
    }
    for rid, data in rows.items():
        doc = {"id": rid, "evidence": [{"source": "тест"}], **data}
        (lib / f"{rid}.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("VF_LIBRARY_DIR", str(lib.parent))

    prompts: list[str] = []
    real = edit_stage.ask_json

    def spy(ctx, prompt, **kw):  # type: ignore[no-untyped-def]
        prompts.append(prompt)
        return real(ctx, prompt, **kw)

    monkeypatch.setattr(edit_stage, "ask_json", spy)
    report = run_episode(
        episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True, stop="edit")
    )
    assert report.ok, report.reason
    (prompt,) = prompts
    assert "Ступень на повороте" in prompt and "исполнение: план: music_gain_db" in prompt
    assert "Выдох музыкой" not in prompt  # recorded: рендер не умеет, модели не предлагаем
    assert "Звук исходника" not in prompt  # условие format не совпало: эпизод ai_video
    assert "приёмов из библиотеки: 1 (audio_layers 1)" in _stage_message(report, "edit")
