"""Главы длинного сценария: всё, что считает и хранит python, без вызовов модели.

Стадия `director` решает порядок шагов и зовёт модель; здесь — арифметика глав
(секунды и норма слов от цели эпизода), проверка плана, отпечатки свежести,
сборка script.md из глав и обратная раскладка его ручной правки, адреса находок
критика стыков. Отдельно от стадии, как `director_prompts`: этот код читается и
проверяется без единого промпта.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from videoforge.core.io import read_model, write_model
from videoforge.core.script import (
    CHAPTERS_DIR,
    CHAPTERS_FILE,
    SCRIPT_FILE,
    Chapter,
    ChapterCritique,
    ChapterPlan,
    Critique,
    Finding,
    Story,
    is_chapter_heading,
    parse_script,
    split_chapters,
    words_for,
)
from videoforge.core.stage import StageContext
from videoforge.stages import director_prompts as p

# Потолок главы — и порог, с которого ролик пишется по главам. Два основания,
# и они сходятся: у разобранного длинного формата (ref_channel_b) глава —
# микросюжет на 2–5 минут; а 300 с — это ~485 слов речи и ~4 тыс. знаков ответа
# со слоями кадра и эмоции (замер ep001: 8.5 знака сценария на слово
# речи), то есть ~50 с на вызов claude_cli. Ролик не длиннее — одна глава, как было
CHAPTER_MAX_SEC = 300.0
# глава короче — не глава, а звено: «5 глав по 90–120 с» у первого ролика doc_channel
CHAPTER_MIN_SEC = 90.0
# Звено длинного ролика — такт внутри главы. Пункт списка у разобранных каналов
# 45–90 с (ref_channel_a), такт микросюжета короче главы в 3–5 раз. Без этой
# подсказки на 24 минуты модель пишет 6–8 звеньев, и глава = одно звено-простыня
LINK_SEC = 80.0
LINK_MIN_SEC = 45.0
LINK_MAX_SEC = 120.0
# Сколько знаков уже написанного текста класть в промпт главы целиком. Сжимать
# незачем: 24 минуты речи — ~15 тыс. знаков (5.6 знака на слово, замер ep001), а
# rules + blueprint + playbook у doc_channel — ~300 тыс. знаков, то есть текст глав
# добавляет к входу ~5%. Порог — только страховка для роликов за час: сверх него
# ранние главы идут своей записью плана, последняя перед этой — целиком
WRITTEN_MAX_CHARS = 40_000
# находка python-а о форме главы: блоков нет или в главе чужие звенья
CHAPTER_KIND = "не по плану главы"


@dataclass
class ChapterWork:
    """Что вышло у сценария по главам — для свода и сообщения стадии."""

    plan: ChapterPlan
    texts: dict[str, str]
    crits: dict[str, ChapterCritique]
    seams: Critique
    critique: Critique

    def summary(self) -> str:
        rows = []
        for ch in self.plan.chapters:
            speech = sum(b.speech_sec for b in parse_script(self.texts[ch.id]))
            crit = self.crits[ch.id]
            how = "правлена руками" if crit.manual else f"раундов {crit.round}"
            rows.append(f"{ch.id} речи {speech:.0f} из {ch.planned_sec:.0f} с, {how}")
        text = f"глав: {len(self.plan.chapters)} ({'; '.join(rows)})"
        text += (
            f" · стыки: находок {len(self.seams.findings)}, блокирующих {len(self.seams.blocking)}"
        )
        if self.plan.warnings:
            text += f" · план глав: {'; '.join(self.plan.warnings)}"
        return text


def chapter_state(
    ctx: StageContext, cdir: Path, ch: Chapter, plan_sha: str, plan_path: Path
) -> tuple[str, str, ChapterCritique | None]:
    """draft | manual | fresh — и текст с разбором, если они есть.

    Порядок проверок и есть политика: поменялась запись главы в плане — план
    побеждает ручную правку (как подача побеждает цепочку); текст разошёлся с
    отпечатком — правка руками, её не переписывают; сменилась модель — глава
    устарела, если её не правил человек."""
    md = cdir / f"{ch.id}.md"
    if ctx.options.force or not md.exists():
        return "draft", "", None
    text = md.read_text(encoding="utf-8")
    crit_path = cdir / f"{ch.id}.critique.json"
    crit: ChapterCritique | None = None
    if crit_path.exists():
        try:
            crit = read_model(crit_path, ChapterCritique)
        except (ValidationError, ValueError):
            ctx.log.warning("%s не читается — главу переписываю", crit_path.name)
            return "draft", "", None
    if crit is None:
        # текст без разбора: положен руками после плана — или остался от прежнего
        placed_after_plan = md.stat().st_mtime >= plan_path.stat().st_mtime
        return ("manual", text, None) if placed_after_plan else ("draft", "", None)
    if crit.plan_sha != plan_sha:
        return "draft", "", None
    if crit.text_sha != text_sha(text):
        return "manual", text, crit
    if crit.manual or crit.llm == ctx.providers.llm.name:
        return "fresh", text, crit
    return "draft", "", None


def save_chapter(
    cdir: Path,
    ch: Chapter,
    text: str,
    crit: ChapterCritique,
    plan_sha: str,
    *,
    write_text: bool = True,
) -> str:
    """Текст главы, потом её разбор с отпечатками: в таком порядке упавший между
    ними процесс оставляет текст без разбора, а не разбор чужого текста."""
    text = text.strip() + "\n"
    if write_text:
        (cdir / f"{ch.id}.md").write_text(text, encoding="utf-8")
    crit.chapter, crit.plan_sha, crit.text_sha = ch.id, plan_sha, text_sha(text)
    write_model(cdir / f"{ch.id}.critique.json", crit)
    return text


def take_script_edits(ctx: StageContext, plan: ChapterPlan, cdir: Path, script_path: Path) -> None:
    """script.md поправлен руками — правка раскладывается обратно по главам.

    script.md длинного ролика собирается из глав, и без этого естественный жест
    «поправил сценарий на воротах» молча стирался бы следующей сборкой. Правка —
    это текст главы в script.md, который разошёлся с её файлом, при том что
    script.md новее всех глав (глава новее — значит правили главу, а собранный
    файл просто устарел). Сверяется с файлами тех глав, из которых script.md
    собран, а не с планом: план мог перестроиться, и тогда главы перепишутся по
    отпечаткам, а не по ложной «ручной правке». Главу плана без файла (удалили,
    чтобы модель написала её заново) сверять не с чем — она пропускается."""
    files = {f.stem: f for f in cdir.glob("*.md")}
    if not script_path.exists() or not files:
        return  # сценарий ещё не собирался из глав
    if script_path.stat().st_mtime <= max(f.stat().st_mtime for f in files.values()):
        return
    planned = {ch.id for ch in plan.chapters}
    found = split_chapters(script_path.read_text(encoding="utf-8"))
    parts = {cid: body for cid, body in found.items() if cid in files or cid not in planned}
    unknown = [cid for cid in parts if cid not in files]
    if not found or unknown:
        raise ValueError(
            f"{SCRIPT_FILE} поправлен руками, но заголовки глав в нём "
            f"({', '.join(found) or 'нет ни одного'}) не совпадают с файлами в "
            f"{CHAPTERS_DIR}/ ({', '.join(sorted(files))}): правь текст в "
            f"{CHAPTERS_DIR}/<глава>.md или верни заголовки «# chNN · название · секунды»"
        )
    changed = [
        cid
        for cid, body in parts.items()
        if text_sha(body) != text_sha(files[cid].read_text(encoding="utf-8"))
    ]
    for cid in changed:
        files[cid].write_text(parts[cid], encoding="utf-8")
    if changed:
        ctx.log.info("%s поправлен руками — правка разложена по главам: %s", SCRIPT_FILE, changed)


def write_if_changed(path: Path, text: str) -> bool:
    """Собранный файл пишется, только если изменился: иначе новый mtime делает
    устаревшим всё ниже — разметку удержания и стыки — без единой правки."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def _sha(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def text_sha(text: str) -> str:
    """Отпечаток текста главы без разницы в концах строк и хвостовых пробелах:
    редактор, сменивший CRLF, текста не правил."""
    return _sha("\n".join(line.rstrip() for line in text.strip().splitlines()))


