"""Стадия qa: готовый файл → 05_assemble/qa.json — совет человеку на воротах.

Четыре источника замечаний, от дешёвого к дорогому:

1. замеры сборки вне порогов (`FinalOutput.checks`) — уже посчитаны, здесь
   только собраны в один список с именами сцен;
2. полоса субтитров: против зоны интерфейса площадки (детерминированно, из
   чисел стиля и таблицы `PLATFORMS`) и против ЯРКОСТИ ФОНА под ней — белая
   строка на белой футболке не читается, и это измеримо, а не оценочно;
3. стыки: два кадра по обе стороны каждой склейки → vision-модель с коротким
   чеклистом (виден ли шов, читаемы ли субтитры, не закрывают ли предмет).
   Пакетами по несколько стыков, чтобы не платить временем за вызов на каждый;
4. кадры внутри эпизода: несколько равномерно по хронометражу, одним вызовом.
   Разбор стыков смотрит только на соседей склеек — у шортса из одной сцены
   стыков ноль, и картинку не проверял никто; нечитаемые субтитры на светлом
   кадре нашёл человек, а не конвейер. Середина длинной сцены — та же слепая
   зона, просто менее заметная.

Стадия ничего не переделывает: измеряемый дефект чинится правилом рендерера
или линта, а не перепланированием по вердикту модели, который плывёт между
прогонами. Свежесть — по отпечатку сборки: тот же final.mp4 и та же модель →
отчёт переиспользуется, но статус всё равно `done`: за этой стадией ворота, и
пропуск их не открыл бы.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videoforge.core.captions import caption_band, overlay_view, overlay_window
from videoforge.core.editplan import EditPlan, default_plan, load_plan
from videoforge.core.ffmpeg import FfmpegError, aspect_size, band_luminance, frame_at
from videoforge.core.io import write_model
from videoforge.core.project import PLATFORMS
from videoforge.core.qa import (
    ASSEMBLE_DIR,
    QA_FRAMES_DIR,
    CutReview,
    FrameReview,
    QaFinding,
    QaReport,
    load_qa,
    qa_path,
)
from videoforge.core.resources import CaptionStyle
from videoforge.core.stage import Stage, StageContext, StageResult
from videoforge.core.storyboard import Scene, is_filmed
from videoforge.providers.base import ProviderError
from videoforge.stages.assemble import (
    TIMELINE_FILE,
    caption_style_for,
    check_places,
    overlay_style_for,
    watermark_for,
)
from videoforge.stages.llm_json import ask_json
from videoforge.stages.reframe import SEAM_SHARE, clip_seam, clip_stacked_spans

STEP_MARK = "VIDEOFORGE-STEP: qa"
SYSTEM = "You are a picture editor doing the final quality pass on a narrated video."
# кадр до и после стыка — соседи склейки, не она сама. Замерено 04.09.2026: при
# 0.12 с кадр «после» ловил субтитр в момент появления (\fad), и модель звала
# его нечитаемым; у переходов отступ идёт от края перехода, не от склейки
FRAME_OFFSET_SEC = 0.25
CUTS_PER_CALL = 6  # 12 кадров на вызов: claude -p читает каждый файл отдельно
SUMMARY_FINDINGS = 5  # сколько замечаний попадает в сообщение стадии; остальное — в отчёте
# Кадров внутри эпизода на один вызов. Четыре — потому что дефект вроде
# нечитаемых субтитров зависит от того, что под ними: на тёмном кадре тот же
# стиль читается, на светлом нет, и одной точки мало. Больше не берём: это
# страховка от слепой зоны, а не сплошной просмотр
SPOT_FRAMES = 4
SPOT_EDGE_SHARE = 0.08  # края эпизода не берём: там fade in/out, а не картинка
# Контраст текста субтитров к фону под ними. 3:1 — порог WCAG AA для крупного
# текста, ниже него замечание; 4.5:1 — обычный порог для текста, между ними
# «читается, но впритык» — там человек решает сам. Смотрим долю хронометража, а
# не отдельный кадр: одна светлая вспышка ничего не значит
# Обводка тоньше 4% кегля глазом уже не держит букву на пёстром фоне, а её
# контраст к тексту — это и есть граница глифа: чёрная обводка под белым текстом
# даёт 21:1 в любом кадре. Числа — из практики субтитров под видео, а не из
# веб-стандарта: тот писан про мелкий текст абзацами на ровной странице
OUTLINE_SHARE_MIN = 0.04
OUTLINE_CONTRAST_MIN = 4.5
CAPTION_CONTRAST_MIN = 3.0
CAPTION_CONTRAST_COMFORT = 4.5
CAPTION_LOW_CONTRAST_SHARE = 0.15
CAPTION_TIGHT_SHARE = 0.3

PROMPT = (
    STEP_MARK
    + """
