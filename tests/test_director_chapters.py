"""Сценарист по главам: длинный ролик пишется кусками, короткий — как раньше."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from videoforge.core.io import read_model
from videoforge.core.manifest import load_manifest
from videoforge.core.project import SCRIPT_DIR, ProjectSpec, create_episode, create_project
from videoforge.core.runner import run_episode
from videoforge.core.script import (
    CHAPTERS_DIR,
    CHAPTERS_FILE,
    CRITIQUE_FILE,
    RETENTION_FILE,
    ROUNDS_DIR,
    SCRIPT_FILE,
    SEAMS_FILE,
    STORY_FILE,
    Chapter,
    ChapterCritique,
    ChapterPlan,
    Critique,
    Gap,
    Story,
    assemble_script,
    parse_script,
    split_chapters,
    words_for,
)
from videoforge.core.stage import RunOptions
from videoforge.providers.base import ProviderSet
from videoforge.stages import director_chapters
from videoforge.stages.director_chapters import (
    CHAPTER_MAX_SEC,
    apply_numbers,
    chapter_bounds,
    chapter_problems,
    is_long,
    link_budget,
)

LONG_SEC = 900  # 15 минут: 3–10 глав, заглушка раскладывает 11 звеньев в 4 главы


def _project(pid: str, seconds: int) -> Path:
    create_project(
        ProjectSpec(
            id=pid,
            name="Сценарист",
            platforms=["youtube"],
            character="timur",
            style="pixar3d",
            voice="ru_male",
            target_duration_sec=seconds,
        )
    )
    return create_episode(pid, "e1", title="проба", brief="почему компасы врут в горах")


@pytest.fixture
def long_episode(projects_root: Path) -> Path:
    return _project("long", LONG_SEC)


def run_director(episode: Path, providers: ProviderSet, **opts: object) -> object:
    return run_episode(
        episode,
        providers,
        RunOptions(auto_approve=True, raise_errors=True, only="director", **opts),  # type: ignore[arg-type]
    )


class Recording:
    """Обёртка над заглушкой: запоминает промпты по шагам и даёт тесту подменить ответ."""

    def __init__(self, providers: ProviderSet) -> None:
        self.prompts: list[str] = []
        self.hooks: dict[str, object] = {}
        base = type(providers.llm)
        outer = self

        class Llm(base):  # type: ignore[misc, valid-type]
            def complete(self, prompt: str, **kw: object) -> object:
                outer.prompts.append(prompt)
                result = super().complete(prompt, **kw)  # type: ignore[arg-type]
                step = m.group(1) if (m := re.match(r"VIDEOFORGE-STEP:\s*(\w+)", prompt)) else ""
                hook = outer.hooks.get(step)
                if hook is not None:
                    result.text = hook(prompt, result.text)  # type: ignore[operator]
                return result

        providers.llm = Llm()

    def step(self, name: str) -> list[str]:
        return [x for x in self.prompts if re.match(rf"VIDEOFORGE-STEP: {name}\b", x)]

    def drafts_of(self, chapter: str) -> list[str]:
        return [x for x in self.step("draft_chapter") if f"chapter {chapter}," in x]


def out_dir(episode: Path) -> Path:
    return episode / SCRIPT_DIR


def all_mtimes(episode: Path) -> dict[str, int]:
    root = out_dir(episode)
    return {
        f.relative_to(root).as_posix(): f.stat().st_mtime_ns for f in root.rglob("*") if f.is_file()
    }


# ----------------------------------------------------------------- арифметика


def test_seconds_per_chapter_are_counted_by_python() -> None:
    """Доли звеньев — от модели, масштаб — от цели, сумма сходится ровно."""
    story = Story(
        gaps=[
            Gap(id="g1", knows="", wants="", gets="", planned_sec=10),
            Gap(id="g2", knows="", wants="", gets="", planned_sec=20),
            Gap(id="g3", knows="", wants="", gets="", planned_sec=30),
            Gap(id="g4", knows="", wants="", gets="", planned_sec=7),
        ]
    )
    budget = link_budget(story, 1000)
    assert sum(sec for sec, _ in budget.values()) == 1000
    assert budget["g3"][0] == pytest.approx(1000 * 30 / 67, abs=1)
    # норма слов — та же формула, что у блока сценария одним заходом
    assert budget["g2"][1] == words_for(budget["g2"][0])

    plan = ChapterPlan(
        chapters=[
            Chapter(id="ch01", title="а", links=["g1", "g2"], planned_sec=999, words=1),
            Chapter(id="ch02", title="б", links=["g3", "g4"]),
        ]
    )
    apply_numbers(plan, story, 1000)
    # числа, вписанные в план руками или моделью, python пересчитывает
    assert plan.chapters[0].planned_sec == budget["g1"][0] + budget["g2"][0]
    assert plan.chapters[0].words == budget["g1"][1] + budget["g2"][1]
    assert sum(ch.planned_sec for ch in plan.chapters) == 1000


def test_chapter_bounds_and_the_long_threshold() -> None:
    assert not is_long(60) and not is_long(CHAPTER_MAX_SEC)
    assert is_long(CHAPTER_MAX_SEC + 1)
    assert chapter_bounds(1440) == (5, 16)  # 24 минуты: главы от 90 до 300 с
    assert chapter_bounds(600) == (2, 6)


def test_plan_must_cover_the_chain_in_order() -> None:
    """Звено без главы или звено в двух главах — потерянный или повторённый кусок
    сюжета, а не вопрос ритма: такой план не принимается."""
    story = Story(gaps=[Gap(id=f"g{i}", knows="", wants="", gets="") for i in (1, 2, 3)])

    def plan(*links: list[str]) -> ChapterPlan:
        return ChapterPlan(
            chapters=[Chapter(id=f"ch{i}", title="x", links=ls) for i, ls in enumerate(links)]
        )

    with pytest.raises(ValueError, match="нет звеньев"):
        apply_numbers(plan(["g1"], ["g3"]), story, 900)
    with pytest.raises(ValueError, match="двух главах"):
        apply_numbers(plan(["g1", "g2"], ["g2", "g3"]), story, 900)
    with pytest.raises(ValueError, match="порядок"):
        apply_numbers(plan(["g2", "g1"], ["g3"]), story, 900)

    # длина и число глав — не жёсткое условие, а причины повтора
    ok = apply_numbers(plan(["g1", "g2", "g3"]), story, 900)
    problems = chapter_problems(ok, 900)
    assert any("глав 1" in x for x in problems)
    assert any("длиннее" in x for x in problems)


def test_script_parser_knows_chapter_headings() -> None:
    """Заголовок главы не уезжает в реплику последнего блока предыдущей главы,
    а собранный сценарий раскладывается обратно по главам без потерь."""
    chapters = [
        Chapter(id="ch01", title="Первая", planned_sec=40),
        Chapter(id="ch02", title="Вторая", planned_sec=30),
    ]
    texts = {
        "ch01": "## g1 · Блок · 40s\n\n**Текст:** раз два три\n",
        "ch02": "## g2 · Блок · 30s\n\n**Текст:** четыре пять\n**Кадр:** кадр\n",
    }
    md = assemble_script(chapters, texts)
    assert md.startswith("# ch01 · Первая · 40s")
    blocks = parse_script(md)
    assert [(b.id, b.chapter) for b in blocks] == [("g1", "ch01"), ("g2", "ch02")]
    assert blocks[0].text == "раз два три"  # без «# ch02 · Вторая · 30s»
    assert split_chapters(md) == texts


# ----------------------------------------------------------------- короткий


def test_short_video_is_still_one_draft(projects_root: Path, mock_providers: ProviderSet) -> None:
    episode = _project("short", 60)
    rec = Recording(mock_providers)
    run_director(episode, mock_providers)

    out = out_dir(episode)
    assert not (out / CHAPTERS_FILE).exists() and not (out / CHAPTERS_DIR).exists()
    assert not (out / SEAMS_FILE).exists()
    assert len(rec.step("draft")) == 1 and not rec.step("draft_chapter")
    assert "LENGTH:" not in rec.step("story")[0]
    assert (out / ROUNDS_DIR / "round_01.md").exists()
    assert "глав:" not in load_manifest(episode).stages["director"].message


def test_story_synced_to_the_script_does_not_trigger_a_redraft(
    projects_root: Path, mock_providers: ProviderSet
) -> None:
    """Цепочка, поправленная по написанному сценарию, раньше ложилась на диск
    после разбора критика — и следующий запуск считал разбор устаревшим и
    переписывал готовый сценарий заново."""
    episode = _project("sync", 60)
    rec = Recording(mock_providers)

    def off_by_a_second(prompt: str, text: str) -> str:
        data = json.loads(text)
        data["gaps"][0]["planned_sec"] = 5.0  # черновик заглушки пишет g1 на 4 с
        return json.dumps(data, ensure_ascii=False)

    rec.hooks["story"] = off_by_a_second
    run_director(episode, mock_providers)
    assert read_model(out_dir(episode) / STORY_FILE, Story).gaps[0].planned_sec == 4.0

    rec.prompts.clear()
    run_director(episode, mock_providers)
    assert not rec.prompts


# ----------------------------------------------------------------- длинный


def test_long_video_is_written_chapter_by_chapter(
    long_episode: Path, mock_providers: ProviderSet
) -> None:
    rec = Recording(mock_providers)
    run_director(long_episode, mock_providers)
    out = out_dir(long_episode)

    # цепочке сказали длину, план глав сделан, секунды сходятся с целью
    assert "LENGTH: 900 seconds" in rec.step("story")[0]
    plan = read_model(out / CHAPTERS_FILE, ChapterPlan)
    lo, hi = chapter_bounds(LONG_SEC)
    assert lo <= len(plan.chapters) <= hi
    assert sum(ch.planned_sec for ch in plan.chapters) == LONG_SEC
    assert all(ch.planned_sec <= CHAPTER_MAX_SEC for ch in plan.chapters)

    # глава — свой вызов, свой файл, свой разбор
    assert len(rec.step("draft_chapter")) == len(plan.chapters)
    for ch in plan.chapters:
        assert (out / CHAPTERS_DIR / f"{ch.id}.md").exists()
        crit = read_model(out / CHAPTERS_DIR / f"{ch.id}.critique.json", ChapterCritique)
        assert crit.chapter == ch.id and crit.text_sha and crit.plan_sha and not crit.manual

    # script.md собран python-ом в прежнем формате: планнер читает блоки как раньше
    story = read_model(out / STORY_FILE, Story)
    blocks = parse_script((out / SCRIPT_FILE).read_text(encoding="utf-8"))
    assert [b.id for b in blocks] == [g.id for g in story.gaps]
    assert [b.chapter for b in blocks] == [ch.id for ch in plan.chapters for _ in ch.links]
    assert sum(b.planned_sec for b in blocks) == LONG_SEC
    assert not [b for b in blocks if b.text.startswith("#")]

    # вторая глава видит текст первой, первая — что она открывает ролик
    first, second = plan.chapters[0], plan.chapters[1]
    assert "this is the first chapter" in rec.drafts_of(first.id)[0]
    assert "FIRST chapter" in rec.drafts_of(first.id)[0]
    assert f"[{first.links[0]}] слово0" in rec.drafts_of(second.id)[0]
    assert f"picks up where {first.id} stopped" in rec.drafts_of(second.id)[0]
    assert "LAST chapter" in rec.drafts_of(plan.chapters[-1].id)[0]

    # стыки — один проход, свод находок — в critique.json, retention — по сборке
    assert len(rec.step("seams")) == 1
    assert (out / SEAMS_FILE).exists() and (out / RETENTION_FILE).exists()
    critique = json.loads((out / CRITIQUE_FILE).read_text(encoding="utf-8"))
    assert len(critique["findings"]) == len(plan.chapters)  # по мок-находке на главу

    message = load_manifest(long_episode).stages["director"].message
    assert f"глав: {len(plan.chapters)}" in message
    assert f"{first.id} речи" in message and "раундов 1" in message
    assert "шаги: angles, story, chapters, script, seams, retention" in message


def test_long_director_is_idempotent(long_episode: Path, mock_providers: ProviderSet) -> None:
    run_director(long_episode, mock_providers)
    before = all_mtimes(long_episode)
    rec = Recording(mock_providers)

    report = run_director(long_episode, mock_providers)
    assert report.ok  # type: ignore[attr-defined]
    assert all_mtimes(long_episode) == before
    assert not rec.prompts
    assert "сценарий актуален" in load_manifest(long_episode).stages["director"].message


def test_only_the_chapter_with_blocking_findings_is_rewritten(
    long_episode: Path, mock_providers: ProviderSet
) -> None:
    rec = Recording(mock_providers)
    seen: list[str] = []

    def strict_on_ch02(prompt: str, text: str) -> str:
        if "CHAPTER ch02 (" in prompt and "ch02" not in seen:
            seen.append("ch02")
            data = json.loads(text)
            data["findings"][0]["blocking"] = True
            data["findings"][0]["fix"] = "мок: переписать начало главы"
            return json.dumps(data, ensure_ascii=False)
        return text

    rec.hooks["critique_chapter"] = strict_on_ch02
    run_director(long_episode, mock_providers)
    out = out_dir(long_episode)
    plan = read_model(out / CHAPTERS_FILE, ChapterPlan)

    assert len(rec.drafts_of("ch02")) == 2
    assert "мок: переписать начало главы" in rec.drafts_of("ch02")[1]
    assert "THE REJECTED DRAFT" in rec.drafts_of("ch02")[1]  # модель видит, что оставить
    for ch in plan.chapters:
        if ch.id != "ch02":
            assert len(rec.drafts_of(ch.id)) == 1
            assert not (out / CHAPTERS_DIR / ROUNDS_DIR / f"{ch.id}_round_02.md").exists()
    assert (out / CHAPTERS_DIR / ROUNDS_DIR / "ch02_round_02.md").exists()
    assert read_model(out / CHAPTERS_DIR / "ch02.critique.json", ChapterCritique).round == 2
    assert "ch02 речи" in load_manifest(long_episode).stages["director"].message
    assert "раундов 2" in load_manifest(long_episode).stages["director"].message


def test_seams_rewrite_only_the_named_chapter(
    long_episode: Path, mock_providers: ProviderSet
) -> None:
    rec = Recording(mock_providers)

    def repeat_in_ch03(prompt: str, text: str) -> str:
        if len(rec.step("seams")) > 1:
            return text
        return json.dumps(
            {
                "findings": [
                    {
                        "kind": "повтор между главами",
                        "where": "ch03 g7",
                        "why_viewer_leaves": "мок: пример из первой главы звучит второй раз",
                        "fix": "мок: заменить пример в ch03",
                        "blocking": True,
                    }
                ],
                "verdict": "мок: повтор",
            },
            ensure_ascii=False,
        )

    rec.hooks["seams"] = repeat_in_ch03
    run_director(long_episode, mock_providers)
    out = out_dir(long_episode)
    plan = read_model(out / CHAPTERS_FILE, ChapterPlan)

    assert len(rec.step("seams")) == 2  # после переписывания стыки смотрят ещё раз
    assert len(rec.drafts_of("ch03")) == 2
    assert "мок: заменить пример в ch03" in rec.drafts_of("ch03")[1]
    assert all(len(rec.drafts_of(ch.id)) == 1 for ch in plan.chapters if ch.id != "ch03")
    assert (out / ROUNDS_DIR / "seams_round_02.json").exists()
    assert not read_model(out / SEAMS_FILE, Critique).blocking


def test_hand_edited_chapter_keeps_its_neighbours(
    long_episode: Path, mock_providers: ProviderSet
) -> None:
    """Правка одной главы руками: её судит критик, но не переписывает; соседние
    главы не трогаются; script.md и разметка удержания пересобираются."""
    run_director(long_episode, mock_providers)
    out = out_dir(long_episode)
    chapter = out / CHAPTERS_DIR / "ch02.md"
    edited = chapter.read_text(encoding="utf-8").replace("**Текст:** ", "**Текст:** Правка. ", 1)
    chapter.write_text(edited, encoding="utf-8")
    before = all_mtimes(long_episode)

    rec = Recording(mock_providers)
    run_director(long_episode, mock_providers)
    after = all_mtimes(long_episode)

    assert not rec.step("draft_chapter")
    assert len(rec.step("critique_chapter")) == 1 and "CHAPTER ch02 (" in rec.prompts[0]
    for name in ("ch01.md", "ch03.md", "ch01.critique.json", "ch03.critique.json"):
        assert after[f"{CHAPTERS_DIR}/{name}"] == before[f"{CHAPTERS_DIR}/{name}"]
    assert chapter.read_text(encoding="utf-8") == edited
    assert "Правка." in (out / SCRIPT_FILE).read_text(encoding="utf-8")
    assert after[RETENTION_FILE] != before[RETENTION_FILE]
    assert read_model(out / CHAPTERS_DIR / "ch02.critique.json", ChapterCritique).manual
    assert "ch02 речи" in load_manifest(long_episode).stages["director"].message
    assert "правлена руками" in load_manifest(long_episode).stages["director"].message

    # третий запуск: правка принята, делать нечего
    settled = all_mtimes(long_episode)
    rec.prompts.clear()
    run_director(long_episode, mock_providers)
    assert all_mtimes(long_episode) == settled and not rec.prompts


def test_hand_edited_script_goes_back_into_chapters(
    long_episode: Path, mock_providers: ProviderSet
) -> None:
    """script.md длинного ролика собирается из глав — правка в нём не должна
    стираться следующей сборкой, она раскладывается в свою главу."""
    run_director(long_episode, mock_providers)
    out = out_dir(long_episode)
    ch01 = (out / CHAPTERS_DIR / "ch01.md").stat().st_mtime_ns
    script = out / SCRIPT_FILE
    md = script.read_text(encoding="utf-8")
    head, tail = md.split("# ch03 ·", 1)
    script.write_text(head + "# ch03 ·" + tail.replace("слово1 ", "исправлено ", 1), "utf-8")

    rec = Recording(mock_providers)
    run_director(long_episode, mock_providers)

    assert not rec.step("draft_chapter")
    assert "исправлено" in (out / CHAPTERS_DIR / "ch03.md").read_text(encoding="utf-8")
    assert "исправлено" not in (out / CHAPTERS_DIR / "ch02.md").read_text(encoding="utf-8")
    assert (out / CHAPTERS_DIR / "ch01.md").stat().st_mtime_ns == ch01
    assert read_model(out / CHAPTERS_DIR / "ch03.critique.json", ChapterCritique).manual


def test_deleted_chapter_is_written_again_alone(
    long_episode: Path, mock_providers: ProviderSet
) -> None:
    """Штатный способ попросить модель заново: удалить файл главы."""
    run_director(long_episode, mock_providers)
    out = out_dir(long_episode)
    (out / CHAPTERS_DIR / "ch02.md").unlink()

    rec = Recording(mock_providers)
    run_director(long_episode, mock_providers)
    assert len(rec.step("draft_chapter")) == 1 and rec.drafts_of("ch02")
    assert (out / CHAPTERS_DIR / "ch02.md").exists()


def test_script_without_chapter_headings_fails_instead_of_losing_the_edit(
    long_episode: Path, mock_providers: ProviderSet
) -> None:
    run_director(long_episode, mock_providers)
    script = out_dir(long_episode) / SCRIPT_FILE
    script.write_text("## g1 · Всё заново · 900s\n\n**Текст:** свой текст\n", encoding="utf-8")

    report = run_episode(
        long_episode, mock_providers, RunOptions(auto_approve=True, only="director")
    )
    assert not report.ok
    assert "заголовки глав" in (report.reason or "")
    assert "свой текст" in script.read_text(encoding="utf-8")  # правку не стёрли


def test_editing_one_chapter_in_the_plan_redrafts_only_it(
    long_episode: Path, mock_providers: ProviderSet
) -> None:
    run_director(long_episode, mock_providers)
    out = out_dir(long_episode)
    data = json.loads((out / CHAPTERS_FILE).read_text(encoding="utf-8"))
    data["chapters"][2]["about"] = "человек переписал, о чём третья глава"
    (out / CHAPTERS_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    rec = Recording(mock_providers)
    run_director(long_episode, mock_providers)
    assert [re.search(r"chapter (ch\d+),", x).group(1) for x in rec.step("draft_chapter")] == [  # type: ignore[union-attr]
        "ch03"
    ]
    assert "человек переписал" in rec.step("draft_chapter")[0]


def test_script_written_in_one_pass_before_chapters_is_kept(
    long_episode: Path, mock_providers: ProviderSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сценарий длинного ролика, написанный одним заходом до появления глав,
    честно сделан из тогдашних входов — по главам его переписывает только --force."""
    monkeypatch.setattr(director_chapters, "CHAPTER_MAX_SEC", 10_000.0)
    run_director(long_episode, mock_providers)
    out = out_dir(long_episode)
    script = (out / SCRIPT_FILE).read_text(encoding="utf-8")
    monkeypatch.undo()

    rec = Recording(mock_providers)
    run_director(long_episode, mock_providers)
    assert not rec.prompts
    assert not (out / CHAPTERS_FILE).exists()
    assert (out / SCRIPT_FILE).read_text(encoding="utf-8") == script

    run_director(long_episode, mock_providers, force=True)
    assert (out / CHAPTERS_FILE).exists() and rec.step("draft_chapter")
