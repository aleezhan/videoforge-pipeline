"""Аккаунты на площадках: куда уходят ролики проекта и как они там живут.

`accounts/<id>.yaml` — один аккаунт: сеть (YouTube / TikTok / Instagram),
ссылка и проект, чей контент он публикует. Обратной ссылки в `project.yaml`
нет намеренно: статус проекта выводится, а не хранится — есть хотя бы один
аккаунт → проект живой (`live`), нет → демо / в разработке (`demo`). Хранимый
статус пришлось бы не забывать обновлять; выведенный всегда прав.

Проект можно оставить пустым: это свой аккаунт, который ещё ни к чему не
привязан (заведён, но что на него пойдёт — не решено). Чужой канал сюда всё
равно не пишется, ему место в `research/`.

Статистика — снимки, а не «текущее значение»: `accounts/<id>/stats.jsonl`,
по строке на замер (append-only, как журнал расходов в манифесте). Подписчики
без истории ничего не говорят; с историей видно, что было после каждого
ролика. Рядом `videos.json` — последний список роликов площадки с просмотрами
и лайками: по нему страница аккаунта показывает, что именно работает.

Источник записан в каждой строке снимка (`source`): yt-dlp читает числа со
страницы канала, `youtube_api` — официальную статистику токеном аккаунта, и
это два ряда в одном файле, несравнимые между собой намеренно. У официального
ряда есть ещё окно последних дней (просмотры, минуты, удержание, подписки,
доход) и файл по дням — `accounts/<id>/analytics.jsonl`, строка на день,
дописываются только новые даты.

Сеть и формат — разные вещи. `PLATFORMS` в `core/project.py` перечисляет
форматы (`youtube_shorts`, `youtube`, …), под которые делается ролик; аккаунт
живёт в сети, и одна сеть принимает несколько форматов.
"""

from __future__ import annotations

import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr

from videoforge.core.manifest import now
from videoforge.core.project import PLATFORMS, list_projects
from videoforge.core.templates import render, yaml_str
from videoforge.paths import accounts_dir

log = logging.getLogger("videoforge.accounts")

Network = Literal["youtube", "tiktok", "instagram"]


@dataclass(frozen=True)
class NetworkInfo:
    title: str
    platforms: tuple[str, ...]  # форматы из PLATFORMS, которые сеть принимает


NETWORKS: dict[str, NetworkInfo] = {
    "youtube": NetworkInfo("YouTube", ("youtube", "youtube_shorts")),
    "tiktok": NetworkInfo("TikTok", ("tiktok",)),
    "instagram": NetworkInfo("Instagram", ("instagram_reels",)),
}

STATS_FILE = "stats.jsonl"
ANALYTICS_FILE = "analytics.jsonl"  # официальная статистика по дням, строка на день
VIDEOS_FILE = "videos.json"
VIDEO_STATS_DIR = "video_stats"  # по ролику: <id>.api.json (Analytics), <id>.studio.json (браузер)
AVATAR_FILE = "avatar.jpg"  # аватар площадки, качается при sync: аккаунт в пульте показан лицом
# Ряд официальной статистики (YouTube Analytics API токеном аккаунта). Из двух
# снимков одного дня число берётся из него: yt-dlp читает «15,4 тыс.» со
# страницы, API отдаёт точное — и без правила число прыгало бы между рядами
API_SOURCE = "youtube_api"

ProjectStatus = Literal["live", "demo"]


class Account(BaseModel):
    id: str
    network: Network
    # Чей контент публикует аккаунт. Пусто — свой аккаунт, который ещё ни к чему
    # не привязан: площадка есть, а что на неё пойдёт, не решено (так у проекта
    # podcast_clips 11.09.2026 отцеплены TikTok и Instagram — стартуем с одного
    # YouTube). Чужой канал сюда всё равно не пишется: ему место в research/
    project: str = ""
    handle: str = ""  # @имя на площадке
    url: str = ""
    channel_id: str = ""  # технический id (UC… у YouTube) — для сбора статистики
    title: str = ""
    # Канал в Партнёрской программе YouTube. Единственное, что от этого зависит
    # сегодня: свою обложку шортсу YouTube принимает только у партнёрских каналов
    # (с 25.07.2026), у остальных загруженная через API картинка встаёт в Studio
    # заглушкой — и без этого флага публикация обложку шортса не отправляет
    partner: bool = False
    notes: str = ""
    _path: Path = PrivateAttr(default=Path("."))

    def folder(self) -> Path:
        return self._path.parent / self.id

    @property
    def network_title(self) -> str:
        return NETWORKS[self.network].title


