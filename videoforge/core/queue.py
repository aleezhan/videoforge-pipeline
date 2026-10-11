"""Очередь публикаций проекта: в каком порядке готовые ролики уходят на площадки.

Пока роликов пять, порядок держится головой. При тридцати эпизодах человек
перестаёт помнить, что уже уехало, что стоит следующим и почему один из них не
готов. Манифест знает только про свой эпизод; здесь — взгляд поверх проекта.

Своего состояния очередь не хранит, и это главное. Порядок лежит в
`projects/<проект>/queue.yaml` (список id, сверху — следующий), а «уехало /
ждёт ворот / нечего грузить» **выводится** из файлов эпизода: `manifest.publish`,
`06_publish/publish.json`, `05_assemble/final.mp4`. Отметку «опубликовано»
очередь не ставит: её ставит сама публикация в манифест эпизода, и второго
источника правды не появляется.

Файла `queue.yaml` может не быть — тогда порядок это порядок эпизодов на диске.
Эпизод, которого нет в файле, встаёт в конец: забыть дописать id не должно
означать «ролик не выйдет никогда».

**Расписание** живёт в том же файле, блоком `schedule`: слоты дня по местному
времени (`times: ["12:00", "18:00"]`) и `paused`. Задача планировщика Windows
не знает часов — она тикает со своим шагом (`videoforge queue <проект> --tick
--every <минут>`; шаг стоит в самой задаче, поэтому тик приносит его числом и
кладёт в след), а «пора ли» решает `due()` по файлам: последний слот дня прошёл, и
после него ни одна площадка ничего не принимала (`manifest.publish.platforms`,
`in_flight`, упавшая попытка). Так время выхода меняется правкой файла — из
пульта или редактора, — а не перерегистрацией задачи; и два источника правды
(файл и триггер задачи) не заводятся. Сухой прогон отметкой не считается: он
пишет `publish.at`, но ничего не отправляет.

**Слот — не секунда выхода, а начало окна.** Ролик уходит в случайную секунду
окна `[слот, слот + spread_min)`: ровно 12:00:00 каждый день — почерк скрипта,
а площадка такое читает. Секунда выбирается детерминированно из проекта, даты
и слота (`publish_at`), поэтому все тики дня и пульт называют одно и то же
время, а не каждый своё. Разброс по умолчанию `SPREAD_DEFAULT_MIN`; `0` в
файле — выход ровно в слот. Тик, увидевший цель ближе своего шага, ждёт до неё
(это делает CLI, ядро только называет секунду), поэтому разброс имеет смысл
держать не больше шага задачи: что дальше, уедет на следующий тик.

Тик оставляет след — `queue.tick.json` рядом с очередью: когда тикал, какой
слот отработал и почему решил так. Без него план в пульте («13.09 12:00 — h02»)
нельзя отличить от плана, который никто не выполнит, потому что задача снята
или ПК спал. Отработанный слот там же: тик, запустивший публикацию в 12:00,
не должен запускать её второй раз в 12:15, если ролик ещё грузится или
загрузка не удалась — повтор вслепую кладёт дубль, а не чинит ошибку.

**Но отработанным слот считается по результату, а не по запуску.** 13.09.2026
в 18:00 сеть моргала, Claude не достучался до API и упал через 219 с, а слот
уже стоял как отработанный — и до конца дня тики отвечали «уже отработан».
Поэтому: без сети (`is_online`) тик слот не трогает и ждёт; результат запуска
run.ps1 возвращает через `tick_result(code)`, и упавшая попытка (ненулевой
код, или ролик был, а площадка после слота не отвечала) отдаёт слот
следующему тику — до `TICK_MAX_ATTEMPTS` раз, дальше до следующего слота.
Отказ самой площадки (`state: failed` в манифесте) повтором не считается:
она могла принять файл и не ответить, и это по-прежнему смотрит человек.
"""

from __future__ import annotations

import json
import random
import re
import socket
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

from videoforge.core.manifest import load_manifest
from videoforge.core.project import list_episodes, list_projects, load_project
from videoforge.core.storyboard import load_storyboard
from videoforge.paths import (
    ASSEMBLE_DIR,
    FINAL_FILE,
    PUBLISH_DATA_FILE,
    PUBLISH_DIR,
    episode_dir,
    project_dir,
)