You review the seams of a finished narrated video. For each cut below you get
two frames: the last frame BEFORE the cut and the first frame AFTER it (file
names given in that order). Subtitles are burned in. A cut marked with a
transition (crossfade, dip_black, fade_in) is a designed soft join: its frames
are taken outside the transition — judge the compositions on both sides, not
the join itself.

CUTS:
{cuts}

For every cut answer:
- seam: "ok" or "visible". Visible means the join draws attention to itself:
  the same composition repeated (jump cut), a black or corrupted frame, the
  subject jumps without a change of shot, scale or palette changes with no
  motivation.
- captions: "ok", "hard_to_read" (low contrast, too small, cut off at the
  edge), "covers_subject" (text over a face or over the object the narration
  is about), or "none" if neither frame has captions.
- note: one short sentence in {language}, only when something is not ok.

Return ONLY JSON:
{{"cuts": [{{"cut": "s01>s02", "seam": "ok", "captions": "ok", "note": ""}}]}}
"""
)

FRAMES_PROMPT = (
    STEP_MARK
    + """
You review single frames of a finished video, taken at different points of it.
These are not cuts: judge every frame on its own, as a viewer who sees it for
one second. Subtitles are burned in.

FRAMES (file name, scene, second):
{frames}

For every frame answer:
- captions: "ok", "hard_to_read" (low contrast against what is behind them, too
  small, cut off at the edge), "covers_subject" (text over a face or over the
  object the shot is about), or "none" if the frame has no captions.
- framing: "ok", "subject_cut" (the person or object the shot is about is cut
  by the edge of the frame, or only part of a face is visible), or "no_subject"
  (the frame shows nothing the viewer can hold on to).
- ad: "betting" if a gambling advertisement is visible anywhere in the frame —
  a bookmaker or casino logo, a betting promo banner or video, betting odds —
  otherwise "none". Say in the note where in the frame it is.
- note: one short sentence in {language}, only when something is not ok.

Return ONLY JSON:
{{"frames": [{{"frame": "spot01.jpg", "captions": "ok", "framing": "ok",
  "ad": "none", "note": ""}}]}}