class StatsSnapshot(BaseModel):
    """Один замер. Поля без значения остаются None: площадка могла не отдать
    число, и нуль на его месте был бы враньём в графике."""

    at: datetime
    source: str  # ytdlp | manual | youtube_api | …: сменился источник — ряд несравним
    subscribers: int | None = None
    views_total: int | None = None
    likes_total: int | None = None
    video_count: int | None = None
    note: str = ""
    # Окно последних дней — только у официального ряда (source youtube_api):
    # yt-dlp видит итоги «за всё время», а сколько смотрели на этой неделе и
    # досматривают ли — знает лишь площадка. `views` — за окно, в отличие от
    # `views_total`; `window_days` говорит, за сколько дней всё это
    window_days: int | None = None
    views: int | None = None
    watch_minutes: float | None = None
    avg_view_sec: float | None = None
    avg_view_pct: float | None = None
    subs_gained: int | None = None
    subs_lost: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    revenue_usd: float | None = None


class DayStats(BaseModel):
    """Один день официальной статистики (`accounts/<id>/analytics.jsonl`).

    Строка на день, дописываются только новые даты: день, который уже в
    файле, повторный sync не трогает. Поэтому сюда идут дни, которые площадка
    досчитала, — какие именно, решает источник, а не этот файл."""

    day: date
    source: str = API_SOURCE
    views: int | None = None
    watch_minutes: float | None = None
    avg_view_sec: float | None = None
    avg_view_pct: float | None = None
    subs_gained: int | None = None
    subs_lost: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    revenue_usd: float | None = None


class AccountVideo(BaseModel):
    """Ролик на площадке, как его видит зритель: без описания и служебных полей,
    чтобы файл со списком оставался лёгким."""

    id: str
    url: str = ""
    title: str = ""
    published_at: datetime | None = None
    duration_sec: float | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    thumbnail_url: str = ""
    is_short: bool | None = None


class AccountVideos(BaseModel):
    collected_at: datetime
    source: str
    videos: list[AccountVideo] = Field(default_factory=list)


class RetentionPoint(BaseModel):
    """Точка кривой удержания: доля ролика → доля зрителей, которые её
    смотрят (больше 1 — пересматривают), и та же доля относительно похожих
    роликов площадки (0.5 — как у всех, выше — лучше)."""

    at: float
    watching: float
    relative: float | None = None


class TrafficRow(BaseModel):
    views: int = 0
    minutes: float = 0.0


class VideoStats(BaseModel):
    """Один ролик за всё время по Analytics API — последний снимок, файл
    переписывается каждым sync: кривая накопительная, и вчерашняя её версия —
    та же кривая на меньшем числе зрителей."""

    video_id: str
    at: datetime
    source: str = "youtube_api"
    views: int | None = None
    watch_minutes: float | None = None
    avg_view_sec: float | None = None
    avg_view_pct: float | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    subs_gained: int | None = None
    retention: list[RetentionPoint] = Field(default_factory=list)
    traffic: dict[str, TrafficRow] = Field(default_factory=dict)


class StudioStats(BaseModel):
    """Что прочитано со страниц YouTube Studio браузером: то, чего нет в API
    (показы значка и CTR). Числа — как их напечатала Studio; None — на
    странице не нашлось, а не ноль. `text` — видимый текст страницы как есть:
    вёрстка Studio меняется, и разбирать заново проще по сохранённому."""

    video_id: str
    at: datetime
    impressions: int | None = None
    ctr_pct: float | None = None
    views: int | None = None
    avg_view_sec: float | None = None
    screenshots: list[str] = Field(default_factory=list)
    text: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------- файлы


def account_path(account_id: str, base: Path | None = None) -> Path:
    return (base or accounts_dir()) / f"{account_id}.yaml"


