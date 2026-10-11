"""research/index.md — оглавление всех анализов, одна строка на канал.

Зачем: когда анализов станет несколько десятков, перечитывать их целиком перед
каждым видео нельзя. Индекс отвечает на один вопрос — «есть ли у нас разбор
подходящей ниши» — и даёт путь к брифу. Читается целиком даже на сотне строк.

Файл собирается из папок research/*, руками не правится: любая правка
затирается следующей пересборкой. Источник истины — артефакты стадий.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from videoforge.core.io import read_model
from videoforge.intel.analysis import list_analyses, load_analysis_from_dir
from videoforge.intel.models import ChannelData, ClassifyReport, StatsReport
from videoforge.intel.stages.classify import classify_path
from videoforge.intel.stages.collect import channel_path
from videoforge.intel.stages.formula import blueprint_path
from videoforge.intel.stages.stats import STATS_DIR, STATS_FILE
from videoforge.paths import analysis_dir, research_dir, research_index_path

HEADER = """# Анализы каналов

Оглавление `research/`. Собирается командой `videoforge analyze index`, руками
не правится. Прежде чем придумывать канал или писать сценарий, посмотри сюда:
если разбор подходящей ниши уже есть — открой его `blueprint.md`, там формула,
правила хука и измеренный ритм монтажа.
"""


def _humanize(views: float) -> str:
    for bound, suffix in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс")):
        if views >= bound:
            return f"{views / bound:.1f} {suffix}".replace(".0 ", " ")
    return f"{views:.0f}"


def _row(slug: str) -> str:
    """Одна строка индекса. Недостающие стадии не мешают: пишем что есть."""
    adir = analysis_dir(slug)
    config = load_analysis_from_dir(adir)
    title = config.urls[0] if config.urls else slug
    subs = ""
    if channel_path(adir).exists():
        channel = read_model(channel_path(adir), ChannelData).channel
        title = channel.title or title
        if channel.subscribers:
            subs = f" · {_humanize(channel.subscribers)} подписчиков"

    niche = "ниша не определена (нет classify)"
    formats = ""
    if classify_path(adir).exists():
        report = read_model(classify_path(adir), ClassifyReport)
        niche = report.taxonomy.niche
        top = sorted(report.formats, key=lambda f: -f.median_views)[:2]
        if top:
            formats = " · сильные форматы: " + ", ".join(f.name or f.format for f in top)

    numbers = ""
    stats_path = adir / STATS_DIR / STATS_FILE
    if stats_path.exists():
        stats = read_model(stats_path, StatsReport)
        parts = [f"{stats.counted_videos} видео"]
        for group, gs in sorted(stats.groups.items()):
            parts.append(f"медиана {group}: {_humanize(gs.median_views)}")
        if stats.cadence_per_week:
            parts.append(f"{stats.cadence_per_week:g} выпуска/нед.")
        numbers = " · ".join(parts)

    blueprint = blueprint_path(adir)
    link = (
        f"[бриф]({blueprint.relative_to(research_dir()).as_posix()})"
        if blueprint.exists()
        else "брифа нет — стадия formula не доходила"
    )
    goal = f"\n  - зачем брали: {config.goal}" if config.goal else ""
    return (
        f"## {title}{subs}\n\n"
        f"- `{slug}` · {config.language} · {link}\n"
        f"- {niche}{formats}\n"
        f"- {numbers or 'статистики нет'}{goal}\n"
    )


def render_index() -> str:
    slugs = list_analyses()
    if not slugs:
        return f"{HEADER}\n_Анализов пока нет._\n"
    rows = "\n".join(_row(slug) for slug in slugs)
    stamp = datetime.now(UTC).astimezone().strftime("%d.%m.%Y")
    return f"{HEADER}\n_Обновлено {stamp} · анализов: {len(slugs)}_\n\n{rows}"


def write_index() -> Path:
    path = research_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_index(), encoding="utf-8")
    return path
