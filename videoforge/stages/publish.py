"""Стадия publish: готовый файл + упаковка → `06_publish/` для ручной загрузки.

Загрузки здесь нет и пока не будет: YouTube Data API — первый кандидат, TikTok и
Instagram требуют ревью приложения и в личном использовании часто недоступны.
Но всё, что нужно человеку в момент загрузки, стадия готовит и проверяет:

- **заголовок, описание и хэштеги.** У нарезок их пишет критик моментов, который
  читал текст целиком (`Storyboard.publish`); у остальных эпизодов упаковки
  нет — стадия просит её у модели один раз по озвучке и брифу. Своя упаковка
  побеждает: переписывать то, что человек поправил руками, стадия не станет.
- **проверки под площадку**, детерминированные: хронометраж против лимита
  (`PLATFORMS[..]["max_sec"]`) и «сладкой» длины, формат кадра против
  ожидаемого, длина заголовка и описания. Это арифметика, и её должен считать
  python, а не человек в момент, когда он уже нажимает «загрузить».
- **главы в описании** у длинного горизонтального ролика (`chapters`): строки
  «MM:SS Название» по границам сцен из измеренного `05_assemble/timeline.json`,
  а не по плану — отметка обязана попадать в склейку. Главу открывает сцена с
  плашкой-ярлыком (`overlay.kind == "label"`, «ПУНКТ 3»), а называет её ближайшая
  плашка-вывод (`caption`) внутри главы: ярлык говорит «здесь начинается», вывод
  — «о чём». Правила YouTube зашиты числами: первая отметка 00:00, не меньше
  трёх глав, каждая не короче 10 с. Глава без содержательного названия хуже её
  отсутствия — тогда глав не пишется вовсе, а стадия говорит почему.
- **ссылки на исходный выпуск нет** («Full episode: …»): с 15.09.2026 стадия
  дописывала её нарезкам последней строкой, 18.09.2026 владелец убрал — так
  никто из нарезчиков не делает (`features/clip_source_credit`, dropped).

Файл `06_publish/publish.md` открывается и копируется; `publish.json` — то же
самое для будущей автозагрузки. Ролик стадия не трогает и никуда не отправляет.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from videoforge.core.ffmpeg import probe_size
from videoforge.core.io import write_model
from videoforge.core.project import PLATFORMS
from videoforge.core.stage import Stage, StageContext, StageResult
from videoforge.core.storyboard import (
    OVERLAY_MAX_CHARS,
    OverlayKind,
    PublishInfo,
    Scene,
    is_filmed,
)
from videoforge.core.thumbnail import ThumbChoice
from videoforge.core.workflow import load_workflow
from videoforge.paths import ASSEMBLE_DIR, PUBLISH_DATA_FILE, PUBLISH_DIR
from videoforge.providers.base import ProviderError
from videoforge.stages.assemble import TIMELINE_FILE
from videoforge.stages.llm_json import ask_json

OUT_DIR = PUBLISH_DIR  # имя папки живёт в paths: его читают и очередь, и пульт
PAGE_FILE = "publish.md"
DATA_FILE = PUBLISH_DATA_FILE

TITLE_MAX = 100  # YouTube режет заголовок здесь; в ленте видно ещё меньше
DESCRIPTION_MAX = 4800  # лимит 5000, оставляем место под хэштеги и ссылку
HASHTAGS_MAX = 8

# --- главы. Числа — правила YouTube для таймкодов в описании, не наш вкус
CHAPTERS_MIN_SEC = 180.0  # короче — площадка считает ролик шортсом, глав у него нет
CHAPTER_MIN_SEC = 10.0  # глава короче не принимается: сливаем со следующей
CHAPTERS_MIN = 3  # меньше трёх отметок площадка главами не считает
CHAPTER_TITLE_MAX = 60  # в полосе перемотки длиннее не читается
# строка описания, начинающаяся с таймкода: человек уже написал главы сам
_TIMESTAMP_LINE = re.compile(r"(?m)^\d{1,2}:\d{2}(?::\d{2})?\s")

SYSTEM = "You package a finished short video for the feed: title, description, hashtags."
PROMPT = (
    "VIDEOFORGE-STEP: publish\n"
    """
Package this finished video for {platforms}. Answer in {language}.

TITLE THE AUTHOR GAVE IT: {title}
BRIEF: {brief}
LENGTH: {duration:.0f} s

WHAT IS SAID IN IT
{narration}