def load_account(account_id: str, base: Path | None = None) -> Account:
    path = account_path(account_id, base)
    if not path.exists():
        raise FileNotFoundError(f"аккаунта {account_id!r} нет: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("id", path.stem)
    acc = Account.model_validate(data)
    acc._path = path
    return acc


_CHANNEL_ID_LINE = re.compile(r"(?m)^channel_id:.*?(?P<comment>#.*)?$")


def remember_channel_id(acc: Account, channel_id: str) -> bool:
    """Записать технический id канала (UC…), если его ещё нет.

    Правится одна строка, комментарии шаблона остаются: `model_dump` стёр бы
    их, а они здесь для человека. Уже записанный id не трогается — ссылка и
    хэндл у канала меняются, id нет, и молча переписать его значило бы
    подменить аккаунт.

    Заполняется при `account sync`: шаблон обещает «пусто — определится при
    sync», и до 15.09.2026 это было неправдой — поле оставалось пустым у
    живого канала, который синхронизировали десяток раз."""
    channel_id = channel_id.strip()
    if not channel_id or acc.channel_id.strip():
        return False

    def sub(m: re.Match[str]) -> str:
        comment = m.group("comment")
        return f"channel_id: {yaml_str(channel_id)}" + (f"  {comment}" if comment else "")

    text, n = _CHANNEL_ID_LINE.subn(sub, acc._path.read_text(encoding="utf-8"), count=1)
    if n == 0:
        text = text.rstrip("\n") + f"\nchannel_id: {yaml_str(channel_id)}\n"
    acc._path.write_text(text, encoding="utf-8")
    acc.channel_id = channel_id
    return True


def list_accounts(base: Path | None = None) -> list[Account]:
    """Все аккаунты. Битый файл не роняет список — он попадает в лог, а
    остальные показываются: интерфейс должен открыться и с одной опечаткой."""
    root = base or accounts_dir()
    out: list[Account] = []
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.yaml")):
        try:
            out.append(load_account(path.stem, root))
        except Exception as exc:  # noqa: BLE001 — один плохой файл, остальные живы
            log.warning("аккаунт %s не разобран: %s", path.name, exc)
    return out


def validate_account(acc: Account, projects: list[str] | None = None) -> list[str]:
    """Проблемы, с которыми аккаунт нельзя считать привязанным."""
    problems: list[str] = []
    known = projects if projects is not None else list_projects()
    # пустой проект — не проблема, а состояние «пока ни к чему не привязан»;
    # проблема — привязка к тому, чего нет: это опечатка в id
    if acc.project and acc.project not in known:
        problems.append(f"проект {acc.project!r} не существует; есть: {', '.join(known) or '—'}")
    if not acc.url and not acc.handle:
        problems.append("нет ни url, ни handle — статистику собирать неоткуда")
    return problems


def accounts_for_project(project_id: str, base: Path | None = None) -> list[Account]:
    # пустой id не должен собрать все непривязанные аккаунты: «ничей» — не проект
    return [a for a in list_accounts(base) if project_id and a.project == project_id]


def project_status(project_id: str, base: Path | None = None) -> ProjectStatus:
    """Живой проект — тот, у которого есть аккаунт. Остальное — демо."""
    return "live" if accounts_for_project(project_id, base) else "demo"


def platforms_of(network: str) -> list[str]:
    """Форматы `PLATFORMS`, которые принимает сеть."""
    return [p for p in NETWORKS[network].platforms if p in PLATFORMS]


def create_account(
    account_id: str,
    *,
    network: str,
    project: str,
    handle: str = "",
    url: str = "",
    channel_id: str = "",
    title: str = "",
    base: Path | None = None,
) -> tuple[Path, list[str]]:
    """accounts/<id>.yaml из шаблона с комментариями + папка под снимки.

    Проект проверяется здесь, а не при загрузке: аккаунт к несуществующему
    проекту — опечатка, и её надо ловить в момент, когда человек ещё помнит,
    что хотел написать. Пустой проект — не опечатка, а «аккаунт есть, что на
    него пойдёт, ещё не решено»."""
    if network not in NETWORKS:
        raise ValueError(f"неизвестная сеть {network!r}; допустимы: {', '.join(NETWORKS)}")
    if project and project not in list_projects():
        raise ValueError(f"проект {project!r} не существует — сначала videoforge project new")
    path = account_path(account_id, base)
    if path.exists():
        raise FileExistsError(f"аккаунт уже существует: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render(
            "account.yaml",
            {
                "id": account_id,
                "network": network,
                "project": project,
                "handle": yaml_str(handle),
                "url": yaml_str(url),
                "channel_id": yaml_str(channel_id),
                "title": yaml_str(title or handle or account_id),
            },
        ),
        encoding="utf-8",
    )
    (path.parent / account_id).mkdir(exist_ok=True)
    acc = load_account(account_id, base)  # шаблон отрендерился в валидный YAML
    return path, validate_account(acc)


