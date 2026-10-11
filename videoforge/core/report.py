"""HTML-отчёт эпизода: раскадровка по сценам, статусы, расходы — один файл.

Просмотрщик, не интерфейс управления: читает те же storyboard.json и
manifest.json, что и стадии, ничего не меняет. Файл кладётся в папку эпизода,
поэтому ссылки на активы (кадры, клипы, озвучка) — относительные и работают
локально без сервера.
"""

from __future__ import annotations

import html
from pathlib import Path

from videoforge.core.editplan import EditPlan, SceneEdit, load_plan
from videoforge.core.manifest import Manifest, StageRecord
from videoforge.core.qa import CutReview, FrameReview, QaReport, load_qa
from videoforge.core.storyboard import Scene, Storyboard, is_filmed
from videoforge.core.workflow import load_workflow

REPORT_FILE = "report.html"
# подписи видов плашек (Scene.overlay) — одни на отчёт и пульт
OVERLAY_TITLES: dict[str, str] = {
    "caption": "плашка",
    "source": "источник",
    "label": "ярлык",
}

_CSS = """
:root { --bg:#f6f8fa; --card:#fff; --ink:#1f2328; --muted:#57606a; --line:#d0d7de; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --card:#161b22; --ink:#e6edf3; --muted:#8b949e; --line:#30363d; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--ink);
       font:14px/1.5 system-ui, "Segoe UI", sans-serif; }
main { max-width: 900px; margin: 0 auto; }
h1 { font-size:20px; margin:0 0 4px; }
.meta, .money { color:var(--muted); margin:0 0 8px; }
.money b { color:var(--ink); }
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
.scene { background:var(--card); border:1px solid var(--line); border-radius:8px;
         padding:12px 16px; margin:12px 0; }
.scene header { display:flex; gap:12px; align-items:baseline; flex-wrap:wrap; }
.scene .cost { margin-left:auto; color:var(--muted); }
.scene .row { display:flex; gap:16px; margin-top:8px; align-items:flex-start; }
.scene img { max-width:220px; border-radius:6px; border:1px solid var(--line); }
.narration { margin:4px 0; }
.overlay { margin:4px 0; color:var(--muted); }
.overlay .badge { background:#d4a72c33; color:#7d4e00; }
details { margin-top:6px; }
summary { cursor:pointer; color:var(--muted); }
dl { display:grid; grid-template-columns:max-content 1fr; gap:2px 12px; margin:8px 0 0; }
dt { color:var(--muted); }
dd { margin:0; overflow-wrap:anywhere; }
video { max-width:100%; border-radius:8px; margin-top:8px; }
a { color:#0969da; }

/* лист раскадровки: весь эпизод одним экраном — ритм и пробелы видно глазами,
   а не вычитыванием JSON. Ячейка = бит, ширина = длительность */
.sheet { display:grid; grid-template-columns:repeat(auto-fill, minmax(128px, 1fr));
         gap:8px; margin:12px 0; }
.cell { background:var(--card); border:1px solid var(--line); border-radius:6px;
        overflow:hidden; display:flex; flex-direction:column; }
.cell .pic { aspect-ratio:9/16; background:var(--bg); display:flex; align-items:center;
             justify-content:center; overflow:hidden; }
.cell .pic img { width:100%; height:100%; object-fit:cover; border:none; border-radius:0;
                 max-width:none; }
.cell .pic span { color:var(--muted); font-size:11px; padding:6px; text-align:center; }
.cell .cap { padding:5px 7px; font-size:11px; line-height:1.35; }
.cell .cap b { font-variant-numeric:tabular-nums; }
.cell .cap .t { color:var(--muted); }
.cell .cap p { margin:3px 0 0; color:var(--muted);
               display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical;
               overflow:hidden; }
.cell .stripe { height:3px; background:var(--line); }
.cell .stripe.pass { background:#1a7f37; }
.cell .stripe.fail { background:#cf222e; }
.cell .parts { color:var(--muted); }
.legend { color:var(--muted); margin:0 0 8px; }

/* монтажный лист: что сделал монтаж с каждой сценой, без пересмотра ролика */
.montage td { vertical-align: top; }
.thumbs { display:flex; gap:4px; }
.thumbs img { width:120px; border-radius:4px; border:1px solid var(--line); }
.montage .why { color:var(--muted); font-size:12px; margin-top:2px; }
ul.qa li.info { color:var(--muted); }
/* кадры внутри эпизода: на что смотрел QA там, где стыков нет */
.spots { display:flex; gap:8px; flex-wrap:wrap; margin:12px 0; }
.spots figure { margin:0; max-width:160px; }
.spots img { width:100%; border-radius:6px; border:1px solid var(--line); }
.spots figcaption { font-size:11px; color:var(--muted); margin-top:4px; }
.checks td.ok { color:#1a7f37; }
.checks td.bad { color:#cf222e; font-weight:600; }
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _badge(label: str, status: str) -> str:
    return f'<span class="badge {_e(status)}">{_e(label)}: {_e(status)}</span>'


def _usd(value: float) -> str:
    return f"${value:.3f}"


def _asset_links(scene: Scene) -> str:
    links = [
        f'<a href="{_e(path)}">{_e(kind)}</a>'
        for kind in ("image", "video", "audio", "captions", "clip")
        if (path := getattr(scene.assets, kind))
    ]
    return " · ".join(links) or "—"


def _attempts_html(scene: Scene) -> str:
    """Попытки актива, если их было больше одной: какая прошла, у какой сколько
    замечаний и где её файл. Выбранная помечена — на воротах человек видит,
    почему в кадре именно она, и может взять другую руками по пути."""
    rows = []
    for kind, log in scene.retried().items():
        kept = getattr(scene.assets, kind, None)
        items = []
        for a in log:
            verdict = "прошла" if a.passed else f"{len(a.issues)} замеч."
            mark = " ← выбрана" if a.path == kept else ""
            items.append(
                f'v{a.n:02d} — {_e(verdict)}, <a href="{_e(a.path)}">{_e(a.path)}</a>{mark}'
            )
        rows.append(f"<dt>попытки {_e(kind)}</dt><dd>{'; '.join(items)}</dd>")
    return "".join(rows)


def _scene_html(scene: Scene) -> str:
    checks = _badge("img", scene.checks.image) + _badge("vid", scene.checks.video)
    checks += _badge("aud", scene.checks.audio)
    thumb = f'<img src="{_e(scene.assets.image)}" loading="lazy">' if scene.assets.image else ""
    narration = f'<p class="narration">«{_e(scene.narration)}»</p>' if scene.narration else ""
    # плашка — то, что зритель прочитает с экрана; на воротах после планнера её
    # судят вместе с репликой, поэтому она на виду, а не в «деталях»
    overlay = ""
    if scene.overlay is not None:
        marks = f" · цветом: {', '.join(scene.overlay.emphasis)}" if scene.overlay.emphasis else ""
        overlay = (
            f'<p class="overlay"><span class="badge">{_e(OVERLAY_TITLES[scene.overlay.kind])}'
            f"</span> {_e(scene.overlay.text)}{_e(marks)}</p>"
        )
    cast = ", ".join(scene.cast) or "никого (пейзаж, предмет, символ)"
    attempts = " · ".join(f"{k}: {v}" for k, v in scene.attempts.items()) or "—"
    provenance = " · ".join(f"{k}: {v}" for k, v in scene.provenance.items()) or "—"
    notes = "; ".join(f"{k}: {v}" for k, v in scene.checks.notes.items())
    notes_row = f"<dt>замечания</dt><dd>{_e(notes)}</dd>" if notes else ""
    measured = " · ".join(f"{k}: {v:.1f}s" for k, v in scene.measured_sec.items())
    measured_row = f"<dt>факт. длительность</dt><dd>{_e(measured)}</dd>" if measured else ""
    # проверка по описанию и то, чем чинили: референс композиции — ссылкой на
    # файл, правки к описанию — как ушли в промпт
    review_row = ""
    if (rv := scene.review.get("image")) is not None:
        what = f"{rv.severity}: видно — {rv.see or '—'}"
        if rv.issue:
            what += f"; не так — {rv.issue}"
        if rv.fix:
            what += f"; чинить — {rv.fix}"
        review_row = f"<dt>по описанию</dt><dd>{_e(what)}</dd>"
    refs_row = ""
    if scene.reference_assets:
        links = " · ".join(f'<a href="{_e(r)}">{_e(r)}</a>' for r in scene.reference_assets)
        refs_row = f"<dt>референс</dt><dd>{links}</dd>"
    fixes_row = f"<dt>правки</dt><dd>{_e('; '.join(scene.fixes))}</dd>" if scene.fixes else ""
    return f"""
