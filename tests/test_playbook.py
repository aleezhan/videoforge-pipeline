"""Слой знаний: оглавление research/, слияние playbook и стадия distill."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from videoforge.core.io import read_model
from videoforge.core.playbook import Playbook, PlaybookRule, load_playbook
from videoforge.core.stage import RunOptions
from videoforge.intel.collectors.mock import MockCollector
from videoforge.intel.mock_llm import MockIntelLlm
from videoforge.intel.models import DistillProposal, RuleVerdict
from videoforge.intel.playbook import merge, render_playbook
from videoforge.intel.runner import run_analysis
from videoforge.paths import playbook_path, research_index_path


def _proposal(**kwargs: object) -> DistillProposal:
    return DistillProposal(computed_at=datetime.now(UTC), llm="mock", **kwargs)  # type: ignore[arg-type]


def test_new_rule_starts_as_candidate_and_becomes_confirmed() -> None:
    """Правило становится подтверждённым только со второго канала."""
    book = Playbook(updated_at=datetime.now(UTC))
    rule = PlaybookRule(id="hook_question", text="Начинать с вопроса")
    book = merge(book, _proposal(new_rules=[rule]), "chan_a")
    assert book.rules[0].status == "кандидат"
    assert book.analyses == ["chan_a"]

    book = merge(
        book,
        _proposal(verdicts=[RuleVerdict(id="hook_question", verdict="confirms")]),
        "chan_b",
    )
    assert book.rules[0].held_on == ["chan_a", "chan_b"]
    assert book.rules[0].status == "подтверждено"


def test_contradiction_outweighs_confirmations() -> None:
    """Опровержение важнее любого числа подтверждений — правило становится спорным."""
    book = Playbook(
        updated_at=datetime.now(UTC),
        rules=[PlaybookRule(id="r", text="Правило", held_on=["a", "b", "c"])],
    )
    book = merge(
        book,
        _proposal(verdicts=[RuleVerdict(id="r", verdict="contradicts", why="у них наоборот")]),
        "chan_d",
    )
    assert book.rules[0].status == "спорное"
    assert book.rules[0].contradicted_by == ["chan_d"]
    assert book.rules[0].checks["chan_d"].why == "у них наоборот"

    text = render_playbook(book)
    assert "опровергнуто: chan_d" in text
    assert "не сработало на chan_d: у них наоборот" in text


def test_every_verdict_leaves_its_evidence() -> None:
    """Проверка правила видна в своде, включая «неприменимо».

    Счётчик говорит, сколько каналов правило трогали; чем именно там его
    проверяли — говорят эти строки. «Проверено и не подошло» — знание, от
    молчания оно отличаться обязано."""
    book = Playbook(updated_at=datetime.now(UTC), rules=[PlaybookRule(id="r", text="Правило")])
    book = merge(
        book,
        _proposal(verdicts=[RuleVerdict(id="r", verdict="confirms", why="5 из 5 хитов")]),
        "chan_a",
    )
    book = merge(
        book,
        _proposal(verdicts=[RuleVerdict(id="r", verdict="not_applicable", why="счётчика нет")]),
        "chan_b",
    )
    assert book.rules[0].held_on == ["chan_a"]  # неприменимо не считается подтверждением
    assert book.rules[0].status == "кандидат"

    text = render_playbook(book)
    assert "держится на chan_a: 5 из 5 хитов" in text
    assert "неприменимо на chan_b: счётчика нет" in text


def test_contradiction_narrows_universal_scope() -> None:
    """Опровержение сужает область правила: universal → ниша, где оно держалось.

    Правило, не сработавшее на канале другой ниши, обычно не ошибочно — оно
    оказалось уже, чем выглядело. Обратно, до universal, одна генерация
    расширить его не может."""
    book = Playbook(
        updated_at=datetime.now(UTC),
        rules=[PlaybookRule(id="r", text="Правило", held_on=["a"], note="измерено на анимации")],
    )
    verdict = RuleVerdict(id="r", verdict="contradicts", why="тут иначе", scope="2D-научпоп")
    book = merge(book, _proposal(verdicts=[verdict]), "chan_b")
    assert book.rules[0].scope == "2D-научпоп"
    text = render_playbook(book)
    assert "ниша: 2D-научпоп" in text
    assert "оговорка: измерено на анимации" in text  # оговорка автора не затёрта

    # расширить обратно нельзя: область правила уже не universal
    back = RuleVerdict(id="r", verdict="contradicts", why="и тут", scope="universal")
    book = merge(book, _proposal(verdicts=[back]), "chan_c")
    assert book.rules[0].scope == "2D-научпоп"

    # но снятое опровержение возвращает прежнюю область: подтверждённое правило
    # не должно остаться подписанным чужой нишей
    for chan in ("chan_b", "chan_c"):
        book = merge(book, _proposal(verdicts=[RuleVerdict(id="r", verdict="confirms")]), chan)
    assert book.rules[0].scope == "universal"
    assert book.rules[0].contradicted_by == []


def test_merge_is_idempotent() -> None:
    """Повторный прогон того же анализа не удваивает счётчики."""
    book = Playbook(updated_at=datetime.now(UTC))
    proposal = _proposal(new_rules=[PlaybookRule(id="r", text="Правило")])
    book = merge(book, proposal, "chan")
    book = merge(book, proposal, "chan")
    assert len(book.rules) == 1
    assert book.rules[0].held_on == ["chan"]
    assert book.analyses == ["chan"]


def test_rerun_replaces_own_contribution() -> None:
    """Повторный distill того же анализа заменяет его правила, а не добавляет вторые.

    Новая генерация формулирует те же наблюдения другими словами и под другими
    id: без замены свод копит по набору правил на каждый прогон."""
    book = Playbook(updated_at=datetime.now(UTC))
    book = merge(book, _proposal(new_rules=[PlaybookRule(id="loops", text="Пять петель")]), "a")
    book = merge(book, _proposal(new_rules=[PlaybookRule(id="riddle", text="Одна загадка")]), "a")
    assert [r.id for r in book.rules] == ["riddle"]

    # правило, которое успел подтвердить другой канал, остаётся общим знанием
    book = merge(book, _proposal(verdicts=[RuleVerdict(id="riddle", verdict="confirms")]), "b")
    book = merge(book, _proposal(new_rules=[PlaybookRule(id="third", text="Третье")]), "a")
    assert {r.id for r in book.rules} == {"riddle", "third"}
    assert book.rules[0].held_on == ["a", "b"]


def test_verdict_flips_from_confirm_to_contradict() -> None:
    """Пересмотр вердикта заменяет прежний, а не добавляется к нему."""
    book = Playbook(updated_at=datetime.now(UTC), rules=[PlaybookRule(id="r", text="Правило")])
    book = merge(book, _proposal(verdicts=[RuleVerdict(id="r", verdict="confirms")]), "chan")
    assert book.rules[0].held_on == ["chan"]
    book = merge(book, _proposal(verdicts=[RuleVerdict(id="r", verdict="contradicts")]), "chan")
    assert book.rules[0].held_on == []
    assert book.rules[0].contradicted_by == ["chan"]


def test_verdict_about_a_deleted_rule_is_ignored() -> None:
    """Правило удалили руками — вердикт про него не должен его воскрешать."""
    book = Playbook(updated_at=datetime.now(UTC))
    book = merge(book, _proposal(verdicts=[RuleVerdict(id="gone", verdict="confirms")]), "chan")
    assert book.rules == []


def test_distill_writes_playbook_after_full_run(analysis: Path) -> None:
    """Стадия distill доводит анализ до общего свода правил."""
    run_analysis(
        analysis,
        MockCollector(),
        RunOptions(auto_approve=True, raise_errors=True),
        llm=MockIntelLlm(),
    )
    proposal = read_model(analysis / "07_distill" / "proposal.json", DistillProposal)
    assert len(proposal.new_rules) == 2

    book = load_playbook()
    assert book.analyses == ["chan"]
    assert {r.id for r in book.rules} == {"mock_hook_question", "mock_clip_length"}
    assert all(r.status == "кандидат" for r in book.rules)  # один канал — только кандидаты

    text = playbook_path().read_text(encoding="utf-8")
    assert "Обязательное" in text and "Рекомендации" in text
    assert "mock: клип 2 секунды" in text


def test_index_lists_analyses(analysis: Path) -> None:
    """Оглавление собирается из папок анализов и не требует полного прогона."""
    from videoforge.intel.index import write_index

    run_analysis(
        analysis,
        MockCollector(),
        RunOptions(auto_approve=True, raise_errors=True, stop="stats"),
        llm=MockIntelLlm(),
    )
    path = write_index()
    assert path == research_index_path()
    text = path.read_text(encoding="utf-8")
    assert "Mock Channel" in text
    assert "`chan`" in text
    assert "брифа нет" in text  # formula не доходила — индекс говорит об этом честно
    assert "80 видео" in text