# ---------------------------------------------------------------- снимки


def stats_path(acc: Account) -> Path:
    return acc.folder() / STATS_FILE


def videos_path(acc: Account) -> Path:
    return acc.folder() / VIDEOS_FILE


def add_snapshot(acc: Account, snapshot: StatsSnapshot) -> Path:
    """Дописать замер. Только добавление: история — это и есть ценность."""
    path = stats_path(acc)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(snapshot.model_dump_json() + "\n")
    return path


def load_history(acc: Account) -> list[StatsSnapshot]:
    path = stats_path(acc)
    if not path.exists():
        return []
    out: list[StatsSnapshot] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(StatsSnapshot.model_validate_json(line))
        except ValueError as exc:
            log.warning("строка статистики %s пропущена: %s", acc.id, exc)
    out.sort(key=lambda s: s.at)
    return out


def latest_snapshot(acc: Account) -> StatsSnapshot | None:
    history = load_history(acc)
    return history[-1] if history else None


METRICS: tuple[str, ...] = ("subscribers", "views_total", "likes_total", "video_count")


def latest_known(history: list[StatsSnapshot]) -> dict[str, dict[str, object] | None]:
    """По каждой метрике — последнее ИЗВЕСТНОЕ значение с датой и источником.

    Ручной замер «подписчиков 15 400» не должен стирать число роликов, снятое
    автоматически неделей раньше: в снимке его нет, а у аккаунта — есть.

    Из снимков одного дня побеждает `API_SOURCE`, а не тот, что лёг позже:
    sync пишет ряд yt-dlp и ряд API подряд, и по секунде записи число
    прыгало бы между округлённым и точным."""
    out: dict[str, dict[str, object] | None] = {}
    for key in METRICS:
        out[key] = None
        known = [s for s in history if getattr(s, key) is not None]
        if not known:
            continue
        last = known[-1]
        same_day = [s for s in known if s.at.date() == last.at.date()]
        pick = next((s for s in reversed(same_day) if s.source == API_SOURCE), last)
        out[key] = {"value": getattr(pick, key), "at": pick.at, "source": pick.source}
    return out


# ------------------------------------------------------------ по дням (API)


def analytics_path(acc: Account) -> Path:
    return acc.folder() / ANALYTICS_FILE


def load_days(acc: Account) -> list[DayStats]:
    path = analytics_path(acc)
    if not path.exists():
        return []
    out: list[DayStats] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(DayStats.model_validate_json(line))
        except ValueError as exc:
            log.warning("строка статистики по дням %s пропущена: %s", acc.id, exc)
    out.sort(key=lambda d: d.day)
    return out


def add_days(acc: Account, rows: list[DayStats]) -> list[DayStats]:
    """Дописать дни, которых в файле ещё нет; вернуть дописанные.

    Только добавление и только новые даты: день из файла повторный sync не
    трогает — иначе один и тот же файл рассказывал бы вчера одно, сегодня
    другое, и графику нечему было бы верить."""
    known = {d.day for d in load_days(acc)}
    fresh: list[DayStats] = []
    for row in sorted(rows, key=lambda r: r.day):
        if row.day in known:
            continue
        known.add(row.day)
        fresh.append(row)
    if not fresh:
        return []
    path = analytics_path(acc)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in fresh:
            fh.write(row.model_dump_json() + "\n")
    return fresh