"""
)


class QaStage(Stage):
    name = "qa"
    needs = ("llm",)  # vision: claude_cli читает кадры по пути, gemini — как картинки

    def run(self, ctx: StageContext) -> StageResult:
        final = ctx.storyboard.final
        if not final.video or not ctx.abs(final.video).exists():
            return StageResult(status="failed", message="нет итогового видео — сначала assemble")
        llm = ctx.providers.llm.name
        previous = load_qa(ctx.project_dir)
        if (
            previous is not None
            and not ctx.options.force
            and previous.final_fingerprint == final.inputs_fingerprint
            and previous.llm == llm
        ):
            return StageResult(status="done", message="QA актуален: " + _summary(previous))

        scenes = ctx.storyboard.ordered_scenes()
        timeline = _timeline(ctx)
        with ctx.step("замеры и полоса субтитров"):
            findings = (
                _measure_findings(ctx, scenes)
                + _caption_findings(ctx)
                + _overlay_findings(ctx)
                + _watermark_findings(ctx)
            )
        with ctx.step("кадры для разбора") as frames_step:
            cuts = _cut_frames(ctx, timeline, load_plan(ctx.project_dir))
            spots = _spot_frames(ctx, timeline)
            frames_step.note = f"стыков {len(cuts)}, точек {len(spots)}"
        calls, seconds = 0, 0.0
        try:
            for start in range(0, len(cuts), CUTS_PER_CALL):
                batch = cuts[start : start + CUTS_PER_CALL]
                ctx.log.info("QA стыков %d–%d из %d…", start + 1, start + len(batch), len(cuts))
                began = time.monotonic()
                with ctx.step("модель: стыки", f"{llm}, кадров {2 * len(batch)}"):
                    raw = ask_json(
                        ctx,
                        PROMPT.format(
                            cuts=_cuts_text(ctx, batch), language=ctx.storyboard.language
                        ),
                        system=SYSTEM,
                        images=[ctx.abs(f) for c in batch for f in c.frames],
                    )
                seconds += time.monotonic() - began
                calls += 1
                _apply_verdicts(batch, raw)
            if spots:
                ctx.log.info("QA кадров внутри эпизода: %d…", len(spots))
                began = time.monotonic()
                with ctx.step("модель: кадры", f"{llm}, кадров {len(spots)}"):
                    raw = ask_json(
                        ctx,
                        FRAMES_PROMPT.format(
                            frames=_frames_text(spots), language=ctx.storyboard.language
                        ),
                        system=SYSTEM,
                        images=[ctx.abs(f.frame) for f in spots],
                    )
                seconds += time.monotonic() - began
                calls += 1
                _apply_frame_verdicts(spots, raw)
        except (ProviderError, ValueError, TypeError) as exc:
            return StageResult(status="failed", message=f"QA кадров: {exc}")
        for cut in cuts:
            if not cut.clean:
                findings.append(
                    QaFinding(
                        code="cut",
                        message=f"{cut.prev}→{cut.next}: {_describe(cut)}",
                        scene_id=cut.next,
                    )
                )
        for spot in spots:
            if not spot.clean:
                where = f"сцена {spot.scene_id}, " if spot.scene_id else ""
                findings.append(
                    QaFinding(
                        code="frame",
                        message=f"{where}{spot.at_sec:.0f} с: {_describe_frame(spot)}",
                        scene_id=spot.scene_id or None,
                    )
                )
        report = QaReport(
            llm=llm,
            generated_at=datetime.now(UTC),
            final_fingerprint=final.inputs_fingerprint,
            findings=findings,
            cuts=cuts,
            frames=spots,
        )
        write_model(qa_path(ctx.project_dir), report)
        note = f" · вызовов модели {calls}, {seconds:.0f} с" if calls else ""
        return StageResult(status="done", message="QA: " + _summary(report) + note, changed=True)


# ------------------------------------------------------------------ замечания


def _summary(report: QaReport) -> str:
    warnings = report.warnings
    text = f"стыков {len(report.cuts)}, кадров {len(report.frames)}, замечаний {len(warnings)}"
    if not warnings:
        return text
    shown = "; ".join(f.message for f in warnings[:SUMMARY_FINDINGS])
    more = f" (+{len(warnings) - SUMMARY_FINDINGS})" if len(warnings) > SUMMARY_FINDINGS else ""
    return f"{text}: {shown}{more} — совет, решает человек на воротах"


def _measure_findings(ctx: StageContext, scenes: list[Scene]) -> list[QaFinding]:
    """Замеры сборки вне порогов — с именами сцен, как в сообщении сборки."""
    final = ctx.storyboard.final
    checks = final.checks(
        captions_expected=final.captions is not None,
        music_expected=final.music_track is not None,
        filmed=is_filmed(scenes),
        fps_expected=ctx.storyboard.fps,
    )
    where = check_places(final, scenes)  # та же таблица, что в сообщении сборки
    out: list[QaFinding] = []
    for c in checks:
        if c.ok:
            continue
        places = next((v for k, v in where.items() if c.label.startswith(k)), [])
        tail = f" ({', '.join(p.note for p in places)})" if places else ""
        out.append(
            QaFinding(
                code="measure",
                message=f"замер вне порога: {c.label} {c.value}, порог {c.target}{tail}",
                scene_id=places[0].scene_id if places else None,
            )
        )
    return out


def caption_zone_findings(
    style: CaptionStyle,
    size: tuple[int, int],
    platforms: list[str],
    *,
    what: str = "субтитры",
    margin_field: str = "margin_pct",
    code: str = "caption",
) -> list[QaFinding]:
    """Полоса субтитров против зоны интерфейса каждой площадки проекта.

    Те же формулы служат плашкам (`overlay_view`): `what` и `margin_field`
    называют в замечании то, что человек будет править."""
    top, bottom = caption_band(style, size)
    out: list[QaFinding] = []
    for platform in platforms:
        zone = PLATFORMS.get(platform, {}).get("ui_zone")
        if not zone:
            continue
        title = PLATFORMS[platform]["title"]
        if style.position == "bottom" and 1 - bottom < zone["bottom"]:
            out.append(
                QaFinding(
                    code=f"{code}_zone",
                    message=(
                        f"{what} в зоне интерфейса {title}: нижний край на {1 - bottom:.0%} "
                        f"высоты от низа, интерфейс занимает {zone['bottom']:.0%} — подними "
                        f"{margin_field} стиля {style.id} до ≥ {zone['bottom'] * 100:.0f}"
                    ),
                )
            )
        elif style.position == "top" and top < zone["top"]:
            out.append(
                QaFinding(
                    code=f"{code}_zone",
                    message=(
                        f"{what} в зоне интерфейса {title}: верхний край на {top:.0%} высоты, "
                        f"интерфейс занимает {zone['top']:.0%} сверху — подними {margin_field} "
                        f"стиля {style.id} до ≥ {zone['top'] * 100:.0f}"
                    ),
                )
            )
        if zone["right"] and style.side_margin_pct / 100 < zone["right"] and code == "caption":
            out.append(
                QaFinding(
                    code="caption_side",
                    severity="info",
                    message=(
                        f"длинная реплика может уйти под кнопки {title}: side_margin_pct "
                        f"{style.side_margin_pct:g}% < {zone['right']:.0%}"
                    ),
                )
            )
    return out


def stack_seam_findings(
    band: tuple[float, float],
    stacked: dict[str, list[tuple[float, float]]],
    *,
    seams: dict[str, float] | None = None,
    what: str = "субтитры",
    margin_field: str = "margin_pct",
    code: str = "caption",
) -> list[QaFinding]:
    """Полоса текста против стыка половин стека — кадрирование «двое друг над
    другом» (стык посередине, `SEAM_SHARE`) и «игра сверху, вебка снизу» (стык
    над вебкой; по сцене — `seams`, из паспорта кадрирования).

    `band` — (верх, низ) полосы в долях высоты, те же, что у `caption_band`;
    `stacked` — по сцене секунды, где кадр собран стеком (`clip_stacked_spans`).
    Строка поперёк шва лежит на двух разных картинках сразу, и читается хуже,
    чем в любой из половин; замечание, не блок — стиль решает человек."""
    top, bottom = band
    out: list[QaFinding] = []
    for scene_id, spans in stacked.items():
        seam = (seams or {}).get(scene_id, SEAM_SHARE)
        if not spans or not (top < seam < bottom):
            continue
        where = ", ".join(f"{a:.1f}–{b:.1f} с" for a, b in spans)
        out.append(
            QaFinding(
                code=f"{code}_seam",
                scene_id=scene_id,
                message=(
                    f"{what} ложатся на стык половин кадра: полоса {top:.0%}–{bottom:.0%} "
                    f"высоты, стык на {seam:.0%}; сцена {scene_id} собрана стеком в "
                    f"{where} — сдвинь {margin_field} стиля, чтобы полоса легла целиком в "
                    "одну половину"
                ),
            )
        )
    return out


def _stacked_by_scene(ctx: StageContext) -> dict[str, list[tuple[float, float]]]:
    """Где в каждой сцене кадр собран стеком — по паспортам кадрирования."""
    out: dict[str, list[tuple[float, float]]] = {}
    for scene in ctx.storyboard.scenes:
        if not scene.assets.video:
            continue
        spans = clip_stacked_spans(ctx.abs(scene.assets.video))
        if spans:
            out[scene.id] = spans
    return out


def _seams_by_scene(ctx: StageContext) -> dict[str, float]:
    """Где у каждой сцены стык верха и низа стека, доля высоты выхода."""
    return {
        scene.id: clip_seam(ctx.abs(scene.assets.video))
        for scene in ctx.storyboard.scenes
        if scene.assets.video
    }


def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    """Относительная яркость цвета по WCAG (0 — чёрный, 1 — белый)."""
    text = hex_color.lstrip("#")
    if len(text) != 6:
        return 1.0  # цвет не разобрали — считаем светлым, это осторожная сторона
    r, g, b = (int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def _srgb_gray(hex_color: str) -> float:
    """Уровень серого 0..255 в гамма-пространстве (Rec.601 по каналам sRGB) —
    то, что видит замер яркости полосы, если этот цвет залить в кадр."""
    text = hex_color.lstrip("#")
    if len(text) != 6:
        return 255.0
    r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b


def contrast_ratio(a: float, b: float) -> float:
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


def caption_contrast_finding(
    style: CaptionStyle,
    samples: list[tuple[float, float]],
    *,
    what: str = "субтитры",
    code: str = "caption_contrast",
    frame: tuple[int, int] | None = None,
) -> QaFinding | None:
    """Контраст субтитров к фону под ними — измерение, а не мнение.

    `samples` — (секунда, средняя яркость полосы субтитров 0..255); яркость
    сравнивается с самым светлым из цветов текста. Замер появился потому, что
    vision-модель на одном и том же ролике то называла субтитры нечитаемыми, то
    нет: на первом шортсе замер показал 3.5:1 в худшей точке и ниже 4.5:1 на 70%
    хронометража — то есть ролик целиком в пограничной зоне, чего ни один кадр
    по отдельности не говорит.

    Ниже 3:1 (WCAG AA для крупного текста) — замечание: текст почти сливается.
    Между 3:1 и 4.5:1 — не приговор, а цифра человеку: обводка спасает частично,
    и где проходит граница читаемости, решает он, глядя на свой канал."""
    if not samples:
        return None
    text = max(luminance(style.color), luminance(style.highlight))
    # YAVG — яркость в гамма-пространстве, то есть уровень серого sRGB: ровно то,
    # с чем сравнивается цвет текста. Подложка стиля лежит между текстом и
    # кадром: фон под буквами — смесь кадра и подложки по её непрозрачности.
    # Смешивать надо в гамме, как это делает рендер (libass поверх кадра), а не
    # в линейном свете: чёрное стекло 0.6 над белой футболкой (250) даёт серый
    # 100, и контраст к белому тексту 5.9:1; в линейном свете вышло бы 2.4:1 —
    # замечание на кадре, который читается без труда
    box = min(1.0, max(0.0, style.box_opacity))
    box_gray = _srgb_gray(style.box_color)

    def background(y: float) -> float:
        return _srgb_to_linear((y * (1 - box) + box_gray * box) / 255)

    # ЧИТАЕМОСТЬ ДЕРЖИТ ОБВОДКА, а не разница «буква против фона». Обводка —
    # граница буквы, и она одна и та же в каждом кадре, каким бы фон ни был.
    # Пока проверка её не знала, она горела на ВСЕХ роликах подряд: 1.7 / 2.9 /
    # 2.8 / 3.1 при пороге 4.5, и на кадрах текст при этом читался. Проверка,
    # которая срабатывает всегда, сигналом быть перестаёт.
    font_px = style.size_pct / 100 * frame[1] if frame else 0.0
    outline_share = style.outline / font_px if font_px else 0.0
    outline_ratio = contrast_ratio(text, _srgb_to_linear(_srgb_gray(style.outline_color) / 255))
    guarded = outline_share >= OUTLINE_SHARE_MIN and outline_ratio >= OUTLINE_CONTRAST_MIN

    ratios = [(t, contrast_ratio(text, background(y))) for t, y in samples]
    worst_at, worst = min(ratios, key=lambda r: r[1])
    if font_px and not guarded:
        # вот это и есть настоящий брак: буква ничем не отделена от кадра
        return QaFinding(
            code=code,
            message=(
                f"у стиля {style.id} обводка {style.outline:g} px при кегле {font_px:.0f} px "
                f"({outline_share:.1%} от буквы, контраст обводки к тексту {outline_ratio:.1f}:1) "
                f"— этого мало, чтобы {what} держались на любом фоне. Худший контраст к фону "
                f"{worst:.1f}:1 на {worst_at:.0f} с"
            ),
        )
    if guarded:
        return None  # обводка есть и достаточна — фон уже ничего не решает
    bad = [t for t, c in ratios if c < CAPTION_CONTRAST_MIN]
    if len(bad) / len(ratios) >= CAPTION_LOW_CONTRAST_SHARE:
        return QaFinding(
            code=code,
            message=(
                f"{what} почти сливаются с фоном {len(bad) / len(ratios):.0%} времени показа "
                f"(контраст ниже {CAPTION_CONTRAST_MIN:g}:1, худший {worst:.1f}:1 на "
                f"{worst_at:.0f} с) — стилю {style.id} нужна обводка потолще, подложка "
                f"или цвет темнее"
            ),
        )
    tight = sum(1 for _, c in ratios if c < CAPTION_CONTRAST_COMFORT) / len(ratios)
    if tight < CAPTION_TIGHT_SHARE:
        return None
    return QaFinding(
        code=code,
        severity="info",
        message=(
            f"контраст {what} к фону: худший {worst:.1f}:1 на {worst_at:.0f} с, ниже "
            f"{CAPTION_CONTRAST_COMFORT:g}:1 на {tight:.0%} времени показа — читается, но "
            f"впритык; толще обводка у стиля {style.id} снимет вопрос"
        ),
    )


def _caption_findings(ctx: StageContext) -> list[QaFinding]:
    final = ctx.storyboard.final
    if final.captions is None:
        return []
    plan = load_plan(ctx.project_dir) or default_plan(ctx.storyboard)
    style = caption_style_for(ctx, plan)
    if style is None:
        return []
    size = aspect_size(ctx.storyboard.aspect_ratio)
    platforms = ctx.project.channel.platforms if ctx.project else []
    out = caption_zone_findings(style, size, platforms)
    top, bottom = caption_band(style, size)
    out += stack_seam_findings((top, bottom), _stacked_by_scene(ctx), seams=_seams_by_scene(ctx))
    # меряем там, где текст вообще может быть: по центру, без полей стиля
    try:
        samples = band_luminance(
            ctx.abs(final.video or ""),
            top,
            bottom,
            width=max(0.1, 1 - 2 * style.side_margin_pct / 100),
        )
    except FfmpegError as exc:
        ctx.log.warning("яркость полосы субтитров не измерена: %s", exc)
        return out
    contrast = caption_contrast_finding(style, samples, frame=size)
    return out + ([contrast] if contrast else [])


def overlay_windows(
    scenes: list[Scene], timeline: list[dict[str, Any]]
) -> list[tuple[float, float]]:
    """Секунды финала, где нарисована плашка, — по монтажному листу сцен и окну
    самой плашки (`SceneOverlay.start_sec`/`end_sec`): хук на первые две
    секунды нарезки меряется на первых двух секундах, а не по всему ролику —
    той же функцией, которой сборка его рисовала. Окно откладывается от сцены
    в таймлайне сборки (у замедленного хвоста он уже растянут): плашка в
    самом хвосте с окном ляжет на доли секунды раньше нарисованной, хук в
    голове ролика — точно."""
    shown = {s.id: s.overlay for s in scenes if s.montage.overlay and s.overlay is not None}
    out: list[tuple[float, float]] = []
    for t in timeline:
        overlay = shown.get(str(t.get("scene_id")))
        if overlay is None:
            continue
        start = float(t.get("start", 0.0)) + float(t.get("lead", 0.0))
        window = overlay_window(start, float(t.get("end", 0.0)), overlay)
        if window is not None:
            out.append(window)
    return out


def _overlay_findings(ctx: StageContext) -> list[QaFinding]:
    """Плашки (`Scene.overlay`) — та же проверка, что у субтитров: полоса
    против зоны интерфейса и контраст к измеренной яркости фона, но только в
    те секунды, когда плашка на экране: пустой верх кадра без текста мерить
    незачем."""
    final = ctx.storyboard.final
    if not final.overlay_events:
        return []
    windows = overlay_windows(ctx.storyboard.scenes, _timeline(ctx))
    if not windows:
        return []
    plan = load_plan(ctx.project_dir) or default_plan(ctx.storyboard)
    view = overlay_view(overlay_style_for(ctx, plan))
    size = aspect_size(ctx.storyboard.aspect_ratio)
    platforms = ctx.project.channel.platforms if ctx.project else []
    out = caption_zone_findings(
        view, size, platforms, what="плашки", margin_field="overlay_margin_pct", code="overlay"
    )
    top, bottom = caption_band(view, size)
    out += stack_seam_findings(
        (top, bottom),
        _stacked_by_scene(ctx),
        seams=_seams_by_scene(ctx),
        what="плашки",
        margin_field="overlay_margin_pct",
        code="overlay",
    )
    try:
        samples = band_luminance(
            ctx.abs(final.video or ""),
            top,
            bottom,
            width=max(0.1, 1 - 2 * view.side_margin_pct / 100),
        )
    except FfmpegError as exc:
        ctx.log.warning("яркость полосы плашек не измерена: %s", exc)
        return out
    inside = [(t, y) for t, y in samples if any(a <= t <= b for a, b in windows)]
    contrast = caption_contrast_finding(view, inside, what="плашки", code="overlay_contrast")
    return out + ([contrast] if contrast else [])


def watermark_findings(
    rect: tuple[int, int, int, int],
    size: tuple[int, int],
    platforms: list[str],
    *,
    captions: tuple[float, float] | None = None,
    overlays: tuple[float, float] | None = None,
    file: str = "",
) -> list[QaFinding]:
    """Знак канала против полосы субтитров, полосы плашек и зоны интерфейса.

    `rect` — знак в пикселях финала (x, y, ширина, высота); полосы — (верх,
    низ) в долях высоты, те же, что у `caption_band`. Замечание, не блок:
    сборка ставит знак вне зоны по построению, но знак шире свободного места
    прижимается к краю, а полосу текста задаёт стиль, о знаке не зная, —
    совпасть они могут только здесь. Полоса сравнивается по высоте, без
    ширины: самая длинная реплика доходит до боковых полей стиля, то есть
    почти до углов, и «знак ниже верха полосы» уже значит «текст ляжет на
    знак»."""
    x, y, w, h = rect
    frame_w, frame_h = size
    top, bottom, right = y / frame_h, (y + h) / frame_h, (x + w) / frame_w
    what = f"водяной знак {file}".strip()
    out: list[QaFinding] = []
    for label, band in (("субтитров", captions), ("плашек", overlays)):
        if band is not None and top < band[1] and bottom > band[0]:
            out.append(
                QaFinding(
                    code="watermark_band",
                    message=(
                        f"{what} лежит в полосе {label}: знак на {top:.0%}–{bottom:.0%} высоты, "
                        f"полоса {band[0]:.0%}–{band[1]:.0%} — текст ляжет поверх знака; "
                        "смени corner или уменьши width_pct в project.yaml"
                    ),
                )
            )
    for platform in platforms:
        zone = PLATFORMS.get(platform, {}).get("ui_zone")
        if not zone:
            continue
        hits: list[str] = []
        if zone.get("top") and top < zone["top"]:
            hits.append("верхнюю полосу")
        if zone.get("bottom") and bottom > 1 - zone["bottom"]:
            hits.append("нижний блок «канал / описание / звук»")
        if zone.get("right") and right > 1 - zone["right"]:
            hits.append("колонку кнопок")
        if hits:
            out.append(
                QaFinding(
                    code="watermark_zone",
                    message=(
                        f"{what} попадает в зону интерфейса {PLATFORMS[platform]['title']}: "
                        f"{', '.join(hits)} — интерфейс его закроет; уменьши width_pct или "
                        "смени corner"
                    ),
                )
            )
    return out


def _watermark_findings(ctx: StageContext) -> list[QaFinding]:
    """Знак канала (`final.watermark`) — по тем же числам, которыми сборка его
    ставила (`watermark_for`), против полос текста, которые она же рисовала."""
    final = ctx.storyboard.final
    if not final.watermark:
        return []
    size = aspect_size(ctx.storyboard.aspect_ratio)
    mark, error = watermark_for(ctx, size)
    if mark is None:
        if error:  # файл убрали после сборки — знак в ролике есть, проверить нечем
            ctx.log.warning("водяной знак не проверен: %s", error)
        return []
    plan = load_plan(ctx.project_dir) or default_plan(ctx.storyboard)
    style = caption_style_for(ctx, plan) if final.captions else None
    captions = caption_band(style, size) if style is not None else None
    overlays = (
        caption_band(overlay_view(overlay_style_for(ctx, plan)), size)
        if final.overlay_events
        else None
    )
    platforms = ctx.project.channel.platforms if ctx.project else []
    return watermark_findings(
        (mark.x, mark.y, mark.width, mark.height),
        size,
        platforms,
        captions=captions,
        overlays=overlays,
        file=final.watermark,
    )


# ------------------------------------------------------------------ стыки


def _timeline(ctx: StageContext) -> list[dict[str, Any]]:
    path = ctx.project_dir / ASSEMBLE_DIR / TIMELINE_FILE
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return (
        [t for t in data if isinstance(t, dict) and "scene_id" in t]
        if isinstance(data, list)
        else []
    )


def _transition(ctx: StageContext, plan: EditPlan | None, scene_id: str) -> tuple[str, float]:
    """Как собран вход сцены: тип — из монтажного листа (что реально отрендерено),
    длительность — из плана, если он и есть то, что рендерили."""
    try:
        kind = ctx.storyboard.scene(scene_id).montage.transition_in
    except KeyError:
        return "cut", 0.0
    edit = plan.scene(scene_id) if plan else None
    if edit is not None and edit.transition_in.type == kind:
        return kind, edit.transition_in.duration_sec
    return kind, 0.0


def _frame_times(at: float, kind: str, duration: float) -> tuple[float, float]:
    """Секунды кадров до и после стыка — за пределами перехода, если он есть:
    полурастворённый кадр внутри crossfade модель честно называет швом."""
    off = FRAME_OFFSET_SEC
    if kind in ("crossfade", "fade_in"):
        return at - off, at + duration + off  # хвост прошлой сцены растворяется в начале этой
    if kind == "dip_black":
        return at - duration / 2 - off, at + duration / 2 + off
    return at - off, at + off


def _cut_frames(
    ctx: StageContext, timeline: list[dict[str, Any]], plan: EditPlan | None
) -> list[CutReview]:
    """Кадр до и кадр после каждой склейки — из готового файла, с субтитрами."""
    out_dir = ctx.stage_dir(ASSEMBLE_DIR) / QA_FRAMES_DIR
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob("cut*.jpg"):
        old.unlink()
    final = ctx.abs(ctx.storyboard.final.video or "")
    cuts: list[CutReview] = []
    for i in range(1, len(timeline)):
        prev, cur = timeline[i - 1], timeline[i]
        at = float(cur.get("start", 0.0)) + float(cur.get("lead", 0.0))
        kind, duration = _transition(ctx, plan, str(cur["scene_id"]))
        t_before, t_after = _frame_times(at, kind, duration)
        stem = f"cut{i:02d}_{prev['scene_id']}_{cur['scene_id']}"
        try:
            before = frame_at(final, max(0.0, t_before), out_dir / f"{stem}_before.jpg")
            after = frame_at(final, t_after, out_dir / f"{stem}_after.jpg")
        except FfmpegError as exc:
            ctx.log.warning(
                "стык %s → %s: кадры не вытащены: %s", prev["scene_id"], cur["scene_id"], exc
            )
            continue
        cuts.append(
            CutReview(
                prev=str(prev["scene_id"]),
                next=str(cur["scene_id"]),
                at_sec=round(at, 3),
                transition=kind,
                frames=[ctx.rel(before), ctx.rel(after)],
            )
        )
    return cuts


def _cuts_text(ctx: StageContext, cuts: list[CutReview]) -> str:
    lines = []
    for c in cuts:
        mark = f" ({c.transition})" if c.transition != "cut" else ""
        lines.append(f"{c.cut_id}{mark}: {Path(c.frames[0]).name}, {Path(c.frames[1]).name}")
    return "\n".join(lines)


def _apply_verdicts(batch: list[CutReview], raw: Any) -> None:
    """Вердикты модели → стыки. Незнакомый стык пропускается, пропущенный
    остаётся not_assessed: молчание модели — не «ok»."""
    data = raw.get("cuts") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        raise ValueError("ответ модели без списка cuts")
    by_id = {c.cut_id: c for c in batch}
    for item in data:
        if not isinstance(item, dict):
            continue
        cut = by_id.get(str(item.get("cut", "")))
        if cut is None:
            continue
        seam, captions = item.get("seam"), item.get("captions")
        cut.seam = seam if seam in ("ok", "visible") else "not_assessed"
        cut.captions = (
            captions
            if captions in ("ok", "hard_to_read", "covers_subject", "none")
            else "not_assessed"
        )
        cut.note = str(item.get("note") or "")[:300]


# ------------------------------------------------------------------ кадры внутри


def spot_times(
    duration: float, n: int = SPOT_FRAMES, *, edge: float = SPOT_EDGE_SHARE
) -> list[float]:
    """Секунды кадров внутри эпизода: равномерно, без краёв с fade.

    Равномерно, а не по сценам: слепая зона — это длинный кусок без склеек, и
    находится он по хронометражу, а не по числу сцен. У шортса из одной сцены
    сцена ничего не подсказывает вовсе."""
    if duration <= 0:
        return []
    lo, hi = duration * edge, duration * (1 - edge)
    if hi <= lo:
        return [duration / 2]
    return [round(lo + (hi - lo) * (k + 0.5) / n, 3) for k in range(n)]


def _scene_at(timeline: list[dict[str, Any]], t: float) -> str:
    """Какая сцена идёт в эту секунду готового файла."""
    current = ""
    for item in timeline:
        if float(item.get("start", 0.0)) <= t:
            current = str(item.get("scene_id", ""))
        else:
            break
    return current


def _spot_frames(ctx: StageContext, timeline: list[dict[str, Any]]) -> list[FrameReview]:
    out_dir = ctx.stage_dir(ASSEMBLE_DIR) / QA_FRAMES_DIR
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob("spot*.jpg"):
        old.unlink()
    final = ctx.abs(ctx.storyboard.final.video or "")
    duration = ctx.storyboard.final.duration_sec or 0.0
    frames: list[FrameReview] = []
    for i, t in enumerate(spot_times(duration), start=1):
        try:
            path = frame_at(final, t, out_dir / f"spot{i:02d}.jpg")
        except FfmpegError as exc:
            ctx.log.warning("кадр на %.1f с не вытащен: %s", t, exc)
            continue
        frames.append(FrameReview(scene_id=_scene_at(timeline, t), at_sec=t, frame=ctx.rel(path)))
    return frames


def _frames_text(frames: list[FrameReview]) -> str:
    return "\n".join(f"{f.name}: {f.scene_id or '—'}, {f.at_sec:.1f} s" for f in frames)


def _apply_frame_verdicts(frames: list[FrameReview], raw: Any) -> None:
    """Вердикты модели → кадры. Пропущенный кадр остаётся `not_assessed`:
    молчание модели — не «ok», как и на стыках."""
    data = raw.get("frames") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        raise ValueError("ответ модели без списка frames")
    by_name = {f.name: f for f in frames}
    for item in data:
        if not isinstance(item, dict):
            continue
        frame = by_name.get(Path(str(item.get("frame", ""))).name)
        if frame is None:
            continue
        captions, framing = item.get("captions"), item.get("framing")
        frame.captions = (
            captions
            if captions in ("ok", "hard_to_read", "covers_subject", "none")
            else "not_assessed"
        )
        frame.framing = (
            framing if framing in ("ok", "subject_cut", "no_subject") else "not_assessed"
        )
        ad = item.get("ad")
        frame.ad = ad if ad in ("none", "betting") else "not_assessed"
        frame.note = str(item.get("note") or "")[:300]


def _describe_frame(frame: FrameReview) -> str:
    parts = []
    if frame.captions == "hard_to_read":
        parts.append("субтитры плохо читаются")
    elif frame.captions == "covers_subject":
        parts.append("субтитры закрывают предмет")
    if frame.framing == "subject_cut":
        parts.append("кадр режет героя")
    elif frame.framing == "no_subject":
        parts.append("в кадре не за что зацепиться")
    if frame.ad == "betting":
        parts.append("в кадре реклама ставок или казино — обрезать кропом или размыть")
    text = ", ".join(parts) or "замечание"
    return f"{text} — {frame.note}" if frame.note else text


def _describe(cut: CutReview) -> str:
    parts = []
    if cut.seam == "visible":
        parts.append("шов виден")
    if cut.captions == "hard_to_read":
        parts.append("субтитры плохо читаются")
    elif cut.captions == "covers_subject":
        parts.append("субтитры закрывают предмет")
    text = ", ".join(parts) or "замечание"
    return f"{text} — {cut.note}" if cut.note else text