Return ONLY a valid JSON object:
{{"title": "...", "description": "...", "hashtags": ["..."]}}

- `title`: what the viewer sees in the feed, up to {title_max} characters. The
  intrigue must come FROM THE VIDEO: a claim, a number, a confession, a question
  it answers. No clickbait promising what is not there, no ALL CAPS, no emoji.
- `description`: two or three sentences — what happens and who is speaking.
- `hashtags`: 3-6 items without the "#", lowercase.
"""
)


class PublishCheck(BaseModel):
    label: str
    value: str
    ok: bool
    target: str
    # проваленная блокирующая проверка останавливает загрузку (без --force):
    # остальные — совет человеку, эта — ролик, который площадка всё равно скроет
    blocking: bool = False


class Chapter(BaseModel):
    """Глава ролика: секунда готового файла, где она начинается, и название,
    которое зритель видит в полосе перемотки."""

    start_sec: float
    title: str


class PublishPlan(BaseModel):
    """Что и куда готовы выложить — плюс замеры против требований площадки."""

    generated_at: datetime | None = None
    by: str = ""  # storyboard | llm — откуда взялась упаковка
    video: str = ""
    duration_sec: float = 0.0
    platforms: list[str] = Field(default_factory=list)
    title: str = ""
    # описание уже с блоком глав в конце: загрузка берёт его как есть, и
    # отдельно склеивать ей нечего. Чистый текст без глав живёт в раскадровке
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    # главы по измеренным границам сцен (`chapters`); пусто — ролик короткий,
    # вертикальный или главам не из чего собраться. Здесь они для пульта и
    # проверок, в описание они уже вписаны
    chapters: list[Chapter] = Field(default_factory=list)
    # строка кредита за музыку, если лицензия трека требует упоминания. Её
    # обязательно вставить в описание при загрузке — CC BY без имени автора
    # это нарушение лицензии, о котором человек узнаёт от площадки
    attribution: str = ""
    # обложка: путь относительно папки эпизода и объяснение, почему этот кадр.
    # Площадка иначе возьмёт случайную секунду — чаще всего с открытым ртом
    thumbnail: str = ""
    thumbnail_why: str = ""
    checks: list[PublishCheck] = Field(default_factory=list)
    # подсказка про плашку-хук у нарезки без текста поверх кадра (`hook_hint`):
    # ставить её или нет — решение человека, стадия только напоминает, что так можно
    hook_hint: str = ""

    @property
    def failed(self) -> list[PublishCheck]:
        return [c for c in self.checks if not c.ok]


# окно хука по эталону: первые две секунды, пока зритель ещё не понял, кто говорит
HOOK_END_SEC = 2.0


def hook_hint(scenes: list[Scene], title: str) -> str:
    """Напоминание о плашке-хуке для нарезки, где текста поверх кадра нет.

    У канала-эталона (research/yuka_mma, blueprint) первые две секунды перекрыты
    крупным заголовком-вопросом: зритель читает обещание раньше, чем понимает,
    кто говорит. `Scene.overlay` с окном `start_sec`/`end_sec` такую плашку
    рисует, но САМА стадия её не ставит: у нарезки плашку пишет человек, и
    правила канала (`rules.md`) вправе запрещать графику вовсе. Поэтому — строка
    в publish.md с готовым JSON, а не правка раскадровки. Только нарезкам
    (`is_filmed`) и только если ни у одной сцены плашки нет: у ai_video плашки
    ставит планнер, а поверх уже поставленной подсказка была бы шумом."""
    title = title.strip()
    if not title or not is_filmed(scenes):
        return ""
    if any(s.overlay is not None and s.overlay.text.strip() for s in scenes):
        return ""
    first = min(scenes, key=lambda s: s.order)
    plate = {"kind": "caption", "text": title, "start_sec": 0, "end_sec": HOOK_END_SEC}
    too_long = (
        f"; заголовок длиннее {OVERLAY_MAX_CHARS} знаков — на плашке оставь одну мысль"
        if len(title) > OVERLAY_MAX_CHARS
        else ""
    )
    return (
        f"текста поверх кадра нет, а у канала-эталона первые {HOOK_END_SEC:g} с перекрыты "
        f"заголовком-вопросом. Если правила канала графику не запрещают — сцене `{first.id}` "
        f'в `storyboard.json` добавь `"overlay": {json.dumps(plate, ensure_ascii=False)}` '
        f"и пересобери (`videoforge run <проект>/<эпизод> --only assemble`){too_long}"
    )


# ------------------------------------------------------------------ главы


@dataclass
class _Span:
    """Заготовка главы: отрезок готового файла и сцены, которые в него попали.
    Название ищется по сценам уже после слияния коротких — чтобы у слитой
    главы был выбор из всех её плашек, а не только из тех, что были до склейки."""

    start: float
    end: float
    scenes: list[Scene] = field(default_factory=list)

    @property
    def sec(self) -> float:
        return self.end - self.start

    def absorb(self, other: _Span) -> None:
        self.end = other.end
        self.scenes.extend(other.scenes)


def _plate(scene: Scene, kind: OverlayKind) -> str:
    """Текст плашки сцены нужного вида; пусто — плашки нет или она другого вида."""
    plate = scene.overlay
    if plate is None or plate.kind != kind:
        return ""
    return plate.text.strip()


def chapter_title(text: str) -> str:
    """Название главы из текста плашки: одна строка, без точки на конце, не
    длиннее `CHAPTER_TITLE_MAX`. Плашка — реплика («Вода была раньше земли.»),
    глава — заголовок: точка в полосе перемотки выглядит как обрыв. Длинное
    режется по слову с многоточием, а не по букве."""
    one = " ".join(text.split()).rstrip(" .…")
    if len(one) <= CHAPTER_TITLE_MAX:
        return one
    head = one[: CHAPTER_TITLE_MAX - 1]
    cut = head.rsplit(" ", 1)[0].rstrip(" ,;:—-") if " " in head else head
    return f"{cut}…"


def timestamp(sec: float, *, hours: bool = False) -> str:
    """Секунда → «MM:SS», у ролика длиннее часа — «H:MM:SS» (`hours`).

    Округление вниз: отметка, оказавшаяся на кадр ПОЗЖЕ склейки, показывает
    зрителю хвост прошлой главы."""
    total = int(sec)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if hours or h else f"{m:02d}:{s:02d}"


def chapters(
    scenes: list[Scene],
    timeline: list[dict[str, Any]],
    *,
    aspect_ratio: str,
    duration_sec: float | None = None,
) -> list[Chapter]:
    """Главы ролика по плашкам сцен и измеренным границам из `timeline.json`.

    `timeline` — строки `05_assemble/timeline.json` (`scene_id`, `start`,
    `end`): их пишет сборка по фактическим длинам клипов, и только по ним
    отметка попадает в склейку; сумма `duration_sec` сцен — план, и на ней
    главы не строятся. `duration_sec` — длина готового файла (гейт «не короче
    трёх минут» и формат отметок); None — конец таймлайна.

    Правила, по порядку: только 16:9 не короче `CHAPTERS_MIN_SEC` (вертикаль —
    шортс, главы ему не нужны); главу открывает сцена с плашкой `label`; первая
    глава всегда с 00:00 — сцены до первого ярлыка образуют вступление; глава
    короче `CHAPTER_MIN_SEC` сливается со следующей (последняя — с предыдущей);
    название — ближайшая плашка `caption` внутри главы, без неё — текст ярлыка;
    у вступления ярлыка нет, и без вывода в нём названия не выходит. Любой
    провал — глав нет вовсе, без ошибки: три главы без четвёртой, которую было
    не назвать, врут зрителю о структуре ролика, и лучше пусть площадка
    поставит свои."""
    found, _ = _chapters(scenes, timeline, aspect_ratio=aspect_ratio, duration_sec=duration_sec)
    return found


def _chapters(
    scenes: list[Scene],
    timeline: list[dict[str, Any]],
    *,
    aspect_ratio: str,
    duration_sec: float | None,
) -> tuple[list[Chapter], str]:
    """То же, что `chapters`, плюс причина, почему глав нет, — для сообщения
    стадии. Пустая причина у пустого списка: главы ролику не положены (шортс,
    короткий), и говорить об этом на каждом шортсе незачем."""
    rows = [t for t in timeline if isinstance(t, dict) and "scene_id" in t]
    total = duration_sec or (float(rows[-1].get("end", 0.0)) if rows else 0.0)
    if aspect_ratio != "16:9" or total < CHAPTERS_MIN_SEC:
        return [], ""
    if not rows:
        return [], "границы сцен не измерены (нет timeline.json)"
    by_id = {s.id: s for s in scenes}
    spans: list[_Span] = []
    labels = 0
    for row in rows:
        start, end = float(row.get("start", 0.0)), float(row.get("end", 0.0))
        scene = by_id.get(str(row.get("scene_id", "")))
        if scene is None:  # в таймлайне есть, в раскадровке нет: часть текущей главы
            if spans:
                spans[-1].end = end
            continue
        opens = bool(_plate(scene, "label"))
        labels += opens
        if opens or not spans:
            spans.append(_Span(start=start, end=end, scenes=[scene]))
        else:
            spans[-1].absorb(_Span(start=start, end=end, scenes=[scene]))
    if not labels:
        return [], "в раскадровке нет плашек-ярлыков (label) — главам не с чего начинаться"
    spans[0].start = 0.0  # первая отметка — всегда начало файла, требование площадки

    merged: list[_Span] = []
    for span in spans:
        if merged and merged[-1].sec < CHAPTER_MIN_SEC:
            merged[-1].absorb(span)
        else:
            merged.append(span)
    if len(merged) > 1 and merged[-1].sec < CHAPTER_MIN_SEC:
        merged[-2].absorb(merged.pop())

    out: list[Chapter] = []
    for span in merged:
        text = next((t for s in span.scenes if (t := _plate(s, "caption"))), "") or next(
            (t for s in span.scenes if (t := _plate(s, "label"))), ""
        )
        title = chapter_title(text)
        if not title:
            return [], (
                f"у главы с {timestamp(span.start)} нет названия: ни вывода (caption), "
                f"ни ярлыка — глава без смысла хуже её отсутствия"
            )
        out.append(Chapter(start_sec=round(span.start, 3), title=title))
    if len(out) < CHAPTERS_MIN:
        return [], f"глав {len(out)}, площадка требует не меньше {CHAPTERS_MIN}"
    return out, ""


def chapter_lines(found: list[Chapter], *, duration_sec: float) -> str:
    """Строки «MM:SS Название» — так, как их читает площадка."""
    hours = duration_sec >= 3600
    return "\n".join(f"{timestamp(c.start_sec, hours=hours)} {c.title}" for c in found)


def describe(description: str, found: list[Chapter], *, duration_sec: float) -> str:
    """Описание с блоком глав в конце.

    Таймкоды, которые человек вписал в описание сам, побеждают — как и вся
    своя упаковка; второй список площадка прочла бы как ещё одни главы."""
    if found and not _TIMESTAMP_LINE.search(description):
        return f"{description}\n\n{chapter_lines(found, duration_sec=duration_sec)}".strip()
    return description


def _timeline(edir: Path) -> list[dict[str, Any]]:
    """Измеренные границы сцен, как их записала сборка. Нет файла — пусто."""
    path = edir / ASSEMBLE_DIR / TIMELINE_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


def platform_checks(
    info: PublishInfo,
    *,
    platforms: list[str],
    duration_sec: float,
    size: tuple[int, int] | None,
) -> list[PublishCheck]:
    """Требования площадок — арифметикой, а не памятью человека."""
    rows = [
        PublishCheck(
            label="заголовок",
            value=f"{len(info.title)} знаков",
            ok=0 < len(info.title) <= TITLE_MAX,
            target=f"1–{TITLE_MAX}",
        ),
        # пустое описание площадка принимает: нижней границы нет, считается
        # только лимит
        PublishCheck(
            label="описание",
            value=f"{len(info.description)} знаков",
            ok=len(info.description) <= DESCRIPTION_MAX,
            target=f"до {DESCRIPTION_MAX}",
        ),
        PublishCheck(
            label="хэштеги",
            value=str(len(info.hashtags)),
            ok=0 < len(info.hashtags) <= HASHTAGS_MAX,
            target=f"1–{HASHTAGS_MAX}",
        ),
    ]
    for name in platforms:
        spec: dict[str, Any] = PLATFORMS.get(name, {})
        if not spec:
            continue
        title = spec["title"]
        limit = spec.get("max_sec")
        if limit:
            rows.append(
                PublishCheck(
                    label=f"{title}: хронометраж",
                    value=f"{duration_sec:.0f} с",
                    ok=duration_sec <= limit,
                    target=f"≤ {limit} с",
                )
            )
        lo, hi = spec.get("sweet_spot_sec", (0, 0))
        if hi:
            rows.append(
                PublishCheck(
                    label=f"{title}: удачная длина",
                    value=f"{duration_sec:.0f} с",
                    # не запрет, а ориентир: за пределами «сладкой» длины ролик
                    # живёт хуже, но выкладывать его никто не мешает
                    ok=lo <= duration_sec <= hi,
                    target=f"{lo}–{hi} с",
                )
            )
        want = spec.get("aspect_ratio")
        if want and size:
            w, h = size
            actual = round(w / h, 3)
            expect = {"9:16": round(9 / 16, 3), "16:9": round(16 / 9, 3), "1:1": 1.0}.get(want)
            rows.append(
                PublishCheck(
                    label=f"{title}: формат кадра",
                    value=f"{w}×{h}",
                    ok=expect is None or abs(actual - expect) < 0.02,
                    target=want,
                )
            )
    return rows


def _music_attribution(ctx: StageContext) -> str:
    """Строка кредита за трек, который реально попал в готовый файл.

    Берётся из ресурса того трека, который назвала сборка (`final.music_track`),
    а не из проекта: план монтажа мог сменить трек, и кредит должен быть за то,
    что слышно. Пусто — лицензия упоминания не требует (CC0, свой трек)."""
    from videoforge.core.resources import MusicTrack, load_resource

    track_id = ctx.storyboard.final.music_track
    if not track_id:
        return ""
    try:
        track = load_resource("music", track_id, MusicTrack, ctx.resource_dirs)
    except FileNotFoundError:
        ctx.log.warning("трек %s не найден — кредит за музыку не подставлен", track_id)
        return ""
    return track.attribution.strip()


def _thumbnail(ctx: StageContext, video: Path, title: str) -> ThumbChoice | None:
    """Обложка: кадр, где герой в кадре и на экране слово из заголовка.

    Не падаем, если не вышло: ролик без своей обложки выкладывается, площадка
    возьмёт свой кадр — а вот эпизод, застрявший из-за картинки, не выкладывается
    вообще."""
    from videoforge.core.thumbnail import choose

    ass = video.parent / "captions.ass"
    try:
        return choose(
            video,
            title=title,
            ass_text=ass.read_text(encoding="utf-8") if ass.exists() else "",
        )
    except (OSError, ValueError, RuntimeError) as exc:
        ctx.log.warning("обложку выбрать не вышло: %s", exc)
        return None


def render_page(plan: PublishPlan) -> str:
    """Страница, которую человек открывает рядом с формой загрузки."""
    tags = " ".join(f"#{t}" for t in plan.hashtags)
    checks = "\n".join(
        f"| {c.label} | {c.value} | {c.target} | {'ок' if c.ok else '**мимо**'} |"
        for c in plan.checks
    )
    credit = (
        f"\n**Обязательно в описании (лицензия музыки):** {plan.attribution}\n"
        if plan.attribution
        else ""
    )
    cover = (
        f"\n**Обложка:** `{plan.thumbnail}` — {plan.thumbnail_why}\n"
        if plan.thumbnail
        else "\n**Обложка:** своей нет — площадка возьмёт случайный кадр\n"
    )
    hook = f"\n**Плашка-хук:** {plan.hook_hint}\n" if plan.hook_hint else ""
    heads = ""
    if plan.chapters:
        # то же, что в конце описания, но таблицей: описание копируют, таблицу читают
        rows = "\n".join(
            f"| {timestamp(c.start_sec, hours=plan.duration_sec >= 3600)} | {c.title} |"
            for c in plan.chapters
        )
        heads = f"\n## Главы\n\n| время | глава |\n|---|---|\n{rows}\n"
    return f"""# К загрузке