QUEUE_FILE = "queue.yaml"
SCHEDULE_KEY = "schedule"
TICK_FILE = "queue.tick.json"
# шаг задачи планировщика по умолчанию, минут: сколько на самом деле стоит в
# задаче, знает только она, поэтому тик приносит своё число с собой
# (`--every`) и кладёт его в след — иначе ядро судит о свежести и о том,
# сколько ждать выхода, по чужой настройке
TICK_EVERY_MIN = 15
# столько пропущенных тиков подряд значат, что планировщик молчит (задача
# снята, ПК спал), и план в пульте — не обещание
TICK_STALE_TICKS = 3
# разброс выхода внутри слота, минут: 12:00 в файле значит «между 12:00 и 12:40»
SPREAD_DEFAULT_MIN = 40
# попыток на слот, когда сеть есть, а публикация не прошла; дальше — до
# следующего слота, и человек читает отчёт тика
TICK_MAX_ATTEMPTS = 3
# кому тик должен дозвониться, чтобы запуск имел смысл: API Claude и YouTube
NETWORK_HOSTS: tuple[str, ...] = ("api.anthropic.com", "www.googleapis.com")
# запас меньше стольких дней — предупреждение: один вечер с одним источником
# даёт 3–4 шортса, то есть пополнять надо начинать за три дня, не за день
RUNWAY_WARN_DAYS = 3

# Состояние ролика в очереди. Порядок важен: так они и показываются человеку.
State = Literal["published", "ready", "waiting", "unfinished"]

STATE_TITLE: dict[str, str] = {
    "published": "уехало",
    "ready": "готово",
    "waiting": "ждёт ворот",
    "unfinished": "не собран",
}


@dataclass(frozen=True)
class QueueItem:
    episode: str
    state: State
    title: str = ""
    note: str = ""  # почему не готов или куда уже уехал

    @property
    def line(self) -> str:
        head = f"{self.episode:<22} {STATE_TITLE[self.state]:<12}"
        tail = self.title or self.note
        if self.title and self.note:
            tail = f"{self.title} · {self.note}"
        return f"{head} {tail}".rstrip()


def queue_path(project_id: str) -> Path:
    return project_dir(project_id) / QUEUE_FILE


def _read_queue_file(project_id: str) -> dict[str, Any]:
    """Файл очереди как словарь; любая беда с ним — пустой словарь.

    Ошибка в файле не должна ронять публикацию: неразобранный YAML — это
    «не задано», а не отказ работать."""
    path = queue_path(project_id)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def load_order(project_id: str) -> list[str]:
    """Порядок выпуска: сначала то, что записано, потом всё остальное."""
    on_disk = list_episodes(project_id)
    try:
        written = [str(e) for e in (_read_queue_file(project_id).get("episodes") or [])]
    except TypeError:  # строка вместо списка id — «порядок не задан»
        written = []
    order = [e for e in written if e in on_disk]
    order += [e for e in on_disk if e not in order]
    return order


def item_state(project_id: str, episode_id: str) -> QueueItem:
    """Состояние одного эпизода — по его же файлам, без своих отметок."""
    edir = episode_dir(project_id, episode_id)
    title = ""
    try:
        sb = load_storyboard(edir)
        title = (sb.publish.title if sb.publish else "") or sb.title
    except (FileNotFoundError, ValueError):
        pass
    try:
        manifest = load_manifest(edir)
    except (FileNotFoundError, ValueError):
        return QueueItem(episode_id, "unfinished", title, "нет манифеста")
    record = manifest.publish
    if record and record.state == "done" and record.platforms:
        where = ", ".join(sorted(record.platforms))
        return QueueItem(episode_id, "published", title, f"на {where}")
    if not (edir / ASSEMBLE_DIR / FINAL_FILE).exists():
        return QueueItem(episode_id, "unfinished", title, "нет собранного ролика")
    if manifest.awaiting_approval:
        # ворота стоят ПЕРЕД упаковкой (qa → gate → publish): собранный ролик без
        # publish.json — это «ждёт ворот», а не «не собран», иначе десять готовых
        # к просмотру шортсов читались как несобранные (14.09.2026)
        return QueueItem(
            episode_id, "waiting", title, f"одобрить стадию {manifest.awaiting_approval}"
        )
    if not (edir / PUBLISH_DIR / PUBLISH_DATA_FILE).exists():
        return QueueItem(
            episode_id, "unfinished", title, "нет упаковки (стадия publish: videoforge run)"
        )
    return QueueItem(episode_id, "ready", title)