def save_videos(acc: Account, videos: AccountVideos) -> Path:
    path = videos_path(acc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(videos.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_videos(acc: Account) -> AccountVideos | None:
    path = videos_path(acc)
    if not path.exists():
        return None
    try:
        return AccountVideos.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        log.warning("список роликов %s не разобран: %s", acc.id, exc)
        return None


def video_stats_dir(acc: Account) -> Path:
    return acc.folder() / VIDEO_STATS_DIR


def save_video_stats(acc: Account, stats: VideoStats | StudioStats) -> Path:
    """`video_stats/<id>.api.json` или `<id>.studio.json` — источники рядом, не
    вперемешку: у API и у Studio разные окна и разное запаздывание."""
    kind = "studio" if isinstance(stats, StudioStats) else "api"
    path = video_stats_dir(acc) / f"{stats.video_id}.{kind}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stats.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_video_stats(acc: Account, video_id: str) -> VideoStats | None:
    return _load_model(video_stats_dir(acc) / f"{video_id}.api.json", VideoStats)


def load_studio_stats(acc: Account, video_id: str) -> StudioStats | None:
    return _load_model(video_stats_dir(acc) / f"{video_id}.studio.json", StudioStats)


def _load_model[M: BaseModel](path: Path, model: type[M]) -> M | None:
    if not path.exists():
        return None
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        log.warning("%s не разобран: %s", path, exc)
        return None


def avatar_path(acc: Account) -> Path:
    return acc.folder() / AVATAR_FILE


def save_avatar(acc: Account, url: str) -> Path | None:
    """Скачать аватар площадки рядом со снимками. Неудача — не ошибка синка:
    числа важнее картинки, а картинка появится на следующем прогоне."""
    if not url:
        return None
    path = avatar_path(acc)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        urllib.request.urlretrieve(url, path)  # noqa: S310 — https из метаданных площадки
    except (OSError, ValueError) as exc:
        log.warning("аватар %s не скачался: %s", acc.id, exc)
        return None
    return path


def last_published(videos: AccountVideos | None) -> datetime | None:
    """Дата последнего ролика на площадке — первое, что должно бросаться в глаза:
    «ой, давно не публиковал» видно без чтения таблицы."""
    if videos is None:
        return None
    dates = [v.published_at for v in videos.videos if v.published_at]
    return max(dates) if dates else None


def manual_snapshot(values: dict[str, int], note: str = "") -> StatsSnapshot:
    """Замер, введённый руками: для сетей, откуда числа автоматически не достать.

    Ключи — поля снимка (`subscribers`, `views_total`, `likes_total`,
    `video_count`); незнакомый ключ — ошибка, а не молчаливый пропуск."""
    allowed = {"subscribers", "views_total", "likes_total", "video_count"}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"неизвестные поля {unknown}; допустимы: {', '.join(sorted(allowed))}")
    if not values:
        raise ValueError("ручной замер пуст: укажи хотя бы subscribers=…")
    return StatsSnapshot(at=now(), source="manual", note=note, **values)


def parse_manual(pairs: list[str]) -> dict[str, int]:
    """`subscribers=1200 views_total=34000` → словарь чисел.

    Пары принимаются и по одной (`--manual a=1 --manual b=2`), и списком в
    одном аргументе через пробел или запятую — как человеку удобнее."""
    out: dict[str, int] = {}
    items = [p for chunk in pairs for p in chunk.replace(",", " ").split() if p]
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"ожидалось поле=число, получено {item!r}")
        try:
            out[key.strip()] = int(value.strip().replace("_", "").replace(" ", ""))
        except ValueError:
            raise ValueError(f"{key.strip()}: не число — {value.strip()!r}") from None
    return out


def summary_line(acc: Account) -> str:
    """Строка для списка: сеть, проект, последние подписчики и дата замера."""
    last = latest_snapshot(acc)
    if last is None:
        stats = "замеров нет"
    else:
        known = latest_known(load_history(acc))["subscribers"]
        subs = f"{known['value']:,}".replace(",", " ") if known else "?"
        stats = f"подписчиков {subs} · {last.at:%Y-%m-%d} ({last.source})"
    project = acc.project or "—"  # «ничей» лучше пустоты: пустота читается как обрезанная строка
    return f"{acc.id:<24} {acc.network_title:<10} проект: {project:<16} {stats}"


def dump_yaml(acc: Account) -> str:
    return yaml.safe_dump(acc.model_dump(), allow_unicode=True, sort_keys=False).rstrip()
