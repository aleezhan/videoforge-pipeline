"""HTML-отчёт выбора моментов: `sources/<id>/highlights.html`.

Просмотрщик, как `core/report.py`: читает те же `source.json` и
`highlights.json`, ничего не меняет. Человек смотрит его, чтобы выбрать
моменты (`videoforge source pick`), — поэтому наверху график пересмотров с
отметками кандидатов и глав, а в таблице у каждого кандидата видно и чем он
цепляет, и что python подвинул, и текст целиком.
"""

from __future__ import annotations

import html
from pathlib import Path

from videoforge.clips.highlights import Candidate, Highlights, format_clock, load_highlights
from videoforge.clips.source import SourceInfo, load_source

REPORT_FILE = "highlights.html"

_CSS = """
:root { --bg:#f6f8fa; --card:#fff; --ink:#1f2328; --muted:#57606a; --line:#d0d7de;
        --accent:#0969da; --heat:#cf222e; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --card:#161b22; --ink:#e6edf3; --muted:#8b949e; --line:#30363d;
          --accent:#4493f8; --heat:#f85149; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--ink);
       font:14px/1.5 system-ui, "Segoe UI", sans-serif; }
main { max-width: 1000px; margin: 0 auto; }
h1 { font-size:20px; margin:0 0 4px; }
h2 { font-size:16px; margin:24px 0 8px; }
.meta { color:var(--muted); margin:0 0 8px; }
.banner { background:#fff8c5; border:1px solid #d4a72c66; color:#4d2d00;
          padding:8px 12px; border-radius:8px; margin:12px 0; }
@media (prefers-color-scheme: dark) { .banner { background:#3a2d00; color:#f0d58c; } }
.banner ul { margin:4px 0 0; padding-left:20px; }
table { border-collapse:collapse; width:100%; background:var(--card);
        border:1px solid var(--line); border-radius:8px; margin:12px 0; }
th, td { text-align:left; padding:8px 10px; border-top:1px solid var(--line);
         vertical-align:top; }
tr:first-child > * { border-top:none; }
th { color:var(--muted); font-weight:600; }
.num { text-align:right; font-variant-numeric: tabular-nums; white-space:nowrap; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px;
         background:var(--line); color:var(--ink); }
.hid { font-variant-numeric:tabular-nums; font-weight:600; }
.hook { margin:0; }
.tags { color:var(--accent); font-size:12px; margin:2px 0 0; }
.why { color:var(--muted); font-size:13px; margin:2px 0 0; }
details { margin-top:6px; }
summary { cursor:pointer; color:var(--muted); }
details p { margin:6px 0 0; color:var(--muted); }
.bar { display:block; height:8px; border-radius:4px; background:var(--line); min-width:40px; }
.bar i { display:block; height:100%; border-radius:4px; background:var(--heat); }
.viz { background:var(--card); border:1px solid var(--line); border-radius:8px;
       padding:12px; margin:12px 0; }
.viz svg { width:100%; height:auto; display:block; }
.viz .heat { fill:var(--heat); fill-opacity:0.35; }
.viz .chapter { stroke:var(--line); stroke-width:1; }
.viz .cand { fill:var(--accent); }
.viz .tick { fill:var(--muted); font-size:11px; }
.viz .lbl { fill:var(--accent); font-size:10px; font-weight:600; }
.legend { color:var(--muted); margin:0 0 6px; }
a { color:var(--accent); }
tr:target { outline:2px solid var(--accent); }
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _chart_svg(info: SourceInfo, candidates: list[Candidate]) -> str:
    """Тепловая карта пересмотров с отметками глав и кандидатов.

    Один экран отвечает на вопрос, ради которого карту и качали: попали ли
    выбранные моменты туда, куда зрители возвращались."""
    total = info.duration_sec
    if total <= 0 or not (info.heatmap or candidates):
        return ""
    width, height = 940, 150
    left, right, top = 8, 8, 10
    strip, axis = 22, 20  # полоса кандидатов и подписи времени
    plot_w = width - left - right
    plot_h = height - top - strip - axis

    def x(t: float) -> float:
        return left + max(0.0, min(t / total, 1.0)) * plot_w

    parts: list[str] = []
    peak = max((h.value for h in info.heatmap), default=1.0) or 1.0
    for h in info.heatmap:
        bar_h = h.value / peak * plot_h
        parts.append(
            f'<rect class="heat" x="{x(h.start_sec):.1f}" y="{top + plot_h - bar_h:.1f}" '
            f'width="{max(x(h.end_sec) - x(h.start_sec) - 0.5, 0.5):.1f}" height="{bar_h:.1f}"/>'
        )
    for ch in info.chapters:
        parts.append(
            f'<line class="chapter" x1="{x(ch.start_sec):.1f}" y1="{top}" '
            f'x2="{x(ch.start_sec):.1f}" y2="{top + plot_h}"><title>{_e(ch.title)}</title></line>'
        )
    base = top + plot_h + 6
    for c in candidates:
        w = max(x(c.end_sec) - x(c.start_sec), 2.0)
        tip = f"{c.id} · {format_clock(c.start_sec)} · {c.duration_sec:.0f} с · {c.title}"
        parts.append(
            f'<a href="#{_e(c.id)}"><rect class="cand" x="{x(c.start_sec):.1f}" y="{base}" '
            f'width="{w:.1f}" height="8" rx="2"><title>{_e(tip)}</title></rect>'
            f'<text class="lbl" x="{x(c.start_sec):.1f}" y="{base + 20}">{_e(c.id)}</text></a>'
        )
    for k in range(5):
        t = total * k / 4
        anchor = "start" if k == 0 else ("end" if k == 4 else "middle")
        parts.append(
            f'<text class="tick" x="{x(t):.1f}" y="{height - 2}" '
            f'text-anchor="{anchor}">{format_clock(t)}</text>'
        )
    # Карты нет — сказать это словами. Пустой график без полос читается как
    # «никто ничего не пересматривал», а у локального файла карты не бывает
    # вовсе, и heat кандидатов там честный ноль, а не измеренная тишина
    if info.heatmap:
        heat_legend = "полосы — как часто пересматривали (YouTube), "
    else:
        why = "локальный файл" if info.file and not info.url else "YouTube её не отдал"
        heat_legend = f"карты пересмотров нет ({why}) — heat у кандидатов не измерен; "
    legend = (
        f'<div class="legend">{heat_legend}'
        "вертикали — границы глав, синие отрезки — кандидаты</div>"
    )
    svg = (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Пересмотры по времени видео и выбранные моменты">{"".join(parts)}</svg>'
    )
    return f'<div class="viz">{legend}{svg}</div>'


def _rows(candidates: list[Candidate], top_heat: float) -> str:
    rows: list[str] = []
    for c in candidates:
        share = (c.heat / top_heat * 100) if top_heat > 0 else 0.0
        heat = (
            f'<span class="bar"><i style="width:{share:.0f}%"></i></span>'
            f'<span class="num">{c.heat:.2f}</span>'
            if top_heat > 0
            else "—"
        )
        turns = f" · смен говорящего {c.speaker_turns}" if c.speaker_turns else ""
        tags = (
            "<p class='tags'>" + " ".join(f"#{_e(t)}" for t in c.hashtags) + "</p>"
            if c.hashtags
            else ""
        )
        desc = f'<p class="why">{_e(c.description)}</p>' if c.description else ""
        held = f'<p class="why">держит: {_e(c.verdict_why)}</p>' if c.verdict_why else ""
        # подпись над кадром и выброшенные реплики (video.headline, video.tighten)
        if c.headline:
            desc = f'<p class="hook">над кадром: {_e(c.headline)}</p>' + desc
        if c.pieces:
            cut = "".join(f"<li>{_e(t)}</li>" for t in c.dropped_text)
            held += (
                f'<p class="why">кусков {len(c.pieces)}, в шортсе {c.played_sec:.0f} с из '
                f"{c.duration_sec:.0f}</p><details><summary>выброшено</summary><ul>{cut}</ul>"
                "</details>"
            )
        rows.append(
            f'<tr id="{_e(c.id)}"><td class="hid">{_e(c.id)}</td>'
            f'<td class="num">{_e(format_clock(c.start_sec))}<br>'
            f'<span class="why">{c.duration_sec:.0f} с</span></td>'
            f"<td>{heat}</td>"
            f"<td><b>{_e(c.publish_title or c.title) or '—'}</b>"
            f"{f' <span class=badge>{_e(c.kind)}</span>' if c.kind else ''}"
            f'<p class="hook">{_e(c.hook)}</p>'
            f"{desc}{tags}{held}"
            f'<p class="why">{_e(c.chapter)}{turns}</p>'
            f"<details><summary>текст момента</summary><p>{_e(c.text)}</p></details></td></tr>"
        )
    return "".join(rows)


def _rejected_html(candidates: list[Candidate]) -> str:
    """Не попавшие в список — с причиной и текстом. Человек вправе не
    согласиться: момент никуда не делся, `source pick` возьмёт его по id.

    Закрытый критиком и не влезший в запрошенное число — разные вещи, и в
    таблице это должно быть видно: второй прошёл и критика, и длину, у него
    отличие одно — heat ниже, чем у дюжины сверху."""
    if not candidates:
        return ""
    rows = "".join(
        f'<tr id="{_e(c.id)}"><td class="hid">{_e(c.id)}</td>'
        f'<td class="num">{_e(format_clock(c.start_sec))}<br>'
        f'<span class="why">{c.duration_sec:.0f} с</span></td>'
        f"<td><b>{_e(c.title) or '—'}</b>"
        f'<p class="why">{"не влез в список" if c.verdict == "keep" else "закрыт"}: '
        f"{_e(c.verdict_why) or '—'}</p>"
        f"<details><summary>текст момента</summary><p>{_e(c.text)}</p></details></td></tr>"
        for c in candidates
    )
    overflow = sum(c.verdict == "keep" for c in candidates)
    tail = f", из них {overflow} прошли критика и не влезли в число" if overflow else ""
    return (
        f"<h2>Не в списке ({len(candidates)}{tail})</h2>"
        f'<p class="meta">Совет, а не запрет: любой из них можно взять по id.</p>'
        f"<table><tr><th>id</th><th>время</th><th>момент и причина</th></tr>{rows}</table>"
    )


def _notes(data: Highlights) -> str:
    blocks = []
    if data.warnings:
        items = "".join(f"<li>{_e(w)}</li>" for w in data.warnings)
        blocks.append(
            f'<div class="banner">Выброшено проверкой (модель отвечала {data.calls} раз):'
            f"<ul>{items}</ul></div>"
        )
    if data.fixes:
        items = "".join(f"<li>{_e(f)}</li>" for f in data.fixes)
        blocks.append(f"<details><summary>python подвинул: {len(data.fixes)}</summary>"
                      f"<ul>{items}</ul></details>")  # fmt: skip
    return "".join(blocks)


def render_highlights_report(sdir: Path) -> Path:
    """Собрать highlights.html рядом с highlights.json."""
    info = load_source(sdir)
    data = load_highlights(sdir)
    if info is None or data is None:
        raise FileNotFoundError(f"нет source.json или highlights.json в {sdir}")
    candidates = data.candidates
    top_heat = max((c.heat for c in candidates), default=0.0)
    generated = data.generated_at.strftime("%d.%m.%Y %H:%M") if data.generated_at else "—"
    picks = " ".join(c.id for c in candidates) or "<id>"
    chunks = f" · транскрипт кусками: {data.chunks}" if data.chunks > 1 else ""
    body = (
        f"<h1>Моменты: {_e(info.title or info.id)}</h1>"
        f'<p class="meta">{_e(format_clock(info.duration_sec))} · '
        f"{_e(info.channel or '—')} · глав {len(info.chapters)} · слов {info.word_count} · "
        f"кандидатов {len(candidates)} из {data.requested} · модель {_e(data.llm or '—')}"
        f"{chunks} · {_e(generated)}</p>"
        f"{_notes(data)}"
        f"{_chart_svg(info, candidates)}"
        f"<table><tr><th>id</th><th>время</th>"
        f"<th>пересмотры</th><th>момент</th></tr>"
        f"{_rows(candidates, top_heat)}</table>"
        f"{_rejected_html(data.rejected)}"
        f'<p class="meta">выбрать: <code>videoforge source pick '
        f"{_e(sdir.parent.parent.name)} {_e(info.id)} {_e(picks)}</code> · момента нет в "
        f"списке — по времени: <code>--at 20:41-21:33 --title «…»</code></p>"
    )
    page = (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Моменты: {_e(info.title or info.id)}</title>"
        f"<style>{_CSS}</style></head><body><main>{body}</main></body></html>"
    )
    path = sdir / REPORT_FILE
    path.write_text(page, encoding="utf-8")
    return path
