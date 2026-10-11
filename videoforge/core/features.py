"""Планы и возможности: что инструмент умеет и что решили сделать.

`features/<id>.yaml` — одна запись: возможность инструмента на любой стадии
жизни, от идеи до сделанного. Одна папка и один формат на оба раздела пульта:
«Планы» показывает всё, что не сделано, «Возможности» — сделанное. Идея,
доведённая до конца, никуда не переезжает — у неё меняется `status` и
появляется `how_to_use`.

Зачем это файл, а не память: за один разговор всплывает пять идей, и через
неделю их нет. Запись здесь пишется для человека, который открыл страницу без
контекста разговора и хочет за десять секунд понять «что это и зачем». Числа и
ссылки — отдельными полями, не в summary.

Статус `dropped` (отклонено) хранится намеренно, с причиной: решение «не
делаем и почему» ценнее, чем пустое место, куда та же идея придёт снова.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr

from videoforge.core.templates import render, yaml_str
from videoforge.paths import features_dir

log = logging.getLogger("videoforge.features")

Status = Literal["idea", "planned", "in_progress", "done", "dropped"]

# порядок — это порядок показа: сначала то, что ближе к делу
STATUSES: dict[str, str] = {
    "in_progress": "в работе",
    "planned": "решили делать",
    "idea": "идея",
    "done": "умеем",
    "dropped": "отклонено",
}

# область = где это живёт в конвейере; порядок — порядок стадий, потом остальное
AREAS: dict[str, str] = {
    "script": "Сценарий",
    "planner": "Раскадровка",
    "images": "Картинки",
    "video": "Видео",
    "audio": "Озвучка и звук",
    "edit": "Монтаж",
    "assemble": "Сборка и субтитры",
    "qa": "Проверка готового",
    "publish": "Публикация",
    "accounts": "Аккаунты и статистика",
    "clips": "Нарезки из длинного",
    "intel": "Анализ каналов",
    "resources": "Ресурсы, каст и библиотека приёмов",
    "gpu": "Своя карта",
    "ui": "Пульт и CLI",
    "other": "Прочее",
}

OPEN_STATUSES: tuple[str, ...] = ("in_progress", "planned", "idea")

# тема — несколько записей внутри области об одном и том же: четыре записи
# про обложку читаются как одна возможность «Обложка» с деталями и планами
# внутри, а не как четыре узла в ряд на карте «Возможности». Пустая тема —
# запись сама по себе; тема заводится только там, где записей уже несколько,
# поэтому словарь короткий намеренно. Порядок — порядок показа внутри стадии
TOPICS: dict[str, str] = {
    "captions": "Текст на экране",
    "final_checks": "Замеры готового",
    "thumbnail": "Обложка",
    "synthetic": "Пометка ИИ",
    "upload": "Загрузка на площадку",
    "packaging": "Упаковка",
    "lora": "LoRA персонажа",
    "image_check": "Проверка кадра",
    "voice": "Голос",
    "music": "Музыка и звуки",
    "reframe": "Кадрирование",
    "cast": "Каст",
    "library": "Библиотека приёмов",
    "tests": "Тесты",
}


class Feature(BaseModel):
    id: str
    title: str
    status: Status = "idea"
    area: str = "other"
    topic: str = ""  # тема внутри области (TOPICS); пусто — запись сама по себе
    summary: str = ""  # что это, простым языком — как объяснить другу за минуту
    gives: str = ""  # что получим: зачем это делать
    cost: str = ""  # на что влияет: время, деньги, память карты, качество, работа человека
    how_to_use: str = ""  # для done: команда, флаг, файл — где это включается
    requires: list[str] = Field(default_factory=list)  # что нужно прежде
    links: list[str] = Field(default_factory=list)
    source: str = ""  # откуда взялось: разговор такого-то числа, документ, чужой продукт
    recorded: str = ""  # дата записи YYYY-MM-DD
    done_at: str | None = None
    notes: str = ""
    _path: Path = PrivateAttr(default=Path("."))

    @property
    def status_title(self) -> str:
        return STATUSES.get(self.status, self.status)

    @property
    def area_title(self) -> str:
        return AREAS.get(self.area, self.area)

    @property
    def topic_title(self) -> str:
        return TOPICS.get(self.topic, self.topic)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


# ---------------------------------------------------------------- файлы


def feature_path(feature_id: str, base: Path | None = None) -> Path:
    return (base or features_dir()) / f"{feature_id}.yaml"


def load_feature(feature_id: str, base: Path | None = None) -> Feature:
    path = feature_path(feature_id, base)
    if not path.exists():
        raise FileNotFoundError(f"записи {feature_id!r} нет: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("id", path.stem)
    data["topic"] = data.get("topic") or ""  # `topic:` без значения — «темы нет», не ошибка
    for key in ("recorded", "done_at"):  # YAML читает голую дату как date
        if isinstance(data.get(key), date):
            data[key] = data[key].isoformat()
    feat = Feature.model_validate(data)
    feat._path = path
    return feat


def list_features(base: Path | None = None) -> list[Feature]:
    """Все записи. Битый файл не роняет список — он попадает в лог, остальные
    показываются: страница должна открыться и с одной опечаткой."""
    root = base or features_dir()
    out: list[Feature] = []
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.yaml")):
        try:
            out.append(load_feature(path.stem, root))
        except Exception as exc:  # noqa: BLE001 — один плохой файл, остальные живы
            log.warning("запись %s не разобрана: %s", path.name, exc)
    return out


def validate_feature(feat: Feature) -> list[str]:
    """Без чего запись бесполезна человеку, который её откроет через месяц."""
    problems: list[str] = []
    if feat.status not in STATUSES:
        problems.append(f"неизвестный статус '{feat.status}'; есть: {', '.join(STATUSES)}")
    if feat.area not in AREAS:
        problems.append(f"неизвестная область '{feat.area}'; есть: {', '.join(AREAS)}")
    if feat.topic and feat.topic not in TOPICS:
        problems.append(f"неизвестная тема '{feat.topic}'; есть: {', '.join(TOPICS)}")
    if not feat.summary.strip():
        problems.append("пустой summary: через месяц никто не вспомнит, что это было")
    if feat.status == "done" and not feat.how_to_use.strip():
        problems.append("status: done, но how_to_use пуст — умеем, а как включить, неизвестно")
    if feat.status == "dropped" and not feat.notes.strip():
        problems.append("status: dropped без причины в notes — та же идея придёт снова")
    return problems


def sort_key(feat: Feature) -> tuple[int, int, str]:
    statuses = list(STATUSES)
    areas = list(AREAS)
    return (
        statuses.index(feat.status) if feat.status in STATUSES else len(statuses),
        areas.index(feat.area) if feat.area in AREAS else len(areas),
        feat.id,
    )


def create_feature(
    feature_id: str,
    *,
    title: str,
    area: str = "other",
    topic: str = "",
    status: str = "idea",
    summary: str = "",
    gives: str = "",
    cost: str = "",
    source: str = "",
    base: Path | None = None,
) -> tuple[Path, list[str]]:
    """features/<id>.yaml из шаблона с комментариями."""
    if status not in STATUSES:
        raise ValueError(f"неизвестный статус {status!r}; допустимы: {', '.join(STATUSES)}")
    if area not in AREAS:
        raise ValueError(f"неизвестная область {area!r}; допустимы: {', '.join(AREAS)}")
    if topic and topic not in TOPICS:
        raise ValueError(f"неизвестная тема {topic!r}; допустимы: {', '.join(TOPICS)}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]*", feature_id):
        raise ValueError(f"id {feature_id!r}: латиница, цифры и подчёркивание")
    path = feature_path(feature_id, base)
    if path.exists():
        raise FileExistsError(f"запись уже существует: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    path.write_text(
        render(
            "feature.yaml",
            {
                "id": feature_id,
                "title": yaml_str(title),
                "status": status,
                "area": area,
                "topic": yaml_str(topic),
                "summary": yaml_str(summary),
                "gives": yaml_str(gives),
                "cost": yaml_str(cost),
                "source": yaml_str(source),
                "recorded": today,
                "done_at": today if status == "done" else "null",
            },
        ),
        encoding="utf-8",
    )
    feat = load_feature(feature_id, base)
    return path, validate_feature(feat)


_STATUS_LINE = re.compile(r"^status:[^\n#]*(?P<comment>#.*)?$", re.MULTILINE)
_DONE_LINE = re.compile(r"^done_at:[^\n#]*(?P<comment>#.*)?$", re.MULTILINE)


def set_status(feature_id: str, status: str, base: Path | None = None) -> tuple[Path, list[str]]:
    """Сменить статус, не трогая остальной файл: комментарии в нём — для
    человека, и model_dump их бы стёр. Правятся две строки: `status:` и
    `done_at:` (дата ставится при переходе в done и снимается при уходе из него)."""
    if status not in STATUSES:
        raise ValueError(f"неизвестный статус {status!r}; допустимы: {', '.join(STATUSES)}")
    feat = load_feature(feature_id, base)  # заодно убеждаемся, что файл читается
    path = feat._path
    text = path.read_text(encoding="utf-8")

    def _replace(pattern: re.Pattern[str], key: str, value: str, text: str) -> str:
        def sub(m: re.Match[str]) -> str:
            comment = m.group("comment")
            return f"{key}: {value}" + (f"  {comment}" if comment else "")

        new, n = pattern.subn(sub, text, count=1)
        if n == 0:
            new = text.rstrip("\n") + f"\n{key}: {value}\n"
        return new

    text = _replace(_STATUS_LINE, "status", status, text)
    done_at = date.today().isoformat() if status == "done" else "null"
    text = _replace(_DONE_LINE, "done_at", done_at, text)
    path.write_text(text, encoding="utf-8")
    return path, validate_feature(load_feature(feature_id, base))


def summary_line(feat: Feature) -> str:
    return f"{feat.id:<28} [{feat.area_title}] {feat.title}"


def dump_yaml(feat: Feature) -> str:
    return yaml.safe_dump(feat.model_dump(), allow_unicode=True, sort_keys=False).rstrip()
