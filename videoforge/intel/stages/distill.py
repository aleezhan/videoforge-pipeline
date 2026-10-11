"""Стадия distill: формула канала → вклад в общий playbook.

Последняя стадия анализа и единственная, которая пишет за пределы своей папки:
playbook по природе межканальный. След всё равно остаётся локальным —
07_distill/proposal.json хранит ровно то, что предложил этот анализ.

Разделение ответственности здесь принципиальное: LLM только выносит вердикт по
каждому существующему правилу (подтверждает / опровергает / не применимо) и
предлагает новые. Слияние со счётчиками делает python (intel/playbook.py) —
модель не может переписать накопленное за все прошлые анализы.

После стадии ворота: правила идут в промпт планнера, то есть влияют на все
будущие видео. Такое изменение человек должен увидеть.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from videoforge.core.io import read_model, write_model
from videoforge.core.playbook import PlaybookRule, load_playbook
from videoforge.intel.models import (
    ChannelData,
    ClassifyReport,
    DistillProposal,
    Formula,
    RuleVerdict,
)
from videoforge.intel.playbook import merge, save_playbook
from videoforge.intel.stage import IntelContext, IntelStage, StageResult
from videoforge.intel.stages.classify import _ask_json, classify_path
from videoforge.intel.stages.collect import channel_path
from videoforge.intel.stages.formula import formula_path

DISTILL_DIR = "07_distill"
PROPOSAL_FILE = "proposal.json"

_DISTILL_PROMPT = """Update a cross-channel production playbook with one new channel.

The playbook holds rules that survived across several analysed channels. Your
job is NOT to rewrite it. For each existing rule say whether THIS channel
confirms it, contradicts it, or says nothing about it — then propose the few
genuinely new rules this channel teaches.

THIS CHANNEL
{channel}

WHAT THE ANALYSIS FOUND
{formula}

EXISTING PLAYBOOK RULES
{rules}

Return ONLY valid JSON with exactly this shape (no fences, no comments):
{{
  "verdicts": [{{"id": "existing rule id", "verdict": "confirms|contradicts|not_applicable",
    "why": "REQUIRED for every verdict: the evidence, one sentence",
    "scope": "with contradicts only: the niche where the rule still holds, else empty"}}],
  "new_rules": [{{"id": "snake_case_ascii_id", "text": "the rule, imperative",
    "tier": "must|should|avoid", "scope": "universal or the niche it applies to",
    "note": "limits of the rule, or empty"}}]
}}

Rules:
- A verdict for every existing rule id, no invented ids.
- Every verdict needs "why", and "why" must point at something measured above:
  a number, a timecode, or a count as "N of M". A verdict you cannot back with
  a figure from the data is "not_applicable", never a guess — an unsupported
  label flips between runs and poisons the playbook.
- Decide the verdict from that evidence, in this order: is the rule's mechanism
  present in this channel at all? If not — "not_applicable". If present and the
  data agrees — "confirms". If present and the data goes the other way —
  "contradicts". Contradictions are the valuable output here, do not soften them.
- With "contradicts", fill "scope" with the niche the rule should be demoted to
  — the kind of channel where it did hold. A rule that fails here is usually
  not wrong, it is narrower than it looked.
- At most 10 new rules, and only what is NOT already covered. A rule must be
  actionable when writing a storyboard, not an observation about this channel.
- One rule states exactly ONE claim. Never join two pieces of advice with "and"
  — a compound rule cannot be confirmed or contradicted as a whole, and the
  next channel's verdict on it means nothing. Split instead: two rules out of
  one piece of advice is the normal outcome, not a waste of a slot.
- If "shot_recipe" above carries measured numbers, exactly one new rule must
  state that editing rhythm — clip length, cuts per minute, and how density
  changes across the runtime — as a "must". This is not optional: the rhythm is
  the one thing the next channel can be measured against instead of argued
  with, and it feeds scene_duration_sec of our own production. Same for the
  measured hook and CTA timings: numbers outrank adjectives when slots are
  short.
- tier "must" only for what a machine can check against a storyboard (timings,
  clip length, scene counts). Everything judgemental is "should".
- "scope" is "universal" only if you would apply the rule to a channel in an
  unrelated niche; otherwise name the niche.
- Write text and note in the language "{language}"; ids stay ASCII snake_case."""


def proposal_path(analysis_dir: Path) -> Path:
    return analysis_dir / DISTILL_DIR / PROPOSAL_FILE


class DistillStage(IntelStage):
    name = "distill"

    def run(self, ctx: IntelContext) -> StageResult:
        src = formula_path(ctx.analysis_dir)
        if not src.exists():
            return StageResult(status="failed", message=f"нет {src} — сначала стадия formula")
        if ctx.llm is None:
            return StageResult(status="failed", message="нет LLM-провайдера (--llm или --mock)")

        out = proposal_path(ctx.analysis_dir)
        if out.exists() and not ctx.options.force:
            existing = read_model(out, DistillProposal)
            if existing.llm == ctx.llm.name and out.stat().st_mtime >= src.stat().st_mtime:
                return StageResult(
                    status="skipped",
                    message=f"вклад в playbook уже посчитан ({out}); --force — переделать",
                )

        formula = read_model(src, Formula)
        playbook = load_playbook()
        channel = {"slug": ctx.config.id, "goal": ctx.config.goal}
        if channel_path(ctx.analysis_dir).exists():
            snapshot = read_model(channel_path(ctx.analysis_dir), ChannelData).channel
            channel["title"] = snapshot.title
        if classify_path(ctx.analysis_dir).exists():
            taxonomy = read_model(classify_path(ctx.analysis_dir), ClassifyReport).taxonomy
            channel["niche"] = taxonomy.niche
            channel["audience"] = taxonomy.audience

        ctx.log.info(
            "сверяю с playbook: правил %d, анализов учтено %d…",
            len(playbook.rules),
            len(playbook.analyses),
        )
        raw = _ask_json(
            ctx.llm,
            _DISTILL_PROMPT.format(
                channel=json.dumps(channel, ensure_ascii=False),
                formula=json.dumps(
                    formula.model_dump(exclude={"computed_at", "llm", "video_ideas"}),
                    ensure_ascii=False,
                    default=str,
                ),
                rules=json.dumps(
                    [
                        {"id": r.id, "text": r.text, "tier": r.tier, "scope": r.scope}
                        for r in playbook.rules
                    ],
                    ensure_ascii=False,
                ),
                language=ctx.config.language,
            ),
            ctx,
        )
        try:
            proposal = DistillProposal(
                computed_at=datetime.now(UTC),
                llm=ctx.llm.name,
                verdicts=[RuleVerdict.model_validate(v) for v in raw.get("verdicts") or []],
                new_rules=[PlaybookRule.model_validate(r) for r in raw.get("new_rules") or []],
            )
        except (ValidationError, AttributeError, TypeError) as exc:
            return StageResult(status="failed", message=f"ответ LLM не разобрался: {exc}")

        ctx.stage_dir(DISTILL_DIR)
        write_model(out, proposal)
        path = save_playbook(merge(playbook, proposal, ctx.config.id))

        contradictions = sum(1 for v in proposal.verdicts if v.verdict == "contradicts")
        confirmations = sum(1 for v in proposal.verdicts if v.verdict == "confirms")
        return StageResult(
            status="done",
            message=(
                f"playbook: +{len(proposal.new_rules)} правил, подтверждено {confirmations}, "
                f"опровергнуто {contradictions} → {path}"
            ),
            changed=True,
        )
