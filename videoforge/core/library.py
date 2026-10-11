"""Библиотека приёмов: что можно применить в точке решения и при каких условиях.

Зачем отдельная сущность. Знание «как делается ролик» до сих пор оседало в
переписке и в `docs/*.md`, а документ, который надо вспомнить, надёжен ровно
настолько же, насколько память. Хуже того: наблюдение и правило записывались
одинаково. Фраза «у эталона затухание 0.5 с» — это наблюдение; правилом она
становится, только когда написано, **когда применять и когда нет**.

Устройство. Точка решения (`decision`) — момент производства, где надо выбрать:
чем кончается звук, как выглядят субтитры, какая цветокоррекция. В точке лежат
несколько приёмов, у каждого условия, доказательство и способ исполнения.
До дела приём доходит двумя путями. Человек (или Claude, собирая ролик руками)
читает библиотеку через `videoforge lib` и пульт. И стадия, чья модель выбирает
в точке библиотеки, объявляет точку в `Stage.decisions` и кладёт подходящие
приёмы в промпт (`for_stage`; условия ролика считает `conditions` из тех же
файлов, что читает стадия), а в своё сообщение — сколько и каких предложено.
Сегодня так делает одна стадия — `edit` в точке `audio_layers`. Точка
подключается к стадии только когда у модели есть ПОЛЕ ОТВЕТА, которым приём
выражается, и ЗАМЕР, по которому его выбирать: иначе приём в промпте инертен,
а это та же ложь «написано как сделанное», только в промпте. До 11.09.2026 этот
абзац утверждал, что стадии подтягивают приёмы сами, при том что `brief` не
вызывался ниоткуда, — вскрыл внешний аудит.

Чего здесь НЕ должно быть: подогнанных чисел. Кривая цветокоррекции, снятая с
конкретного релиза конкретного аниме, живёт в папке эпизода. В библиотеку идёт
форма приёма («тени в ноль, света чуть вверх, насыщенность +8%») и метод замера.
Иначе через месяц кривую Re:Zero наложат на другое аниме.

Чем это НЕ является. `research/playbook.md` копит правила про **содержание**
(хук в первые две секунды, выдох на 15 секунд) и кормит сценариста с планнером.
Библиотека — про **исполнение**, и кормит стадии рендера. Дисциплина общая:
атомарность, доказательство числом, и ценность опровержения выше ценности
подтверждения.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr

from videoforge.core.storyboard import Storyboard, is_filmed
from videoforge.paths import library_dir

if TYPE_CHECKING:
    from videoforge.core.project import ProjectConfig

log = logging.getLogger("videoforge.library")

# Точки решения. Список намеренно короткий и закрытый: приём, которому некуда
# лечь, — это либо новая точка (тогда её заводят осознанно), либо не приём.
DECISIONS: dict[str, str] = {
    "source_prep": "Подготовка исходника: чистка, разрешение, частота кадров",
    "shot_selection": "Что берём в ролик: сцена, моменты, порядок",
    "cut_rhythm": "Ритм нарезки: длины планов, к чему привязаны склейки",
    # «кадр показывает предмет реплики» точкой решения НЕ является: это правило,
    # которое планнер и проверка читаемости применяют всегда (features/
    # frame_readability_check), а не приём, который выбирают из галереи
    "framing": "Кадр и формат: соотношение, кадрирование, движение внутри кадра",
    "grade": "Цветокоррекция",
    # текст на экране разложен на четыре точки (08.09.2026): человек листает
    # галерею и говорит «цвет вот этот, шрифт вот тот» — каждая точка отвечает
    # за одно, и выбранное просто переписывается в caption_styles/<id>.yaml
    "captions": "Текст на экране: размер, место, поведение",
    "caption_font": "Шрифт субтитров",
    "caption_color": "Цвет текста и подсветки",
    "caption_animation": "Появление и движение слов",
    "audio_layers": "Что звучит: музыка, оригинальный звук, озвучка, их баланс",
    "ending_audio": "Чем кончается звук",
    "packaging": "Упаковка: заголовок, описание, обложка",
}

# Группы точек для глаза: пульт показывает библиотеку тремя-четырьмя разделами
# («картинка», «текст», «звук»), а не одиннадцатью карточками вразнобой. Каждая
# точка ровно в одной группе — проверяет тест.
DECISION_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "picture": (
        "Картинка и монтаж",
        ("shot_selection", "cut_rhythm", "framing", "grade", "source_prep"),
    ),
    "text": (
        "Текст на экране",
        ("captions", "caption_font", "caption_color", "caption_animation"),
    ),
    "sound": ("Звук", ("audio_layers", "ending_audio")),
    "packaging": ("Упаковка", ("packaging",)),
}

# Словарь условий. Тоже закрытый — иначе условия превращаются в прозу, которую
# нельзя проверить машиной. Всё, что не влезло, идёт в `notes` и ждёт, пока
# заслужит своё поле.
CONDITION_KEYS: dict[str, str] = {
    "format": "ai_video | clips_from_long | anime_edit",
    "platform": "youtube | instagram | tiktok",
    "aspect": "9:16 | 16:9 | 1:1",
    "footage": "generated | filmed | animation",
    "has_speech": "есть ли речь (озвучка или реплики в исходнике)",
    "music": "есть ли музыка",
    "duration_band": "short (<60 с) | mid (<8 мин) | long",
    "mood": "чувство, под которое делается ролик: surprise | aggression | admiration | calm",
}

# Полосы длительности для `conditions`: короче минуты — шортс, от восьми минут —
# длинный ролик (порог, с которого YouTube ставит рекламу внутри, и ролик
# делается иначе), между — mid
DURATION_BANDS: tuple[tuple[float, str], ...] = ((60.0, "short"), (480.0, "mid"))

Verdict = Literal["подходит", "не подходит", "не проверено"]


class Evidence(BaseModel):
    """Откуда приём взялся. Без этого приём — мнение, а не знание.

    `verdict: failed` так же ценен, как `works`: запись «пробовали здесь, не
    сработало, потому что» останавливает повторную ошибку, а подтверждение
    десятого подряд успеха не добавляет ничего."""

    source: str  # ссылка или описание: чей ролик, какой канал, сколько просмотров
    measured: str = ""  # числа, а не впечатление
    date: str = ""
    verdict: Literal["works", "failed", "n/a"] = "works"


class Technique(BaseModel):
    id: str
    decision: str
    title: str
    summary: str = ""
    # implemented — умеем и знаем чем; recorded — видели у других, не умеем.
    # Второй статус нужен ровно затем, чтобы честно отвечать «такое мы не умеем,
    # записать?» вместо того, чтобы молча не предложить.
    status: Literal["implemented", "recorded"] = "recorded"
    # Пустые условия запрещены: «всегда» пишется явно, чтобы это было решением,
    # а не забывчивостью. Проверяет `validate_technique`.
    always: bool = False
    applies_when: dict[str, Any] = Field(default_factory=dict)
    never_when: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    implemented_by: str = ""  # стадия/функция/настройка, которая это делает
    evidence: list[Evidence] = Field(default_factory=list)
    notes: str = ""
    _path: Path = PrivateAttr(default=Path("."))

    def matches(self, ctx: dict[str, Any]) -> tuple[Verdict, list[str]]:
        """Подходит ли приём под описание ролика.

        Возвращает вердикт и список условий, которые проверить не удалось.
        Непроверенное не прячем: показать с пометкой полезнее, чем умолчать."""
        unknown: list[str] = []
        for key, want in self.never_when.items():
            got = ctx.get(key)
            if got is None:
                unknown.append(key)
            elif _hit(got, want):
                return "не подходит", unknown
        if self.always and not self.applies_when:
            return "подходит", unknown
        for key, want in self.applies_when.items():
            got = ctx.get(key)
            if got is None:
                unknown.append(key)
            elif not _hit(got, want):
                return "не подходит", unknown
        return ("не проверено" if unknown else "подходит"), unknown


def _hit(got: Any, want: Any) -> bool:
    """Значение условия: скаляр или список допустимых. У ролика значение тоже
    бывает списком (площадок несколько) — тогда попадание любого элемента:
    ролик, который уходит и на YouTube, и в TikTok, «идёт в TikTok»."""
    if isinstance(got, list):
        return any(_hit(g, want) for g in got)
    if isinstance(want, list):
        return got in want
    return got == want


def conditions(
    storyboard: Storyboard, project: ProjectConfig | None, workflow: str
) -> dict[str, Any]:
    """Условия ролика по словарю `CONDITION_KEYS` — из тех же файлов, что читает
    стадия: раскадровка, project.yaml и имя workflow из манифеста.

    Считает python, а не модель: условие — факт о ролике (снятый или
    нарисованный, есть ли речь, есть ли музыка), и факт обязан быть одним и тем
    же у CLI, у стадии и у пульта. `mood` не выводится — чувство ролика нигде не
    записано, и приём с таким условием честно приходит с пометкой «условие не
    проверено». `footage: animation` появится вместе с workflow anime_edit —
    сегодня сцены с источником это снятое видео."""
    scenes = storyboard.ordered_scenes()
    total = sum(s.duration_sec for s in scenes)
    out: dict[str, Any] = {
        "format": workflow,
        "aspect": storyboard.aspect_ratio,
        "footage": "filmed" if is_filmed(scenes) else "generated",
        "has_speech": any(s.narration.strip() for s in scenes),
        "duration_band": next((name for limit, name in DURATION_BANDS if total < limit), "long"),
    }
    if project is None:
        out["music"] = storyboard.resources.music is not None
        return out
    out["music"] = bool(project.video.music)
    networks = [p.split("_", 1)[0] for p in project.channel.platforms]  # youtube_shorts → youtube
    if networks:
        out["platform"] = list(dict.fromkeys(networks))
    return out


def validate_technique(t: Technique) -> list[str]:
    """Правила, без которых библиотека сгниёт. Возвращает список проблем."""
    problems: list[str] = []
    if t.decision not in DECISIONS:
        problems.append(f"неизвестная точка решения '{t.decision}'; есть: {', '.join(DECISIONS)}")
    for field, data in (("applies_when", t.applies_when), ("never_when", t.never_when)):
        for key in data:
            if key not in CONDITION_KEYS:
                problems.append(
                    f"{field}: условие '{key}' вне словаря; есть: {', '.join(CONDITION_KEYS)}"
                )
    if not t.always and not t.applies_when:
        problems.append(
            "нет ни одного условия: напиши applies_when или always: true — "
            "«всегда» должно быть решением, а не забывчивостью"
        )
    if not t.evidence:
        problems.append("нет evidence: приём без измеренного доказательства — это мнение")
    if t.status == "implemented" and not t.implemented_by:
        problems.append("status: implemented, но не сказано, чем именно (implemented_by)")
    return problems


def load_all(base: Path | None = None) -> list[Technique]:
    """Все приёмы из library/<decision>/<id>.yaml."""
    root = base or library_dir()
    out: list[Technique] = []
    if not root.is_dir():
        return out
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        for path in sorted(folder.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                log.warning("приём %s не читается: %s", path.name, exc)
                continue
            data.setdefault("id", path.stem)
            data.setdefault("decision", folder.name)
            try:
                t = Technique.model_validate(data)
            except Exception as exc:  # noqa: BLE001 — битый файл не должен ронять прогон
                log.warning("приём %s не разобран: %s", path.name, exc)
                continue
            t._path = path
            out.append(t)
    return out


def for_decision(
    decision: str, ctx: dict[str, Any] | None = None, base: Path | None = None
) -> list[tuple[Technique, Verdict, list[str]]]:
    """Что можно применить в этой точке. Подходящие идут первыми."""
    ctx = ctx or {}
    rows = [(t, *t.matches(ctx)) for t in load_all(base) if t.decision == decision]
    order = {"подходит": 0, "не проверено": 1, "не подходит": 2}
    return sorted(rows, key=lambda r: (order[r[1]], r[0].status != "implemented", r[0].id))


def brief(
    decision: str,
    ctx: dict[str, Any] | None = None,
    base: Path | None = None,
    *,
    implemented_only: bool = False,
) -> str:
    """Короткий текст для промпта стадии и для сообщения на воротах.

    Не подходящие приёмы сюда не идут: задача — сузить выбор, а не пересказать
    библиотеку. `implemented_only` — для промпта модели (см. `for_stage`)."""
    rows = [r for r in for_decision(decision, ctx, base) if r[1] != "не подходит"]
    if implemented_only:
        rows = [r for r in rows if r[0].status == "implemented"]
    return _brief_text(decision, rows)


def _brief_text(decision: str, rows: list[tuple[Technique, Verdict, list[str]]]) -> str:
    if not rows:
        return ""
    lines = [f"Приёмы из библиотеки — {DECISIONS.get(decision, decision)}:"]
    for t, _verdict, unknown in rows:
        mark = "" if t.status == "implemented" else " [не реализовано]"
        tail = f" (условия не проверены: {', '.join(unknown)})" if unknown else ""
        lines.append(f"- {t.title}{mark}: {t.summary}{tail}")
        if t.params:
            lines.append(f"  параметры: {t.params}")
        if t.implemented_by:
            lines.append(f"  исполнение: {t.implemented_by}")
    return "\n".join(lines)


@dataclass(frozen=True)
class StageBrief:
    """Что стадия положила в промпт: текст и id приёмов по точкам. Второе идёт
    в сообщение стадии — человек должен видеть, что модели предложили, иначе
    «библиотека в промпте» нельзя отличить от «библиотеки нет»."""

    text: str
    offered: dict[str, list[str]]

    def describe(self) -> str:
        total = sum(len(ids) for ids in self.offered.values())
        parts = ", ".join(f"{k} {len(v)}" for k, v in self.offered.items() if v)
        return f"приёмов из библиотеки: {total}" + (f" ({parts})" if parts else "")


def for_stage(
    decisions: Sequence[str], ctx: dict[str, Any], base: Path | None = None
) -> StageBrief:
    """Бриф для промпта стадии по её точкам решения (`Stage.decisions`).

    Только `implemented`: `recorded` — то, чего рендер не умеет, и модели такое
    предлагать нельзя — она изобразит приём тем, что есть (ловушка
    breath_music_lift: подъём музыки в каждую паузу вместо трёх-четырёх точек).
    Для человека статус «видели, не умеем» по-прежнему виден в `lib list`."""
    texts: list[str] = []
    offered: dict[str, list[str]] = {}
    for decision in decisions:
        rows = [
            r
            for r in for_decision(decision, ctx, base)
            if r[1] != "не подходит" and r[0].status == "implemented"
        ]
        offered[decision] = [t.id for t, _verdict, _unknown in rows]
        text = _brief_text(decision, rows)
        if text:
            texts.append(text)
    return StageBrief(text="\n\n".join(texts), offered=offered)


def write_index(base: Path | None = None) -> Path:
    """library/index.md — оглавление: что умеем и что только видели."""
    root = base or library_dir()
    root.mkdir(parents=True, exist_ok=True)
    items = load_all(root)
    by_decision: dict[str, list[Technique]] = {}
    for t in items:
        by_decision.setdefault(t.decision, []).append(t)

    done = sum(1 for t in items if t.status == "implemented")
    lines = [
        "# Библиотека приёмов",
        "",
        "Генерируется `videoforge lib index`, руками не править.",
        "",
        f"Приёмов: {len(items)} — умеем {done}, видели но не умеем {len(items) - done}.",
        "",
    ]
    for key, title in DECISIONS.items():
        group = sorted(by_decision.get(key, []), key=lambda t: (t.status != "implemented", t.id))
        lines.append(f"## {key} — {title}")
        if not group:
            lines += ["", "_пусто_", ""]
            continue
        lines.append("")
        for t in group:
            mark = "✔" if t.status == "implemented" else "?"
            cond = "всегда" if t.always and not t.applies_when else _cond_text(t)
            lines.append(f"- {mark} **{t.title}** (`{t.id}`) — {t.summary} · _когда:_ {cond}")
        lines.append("")
    unknown = sorted(set(by_decision) - set(DECISIONS))
    if unknown:
        lines += [f"## вне словаря точек: {', '.join(unknown)}", ""]

    path = root / "index.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _cond_text(t: Technique) -> str:
    parts = [f"{k}={_fmt(v)}" for k, v in t.applies_when.items()]
    parts += [f"НЕ {k}={_fmt(v)}" for k, v in t.never_when.items()]
    return ", ".join(parts) or "всегда"


def _fmt(v: Any) -> str:
    return "/".join(str(x) for x in v) if isinstance(v, list) else str(v)


# ---------------------------------------------------------------- примеры

# расширение → вид примера; всё остальное примером не считается
EXAMPLE_KINDS: dict[str, str] = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".mp4": "video",
    ".webm": "video",
    ".wav": "audio",
    ".mp3": "audio",
    ".m4a": "audio",
}


@dataclass(frozen=True)
class Example:
    """Пример приёма «было → стало»: `library/<decision>/<id>/before.*` и
    `after.*`. Глаз выбирает приём по картинке, а не по параметрам, поэтому
    пример — часть приёма, а не украшение. `after` обязателен, `before` — если
    есть с чем сравнить (у приёма цветокоррекции есть, у «дать треку
    закончиться» нет)."""

    after: Path
    before: Path | None
    kind: str  # image | video | audio


def example_dir(t: Technique) -> Path:
    return t._path.parent / t.id


def _named(folder: Path, stem: str) -> Path | None:
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.stem == stem and path.suffix.lower() in EXAMPLE_KINDS:
            return path
    return None


def find_example(t: Technique) -> Example | None:
    folder = example_dir(t)
    if not folder.is_dir():
        return None
    after = _named(folder, "after")
    if after is None:
        return None
    return Example(
        after=after, before=_named(folder, "before"), kind=EXAMPLE_KINDS[after.suffix.lower()]
    )


def add_example(t: Technique, after: Path, before: Path | None = None) -> Example:
    """Скопировать файлы примера в папку приёма под именами before/after.

    Пример у приёма один: старые before.*/after.* заменяются. Вид (картинка,
    видео, звук) определяется расширением, чужие расширения — ошибка здесь, а не
    пустая рамка в пульте."""
    for src in (after, before):
        if src is None:
            continue
        if not src.is_file():
            raise FileNotFoundError(f"файла нет: {src}")
        if src.suffix.lower() not in EXAMPLE_KINDS:
            raise ValueError(
                f"{src.name}: расширение не годится для примера; можно {', '.join(EXAMPLE_KINDS)}"
            )
    folder = example_dir(t)
    folder.mkdir(parents=True, exist_ok=True)
    for stem, src in (("after", after), ("before", before)):
        if src is None:
            continue
        old = _named(folder, stem)
        if old is not None:
            old.unlink()
        shutil.copy2(src, folder / f"{stem}{src.suffix.lower()}")
    ex = find_example(t)
    assert ex is not None  # after только что скопирован
    return ex
