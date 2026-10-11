"""Проверка читаемости раскадровки: поймёт ли зритель кадр без звука.

Зачем. В ep001 (doc_channel, 08.09.2026) планнер написал «три борозды без букв»
там, где речь про надпись, и «ленту чистого пламени» там, где речь про книгу:
промпты исполнимы, художник их нарисовал, а зритель за секунду не понял бы, о
чём сцена. Однообразие ловится арифметикой (`variation`), а смысл кадра — нет,
поэтому здесь второй вызов модели, по образцу критика у сценариста.

Как. Модель получает ТОЛЬКО описание кадра (и текст плашки, если она есть) —
без реплики, чтобы не подсказывать, — и на каждую сцену отвечает тремя словами:
что зритель увидит за секунду. Python сверяет слова с существительными реплики
по корням (первые буквы слова, без лемматизации — для «связано / не связано»
этого хватает). Сцены без совпадения уходят на второй, короткий вызов: модель
видит реплику и увиденное и судит «связано / не связано» с причиной. Причина
нужна не для отчёта, а для повтора планнера: он получает её текстом ошибки.

Несвязанные сцены — блок, как у однообразия: один повтор планнера с причинами,
дальше предупреждение на ворота и заметка в `checks.notes["readability"]`.
Стоимость — $0 на claude_cli, ~6 с + символы ответа / 90.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from videoforge.core.stage import StageContext
from videoforge.core.storyboard import Scene
from videoforge.providers.base import ProviderError
from videoforge.stages.llm_json import ask_json

STEP_MARK = "VIDEOFORGE-STEP: readability"
VERDICT_MARK = "VIDEOFORGE-STEP: readability_verdict"
SYSTEM = "You are a viewer watching a narrated video with the sound off."
BATCH = 50  # сцен на вызов: ответ ~40 символов на сцену, claude_cli ≈ 6 с + символы/90
ROOT_LEN = 4  # корень = первые буквы слова: «могил-а/-ы/-е» совпадают без лемматизации
MIN_WORD_LEN = 4  # короче — предлоги, союзы и местоимения
SEEN_WORDS = 3

# служебные слова, которые иначе становятся «корнями» реплики (и ключевыми
# словами плашек у планнера — тот же список)
STOP_WORDS = frozenset(
    """
    когда тогда если чтобы потому поэтому который которая которое которые этот
    этого этому этой этом этих тоже также даже только просто очень самый такой
    такая такое такие может можно нужно надо быть было были есть будет будут
    после перед через между сейчас теперь всегда никогда почему зачем себя свой
    свою своё своя свои ваши ваша ваше наши наша наше весь всех всего всем ними
    нами вами него неё них здесь туда сюда оттуда ещё уже вот как-то что-то
    кто-то либо однако значит именно вообще совсем почти снова опять сколько
    столько меньше больше много мало every there their about would could
    """.split()
)


@dataclass
class ReadabilityFinding:
    scene_id: str
    narration: str
    seen: list[str]
    reason: str = ""


@dataclass
class ReadabilityResult:
    unrelated: list[ReadabilityFinding] = field(default_factory=list)
    seen: dict[str, list[str]] = field(default_factory=dict)  # что модель увидела, по сценам
    calls: int = 0
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.unrelated


DESCRIBE = (
    STEP_MARK
    + """
Below are descriptions of single frames of a narrated video. You do NOT hear
the narration. For every frame, say what a viewer will understand in ONE second
of looking: the thing, place or person it is about — not the style, not the
lighting, not the mood. If a frame carries an on-screen plate, its text is what
the viewer reads, so it counts as seen.

FRAMES (id | frame description | on-screen plate, if any)
{frames}

Return ONLY a valid JSON object, no fences, no commentary:
{{"frames": [{{"id": "s01", "seen": ["word", "word", "word"]}}]}}

Rules:
- Exactly {n} words per frame, nouns first, in {language}. Write the object
  you would name to a friend ("могила", "книга", "весы"), not adjectives.
- Answer for every id listed, in the same order. Do not invent what is not in
  the description: a frame that shows nothing nameable gets ["ничего"]."""
)

VERDICT = (
    VERDICT_MARK
    + """
For each scene below you get the narration the viewer HEARS and what the
viewer SEES in the frame (named by someone who watched it with the sound off).
Decide whether the picture shows what the words are about, so that a viewer who
sees the frame for one second without sound understands the subject.

SCENES
{scenes}

Return ONLY a valid JSON object, no fences, no commentary:
{{"scenes": [{{"id": "s01", "related": true, "reason": ""}}]}}

Rules:
- related = true when the seen things ARE the subject of the narration or its
  obvious setting (a grave for words about burial, a book for words about a
  verse, a market for words about trade). A metaphor counts only if the
  narration names it itself.
- related = false when the frame is a riddle: an abstraction the words never
  mention, a texture, a glow, "three grooves without letters" for a sentence
  about an inscription. Then `reason` says in one sentence, in {language}, what
  the frame should show instead — name the noun.