def queue(project_id: str) -> list[QueueItem]:
    return [item_state(project_id, e) for e in load_order(project_id)]


def next_ready(project_id: str) -> QueueItem | None:
    """Что уйдёт следующим. Ничего готового — None, и это нормальный ответ."""
    for item in queue(project_id):
        if item.state == "ready":
            return item
    return None


# ---------------------------------------------------------------- расписание

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
# блок `schedule:` целиком — до следующего ключа верхнего уровня или конца файла.
# Комментарии внутри блока идут с отступом и меняются вместе с ним; комментарии
# человека над `episodes:` не трогаются
_SCHEDULE_BLOCK = re.compile(r"^schedule:[^\n]*\n(?:[ \t]+[^\n]*\n)*", re.M)


@dataclass(frozen=True)
class Schedule:
    """Слоты дня по местному времени, отсортированы. Пусто — расписания нет.
    `spread_min` — окно после слота, внутри которого выбирается секунда выхода."""

    times: tuple[str, ...] = ()
    paused: bool = False
    spread_min: int = SPREAD_DEFAULT_MIN

    @property
    def per_day(self) -> int:
        return len(self.times)

    @property
    def active(self) -> bool:
        return bool(self.times) and not self.paused


def parse_times(values: list[Any] | tuple[Any, ...]) -> tuple[str, ...]:
    """«12:00, 18:00» → отсортированные HH:MM без повторов; мусор — ValueError.

    YAML 1.1 читает незакавыченное `12:00` как число 720 (минуты): такое тоже
    принимается — файл правят руками, и кавычки там забудут."""
    out: set[str] = set()
    for raw in values:
        if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw < 24 * 60:
            hh, mm = divmod(raw, 60)
        else:
            m = _TIME_RE.match(str(raw).strip())
            if m is None:
                raise ValueError(f"время слота пишется как HH:MM, а не {raw!r}")
            hh, mm = int(m.group(1)), int(m.group(2))
        out.add(f"{hh:02d}:{mm:02d}")
    return tuple(sorted(out))


def load_schedule(project_id: str) -> Schedule:
    """Расписание из файла очереди; неразобранное — как отсутствующее."""
    raw = _read_queue_file(project_id).get(SCHEDULE_KEY)
    if not isinstance(raw, dict):
        return Schedule()
    values = raw.get("times") or []
    if not isinstance(values, list):
        values = [values]
    try:
        times = parse_times(values)
    except ValueError:
        times = ()
    spread = raw.get("spread_min", SPREAD_DEFAULT_MIN)
    if isinstance(spread, bool) or not isinstance(spread, int) or spread < 0:
        spread = SPREAD_DEFAULT_MIN  # мусор в файле — как не задано
    return Schedule(times=times, paused=bool(raw.get("paused", False)), spread_min=spread)


def render_schedule(schedule: Schedule) -> str:
    times = ", ".join(f'"{t}"' for t in schedule.times)
    paused = "true" if schedule.paused else "false"
    return (
        "schedule:\n"
        "  # Слоты дня по местному времени. Тик планировщика (со своим шагом,\n"
        "  # `videoforge queue <проект> --tick`) смотрит, прошёл ли слот и не уходил\n"
        "  # ли ролик после него; пульт правит это на странице проекта, «Расписание».\n"
        "  # Слот ловит ближайший тик: при редком шаге (с 18.09.2026 — 2 часа от\n"
        "  # полуночи) ставь слоты на часы, когда тик ходит, иначе ролик уйдёт\n"
        "  # позже — на следующем тике\n"
        f"  times: [{times}]\n"
        f"  spread_min: {schedule.spread_min}  # слот — начало окна: ролик уходит в случайную\n"
        "                  # секунду следующих N минут, не ровно в 12:00:00; 0 — ровно в слот\n"
        f"  paused: {paused}  # true — слоты стоят, тик молчит, ничего не выходит само\n"
    )