def chapter_sha(ch: Chapter, story: Story) -> str:
    """Из чего написана глава: её запись в плане (с посчитанными секундами) и её
    звенья цепочки. Соседние главы в отпечаток не входят — их правка эту не трогает."""
    gaps = {g.id: g for g in story.gaps}
    return _sha(
        {
            "chapter": ch.model_dump(),
            "links": [gaps[link].model_dump() for link in ch.links if link in gaps],
        }
    )


# ------------------------------------------------------------ план глав


def is_long(target_sec: float) -> bool:
    """Пишется ли ролик по главам."""
    return target_sec > CHAPTER_MAX_SEC


def chapter_bounds(target_sec: float) -> tuple[int, int]:
    """Сколько глав допустимо: не длиннее потолка и не короче минимума."""
    lo = max(1, math.ceil(target_sec / CHAPTER_MAX_SEC))
    hi = max(lo, math.floor(target_sec / CHAPTER_MIN_SEC))
    return lo, hi


def story_length(target_sec: float) -> str:
    """Подсказка цепочке о зерне звена — только длинному ролику."""
    if not is_long(target_sec):
        return ""
    return p.STORY_LENGTH.format(
        target_sec=f"{target_sec:.0f}",
        links=max(2, round(target_sec / LINK_SEC)),
        link_min=f"{LINK_MIN_SEC:.0f}",
        link_max=f"{LINK_MAX_SEC:.0f}",
    )


