"""В промпт канала — правила playbook его ниши, и не длиннее потолка.

Свод целиком (research/playbook.md) остаётся человеку и distill; сценарист,
планнер и монтажёр получают правила, которые мерили на анализах канала, плюс
общие. Не влезло — уходят сначала кандидаты, потом слабейшие.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from videoforge.core import project as project_module
from videoforge.core import runner
from videoforge.core.playbook import (
    Playbook,
    PlaybookRule,
    RuleCheck,
    prompt_rules,
    relevant_rules,
)
from videoforge.core.project import (
    ProjectSpec,
    create_episode,
    create_project,
    load_project,
    project_blueprint,
)
from videoforge.core.stage import RunOptions, Stage, StageContext, StageResult
from videoforge.intel.playbook import render_playbook, save_playbook
from videoforge.paths import blueprint_path
from videoforge.providers.base import ProviderSet

OURS = ["chan_a", "chan_b"]
BIG = 1_000_000


def _rule(rule_id: str, **kwargs: Any) -> PlaybookRule:
    return PlaybookRule(id=rule_id, text=f"Правило {rule_id}", **kwargs)


def _book(*rules: PlaybookRule) -> Playbook:
    return Playbook(updated_at=datetime.now(UTC), analyses=["x"], rules=list(rules))


def _brief(slug: str, text: str) -> None:
    path = blueprint_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- отбор по нише


def test_our_niche_and_universal_go_in_a_foreign_niche_stays_out() -> None:
    """Ниша правила — свободный текст модели, с нишей анализа он не совпадает.
    Принадлежность решают вердикты анализов канала, а не строка scope."""
    book = _book(
        _rule("general"),  # universal, наши анализы его не трогали
        _rule("held_here", scope="длинное просвещение", held_on=["travel", "chan_a"]),
        _rule(
            "broken_here",
            scope="научпоп",
            held_on=["travel", "food"],
            contradicted_by=["chan_b"],
            checks={"chan_b": RuleCheck(verdict="contradicts", why="у нас главы по 3 минуты")},
        ),
        _rule("foreign", scope="нарезки подкастов", held_on=["podcast"]),
        _rule(
            "not_here",
            scope="тревел",
            held_on=["travel"],
            checks={"chan_a": RuleCheck(verdict="not_applicable", why="натурных съёмок нет")},
        ),
    )
    assert [r.id for r in relevant_rules(book, OURS)] == ["general", "held_here", "broken_here"]

    rules = prompt_rules(book, OURS, max_chars=BIG)
    assert (rules.used, rules.relevant, rules.total, rules.dropped) == (3, 3, 5, 0)
    assert "Правило foreign" not in rules.text
    assert "Правило not_here" not in rules.text
    # опровержение на нашем канале — знание, а не повод промолчать
    assert "не сработало на chan_b: у нас главы по 3 минуты" in rules.text


def test_prompt_rules_keep_the_playbook_format_with_our_checks_only() -> None:
    """Правило в промпте выглядит так же, как в playbook.md; проверки чужих
    каналов — замеры чужих ниш, в промпт они не идут."""
    rule = _rule(
        "r",
        scope="научпоп",
        held_on=["travel", "chan_a"],
        note="измерено на 8 роликах",
        checks={
            "travel": RuleCheck(verdict="confirms", why="чужой замер"),
            "chan_a": RuleCheck(verdict="confirms", why="6 из 6 хитов"),
        },
    )
    book = _book(rule)
    text = prompt_rules(book, OURS, max_chars=BIG).text
    full = render_playbook(book)
    for line in (
        "- ✔ **Правило r**",
        "  - держится: 2 (travel, chan_a) · ниша: научпоп",
        "  - оговорка: измерено на 8 роликах",
        "  - держится на chan_a: 6 из 6 хитов",
    ):
        assert line in text.splitlines()
        assert line in full.splitlines()
    assert "чужой замер" in full
    assert "чужой замер" not in text


# ---------------------------------------------------------------- потолок


def test_over_the_cap_candidates_go_first_then_the_weakest() -> None:
    book = _book(
        _rule("candidate", held_on=["chan_a"]),  # один канал
        _rule("only_broken", held_on=["x", "y"], contradicted_by=["chan_a"]),
        _rule("split", held_on=["chan_a", "x"], contradicted_by=["chan_b"]),
        _rule("strong", held_on=["chan_a", "chan_b", "x", "y"]),
    )
    order = ["strong", "split", "only_broken", "candidate"]  # от сильного к слабому
    full = prompt_rules(book, OURS, max_chars=BIG)
    assert full.used == 4

    counts = set()
    for cap in range(len(full.text), -1, -5):
        rules = prompt_rules(book, OURS, max_chars=cap)
        kept = [rid for rid in order if f"**Правило {rid}**" in rules.text]
        assert kept == order[: rules.used], cap  # уходит всегда хвост слабейших
        assert len(rules.text) <= cap
        assert rules.dropped == 4 - rules.used
        counts.add(rules.used)
    assert counts == {0, 1, 2, 3, 4}


def test_a_short_weak_rule_does_not_take_the_place_of_a_long_strong_one() -> None:
    weak = _rule("weak", held_on=["chan_a"])
    strong = PlaybookRule(
        id="strong", text="Сильное правило, " + "длинное " * 60, held_on=["chan_a", "chan_b"]
    )
    only_weak = len(prompt_rules(_book(weak), OURS, max_chars=BIG).text)
    rules = prompt_rules(_book(weak, strong), OURS, max_chars=only_weak)
    assert (rules.used, rules.dropped, rules.text) == (0, 2, "")


# ---------------------------------------------------------------- blueprint проекта


def test_project_without_research_gets_no_playbook(
    projects_root: Path, research_root: Path
) -> None:
    save_playbook(_book(_rule("general")))
    create_project(ProjectSpec(id="bare"))
    blueprint = project_blueprint(load_project("bare"))
    assert (blueprint.text, blueprint.note) == ("", "")


def test_analysis_without_playbook_json_gives_its_brief(
    projects_root: Path, research_root: Path
) -> None:
    _brief("chan_a", "# бриф\n- глава 3 минуты\n")
    create_project(ProjectSpec(id="p", research=["chan_a"]))
    blueprint = project_blueprint(load_project("p"))
    assert (blueprint.text, blueprint.note) == ("# бриф\n- глава 3 минуты\n", "")


def test_briefs_count_toward_the_cap(
    projects_root: Path, research_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = "# бриф\n" + "про главы " * 30
    _brief("chan_a", brief)  # у chan_b брифа нет — это не ошибка
    save_playbook(_book(*[_rule(f"r{i}", held_on=OURS) for i in range(20)]))
    create_project(ProjectSpec(id="p", research=OURS))
    cfg = load_project("p")

    roomy = project_blueprint(cfg)
    assert roomy.rules is not None and roomy.rules.used == 20
    assert roomy.text.startswith(brief)

    monkeypatch.setattr(project_module, "BLUEPRINT_MAX_CHARS", len(roomy.text) - 1)
    tight = project_blueprint(cfg)
    assert tight.rules is not None and 0 < tight.rules.used < 20
    assert len(tight.text) < len(roomy.text)
    assert tight.text.startswith(brief)  # бриф не режется, режется playbook
    assert tight.note == (
        f"правил playbook: {tight.rules.used} из 20 (ниша: chan_a, chan_b; в своде 20), "
        f"не влезло {20 - tight.rules.used}"
    )

    monkeypatch.setattr(project_module, "BLUEPRINT_MAX_CHARS", len(brief))  # бриф занял всё
    starved = project_blueprint(cfg)
    assert starved.rules is not None and starved.rules.used == 0
    assert starved.text == brief


# ---------------------------------------------------------------- сообщение стадии


class _TakesBlueprint(Stage):
    name = "director"
    needs: ClassVar[tuple[str, ...]] = ()
    changed: ClassVar[bool] = True

    def run(self, ctx: StageContext) -> StageResult:
        assert "Правило here" in ctx.blueprint
        return StageResult(status="done", message="сценарий готов", changed=self.changed)


class _IgnoresBlueprint(Stage):
    name = "planner"
    needs: ClassVar[tuple[str, ...]] = ()

    def run(self, ctx: StageContext) -> StageResult:
        return StageResult(status="done", message="сцены готовы", changed=True)


def test_the_stage_that_took_the_blueprint_says_how_many_rules_went_in(
    projects_root: Path,
    research_root: Path,
    mock_providers: ProviderSet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_playbook(
        _book(
            _rule("here", held_on=["chan_a"]),
            _rule("foreign", scope="нарезки", held_on=["podcast"]),
        )
    )
    create_project(ProjectSpec(id="p", research=["chan_a"]))
    edir = create_episode("p", "e1", title="проба")
    monkeypatch.setattr(
        runner,
        "stage_registry",
        lambda: {"director": _TakesBlueprint, "planner": _IgnoresBlueprint},
    )
    options = RunOptions(stop="planner", auto_approve=True, force=True, raise_errors=True)

    report = runner.run_episode(edir, mock_providers, options)
    messages = {name: result.message for name, result in report.executed}
    assert messages == {
        "director": "сценарий готов · правил playbook: 1 из 1 (ниша: chan_a; в своде 2), "
        "не влезло 0",
        "planner": "сцены готовы",  # blueprint не брала — и строки нет
    }

    # стадия ничего не сделала — промпта не было, и говорить о правилах незачем
    monkeypatch.setattr(_TakesBlueprint, "changed", False)
    report = runner.run_episode(edir, mock_providers, options)
    assert dict((n, r.message) for n, r in report.executed)["director"] == "сценарий готов"