def save_schedule(project_id: str, schedule: Schedule) -> Path:
    """Записать блок `schedule`, не трогая остального: порядок эпизодов и
    комментарии человека остаются как были. Файла нет — появится только с
    расписанием: порядок при этом по-прежнему «как на диске»."""
    path = queue_path(project_id)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"  # иначе последняя строка блока без перевода строки уцелеет
    block = render_schedule(schedule)
    new, n = _SCHEDULE_BLOCK.subn(lambda _m: block, text, count=1)
    if n == 0:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return path


def scheduled_projects() -> list[str]:
    """Проекты, которые выходят сами: расписание есть и не на паузе.

    Тик планировщика без имени проекта обходит этот список — второй канал
    попадает в обход правкой своего queue.yaml, а не перерегистрацией задачи
    Windows, и второго источника правды («какие проекты тикать») не появляется.
    Полигон (`kind: test`) и скрытый образец (`hidden: true`) не выходят
    никогда, даже с расписанием в файле: их ролики смотрят глазами. Битый
    project.yaml одного проекта не роняет тик соседей — он просто не в списке."""
    out: list[str] = []
    for pid in list_projects():
        try:
            cfg = load_project(pid)
        except (ValueError, yaml.YAMLError):  # ValidationError — тоже ValueError
            continue
        if cfg.kind == "test" or cfg.hidden:
            continue
        if load_schedule(pid).active:
            out.append(pid)
    return out


def local_now() -> datetime:
    """Сейчас с местным поясом: слоты в файле местные, отметки манифеста — UTC."""
    return datetime.now().astimezone()


