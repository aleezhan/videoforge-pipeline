"""Свод правил поверх всех анализов: research/playbook.json + playbook.md.

Смысл файла — не копить наблюдения, а отсеивать их. Правило, которое только
подтверждается, знания не добавляет; знание появляется там, где следующий
канал предыдущему противоречит. Поэтому у каждого правила есть счётчики «где
держится» и «где опровергнуто», а статус выводится из них, а не назначается.

Слияние здесь — чистый python. LLM выносит вердикт по каждому правилу и
предлагает новые (стадия distill), но переписать накопленное не может: иначе
одна неудачная генерация тихо испортила бы всю память проекта.

Три уровня правил различаются по проверяемости:
  must   — сверяется с раскадровкой автоматически (длина хука, длина клипа);
  should — эвристика, уезжает в промпт планнера;
  avoid  — антипаттерн.

Модель свода и вид правила в тексте — в core/playbook.py: свод читает и
конвейер эпизода, а в промпт канала уходят не все правила, а его ниши.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from videoforge.core.io import write_model
from videoforge.core.playbook import Playbook, RuleCheck, tier_sections
from videoforge.intel.models import DistillProposal
from videoforge.paths import playbook_json_path, playbook_path


def slugify_rule_id(text: str) -> str:
    """Запасной id, когда LLM его не дала: латиница из первых слов."""
    ascii_only = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return "_".join(ascii_only.split("_")[:5]) or "rule"


def merge(playbook: Playbook, proposal: DistillProposal, slug: str) -> Playbook:
    """Влить вклад одного анализа. Идемпотентно: повторный прогон не удваивает счётчики."""
    # Вклад анализа заменяется, а не копится. Повторный distill (--force, новая
    # генерация) предлагает те же наблюдения другими словами и под другими id —
    # без этой уборки свод получает два набора правил про один канал. Снимается
    # только то, что этот анализ сам и предложил и что с тех пор никто не
    # подтвердил и не опроверг: как только правило тронул второй канал, оно
    # стало общим знанием и одному анализу больше не принадлежит.
    playbook.rules = [
        rule
        for rule in playbook.rules
        if not (rule.introduced_by == slug and rule.held_on == [slug] and not rule.contradicted_by)
    ]
    by_id = {rule.id: rule for rule in playbook.rules}

    for verdict in proposal.verdicts:
        rule = by_id.get(verdict.id)
        if rule is None:  # правило успели удалить руками — вердикт про него не нужен
            continue
        rule.held_on = [s for s in rule.held_on if s != slug]
        rule.contradicted_by = [s for s in rule.contradicted_by if s != slug]
        rule.checks[slug] = RuleCheck(verdict=verdict.verdict, why=verdict.why)
        if verdict.verdict == "confirms":
            rule.held_on.append(slug)
        elif verdict.verdict == "contradicts":
            rule.contradicted_by.append(slug)
            # опровержение почти никогда не отменяет правило целиком — оно
            # показывает его границу. Поэтому «универсальное» правило, не
            # сработавшее на канале другой ниши, переезжает в нишу, где оно
            # держится. Только в эту сторону: расширить область обратно до
            # universal одна генерация не может, это делается новым правилом.
            if rule.scope == "universal" and verdict.scope and verdict.scope != "universal":
                rule.scope_before, rule.scope = rule.scope, verdict.scope
        if not rule.contradicted_by and rule.scope_before:
            # опровержений не осталось (вердикт пересмотрен) — область правила
            # возвращается: подтверждённое правило с чужой нишей в подписи врёт
            rule.scope, rule.scope_before = rule.scope_before, ""

    for candidate in proposal.new_rules:
        rule_id = candidate.id or slugify_rule_id(candidate.text)
        if rule_id in by_id:  # уже знаем — считаем подтверждением, а не дублируем
            existing = by_id[rule_id]
            if slug not in existing.held_on and slug not in existing.contradicted_by:
                existing.held_on.append(slug)
            continue
        fresh = candidate.model_copy(
            update={"id": rule_id, "held_on": [slug], "introduced_by": slug}
        )
        by_id[rule_id] = fresh
        playbook.rules.append(fresh)

    if slug not in playbook.analyses:
        playbook.analyses.append(slug)
    playbook.updated_at = datetime.now(UTC)
    return playbook


def render_playbook(playbook: Playbook) -> str:
    """playbook.json → playbook.md. Этот текст читает планнер и человек."""
    if not playbook.rules:
        return (
            "# Playbook\n\n_Правил пока нет: прогони `videoforge analyze run <slug>` "
            "до стадии distill._\n"
        )
    lines = [
        "# Playbook",
        "",
        "Правила, накопленные по разобранным каналам. Собирается стадией `distill`,",
        "руками не правится — правки затрёт следующий анализ. У каждого правила",
        "видно, на скольких каналах оно держится: `✔ подтверждено` — держится",
        "минимум на двух, `? кандидат` — пока на одном, `! спорное` — где-то",
        "не сработало, и это важнее подтверждений.",
        "",
        f"_Учтено анализов: {len(playbook.analyses)} "
        f"({', '.join(playbook.analyses) or '—'}) · "
        f"обновлено {playbook.updated_at.astimezone():%d.%m.%Y}_",
        "",
    ]
    lines += tier_sections(playbook.rules)
    return "\n".join(lines) + "\n"


def save_playbook(playbook: Playbook) -> Path:
    write_model(playbook_json_path(), playbook)
    path = playbook_path()
    path.write_text(render_playbook(playbook), encoding="utf-8")
    return path
