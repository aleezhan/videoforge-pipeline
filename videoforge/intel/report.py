"""HTML-отчёт анализа: динамика канала, выбросы, таблицы — один файл.

Просмотрщик, не интерфейс: читает те же артефакты, что и стадии
(analysis.yaml, manifest.json, 01_collect/channel.json, 02_stats/stats.json),
ничего не меняет. Терпим к недостающему: отчёт полезен на любом этапе.

График — точки «дата × просмотры» на лог-шкале: линейная шкала прячет всё,
кроме главного хита. Пунктир — скользящая медиана соседей (то, от чего
считается ratio выброса). Ряды long/shorts — слоты 1–2 категориальной палитры
(валидированы для точечных графиков в обеих темах); выброс — не третий цвет,
а крупнее точка + кольцо цвета фона + подпись ×N.
"""

from __future__ import annotations

import html
import math
from datetime import datetime
from pathlib import Path

from videoforge.core.io import read_model
from videoforge.core.manifest import Manifest, StageRecord, load_manifest
from videoforge.core.workflow import load_workflow
from videoforge.intel.analysis import load_analysis_from_dir
from videoforge.intel.models import (
    DEFAULT_RPM,
    AnalysisConfig,
    ChannelData,
    ClassifyReport,
    DeepReport,
    DeepVideoAnalysis,
    Formula,
    StatsReport,
    VideoStats,
    VisualReport,
    VisualVideoAnalysis,
)
from videoforge.intel.stages.classify import classify_path
from videoforge.intel.stages.collect import channel_path
from videoforge.intel.stages.deep import ANALYSIS_FILE, DEEP_DIR, deep_index_path
from videoforge.intel.stages.formula import BLUEPRINT_FILE, FORMULA_DIR, formula_path
from videoforge.intel.stages.stats import STATS_DIR, STATS_FILE
from videoforge.intel.stages.visual import VISUAL_DIR, visual_index_path

REPORT_FILE = "report.html"

GROUP_TITLES = {"long": "длинные", "shorts": "shorts"}
_SERIES_CLASS = {"long": "s1", "shorts": "s2"}

