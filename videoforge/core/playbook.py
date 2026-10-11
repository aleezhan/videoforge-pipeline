"""Свод правил playbook: контракт `research/playbook.json` и отбор правил в промпт.

Свод пишет анализатор: стадия distill сливает вклад канала со счётчиками
(`intel/playbook.py`) и рендерит `research/playbook.md` целиком — для человека
и для следующего distill. А читает свод ещё и конвейер эпизода:
`core.project.load_blueprint` кладёт правила в промпт сценариста, планнера,
монтажёра и выбора моментов. Ядро от анализатора не зависит, поэтому модель
правила и её текстовый вид живут здесь, а слияние — там.

Зачем отбор. В своде правила по ВСЕМ разобранным каналам: на 16.09.2026 — 159
правил и 248 тыс. знаков, и у канала научпопа в промпте были сонные начитки и
нарезки подкаста. Весь файл уходил в каждый вызов, а вход claude_cli бесплатен
до ~60 тыс. знаков и на 345 тысячах шёл 158–569 с; сценарий по главам — это
~14 вызовов на 24-минутный ролик. В промпт идут правила, относящиеся к каналу,
и столько, сколько влезает (`prompt_rules`).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from videoforge.core.io import read_model
from videoforge.paths import playbook_json_path

RuleTier = Literal["must", "should", "avoid"]
"""must — проверяемо по раскадровке; should — эвристика для планнера; avoid — антипаттерн."""

Verdict = Literal["confirms", "contradicts", "not_applicable"]

UNIVERSAL = "universal"


class RuleCheck(BaseModel):
    """След одной проверки правила одним каналом: вердикт и чем он обоснован.

    Без обоснования вердикт — это оценка на глаз, и она гуляет от генерации к
    генерации: два прогона на одних данных могут дать «подтверждает» и
    «опровергает». Требование сослаться на измеренное число, таймкод или счёт
    «N из M» и делает вердикт проверяемым человеком, а заодно устойчивым."""

    verdict: Verdict
    why: str = ""


class PlaybookRule(BaseModel):
    """Правило, накопленное по нескольким каналам, вместе с доказательствами.

    Счётчики `held_on`/`contradicted_by` — главное в модели: правило, которое
    только подтверждается, не добавляет знания; знание появляется там, где
    следующий канал предыдущему противоречит."""

    id: str  # snake_case, стабилен: по нему правило узнаётся между анализами
    text: str
    tier: RuleTier = "should"
    scope: str = UNIVERSAL  # universal или ниша, к которой правило привязано
    held_on: list[str] = Field(default_factory=list)  # slug'и анализов, где подтвердилось
    contradicted_by: list[str] = Field(default_factory=list)  # где не сработало
    note: str = ""  # оговорка автора правила: на чём измерено, где границы
    # чем каждый канал обосновал свой вердикт, включая «неприменимо»: проверено
    # и не подошло — это тоже знание, молчание от него неотличимо. Отдельно от
    # note: возражение второго канала не заменяет измеренную оговорку первого.
    checks: dict[str, RuleCheck] = Field(default_factory=dict)
    # область правила до сужения. Опровержение сужает scope до ниши, а снятое
    # опровержение должно вернуть прежнюю — иначе повторный distill оставляет
    # правило подтверждённым, но с чужой нишей в подписи.
    scope_before: str = ""
    # какой анализ правило предложил. Нужно, чтобы повторный distill заменял
    # свой прежний вклад, а не добавлял к нему второй набор формулировок;
    # правило, которое кто-то ещё подтвердил или опроверг, не трогается.
    introduced_by: str = ""

    @property
    def status(self) -> str:
        if self.contradicted_by:
            return "спорное"
        return "подтверждено" if len(self.held_on) >= 2 else "кандидат"


class Playbook(BaseModel):
    """research/playbook.json — свод правил поверх всех анализов."""

    updated_at: datetime
    analyses: list[str] = Field(default_factory=list)  # какие анализы уже учтены
    rules: list[PlaybookRule] = Field(default_factory=list)


def load_playbook() -> Playbook:
    path = playbook_json_path()
    if not path.exists():
        return Playbook(updated_at=datetime.now(UTC))
    return read_model(path, Playbook)


# ---------------------------------------------------------------- текстовый вид

TIER_TITLES = {
    "must": "Обязательное (проверяется по раскадровке)",
    "should": "Рекомендации (уходят в промпт планнера)",
    "avoid": "Антипаттерны",
}
STATUS_MARK = {"подтверждено": "✔", "кандидат": "?", "спорное": "!"}
CHECK_TITLES = {
    "confirms": "держится на",
    "contradicts": "не сработало на",
    "not_applicable": "неприменимо на",
}


def rule_lines(rule: PlaybookRule, checks_from: Collection[str] | None = None) -> list[str]:
    """Строки одного правила в playbook.md. `checks_from` — чьи проверки показывать
    (None — все): в промпт канала идут только проверки его анализов."""
    mark = STATUS_MARK[rule.status]
    scope = "" if rule.scope == UNIVERSAL else f" · ниша: {rule.scope}"
    evidence = f"держится: {len(rule.held_on)}"
    if rule.held_on:
        evidence += f" ({', '.join(rule.held_on)})"
    if rule.contradicted_by:
        evidence += f" · опровергнуто: {', '.join(rule.contradicted_by)}"
    lines = [f"- {mark} **{rule.text}**", f"  - {evidence}{scope}"]
    if rule.note:
        lines.append(f"  - оговорка: {rule.note}")
    # проверки идут отдельными строками и с именем канала: счётчик
    # говорит, сколько каналов правило трогали, а эти строки — что
    # именно там измерили. Без них «подтверждено» ничем не отличается
    # от «модели так показалось».
    for source, check in rule.checks.items():
        if check.why and (checks_from is None or source in checks_from):
            lines.append(f"  - {CHECK_TITLES[check.verdict]} {source}: {check.why}")
    return lines


def tier_sections(
    rules: Sequence[PlaybookRule], checks_from: Collection[str] | None = None
) -> list[str]:
    """Правила по уровням must → should → avoid, спорные в каждом наверху."""
    lines: list[str] = []
    for tier in ("must", "should", "avoid"):
        tier_rules = [r for r in rules if r.tier == tier]
        if not tier_rules:
            continue
        lines += [f"## {TIER_TITLES[tier]}", ""]
        # спорные наверх: они требуют решения, подтверждённые просто работают
        for rule in sorted(tier_rules, key=lambda r: (not r.contradicted_by, -len(r.held_on))):
            lines += rule_lines(rule, checks_from)
        lines.append("")
    return lines


# ---------------------------------------------------------------- отбор в промпт


def relevant_rules(playbook: Playbook, analyses: Sequence[str]) -> list[PlaybookRule]:
    """Правила, относящиеся к каналу, который стоит на анализах `analyses`.

    Ниша правила (`scope`) — свободный текст модели («длинные документальные
    ролики 30+ минут»): на 16.09.2026 у 159 правил 121 разная ниша, и ни одна
    не совпадает с нишей анализа из classify. Сравнивать строки бесполезно.
    Зато distill каждого анализа выносит вердикт по каждому правилу свода
    (у анализов doc_channel и podcast_clips покрытие 100%, кроме 10 из 119 у
    rasul_chabdarov), и счётчики прямо говорят, мерили ли правило на наших
    каналах-эталонах. Отсюда:
      - держится или опровергнуто хоть на одном нашем анализе — берём: это
        измерено в нашей нише, и опровержение здесь ценнее подтверждения;
      - universal — берём: правило про всех;
      - остальное — чужая ниша, которую наш анализ не трогал (правила
        чужой ниши у нарезок подкаста) или назвал неприменимой («натурных
        съёмок на канале нет», «шуток в данных нет» — 55 таких вердиктов у
        ref_channel_a). В промпте это шум.
    Без анализов — пусто: канал придуман без чужого опыта."""
    ours = set(analyses)
    if not ours:
        return []
    return [
        rule
        for rule in playbook.rules
        if rule.scope == UNIVERSAL or ours & {*rule.held_on, *rule.contradicted_by}
    ]


def rule_strength(rule: PlaybookRule, analyses: Collection[str]) -> tuple[int, ...]:
    """Чем больше, тем позже правило уходит из промпта, когда место кончилось."""
    held = sum(1 for slug in rule.held_on if slug in analyses)
    contra = sum(1 for slug in rule.contradicted_by if slug in analyses)
    return (
        # кандидат держится на одном канале — первым: знания в нём меньше всего,
        # а у кандидата своего анализа оно ещё и повторяет бриф, который уже в промпте
        int(rule.status != "кандидат"),
        int(held > 0),  # держится хоть на одном нашем эталоне, а не только опровергнуто
        held - contra,  # перевес вердиктов наших анализов
        len(rule.held_on) - len(rule.contradicted_by),  # перевес по всем каналам
        len(rule.held_on),
    )


@dataclass
class PromptRules:
    """Часть playbook, ушедшая в промпт канала, и сколько не ушло."""

    text: str  # раздел промпта; "" — ни одно правило не подошло или не влезло
    analyses: list[str]
    total: int  # правил в своде
    relevant: int  # из них относятся к каналу
    used: int  # из них вошли в промпт

    @property
    def dropped(self) -> int:
        return self.relevant - self.used

    def summary(self) -> str:
        """Одна строка для лога и сообщения стадии."""
        return (
            f"правил playbook: {self.used} из {self.relevant} "
            f"(ниша: {', '.join(self.analyses)}; в своде {self.total}), "
            f"не влезло {self.dropped}"
        )


def render_prompt_rules(
    rules: Sequence[PlaybookRule], analyses: Sequence[str], relevant: int
) -> str:
    """Раздел промпта: тот же вид правил, что в playbook.md, но проверки только
    анализов канала. Чужие проверки — замеры чужих ниш; счётчик в строке правила
    и так говорит, где оно держалось. Замер 16.09.2026: 128 правил doc_channel со
    всеми проверками — 204 тыс. знаков, только со своими — 100 тысяч."""
    if not rules:
        return ""
    names = ", ".join(analyses)
    lines = [
        "# Playbook",
        "",
        f"Правила, накопленные по разобранным каналам, — не весь свод, а {len(rules)} "
        f"из {relevant}, относящихся к этому каналу: проверенные на его анализах "
        f"({names}) или общие (universal); не влезшие в промпт — слабейшие.",
        "`✔ подтверждено` — держится минимум на двух каналах, `? кандидат` — пока на",
        "одном, `! спорное` — где-то не сработало, и это важнее подтверждений. Под",
        "правилом — что показали именно анализы этого канала.",
        "",
        *tier_sections(rules, checks_from=set(analyses)),
    ]
    return "\n".join(lines) + "\n"


def prompt_rules(playbook: Playbook, analyses: Sequence[str], max_chars: int) -> PromptRules:
    """Правила канала для промпта не длиннее `max_chars` знаков.

    Порядок отсечения — `rule_strength`: сначала кандидаты с одним каналом,
    потом по слабости. Отсекается строго хвост слабейших: более слабое, но
    короткое правило не пролезает на место сильного и длинного."""
    ranked = sorted(
        relevant_rules(playbook, analyses),
        key=lambda r: rule_strength(r, set(analyses)),
        reverse=True,  # сортировка устойчива: при равной силе раньше — старшее правило
    )

    def text(count: int) -> str:
        return render_prompt_rules(ranked[:count], analyses, len(ranked))

    # длина текста растёт с числом правил — сколько влезает, ищем делением пополам
    low, high = 0, len(ranked)
    while low < high:
        mid = (low + high + 1) // 2
        if len(text(mid)) <= max_chars:
            low = mid
        else:
            high = mid - 1
    return PromptRules(
        text=text(low),
        analyses=list(analyses),
        total=len(playbook.rules),
        relevant=len(ranked),
        used=low,
    )
