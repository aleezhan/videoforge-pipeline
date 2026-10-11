"""Полезные источники: чужие сайты, сервисы, каналы и статьи, на которые
стоит вернуться.

`links/<id>.yaml` — одна запись на источник. Это закладки, а не планы: в
`features/` живут возможности самого инструмента, здесь — чужое, что мы только
посмотрели и, может быть, применим. Смешивать нельзя, иначе «Планы»
превращаются в свалку ссылок.

Зачем это файл, а не сообщение в чате: ссылка, названная в разговоре, через
неделю не находится. Запись пишется для человека, который открыл страницу без
контекста: `summary` — что это, `why` — где нам это может пригодиться, `cost` —
сколько стоит, если это сервис.

Поле `checked` — дата, когда источник последний раз открывали: чужой сайт
умирает молча, и запись без даты проверки со временем начинает врать.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, PrivateAttr

# области — те же, что у features/: «где это может пригодиться» отвечает на тот
# же вопрос, что «где живёт возможность», и иконки в пульте уже нарисованы
from videoforge.core.features import AREAS
from videoforge.core.templates import render, yaml_str
from videoforge.paths import links_dir

log = logging.getLogger("videoforge.links")

# вид источника; порядок — порядок показа на странице
KINDS: dict[str, str] = {
    "service": "Сервисы",
    "tool": "Инструменты",
    "channel": "Каналы и авторы",
    "article": "Статьи и разборы",
    "docs": "Документация",
    "assets": "Материалы: музыка, кадры, шрифты",
    "community": "Сообщества",
    "other": "Прочее",
}


class Link(BaseModel):
    id: str
    title: str
    url: str
    kind: str = "other"
    area: str = "other"  # где может пригодиться: те же области, что у features/
    summary: str = ""  # что это, простым языком — как объяснить другу за минуту
    why: str = ""  # зачем нам: где в нашей работе это может пригодиться
    cost: str = ""  # сколько стоит и на каких условиях, если это сервис
    tags: list[str] = Field(default_factory=list)
    added: str = ""  # дата записи YYYY-MM-DD
    checked: str = ""  # когда последний раз открывали: чужой сайт умирает молча
    source: str = ""  # откуда взялось: разговор такого-то числа, чужой канал
    notes: str = ""
    _path: Path = PrivateAttr(default=Path("."))

    @property
    def kind_title(self) -> str:
        return KINDS.get(self.kind, self.kind)

    @property
    def area_title(self) -> str:
        return AREAS.get(self.area, self.area)

    @property
    def host(self) -> str:
        """Домен для показа: ссылка в списке читается по нему, а не по хвосту."""
        return urlparse(self.url).netloc.removeprefix("www.")


# ---------------------------------------------------------------- файлы


def link_path(link_id: str, base: Path | None = None) -> Path:
    return (base or links_dir()) / f"{link_id}.yaml"


def load_link(link_id: str, base: Path | None = None) -> Link:
    path = link_path(link_id, base)
    if not path.exists():
        raise FileNotFoundError(f"источника {link_id!r} нет: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("id", path.stem)
    for key in ("added", "checked"):  # YAML читает голую дату как date
        if isinstance(data.get(key), date):
            data[key] = data[key].isoformat()
    link = Link.model_validate(data)
    link._path = path
    return link


def list_links(base: Path | None = None) -> list[Link]:
    """Все записи. Битый файл не роняет список — он попадает в лог, остальные
    показываются: страница должна открыться и с одной опечаткой."""
    root = base or links_dir()
    out: list[Link] = []
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.yaml")):
        try:
            out.append(load_link(path.stem, root))
        except Exception as exc:  # noqa: BLE001 — один плохой файл, остальные живы
            log.warning("источник %s не разобран: %s", path.name, exc)
    return out


def validate_link(link: Link) -> list[str]:
    """Без чего запись бесполезна человеку, который откроет её через месяц."""
    problems: list[str] = []
    if link.kind not in KINDS:
        problems.append(f"неизвестный вид '{link.kind}'; есть: {', '.join(KINDS)}")
    if link.area not in AREAS:
        problems.append(f"неизвестная область '{link.area}'; есть: {', '.join(AREAS)}")
    if not urlparse(link.url).scheme.startswith("http"):
        problems.append(f"url '{link.url}' без http(s): ссылка не откроется")
    if not link.summary.strip():
        problems.append("пустой summary: через месяц никто не вспомнит, что это было")
    if not link.why.strip():
        problems.append("пустой why: закладка без «зачем нам» — просто ссылка")
    return problems


def sort_key(link: Link) -> tuple[int, int, str]:
    kinds = list(KINDS)
    areas = list(AREAS)
    return (
        kinds.index(link.kind) if link.kind in KINDS else len(kinds),
        areas.index(link.area) if link.area in AREAS else len(areas),
        link.id,
    )


def create_link(
    link_id: str,
    *,
    title: str,
    url: str,
    kind: str = "other",
    area: str = "other",
    summary: str = "",
    why: str = "",
    cost: str = "",
    source: str = "",
    base: Path | None = None,
) -> tuple[Path, list[str]]:
    """links/<id>.yaml из шаблона с комментариями."""
    if kind not in KINDS:
        raise ValueError(f"неизвестный вид {kind!r}; допустимы: {', '.join(KINDS)}")
    if area not in AREAS:
        raise ValueError(f"неизвестная область {area!r}; допустимы: {', '.join(AREAS)}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]*", link_id):
        raise ValueError(f"id {link_id!r}: латиница, цифры и подчёркивание")
    path = link_path(link_id, base)
    if path.exists():
        raise FileExistsError(f"источник уже записан: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    path.write_text(
        render(
            "link.yaml",
            {
                "id": link_id,
                "title": yaml_str(title),
                "url": yaml_str(url),
                "kind": kind,
                "area": area,
                "summary": yaml_str(summary),
                "why": yaml_str(why),
                "cost": yaml_str(cost),
                "source": yaml_str(source),
                "added": today,
                "checked": today,
            },
        ),
        encoding="utf-8",
    )
    link = load_link(link_id, base)
    return path, validate_link(link)


def summary_line(link: Link) -> str:
    return f"{link.id:<24} [{link.area_title}] {link.title} — {link.host}"


def dump_yaml(link: Link) -> str:
    return yaml.safe_dump(link.model_dump(), allow_unicode=True, sort_keys=False).rstrip()