def link_budget(story: Story, target_sec: float) -> dict[str, tuple[float, int]]:
    """Секунды и норма слов каждого звена — от цели эпизода, а не от модели.

    Модель задаёт звеньям разную длину, и это её суждение о ритме; сумма же —
    арифметика, и она у модели не сходится. Поэтому доли берутся из цепочки,
    а масштаб — из цели; целые секунды раздаются методом наибольших остатков,
    чтобы сумма совпала с целью ровно."""
    gaps = story.gaps
    if not gaps:
        return {}
    total = target_sec if target_sec > 0 else sum(g.planned_sec for g in gaps)
    weights = [max(0.0, g.planned_sec) for g in gaps]
    if sum(weights) <= 0:
        weights = [1.0] * len(gaps)
    exact = [total * w / sum(weights) for w in weights]
    secs = [math.floor(x) for x in exact]
    rest = round(total) - sum(secs)
    by_remainder = sorted(range(len(gaps)), key=lambda k: exact[k] - secs[k], reverse=True)
    for k in by_remainder[:rest]:
        secs[k] += 1
    return {g.id: (float(s), words_for(s)) for g, s in zip(gaps, secs, strict=True)}


def parse_chapters(raw: Any, *, llm: str) -> ChapterPlan:
    if not isinstance(raw, dict):
        raise ValueError(f"план глав: ожидался объект, пришло {type(raw).__name__}")
    data = raw
    chapters = data.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("модель не вернула ни одной главы")
    cleaned: list[dict[str, Any]] = []
    for i, item in enumerate(chapters, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"глава {i}: ожидался объект, пришло {type(item).__name__}")
        links = item.get("links")
        cleaned.append(
            {
                **item,
                # id — имя файла главы: по порядку, а не как назвала модель
                "id": f"ch{i:02d}",
                "title": str(item.get("title") or f"ch{i:02d}"),
                "links": [str(x) for x in links] if isinstance(links, list) else [],
            }
        )
    return ChapterPlan.model_validate(
        {
            "promise": data.get("promise", ""),
            "throughline": data.get("throughline", ""),
            "chapters": cleaned,
            "llm": llm,
            "generated_at": datetime.now(UTC),
        }
    )


