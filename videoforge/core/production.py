"""Выход продакшена: сколько попыток стоит принятый кадр или клип.

Полигон (`videoforge bench`) меняет умолчания — граф, веса, сиды, — а стало ли
от этого лучше в живых эпизодах, до 14.09.2026 было нечем измерить
(`features/production_yield`). Здесь это считается по файлам эпизодов, без
единого вызова: попытки на принятый актив по стадии, провайдеру, формату и
проекту, доля принятых с первой попытки, отвергнутые попытки, деньги за них и
причины отказа. Команда — `videoforge yield`; модуль зовётся не `yield`,
потому что это слово в Python зарезервировано и модулем быть не может.

Откуда что берётся:

- `Scene.attempts[kind]` — счётчик генераций, `Scene.cost_usd[kind]` — сумма
  за все, `Scene.provenance[kind]` — чем сделан актив.
- `Scene.attempt_log[kind]` (с 14.09.2026, `features/image_best_attempt`) —
  каждая попытка с вердиктом, замечаниями и ценой вместе с проверкой. Только
  у него есть ПРИЧИНЫ отказа; у старых прогонов они «не сохранены», и это
  говорится числом, а не молчанием.
- `Manifest.ledger` — журнал расходов: строка на генерацию с ценой и номером
  попытки (`image v3`). Он помнит и то, что раскадровка забыла: на ep002 у
  сцены s03 счётчик говорит 2, а журнал — 11 генераций на $0.0106 (счётчик
  сбрасывали руками, деньги копились). Поэтому попыток у сцены — БОЛЬШЕЕ из
  счётчика, журнала попыток и журнала расходов: заплачено за одиннадцать,
  принят один, и выход — одиннадцать к одному.

Провайдер — из `provenance`, а не из `manifest.providers`: манифест ep001 и
ep002 называет gemini и fal, а кадры сделаны comfy (запись переписывает
только прогон, который кого-то звал, и у эпизода, собранного после смены
переменных окружения, она отстаёт). Манифест — последний запасной вариант.

Деньги за отвергнутые попытки: по журналу попыток, если он полный; иначе по
журналу расходов (цена каждой генерации известна, принятая — по номеру в
имени файла актива); и только без обоих — оценка по средней цене попытки,
помеченная как оценка (`estimated`). Оценка врёт в обе стороны: упавшая
попытка стоит ноль, а первая после подъёма карты — втрое дороже.

Не считаются: сцены на заглушке (`mock` — demo и тесты на заглушках дают
единицу на всём и красят среднее) и сцены-отрезки нарезок (`Scene.source`:
отрезок не проверяется, второй `cut` — это докачка, а не отказ). Пропущенное
называется числом, чтобы «строк нет» отличалось от «нечего считать».
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Any

from videoforge.core.manifest import CostEntry, Manifest, load_manifest, manifest_path
from videoforge.core.project import list_episodes, list_projects
from videoforge.core.storyboard import Attempt, Scene, Storyboard, load_storyboard
from videoforge.paths import episode_dir

# виды активов, у которых есть попытки с проверкой; озвучка тоже считает
# попытки, но её не проверяют — отвергать нечего
KINDS: tuple[str, ...] = ("image", "video")
# стадия, чьи строки журнала расходов относятся к виду актива
STAGE_OF = {"image": "images", "video": "video"}
MOCK = "mock"
# причина у попытки, которая проверку прошла, но в активе не осталась:
# стадию перезапустили с --force или человек взял другую руками
REPLACED = "прошла, но заменена повтором"
# отвергнута, а замечаний контролёр не оставил — бывает, когда ответ модели
# «не прошло» пришёл без списка
SILENT = "отвергнута без замечаний"
TOP_REASONS = 3

# номер попытки в имени актива: `02_images/s03_v02.png` → 2
_VERSION = re.compile(r"_v(\d+)\.[A-Za-z0-9]+$")
# строка журнала расходов за генерацию (стадии пишут `image v3`, `video v1`);
# проверка кадра пишется отдельно (`image check`) и здесь не генерация
_NOTE = {kind: re.compile(rf"^{kind} v(\d+)$") for kind in KINDS}


@dataclass(frozen=True)
class EpisodeFiles:
    """Файлы одного эпизода — вход счётчика. Манифеста может не быть: тогда
    журнала расходов нет, и деньги считаются по раскадровке."""

    project_id: str
    episode_id: str
    storyboard: Storyboard
    manifest: Manifest | None = None

    @property
    def ref(self) -> str:
        return f"{self.project_id}/{self.episode_id}"


@dataclass
class Row:
    """Строка сводки: один вид актива у одного провайдера в одном формате
    одного проекта. Всё, что выводится делением, — свойства: сырые суммы
    складываются по сценам, а деление «попыток на принятый» по строкам не
    складывается."""

    kind: str
    provider: str
    aspect_ratio: str
    project: str
    episodes: set[str] = field(default_factory=set)
    scenes: int = 0  # сцен, у которых были попытки
    accepted: int = 0  # сцен с принятым активом (pass или skipped)
    # из принятых — без проверки (`skipped`: VF_VISION_CHECK=off или отказ
    # контролёра). «100% с первой» у ep001 — ровно это: кадры никто не смотрел,
    # и как сигнал качества доля не читается
    unchecked: int = 0
    attempts: int = 0  # генераций всего, включая у сцен без принятого
    # попыток у каждой принятой сцены — из них медиана и доля «с первой»
    per_accepted: list[int] = field(default_factory=list)
    rejected_usd: float = 0.0
    estimated: int = 0  # сцен, где деньги за отвергнутые — оценка, а не запись
    reasons: Counter[str] = field(default_factory=Counter)
    unexplained: int = 0  # отвергнутых попыток, причины которых не сохранены

    @property
    def rejected(self) -> int:
        """Каждая принятая сцена приняла ровно одну попытку, остальное отвергнуто."""
        return self.attempts - self.accepted

    @property
    def mean(self) -> float | None:
        """Попыток на принятый актив: все генерации строки на все принятые."""
        return round(self.attempts / self.accepted, 2) if self.accepted else None

    @property
    def median(self) -> float | None:
        return float(median(self.per_accepted)) if self.per_accepted else None

    @property
    def first_try_share(self) -> float | None:
        if not self.per_accepted:
            return None
        return round(sum(1 for n in self.per_accepted if n == 1) / len(self.per_accepted), 3)

    def top_reasons(self, limit: int = TOP_REASONS) -> list[tuple[str, int]]:
        return self.reasons.most_common(limit)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "provider": self.provider,
            "aspect_ratio": self.aspect_ratio,
            "project": self.project,
            "episodes": sorted(self.episodes),
            "scenes": self.scenes,
            "accepted": self.accepted,
            "accepted_unchecked": self.unchecked,
            "attempts": self.attempts,
            "attempts_per_accepted": self.mean,
            "attempts_per_accepted_median": self.median,
            "first_try_share": self.first_try_share,
            "rejected": self.rejected,
            "rejected_usd": round(self.rejected_usd, 6),
            "rejected_usd_estimated": self.estimated > 0,
            "reasons": [{"issue": text, "count": n} for text, n in self.top_reasons()],
            "reasons_unknown": self.unexplained,
        }


@dataclass
class Yield:
    """Сводка плюс то, что в неё не вошло, — чтобы пустая таблица объяснялась."""

    rows: list[Row]
    mock_scenes: int = 0  # сцен на заглушке — не считаются
    cut_scenes: int = 0  # сцен-отрезков нарезок — не считаются
    skipped_episodes: int = 0  # эпизодов раньше `since`

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": [r.as_dict() for r in self.rows],
            "skipped": {
                "mock_scenes": self.mock_scenes,
                "cut_scenes": self.cut_scenes,
                "episodes_before_since": self.skipped_episodes,
            },
        }


@dataclass(frozen=True)
class SceneYield:
    """Что одна сцена вносит в строку: провайдер и счёт по одному виду актива."""

    provider: str
    attempts: int
    accepted: bool
    checked: bool  # принят по вердикту контролёра, а не потому, что проверки не было
    rejected_usd: float
    estimated: bool
    reasons: list[str]
    unexplained: int


def _clean(text: str) -> str:
    """Замечание контролёра как ключ счётчика: лишние пробелы и точка в конце
    делают из одной причины две."""
    return " ".join(text.split()).rstrip(".")


def _version(path: str | None) -> int | None:
    m = _VERSION.search(path or "")
    return int(m.group(1)) if m else None


def _note_version(entry: CostEntry, kind: str) -> int | None:
    m = _NOTE[kind].match(entry.note)
    return int(m.group(1)) if m else None


def ledger_index(manifest: Manifest | None) -> dict[tuple[str, str], list[CostEntry]]:
    """Генерации из журнала расходов по (вид, сцена), в порядке записи.

    Только строки за генерацию: проверка кадра пишется своей строкой
    (`image check`) и попыткой не является. Бесплатные генерации в журнал не
    попадают (`Manifest.add_cost` пропускает ноль) — поэтому счётчик сцены
    остаётся вторым источником числа попыток."""
    index: dict[tuple[str, str], list[CostEntry]] = {}
    if manifest is None:
        return index
    for entry in manifest.ledger:
        for kind, stage in STAGE_OF.items():
            if entry.stage == stage and entry.scene_id and _NOTE[kind].match(entry.note):
                index.setdefault((kind, entry.scene_id), []).append(entry)
    return index


def _kept_index(log: list[Attempt], kept_path: str | None) -> int | None:
    """Какая запись журнала попыток — принятый актив: по пути, и последняя из
    совпавших (после сброса счётчика путь `_v01` встречается дважды)."""
    if not kept_path:
        return None
    hits = [i for i, a in enumerate(log) if a.path == kept_path]
    return hits[-1] if hits else None


def _money(
    scene: Scene,
    kind: str,
    log: list[Attempt],
    entries: list[CostEntry],
    attempts: int,
    accepted: bool,
) -> tuple[float, bool]:
    """Деньги за отвергнутые попытки и оценка ли это.

    Журнал попыток — если он покрывает все попытки (у него и проверка в цене);
    иначе журнал расходов (у каждой генерации своя цена, принятая — по
    номеру из имени файла, последняя из одноимённых); и только без обоих —
    средняя цена попытки, то есть оценка."""
    kept_path = getattr(scene.assets, kind, None)
    if log and len(log) >= attempts:
        total = sum(a.cost_usd for a in log)
        kept = _kept_index(log, kept_path)
        kept_cost = log[kept].cost_usd if accepted and kept is not None else 0.0
        return round(total - kept_cost, 6), False
    if entries:
        total = sum(e.usd for e in entries)
        kept_cost = 0.0
        if accepted:
            n = _version(kept_path)
            same = [e for e in entries if n is not None and _note_version(e, kind) == n]
            # без номера в имени (клип нарезки, старый актив) принята последняя
            kept_cost = (same or entries)[-1].usd
        return round(total - kept_cost, 6), False
    total = scene.cost_usd.get(kind, 0.0)
    if attempts == 0 or total == 0.0:
        return 0.0, False
    rejected = attempts - (1 if accepted else 0)
    return round(total * rejected / attempts, 6), rejected > 0


def _reasons(log: list[Attempt], kept_path: str | None, accepted: bool) -> list[str]:
    """Причины отказа по журналу попыток: замечания каждой отвергнутой.
    Принятая попытка причин не имеет; у непринятой сцены отвергнуты все."""
    kept = _kept_index(log, kept_path) if accepted else None
    out: list[str] = []
    for i, attempt in enumerate(log):
        if i == kept:
            continue
        if attempt.issues:
            out.extend(_clean(x) for x in attempt.issues if x.strip())
        elif attempt.passed:
            out.append(REPLACED)
        else:
            out.append(SILENT)
    return out


def scene_yield(
    scene: Scene, kind: str, entries: list[CostEntry], fallback_provider: str = ""
) -> SceneYield | None:
    """Счёт одной сцены по одному виду актива; None — попыток не было."""
    log = list(scene.attempt_log.get(kind, []))
    attempts = max(scene.attempts.get(kind, 0), len(log), len(entries))
    if attempts == 0:
        return None
    provider = (
        scene.provenance.get(kind)
        or (log[-1].provider if log else "")
        or (entries[-1].provider if entries else "")
        or fallback_provider
        or "?"
    )
    path = getattr(scene.assets, kind, None)
    status = getattr(scene.checks, kind, "pending")
    accepted = bool(path) and status in ("pass", "skipped")
    usd, estimated = _money(scene, kind, log, entries, attempts, accepted)
    reasons = _reasons(log, path, accepted)
    explained = len(log) - (1 if accepted and _kept_index(log, path) is not None else 0)
    rejected = attempts - (1 if accepted else 0)
    return SceneYield(
        provider=provider,
        attempts=attempts,
        accepted=accepted,
        checked=accepted and status == "pass",
        rejected_usd=usd,
        estimated=estimated,
        reasons=reasons,
        unexplained=max(0, rejected - explained),
    )


def _episode_date(files: EpisodeFiles, kinds: Iterable[str]) -> date | None:
    """Когда эпизод считать: последний запуск стадий этих видов, без записи —
    день создания. У попыток своих дат нет, у эпизода — есть."""
    m = files.manifest
    if m is None:
        return None
    stamps = []
    for kind in kinds:
        rec = m.stages.get(STAGE_OF[kind])
        if rec is not None:
            at = rec.finished_at or rec.started_at
            if at is not None:
                stamps.append(at)
    return (max(stamps) if stamps else m.created_at).date()


def measure(
    episodes: Iterable[EpisodeFiles],
    *,
    kinds: Iterable[str] = KINDS,
    since: date | None = None,
) -> Yield:
    """Сводка по эпизодам. Чистая функция: файлы уже прочитаны."""
    wanted = tuple(kinds)
    unknown = [k for k in wanted if k not in STAGE_OF]
    if unknown:
        raise ValueError(f"вид актива не из {sorted(STAGE_OF)}: {unknown}")
    rows: dict[tuple[str, str, str, str], Row] = {}
    result = Yield(rows=[])
    for files in episodes:
        if since is not None:
            when = _episode_date(files, wanted)
            if when is not None and when < since:
                result.skipped_episodes += 1
                continue
        index = ledger_index(files.manifest)
        fallback = files.manifest.providers if files.manifest else {}
        sb = files.storyboard
        for scene in sb.scenes:
            if scene.source is not None:
                result.cut_scenes += 1
                continue
            for kind in wanted:
                entries = index.get((kind, scene.id), [])
                one = scene_yield(scene, kind, entries, fallback.get(kind, ""))
                if one is None:
                    continue
                if one.provider == MOCK:
                    result.mock_scenes += 1
                    continue
                key = (kind, one.provider, sb.aspect_ratio, files.project_id)
                row = rows.get(key)
                if row is None:
                    row = rows[key] = Row(kind, one.provider, sb.aspect_ratio, files.project_id)
                row.episodes.add(files.episode_id)
                row.scenes += 1
                row.attempts += one.attempts
                if one.accepted:
                    row.accepted += 1
                    row.unchecked += int(not one.checked)
                    row.per_accepted.append(one.attempts)
                row.rejected_usd = round(row.rejected_usd + one.rejected_usd, 6)
                row.estimated += int(one.estimated)
                row.reasons.update(one.reasons)
                row.unexplained += one.unexplained
    result.rows = sorted(
        rows.values(), key=lambda r: (r.kind, r.project, r.provider, r.aspect_ratio)
    )
    return result


def load_episodes(project_ids: Iterable[str] | None = None) -> list[EpisodeFiles]:
    """Раскадровки и манифесты с диска: названные проекты или все, у кого есть
    `project.yaml` (скрытые тоже — demo отсеется по заглушке, а не по флагу)."""
    out: list[EpisodeFiles] = []
    for project_id in list_projects() if project_ids is None else project_ids:
        for episode_id in list_episodes(project_id):
            edir = episode_dir(project_id, episode_id)
            manifest = load_manifest(edir) if manifest_path(edir).exists() else None
            out.append(EpisodeFiles(project_id, episode_id, load_storyboard(edir), manifest))
    return out