_CSS = """
:root { --bg:#f6f8fa; --card:#fff; --ink:#1f2328; --muted:#57606a; --line:#d0d7de;
        --series-1:#2a78d6; --series-2:#eb6834; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --card:#161b22; --ink:#e6edf3; --muted:#8b949e; --line:#30363d;
          --series-1:#3987e5; --series-2:#d95926; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--ink);
       font:14px/1.5 system-ui, "Segoe UI", sans-serif; }
main { max-width: 900px; margin: 0 auto; }
h1 { font-size:20px; margin:0 0 4px; }
h2 { font-size:16px; margin:20px 0 4px; }
.meta, .money { color:var(--muted); margin:0 0 8px; }
.money b, .meta b { color:var(--ink); }
.banner { background:#fff8c5; border:1px solid #d4a72c66; color:#4d2d00;
          padding:8px 12px; border-radius:8px; margin:12px 0; }
table { border-collapse:collapse; width:100%; background:var(--card);
        border:1px solid var(--line); border-radius:8px; margin:12px 0; }
th, td { text-align:left; padding:6px 10px; border-top:1px solid var(--line); }
tr:first-child > * { border-top:none; }
th { color:var(--muted); font-weight:600; }
.num { text-align:right; font-variant-numeric: tabular-nums; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px;
         background:var(--line); color:var(--ink); margin-right:4px; }
.badge.pass, .badge.done { background:#1a7f3722; color:#1a7f37; }
.badge.fail, .badge.failed, .badge.blocked { background:#cf222e22; color:#cf222e; }
.badge.skipped { background:#0969da22; color:#0969da; }
.badge.pending { color:var(--muted); }
.badge.running { background:#d4a72c33; color:#7d4e00; }
tr.outlier td { font-weight:600; }
.thumb { width:120px; height:auto; border-radius:4px; display:block; border:1px solid var(--line); }
.strip { display:flex; gap:6px; overflow-x:auto; margin:8px 0; padding-bottom:4px; }
.keyframe { height:80px; width:auto; border-radius:4px; border:1px solid var(--line); flex:none; }
.dotmark { display:inline-block; width:10px; height:10px; border-radius:50%;
           margin-right:6px; vertical-align:-1px; }
.dotmark.s1 { background:var(--series-1); }
.dotmark.s2 { background:var(--series-2); }
.viz { background:var(--card); border:1px solid var(--line); border-radius:8px;
       padding:12px 16px; margin:12px 0; }
.viz svg { width:100%; height:auto; display:block; }
.legend { display:flex; gap:16px; color:var(--muted); font-size:12px; margin:0 0 4px; }
.dot { opacity:.75; }
.dot.s1 { fill:var(--series-1); }
.dot.s2 { fill:var(--series-2); }
.dot.outlier { stroke:var(--card); stroke-width:2; opacity:1; }
.median { fill:none; stroke-width:2; stroke-dasharray:5 4; opacity:.55; }
.median.s1 { stroke:var(--series-1); }
.median.s2 { stroke:var(--series-2); }
.gridline { stroke:var(--line); stroke-width:1; }
.tick { fill:var(--muted); font-size:11px; }
.lbl { fill:var(--ink); font-size:11px; font-weight:600; }
details { margin-top:6px; }
summary { cursor:pointer; color:var(--muted); }
a { color:#0969da; }
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _views(value: float | int) -> str:
    return f"{int(value):,}".replace(",", " ")


def _views_short(value: float) -> str:
    for bound, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= bound:
            n = value / bound
            return f"{n:g}{suffix}" if n < 100 else f"{n:.0f}{suffix}"
    return f"{value:g}"


def _duration(sec: float | None) -> str:
    if sec is None:
        return "—"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _date(dt: datetime | None) -> str:
    return dt.strftime("%d.%m.%Y") if dt else "—"


def _video_link(title: str, video_id: str, urls: dict[str, str]) -> str:
    label = _e(title or video_id)
    url = urls.get(video_id)
    return f'<a href="{_e(url)}">{label}</a>' if url else label


def _thumb(video_id: str, title: str, thumbs: dict[str, str], urls: dict[str, str]) -> str:
    """Превью выброса: половина решения о клике, и оно уже собрано коллектором.

    Картинка тянется с серверов YouTube — отчёт смотрят локально, а без сети
    браузер просто покажет пустую ячейку."""
    src = thumbs.get(video_id)
    if not src:
        return "—"
    img = f'<img class="thumb" src="{_e(src)}" alt="{_e(title)}" loading="lazy">'
    url = urls.get(video_id)
    return f'<a href="{_e(url)}">{img}</a>' if url else img


def _money(value: float) -> str:
    return f"{value:.2f}" if value < 10 else f"{int(value):,}".replace(",", " ")


def _revenue(views: float, group: str, rpm: dict[str, tuple[float, float]]) -> str:
    """Вилка дохода: просмотры × RPM-допущение. Не данные — оценка."""
    if group not in rpm:
        return "—"
    low, high = rpm[group]
    return f"${_money(views / 1000 * low)}–{_money(views / 1000 * high)}"


def _effective_rpm(
    config: AnalysisConfig, classify: ClassifyReport | None
) -> tuple[dict[str, tuple[float, float]], str]:
    """Чей RPM берём: своя вилка из analysis.yaml → оценка LLM из classify → дефолт."""
    if config.rpm_usd:
        return config.rpm_usd, "rpm_usd из analysis.yaml"
    if classify is not None and classify.taxonomy.rpm_suggestion:
        return classify.taxonomy.rpm_suggestion, "оценка LLM для этой ниши (classify)"
    return DEFAULT_RPM, "допущение по умолчанию — уточнится после classify"


# ---------------------------------------------------------------- график


def _chart_svg(rows: list[VideoStats], urls: dict[str, str]) -> str:
    dated = [r for r in rows if r.published_at is not None and r.views > 0]
    if len(dated) < 2:
        return ""
    width, height = 860, 300
    left, right, top, bottom = 56, 12, 14, 26
    plot_w, plot_h = width - left - right, height - top - bottom

    t0 = min(r.published_at for r in dated if r.published_at)
    t1 = max(r.published_at for r in dated if r.published_at)
    span = max((t1 - t0).total_seconds(), 1.0)
    lo = math.floor(math.log10(max(min(r.views for r in dated), 1)))
    hi = math.ceil(math.log10(max(r.views for r in dated)))
    hi = max(hi, lo + 1)

    def x(dt: datetime) -> float:
        return left + (dt - t0).total_seconds() / span * plot_w

    def y(views: float) -> float:
        return top + (hi - math.log10(max(views, 1.0))) / (hi - lo) * plot_h

    parts: list[str] = []
    for power in range(lo, hi + 1):  # горизонтальная сетка по степеням десятки
        gy = y(10.0**power)
        parts.append(
            f'<line class="gridline" x1="{left}" y1="{gy:.1f}" x2="{left + plot_w}" y2="{gy:.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{left - 8}" y="{gy + 4:.1f}" '
            f'text-anchor="end">{_views_short(10.0**power)}</text>'
        )
    for k in range(4):  # четыре подписи дат; крайние прижаты внутрь, чтобы не обрезались
        dt = t0 + (t1 - t0) * (k / 3)
        anchor = "start" if k == 0 else ("end" if k == 3 else "middle")
        parts.append(
            f'<text class="tick" x="{x(dt):.1f}" y="{height - 8}" '
            f'text-anchor="{anchor}">{dt.strftime("%m.%Y")}</text>'
        )

    by_group: dict[str, list[VideoStats]] = {}
    for r in dated:
        by_group.setdefault(r.group, []).append(r)
    for group, items in sorted(by_group.items()):
        cls = _SERIES_CLASS.get(group, "s1")
        pts = " ".join(
            f"{x(r.published_at):.1f},{y(r.rolling_median_views):.1f}"
            for r in items
            if r.published_at and r.rolling_median_views > 0
        )
        if pts:
            parts.append(f'<polyline class="median {cls}" points="{pts}"/>')
    top_outliers = {r.video_id for r in sorted(dated, key=lambda r: -r.ratio)[:3] if r.outlier}
    for group, items in sorted(by_group.items()):
        cls = _SERIES_CLASS.get(group, "s1")
        for r in items:
            assert r.published_at is not None
            cx, cy = x(r.published_at), y(r.views)
            radius = 6 if r.outlier else 4
            outlier_cls = " outlier" if r.outlier else ""
            tip = (
                f"{r.title or r.video_id} · {_date(r.published_at)} · "
                f"{_views(r.views)} просмотров · ×{r.ratio:g}"
            )
            dot = (
                f'<circle class="dot {cls}{outlier_cls}" cx="{cx:.1f}" cy="{cy:.1f}" '
                f'r="{radius}"><title>{_e(tip)}</title></circle>'
            )
            url = urls.get(r.video_id)
            parts.append(f'<a href="{_e(url)}">{dot}</a>' if url else dot)
            if r.video_id in top_outliers:
                flip = cx > left + plot_w - 48  # у правого края подпись уходит влево от точки
                lx, anchor = (cx - 9, ' text-anchor="end"') if flip else (cx + 9, "")
                parts.append(
                    f'<text class="lbl"{anchor} x="{lx:.1f}" y="{cy + 4:.1f}">×{r.ratio:g}</text>'
                )

    legend = ""
    if len(by_group) > 1:
        swatches = "".join(
            f'<span><span class="dotmark {_SERIES_CLASS.get(g, "s1")}"></span>'
            f"{_e(GROUP_TITLES.get(g, g))}</span>"
            for g in sorted(by_group)
        )
        legend = f'<div class="legend">{swatches}<span>· · · скользящая медиана</span></div>'
    svg = (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Просмотры видео по датам, лог-шкала">{"".join(parts)}</svg>'
    )
    return f'<div class="viz">{legend}{svg}</div>'


def _formula_html(formula: Formula) -> str:
    """Формула контента: наблюдения, структура, заголовки, идеи."""

    def bullets(items: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>" if items else ""

    steps = "".join(
        f"<tr><td class='num'>{i}</td><td>{_e(s.step)}</td><td>{_e(s.purpose)}</td></tr>"
        for i, s in enumerate(formula.content_formula, 1)
    )
    steps_table = (
        f"<table><tr><th>#</th><th>бит</th><th>зачем</th></tr>{steps}</table>" if steps else ""
    )
    titles = "".join(
        f"<tr><td>{_e(t.pattern)}</td><td>{_e(t.example)}</td></tr>" for t in formula.title_formulas
    )
    titles_table = (
        f"<table><tr><th>формула заголовка</th><th>пример</th></tr>{titles}</table>"
        if titles
        else ""
    )
    advice = "".join(
        f"<tr><td>{_e(a.format)}</td><td>{_e(a.verdict)}</td><td>{_e(a.why)}</td></tr>"
        for a in formula.format_advice
    )
    advice_table = (
        f"<table><tr><th>формат</th><th>вердикт</th><th>почему</th></tr>{advice}</table>"
        if advice
        else ""
    )
    ideas = "".join(
        f"<li><b>{_e(v.title)}</b> ({_e(v.format)}) — хук: {_e(v.hook)}. {_e(v.outline)}</li>"
        for v in formula.video_ideas
    )
    ideas_html = (
        f"<details><summary>идеи по формуле ({len(formula.video_ideas)})</summary>"
        f"<ul>{ideas}</ul></details>"
        if ideas
        else ""
    )
    notes = (
        f'<p class="meta">производство: {_e(formula.production_notes)}</p>'
        if formula.production_notes
        else ""
    )
    caveats = (
        f'<p class="meta">ограничения: {_e("; ".join(formula.caveats))}</p>'
        if formula.caveats
        else ""
    )
    hooks = bullets([f"хук: {h}" for h in formula.hook_rules])
    return (
        f'<div class="viz">{bullets(formula.what_works)}{steps_table}{hooks}'
        f"{titles_table}{advice_table}{notes}{ideas_html}{caveats}</div>"
    )


# ---------------------------------------------------------------- таблицы


def _stages_html(manifest: Manifest) -> str:
    rows = []
    for ws in load_workflow(manifest.workflow).stages:
        rec = manifest.stages.get(ws.name, StageRecord())
        gate = "—"
        if ws.gate_after:
            gate = "одобрено" if ws.name in manifest.approvals else "ворота"
        rows.append(
            f'<tr><td>{_e(ws.name)}</td><td><span class="badge {_e(rec.status)}">'
            f"{_e(rec.status)}</span></td>"
            f'<td class="num">${manifest.stage_cost(ws.name):.3f}</td><td>{_e(gate)}</td>'
            f"<td>{_e(rec.error or rec.message)}</td></tr>"
        )
    return (
        "<table><tr><th>стадия</th><th>статус</th><th>потрачено</th>"
        "<th>ворота</th><th>сообщение</th></tr>" + "".join(rows) + "</table>"
    )


def _threshold_note(stats: StatsReport) -> str:
    """Какой порог применён к каждой группе и откуда он взялся."""
    per_group = " · ".join(
        f"{GROUP_TITLES.get(name, name)} ×{gs.outlier_threshold:g}"
        for name, gs in sorted(stats.groups.items())
        if gs.outlier_threshold
    )
    if stats.threshold == "auto":
        source = "порог авто, по разбросу самого канала (медиана ratio + 3σ по MAD)"
    else:
        source = f"порог задан вручную: ×{stats.threshold:g}"
    return f'<p class="meta">{source}{" — " + per_group if per_group else ""}</p>'


def _outliers_html(
    stats: StatsReport,
    urls: dict[str, str],
    rpm: dict[str, tuple[float, float]],
    thumbs: dict[str, str],
) -> str:
    note = _threshold_note(stats)
    if not stats.outliers:
        return f"<p class='meta'>выбросов нет</p>{note}"
    by_id = {r.video_id: r for r in stats.videos}
    rows = []
    baited = False
    for i, o in enumerate(stats.outliers, start=1):
        row = by_id.get(o.video_id)
        published = row.published_at if row else None
        duration = row.duration_sec if row else None
        # комментариев на лайк: подпись «напиши слово в комментарии» поднимает
        # вовлечение, не поднимая содержания — такой хит нельзя брать за образец
        engagement = "—"
        if row and row.engagement is not None:
            mark = " ⚑" if row.engagement_outlier else ""
            engagement = f"{row.engagement:.2f}{mark}"
            baited = baited or row.engagement_outlier
        rows.append(
            f'<tr><td class="num">{i}</td><td class="num">×{o.ratio:g}</td>'
            f"<td>{_thumb(o.video_id, o.title, thumbs, urls)}</td>"
            f'<td class="num">{_views(o.views)}</td>'
            f'<td class="num">{_revenue(o.views, o.group, rpm)}</td>'
            f'<td class="num">{_e(engagement)}</td>'
            f"<td>{_e(GROUP_TITLES.get(o.group, o.group))}</td>"
            f"<td>{_date(published)}</td><td class='num'>{_duration(duration)}</td>"
            f"<td>{_video_link(o.title, o.video_id, urls)}</td></tr>"
        )
    bait_note = (
        "<p class='meta'>⚑ — комментариев на лайк втрое выше нормы этого канала: "
        "вовлечение поднято призывом комментировать, а не содержанием. "
        "Такой ролик — плохой образец для производственного брифа.</p>"
        if baited
        else ""
    )
    return (
        f"<table><tr><th>#</th><th>ratio</th><th>превью</th><th>просмотры</th>"
        f"<th>доход (оценка)</th><th>комм/лайк</th><th>группа</th><th>дата</th>"
        f"<th>длина</th><th>видео</th></tr>{''.join(rows)}</table>{bait_note}{note}"
    )


def _groups_html(
    stats: StatsReport, urls: dict[str, str], rpm: dict[str, tuple[float, float]]
) -> str:
    titles = {r.video_id: r.title for r in stats.videos}
    views_sum: dict[str, int] = {}
    for r in stats.videos:
        views_sum[r.group] = views_sum.get(r.group, 0) + r.views
    rows = []
    for name, gs in sorted(stats.groups.items()):
        top = _video_link(titles.get(gs.top_video_id, gs.top_video_id), gs.top_video_id, urls)
        rows.append(
            f'<tr><td><span class="dotmark {_SERIES_CLASS.get(name, "s1")}"></span>'
            f"{_e(GROUP_TITLES.get(name, name))}</td>"
            f'<td class="num">{gs.count}</td><td class="num">{_views(gs.median_views)}</td>'
            f'<td class="num">{_views(gs.mean_views)}</td>'
            f'<td class="num">{_views(gs.max_views)}</td>'
            f'<td class="num">×{gs.outlier_threshold:g}</td>'
            f'<td class="num">{_revenue(views_sum.get(name, 0), name, rpm)}</td>'
            f"<td>{top}</td></tr>"
        )
    return (
        "<table><tr><th>группа</th><th>видео</th><th>медиана</th><th>среднее</th>"
        "<th>максимум</th><th>порог выброса</th><th>доход (оценка)</th>"
        "<th>лучшее видео</th></tr>" + "".join(rows) + "</table>"
    )


def _rpm_note(stats: StatsReport, rpm: dict[str, tuple[float, float]], source: str) -> str:
    """Дисклеймер: откуда взялись цифры дохода и почему это только вилка."""
    if not rpm:
        return ""
    assumptions = " · ".join(
        f"{GROUP_TITLES.get(g, g)} ${low:g}–{high:g}" for g, (low, high) in sorted(rpm.items())
    )
    total_low = total_high = 0.0
    for r in stats.videos:
        if r.group in rpm:
            low, high = rpm[r.group]
            total_low += r.views / 1000 * low
            total_high += r.views / 1000 * high
    total = ""
    if total_high > 0:
        total = (
            f" Суммарно за собранные {len(stats.videos)} видео: "
            f"≈ ${_money(total_low)}–{_money(total_high)}."
        )
    return (
        f'<p class="meta">Доход — грубая оценка: просмотры × RPM ({source}; '
        f"за 1000 просмотров: {assumptions}).{total} Реальный RPM и сам факт "
        f"монетизации YouTube не раскрывает — точные цифры видит только владелец канала.</p>"
    )


def _videos_html(stats: StatsReport, urls: dict[str, str], formats: dict[str, str]) -> str:
    fmt_head = "<th>формат</th>" if formats else ""
    rows = []
    for r in reversed(stats.videos):  # свежие сверху
        cls = ' class="outlier"' if r.outlier else ""
        fmt_cell = f"<td>{_e(formats.get(r.video_id, '—'))}</td>" if formats else ""
        rows.append(
            f"<tr{cls}><td>{_date(r.published_at)}</td>"
            f"<td>{_e(GROUP_TITLES.get(r.group, r.group))}</td>{fmt_cell}"
            f'<td class="num">{_views(r.views)}</td><td class="num">×{r.ratio:g}</td>'
            f'<td class="num">{_duration(r.duration_sec)}</td>'
            f"<td>{_video_link(r.title, r.video_id, urls)}</td></tr>"
        )
    table = (
        f"<table><tr><th>дата</th><th>группа</th>{fmt_head}<th>просмотры</th><th>ratio</th>"
        "<th>длина</th><th>видео</th></tr>" + "".join(rows) + "</table>"
    )
    return f"<details><summary>все видео ({len(stats.videos)})</summary>{table}</details>"


def _classify_html(cls: ClassifyReport, urls: dict[str, str], config: AnalysisConfig) -> str:
    """Ниша, форматы и подсказка по RPM из таксономии."""
    tax = cls.taxonomy
    meta = f'<p class="meta">ниша: <b>{_e(tax.niche)}</b></p>'
    if tax.audience:
        meta += f'<p class="meta">аудитория: {_e(tax.audience)}</p>'
    if tax.notes:
        meta += f'<p class="meta">{_e(tax.notes)}</p>'
    total = len(cls.videos) or 1
    rows = []
    for f in cls.formats:
        top = _video_link("", f.top_video_id, urls) if f.top_video_id else "—"
        rows.append(
            f"<tr><td>{_e(f.name or f.format)}</td>"
            f'<td class="num">{f.count}</td><td class="num">{f.count / total:.0%}</td>'
            f'<td class="num">{_views(f.median_views)}</td>'
            f'<td class="num">{_views(f.mean_views)}</td><td>{top}</td></tr>'
        )
    table = (
        "<table><tr><th>формат</th><th>видео</th><th>доля</th><th>медиана</th>"
        "<th>среднее</th><th>лучшее видео</th></tr>" + "".join(rows) + "</table>"
    )

    rpm_hint = ""
    if tax.rpm_suggestion:
        suggested = " · ".join(
            f"{GROUP_TITLES.get(g, g)} ${low:g}–{high:g}"
            for g, (low, high) in sorted(tax.rpm_suggestion.items())
        )
        if config.rpm_usd and config.rpm_usd != tax.rpm_suggestion:
            rpm_hint = (
                f'<p class="meta">RPM ниши по оценке LLM: {suggested} за 1000 просмотров; '
                f"в analysis.yaml задана своя вилка — в оценке дохода используется она.</p>"
            )
        else:
            rpm_hint = (
                f'<p class="meta">RPM ниши по оценке LLM: {suggested} за 1000 просмотров — '
                f"по нему считается доход в отчёте; своя вилка — rpm_usd в analysis.yaml.</p>"
            )
    return meta + table + rpm_hint


def _deep_html(
    analyses: list[DeepVideoAnalysis],
    stats: StatsReport | None,
    urls: dict[str, str],
    titles: dict[str, str],
) -> str:
    """Карточки разбора: хук цитатой, структура и крючки — в details."""
    rows = {r.video_id: r for r in stats.videos} if stats else {}
    cards = []
    for a in analyses:
        row = rows.get(a.video_id)
        perf = f" · ×{row.ratio:g} · {_views(row.views)} просмотров" if row else ""
        beats = "".join(
            f"<tr><td class='num'>{_e(b.t)}</td><td>{_e(b.beat)}</td><td>{_e(b.purpose)}</td></tr>"
            for b in a.structure
        )
        gaps = "; ".join(a.curiosity_gaps)
        gaps_html = f"<p class='meta'>крючки любопытства: {_e(gaps)}</p>" if gaps else ""
        takeaways = "".join(f"<li>{_e(t)}</li>" for t in a.takeaways)
        cta = _e(a.cta) if a.cta else "—"
        cards.append(
            f'<div class="viz">'
            f"<p><b>{_video_link(titles.get(a.video_id, ''), a.video_id, urls)}</b>{perf}</p>"
            f"<p>«{_e(a.hook.text)}» — хук: {_e(a.hook.type)}. {_e(a.hook.why_it_works)}</p>"
            f'<p class="meta">наррация: {_e(a.narration_style)} · CTA: {cta}</p>'
            f"<details><summary>структура ({len(a.structure)} битов)</summary>"
            f"<table><tr><th>t</th><th>бит</th><th>зачем</th></tr>{beats}</table>"
            f"{gaps_html}</details>"
            f"<ul>{takeaways}</ul></div>"
        )
    return "".join(cards)


def _visual_html(
    analyses: list[VisualVideoAnalysis],
    urls: dict[str, str],
    titles: dict[str, str],
    analysis_dir: Path,
) -> str:
    """Карточки визуального разбора: измеренный ритм + что в кадре + сами кадры."""
    cards = []
    for a in analyses:
        s = a.shots
        density = " → ".join(f"{d:g}" for d in s.density)
        measured = (
            f'<p class="meta">план в среднем <b>{s.mean_shot_sec:g} с</b> '
            f"(медиана {s.median_shot_sec:g}, длинные до {s.p90_shot_sec:g}) · "
            f"планов {s.shot_count} · склеек в минуту <b>{s.cuts_per_min:g}</b>"
            + (f" · по пятым долям: {density}" if density else "")
            # порог детекта задаёт все числа выше: без него они не читаются
            + (f" · порог смены картинки {s.threshold:g}" if s.threshold else "")
            + "</p>"
        )
        moves = ", ".join(a.camera_moves)
        staging = "".join(f"<li>{_e(p)}</li>" for p in a.staging_patterns)
        transferable = "".join(f"<li>{_e(t)}</li>" for t in a.transferable)
        thumb = a.thumbnail
        thumb_html = ""
        if thumb.elements or thumb.why_clickable:
            thumb_html = (
                f'<p class="meta">обложка: {_e(thumb.elements)}'
                + (f" · надпись «{_e(thumb.text)}»" if thumb.text else "")
                + (f" · {_e(thumb.why_clickable)}" if thumb.why_clickable else "")
                + "</p>"
            )
        frames = sorted((analysis_dir / VISUAL_DIR / a.video_id).glob("frame_*.jpg"))
        strip = "".join(
            f'<img class="keyframe" src="{VISUAL_DIR}/{_e(a.video_id)}/{_e(f.name)}" '
            f'alt="{_e(f.stem)}" loading="lazy">'
            for f in frames
        )
        cards.append(
            f'<div class="viz">'
            f"<p><b>{_video_link(titles.get(a.video_id, ''), a.video_id, urls)}</b></p>"
            f"{measured}"
            f"<p>{_e(a.shot_language)}</p>"
            f'<p class="meta">камера: {_e(moves) or "—"} · палитра: {_e(a.palette)}</p>'
            f'<p class="meta">текст в кадре: {_e(a.text_on_screen)}</p>'
            f"{thumb_html}"
            + (f'<div class="strip">{strip}</div>' if strip else "")
            + (
                f"<details><summary>подача чисел и сравнений</summary><ul>{staging}</ul></details>"
                if staging
                else ""
            )
            + f"<ul>{transferable}</ul></div>"
        )
    return "".join(cards)


def _shot_recipe_html(formula: Formula) -> str:
    """Производственные числа отдельным блоком — их переносят в project.yaml."""
    r = formula.shot_recipe
    if not (r.scene_duration_sec or r.cuts_per_min or r.rhythm):
        return ""
    bits = []
    if r.scene_duration_sec:
        bits.append(
            f"длина клипа <b>{r.scene_duration_sec:g} с</b> (<code>scene_duration_sec</code>)"
        )
    if r.shot_range_sec:
        bits.append(f"разброс планов {_e(r.shot_range_sec)} с")
    if r.cuts_per_min:
        bits.append(f"склеек в минуту <b>{r.cuts_per_min:g}</b>")
    moves = f"<p class='meta'>камера: {_e(', '.join(r.camera_moves))}</p>" if r.camera_moves else ""
    staging = "".join(f"<li>{_e(s)}</li>" for s in r.staging)
    return (
        f'<div class="viz"><p>{" · ".join(bits)}</p>'
        f"<p>{_e(r.rhythm)}</p>{moves}" + (f"<ul>{staging}</ul>" if staging else "") + "</div>"
    )


# ---------------------------------------------------------------- сборка


def render_analysis_report(analysis_dir: Path) -> Path:
    config = load_analysis_from_dir(analysis_dir)
    manifest = load_manifest(analysis_dir)

    data: ChannelData | None = None
    if channel_path(analysis_dir).exists():
        data = read_model(channel_path(analysis_dir), ChannelData)
    stats: StatsReport | None = None
    stats_path = analysis_dir / STATS_DIR / STATS_FILE
    if stats_path.exists():
        stats = read_model(stats_path, StatsReport)
    classify: ClassifyReport | None = None
    if classify_path(analysis_dir).exists():
        classify = read_model(classify_path(analysis_dir), ClassifyReport)
    deep_analyses: list[DeepVideoAnalysis] = []
    if deep_index_path(analysis_dir).exists():
        deep = read_model(deep_index_path(analysis_dir), DeepReport)
        for entry in deep.videos:
            path = analysis_dir / DEEP_DIR / entry.video_id / ANALYSIS_FILE
            if entry.status == "done" and path.exists():
                deep_analyses.append(read_model(path, DeepVideoAnalysis))
    visual_analyses: list[VisualVideoAnalysis] = []
    if visual_index_path(analysis_dir).exists():
        for v_entry in read_model(visual_index_path(analysis_dir), VisualReport).videos:
            v_path = analysis_dir / VISUAL_DIR / v_entry.video_id / "visual.json"
            if v_entry.status == "done" and v_path.exists():
                visual_analyses.append(read_model(v_path, VisualVideoAnalysis))
    formula: Formula | None = None
    if formula_path(analysis_dir).exists():
        formula = read_model(formula_path(analysis_dir), Formula)
    urls = {v.id: v.url for v in data.videos if v.url} if data else {}
    video_titles = {v.id: v.title for v in data.videos} if data else {}
    thumbs = {v.id: v.thumbnail_url for v in data.videos if v.thumbnail_url} if data else {}

    banner = ""
    if manifest.awaiting_approval:
        banner = (
            f'<div class="banner">стадия «{_e(manifest.awaiting_approval)}» ждёт одобрения: '
            f"videoforge analyze approve {_e(config.id)} {_e(manifest.awaiting_approval)}</div>"
        )
    channel_meta = ""
    if data is not None:
        ch = data.channel
        subs = f" · подписчиков: <b>{_views(ch.subscribers)}</b>" if ch.subscribers else ""
        total = f" · видео на канале: {ch.video_count}" if ch.video_count else ""
        channel_meta = (
            f'<p class="meta">канал <a href="{_e(ch.url)}">{_e(ch.title or ch.url)}</a>{subs}'
            f"{total} · собрано: {len(data.videos)} видео ({_date(ch.collected_at)}, "
            f"{_e(ch.collector)})</p>"
        )
    goal = f'<p class="meta">цель: {_e(config.goal)}</p>' if config.goal else ""

    body = ""
    if stats is not None:
        cadence = ""
        if stats.cadence_per_week:
            cadence = f'<p class="meta">публикаций в неделю: <b>{stats.cadence_per_week:g}</b></p>'
        format_names = {}
        classify_section = ""
        if classify is not None:
            names = {f.id: (f.name or f.id) for f in classify.taxonomy.formats}
            format_names = {c.video_id: names.get(c.format, c.format) for c in classify.videos}
            classify_section = f"<h2>Ниша и форматы</h2>{_classify_html(classify, urls, config)}"
        deep_section = ""
        if deep_analyses:
            deep_section = "<h2>Разбор лучших видео</h2>" + _deep_html(
                deep_analyses, stats, urls, video_titles
            )
        visual_section = ""
        if visual_analyses:
            visual_section = "<h2>Как это снято и смонтировано</h2>" + _visual_html(
                visual_analyses, urls, video_titles, analysis_dir
            )
        formula_section = ""
        if formula is not None:
            blueprint = analysis_dir / FORMULA_DIR / BLUEPRINT_FILE
            link = (
                f'<p class="meta">производственный бриф: '
                f'<a href="{FORMULA_DIR}/{BLUEPRINT_FILE}">{_e(blueprint)}</a></p>'
                if blueprint.exists()
                else ""
            )
            formula_section = (
                f"<h2>Формула контента</h2>{_formula_html(formula)}"
                f"{_shot_recipe_html(formula)}{link}"
            )
        rpm, rpm_source = _effective_rpm(config, classify)
        body = (
            f"<h2>Динамика просмотров</h2>{_chart_svg(stats.videos, urls)}{cadence}"
            f"<h2>Выбросы (просмотры против медианы соседей)</h2>"
            f"{_outliers_html(stats, urls, rpm, thumbs)}"
            f"{classify_section}"
            f"{formula_section}"
            f"{deep_section}"
            f"{visual_section}"
            f"<h2>Группы</h2>{_groups_html(stats, urls, rpm)}"
            f"{_rpm_note(stats, rpm, rpm_source)}"
            f"{_videos_html(stats, urls, format_names)}"
        )
    elif data is not None:
        body = "<p class='meta'>статистики ещё нет — запусти стадию stats</p>"
    else:
        body = "<p class='meta'>данных ещё нет — запусти videoforge analyze run</p>"

    page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>анализ {_e(config.id)}</title>
<style>{_CSS}</style></head>
<body><main>
<h1>Анализ: {_e(config.id)}</h1>
<p class="meta">режим {_e(config.mode)} · {_e(", ".join(config.urls))}</p>
{channel_meta}
{goal}
<p class="money">бюджет <b>${manifest.budget_usd:.2f}</b> ·
потрачено <b>${manifest.spent_usd:.3f}</b></p>
{banner}
{_stages_html(manifest)}
{body}
</main></body></html>"""
    out = analysis_dir / REPORT_FILE
    out.write_text(page, encoding="utf-8")
    return out
