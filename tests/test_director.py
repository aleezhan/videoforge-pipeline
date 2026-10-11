"""Сценарист: тема → подачи → цепочка гэпов → сценарий → критик → удержание."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from videoforge.core.io import read_model
from videoforge.core.manifest import load_manifest
from videoforge.core.project import SCRIPT_DIR, ProjectSpec, create_episode, create_project
from videoforge.core.runner import run_episode
from videoforge.core.script import (
    ANGLES_FILE,
    CRITIQUE_FILE,
    RETENTION_FILE,
    ROUNDS_DIR,
    SCRIPT_FILE,
    STORY_FILE,
    Angles,
    Retention,
    Story,
    parse_script,
)
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, load_storyboard, save_storyboard
from videoforge.providers.base import ProviderSet

ARTIFACTS = (ANGLES_FILE, STORY_FILE, SCRIPT_FILE, CRITIQUE_FILE, RETENTION_FILE)


@pytest.fixture
def topic_episode(projects_root: Path) -> Path:
    """Эпизод, у которого есть только тема: остальное должен написать сценарист."""
    create_project(
        ProjectSpec(
            id="d",
            name="Сценарист",
            platforms=["youtube_shorts"],
            character="timur",
            style="pixar3d",
            voice="ru_male",
            target_duration_sec=30,
        )
    )
    return create_episode("d", "e1", title="проба", brief="почему компасы врут в горах")


def script_dir(episode: Path) -> Path:
    return episode / SCRIPT_DIR


def run_director(episode: Path, providers: ProviderSet, **opts: object) -> object:
    return run_episode(
        episode,
        providers,
        RunOptions(auto_approve=True, raise_errors=True, only="director", **opts),  # type: ignore[arg-type]
    )


def test_director_writes_every_artifact(topic_episode: Path, mock_providers: ProviderSet) -> None:
    run_director(topic_episode, mock_providers)
    out = script_dir(topic_episode)
    for name in ARTIFACTS:
        assert (out / name).exists(), f"нет {name}"

    angles = read_model(out / ANGLES_FILE, Angles)
    assert 5 <= len(angles.options) <= 10
    assert angles.chosen in {a.id for a in angles.options}
    # выбирается подача с максимальной суммой оценок, а не первая попавшаяся
    assert angles.chosen == max(angles.options, key=lambda a: a.total).id

    story = read_model(out / STORY_FILE, Story)
    assert story.angle_id == angles.chosen
    assert story.gaps and story.ending
    assert not story.gaps[-1].opens  # последнее звено закрывает разрыв, а не открывает

    retention = read_model(out / RETENTION_FILE, Retention)
    assert retention.beats and retention.score_reasons
    assert (out / ROUNDS_DIR / "round_01.md").exists()


def test_speech_density_is_checked_by_python(
    topic_episode: Path, mock_providers: ProviderSet
) -> None:
    """Недобор речи — арифметика, а не суждение: модель его систематически не видит,
    и на длинном формате из этого выходит ролик с провалами тишины."""
    from videoforge.stages.director import _short_blocks

    short = "## g1 · Блок · 40s\n\n**Текст:** Три слова всего.\n**Кадр:** frame\n"
    findings = _short_blocks(short)
    assert len(findings) == 1
    assert findings[0].blocking
    assert findings[0].where == "g1"
    assert "40" in findings[0].why_viewer_leaves

    full_words = " ".join(f"слово{n}" for n in range(75))  # 40 с × 0.85 × 2.2 ≈ 75
    assert not _short_blocks(f"## g1 · Блок · 40s\n\n**Текст:** {full_words}\n")

    # заглушка пишет ровно норму — значит в прогоне эта находка не всплывает
    run_director(topic_episode, mock_providers)
    critique = json.loads((script_dir(topic_episode) / CRITIQUE_FILE).read_text(encoding="utf-8"))
    assert not [f for f in critique["findings"] if "с речи при заявленных" in f["fix"]]


def test_retention_measures_dead_zones_in_python(
    topic_episode: Path, mock_providers: ProviderSet
) -> None:
    """«Уберём — станет хуже» судится по блокам, мёртвые зоны считает код."""
    run_director(topic_episode, mock_providers)
    retention = read_model(script_dir(topic_episode) / RETENTION_FILE, Retention)

    assert retention.blocks, "приговор по блокам не выставлен"
    assert any(b.removable for b in retention.blocks)  # заглушка помечает один блок лишним
    assert all(b.id for b in retention.blocks)
    assert retention.score and retention.score_reasons  # балл ставится вторым шагом, по числам


def test_narration_spanning_several_paragraphs_is_counted_whole() -> None:
    """Длинную реплику модель разбивает на абзацы. Считать одну первую строку —
    значит недосчитать речь в разы и завалить сценарий ложными находками."""
    from videoforge.stages.director import _short_blocks

    md = (
        "## g1 · Блок · 12s\n\n"  # 19 слов на 12 с — норма, находкой быть не должен
        "**Текст:** Первый абзац реплики.\n\n"
        "Второй абзац той же реплики, он тоже звучит.\n\n"
        "И третий абзац, который договаривает мысль до конца.\n\n"
        "**Кадр:** широкий план, который в счёт речи не идёт вообще никак\n"
        "**Эмоция:** и эта строка тоже не речь\n\n"
        "## g2 · Второй · 10s\n\n"
        "**Текст:** короткая реплика\n"
    )
    blocks = parse_script(md)
    assert [b.id for b in blocks] == ["g1", "g2"]
    assert blocks[0].planned_sec == 12.0
    assert blocks[0].words == 19  # 3 + 8 + 8: все три абзаца, без «Кадр» и «Эмоция»
    assert "широкий план" not in blocks[0].text
    assert blocks[1].text == "короткая реплика"
    # короткий второй блок находкой быть обязан, первый — нет
    assert [f.where for f in _short_blocks(md)] == ["g2"]


def test_dead_zones_count_the_unmarked_tail() -> None:
    """Разметка, оборванная на середине, — это не «конец», а мёртвая зона."""
    from videoforge.core.script import Beat, dead_zones

    beats = [Beat(t_sec=0), Beat(t_sec=10), Beat(t_sec=20)]
    zones = dead_zones(beats, limit_sec=25, total_sec=100)
    assert len(zones) == 1
    assert (zones[0].start_sec, zones[0].end_sec) == (20, 100)

    inner = dead_zones([Beat(t_sec=0), Beat(t_sec=60)], limit_sec=25, total_sec=60)
    assert len(inner) == 1 and inner[0].seconds == 60


def test_story_follows_the_script_when_the_critic_drops_a_block(
    topic_episode: Path, mock_providers: ProviderSet
) -> None:
    """Критик вправе слить звенья; цепочка обязана описывать то, что написано."""
    from videoforge.stages.director import _sync_story

    run_director(topic_episode, mock_providers)
    out = script_dir(topic_episode)
    story = read_model(out / STORY_FILE, Story)
    assert {g.id for g in story.gaps} == {
        b.id for b in parse_script((out / SCRIPT_FILE).read_text(encoding="utf-8"))
    }

    # сценарий, из которого выпал средний блок и у которого сдвинута длительность
    (out / SCRIPT_FILE).write_text(
        "## g1 · Первый · 4s\n\n**Текст:** раз\n\n## g3 · Третий · 30s\n\n**Текст:** три\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(log=logging.getLogger("test"))
    assert _sync_story(ctx, story, out / SCRIPT_FILE)  # type: ignore[arg-type]
    assert [g.id for g in story.gaps] == ["g1", "g3"]
    assert [g.planned_sec for g in story.gaps] == [4.0, 30.0]


def test_director_is_idempotent(topic_episode: Path, mock_providers: ProviderSet) -> None:
    run_director(topic_episode, mock_providers)
    out = script_dir(topic_episode)
    before = {name: (out / name).stat().st_mtime_ns for name in ARTIFACTS}

    report = run_director(topic_episode, mock_providers)
    assert report.ok  # type: ignore[attr-defined]
    assert {name: (out / name).stat().st_mtime_ns for name in ARTIFACTS} == before


def test_hand_edited_angle_rebuilds_everything_below(
    topic_episode: Path, mock_providers: ProviderSet
) -> None:
    """Человек поправил выбор подачи — цепочка ниже пересобирается, сам файл не трогают."""
    run_director(topic_episode, mock_providers)
    out = script_dir(topic_episode)
    angles_path = out / ANGLES_FILE
    data = json.loads(angles_path.read_text(encoding="utf-8"))
    other = next(a["id"] for a in data["options"] if a["id"] != data["chosen"])
    data["chosen"] = other
    angles_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    story_before = (out / STORY_FILE).stat().st_mtime_ns

    run_director(topic_episode, mock_providers)
    assert json.loads(angles_path.read_text(encoding="utf-8"))["chosen"] == other
    assert (out / STORY_FILE).stat().st_mtime_ns != story_before
    assert read_model(out / STORY_FILE, Story).angle_id == other


def test_broken_choice_fails_before_the_next_step(
    topic_episode: Path, mock_providers: ProviderSet
) -> None:
    """Опечатка в chosen должна падать здесь, а не уезжать в сценарий молча."""
    run_director(topic_episode, mock_providers)
    angles_path = script_dir(topic_episode) / ANGLES_FILE
    data = json.loads(angles_path.read_text(encoding="utf-8"))
    data["chosen"] = "нет-такой-подачи"
    angles_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    report = run_episode(
        topic_episode, mock_providers, RunOptions(auto_approve=True, only="director")
    )
    assert not report.ok
    assert "нет-такой-подачи" in (report.reason or "")


def test_critique_loop_respects_the_cap(topic_episode: Path, mock_providers: ProviderSet) -> None:
    """Цикл черновик → критик ограничен max_attempts, а не крутится сам по себе."""

    class Blocking(type(mock_providers.llm)):  # type: ignore[misc]
        """Критик, которого ничем не устроить."""

        def complete(self, prompt: str, **kw: object) -> object:
            result = super().complete(prompt, **kw)  # type: ignore[arg-type]
            if prompt.startswith("VIDEOFORGE-STEP: critique"):
                data = json.loads(result.text)
                data["findings"][0]["blocking"] = True
                result.text = json.dumps(data, ensure_ascii=False)
            return result

    mock_providers.llm = Blocking()
    run_episode(
        topic_episode,
        mock_providers,
        RunOptions(auto_approve=True, raise_errors=True, only="director", max_attempts=2),
    )
    rounds = sorted((script_dir(topic_episode) / ROUNDS_DIR).glob("round_*.md"))
    assert len(rounds) == 2  # потолок соблюдён, бесконечного цикла нет


def test_hand_written_script_is_left_alone(
    topic_episode: Path, mock_providers: ProviderSet
) -> None:
    out = script_dir(topic_episode)
    out.mkdir(parents=True, exist_ok=True)
    (out / SCRIPT_FILE).write_text("# Мой сценарий\n", encoding="utf-8")

    run_director(topic_episode, mock_providers)
    assert (out / SCRIPT_FILE).read_text(encoding="utf-8") == "# Мой сценарий\n"
    assert not (out / ANGLES_FILE).exists()
    assert load_manifest(topic_episode).stages["director"].status == "skipped"


def test_hand_written_storyboard_is_left_alone(
    topic_episode: Path, mock_providers: ProviderSet
) -> None:
    sb = load_storyboard(topic_episode)
    sb.scenes = [Scene(id="s1", order=1, narration="своё", visual_prompt="own")]
    save_storyboard(topic_episode, sb)

    run_director(topic_episode, mock_providers)
    assert not (script_dir(topic_episode) / ANGLES_FILE).exists()
    assert load_manifest(topic_episode).stages["director"].status == "skipped"


def test_empty_episode_fails_honestly(projects_root: Path, mock_providers: ProviderSet) -> None:
    create_project(ProjectSpec(id="e", platforms=["youtube_shorts"]))
    episode = create_episode("e", "e1")

    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True, only="director"))
    assert not report.ok
    assert "brief" in (report.reason or "")


def test_gate_stops_after_the_script(topic_episode: Path, mock_providers: ProviderSet) -> None:
    """Сценарий определяет всё, что ниже стоит денег, — человек смотрит его до планнера."""
    report = run_episode(topic_episode, mock_providers, RunOptions(raise_errors=True))
    assert report.awaiting_approval == "director"
    assert "planner" not in load_manifest(topic_episode).stages