<section class="scene">
  <header>
    <b>#{scene.order} {_e(scene.id)}</b>
    <span>{scene.duration_sec:g}s</span>
    {checks}
    <span class="cost">{_usd(sum(scene.cost_usd.values()))}</span>
  </header>
  <div class="row">
    {thumb}
    <div>
      {narration}
      {overlay}
      <details>
        <summary>промпты и детали</summary>
        <dl>
          <dt>visual</dt><dd>{_e(scene.visual_prompt) or "—"}</dd>
          <dt>motion</dt><dd>{_e(scene.motion_prompt) or "—"}</dd>
          <dt>каст</dt><dd>{_e(cast)}</dd>
          <dt>попытки</dt><dd>{_e(attempts)}</dd>
          {_attempts_html(scene)}
          <dt>провайдеры</dt><dd>{_e(provenance)}</dd>
          {measured_row}
          {notes_row}
          {review_row}
          {refs_row}
          {fixes_row}
          <dt>активы</dt><dd>{_asset_links(scene)}</dd>
        </dl>
      </details>
    </div>
  </div>
</section>"""


def _beats(scenes: list[Scene]) -> list[list[Scene]]:
    """Сцены, сгруппированные в биты: продолжения одного плана идут в свою группу."""
    beats: list[list[Scene]] = []
    for scene in scenes:
        if scene.continues and beats:
            beats[-1].append(scene)
        else:
            beats.append([scene])
    return beats


def _cell_html(beat: list[Scene]) -> str:
    head = beat[0]
    seconds = sum(s.duration_sec for s in beat)
    if head.assets.image:
        pic = f'<img src="{_e(head.assets.image)}" loading="lazy" alt="">'
    else:
        # до стадии картинок ячейка пустая — и это ровно то, что надо видеть
        # на воротах после планнера: форма эпизода читается по тексту кадра
        pic = f"<span>{_e(head.visual_prompt[:110]) or 'кадра ещё нет'}</span>"
    parts = f' <span class="parts">×{len(beat)}</span>' if len(beat) > 1 else ""
    text = head.narration or head.visual_prompt
    if head.overlay is not None:
        text = f"[{head.overlay.text}] {text}"
    status = head.checks.image if head.checks.image in ("pass", "fail") else ""
    return (
        f'<div class="cell"><div class="stripe {_e(status)}"></div>'
        f'<div class="pic">{pic}</div>'
        f'<div class="cap"><b>#{head.order}</b> <span class="t">{seconds:g}s</span>{parts}'
        f"<p>{_e(text[:120])}</p></div></div>"
    )


def _sheet_html(storyboard: Storyboard) -> str:
    """Лист раскадровки — весь эпизод одним экраном.

    Ворота после планнера человек проходит, глядя на форму эпизода: где густо,
    где длинный held shot, где повторяется один и тот же кадр. По JSON это не
    читается, а по листу видно сразу — так на такие листы и смотрят режиссёры."""
    beats = _beats(storyboard.ordered_scenes())
    if not beats:
        return ""
    seconds = sum(s.duration_sec for s in storyboard.scenes)
    long_beats = sum(1 for b in beats if sum(s.duration_sec for s in b) >= 15)
    cells = "".join(_cell_html(b) for b in beats)
    return (
        f"<h2>Лист раскадровки</h2>"
        f'<p class="legend">битов: {len(beats)} · кадров: {len(storyboard.scenes)} · '
        f"хронометраж ~{seconds:.0f} с · длинных планов (≥15 с): {long_beats} · "
        f"«×N» — один план, разрезанный на N клипов по потолку провайдера</p>"
        f'<div class="sheet">{cells}</div>'
    )


def _plan_cell(edit: SceneEdit | None) -> str:
    """Что план монтажа решил для сцены и почему. Без `why` ворота после
    сборки — театр: судить план по JSON человек не будет."""
    if edit is None:
        return "—"
    bits: list[str] = []
    tr = edit.transition_in
    if tr.type != "cut":
        bits.append(f"{tr.type} {tr.duration_sec:g}s")
    if edit.emphasis:
        bits.append("акцент: " + ", ".join(f"«{w}»" for w in edit.emphasis))
    if edit.music_gain_db is not None:
        bits.append(f"музыка {edit.music_gain_db:+g} дБ")
    if not edit.captions:
        bits.append("без субтитров")
    if len(edit.segments) > 1:
        bits.append(f"отрезков: {len(edit.segments)}")
    text = _e(" · ".join(bits)) if bits else "—"
    why = f'<div class="why">{_e(edit.why)}</div>' if edit.why else ""
    return text + why


def _timeline_html(plan: EditPlan | None) -> str:
    """Эффекты, звуки, ступени музыки и замедленный хвост — по секундам ролика.

    Отдельной строкой от сцен, потому что они и живут не в сценах: удар стоит
    НА склейке, пролёт — до неё, дроп музыки — посреди реплики. Человеку на
    воротах нужно видеть их в одном порядке со временем, чтобы сверить с тем,
    что он услышал."""
    if plan is None:
        return ""
    rows: list[tuple[float, str, str, str]] = []
    for e in plan.effects:
        what = f"{e.kind} · {e.duration_sec:g}s · ×{e.strength:g}"
        rows.append((e.at_sec, "эффект", what, e.why))
    for s in plan.sfx:
        gain = f" · {s.gain_db:g} дБ" if s.gain_db is not None else ""
        rows.append((s.at_sec, "звук", f"{s.id}{gain}", s.why))
    for c in plan.music.cues if plan.music else []:
        rows.append((c.at_sec, "музыка", f"{c.gain_db:g} дБ", c.why))
    if not rows and plan.outro is None:
        return ""
    body = "".join(
        f'<tr><td class="num">{at:.1f}s</td><td>{_e(kind)}</td><td>{_e(what)}</td>'
        f"<td>{_e(why) or '—'}</td></tr>"
        for at, kind, what, why in sorted(rows)
    )
    if plan.outro is not None:
        o = plan.outro
        what = f"последние {o.take_sec:g}s × {o.speed:g}"
        if o.pitch_drop:
            what += ", голос ниже"
        body += (
            f'<tr><td class="num">конец</td><td>замедление</td><td>{_e(what)}</td>'
            f"<td>{_e(o.why) or '—'}</td></tr>"
        )
    return (
        '<table class="montage"><tr><th>секунда</th><th>что</th><th>подробности</th>'
        "<th>почему</th></tr>" + body + "</table>"
    )


def _montage_html(storyboard: Storyboard, plan: EditPlan | None) -> str:
    """Монтажный лист: по строке на собранную сцену и таблица замеров финала.

    Ворота после сборки человек проходит по этому листу: где заморозка, где
    кроп, сколько реплик субтитров, музыка ли, что решил план и почему, — и по
    таблице «замер / порог», а не пересматривая ролик целиком и не вычитывая
    JSON."""
    final = storyboard.final
    if not final.video:
        return ""
    scenes = [s for s in storyboard.ordered_scenes() if s.assets.clip]
    rows = []
    for s in scenes:
        thumbs = "".join(f'<img src="{_e(t)}" loading="lazy" alt="">' for t in s.assets.thumbs)
        frozen = s.measured_sec.get("frozen", 0.0)
        crop = s.montage.cropped_share
        flags = []
        if frozen:
            flags.append(f"заморозка {frozen:.1f}s")
        if s.montage.freeze_sec >= 0.2:
            flags.append(f"застыл (детектор) {s.montage.freeze_sec:.1f}s")
        if s.montage.black_sec >= 0.2:
            flags.append(f"тёмный {s.montage.black_sec:.1f}s")
        if s.montage.silence_sec >= 0.2:
            flags.append(f"тишина {s.montage.silence_sec:.1f}s")
        if crop > 0.02:
            flags.append(f"кроп {crop:.0%}")
        rows.append(
            f"<tr><td><b>#{s.order}</b> {_e(s.id)}</td>"
            f'<td><div class="thumbs">{thumbs}</div></td>'
            f'<td class="num">{s.measured_sec.get("clip", s.duration_sec):.1f}s</td>'
            f'<td class="num">{s.montage.shots}</td>'
            f"<td>{_e(s.montage.transition_in)}</td>"
            f'<td class="num">{s.montage.caption_events}</td>'
            f"<td>{'да' if s.montage.music else '—'}</td>"
            f"<td>{_plan_cell(plan.scene(s.id) if plan else None)}</td>"
            f"<td>{_e('; '.join(flags)) or '—'}</td></tr>"
        )
    checks = final.checks(
        captions_expected=final.captions is not None,
        music_expected=final.music_track is not None,
        filmed=is_filmed(scenes),
        fps_expected=storyboard.fps,
    )
    check_rows = "".join(
        f'<tr><td>{_e(c.label)}</td><td class="{"ok" if c.ok else "bad"}">{_e(c.value)}</td>'
        f"<td>{_e(c.target)}</td></tr>"
        for c in checks
    )
    bad = sum(1 for c in checks if not c.ok)
    verdict = "все замеры в порогах" if not bad else f"вне порогов: {bad}"
    author = final.edit_author
    if plan is not None and plan.author == "llm" and plan.llm:
        author += f" ({plan.llm})"
    return (
        "<h2>Монтажный лист</h2>"
        f'<p class="legend">план: {_e(author)} · склеек: {final.cuts} · '
        f"переходов (не hard cut): {final.transitions} · "
        f"реплик субтитров: {final.caption_events} · плашек: {final.overlay_events} · "
        f"музыка: {_e(final.music_track or 'нет')} · "
        f"подложка: {_e(final.bed_track or 'нет')} · "
        f"знак: {_e(final.watermark or 'нет')} · {_e(verdict)}</p>"
        + _timeline_html(plan)
        + '<table class="montage"><tr><th>сцена</th><th>кадры</th><th>длина</th><th>планов</th>'
        "<th>вход</th><th>субтитров</th><th>музыка</th><th>план монтажа / почему</th>"
        "<th>заметки</th></tr>" + "".join(rows) + "</table>"
        '<table class="checks"><tr><th>замер</th><th>значение</th><th>порог</th></tr>'
        + check_rows
        + "</table>"
    )


def _cut_verdict(cut: CutReview) -> str:
    parts = []
    if cut.seam == "visible":
        parts.append("шов виден")
    if cut.captions == "hard_to_read":
        parts.append("субтитры плохо читаются")
    elif cut.captions == "covers_subject":
        parts.append("субтитры закрывают предмет")
    return ", ".join(parts) or "—"


def _qa_html(qa: QaReport | None) -> str:
    """QA финала: список замечаний и стыки с вердиктом модели — два кадра по
    сторонам склейки. Совет, не приговор: решает человек на воротах."""
    if qa is None:
        return ""
    items = "".join(f'<li class="{_e(f.severity)}">{_e(f.message)}</li>' for f in qa.findings)
    rows = []
    for c in qa.cuts:
        if c.clean:
            continue
        thumbs = "".join(f'<img src="{_e(t)}" loading="lazy" alt="">' for t in c.frames)
        how = f" · {_e(c.transition)}" if c.transition != "cut" else ""
        rows.append(
            f"<tr><td>{_e(c.prev)} → {_e(c.next)}<br>"
            f"<span class='t'>{c.at_sec:.1f}s{how}</span></td>"
            f'<td><div class="thumbs pair">{thumbs}</div></td>'
            f"<td>{_e(_cut_verdict(c))}</td><td>{_e(c.note) or '—'}</td></tr>"
        )
    table = (
        '<table class="montage"><tr><th>стык</th><th>до / после</th><th>вердикт</th>'
        "<th>заметка</th></tr>" + "".join(rows) + "</table>"
        if rows
        else ""
    )
    # кадры внутри эпизода показываем ВСЕ, а не только с замечаниями: их мало, и
    # человеку полезно видеть, на что модель вообще смотрела
    spots = "".join(
        f'<figure><img src="{_e(f.frame)}" loading="lazy" alt="">'
        f"<figcaption>{_e(f.scene_id or '—')} · {f.at_sec:.0f}s<br>"
        f"<span class='t'>{_e(_frame_verdict(f))}</span>"
        + (f"<br>{_e(f.note)}" if f.note else "")
        + "</figcaption></figure>"
        for f in qa.frames
    )
    return (
        "<h2>QA финала</h2>"
        f'<p class="legend">стыков проверено: {len(qa.cuts)} · кадров внутри: '
        f"{len(qa.frames)} · замечаний: {len(qa.warnings)} · "
        f"модель: {_e(qa.llm or '—')} · совет, не приговор: решает человек на воротах</p>"
        + (f'<ul class="qa">{items}</ul>' if items else '<p class="legend">замечаний нет</p>')
        + table
        + (f'<div class="spots">{spots}</div>' if spots else "")
    )


def _frame_verdict(frame: FrameReview) -> str:
    parts = [f"субтитры: {frame.captions}"]
    if frame.framing != "ok":
        parts.append(f"кадр: {frame.framing}")
    return " · ".join(parts)


def _stages_html(manifest: Manifest, estimates: dict[str, float | None] | None) -> str:
    rows = []
    for ws in load_workflow(manifest.workflow).stages:
        rec = manifest.stages.get(ws.name, StageRecord())
        gate = "—"
        if ws.gate_after:
            gate = "одобрено" if ws.name in manifest.approvals else "ворота"
        forecast = "—"
        if estimates is not None and ws.name in estimates:
            cost = estimates[ws.name]
            forecast = _usd(cost) if cost is not None else "не реализована"
        rows.append(
            f"<tr><td>{_e(ws.name)}</td><td>{_badge('статус', rec.status)}</td>"
            f'<td class="num">{_usd(manifest.stage_cost(ws.name))}</td>'
            f'<td class="num">{_e(forecast)}</td><td>{_e(gate)}</td>'
            f"<td>{_e(rec.error or rec.message)}</td></tr>"
        )
    return (
        "<table><tr><th>стадия</th><th>статус</th><th>потрачено</th>"
        "<th>прогноз</th><th>ворота</th><th>сообщение</th></tr>" + "".join(rows) + "</table>"
    )


def render_report(
    episode_dir: Path,
    storyboard: Storyboard,
    manifest: Manifest,
    *,
    estimates: dict[str, float | None] | None = None,
) -> Path:
    """Собрать report.html в папке эпизода и вернуть путь к нему."""
    ref = f"{storyboard.project_id}/{storyboard.episode_id}".rstrip("/")
    title = f" — {storyboard.title}" if storyboard.title else ""
    banner = ""
    if manifest.awaiting_approval:
        banner = (
            f'<div class="banner">стадия «{_e(manifest.awaiting_approval)}» ждёт одобрения: '
            f"videoforge approve {_e(ref)} {_e(manifest.awaiting_approval)}</div>"
        )
    forecast = ""
    if estimates is not None:
        remaining = sum(cost for cost in estimates.values() if cost)
        forecast = f" · осталось по прогнозу <b>{_usd(remaining)}</b>"
    final = ""
    if storyboard.final.video:
        result = storyboard.final
        measured = [f"{result.duration_sec or 0:.1f}s"]
        if result.frozen_sec:
            share = result.frozen_sec / max(result.duration_sec or 1.0, 0.001)
            measured.append(f"замороженный кадр: {result.frozen_sec:.1f}s ({share:.0%})")
        if result.av_drift_sec is not None:
            measured.append(f"звук минус картинка: {result.av_drift_sec:+.3f}s")
        if result.fps is not None:
            measured.append(f"{result.fps:g} к/с")
        if result.loudness_lufs is not None:
            measured.append(f"{result.loudness_lufs:.1f} LUFS")
        if result.true_peak_dbtp is not None:
            measured.append(f"пик {result.true_peak_dbtp:.1f} dBTP")
        final = (
            f"<h2>Итог</h2><p><a href='{_e(storyboard.final.video)}'>"
            f"{_e(storyboard.final.video)}</a> "
            f"({_e(' · '.join(measured))})</p>"
            f"<video controls preload='none' src='{_e(storyboard.final.video)}'></video>"
            f"{_montage_html(storyboard, load_plan(episode_dir))}"
            f"{_qa_html(load_qa(episode_dir))}"
        )
    scenes = "".join(_scene_html(s) for s in storyboard.ordered_scenes())
    page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(ref)}{_e(title)}</title>
<style>{_CSS}</style></head>
<body><main>
<h1>{_e(ref)}{_e(title)}</h1>
<p class="meta">workflow {_e(manifest.workflow)} · {_e(storyboard.aspect_ratio)} ·
язык {_e(storyboard.language)} · сцен: {len(storyboard.scenes)}</p>
<p class="money">бюджет <b>{_usd(manifest.budget_usd)}</b> ·
потрачено <b>{_usd(manifest.spent_usd)}</b>{forecast}</p>
{banner}
{_stages_html(manifest, estimates)}
{_sheet_html(storyboard)}
<h2>Сцены</h2>
{scenes}
{final}
</main></body></html>"""
    out = episode_dir / REPORT_FILE
    out.write_text(page, encoding="utf-8")
    return out