**Файл:** `{plan.video}` · {plan.duration_sec:.0f} с · площадки: {", ".join(plan.platforms) or "—"}

## Заголовок

{plan.title or "— (упаковки нет)"}

## Описание

{plan.description or "—"}

{tags}
{credit}{cover}{hook}{heads}
## Проверки

| замер | значение | требование | |
|---|---|---|---|
{checks}

Загрузку стадия не делает: файл и текст готовы, дальше руками.
"""


class PublishStage(Stage):
    name = "publish"
    needs = ()  # своя упаковка есть — модель не нужна; иначе стадия скажет, чего не хватает

    def run(self, ctx: StageContext) -> StageResult:
        sb = ctx.storyboard
        final = sb.final.video
        if not final or not ctx.abs(final).exists():
            return StageResult(status="failed", message="нет итогового видео — сначала assemble")

        info, by = sb.publish, "storyboard"
        if not info.title.strip():
            try:
                info, by = self._ask(ctx), "llm"
            except (ProviderError, ValueError, TypeError) as exc:
                ctx.log.warning("упаковку не спросили у модели: %s", exc)
                info, by = PublishInfo(title=sb.title), "storyboard"

        path = ctx.abs(final)
        duration = sb.final.duration_sec or 0.0
        platforms = ctx.project.channel.platforms if ctx.project else []
        thumb = _thumbnail(ctx, path, info.title)
        found, no_chapters = _chapters(
            sb.scenes,
            _timeline(ctx.project_dir),
            aspect_ratio=sb.aspect_ratio,
            duration_sec=duration,
        )
        # главы — часть описания, значит и лимит описания считается с ними:
        # площадка режет по 5000 знаков молча, и обрезанный список глав она
        # прочтёт как список покороче
        packed = PublishInfo(
            title=info.title.strip(),
            description=describe(info.description.strip(), found, duration_sec=duration),
            hashtags=list(info.hashtags),
        )
        plan = PublishPlan(
            generated_at=datetime.now(UTC),
            by=by,
            video=final,
            duration_sec=duration,
            platforms=platforms,
            title=packed.title,
            description=packed.description,
            hashtags=packed.hashtags,
            chapters=found,
            attribution=_music_attribution(ctx),
            thumbnail=ctx.rel(thumb.path) if thumb else "",
            thumbnail_why=thumb.why if thumb else "",
            checks=platform_checks(
                packed,
                platforms=platforms,
                duration_sec=duration,
                size=probe_size(path),
            ),
            # совет ведёт в пересборку плашкой — он только для нарезки из
            # длинного (workflow со стадией cut)
            hook_hint=(
                hook_hint(sb.scenes, info.title)
                if "cut" in load_workflow(ctx.manifest.workflow).stage_names()
                else ""
            ),
        )
        out = ctx.stage_dir(OUT_DIR)
        (out / PAGE_FILE).write_text(render_page(plan), encoding="utf-8")
        write_model(out / DATA_FILE, plan)
        if by == "llm":  # упаковку от модели кладём и в раскадровку: она теперь своя
            # без блока глав: они выводятся из таймлайна на каждом прогоне, а
            # записанные в раскадровку пережили бы пересборку и врали бы
            sb.publish = PublishInfo(
                title=plan.title, description=info.description.strip(), hashtags=plan.hashtags
            )
            ctx.save()

        rel = ctx.rel(out / PAGE_FILE)
        if not plan.title:
            return StageResult(
                status="skipped",
                message=(
                    f"упаковки нет и модель её не дала — файл готов к ручной загрузке: {final}"
                ),
            )
        bad = "; ".join(f"{c.label} {c.value} (надо {c.target})" for c in plan.failed)
        note = f" · ПРОВЕРЬ: {bad}" if bad else ""
        if plan.chapters:
            note += f" · глав в описании: {len(plan.chapters)}"
        elif no_chapters:  # длинный горизонтальный ролик остался без глав — сказать почему
            note += f" · глав нет: {no_chapters}"
        return StageResult(
            status="done",
            message=f"{rel}: «{plan.title}» ({by}), загрузка руками{note}",
            changed=True,
        )

    @staticmethod
    def _ask(ctx: StageContext) -> PublishInfo:
        sb = ctx.storyboard
        narration = "\n".join(s.narration for s in sb.ordered_scenes() if s.narration.strip())
        platforms = ctx.project.channel.platforms if ctx.project else []
        raw = ask_json(
            ctx,
            PROMPT.format(
                platforms=", ".join(platforms) or "vertical short video",
                # упаковку читают, а не слушают: её язык — язык текста на
                # экране, а не язык речи в ролике
                language=sb.text_language,
                title=sb.title or "—",
                brief=sb.brief.strip() or "—",
                duration=sb.final.duration_sec or 0.0,
                narration=narration[:6000] or "—",
                title_max=TITLE_MAX,
            ),
            system=SYSTEM,
        )
        if not isinstance(raw, dict):
            raise ValueError(f"ожидался объект упаковки, пришло {type(raw).__name__}")
        tags = raw.get("hashtags")
        return PublishInfo(
            title=str(raw.get("title") or "").strip()[:TITLE_MAX],
            description=str(raw.get("description") or "").strip(),
            hashtags=[str(t).lstrip("#").strip() for t in tags or [] if str(t).strip()][
                :HASHTAGS_MAX
            ],
        )