def _aware(dt: datetime) -> datetime:
    """Отметка манифеста без пояса — это UTC (`core.manifest.now`)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _slot(day: date, hhmm: str, like: datetime) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(hh, mm), tzinfo=like.tzinfo)


def last_slot(schedule: Schedule, now: datetime) -> datetime | None:
    """Последний слот сегодня, который уже наступил. Вчерашние не считаются:
    пропущенный день не нагоняется — у канала узкое место не частота."""
    passed = [s for t in schedule.times if (s := _slot(now.date(), t, now)) <= now]
    return max(passed) if passed else None


def slots_after(schedule: Schedule, now: datetime, count: int) -> list[datetime]:
    """Ближайшие `count` слотов строго после `now`, день за днём."""
    out: list[datetime] = []
    day = now.date()
    while len(out) < count and schedule.times:
        for t in schedule.times:
            s = _slot(day, t, now)
            if s > now:
                out.append(s)
        day += timedelta(days=1)
    return out[:count]


def _spread_for(schedule: Schedule, slot: datetime) -> int:
    """Разброс слота в минутах: не дальше следующего слота дня — окна двух
    слотов не должны накладываться, иначе второй ролик выйдет раньше первого."""
    later = [s for t in schedule.times if (s := _slot(slot.date(), t, slot)) > slot]
    if not later:
        return schedule.spread_min
    gap_min = int((min(later) - slot).total_seconds() // 60) - 1
    return max(0, min(schedule.spread_min, gap_min))


def publish_at(project_id: str, schedule: Schedule, slot: datetime) -> datetime:
    """Секунда выхода для слота: слот плюс случайная доля разброса.

    Случайность детерминированная — из проекта, даты и слота: тик в 12:00, тик
    в 12:15 и пульт обязаны называть одну и ту же секунду, иначе план врёт, а
    тик, ждущий цель, ждёт каждый раз другую."""
    spread = _spread_for(schedule, slot)
    if spread <= 0:
        return slot
    seed = f"{project_id}|{slot.date().isoformat()}|{slot:%H:%M}"
    frac = random.Random(seed).random()
    return slot + timedelta(seconds=int(frac * spread * 60))


def acted_since(project_id: str, since: datetime) -> list[str]:
    """Эпизоды, с которыми после `since` уже что-то делали на площадке:
    приняла (`platforms[*].at`), отдали и не дождались ответа (`in_flight`),
    отказала (`state: failed`). Сухой прогон не считается — он ничего не шлёт."""
    out: list[str] = []
    for ep in list_episodes(project_id):
        try:
            rec = load_manifest(episode_dir(project_id, ep)).publish
        except (FileNotFoundError, ValueError):
            continue
        if rec is None:
            continue
        stamps = [p.at for p in rec.platforms.values() if p.at]
        stamps += list(rec.in_flight.values())
        if rec.state == "failed" and rec.at:
            stamps.append(rec.at)
        if any(_aware(t) >= since for t in stamps):
            out.append(ep)
    return out


@dataclass(frozen=True)
class Tick:
    """След тика: когда, что решил, какой слот отработал (запустил публикацию)
    и чем это кончилось. `handled` держится, пока не наступит другой слот;
    `failed` значит «попытка по нему упала, следующий тик повторит»."""

    at: datetime
    due: bool
    slot: datetime | None  # последний наступивший слот дня
    target: datetime | None  # секунда выхода этого слота: слот + разброс
    handled: datetime | None  # слот, по которому запускали публикацию
    handled_at: datetime | None  # когда запускали в последний раз
    attempts: int  # попыток по `handled`
    failed: bool  # последняя попытка упала — слот ещё не отработан
    item: str | None  # что собирались выложить; None — готовых не было
    why: str
    every_min: int = TICK_EVERY_MIN  # шаг задачи, с которым пришёл этот тик

    def stale(self, now: datetime) -> bool:
        return (now - self.at) > timedelta(minutes=TICK_STALE_TICKS * self.every_min)

    def wait_sec(self, now: datetime) -> int:
        """Сколько ждать до цели, если она ближе одного шага задачи: тогда
        ждёт этот же тик, и выход попадает в свою секунду, а не в сетку
        задачи. Цель дальше или уже прошла — 0."""
        if self.due or self.target is None:
            return 0
        left = int((self.target - now).total_seconds())
        return left if 0 < left <= self.every_min * 60 else 0


def tick_path(project_id: str) -> Path:
    return project_dir(project_id) / TICK_FILE


def _dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def load_tick(project_id: str) -> Tick | None:
    path = tick_path(project_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Tick(
            at=datetime.fromisoformat(data["at"]),
            due=bool(data.get("due")),
            slot=_dt(data.get("slot")),
            target=_dt(data.get("target")),
            handled=_dt(data.get("handled")),
            handled_at=_dt(data.get("handled_at")),
            attempts=int(data.get("attempts") or 0),
            failed=bool(data.get("failed")),
            item=str(data["item"]) if data.get("item") else None,
            why=str(data.get("why", "")),
            every_min=int(data.get("every_min") or TICK_EVERY_MIN),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None  # битый след — как отсутствующий: тик перепишет


def save_tick(project_id: str, tick: Tick) -> Path:
    path = tick_path(project_id)
    path.write_text(
        json.dumps(
            {
                "at": tick.at.isoformat(),
                "due": tick.due,
                "slot": tick.slot.isoformat() if tick.slot else None,
                "target": tick.target.isoformat() if tick.target else None,
                "handled": tick.handled.isoformat() if tick.handled else None,
                "handled_at": tick.handled_at.isoformat() if tick.handled_at else None,
                "attempts": tick.attempts,
                "failed": tick.failed,
                "item": tick.item,
                "why": tick.why,
                "every_min": tick.every_min,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def is_online(hosts: tuple[str, ...] = NETWORK_HOSTS, timeout: float = 5.0) -> bool:
    """Дозвониться до каждого хоста по 443. Не DNS-запрос, а соединение: при
    моргающей сети имя резолвится из кэша, а пакеты не ходят."""
    for host in hosts:
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                pass
        except OSError:
            return False
    return True


@dataclass(frozen=True)
class Due:
    slot: datetime
    item: QueueItem | None  # что уйдёт: первый готовый; None — готовых нет


def due(project_id: str, now: datetime | None = None) -> tuple[Due | None, str]:
    """Пора ли публиковать: (что, почему). None — не пора, и причина словами."""
    now = now or local_now()
    schedule = load_schedule(project_id)
    if not schedule.times:
        return None, "расписания нет"
    if schedule.paused:
        return None, "расписание на паузе"
    slot = last_slot(schedule, now)
    if slot is None:
        return None, f"первый слот дня ({schedule.times[0]}) ещё не наступил"
    target = publish_at(project_id, schedule, slot)
    if now < target:
        return None, f"слот {slot:%H:%M} наступил, выход в {target:%H:%M:%S}"
    acted = acted_since(project_id, slot)
    if acted:
        return None, f"после слота {slot:%H:%M} площадка уже отвечала: {', '.join(acted)}"
    prev = load_tick(project_id)
    if prev is not None and prev.handled == slot:
        when = prev.handled_at or prev.at
        if not prev.failed:
            return None, f"слот {slot:%H:%M} уже отработан тиком в {when:%H:%M}"
        if prev.attempts >= TICK_MAX_ATTEMPTS:
            return None, (f"слот {slot:%H:%M}: {prev.attempts} попытки упали — до следующего слота")
        return Due(slot, next_ready(project_id)), (
            f"слот {slot:%H:%M}: попытка {prev.attempts} в {when:%H:%M} упала, повтор"
        )
    return Due(
        slot, next_ready(project_id)
    ), f"слот {slot:%H:%M} прошёл, после него ничего не уходило"


def tick(
    project_id: str,
    now: datetime | None = None,
    *,
    online: bool = True,
    every_min: int = TICK_EVERY_MIN,
) -> Tick:
    """Один тик планировщика: решить и оставить след. Вызывающий (run.ps1)
    запускает публикацию, если `due`, и возвращает её код через `tick_result`.
    Слот запоминается как «запущенный» здесь же, чтобы следующий тик не
    запустил его второй раз; отработанным он станет по результату.
    `online=False` — сети нет: слот не трогается, тик ждёт следующего.
    `every_min` — с каким шагом ходит задача: от него считается, сколько тик
    вправе ждать секунду выхода и когда след считать протухшим."""
    now = now or local_now()
    schedule = load_schedule(project_id)
    prev = load_tick(project_id)
    verdict, why = due(project_id, now)
    slot = verdict.slot if verdict else last_slot(schedule, now)
    target = publish_at(project_id, schedule, slot) if slot else None
    if verdict is not None and online:
        same = prev is not None and prev.handled == verdict.slot
        result = Tick(
            at=now,
            due=True,
            slot=slot,
            target=target,
            handled=verdict.slot,
            handled_at=now,
            attempts=(prev.attempts + 1) if same and prev else 1,
            failed=False,
            item=verdict.item.episode if verdict.item else None,
            why=why,
            every_min=every_min,
        )
    else:
        if verdict is not None:
            why = f"сети нет, слот {verdict.slot:%H:%M} ждёт её: " + why
        result = Tick(
            at=now,
            due=False,
            slot=slot,
            target=target,
            handled=prev.handled if prev else None,
            handled_at=prev.handled_at if prev else None,
            attempts=prev.attempts if prev else 0,
            failed=prev.failed if prev else False,
            item=prev.item if prev else None,
            why=why,
            every_min=every_min,
        )
    save_tick(project_id, result)
    return result


def tick_result(project_id: str, code: int, now: datetime | None = None) -> Tick | None:
    """Чем кончился запуск по последнему тику. Упала — ненулевой код, или
    ролик был, а площадка после слота так и не отвечала (запуск прошёл, а
    публикации не случилось). Упавшая попытка отдаёт слот следующему тику;
    тика с запуском не было — None, писать нечего."""
    now = now or local_now()
    prev = load_tick(project_id)
    if prev is None or prev.handled is None:
        return None
    acted = acted_since(project_id, prev.handled)
    failed = code != 0 or (prev.item is not None and not acted)
    if failed:
        left = TICK_MAX_ATTEMPTS - prev.attempts
        why = f"попытка {prev.attempts} по слоту {prev.handled:%H:%M} упала (код {code})" + (
            ", повтор на следующем тике"
            if left > 0
            else ", попытки кончились — до следующего слота"
        )
    elif prev.item:
        why = f"слот {prev.handled:%H:%M} отработан: ушёл {prev.item}"
    else:
        why = f"слот {prev.handled:%H:%M} отработан: выкладывать было нечего"
    result = replace(prev, at=now, due=False, failed=failed, why=why)
    save_tick(project_id, result)
    return result


# ---------------------------------------------------------------- план и запас


@dataclass(frozen=True)
class PlanSlot:
    at: datetime
    item: QueueItem | None  # None — слот пустой: к этому часу выкладывать нечего
    pending: bool = False  # слот уже прошёл и ждёт тика: уйдёт в ближайшие минуты


def plan(project_id: str, now: datetime | None = None) -> list[PlanSlot]:
    """Что и когда уйдёт: готовые в порядке очереди по ближайшим слотам, плюс
    первый пустой слот — он и есть момент, когда контент кончается."""
    now = now or local_now()
    schedule = load_schedule(project_id)
    if not schedule.active:
        return []
    ready = [i for i in queue(project_id) if i.state == "ready"]
    slots: list[PlanSlot] = []
    verdict, _ = due(project_id, now)
    if verdict is not None:
        slots.append(PlanSlot(publish_at(project_id, schedule, verdict.slot), None, pending=True))
    elif (slot := last_slot(schedule, now)) and now < publish_at(project_id, schedule, slot):
        # слот наступил, но его секунда ещё впереди: он в плане первым
        slots.append(PlanSlot(publish_at(project_id, schedule, slot), None))
    need = len(ready) + 1 - len(slots)
    slots += [
        PlanSlot(publish_at(project_id, schedule, at), None)
        for at in slots_after(schedule, now, need)
    ]
    return [
        PlanSlot(s.at, ready[i] if i < len(ready) else None, s.pending) for i, s in enumerate(slots)
    ]


@dataclass(frozen=True)
class Runway:
    """Запас: на сколько хватит готовых при текущих слотах."""

    ready: int
    waiting: int  # ждут ворот: одобри — и запас вырастет
    unfinished: int
    per_day: int
    ends: datetime | None  # первый пустой слот
    days: float | None  # до него от «сейчас», в днях

    @property
    def level(self) -> Literal["ok", "warn", "bad", "none"]:
        if self.days is None:
            return "none"
        if self.ready == 0:
            return "bad"
        return "warn" if self.days <= RUNWAY_WARN_DAYS else "ok"


def runway(project_id: str, now: datetime | None = None) -> Runway:
    now = now or local_now()
    rows = queue(project_id)
    count = {s: sum(r.state == s for r in rows) for s in ("ready", "waiting", "unfinished")}
    schedule = load_schedule(project_id)
    ends: datetime | None = None
    days: float | None = None
    if schedule.active:
        empty = [s for s in plan(project_id, now) if s.item is None]
        if empty:
            ends = empty[0].at
            days = round((ends - now).total_seconds() / 86400, 1)
    return Runway(
        ready=count["ready"],
        waiting=count["waiting"],
        unfinished=count["unfinished"],
        per_day=schedule.per_day,
        ends=ends,
        days=days,
    )


@dataclass(frozen=True)
class Notice:
    """Предупреждение человеку: уровень и текст. Не `Warning` — это имя занято
    питоном."""

    level: Literal["info", "warn", "bad"]
    text: str


def notices(project_id: str, now: datetime | None = None, *, live: bool = False) -> list[Notice]:
    """Что человек должен увидеть первым: запас кончается, расписание стоит,
    планировщик молчит. `live` — у проекта есть аккаунт, ролики обязаны
    выходить: у полигона без расписания предупреждать нечего."""
    now = now or local_now()
    schedule = load_schedule(project_id)
    out: list[Notice] = []
    if not schedule.times:
        if live:
            out.append(Notice("warn", "расписания нет: ролики выходят только по команде руками"))
        return out
    if schedule.paused:
        out.append(Notice("info", "расписание на паузе: слоты стоят, тик ничего не выкладывает"))
    rw = runway(project_id, now)
    hint = ""
    if rw.waiting:
        hint += f"; ждут ворот: {rw.waiting} — одобри, и запас вырастет"
    if rw.unfinished:
        hint += f"; не собрано: {rw.unfinished}"
    if rw.ready == 0:
        out.append(Notice("bad", "выкладывать нечего: готовых роликов нет" + hint))
    elif rw.days is not None and rw.days <= RUNWAY_WARN_DAYS:
        out.append(
            Notice(
                "warn",
                f"контента на {rw.days:g} дн. — до {rw.ends:%d.%m %H:%M}: готовых {rw.ready} "
                f"при {rw.per_day} в день" + hint,
            )
        )
    last = load_tick(project_id)
    if last is None:
        if live and schedule.active:
            out.append(
                Notice(
                    "bad",
                    "планировщик ещё не тикал ни разу: задача «videoforge post-today» "
                    "не поставлена (run.ps1 -Install)",
                )
            )
    elif last.stale(now) and schedule.active:
        out.append(
            Notice(
                "bad",
                f"планировщик молчит с {last.at:%d.%m %H:%M}: задача снята или ПК спал — "
                "слоты не выполняются",
            )
        )
    return out