def apply_numbers(plan: ChapterPlan, story: Story, target_sec: float) -> ChapterPlan:
    """Проверить состав глав и посчитать их секунды и норму слов.

    Состав — жёсткое условие: каждое звено цепочки ровно в одной главе, по
    порядку. Иначе ValueError — глава без звена или звено дважды это не ритм,
    а потерянный или повторённый кусок сюжета."""
    if not plan.chapters:
        raise ValueError(f"в {CHAPTERS_FILE} нет ни одной главы")
    ids = [ch.id for ch in plan.chapters]
    bad = [cid for cid in ids if not re.fullmatch(r"[\w-]+", cid)]
    if bad or len(set(ids)) != len(ids):
        raise ValueError(
            f"id глав должны быть разными и годными в имя файла (буквы, цифры, _ и -): {ids}"
        )
    expected = [g.id for g in story.gaps]
    got = [link for ch in plan.chapters for link in ch.links]
    if got != expected:
        missing = [g for g in expected if g not in got]
        extra = [link for link in got if link not in expected]
        twice = sorted({link for link in got if got.count(link) > 1})
        empty = [ch.id for ch in plan.chapters if not ch.links]
        why = [
            f"нет звеньев {missing}" if missing else "",
            f"звеньев нет в цепочке: {extra}" if extra else "",
            f"звенья в двух главах: {twice}" if twice else "",
            f"главы без звеньев: {empty}" if empty else "",
        ]
        reason = "; ".join(w for w in why if w) or f"порядок звеньев не тот, что в цепочке: {got}"
        raise ValueError(f"главы должны покрыть цепочку {expected} подряд и по разу — {reason}")
    budget = link_budget(story, target_sec)
    for ch in plan.chapters:
        ch.planned_sec = sum(budget[link][0] for link in ch.links)
        ch.words = sum(budget[link][1] for link in ch.links)
    plan.target_duration_sec = target_sec
    return plan


def chapter_problems(plan: ChapterPlan, target_sec: float) -> list[str]:
    """Что не так с раскладкой, кроме состава: число глав и их длина."""
    lo, hi = chapter_bounds(target_sec)
    problems: list[str] = []
    count = len(plan.chapters)
    if not lo <= count <= hi:
        problems.append(f"глав {count}, а на {target_sec:.0f} с нужно от {lo} до {hi}")
    for ch in plan.chapters:
        if ch.planned_sec > CHAPTER_MAX_SEC and len(ch.links) > 1:
            problems.append(
                f"{ch.id}: {ch.planned_sec:.0f} с — длиннее {CHAPTER_MAX_SEC:.0f} с, "
                "раздели её звенья на две главы"
            )
        if ch.planned_sec < CHAPTER_MIN_SEC and count > 1:
            problems.append(
                f"{ch.id}: {ch.planned_sec:.0f} с — короче {CHAPTER_MIN_SEC:.0f} с, "
                "отдай её звенья соседней главе"
            )
    return problems


def chapter_payload(ch: Chapter) -> dict[str, Any]:
    return ch.model_dump()


def plan_payload(plan: ChapterPlan) -> str:
    return json.dumps(
        {
            "promise": plan.promise,
            "throughline": plan.throughline,
            "chapters": [chapter_payload(ch) for ch in plan.chapters],
        },
        ensure_ascii=False,
        indent=1,
    )


def links_payload(
    ch: Chapter, story: Story, budget: dict[str, tuple[float, int]]
) -> list[dict[str, Any]]:
    """Звенья главы с посчитанными секундами и нормой слов — как у сценария
    одним заходом, только секунды уже приведены к цели эпизода."""
    gaps = {g.id: g for g in story.gaps}
    return [
        {**gaps[link].model_dump(), "planned_sec": budget[link][0], "words": budget[link][1]}
        for link in ch.links
        if link in gaps and link in budget
    ]


