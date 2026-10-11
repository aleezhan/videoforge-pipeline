"""Сценарист: тема эпизода → сценарий (01_script/).

Шаги, каждый оставляет свой файл на диске:

    angles.json     5-10 подач одной темы с оценками и выбранной лучшей
    story.json      цепочка curiosity gaps: знает → хочет → получает → новый вопрос
    script.md       сценарий; у каждого блока три слоя: текст, кадр, эмоция
    critique.json   проход критика: где зритель уйдёт и что именно править
    retention.json  разметка по секундам + «уберём — станет хуже» + Retention Score

Шаг пропускается, если его файл есть, новее своих входов и сделан текущей
моделью, — как `formula` в анализаторе. Отсюда бесплатно получается ручное
вмешательство: поправил `chosen` в angles.json — его mtime стал новее, и всё,
что ниже по цепочке, пересобирается само.

Короткий ролик (до `CHAPTER_MAX_SEC`) пишется одним заходом: черновик и критик
крутятся циклом с потолком `ctx.options.max_attempts` — так же, как images
перегенерирует кадр до проверки. Раунды складываются в `01_script/rounds/`,
наверху лежит последний вариант.

Длинный ролик пишется ПО ГЛАВАМ. Одним вызовом 24 минуты — это ~2700 слов и
~23 тыс. знаков ответа: по замеру claude_cli (6 с + знаки/90) больше четырёх
минут на один черновик, а длинный ответ хуже держит план. Поэтому после цепочки:

    chapters.json         план глав: звенья цепочки подряд → главы; название,
                          о чём, чем кончается (крючок в следующую), обещание
                          начала и сквозная линия. Секунды и норму слов главы
                          считает python из цели эпизода, а не модель
    chapters/chNN.md      текст главы — тот же формат блоков; каждая глава —
                          свой вызов с планом, цепочкой и ТЕКСТОМ уже написанных глав
    chapters/chNN.critique.json   критик главы (+ недобор речи и нейроязык python-ом),
                          цикл с потолком max_attempts на главу; переписывается
                          только глава с блокирующими находками
    script.md             собирает python из глав, с заголовками `# chNN · … · Ns`
    seams.json            один критик по целому: повторы между главами, закрыто ли
                          обещание, не оборвана ли сквозная линия, крючки на стыках.
                          Блокирующая находка переписывает названную главу —
                          только ту, что модель написала в этом же прогоне
    critique.json         свод: находки всех глав и стыков — его читает человек

Свежесть главы — по отпечаткам в её разборе, а не по mtime: план всех глав —
один файл, и по его времени не отличить «поменялась запись этой главы» от
«поменялась соседняя». Правка главы руками (или script.md — она раскладывается
обратно по главам) даёт расхождение текста с отпечатком: главу судит критик,
но не переписывает, соседние главы не трогаются, пересобираются script.md и
разметка удержания.

Правила берутся из `ctx.blueprint` и `ctx.rules`, а не из промпта: библиотека
хуков живёт в playbook и обновляется каждым новым анализом.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from videoforge.core.io import read_model, write_model
from videoforge.core.project import SCRIPT_DIR
from videoforge.core.script import (
    ANGLES_FILE,
    CHANGE_KINDS,
    CHAPTERS_DIR,
    CHAPTERS_FILE,
    CRITIQUE_FILE,
    FINDING_KINDS,
    RETENTION_FILE,
    ROUNDS_DIR,
    SCRIPT_FILE,
    SEAM_KINDS,
    SEAMS_FILE,
    STORY_FILE,
    Angles,
    Artifact,
    ChapterCritique,
    ChapterPlan,
    Critique,
    Finding,
    Retention,
    ScriptBlock,
    Story,
    assemble_script,
    dead_zones,
    parse_script,
    words_for,
)
from videoforge.core.stage import Stage, StageContext, StageResult
from videoforge.providers.base import ProviderError
from videoforge.stages import director_prompts as p
from videoforge.stages.cliche import cliche_findings, lint_script
from videoforge.stages.director_chapters import (
    CHAPTER_MAX_SEC,
    CHAPTER_MIN_SEC,
    ChapterWork,
    apply_numbers,
    chapter_bounds,
    chapter_payload,
    chapter_problems,
    chapter_sha,
    chapter_state,
    is_long,
    link_budget,
    links_payload,
    parse_chapters,
    plan_findings,
    plan_payload,
    position,
    save_chapter,
    seam_targets,
    story_length,
    take_script_edits,
    without_chapter_headings,
    write_if_changed,
    written_so_far,
)
from videoforge.stages.llm_json import ask_json, ask_text

MAX_ANGLES = 10  # больше модель начинает переформулировать одно и то же
# промежуток без единой смены длиннее этого — мёртвая зона. Взят от измеренного
# у разобранных каналов: там самый длинный план 17–23 с, а зона без смен вообще
# начинается там, где кончается даже он
DEAD_ZONE_SEC = 25.0


class DirectorStage(Stage):
    name = "director"
    needs = ("llm",)

    def run(self, ctx: StageContext) -> StageResult:
        out = ctx.project_dir / SCRIPT_DIR
        angles_path = out / ANGLES_FILE
        script_path = out / SCRIPT_FILE

        # чужая работа: сценарий или раскадровка написаны руками — не трогаем
        if script_path.exists() and not angles_path.exists():
            return StageResult(status="skipped", message=f"сценарий написан руками: {SCRIPT_FILE}")
        if ctx.storyboard.scenes and not angles_path.exists():
            return StageResult(status="skipped", message="раскадровка написана вручную")

        topic = ctx.storyboard.brief.strip()
        if not topic:
            return StageResult(
                status="failed",
                message=(
                    "не из чего писать сценарий: заполни brief в storyboard.json, "
                    f"положи готовый сценарий в {SCRIPT_DIR}/{SCRIPT_FILE} "
                    "или пропиши scenes вручную"
                ),
            )

        out.mkdir(parents=True, exist_ok=True)
        try:
            return self._write(ctx, out, topic)
        except (ProviderError, ValidationError, ValueError, TypeError) as exc:
            return StageResult(status="failed", message=f"сценарист: {exc}")

    # ------------------------------------------------------------------ шаги

    def _write(self, ctx: StageContext, out: Path, topic: str) -> StageResult:
        llm = ctx.providers.llm.name
        episode = _episode_payload(ctx)
        common = {
            "rules": ctx.rules or "—",
            "blueprint": ctx.blueprint or "—",
            "episode": json.dumps(episode, ensure_ascii=False),
            "language": ctx.storyboard.language,
        }
        episode_sec = float(episode["target_duration_sec"] or 0)
        done: list[str] = []

        angles_path = out / ANGLES_FILE
        angles = _fresh(ctx, angles_path, [], Angles)
        if angles is None:
            ctx.log.info("подачи темы «%s»…", topic[:60])
            angles = _parse_angles(
                ask_json(ctx, p.ANGLES.format(topic=topic, **common), system=p.SYSTEM),
                topic=topic,
                llm=llm,
            )
            write_model(angles_path, angles)
            done.append("angles")

        chosen = _chosen_angle(angles)
        story_path = out / STORY_FILE
        story = _fresh(ctx, story_path, [angles_path], Story)
        if story is None:
            ctx.log.info("цепочка любопытства для подачи «%s»…", chosen["title"])
            story = _parse_story(
                ask_json(
                    ctx,
                    p.STORY.format(
                        angle=json.dumps(chosen, ensure_ascii=False),
                        length=story_length(episode_sec),
                        **common,
                    ),
                    system=p.SYSTEM,
                ),
                angle_id=chosen["id"],
                target_sec=episode_sec,
                llm=llm,
            )
            write_model(story_path, story)
            done.append("story")

        script_path = out / SCRIPT_FILE
        work: ChapterWork | None = None
        if is_long(episode_sec) and not _written_in_one_pass(ctx, out):
            work = self._chapters(ctx, out, story, chosen, common, episode_sec, done)
            critique = work.critique
            target_sec = episode_sec
        else:
            critique = self._one_pass(ctx, out, story, common, done)
            target_sec = story.target_duration_sec or episode_sec

        script_md = script_path.read_text(encoding="utf-8")
        blocks = parse_script(script_md)

        retention_path = out / RETENTION_FILE
        retention = _fresh(ctx, retention_path, [script_path], Retention)
        if retention is None:
            retention = self._mark_and_score(ctx, script_md, blocks, target_sec)
            write_model(retention_path, retention)
            done.append("retention")

        if not done:
            return StageResult(status="done", message=f"сценарий актуален: {ctx.rel(script_path)}")

        blocking = len(critique.blocking)
        note = (
            f" · критик оставил блокирующих находок: {blocking} — прочитай "
            f"{CRITIQUE_FILE} до approve"
            if blocking
            else ""
        )
        if blocking:
            ctx.log.warning(
                "критик не снял %d блокирующих находок за %d раундов: %s",
                blocking,
                critique.round,
                "; ".join(f.where for f in critique.blocking),
            )
        speech = sum(b.speech_sec for b in blocks)
        fill = speech / target_sec if target_sec else 0.0
        dead = sum(z.seconds for z in retention.dead_zones)
        cliches = lint_script(script_md, ctx.storyboard.language)
        by_chapters = f"{work.summary()} · " if work else ""
        return StageResult(
            status="done",
            message=(
                f"{ctx.rel(script_path)} · подач: {len(angles.options)} · {by_chapters}"
                f"блоков: {len(blocks)} · речи {speech:.0f} из {target_sec:.0f} с "
                f"({fill:.0%}) · мёртвых зон {len(retention.dead_zones)} на {dead:.0f} с · "
                f"удержание: {retention.score}/100 · {cliches.summary()} · "
                f"шаги: {', '.join(done)}{note}"
            ),
            changed=True,
        )

    def _one_pass(
        self, ctx: StageContext, out: Path, story: Story, common: dict[str, str], done: list[str]
    ) -> Critique:
        """Сценарий одним заходом — короткий ролик, одна глава."""
        story_path, script_path, critique_path = (
            out / STORY_FILE,
            out / SCRIPT_FILE,
            out / CRITIQUE_FILE,
        )
        critique = _fresh(ctx, critique_path, [story_path, script_path], Critique)
        if critique is not None and script_path.exists():
            return critique
        text, critique = self._draft_and_critique(ctx, out, story, common)
        script_path.write_text(text, encoding="utf-8")
        # критик волен слить или выбросить блок; тогда цепочка перестаёт
        # описывать то, что написано, — приводим её к сценарию
        if _sync_story(ctx, story, script_path):
            write_model(story_path, story)
        # разбор пишется последним: поправленная цепочка, оказавшись новее
        # его, делала разбор «устаревшим», и следующий запуск переписывал
        # готовый сценарий заново
        write_model(critique_path, critique)
        done.append("script")
        return critique

    @staticmethod
    def _mark_and_score(
        ctx: StageContext, script_md: str, blocks: list[ScriptBlock], target_sec: float
    ) -> Retention:
        """Разметка в два шага: модель ставит отметки смен и судит блоки, python
        меряет мёртвые зоны, и только потом модель выставляет балл — уже по
        числам. Без этого балл получался одинаковым у сорокасекундного ролика и
        у десятиминутного."""
        llm = ctx.providers.llm.name
        ctx.log.info("разметка смен и проверка блоков…")
        marked = _parse_retention(
            ask_json(
                ctx,
                p.RETENTION.format(
                    script=script_md,
                    target_sec=f"{target_sec:.0f}",
                    block_ids=", ".join(b.id for b in blocks) or "—",
                    changes=" | ".join(CHANGE_KINDS),
                    language=ctx.storyboard.language,
                ),
                system=p.SYSTEM,
            ),
            llm=llm,
        )
        marked.dead_zones = dead_zones(marked.beats, limit_sec=DEAD_ZONE_SEC, total_sec=target_sec)
        measured = _measurements(marked, blocks, target_sec)
        ctx.log.info(
            "мёртвых зон: %d, самая длинная %.0f с — считаю балл",
            len(marked.dead_zones),
            max((z.seconds for z in marked.dead_zones), default=0.0),
        )
        scored = _as_dict(
            ask_json(
                ctx,
                p.SCORE.format(
                    script=script_md,
                    measured=json.dumps(measured, ensure_ascii=False, indent=2),
                    language=ctx.storyboard.language,
                ),
                system=p.SYSTEM,
            ),
            "оценка удержания",
        )
        marked.score = int(scored.get("score") or 0)
        marked.score_reasons = scored.get("score_reasons") or {}
        marked.weakest = scored.get("weakest", "")
        return marked

    @staticmethod
    def _draft_and_critique(
        ctx: StageContext, out: Path, story: Story, common: dict[str, str]
    ) -> tuple[str, Critique]:
        """Черновик → критик → переписывание, до `max_attempts` раундов.

        Цикл останавливает критик, а не балл удержания: балл выставляет модель,
        и между прогонами он плывёт — как критерий остановки он не годится."""
        rounds = out / ROUNDS_DIR
        rounds.mkdir(exist_ok=True)
        story_json = json.dumps(_story_payload(story), ensure_ascii=False)
        total_words = sum(words_for(g.planned_sec) for g in story.gaps)
        llm = ctx.providers.llm.name
        critique = Critique(llm=llm, generated_at=datetime.now(UTC))
        text = ""
        for n in range(1, max(1, ctx.options.max_attempts) + 1):
            revision = _revision_note(critique.blocking) if n > 1 else ""
            ctx.log.info("сценарий: черновик, раунд %d", n)
            text = ask_text(
                ctx,
                p.DRAFT.format(
                    story=story_json, total_words=total_words, revision=revision, **common
                ),
                system=p.SYSTEM,
            )
            (rounds / f"round_{n:02d}.md").write_text(text, encoding="utf-8")

            critique = _parse_critique(
                ask_json(
                    ctx,
                    p.CRITIQUE.format(
                        script=text,
                        story=story_json,
                        kinds=" | ".join(FINDING_KINDS),
                        **common,
                    ),
                    system=p.SYSTEM,
                ),
                round_no=n,
                llm=llm,
            )
            # недобор речи ищет python: это арифметика, а не суждение, и модель
            # систематически её не замечает — на длинном формате ролик провисает.
            # Нейроязык — тоже python: критик пишет теми же штампами, которые
            # должен ловить, а список маркеров одинаков от прогона к прогону
            critique.findings = (
                _short_blocks(text) + cliche_findings(text, common["language"]) + critique.findings
            )
            write_model(rounds / f"round_{n:02d}.critique.json", critique)
            if not critique.blocking:
                ctx.log.info("критик не нашёл блокирующих проблем на раунде %d", n)
                break
            ctx.log.warning(
                "раунд %d: блокирующих находок %d — переписываю",
                n,
                len(critique.blocking),
            )
        return text, critique

    # ------------------------------------------------------------- по главам

    def _chapters(
        self,
        ctx: StageContext,
        out: Path,
        story: Story,
        angle: dict[str, Any],
        common: dict[str, str],
        target_sec: float,
        done: list[str],
    ) -> ChapterWork:
        """План глав → каждая глава своим циклом → script.md → стыки → свод."""
        llm = ctx.providers.llm.name
        plan_path = out / CHAPTERS_FILE
        plan = _fresh(ctx, plan_path, [out / STORY_FILE], ChapterPlan)
        if plan is None:
            plan = self._plan_chapters(ctx, story, angle, common, target_sec)
            write_model(plan_path, plan)
            done.append("chapters")
        else:
            # план мог править человек: состав звеньев проверяется, числа — заново
            apply_numbers(plan, story, target_sec)
        budget = link_budget(story, target_sec)

        cdir = out / CHAPTERS_DIR
        (cdir / ROUNDS_DIR).mkdir(parents=True, exist_ok=True)
        script_path = out / SCRIPT_FILE
        if not ctx.options.force:
            take_script_edits(ctx, plan, cdir, script_path)

        texts: dict[str, str] = {}
        crits: dict[str, ChapterCritique] = {}
        drafted: set[str] = set()  # написаны моделью в этом прогоне
        touched = False
        for index, ch in enumerate(plan.chapters):
            plan_sha = chapter_sha(ch, story)
            state, text, crit = chapter_state(ctx, cdir, ch, plan_sha, plan_path)
            if state == "fresh" and crit is not None:
                texts[ch.id], crits[ch.id] = text, crit
                continue
            touched = True
            if state == "manual":
                ctx.log.info("глава %s поправлена руками — судит критик, текст не трогаю", ch.id)
                judged = self._judge_chapter(
                    ctx,
                    plan,
                    index,
                    text,
                    story,
                    budget,
                    common,
                    round_no=crit.round if crit else 1,
                )
                judged.manual = True
                texts[ch.id], crits[ch.id] = text, judged
                save_chapter(cdir, ch, text, judged, plan_sha, write_text=False)
                continue
            text, judged = self._draft_chapter(
                ctx,
                out,
                plan,
                index,
                story,
                budget,
                texts,
                common,
                first_round=1,
                rounds=max(1, ctx.options.max_attempts),
            )
            texts[ch.id], crits[ch.id] = save_chapter(cdir, ch, text, judged, plan_sha), judged
            drafted.add(ch.id)

        if write_if_changed(script_path, assemble_script(plan.chapters, texts)):
            touched = True
        if touched:
            done.append("script")

        seams_path = out / SEAMS_FILE
        seams = _fresh(ctx, seams_path, [script_path, plan_path], Critique)
        if seams is None:
            seams = self._seams(ctx, out, plan, story, budget, texts, crits, drafted, common)
            write_model(seams_path, seams)
            done.append("seams")
            touched = True

        critique = Critique(
            round=max((crits[c.id].round for c in plan.chapters), default=1),
            findings=[f for c in plan.chapters for f in crits[c.id].findings] + seams.findings,
            verdict=seams.verdict,
            llm=llm,
            generated_at=datetime.now(UTC),
        )
        critique_path = out / CRITIQUE_FILE
        if touched or not critique_path.exists():
            write_model(critique_path, critique)
        return ChapterWork(plan=plan, texts=texts, crits=crits, seams=seams, critique=critique)

    @staticmethod
    def _plan_chapters(
        ctx: StageContext,
        story: Story,
        angle: dict[str, Any],
        common: dict[str, str],
        target_sec: float,
    ) -> ChapterPlan:
        """Модель группирует звенья в главы; python проверяет состав, считает
        секунды и норму слов. Раскладка вне границ — один повтор с причинами,
        после него — предупреждение в плане, а не упавшая стадия."""
        lo, hi = chapter_bounds(target_sec)
        budget = link_budget(story, target_sec)
        links = json.dumps(
            [{**g.model_dump(), "planned_sec": budget[g.id][0]} for g in story.gaps],
            ensure_ascii=False,
        )
        revision = ""
        plan: ChapterPlan | None = None
        problems: list[str] = []
        for attempt in range(2):
            ctx.log.info(
                "план глав: звеньев %d, глав от %d до %d (попытка %d)…",
                len(story.gaps),
                lo,
                hi,
                attempt + 1,
            )
            raw = ask_json(
                ctx,
                p.CHAPTERS.format(
                    angle=json.dumps(angle, ensure_ascii=False),
                    links=links,
                    ending=story.ending or "—",
                    target_sec=f"{target_sec:.0f}",
                    lo=lo,
                    hi=hi,
                    max_sec=f"{CHAPTER_MAX_SEC:.0f}",
                    min_sec=f"{CHAPTER_MIN_SEC:.0f}",
                    revision=revision,
                    **common,
                ),
                system=p.SYSTEM,
            )
            try:
                plan = parse_chapters(raw, llm=ctx.providers.llm.name)
                apply_numbers(plan, story, target_sec)
                problems = chapter_problems(plan, target_sec)
            except ValueError as exc:
                plan, problems = None, [str(exc)]
            if not problems:
                break
            ctx.log.warning("план глав, попытка %d: %s", attempt + 1, "; ".join(problems))
            revision = "\n\nYOUR PREVIOUS SPLIT WAS REJECTED. Fix exactly this:\n" + "\n".join(
                f"- {x}" for x in problems
            )
        if plan is None:
            raise ValueError(f"план глав не сложился за две попытки: {'; '.join(problems)}")
        plan.warnings = problems
        return plan

    def _draft_chapter(
        self,
        ctx: StageContext,
        out: Path,
        plan: ChapterPlan,
        index: int,
        story: Story,
        budget: dict[str, tuple[float, int]],
        texts: dict[str, str],
        common: dict[str, str],
        *,
        revision: str = "",
        first_round: int,
        rounds: int,
    ) -> tuple[str, ChapterCritique]:
        """Черновик главы → критик главы → переписывание, `rounds` раундов.

        Модель получает план всех глав и текст уже написанных — чтобы не
        здороваться второй раз и не повторять сказанное. В повтор идёт и её
        прежний черновик: «оставь то, что не критиковали» без текста не выполнить."""
        ch = plan.chapters[index]
        rdir = out / CHAPTERS_DIR / ROUNDS_DIR
        links = json.dumps(links_payload(ch, story, budget), ensure_ascii=False)
        written = written_so_far(plan, texts, index)
        note = revision
        text = ""
        crit: ChapterCritique | None = None
        for n in range(first_round, first_round + rounds):
            ctx.log.info(
                "глава %s (%d из %d): черновик, раунд %d", ch.id, index + 1, len(plan.chapters), n
            )
            text = without_chapter_headings(
                ask_text(
                    ctx,
                    p.DRAFT_CHAPTER.format(
                        chapter_id=ch.id,
                        chapter_no=index + 1,
                        chapter_count=len(plan.chapters),
                        plan=plan_payload(plan),
                        chapter=json.dumps(chapter_payload(ch), ensure_ascii=False),
                        links=links,
                        total_words=ch.words,
                        written=written,
                        revision=note,
                        position=position(plan, index, story),
                        **common,
                    ),
                    system=p.SYSTEM,
                )
            )
            (rdir / f"{ch.id}_round_{n:02d}.md").write_text(text, encoding="utf-8")
            crit = self._judge_chapter(ctx, plan, index, text, story, budget, common, round_no=n)
            write_model(rdir / f"{ch.id}_round_{n:02d}.critique.json", crit)
            if not crit.blocking:
                break
            ctx.log.warning(
                "глава %s, раунд %d: блокирующих находок %d", ch.id, n, len(crit.blocking)
            )
            note = _revision_note(crit.blocking, previous=text)
        if crit is None:
            raise ValueError(f"глава {ch.id}: ни одного раунда (rounds={rounds})")
        return text, crit

    @staticmethod
    def _judge_chapter(
        ctx: StageContext,
        plan: ChapterPlan,
        index: int,
        text: str,
        story: Story,
        budget: dict[str, tuple[float, int]],
        common: dict[str, str],
        *,
        round_no: int,
    ) -> ChapterCritique:
        """Критик главы плюс то, что считает python: форма главы, недобор речи,
        нейроязык — как у сценария одним заходом."""
        ch = plan.chapters[index]
        raw = ask_json(
            ctx,
            p.CRITIQUE_CHAPTER.format(
                chapter_id=ch.id,
                chapter_no=index + 1,
                chapter_count=len(plan.chapters),
                chapter=json.dumps(chapter_payload(ch), ensure_ascii=False),
                links=json.dumps(links_payload(ch, story, budget), ensure_ascii=False),
                script=text,
                plan=plan_payload(plan),
                kinds=" | ".join(FINDING_KINDS),
                **common,
            ),
            system=p.SYSTEM,
        )
        base = _parse_critique(raw, round_no=round_no, llm=ctx.providers.llm.name)
        crit = ChapterCritique.model_validate({**base.model_dump(), "chapter": ch.id})
        crit.findings = (
            plan_findings(text, ch, plan)
            + _short_blocks(text)
            + cliche_findings(text, common["language"])
            + crit.findings
        )
        return crit

    def _seams(
        self,
        ctx: StageContext,
        out: Path,
        plan: ChapterPlan,
        story: Story,
        budget: dict[str, tuple[float, int]],
        texts: dict[str, str],
        crits: dict[str, ChapterCritique],
        drafted: set[str],
        common: dict[str, str],
    ) -> Critique:
        """Критик по целому, до `max_attempts` проходов. Блокирующая находка
        переписывает названную главу одним раундом — но только главу, которую
        модель написала в этом прогоне: правленную руками и готовую до прогона
        стыки не трогают, их находки остаются человеку на воротах."""
        rdir = out / ROUNDS_DIR
        rdir.mkdir(exist_ok=True)
        script_path = out / SCRIPT_FILE
        cap = max(1, ctx.options.max_attempts)
        seams = Critique(llm=ctx.providers.llm.name, generated_at=datetime.now(UTC))
        for n in range(1, cap + 1):
            ctx.log.info("стыки глав: проход %d", n)
            seams = _parse_critique(
                ask_json(
                    ctx,
                    p.SEAMS.format(
                        script=assemble_script(plan.chapters, texts),
                        plan=plan_payload(plan),
                        ending=story.ending or "—",
                        kinds=" | ".join(SEAM_KINDS + FINDING_KINDS),
                        **common,
                    ),
                    system=p.SYSTEM,
                ),
                round_no=n,
                llm=ctx.providers.llm.name,
            )
            write_model(rdir / f"seams_round_{n:02d}.json", seams)
            targets = seam_targets(seams.blocking, plan, texts)
            rewrite = {cid: found for cid, found in targets.items() if cid in drafted}
            if not seams.blocking:
                break
            if not rewrite:
                ctx.log.warning(
                    "стыки: блокирующих находок %d, переписывать нечего — главы правлены "
                    "руками, готовы до прогона или находка не называет главу",
                    len(seams.blocking),
                )
                break
            if n == cap:
                ctx.log.warning(
                    "стыки: блокирующих находок %d после %d проходов", len(seams.blocking), n
                )
                break
            for index, ch in enumerate(plan.chapters):
                if ch.id not in rewrite:
                    continue
                ctx.log.warning("стыки: переписываю %s (находок %d)", ch.id, len(rewrite[ch.id]))
                text, crit = self._draft_chapter(
                    ctx,
                    out,
                    plan,
                    index,
                    story,
                    budget,
                    texts,
                    common,
                    revision=_revision_note(rewrite[ch.id], previous=texts[ch.id]),
                    first_round=crits[ch.id].round + 1,
                    rounds=1,
                )
                texts[ch.id] = save_chapter(
                    out / CHAPTERS_DIR, ch, text, crit, chapter_sha(ch, story)
                )
                crits[ch.id] = crit
            write_if_changed(script_path, assemble_script(plan.chapters, texts))
        return seams


# ---------------------------------------------------------------- свежесть


def _fresh[M: Artifact](
    ctx: StageContext, out: Path, sources: list[Path], model: type[M]
) -> M | None:
    """Готовый артефакт, если он новее входов и сделан текущей моделью.

    Иначе None — шаг переделывается. `--force` обнуляет всё."""
    if ctx.options.force or not out.exists():
        return None
    stamp = out.stat().st_mtime
    if any(s.exists() and s.stat().st_mtime > stamp for s in sources):
        return None
    try:
        artifact = read_model(out, model)
    except (ValidationError, ValueError):
        ctx.log.warning("%s не читается — переделываю", out.name)
        return None
    return artifact if artifact.llm == ctx.providers.llm.name else None


def _written_in_one_pass(ctx: StageContext, out: Path) -> bool:
    """Длинный ролик, сценарий которого написан одним заходом до появления глав.

    Он честно сделан из тогдашних входов и одобрялся человеком: переписывать его
    по главам только потому, что код научился, — значит молча выбросить работу.
    Переписать по главам — `--force`."""
    if (out / CHAPTERS_FILE).exists() or not (out / SCRIPT_FILE).exists():
        return False
    critique = _fresh(ctx, out / CRITIQUE_FILE, [out / STORY_FILE, out / SCRIPT_FILE], Critique)
    if critique is None:
        return False
    ctx.log.info("сценарий написан одним заходом до глав — принят как есть; по главам: --force")
    return True


# ------------------------------------------------------------ разбор ответов


def _episode_payload(ctx: StageContext) -> dict[str, Any]:
    video = ctx.project.video if ctx.project else None
    channel = ctx.project.channel if ctx.project else None
    return {
        "title": ctx.storyboard.title,
        "language": ctx.storyboard.language,
        "aspect_ratio": ctx.storyboard.aspect_ratio,
        "target_duration_sec": video.target_duration_sec if video else 60,
        "platforms": channel.platforms if channel else [],
    }


def _as_dict(raw: Any, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{what}: ожидался объект, пришло {type(raw).__name__}")
    return raw


def _parse_angles(raw: Any, *, topic: str, llm: str) -> Angles:
    data = _as_dict(raw, "подачи")
    options = data.get("options")
    if not isinstance(options, list) or not options:
        raise ValueError("модель не вернула ни одной подачи")
    angles = Angles.model_validate(
        {
            "topic": topic,
            "options": options[:MAX_ANGLES],
            "rationale": data.get("rationale", ""),
            "llm": llm,
            "generated_at": datetime.now(UTC),
        }
    )
    for i, angle in enumerate(angles.options, start=1):
        if not angle.id:
            angle.id = f"a{i}"
    best = max(angles.options, key=lambda a: a.total)
    angles.chosen = best.id
    return angles


def _chosen_angle(angles: Angles) -> dict[str, Any]:
    """Выбранная подача. `chosen` правится руками, поэтому сверяем со списком."""
    for angle in angles.options:
        if angle.id == angles.chosen:
            return angle.model_dump()
    if not angles.options:
        raise ValueError(f"в {ANGLES_FILE} нет ни одной подачи")
    raise ValueError(
        f"в {ANGLES_FILE} выбрана подача '{angles.chosen}', которой нет в списке; "
        f"допустимые id: {', '.join(a.id for a in angles.options)}"
    )


def _parse_story(raw: Any, *, angle_id: str, target_sec: float, llm: str) -> Story:
    data = _as_dict(raw, "цепочка любопытства")
    gaps = data.get("gaps")
    if not isinstance(gaps, list) or not gaps:
        raise ValueError("модель не вернула ни одного звена цепочки")
    story = Story.model_validate(
        {
            "angle_id": angle_id,
            "gaps": gaps,
            "ending": data.get("ending", ""),
            "target_duration_sec": float(data.get("target_duration_sec") or 0) or target_sec,
            "llm": llm,
            "generated_at": datetime.now(UTC),
        }
    )
    for i, gap in enumerate(story.gaps, start=1):
        if not gap.id:
            gap.id = f"g{i}"
    return story


def _parse_critique(raw: Any, *, round_no: int, llm: str) -> Critique:
    data = _as_dict(raw, "разбор критика")
    findings = data.get("findings")
    return Critique.model_validate(
        {
            "round": round_no,
            "findings": findings if isinstance(findings, list) else [],
            "verdict": data.get("verdict", ""),
            "llm": llm,
            "generated_at": datetime.now(UTC),
        }
    )


def _parse_retention(raw: Any, *, llm: str) -> Retention:
    data = _as_dict(raw, "разметка удержания")
    blocks = data.get("blocks")
    return Retention.model_validate(
        {
            "beats": data.get("beats") or [],
            # приговор по блоку — не главное в разметке: кривая запись про один
            # блок не должна ронять стадию вместе с отметками смен
            "blocks": [b for b in blocks if isinstance(b, dict) and b.get("id")]
            if isinstance(blocks, list)
            else [],
            "llm": llm,
            "generated_at": datetime.now(UTC),
        }
    )


def _story_payload(story: Story) -> list[dict[str, Any]]:
    """Цепочка для промпта черновика — с посчитанной нормой слов на звено.
    «Уложись в N секунд» модель не считает, «напиши N слов» — считает."""
    return [{**gap.model_dump(), "words": words_for(gap.planned_sec)} for gap in story.gaps]


def _short_blocks(markdown: str) -> list[Finding]:
    """Блоки, где речи заметно меньше заявленной длительности.

    Считает python: это арифметика, а не суждение. Модель просят посчитать
    слова, и она систематически недобирает — на длинном формате из этого
    выходит ролик с провалами тишины."""
    findings: list[Finding] = []
    for block in parse_script(markdown):
        if block.planned_sec <= 0:
            continue
        need = words_for(block.planned_sec)
        if block.words >= need * 0.8:
            continue
        findings.append(
            Finding(
                kind="сцена ни за чем",
                where=block.id,
                why_viewer_leaves=(
                    f"в блоке {block.words} слов ≈ {block.speech_sec:g} с речи при заявленных "
                    f"{block.planned_sec:g} с — остаток кадр висит молча"
                ),
                fix=(
                    f"дописать блок {block.id} до {need} слов или сократить его "
                    f"длительность до {block.speech_sec:g} с"
                ),
                blocking=True,
            )
        )
    return findings


def _sync_story(ctx: StageContext, story: Story, script_path: Path) -> bool:
    """Привести цепочку к тому, что реально написано.

    Критик вправе попросить слить или выбросить звено, и сценарий его слушается.
    Тогда story.json описывает не этот сценарий — артефакт начинает врать.
    Только для сценария одним заходом: у длинного ролика цепочка — вход плана
    глав, и переписанная цепочка сделала бы устаревшими все главы разом."""
    blocks = {b.id: b for b in parse_script(script_path.read_text(encoding="utf-8"))}
    if not blocks:
        return False
    kept = [g for g in story.gaps if g.id in blocks]
    if not kept:
        return False  # разметка блоков не совпала совсем — цепочку не трогаем
    dropped = [g.id for g in story.gaps if g.id not in blocks]
    changed = bool(dropped)
    for gap in kept:
        if abs(gap.planned_sec - blocks[gap.id].planned_sec) > 0.05:
            gap.planned_sec = blocks[gap.id].planned_sec
            changed = True
    if dropped:
        ctx.log.info("цепочка приведена к сценарию: критик убрал звенья %s", ", ".join(dropped))
    story.gaps = kept
    return changed


def _measurements(
    retention: Retention, blocks: list[ScriptBlock], target_sec: float
) -> dict[str, Any]:
    """Числа, по которым модель ставит балл. Считает python — иначе балл
    оказывается одинаковым у сорокасекундного ролика и у десятиминутного."""
    marks = retention.marks()
    gaps = [round(b - a, 1) for a, b in zip(marks, marks[1:], strict=False)]
    opening = [m for m in marks if m <= target_sec / 5]
    speech = sum(b.speech_sec for b in blocks)
    longest = max(retention.dead_zones, key=lambda z: z.seconds, default=None)
    return {
        "хронометраж_с": round(target_sec, 1),
        "смен_всего": len(marks),
        "последняя_смена_на_с": marks[-1] if marks else 0,
        "не_размечено_в_конце_с": round(target_sec - marks[-1], 1) if marks else target_sec,
        "интервал_между_сменами_с": {
            "минимум": min(gaps, default=0),
            "медиана": sorted(gaps)[len(gaps) // 2] if gaps else 0,
            "максимум": max(gaps, default=0),
        },
        "смен_в_первой_пятой": len(opening),
        "смен_в_минуту": round(len(marks) / (target_sec / 60), 1) if target_sec else 0,
        "мёртвые_зоны": [
            {"с": z.start_sec, "по": z.end_sec, "длина_с": z.seconds} for z in retention.dead_zones
        ],
        "самая_длинная_мёртвая_зона_с": longest.seconds if longest else 0,
        "речи_с": round(speech, 1),
        "доля_речи": round(speech / target_sec, 2) if target_sec else 0,
        "блоков_помечено_выбрасываемыми": sum(1 for b in retention.blocks if b.removable),
        "блоков_всего": len(blocks),
    }


def _revision_note(findings: list[Finding], *, previous: str = "") -> str:
    """Находки критика — обратно в промпт черновика. `previous` — отвергнутый
    черновик: переписывая главу, модель видит, что именно оставить."""
    fixes = "\n".join(f"- [{f.kind}] {f.where}: {f.why_viewer_leaves} → {f.fix}" for f in findings)
    note = (
        "\n\nTHE PREVIOUS DRAFT WAS REJECTED. Apply every fix below; keep what "
        f"was not criticised:\n{fixes}"
    )
    if previous:
        note += f"\n\nTHE REJECTED DRAFT\n{previous.strip()}"
    return note