- Answer for every id, in the same order."""
)


def key_roots(text: str) -> set[str]:
    """Корни знаменательных слов: первые `ROOT_LEN` букв слов длиннее
    `MIN_WORD_LEN`, без служебных. Корни, а не леммы: «могилы» и «могиле»
    совпадают на «моги», и для вопроса «об одном ли речь» этого достаточно."""
    roots: set[str] = set()
    for word in re.findall(r"[a-zа-яё]+(?:-[a-zа-яё]+)?", text.lower()):
        if len(word) < MIN_WORD_LEN or word in STOP_WORDS:
            continue
        roots.add(word[:ROOT_LEN])
    return roots


def related_by_roots(narration: str, seen: list[str], overlay_text: str = "") -> bool:
    """Есть ли у реплики и увиденного общий корень. Текст плашки — на стороне
    увиденного: его зритель прочитал с экрана."""
    return bool(key_roots(narration) & key_roots(" ".join(seen) + " " + overlay_text))


def scenes_to_check(scenes: list[Scene]) -> list[Scene]:
    """Первые сцены битов с речью: продолжения копируют кадр законно, а сцене
    без слов (выдох) не с чем сверяться."""
    return [
        s for s in scenes if not s.continues and s.narration.strip() and s.visual_prompt.strip()
    ]


def _frames_text(scenes: list[Scene]) -> str:
    lines = []
    for s in scenes:
        plate = s.overlay.text if s.overlay is not None else "—"
        lines.append(f"{s.id} | {s.visual_prompt.strip()} | {plate}")
    return "\n".join(lines)


def _parse_seen(raw: Any, ids: list[str]) -> dict[str, list[str]]:
    data = raw.get("frames") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        raise ValueError("ответ модели без списка frames")
    wanted = set(ids)
    out: dict[str, list[str]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id", ""))
        seen = item.get("seen")
        if sid in wanted and isinstance(seen, list):
            out[sid] = [str(w).strip() for w in seen if str(w).strip()][:SEEN_WORDS]
    if not out:
        raise ValueError("модель не описала ни одного кадра")
    # пропущенный кадр — не «связано»: без слов он не пройдёт сверку корней и
    # уйдёт на вердикт вместе с сомнительными
    for sid in ids:
        out.setdefault(sid, [])
    return out


def _parse_verdicts(raw: Any, ids: list[str]) -> dict[str, tuple[bool, str]]:
    data = raw.get("scenes") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        raise ValueError("ответ модели без списка scenes")
    wanted = set(ids)
    out: dict[str, tuple[bool, str]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id", ""))
        if sid in wanted:
            out[sid] = (bool(item.get("related")), str(item.get("reason") or "")[:300])
    return out


def check_readability(ctx: StageContext, scenes: list[Scene]) -> ReadabilityResult:
    """Что зритель увидит за секунду без звука — и о том ли это, о чём реплика.

    Молчание модели — не «связано»: сцена, по которой вердикта нет, остаётся
    в списке несвязанных с пометкой. Ошибка провайдера — ValueError наверх:
    планнер решает, ронять ли стадию."""
    result = ReadabilityResult()
    todo = scenes_to_check(scenes)
    result.checked = len(todo)
    if not todo:
        return result
    language = ctx.storyboard.language
    try:
        for start in range(0, len(todo), BATCH):
            batch = todo[start : start + BATCH]
            raw = ask_json(
                ctx,
                DESCRIBE.format(frames=_frames_text(batch), n=SEEN_WORDS, language=language),
                system=SYSTEM,
            )
            result.calls += 1
            result.seen.update(_parse_seen(raw, [s.id for s in batch]))
    except ProviderError as exc:
        raise ValueError(f"LLM: {exc}") from exc

    by_id = {s.id: s for s in todo}
    doubtful = [
        s
        for s in todo
        if not related_by_roots(
            s.narration, result.seen.get(s.id, []), s.overlay.text if s.overlay else ""
        )
    ]
    if not doubtful:
        return result
    lines = []
    for s in doubtful:
        seen = ", ".join(result.seen.get(s.id, [])) or "—"
        lines.append(f"{s.id} | hears: {s.narration.strip()} | sees: {seen}")
    try:
        raw = ask_json(
            ctx, VERDICT.format(scenes="\n".join(lines), language=language), system=SYSTEM
        )
    except ProviderError as exc:
        raise ValueError(f"LLM: {exc}") from exc
    result.calls += 1
    verdicts = _parse_verdicts(raw, [s.id for s in doubtful])
    for s in doubtful:
        related, reason = verdicts.get(s.id, (False, "модель не вынесла вердикт"))
        if related:
            continue
        result.unrelated.append(
            ReadabilityFinding(
                scene_id=s.id,
                narration=by_id[s.id].narration.strip(),
                seen=result.seen.get(s.id, []),
                reason=reason,
            )
        )
    return result


def format_block(findings: list[ReadabilityFinding], limit: int = 12) -> str:
    """Причины для повтора планнера: сцена, что слышно, что видно, что показать."""
    rows = []
    for f in findings[:limit]:
        seen = ", ".join(f.seen) or "—"
        fix = f" → {f.reason}" if f.reason else ""
        rows.append(f"{f.scene_id}: hears «{f.narration[:90]}», sees «{seen}»{fix}")
    more = f" (+{len(findings) - limit})" if len(findings) > limit else ""
    return (
        f"{len(findings)} scenes are riddles: a viewer with the sound off would not "
        f"understand what they are about. The frame must show the NOUN of the narration; "
        f"anything to be read goes to overlay. " + "; ".join(rows) + more
    )


def note(finding: ReadabilityFinding) -> str:
    """Заметка в `checks.notes["readability"]` — для отчёта и ворот."""
    seen = ", ".join(finding.seen) or "—"
    text = f"без звука зритель увидит: {seen}"
    return f"{text} — {finding.reason}" if finding.reason else text