def written_so_far(plan: ChapterPlan, texts: dict[str, str], index: int) -> str:
    """Закадровый текст глав до этой — то, что зритель уже услышал. Кадр и
    эмоция не идут: повторяется сказанное, а не служебные слои."""
    if index == 0:
        return "— (this is the first chapter: nothing is written yet)"
    parts: list[tuple[Chapter, str]] = []
    for ch in plan.chapters[:index]:
        blocks = parse_script(texts.get(ch.id, ""))
        body = "\n".join(f"[{b.id}] {b.text}" for b in blocks) or texts.get(ch.id, "").strip()
        parts.append((ch, f"# {ch.id} · {ch.title}\n{body}"))
    if sum(len(text) for _, text in parts) > WRITTEN_MAX_CHARS:
        parts = [
            (ch, f"# {ch.id} · {ch.title}\n(only its plan, the text is too long) {ch.about}")
            for ch, _ in parts[:-1]
        ] + parts[-1:]
    return "\n\n".join(text for _, text in parts)


def position(plan: ChapterPlan, index: int, story: Story) -> str:
    ch = plan.chapters[index]
    if index == 0:
        start = p.POSITION_FIRST
    else:
        prev = plan.chapters[index - 1]
        start = p.POSITION_AFTER.format(
            prev_id=prev.id, prev_ends_with=prev.ends_with or prev.about or "—"
        )
    if index == len(plan.chapters) - 1:
        end = p.POSITION_LAST.format(
            promise=plan.promise or "—", ending=story.ending or ch.ends_with or "—"
        )
    else:
        end = p.POSITION_BRIDGE.format(ends_with=ch.ends_with or "—")
    return f"{start} {end}"


def without_chapter_headings(text: str) -> str:
    """Заголовок главы ставит сборка; свой заголовок модели — лишний."""
    return "\n".join(line for line in text.splitlines() if not is_chapter_heading(line)).strip()


def plan_findings(text: str, ch: Chapter, plan: ChapterPlan) -> list[Finding]:
    """Форма главы — python: ответ без блоков не прочтут ни планнер, ни проверки,
    а блок чужого звена зритель услышит дважды."""
    blocks = parse_script(text)
    if not blocks:
        return [
            Finding(
                kind=CHAPTER_KIND,
                where=ch.id,
                why_viewer_leaves=(
                    "в ответе нет ни одного блока «## id · название · секунды» — главу не "
                    "прочтут ни планнер, ни проверка речи"
                ),
                fix=f"написать главу {ch.id} блоками по звеньям {', '.join(ch.links)}",
                blocking=True,
            )
        ]
    owner = {link: other.id for other in plan.chapters for link in other.links}
    foreign = [b.id for b in blocks if owner.get(b.id, ch.id) != ch.id]
    if not foreign:
        return []
    return [
        Finding(
            kind=CHAPTER_KIND,
            where=", ".join(foreign),
            why_viewer_leaves=(
                f"блоки {', '.join(foreign)} — звенья глав "
                f"{', '.join(sorted({owner[b] for b in foreign}))}: зритель услышит их дважды"
            ),
            fix=f"убрать из главы {ch.id} блоки {', '.join(foreign)}",
            blocking=True,
        )
    ]


def seam_targets(
    findings: list[Finding], plan: ChapterPlan, texts: dict[str, str]
) -> dict[str, list[Finding]]:
    """Какую главу переписывать по находке стыков: первая названная глава, а
    если глава не названа — глава названного блока. Находка без адреса
    остаётся человеку."""
    chapter_ids = {ch.id for ch in plan.chapters}
    block_owner: dict[str, str] = {}
    for ch in plan.chapters:
        for block in parse_script(texts.get(ch.id, "")):
            block_owner.setdefault(block.id, ch.id)
    targets: dict[str, list[Finding]] = {}
    for finding in findings:
        tokens = re.findall(r"[\w-]+", finding.where)
        cid = next((t for t in tokens if t in chapter_ids), None) or next(
            (block_owner[t] for t in tokens if t in block_owner), None
        )
        if cid:
            targets.setdefault(cid, []).append(finding)
    return targets
