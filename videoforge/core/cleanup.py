"""Уборка промежуточных файлов эпизода.

Эпизод после прогона весит сотни мегабайт, а нужны из них единицы: у тестового
`t04` — 1.4 ГБ, из которых 51 МБ отрезки скрипта сборки, 437 МБ нормализованные
копии, 849 МБ скачанные исходники, и всё это восстанавливается. Команда
`videoforge episode cleanup` сносит лишнее — но только то, что вернётся, и
только после того, как понятно, откуда.

**Четыре класса, и класс — это ответ «что будет, если стереть».**

- `junk` — вернётся пересборкой за секунды и не стоит ничего: клипы сцен после
  наложения звука и отрезки переходов (`05_assemble/<сцена>.mp4`, `.in/.out/
  .cut.mp4`), склейка до финального прохода (`scenes.mp4`), кадры проверок
  (`<клип>_frames/`, `05_assemble/qa/`), кэши (`*.peaks.json`, `*.motion.json`),
  обложка и кадры планов, которые пульт снимает при первом показе, макеты
  `preview_*`, отрезки скрипта сборки (`seg/`, `tmp/`).
- `regenerable` — воспроизводимо из раскадровки, но стоит денег или минут:
  попытки кадров и клипов, НЕ выбранные в `assets` (`<сцена>_vNN.*`), и
  нормализованные копии исходников у роликов, собранных скриптом (`norm/`).
- `downloaded` — скачанное: докачанный кусок исходника нарезки (`_src.mp4`,
  недокачанные `_src_v`/`_src_a`), точный отрезок до кадрирования, `src/` у
  роликов мимо конвейера. Удаляется ТОЛЬКО с подтверждённой ссылкой: у сцены
  `Scene.source` → `source.json` источника с `url` (или живым локальным файлом),
  у ролика мимо конвейера — `link` в паспорте `materials.json`. Иначе файл
  остаётся, и уборка называет причину: стереть то, чего не вернуть, — не уборка.
- `keep` — всё остальное, включая незнакомое. Контракт эпизода (раскадровка,
  манифест), готовый ролик, субтитры, планы, QA, упаковка, сценарий, текущие
  активы сцен, промпты, паспорт. Незнакомый файл — тоже `keep`: у роликов,
  собранных руками, рядом лежат `build.py`, `audio.wav`, `masks/`, и угадывать,
  что из этого лишнее, нельзя.

Классы построены по реальным папкам (podcast_clips, doc_channel/ep001,
projects/tests), а не по замыслу стадий: в `05_assemble` у doc_channel лежит
`final.before_model_effects.mp4`, которого не пишет никто, — он `keep`.

**Что уборка не делает.** Не трогает эпизод под замком (`episode_lock`, как
пересборка: удалять клипы сцен, пока их читает ffmpeg, нельзя). Не заходит в
`projects/_bench` и `research/` — там дубли замеров и артефакты анализов, у них
своя логика хранения. Не удаляет папки, только файлы (опустевшие папки
подчищает). След остаётся в манифесте: `cleanup: {at, classes, freed_mb}`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from videoforge.core.manifest import CleanupRecord, load_manifest, now, save_manifest
from videoforge.core.materials import load_materials
from videoforge.core.storyboard import STORYBOARD_FILE, Scene, Storyboard, load_storyboard
from videoforge.paths import EPISODES_DIR, bench_dir, research_dir

Cls = Literal["junk", "regenerable", "downloaded", "keep"]
CLASSES: tuple[Cls, ...] = ("junk", "regenerable", "downloaded", "keep")
DELETABLE: tuple[Cls, ...] = ("junk", "regenerable", "downloaded")
DEFAULT_CLASSES: tuple[Cls, ...] = ("junk",)

CLASS_TITLES: dict[str, str] = {
    "junk": "мусор: вернётся пересборкой за секунды",
    "regenerable": "воспроизводимо из раскадровки, но стоит денег или минут",
    "downloaded": "скачанное: удаляется только с подтверждённой ссылкой",
    "keep": "остаётся",
}

MB = 1024 * 1024

# Папки стадий — по именам, а не по номерам: у ролика мимо конвейера их нет
IMAGES_DIR = "02_images"
VIDEO_DIR = "03_video"
ASSEMBLE_DIR = "05_assemble"
LOCK_FILE = ".run.lock"  # то же имя, что у core.runner; сюда не импортируется (см. cleanup)

# Папки роликов, собранных скриптом (projects/tests/t04): имена из его build.py
SCRIPT_JUNK_DIRS = ("seg", "tmp")
SCRIPT_NORM_DIR = "norm"
SCRIPT_SRC_DIR = "src"

# кэши, которые считаются из файла рядом за доли секунды (`core/waveform`,
# `core/motion`) и без него бессмысленны; `.tmp` — недописанный write_model
CACHE_SUFFIXES = (".peaks.json", ".motion.json", ".tmp")
ATTEMPT_RE = re.compile(r"^(?P<sid>.+)_v(?P<n>\d{2})\.(png|jpg|jpeg|webp|mp4|webm)$")
TRANSITION_RE = re.compile(r"^(?P<sid>.+)\.(in|out|cut)\.mp4$")
# докачанный кусок и его недокачанные половины (`providers.youtube.download_section`
# качает картинку и звук отдельно: `_src_v.mp4`, `_src_a.m4a`)
SOURCE_PIECE_RE = re.compile(r"^(?P<sid>.+?)_src(_v|_a)?\.(?!json$)[^.]+$")


class CleanupRefused(RuntimeError):
    """Здесь убирать нельзя: не эпизод, полигон замеров или анализ."""


@dataclass(frozen=True)
class Entry:
    """Один файл эпизода: путь от папки эпизода, размер, класс и почему."""

    path: str
    size: int
    cls: Cls
    why: str
    # только у `downloaded`: есть ли откуда вернуть, и откуда
    restorable: bool = True
    restore: str = ""

    @property
    def mb(self) -> float:
        return round(self.size / MB, 1)


@dataclass
class Result:
    """Итог уборки или сухого прогона.

    `chosen` — что удалится по выбранным классам (у `downloaded` — только с
    подтверждённой ссылкой), `blocked` — что из выбранного НЕ удалится и почему,
    `deleted` — что реально стёрто (пусто у сухого прогона)."""

    episode_dir: Path
    classes: tuple[str, ...]
    entries: list[Entry]
    chosen: list[Entry] = field(default_factory=list)
    blocked: list[Entry] = field(default_factory=list)
    deleted: list[Entry] = field(default_factory=list)
    applied: bool = False

    def by_class(self) -> dict[str, list[Entry]]:
        out: dict[str, list[Entry]] = {c: [] for c in CLASSES}
        for e in self.entries:
            out[e.cls].append(e)
        return out

    @property
    def freed_bytes(self) -> int:
        return sum(e.size for e in self.deleted)

    @property
    def would_free_bytes(self) -> int:
        return sum(e.size for e in self.chosen)


# ---------------------------------------------------------------- контекст


@dataclass
class _Context:
    edir: Path
    storyboard: Storyboard
    scene_ids: set[str]
    current: set[str]  # все пути `assets.*` сцен — текущие активы
    sources: dict[str, Scene]  # сцены-нарезки по id
    materials: dict[str, tuple[str, str, str]]  # путь → (роль, ссылка, название)


def _context(edir: Path) -> _Context:
    sb = load_storyboard(edir)
    current: set[str] = set()
    sources: dict[str, Scene] = {}
    for scene in sb.scenes:
        for value in scene.assets.model_dump().values():
            if isinstance(value, str) and value:
                current.add(PurePosixPath(value).as_posix())
            elif isinstance(value, list):
                current.update(PurePosixPath(v).as_posix() for v in value if v)
        if scene.source is not None:
            sources[scene.id] = scene
    materials: dict[str, tuple[str, str, str]] = {}
    passport = load_materials(edir)
    if passport is not None:
        for item in passport.items:
            if item.path:
                key = PurePosixPath(item.path).as_posix().rstrip("/")
                materials[key] = (item.role, item.link, item.title)
    return _Context(edir, sb, {s.id for s in sb.scenes}, current, sources, materials)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _source_restore(ctx: _Context, scene: Scene) -> tuple[bool, str]:
    """Откуда вернуть скачанный кусок сцены-нарезки.

    `Scene.source.path` — путь от папки ПРОЕКТА (как читает его стадия cut), там
    `source.json` с `url` или локальным `file`. Запасной ответ — паспорт самой
    нарезки `03_video/<сцена>_src.json`: стадия записала в него, откуда качала.
    Читается json, а не модели `clips/` и `stages/`: ядро выше себя не смотрит."""
    assert scene.source is not None
    project_root = ctx.edir.parent.parent  # projects/<p>/episodes/<e> → projects/<p>
    candidates = [
        project_root / scene.source.path / "source.json",
        ctx.edir / VIDEO_DIR / f"{scene.id}_src.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        info = _read_json(path)
        url = str(info.get("url") or "")
        if url:
            return True, url
        local = str(info.get("file") or "")
        if local and Path(local).exists():
            return True, local
    return False, "у источника нет ни ссылки, ни живого локального файла"


def _material_at(ctx: _Context, rel: str) -> tuple[str, str, str] | None:
    """Запись паспорта, которой принадлежит файл: точное совпадение или папка."""
    if rel in ctx.materials:
        return ctx.materials[rel]
    for key, item in ctx.materials.items():
        if rel.startswith(key + "/"):
            return item
    return None


# ---------------------------------------------------------------- классификация


def _classify(ctx: _Context, rel: PurePosixPath, size: int) -> Entry:
    """Класс одного файла. Порядок правил — от самого узкого признака к папке;
    всё, что не подошло ни под одно, — `keep`."""
    path = rel.as_posix()
    name = rel.name
    top = rel.parts[0] if len(rel.parts) > 1 else ""
    parent = rel.parts[-2] if len(rel.parts) > 1 else ""

    def keep(why: str) -> Entry:
        return Entry(path, size, "keep", why)

    def junk(why: str) -> Entry:
        return Entry(path, size, "junk", why)

    # текущие активы — раньше всего: `assets.clip` бывает `.out.mp4`, и по имени
    # он выглядел бы отрезком перехода, но именно этот файл сборка отдала сцене
    if path in ctx.current and top != ASSEMBLE_DIR:
        return keep("текущий актив сцены")

    if name.endswith(CACHE_SUFFIXES):
        return junk("кэш: считается из файла рядом за доли секунды")
    if any(part.endswith("_frames") for part in rel.parts[:-1]):
        return junk("кадры проверки клипа: вырезаны из него, вердикт записан в раскадровке")

    if top == ASSEMBLE_DIR:
        return _classify_assemble(ctx, rel, size)
    if top in (IMAGES_DIR, VIDEO_DIR):
        return _classify_generated(ctx, rel, size)

    # ролик, собранный скриптом мимо конвейера (projects/tests/t04)
    if top in SCRIPT_JUNK_DIRS:
        return junk("отрезок скрипта сборки до склейки: скрипт режет заново")
    if top == SCRIPT_NORM_DIR:
        return Entry(
            path,
            size,
            "regenerable",
            "нормализованная копия исходника: скрипт сборки делает её из src/ заново "
            "(минуты ffmpeg на 4K)",
        )
    item = _material_at(ctx, path)
    if item is not None:
        role, link, title = item
        if role not in ("clip", "source"):
            return keep(f"материал из паспорта ({role}): {title or path}")
        if link:
            return Entry(path, size, "downloaded", f"скачанный материал: {title}", True, link)
        return Entry(path, size, "downloaded", f"в паспорте нет ссылки: {title or path}", False, "")
    if top == SCRIPT_SRC_DIR:
        return Entry(
            path,
            size,
            "downloaded",
            "скачанный исходник, которого нет в паспорте materials.json — восстановить неоткуда",
            False,
            "",
        )
    if rel.parts == (STORYBOARD_FILE,) or rel.parts == ("manifest.json",):
        return keep("контракт эпизода")
    if parent == "thumbs":
        return keep("кадры сцены для листа раскадровки: килобайты, а лист без них слеп")
    return keep("незнакомый файл: не трогаем")


def _classify_assemble(ctx: _Context, rel: PurePosixPath, size: int) -> Entry:
    """`05_assemble/`: сборка переписывает сюда всё при каждой пересборке, кроме
    того, что читают дальше — готового ролика, субтитров, замеров, QA, обложки."""
    path = rel.as_posix()
    name = rel.name
    parent = rel.parts[-2] if len(rel.parts) > 1 else ""

    def junk(why: str) -> Entry:
        return Entry(path, size, "junk", why)

    if len(rel.parts) == 2:
        if name in ("scenes.mp4", "scenes.slow.mp4", "scenes.txt"):
            return junk("склейка до финального прохода: сборка делает заново")
        if TRANSITION_RE.match(name):
            return junk("отрезок перехода или сборка отрезков: сборка делает заново")
        stem = name[: -len(".mp4")] if name.endswith(".mp4") else ""
        if stem and stem in ctx.scene_ids:
            return junk("клип сцены с наложенным звуком: сборка делает заново")
        if name == "final.poster.jpg":
            return junk("обложка для пульта: снимается заново при первом показе")
        if name.startswith("preview_"):
            return junk("макет зон интерфейса площадки: `videoforge preview` делает заново")
        if name == "shots.json":
            return junk("границы планов для пульта: снимаются заново при первом показе")
    if parent == "qa":
        return junk(
            "кадр QA: вырезан из final.mp4 в секунду, записанную в qa.json; вердикт остаётся"
        )
    if parent == "shots":
        return junk("кадры планов для пульта: снимаются заново при первом показе")
    if parent == "thumbs":
        return Entry(
            path, size, "keep", "кадры сцены для листа раскадровки: килобайты, а лист без них слеп"
        )
    if path in ctx.current:
        return Entry(path, size, "keep", "текущий актив сцены")
    return Entry(path, size, "keep", "результат сборки: ролик, субтитры, замеры, QA, обложка")


def _classify_generated(ctx: _Context, rel: PurePosixPath, size: int) -> Entry:
    """`02_images/`, `03_video/`: попытки генерации и докачанные куски нарезок."""
    path = rel.as_posix()
    name = rel.name
    if len(rel.parts) != 2:
        return Entry(path, size, "keep", "незнакомый файл: не трогаем")

    piece = SOURCE_PIECE_RE.match(name)
    if piece and piece.group("sid") in ctx.sources:
        scene = ctx.sources[piece.group("sid")]
        ok, where = _source_restore(ctx, scene)
        return Entry(
            path,
            size,
            "downloaded",
            "докачанный кусок исходника с запасом: стадия cut качает заново"
            if ok
            else f"докачанный кусок исходника: {where}",
            ok,
            where if ok else "",
        )
    stem = name[: -len(".mp4")] if name.endswith(".mp4") else ""
    if stem in ctx.sources and rel.parts[0] == VIDEO_DIR:
        # точный отрезок до кадрирования: текущий актив — кадрированный клип
        # (`_reframed`), иначе этот файл был бы пойман выше как текущий актив
        scene = ctx.sources[stem]
        ok, where = _source_restore(ctx, scene)
        return Entry(
            path,
            size,
            "downloaded",
            "точный отрезок до кадрирования; текущий актив сцены — кадрированный клип. "
            "Вернуть: `videoforge run <ref> --only cut --force`, потом reframe"
            if ok
            else f"точный отрезок до кадрирования: {where}",
            ok,
            where if ok else "",
        )
    attempt = ATTEMPT_RE.match(name)
    if attempt:
        n = int(attempt.group("n"))
        return Entry(
            path,
            size,
            "regenerable",
            f"попытка {n}, в assets не выбрана: воспроизводима из раскадровки, стоит генерации",
        )
    if name.endswith(".prompt.txt"):
        return Entry(path, size, "keep", "промпт сцены, как ушёл провайдеру")
    return Entry(path, size, "keep", "незнакомый файл: не трогаем")


def guard(edir: Path) -> None:
    """Где убирать нельзя. Полигон замеров и анализы — не эпизоды, у них свои
    файлы и своя ценность; папка без раскадровки — не эпизод вовсе."""
    real = edir.resolve()
    for title, root in (
        ("полигон замеров projects/_bench", bench_dir()),
        ("research/", research_dir()),
    ):
        r = root.resolve()
        if real == r or r in real.parents:
            raise CleanupRefused(f"уборка не заходит в {title}: {edir}")
    if not (edir / STORYBOARD_FILE).exists() or edir.parent.name != EPISODES_DIR:
        raise CleanupRefused(f"не эпизод (нет {STORYBOARD_FILE} в episodes/<эпизод>): {edir}")


def classify(edir: Path) -> list[Entry]:
    """Все файлы эпизода с классами. Замок прогона в список не входит — это не
    файл эпизода, а его состояние."""
    guard(edir)
    ctx = _context(edir)
    entries: list[Entry] = []
    for path in sorted(edir.rglob("*")):
        if not path.is_file() or path.name == LOCK_FILE:
            continue
        rel = PurePosixPath(path.relative_to(edir).as_posix())
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entries.append(_classify(ctx, rel, size))
    return entries


# ---------------------------------------------------------------- уборка


def cleanup(
    edir: Path, classes: tuple[str, ...] | list[str] = DEFAULT_CLASSES, *, apply: bool = False
) -> Result:
    """Сухой прогон (`apply=False`) или удаление выбранных классов.

    `downloaded` без подтверждённой ссылки в `chosen` не попадает — ложится в
    `blocked` с причиной. Удаление идёт под замком эпизода: занятый эпизод —
    `core.runner.Busy`, как у пересборки. След — `manifest.cleanup`."""
    chosen_classes = tuple(dict.fromkeys(classes))  # порядок сохранён, дубли сняты
    unknown = [c for c in chosen_classes if c not in DELETABLE]
    if unknown:
        raise ValueError(f"удалять можно только {', '.join(DELETABLE)}; получено: {unknown}")
    entries = classify(edir)
    chosen = [
        e for e in entries if e.cls in chosen_classes and (e.cls != "downloaded" or e.restorable)
    ]
    blocked = [
        e for e in entries if e.cls == "downloaded" and e.cls in chosen_classes and not e.restorable
    ]
    result = Result(edir, chosen_classes, entries, chosen=chosen, blocked=blocked)
    if not apply:
        return result

    # runner тянет реестр стадий — ядру он нужен только здесь, и только ради замка
    from videoforge.core.runner import episode_lock

    with episode_lock(edir, what="уборка"):
        touched: set[Path] = set()
        for e in chosen:
            path = edir / e.path
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            result.deleted.append(e)
            touched.add(path.parent)
        _prune_empty_dirs(edir, touched)
        manifest = load_manifest(edir)
        manifest.cleanup = CleanupRecord(
            at=now(),
            classes=list(chosen_classes),
            freed_mb=round(result.freed_bytes / MB, 1),
            files=len(result.deleted),
        )
        save_manifest(edir, manifest)
    result.applied = True
    return result


def _prune_empty_dirs(edir: Path, touched: set[Path]) -> None:
    """Опустевшие папки, из которых удаляли, — вверх до папки эпизода, её не трогая."""
    root = edir.resolve()
    for start in sorted(touched, key=lambda p: len(p.parts), reverse=True):
        current = start
        while current.resolve() != root and root in current.resolve().parents:
            try:
                os.rmdir(current)  # непустая — OSError, и на этом подъём кончается
            except OSError:
                break
            current = current.parent


# ---------------------------------------------------------------- представление


def entry_dict(e: Entry) -> dict[str, Any]:
    return {
        "path": e.path,
        "bytes": e.size,
        "mb": e.mb,
        "class": e.cls,
        "why": e.why,
        "restorable": e.restorable,
        "restore": e.restore,
    }


def to_dict(result: Result) -> dict[str, Any]:
    """Словарь для пульта и CLI: классы со счётом и списками, что выбрано,
    что заблокировано, что удалено."""
    groups = result.by_class()
    classes = {
        cls: {
            "title": CLASS_TITLES[cls],
            "files": len(items),
            "bytes": sum(e.size for e in items),
            "mb": round(sum(e.size for e in items) / MB, 1),
            "items": [entry_dict(e) for e in items],
        }
        for cls, items in groups.items()
    }
    total = sum(e.size for e in result.entries)
    return {
        "episode_dir": str(result.episode_dir),
        "total_files": len(result.entries),
        "total_mb": round(total / MB, 1),
        "classes": classes,
        "selected": list(result.classes),
        "chosen": [entry_dict(e) for e in result.chosen],
        "would_free_mb": round(result.would_free_bytes / MB, 1),
        "blocked": [entry_dict(e) for e in result.blocked],
        "applied": result.applied,
        "deleted": [entry_dict(e) for e in result.deleted],
        "freed_mb": round(result.freed_bytes / MB, 1),
    }
