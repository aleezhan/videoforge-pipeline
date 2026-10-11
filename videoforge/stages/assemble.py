"""Сборка: план монтажа → клипы сцен → склейка → финальный проход → final.mp4.

Стадия исполняет `05_edit/edit_plan.json` детерминированно: отрезки клипов в
коробке сцены, переходы на границах, акценты в субтитрах, ступени музыки. Плана
нет или он не прошёл проверку — собирает по умолчанию («монтажёр без мнения»:
hard cut везде) и говорит об этом в сообщении. Длину сцены (`Scene.duration_sec`,
её меряет озвучка) и порядок задаёт раскадровка; единственное, что план меняет
в длине, — crossfade, и он съедает только хвост тишины предыдущей сцены.

Таймлайн — измеренный, не плановый: после всех переходов каждый клип сцены
меряется, и по этим длинам считаются смещения субтитров, ступени музыки и
привязка детекторов к сценам (`05_assemble/timeline.json`).

Готовый файл меряется, а не предполагается: громкость и пик по EBU R128, уровни
голоса и музыки, чёрные и застывшие кадры, тишина, покрытие речи субтитрами. Всё
в `FinalOutput`, оттуда — в сообщение стадии и в монтажный лист отчёта.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from videoforge.core import headline as headlines
from videoforge.core.captions import (
    CaptionEvent,
    OverlayEvent,
    SceneWords,
    TranslatedCaptions,
    Word,
    build_ass,
    build_events,
    caption_band,
    overlay_window,
    parse_srt_words,
    speech_seconds,
    stretch_events,
    stretch_overlays,
    translated_events,
)
from videoforge.core.editplan import EditPlan, SceneEdit, default_plan, load_plan, plan_path
from videoforge.core.ffmpeg import (
    FPS,
    GRADES,
    XFADE_EPS,
    BedMix,
    Cut,
    Effect,
    FinalMix,
    MusicMix,
    SlowTail,
    Sound,
    Watermark,
    aspect_size,
    build_scene_video,
    concat,
    crossfade_pair,
    fade_edges,
    finalize,
    measure_final,
    mux_scene,
    probe_duration,
    probe_fps,
    probe_size,
    probe_stream_duration,
    scene_thumbs,
    slow_tail,
    watermark_xy,
)
from videoforge.core.io import read_model
from videoforge.core.pauses import TimedWord, scene_lead_in, sentence_ends, speech_pauses
from videoforge.core.project import PLATFORMS, AudioConfig, VideoConfig, WatermarkConfig
from videoforge.core.resources import CaptionStyle, MusicTrack, Sfx, load_resource
from videoforge.core.stage import Stage, StageContext, StageResult
from videoforge.core.storyboard import (
    SILENCE_GAPS_KEPT,
    FinalOutput,
    Scene,
    SilenceGap,
    is_filmed,
)
from videoforge.stages.edit import run_lint
from videoforge.stages.edit_lint import resolve_segments
from videoforge.stages.reframe import facecam_seam, fit_frame

OUT_DIR = "05_assemble"
FINAL = "final.mp4"
SCENES_FILE = "scenes.mp4"  # склейка до финального прохода
# Имя НЕ должно совпадать с именем видео. Плееры (VLC, MPC, PotPlayer) сами
# подхватывают файл субтитров, у которого то же имя, что у открытого видео, и
# рисуют их поверх уже вшитых — своим шрифтом и своего размера. Человек видит
# два текста один за другим и справедливо считает это браком сборки; в кадрах,
# извлечённых ffmpeg, ничего такого нет, потому что внешние субтитры он не
# подключает. Найдено пользователем на готовом шортсе
ASS_FILE = "captions.ass"
TIMELINE_FILE = "timeline.json"
THUMBS_DIR = "thumbs"
DEFAULT_CAPTION_STYLE = "default"
# поднять, когда меняется сам рендер (фильтры, кодеки): старые финалы устаревают
# так же, как актив при смене провайдера
# 23.09.2026: края дорожки гасятся до loudnorm, а не после; звук финала 256k —
# скупой AAC поднимал пики музыкального ролика выше нуля
RENDER_VERSION = 5
# то же, но для эффектов кадра и замедленного хвоста — их код меняется чаще, а
# ролика без эффектов не касается вовсе. Отдельное число не заставляет
# пересобирать восемь обычных шортсов ради правки в фильтре, которого у них нет.
# 1 — первый рендер эффектов, 2 — дорогие фильтры под `enable` (финальный проход
# эпичного шортса 2м27с → 39 с)
EFFECTS_VERSION = 2
# кроп больше этой доли кадра — провайдер отдал не тот формат, человек должен знать
CROP_WARN_SHARE = 0.02
# интервал детектора, перелившийся через стык на меньшее, — не про эту сцену
DETECTOR_NOTE_SEC = 0.2


def measure_av_drift(final: Path) -> float | None:
    """Насколько звук длиннее картинки в готовом файле, в секундах.

    Длительность контейнера этого не покажет — она одна на файл. Проверка
    держится за то, что склейка перекодирует: замена её на `-c copy` (первое,
    к чему тянет ради скорости) вернёт покадровый зазор контейнера на каждом
    стыке, и он копится. None — дорожку прочитать не удалось."""
    video = probe_stream_duration(final, "0:v:0")
    audio = probe_stream_duration(final, "0:a:0")
    if video is None or audio is None:
        return None
    return round(audio - video, 3)


def format_final(
    final: FinalOutput,
    scenes: list[Scene],
    *,
    captions: bool,
    music: bool,
    fps_expected: float | None = None,
) -> str:
    parts = [f"{final.duration_sec or 0:.1f}s", f"сцен: {len(scenes)}"]
    if final.fps is not None:
        parts.append(f"{final.fps:g} к/с")
    parts.append(f"план: {final.edit_author}")
    if final.transitions:
        parts.append(f"переходов: {final.transitions}")
    if final.effects or final.sfx:
        parts.append(f"эффектов: {final.effects}, звуков: {final.sfx}")
    if final.outro_added_sec:
        parts.append(f"замедленный хвост: +{final.outro_added_sec:.1f}s")
    if captions:
        parts.append(f"субтитров: {final.caption_events}")
    if final.overlay_events:
        parts.append(f"плашек: {final.overlay_events}")
    if music and final.music_track:
        parts.append(f"музыка: {final.music_track}")
    if final.bed_track:
        parts.append(f"подложка: {final.bed_track}")
    if final.watermark:
        parts.append(f"знак: {final.watermark}")
    if stitched := sum(1 for s in scenes if s.montage.zoom > 1.0):
        parts.append(f"продолжений сшито кропом: {stitched}")
    if final.loudness_lufs is not None:
        peak = f" / {final.true_peak_dbtp:.1f} dBTP" if final.true_peak_dbtp is not None else ""
        parts.append(f"{final.loudness_lufs:.1f} LUFS{peak}")
    failed = [
        c
        for c in final.checks(
            captions_expected=captions,
            music_expected=music,
            filmed=is_filmed(scenes),
            fps_expected=fps_expected,
        )
        if not c.ok
    ]
    if failed:
        where = check_places(final, scenes)
        notes = []
        for c in failed:
            places = next((v for k, v in where.items() if c.label.startswith(k)), [])
            tail = f" ({', '.join(p.note for p in places)})" if places else ""
            notes.append(f"{c.label} {c.value}{tail}")
        parts.append("ПРОВЕРЬ: " + "; ".join(notes))
    return ", ".join(parts)


class Place(NamedTuple):
    """Куда смотреть по красному замеру: сцена и подпись для человека."""

    scene_id: str
    note: str


def check_places(final: FinalOutput, scenes: list[Scene]) -> dict[str, list[Place]]:
    """Имена сцен к замерам вне порогов, по началу подписи замера.

    Одна таблица для сообщения сборки и замечаний QA — иначе они называют сцены
    по-разному. Детекторы по картинке называют сцену: тёмная сцена «пустое
    небо» — это материал, а не дефект, и человеку на воротах нужно знать, куда
    смотреть. Тишина называет самые длинные дыры с длиной каждой: «267 с» не
    говорит, где они, а «s12 17.3s» — говорит. Хвост — всегда последняя сцена:
    тишина в конце ничьей другой быть не может."""
    gaps = [Place(g.scene_id, f"{g.scene_id} {g.sec:.1f}s") for g in final.silence_gaps]
    longest = max(final.middle_gaps, key=lambda g: g.sec, default=None)
    return {
        "чёрные": [Place(s.id, s.id) for s in scenes if s.montage.black_sec >= DETECTOR_NOTE_SEC],
        "застывший": [
            Place(s.id, s.id) for s in scenes if s.montage.freeze_sec >= DETECTOR_NOTE_SEC
        ],
        "доля тишины": gaps,
        "самая длинная": [Place(longest.scene_id, longest.scene_id)] if longest else [],
        "тишина в конце": (
            [Place(scenes[-1].id, scenes[-1].id)] if scenes and final.silence_tail_sec else []
        ),
        "паузы речи": [
            Place(g.scene_id, f"{g.scene_id} {g.sec:.1f}s") for g in final.long_pause_gaps
        ],
    }


@dataclass
class TimelineEntry:
    scene_id: str
    start: float  # секунда финала, где начинается клип сцены
    end: float
    lead: float = 0.0  # crossfade: первые lead секунд клипа — ещё хвост прошлой сцены

    @property
    def box(self) -> float:
        return max(0.0, self.end - self.start - self.lead)


def _stretched(t: float, from_sec: float, speed: float) -> float:
    """Секунда исходного таймлайна → секунда таймлайна с замедленным хвостом."""
    return round(t if t <= from_sec else from_sec + (t - from_sec) / speed, 3)


def _per_scene_seconds(
    intervals: list[tuple[float, float]], timeline: list[TimelineEntry]
) -> dict[str, float]:
    """Сколько секунд из интервалов детектора попало в коробку каждой сцены."""
    return {
        t.scene_id: round(sum(max(0.0, min(e, t.end) - max(s, t.start)) for s, e in intervals), 3)
        for t in timeline
    }


def silence_gaps(
    intervals: list[tuple[float, float]],
    timeline: list[TimelineEntry],
    *,
    keep: int = SILENCE_GAPS_KEPT,
) -> list[SilenceGap]:
    """Самые длинные паузы детектора с именем сцены, в порядке времени.

    Берутся `keep` самых длинных (у ep001 пауз 92 — все не читаются), а
    показываются по хронометражу: человек мотает ролик вперёд, а не по длине
    дыр. Сцена — та, на которую пришлась большая часть паузы: дыра на стыке
    называется по большей доле, а не по секунде начала."""
    longest = sorted(intervals, key=lambda iv: iv[1] - iv[0], reverse=True)[:keep]
    gaps = []
    for s, e in longest:
        holder = max(timeline, key=lambda t: min(e, t.end) - max(s, t.start), default=None)
        gaps.append(
            SilenceGap(
                scene_id=holder.scene_id if holder else "",
                at_sec=round(s, 3),
                sec=round(e - s, 3),
            )
        )
    return sorted(gaps, key=lambda g: g.at_sec)


# пауза, добежавшая до конца окна замера ближе, чем на столько, — хвост. Окно
# режет интервалы ровно по `end`, зазор тут только от округления
TAIL_EPS_SEC = 0.05


def silence_tail(intervals: list[tuple[float, float]], *, end: float) -> float:
    """Сколько секунд тишины упирается в конец ролика (`end` — конец окна
    замера, то есть до fade out): «ролик молчит последние N секунд». Пауза,
    кончившаяся раньше, — не хвост, даже если она последняя."""
    return round(max((e - s for s, e in intervals if e >= end - TAIL_EPS_SEC), default=0.0), 3)


def timed_words(
    ctx: StageContext, scenes: list[Scene], timeline: list[TimelineEntry]
) -> list[TimedWord]:
    """Слова речи всех сцен на шкале финала — для замера пауз между словами.

    Те же слова, что у субтитров (`.srt` рядом с озвучкой или отрезком
    нарезки), со смещением клипа сцены и в пределах её коробки. Субтитры плана
    (`captions: false`) и стиль тут ни при чём: пауза звучит и без текста."""
    entries = {t.scene_id: t for t in timeline}
    out: list[TimedWord] = []
    for scene in scenes:
        rel = scene.assets.captions
        if not rel or not ctx.abs(rel).exists():
            continue
        words = parse_srt_words(ctx.abs(rel).read_text(encoding="utf-8"))
        entry = entries[scene.id]
        start = entry.start + entry.lead + scene_lead_in(scene)  # .srt — от начала озвучки
        for word, ends in zip(words, sentence_ends(scene.narration, words), strict=True):
            if start + word.start >= entry.end - 0.05:  # за коробкой слово не звучит
                continue
            out.append(
                TimedWord(scene.id, start + word.start, min(start + word.end, entry.end), ends)
            )
    return out


def _video_cfg(ctx: StageContext) -> VideoConfig:
    return ctx.project.video if ctx.project else VideoConfig()


def _audio_cfg(ctx: StageContext) -> AudioConfig:
    return ctx.project.audio if ctx.project else AudioConfig()


def caption_style_for(ctx: StageContext, plan: EditPlan) -> CaptionStyle | None:
    """Стиль субтитров: из плана, иначе из ресурсов эпизода, иначе `default`,
    иначе встроенные умолчания. Субтитры выключены в проекте → None.
    Тот же выбор делает QA, когда сверяет полосу субтитров с зоной UI площадки."""
    if not _video_cfg(ctx).captions:
        return None
    if plan.caption_style:
        style = load_resource("caption_styles", plan.caption_style, CaptionStyle, ctx.resource_dirs)
    elif ctx.resources.caption_style is not None:
        style = ctx.resources.caption_style
    else:
        try:
            style = load_resource(
                "caption_styles", DEFAULT_CAPTION_STYLE, CaptionStyle, ctx.resource_dirs
            )
        except FileNotFoundError:
            style = CaptionStyle(id=DEFAULT_CAPTION_STYLE)
    return over_webcam(ctx, under_fit_frame(ctx, style))


# зазор между кадром `fit` и субтитрами под ним, доля высоты выхода
CAPTION_FIT_GAP_SHARE = 0.02
# зазор между репликой и вебкой под ней у стека facecam, доля высоты выхода
CAPTION_WEBCAM_GAP_SHARE = 0.02


def over_webcam(ctx: StageContext, style: CaptionStyle) -> CaptionStyle:
    """Субтитры над вебкой, внизу игры — когда каждая сцена эпизода целиком
    собрана стеком facecam_screen (`reframe.facecam_seam`): низ реплики на
    `CAPTION_WEBCAM_GAP_SHARE` выше стыка. Стиль по центру клал её ровно на
    стык игры и вебки (stream_clips, QA всех трёх первых эпизодов 17.09.2026),
    а одобренные пробы t05/t06 ставили реплику над вебкой. Стек только в части
    планов — стиль как есть: там над стыком одиночное окно, и QA скажет сам."""
    seams: list[float] = []
    for scene in ctx.storyboard.ordered_scenes():
        seam = facecam_seam(ctx.abs(scene.assets.video)) if scene.assets.video else None
        if seam is None:
            return style
        seams.append(seam)
    if not seams:
        return style
    margin = (1 - min(seams) + CAPTION_WEBCAM_GAP_SHARE) * 100
    return style.model_copy(update={"position": "bottom", "margin_pct": round(margin, 2)})


def music_start(ctx: StageContext, plan: EditPlan, track: MusicTrack) -> float:
    """С какой секунды ролика начинается трек (`MusicMix.start_sec`;
    отрицательное — пропустить столько секунд трека). Посадка из плана монтажа
    побеждает; иначе трек с точками входа (`MusicTrack.starts`) отдаёт каждому
    ролику свою — по id эпизода, а не случайно: пересборка не меняет музыку."""
    if plan.music and plan.music.start_sec:
        return plan.music.start_sec
    if not track.starts:
        return 0.0
    ref = f"{ctx.storyboard.project_id}/{ctx.storyboard.episode_id}"
    pick = int(hashlib.sha256(ref.encode()).hexdigest(), 16) % len(track.starts)
    return -float(track.starts[pick])


def grade_for(ctx: StageContext) -> str:
    """Цветокоррекция ролика: поле эпизода побеждает поле проекта."""
    return ctx.storyboard.grade or _video_cfg(ctx).grade


def fit_band(ctx: StageContext) -> tuple[int, int] | None:
    """Где по высоте выхода лежат кадры раскладки `fit`: (самый высокий верх,
    самый низкий низ). None — хоть одна сцена кадрирована не так: тогда кадр во
    весь экран, и подписи с субтитрами стоят там, где им велит стиль."""
    tops: list[int] = []
    bottoms: list[int] = []
    for scene in ctx.storyboard.ordered_scenes():
        frame = fit_frame(ctx.abs(scene.assets.video)) if scene.assets.video else None
        if frame is None:
            return None
        tops.append(frame[1])
        bottoms.append(frame[1] + frame[3])
    return (min(tops), max(bottoms)) if tops else None


def under_fit_frame(ctx: StageContext, style: CaptionStyle) -> CaptionStyle:
    """Субтитры под кадром `fit`, а не поверх картинки, пока под кадром есть
    видимое место: верх реплики — на `CAPTION_FIT_GAP_SHARE` ниже самого низкого
    кадра эпизода. Чёрное поле под
    кадром пустует, а шутка мультсериала держится на всём кадре — закрывать его
    текстом незачем. Та же функция у сборки и у QA: полоса, которую QA сверяет
    с зоной интерфейса, — та, что нарисована."""
    band = fit_band(ctx)
    if band is None:
        return style
    size = aspect_size(ctx.storyboard.aspect_ratio)
    margin = (band[1] / size[1] + CAPTION_FIT_GAP_SHARE) * 100
    under = style.model_copy(update={"position": "top", "margin_pct": round(margin, 2)})
    # высокий кадр (кроп 3:4 с размытыми полями) оставляет под собой полосу,
    # которую целиком закрывает блок «канал / описание» площадки: под кадром
    # реплику там не прочесть. Тогда она стоит, где велит стиль, — поверх
    # картинки, как у каналов этого формата (замер erickerhead 19.09.2026)
    platforms = ctx.project.channel.platforms if ctx.project else []
    if caption_band(under, size)[1] > 1 - platform_zone(platforms)["bottom"]:
        return style
    return under


def headline_for(
    ctx: StageContext, out_dir: Path, size: tuple[int, int]
) -> headlines.Headline | None:
    """Подпись над кадром (`Storyboard.headline`) → картинка и место в финале.

    Рисуется, только если проект её включил (`video.headline`) и текст есть.
    Файл назван отпечатком текста: правка подписи даёт новый файл, и сборка
    видит смену входов, а не перезапись того же имени."""
    text = ctx.storyboard.headline.strip()
    if not (_video_cfg(ctx).headline and text):
        return None
    path = out_dir / f"headline_{headlines.fingerprint(text)}.png"
    band = fit_band(ctx)
    probed = probe_size(path) if path.exists() else None
    box = probed or headlines.render(text, path, frame_width=size[0])
    frame = (0, band[0], size[0], band[1] - band[0]) if band else None
    x, y = headlines.place(box, size=size, frame=frame)
    return headlines.Headline(path=path, text=text, x=x, y=y, width=box[0], height=box[1])


def overlay_style_for(ctx: StageContext, plan: EditPlan) -> CaptionStyle:
    """Стиль, которым рисуются плашки (`Scene.overlay`): тот же, что у
    субтитров, а если субтитры в проекте выключены — стиль `default` или
    встроенные умолчания. Текст, который зритель должен прочитать, не исчезает
    оттого, что канал не хочет субтитров под речью. Тем же стилем QA меряет
    контраст плашек к фону."""
    style = caption_style_for(ctx, plan)
    if style is not None:
        return style
    if ctx.resources.caption_style is not None:
        return ctx.resources.caption_style
    try:
        return load_resource(
            "caption_styles", DEFAULT_CAPTION_STYLE, CaptionStyle, ctx.resource_dirs
        )
    except FileNotFoundError:
        return CaptionStyle(id=DEFAULT_CAPTION_STYLE)


def overlay_right_share(ctx: StageContext) -> float:
    """Правое поле плашек: колонка кнопок площадки (`ui_zone.right`) — самая
    широкая из площадок проекта. Источник в правом верхнем углу под кнопками
    Shorts не прочитать."""
    platforms = ctx.project.channel.platforms if ctx.project else []
    zones = [PLATFORMS.get(p, {}).get("ui_zone", {}).get("right", 0.0) for p in platforms]
    return float(max(zones, default=0.0))


def platform_zone(platforms: list[str]) -> dict[str, float]:
    """Зона интерфейса, закрытая хоть на одной площадке проекта: по каждому
    краю — самая широкая из зон. Знак должен быть виден на всех площадках
    сразу, поэтому отступает от объединения, а не от первой в списке."""
    zone: dict[str, float] = {"top": 0.0, "bottom": 0.0, "right": 0.0}
    for platform in platforms:
        for edge, share in (PLATFORMS.get(platform, {}).get("ui_zone") or {}).items():
            zone[edge] = max(zone.get(edge, 0.0), float(share))
    return zone


def watermark_for(ctx: StageContext, size: tuple[int, int]) -> tuple[Watermark | None, str | None]:
    """Знак канала из настроек проекта → пиксели финала, или причина отказа.

    Файл лежит в папке проекта (`project_root`): знак общий для всех эпизодов
    канала, как источники нарезок. Высота — по картинке, ширина — доля кадра;
    положение — `watermark_xy` от объединённой зоны интерфейса площадок. Файл
    задан, но его нет — это отказ, а не пропуск: собрать ролик без знака и
    сказать «готово» значит выпустить не то, что канал заказал."""
    cfg = ctx.project.watermark if ctx.project else WatermarkConfig()
    if not cfg.enabled or not cfg.file:
        return None, None
    if ctx.project_root is None:
        return None, "водяной знак задан, а эпизод лежит вне проекта — файл искать негде"
    path = ctx.project_root / cfg.file
    if not path.exists():
        return None, (
            f"файл водяного знака {cfg.file} не найден: {path} "
            "(project.yaml → watermark.file; пусто — без знака)"
        )
    image = probe_size(path)
    if image is None:
        return None, f"водяной знак {cfg.file}: ffmpeg не прочитал размер картинки ({path})"
    w, h = size
    iw, ih = image
    width = max(1, int(round(w * cfg.width_pct / 100)))
    height = max(1, int(round(width * ih / iw)))
    margin = int(round(min(w, h) * cfg.margin_pct / 100))
    platforms = ctx.project.channel.platforms if ctx.project else []
    x, y = watermark_xy(
        cfg.corner, frame=size, box=(width, height), margin=margin, zone=platform_zone(platforms)
    )
    return Watermark(path=path, x=x, y=y, width=width, height=height, opacity=cfg.opacity), None


def _music_track(ctx: StageContext, plan: EditPlan) -> tuple[MusicTrack | None, str | None]:
    """Трек эпизода (план может выключить музыку или сменить трек) или причина,
    почему сборка его не берёт. Права обязательны."""
    enabled = _video_cfg(ctx).music
    if plan.music is not None and not plan.music.enabled:
        return None, None
    if not enabled and not (plan.music and plan.music.track):
        return None, None
    track = ctx.resources.music
    if plan.music and plan.music.track:
        track = load_resource("music", plan.music.track, MusicTrack, ctx.resource_dirs)
    if track is None:
        return None, (
            "в project.yaml video.music: true, а resources.music не задан — "
            "укажи трек из resources/music/ (videoforge resource new music <id>)"
        )
    if not track.license.strip() or not track.source.strip():
        return None, (
            f"у трека music/{track.id} не заполнены license и source — без прав на музыку "
            "сборка её не берёт (Content ID снимает монетизацию или блокирует ролик)"
        )
    if not track.path().exists():
        return None, f"файл трека music/{track.id} не найден: {track.path()}"
    return track, None


def music_gain(ctx: StageContext, plan: EditPlan, track: MusicTrack) -> float:
    """Уровень трека относительно голоса: план → проект → ресурс.

    Три уровня, потому что три разных решения: файл несёт свой уровень по
    умолчанию, канал говорит, насколько громко трек звучит у НЕГО (один трек
    стоит у нескольких проектов), а план монтажа правит один ролик."""
    if plan.music is not None and plan.music.gain_db is not None:
        return plan.music.gain_db
    channel = _audio_cfg(ctx).music_gain_db
    return channel if channel is not None else track.gain_db


def _bed_track(ctx: StageContext, plan: EditPlan) -> tuple[MusicTrack | None, float, str | None]:
    """Подложка эпизода: (трек, уровень относительно голоса, причина отказа).

    План впереди проекта: `bed` в плане отсутствует — как в проекте;
    `{"track": null}` — слоя нет у этого ролика. Права обязательны, как у
    музыки; и трек обязан быть помечен `bed: true` — слой держит один уровень
    на весь ролик, и трек с развитием на этом месте вернул бы ту самую тишину
    вступления, ради которой слой заведён."""
    cfg = _audio_cfg(ctx)
    track_id = cfg.bed
    gain = cfg.bed_gain_db
    if plan.bed is not None:
        track_id = plan.bed.track
        if plan.bed.gain_db is not None:
            gain = plan.bed.gain_db
    if not track_id:
        return None, gain, None
    error: str | None = None
    try:
        track = load_resource("music", track_id, MusicTrack, ctx.resource_dirs)
    except FileNotFoundError:
        error = (
            f"подложка music/{track_id} не найдена в resources/music/ "
            f"(ровную синтезирует videoforge resource gen-music {track_id} --preset flat_pad)"
        )
        return None, gain, error
    if not track.bed:
        error = (
            f"music/{track.id} не помечен bed: true — подложкой берётся только ровный "
            "зацикливаемый эмбиенс без развития; трек с развитием оставь музыке (resources.music)"
        )
    elif not track.license.strip() or not track.source.strip():
        error = (
            f"у подложки music/{track.id} не заполнены license и source — без прав "
            "сборка её не берёт (Content ID ловит и эмбиенс)"
        )
    elif not track.path().exists():
        error = f"файл подложки music/{track.id} не найден: {track.path()}"
    return (None, gain, error) if error else (track, gain, None)


def _sounds(ctx: StageContext, plan: EditPlan) -> tuple[list[Sound], str | None]:
    """Звуковые эффекты плана → файлы с уровнями. Права обязательны, как у музыки:
    Content ID ловит и трёхсекундный свист."""
    sounds: list[Sound] = []
    for cue in plan.sfx:
        try:
            res = load_resource("sfx", cue.id, Sfx, ctx.resource_dirs)
        except FileNotFoundError:
            return [], (
                f"в плане монтажа звук sfx/{cue.id}, которого нет в resources/sfx/ "
                f"(собери библиотеку: videoforge resource gen-sfx)"
            )
        if not res.license.strip() or not res.source.strip():
            return [], (
                f"у звука sfx/{res.id} не заполнены license и source — без прав сборка его не берёт"
            )
        if not res.path().exists():
            return [], f"файл звука sfx/{res.id} не найден: {res.path()}"
        gain = cue.gain_db if cue.gain_db is not None else res.gain_db
        sounds.append(Sound(path=res.path(), at=cue.at_sec, gain_db=gain))
    return sounds, None


def _effects(plan: EditPlan) -> list[Effect]:
    return [
        Effect(kind=e.kind, at=e.at_sec, duration=e.duration_sec, strength=e.strength)
        for e in plan.effects
    ]


def chain_zoom(scenes: list[Scene], zoom_per_sec: float) -> dict[str, float]:
    """Во сколько раз увеличить кадр каждой сцены, чтобы стык продолжений плана
    (`Scene.continues`) не отпрыгивал. 1.0 у всех, кроме продолжений
    генеративных клипов при заданном `video.zoom_per_sec` проекта.

    i2v начинает каждый клип с исходного масштаба, а предыдущее звено за свои
    d секунд доехало до 1 + k·d — на стыке кадр отпрыгивал на всю величину
    наезда (QA дважды называл дефектом). Продолжение получает ПОСТОЯННЫЙ кроп,
    равный масштабу конца предыдущего звена, и его собственный наезд дальше
    умножается на него — ход продолжается с той же скоростью. Произведение,
    а не сумма 1 + k·Σd: масштабы перемножаются, и третье звено по сумме
    промахивалось бы на k²·d₁·d₂ (1.2% при k = 0.02 и звеньях по 6 с).

    Клип, сшитый провайдером (`Scene.chained`, программная камера), не
    трогается: его старт уже на месте, кроп поверх удвоил бы ход.

    Замер 14.09.2026 на заглушке (три звена по 6 с, k = 0.02, кадр из цветных
    клеток 1080×1920, разность яркости соседних кадров 0..255): стык 33.2 →
    2.1 и 33.2 → 2.3 при 1.3 между соседними кадрами внутри звена
    (`tests/test_zoom_chain.py`)."""
    zoom: dict[str, float] = {}
    factor = 1.0
    previous: Scene | None = None
    for scene in sorted(scenes, key=lambda s: s.order):
        if scene.continues and previous is not None and zoom_per_sec > 0 and not scene.chained:
            factor = round(factor * (1 + zoom_per_sec * previous.duration_sec), 6)
        else:
            factor = 1.0
        zoom[scene.id] = factor
        previous = scene
    return zoom


class PlanRejected(RuntimeError):
    """Битый план не заменит собой готовый ролик: собрать вместо него умолчание
    значит стереть работу тем, чего никто не просил."""


def _plan_for_render(ctx: StageContext, *, has_final: bool) -> tuple[EditPlan, str]:
    """План монтажа или умолчание.

    Правило одно и оно про ПЕРЕЗАПИСЬ, а не про сборку. Готового ролика ещё
    нет — невалидный план не останавливает работу: собираем по умолчанию (hard
    cut везде) и говорим об этом громко, потому что остаться совсем без файла
    хуже, чем получить его без правок. Ролик уже есть — сборка отказывается:
    заменить хороший файл дефолтным нельзя, это молча потерянная работа.

    Разница появилась не из вкуса. Раньше опечатка в плане (несуществующий
    стиль субтитров, путь к клипу с лишней буквой) перезаписывала готовый
    `final.mp4` сборкой без единой правки: файл на месте, играет, длительность
    та же — и понять, что монтажа в нём нет, можно только просмотром. Пока план
    правили в редакторе, это стоило одного прогона; с кнопкой «пересобрать» в
    браузере это стало бы обычным делом.

    Заготовка стадии (`author: default`) под правило не подпадает: это
    машинный черновик, и терять из-за него готовый ролик незачем."""
    plan = load_plan(ctx.project_dir)
    if plan is None:
        return default_plan(ctx.storyboard), "по умолчанию (плана нет — стадия edit его сделает)"
    result = run_lint(ctx, plan)
    if result.blocking:
        reasons = "; ".join(f.message for f in result.blocking)
        if has_final and plan.author != "default":
            raise PlanRejected(
                f"план монтажа ({plan.author}) не прошёл проверку, готовый файл не тронут: "
                f"{reasons}"
            )
        ctx.log.warning("план монтажа не прошёл проверку, собираю по умолчанию: %s", reasons)
        fallback = default_plan(ctx.storyboard)
        return fallback, f"по умолчанию — план не прошёл проверку: {reasons}"
    for fix in result.fixes:
        ctx.log.info("план монтажа: %s", fix)
    return result.plan, plan.author


# Что слышно и НЕ видно. Список короткий и явный намеренно: всё, чего в нём нет,
# считается картинкой и приводит к полной пересборке. Ошибиться в эту сторону
# дорого по времени, в обратную — стыдно: зритель получит новый звук поверх
# старой картинки, и никакой замер этого не поймает
PICTURE_EXCLUDE: dict[str, Any] = {
    "generated_at": True,
    "notes": True,
    "music": True,  # трек, уровни, посадка дропа, ступени
    "bed": True,  # подложка: трек и уровень
    "sfx": True,  # звуки в свои секунды
    "scenes": {"__all__": {"music_gain_db"}},  # уровень музыки под конкретной сценой
}


def inputs_fingerprint(
    ctx: StageContext,
    scenes: list[Scene],
    *,
    plan: EditPlan,
    style: CaptionStyle | None,
    track: MusicTrack | None,
    cfg: VideoConfig,
    picture_only: bool = False,
    watermark: Watermark | None = None,
    bed: MusicTrack | None = None,
    bed_gain_db: float = 0.0,
    headline: headlines.Headline | None = None,
) -> str:
    """Отпечаток входов сборки: план, порядок сцен, их длительности, файлы клипов,
    озвучки и таймингов, стиль субтитров, трек, подложка, fade, водяной знак,
    версия рендера."""
    h = hashlib.sha256(f"{RENDER_VERSION}|{ctx.storyboard.aspect_ratio}".encode())
    h.update(f"|fade:{cfg.fade_in_sec:g}/{cfg.fade_out_sec:g}|tail:{cfg.scene_tail_sec:g}".encode())
    # частота — в хеш только когда задана: пустое поле и раньше значило 24, и
    # все собранные до него финалы иначе разом стали бы «устаревшими»
    if ctx.storyboard.fps is not None:
        h.update(f"|fps:{ctx.storyboard.fps:g}".encode())
    if grade_for(ctx):  # коррекция — так же: пустая в хеш не идёт
        h.update(f"|grade:{grade_for(ctx)}".encode())
    exclude: Any = PICTURE_EXCLUDE if picture_only else {"generated_at", "notes"}
    h.update(b"|plan:" + plan.model_dump_json(exclude=exclude).encode())
    # градиент — в хеш только когда задан, по той же причине, что частота: новое
    # пустое поле стиля не должно разом состарить все собранные финалы
    style_skip = (
        {name for name in ("gradient_to", "strip_punctuation") if not getattr(style, name)}
        if style
        else None
    )
    h.update(b"|style:" + (style.model_dump_json(exclude=style_skip).encode() if style else b"off"))
    # знак — часть картинки: файл (размер и mtime — перерисованный PNG того же
    # имени тоже считается) и где он стоит (в пикселях уже учтены угол, ширина,
    # отступ и зона площадок). Без знака в хеш не идёт ничего — иначе все
    # собранные без него ролики разом стали бы «устаревшими» и пошли на пересборку
    if watermark is not None:
        st = watermark.path.stat()
        h.update(
            f"|wm:{st.st_size}:{st.st_mtime_ns}:{watermark.x}:{watermark.y}"
            f":{watermark.width}:{watermark.height}:{watermark.opacity:g}".encode()
        )
    # подпись — как знак: в хеш только когда есть. Имя файла — отпечаток текста
    # и вида, место — пиксели; mtime не берётся, картинка перерисовывается той же
    if headline is not None:
        h.update(
            f"|headline:{headline.path.name}:{headline.x}:{headline.y}"
            f":{headline.width}:{headline.height}".encode()
        )
    if picture_only:
        pass  # трек и звуки картинку не рисуют
    elif track is not None:
        st = track.path().stat()
        levels = f"{music_gain(ctx, plan, track)}:{track.duck_db}"
        if track.starts:  # точки входа — в хеш только когда заданы
            levels += f":{music_start(ctx, plan, track):g}"
        h.update(f"|music:{track.id}:{st.st_size}:{st.st_mtime_ns}:{levels}".encode())
    else:
        h.update(b"|music:off")
    # подложка — как знак: в хеш идёт только когда есть. Иначе все финалы,
    # собранные без неё, разом стали бы «устаревшими» и пошли на пересборку
    if bed is not None and not picture_only:
        st = bed.path().stat()
        h.update(f"|bed:{bed.id}:{st.st_size}:{st.st_mtime_ns}:{bed_gain_db:g}".encode())
    if plan.effects or plan.sfx or plan.outro is not None:
        h.update(f"|fx:{EFFECTS_VERSION}".encode())
    # звуковые эффекты берутся файлами: перегенерировали библиотеку — финал устарел
    for cue in plan.sfx if not picture_only else []:
        h.update(f"|sfx:{cue.id}:{cue.at_sec:g}".encode())
        for base in ctx.resource_dirs:
            path = base / "sfx" / f"{cue.id}.yaml"
            if path.exists():
                h.update(f":{path.stat().st_mtime_ns}".encode())
                break
    # перевод лежит у сцен, а субтитры на языке речи: язык субтитров сменили на
    # «как речь» (stream_clips 17.09.2026: en → русский канал). Файл перевода
    # остался тем же, и без этой части финал считался бы собранным с английским
    # текстом. Только в этом случае: у финалов с переводом и без него хеш прежний
    wanted = ctx.storyboard.captions_language
    if not (wanted and wanted != ctx.storyboard.language) and any(
        s.assets.translation for s in scenes
    ):
        h.update(b"|captions:speech")
    sources: set[str] = set()
    # кроп стыка продолжений: сменился `zoom_per_sec` или длина соседа — финал
    # устарел. Часть добавляется только там, где кроп есть: у роликов без
    # цепочек отпечаток не меняется, и пересобирать их не за что
    zoom = chain_zoom(scenes, cfg.zoom_per_sec)
    for s in scenes:
        h.update(f"|{s.id}|{s.duration_sec:g}".encode())  # длину сцены задаёт раскадровка
        if lead_in := scene_lead_in(s):  # заход двигает звук и субтитры; нет — хеш прежний
            h.update(f"|in:{lead_in:g}".encode())
        # заглушённый звук картинку не меняет, поэтому в отпечаток картинки не
        # идёт — иначе смена решения перерисовывала бы готовое видео. И только
        # когда глушить велено: иначе все прежние финалы разом устарели бы
        if s.mute and not picture_only:
            h.update(b"|mute")
        if zoom[s.id] > 1.0:
            h.update(f"|zoom:{zoom[s.id]:g}".encode())
        if s.overlay is not None:  # плашка рисуется в финале: поменялся текст — финал устарел
            # без None: новое поле окна, которого у старой плашки нет, — не смена входов
            h.update(b"|overlay:" + s.overlay.model_dump_json(exclude_none=True).encode())
        sources.update(
            x
            for x in (s.assets.video, s.assets.audio, s.assets.captions, s.assets.translation)
            if x
        )
        edit = plan.scene(s.id)
        if edit:
            sources.update(seg.source for seg in edit.segments if seg.source)
    for rel in sorted(sources):
        h.update(b"|")
        if ctx.abs(rel).exists():
            st = ctx.abs(rel).stat()
            h.update(f"{rel}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def _scene_words(
    ctx: StageContext, scenes: list[Scene], timeline: list[TimelineEntry], plan: EditPlan
) -> tuple[list[SceneWords], float]:
    """Слова каждой сцены на измеренном таймлайне и сумма секунд речи, которую
    субтитры должны покрыть. Сцена с `captions: false` в плане из счёта уходит."""
    result: list[SceneWords] = []
    speech = 0.0
    entries = {t.scene_id: t for t in timeline}
    for scene in scenes:
        edit = plan.scene(scene.id)
        if edit is not None and not edit.captions:
            continue
        t = entries[scene.id]
        words: list[Word] = []
        if scene.assets.captions and ctx.abs(scene.assets.captions).exists():
            words = parse_srt_words(ctx.abs(scene.assets.captions).read_text(encoding="utf-8"))
        if words:
            speech += speech_seconds(words)
        elif scene.narration.strip():
            # речь есть, таймингов нет (провайдер без WordBoundary) — это непокрытая речь
            speech += scene.measured_sec.get("audio", 0.0)
        emphasis = frozenset(edit.emphasis) if edit else frozenset()
        # слова — от начала озвучки (после захода), конец коробки — конец сцены
        start = t.start + t.lead + scene_lead_in(scene)
        result.append(SceneWords(scene.id, start, max(0.0, t.end - start), words, emphasis))
    return result, speech


def overlay_events(scenes: list[Scene], timeline: list[TimelineEntry]) -> list[OverlayEvent]:
    """Плашки сцен на измеренном таймлайне. Одна и та же плашка на соседних
    сценах (продолжения одного плана, ярлык пункта на все его сцены) — одно
    событие без мигания на стыке."""
    entries = {t.scene_id: t for t in timeline}
    out: list[OverlayEvent] = []
    for scene in scenes:
        if scene.overlay is None or not scene.overlay.text.strip():
            continue
        t = entries[scene.id]
        # окно плашки (start_sec/end_sec от начала сцены) режет коробку сцены
        window = overlay_window(t.start + t.lead, t.end, scene.overlay)
        if window is None:  # окно целиком за сценой — рисовать нечего
            continue
        start, end = window
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.text == scene.overlay.text
            and prev.kind == scene.overlay.kind
            and abs(prev.end - start) < 0.05
        ):
            prev.end = end
            continue
        out.append(
            OverlayEvent(
                start,
                end,
                scene.overlay.text.strip(),
                scene.overlay.kind,
                frozenset(scene.overlay.emphasis),
                scene.id,
            )
        )
    return out


def _caption_events(
    ctx: StageContext, scenes: list[Scene], scene_words: list[SceneWords], style: CaptionStyle
) -> tuple[list[CaptionEvent], list[str]]:
    """Реплики: на языке зрителя из файла перевода (стадия translate), если
    проект его требует и он есть у сцены, иначе — группировка слов речи по
    стилю. Сцены без перевода при заданном языке субтитров возвращаются
    списком: субтитры у них выйдут на языке речи, и человек должен это знать."""
    wanted = ctx.storyboard.captions_language
    translate = bool(wanted) and wanted != ctx.storyboard.language
    by_id = {s.id: s for s in scenes}
    events: list[CaptionEvent] = []
    untranslated: list[str] = []
    for sw in scene_words:
        scene = by_id[sw.scene_id]
        if translate and scene.assets.translation and ctx.abs(scene.assets.translation).exists():
            captions = read_model(ctx.abs(scene.assets.translation), TranslatedCaptions)
            if captions.language == wanted:
                events.extend(translated_events(sw, captions))
                continue
        if translate and sw.words:
            untranslated.append(sw.scene_id)
        events.extend(build_events([sw], style))
    return events, untranslated


class AssembleStage(Stage):
    name = "assemble"
    needs = ()  # только ffmpeg

    def run(self, ctx: StageContext) -> StageResult:
        scenes = ctx.storyboard.ordered_scenes()
        if not scenes:
            return StageResult(status="failed", message="в раскадровке нет сцен")
        missing = [
            s.id for s in scenes if not (s.assets.video and ctx.abs(s.assets.video).exists())
        ]
        if missing:
            return StageResult(status="failed", message=f"нет клипов для сцен: {missing}")

        cfg = _video_cfg(ctx)
        # готовый ролик уже лежит? тогда битый план его не заменит
        has_final = bool(
            ctx.storyboard.final.video and ctx.abs(ctx.storyboard.final.video).exists()
        )
        try:
            plan, plan_note = _plan_for_render(ctx, has_final=has_final)
        except PlanRejected as exc:
            return StageResult(status="failed", message=str(exc))
        style = caption_style_for(ctx, plan)
        track, music_error = _music_track(ctx, plan)
        if music_error:
            return StageResult(status="failed", message=music_error)
        sounds, sfx_error = _sounds(ctx, plan)
        if sfx_error:
            return StageResult(status="failed", message=sfx_error)
        bed_track, bed_gain, bed_error = _bed_track(ctx, plan)
        if bed_error:
            return StageResult(status="failed", message=bed_error)
        fade_in = plan.fade_in_sec if plan.fade_in_sec is not None else cfg.fade_in_sec
        fade_out = plan.fade_out_sec if plan.fade_out_sec is not None else cfg.fade_out_sec

        # плашки рисуются тем же ASS, что субтитры; когда субтитры выключены, а
        # плашки есть, стиль нужен только им — и в отпечаток входит только тогда
        has_overlays = any(s.overlay is not None and s.overlay.text.strip() for s in scenes)
        ass_style = style if style is not None else (
            overlay_style_for(ctx, plan) if has_overlays else None
        )  # fmt: skip

        size = aspect_size(ctx.storyboard.aspect_ratio)
        # частота эпизода — одна на все шаги рендера: у нарезки её поставил cut по
        # исходнику, у ai_video поле пусто и это 24. Клип другой частоты сюда
        # попадает только если его так и сделали (кадрирование до смены поля,
        # чужой файл), и тогда пересчёт один — в mux_scene — и виден в замере
        fps = ctx.storyboard.fps or FPS
        watermark, watermark_error = watermark_for(ctx, size)
        if watermark_error:
            return StageResult(status="failed", message=watermark_error)

        out_dir = ctx.stage_dir(OUT_DIR)
        final = out_dir / FINAL
        headline = headline_for(ctx, out_dir, size)
        fingerprint = inputs_fingerprint(
            ctx,
            scenes,
            plan=plan,
            style=ass_style,
            track=track,
            cfg=cfg,
            watermark=watermark,
            bed=bed_track,
            bed_gain_db=bed_gain,
            headline=headline,
        )
        picture = inputs_fingerprint(
            ctx,
            scenes,
            plan=plan,
            style=ass_style,
            track=track,
            cfg=cfg,
            picture_only=True,
            watermark=watermark,
            headline=headline,
        )
        if (
            not ctx.options.force
            and final.exists()
            and ctx.storyboard.final.inputs_fingerprint == fingerprint
        ):
            return StageResult(status="done", message=f"уже собрано: {ctx.rel(final)}")
        # картинка та же, а общий отпечаток разошёлся — значит поменялся ТОЛЬКО
        # звук: дорожку картинки берём из готового файла, а не рисуем заново
        keep_video = (
            final
            if (
                not ctx.options.force
                and final.exists()
                and ctx.storyboard.final.video_fingerprint == picture
            )
            else None
        )

        lengths: dict[str, float] = {}

        def duration_of(rel: str) -> float | None:
            if rel not in lengths:
                p = ctx.abs(rel)
                lengths[rel] = probe_duration(p) if p.exists() else -1.0
            return lengths[rel] if lengths[rel] >= 0 else None

        # --- сцены: отрезки по плану → клип → озвучка
        zoom = chain_zoom(scenes, cfg.zoom_per_sec)
        if cfg.zoom_per_sec > 0 and any(s.chained and s.continues for s in scenes):
            ctx.log.warning(
                "video.zoom_per_sec задан, а продолжения плана уже начаты с места хода "
                "провайдером (Scene.chained) — кроп стыка к ним не применён"
            )
        clips: list[Path] = []
        muted_scenes: list[str] = []  # сцены, у которых заглушён свой звук
        voice_expected = any(s.assets.audio for s in scenes)  # своя озвучка есть хоть у одной
        notes: list[str] = []
        for scene in scenes:
            audio = ctx.abs(scene.assets.audio) if scene.assets.audio else None
            if audio and not audio.exists():
                return StageResult(
                    status="failed",
                    message=f"сцена {scene.id}: озвучка {scene.assets.audio} не найдена",
                )
            edit = plan.scene(scene.id) or SceneEdit(scene_id=scene.id)
            segments, errors, _ = resolve_segments(edit, scene, duration_of)
            if errors or not segments:
                return StageResult(
                    status="failed", message="; ".join(errors) or f"сцена {scene.id}: нет отрезков"
                )
            cuts = [Cut(ctx.abs(s.source or ""), s.in_sec, s.out_sec or 0.0) for s in segments]
            assert scene.assets.video
            whole = (
                len(cuts) == 1
                and cuts[0].source == ctx.abs(scene.assets.video)
                and cuts[0].in_sec == 0
                and cuts[0].out_sec
                >= min(duration_of(scene.assets.video) or 0.0, scene.duration_sec) - 0.05
            )
            if not whole and audio is None and scene.source is not None:
                # у нарезки речь живёт в самом клипе, а сборка отрезков звук
                # отбрасывает (`build_scene_video` — `-an`): вышел бы немой шортс.
                # Отрезки со звуком — вместе с вырезанием пауз, не раньше
                return StageResult(
                    status="failed",
                    message=(
                        f"сцена {scene.id}: план режет её на {len(cuts)} отрезка, но речь "
                        f"в самом клипе, а сборка отрезков звук отбрасывает — получился бы "
                        f"немой шортс. Убери segments из плана монтажа"
                    ),
                )
            if whole:
                source = ctx.abs(scene.assets.video)
            else:
                source = out_dir / f"{scene.id}.cut.mp4"
                ctx.log.info("сцена %s: %d отрезков → клип", scene.id, len(cuts))
                with ctx.step("отрезки сцены", f"{scene.id}: {len(cuts)}"):
                    build_scene_video(cuts, source, size=size, fps=fps)
            clip = out_dir / f"{scene.id}.mp4"
            ctx.log.info("сцена %s: сборка", scene.id)
            if zoom[scene.id] > 1.0:
                ctx.log.info(
                    "сцена %s: продолжение плана, кроп ×%.4f — старт на масштабе конца %s",
                    scene.id, zoom[scene.id], scene.continues,
                )  # fmt: skip
            # заглушить звук оригинала можно только если вместо него что-то
            # играет: без трека вышел бы немой ролик, а это хуже чужой музыки
            muted = bool(scene.mute) and track is not None
            if muted:
                muted_scenes.append(scene.id)
            if scene.mute and track is None:
                notes.append(
                    f"сцена {scene.id}: звук оригинала велено заглушить, но музыки у проекта "
                    f"нет — ролик собран с оригинальным звуком (video.music и resources.music)"
                )
            with ctx.step("клип сцены", f"{scene.id}: {scene.duration_sec:.1f} с"):
                muxed = mux_scene(
                    source,
                    audio,
                    clip,
                    size=size,
                    fps=fps,
                    target_sec=scene.duration_sec,
                    zoom=zoom[scene.id],
                    audio_delay=scene_lead_in(scene),  # заход: картинка до озвучки
                    mute=muted,
                )
            scene.measured_sec["frozen"] = round(muxed.frozen_sec, 3)
            scene.montage.zoom = zoom[scene.id]
            scene.montage.shots = len(cuts)
            scene.montage.transition_in = edit.transition_in.type
            scene.montage.lead_sec = 0.0
            scene.montage.music = track is not None
            scene.montage.cropped_share = muxed.cropped_share
            if muxed.frozen_sec > 0:
                ctx.log.warning(
                    "сцена %s: %.2f с замороженного кадра — отрезки короче коробки",
                    scene.id, muxed.frozen_sec,
                )  # fmt: skip
            if muxed.cropped_share > CROP_WARN_SHARE:
                ctx.log.warning(
                    "сцена %s: кроп срезал %.0f%% кадра — клип не в формате эпизода (%s)",
                    scene.id, muxed.cropped_share * 100, ctx.storyboard.aspect_ratio,
                )  # fmt: skip
            clips.append(clip)

        # --- переходы на границах: меняют файлы соседних сцен, не порядок
        transitions = 0
        for i in range(1, len(scenes)):
            scene, prev = scenes[i], scenes[i - 1]
            tr = (plan.scene(scene.id) or SceneEdit(scene_id=scene.id)).transition_in
            if tr.type == "cut" or tr.duration_sec <= 0:
                continue
            transitions += 1
            ctx.log.info("переход %s → %s: %s %.2fs", prev.id, scene.id, tr.type, tr.duration_sec)
            if tr.type == "crossfade":
                out_prev = out_dir / f"{prev.id}.out.mp4"
                out_next = out_dir / f"{scene.id}.in.mp4"
                crossfade_pair(
                    clips[i - 1],
                    clips[i],
                    tr.duration_sec,
                    out_prev=out_prev,
                    out_next=out_next,
                    fps=fps,
                )
                clips[i - 1], clips[i] = out_prev, out_next
                scene.montage.lead_sec = XFADE_EPS
            elif tr.type == "dip_black":
                half = tr.duration_sec / 2
                out_prev = out_dir / f"{prev.id}.out.mp4"
                out_next = out_dir / f"{scene.id}.in.mp4"
                fade_edges(clips[i - 1], out_prev, fade_out=half)
                fade_edges(clips[i], out_next, fade_in=half)
                clips[i - 1], clips[i] = out_prev, out_next
            else:  # fade_in
                out_next = out_dir / f"{scene.id}.in.mp4"
                fade_edges(clips[i], out_next, fade_in=tr.duration_sec)
                clips[i] = out_next

        # --- измеренный таймлайн: по нему живут субтитры, музыка и детекторы
        timeline: list[TimelineEntry] = []
        cursor = 0.0
        with ctx.step("таймлайн и кадры", f"сцен {len(scenes)}"):
            for scene, clip in zip(scenes, clips, strict=True):
                length = probe_duration(clip)
                timeline.append(
                    TimelineEntry(scene.id, cursor, cursor + length, scene.montage.lead_sec)
                )
                cursor += length
                scene.assets.clip = ctx.rel(clip)
                scene.assets.thumbs = [
                    ctx.rel(p) for p in scene_thumbs(clip, out_dir / THUMBS_DIR, scene.id)
                ]
                scene.measured_sec["clip"] = round(length, 3)
                ctx.save()

        # паузы речи — по исходному таймлайну, до замедленного хвоста: меряется
        # темп голоса, каким его поставила озвучка, а не эффект конца ролика
        pauses = speech_pauses(timed_words(ctx, scenes, timeline))

        scenes_file = out_dir / SCENES_FILE
        with ctx.step("склейка сцен", f"{cursor:.1f} с"):
            concat(clips, scenes_file)

        # --- замедленный хвост: единственное намеренное изменение хронометража.
        # Голова не двигается, поэтому всё измеренное до точки замедления верно;
        # субтитры хвоста растягиваются тем же преобразованием, что и картинка
        outro_from, outro_speed, outro_added = 0.0, 1.0, 0.0
        if plan.outro is not None:
            spec = plan.outro
            before = probe_duration(scenes_file)
            slowed = out_dir / "scenes.slow.mp4"
            with ctx.step("замедленный хвост", f"{spec.take_sec:g} с × {spec.speed:g}"):
                after = slow_tail(
                    scenes_file,
                    slowed,
                    SlowTail(
                        take_sec=spec.take_sec,
                        speed=spec.speed,
                        pitch_drop=spec.pitch_drop,
                        zoom=spec.zoom,
                        desaturate=spec.desaturate,
                        vignette=spec.vignette,
                    ),
                    fps=fps,
                )
            outro_from = max(0.0, before - min(spec.take_sec, before))
            outro_speed = max(0.1, min(1.0, spec.speed))
            outro_added = round(after - before, 3)
            scenes_file = slowed
            ctx.log.info("замедленный хвост с %.1f с: %.1f → %.1f с", outro_from, before, after)

        # --- субтитры: слова из озвучки → реплики → ASS по стилю, акценты из плана;
        # плашки (Scene.overlay) — в тот же файл своими стилями
        ass_path: Path | None = None
        fonts_dir: Path | None = None
        untranslated: list[str] = []
        result_final = ctx.storyboard.final
        result_final.caption_events = 0
        result_final.caption_coverage = None
        result_final.captions = None
        result_final.overlay_events = 0
        for scene in scenes:
            scene.montage.caption_events = 0
            scene.montage.overlay = False
        overlays = overlay_events(scenes, timeline) if has_overlays else []
        if ass_style is not None:
            note = f"стиль {ass_style.id}" + ("" if style is not None else ", только плашки")
            with ctx.step("субтитры", note) as captions_step:
                # реплики считаются по НЕзамедленному таймлайну (в нём живут слова),
                # и только потом растягиваются вместе с хвостом
                events: list[CaptionEvent] = []
                speech = 0.0
                emphasis: dict[str, frozenset[str]] = {}
                if style is not None:
                    scene_words, speech = _scene_words(ctx, scenes, timeline, plan)
                    raw_events, untranslated = _caption_events(ctx, scenes, scene_words, style)
                    events = stretch_events(raw_events, from_sec=outro_from, speed=outro_speed)
                    emphasis = {sw.scene_id: sw.emphasis for sw in scene_words if sw.emphasis}
                overlays = stretch_overlays(overlays, from_sec=outro_from, speed=outro_speed)
                ass_path = out_dir / ASS_FILE
                ass_path.write_text(
                    build_ass(
                        events,
                        ass_style,
                        size=size,
                        emphasis=emphasis,
                        overlays=overlays,
                        overlay_right_share=overlay_right_share(ctx),
                    ),
                    encoding="utf-8",
                )
                captions_step.note += f", реплик {len(events)}, плашек {len(overlays)}"
            if style is not None:
                covered = 0.0
                for ev in events:
                    ctx.storyboard.scene(ev.scene_id).montage.caption_events += 1
                    covered += speech_seconds(ev.words)
                result_final.captions = ctx.rel(ass_path)
                result_final.caption_events = len(events)
                result_final.caption_coverage = (
                    round(min(1.0, covered / speech), 3) if speech else None
                )
            result_final.overlay_events = len(overlays)
            for scene in scenes:
                scene.montage.overlay = scene.overlay is not None and bool(
                    scene.overlay.text.strip()
                )
            font = ass_style.font_path()
            if font.exists():
                fonts_dir = font.parent
            else:
                ctx.log.warning("шрифт субтитров %s не найден — libass возьмёт системный", font)

        # таймлайн после замедления хвоста: по нему живут ступени музыки, разбор
        # QA и монтажный лист. Субтитры уже посчитаны — им нужен был исходный
        if outro_speed < 1.0:
            for entry in timeline:
                entry.start = _stretched(entry.start, outro_from, outro_speed)
                entry.end = _stretched(entry.end, outro_from, outro_speed)

        # --- музыка: трек проекта или плана, ступени громкости по сценам
        music = None
        music_steps: list[dict[str, object]] = []  # что применено — в montage.json
        if track is not None:
            gain = music_gain(ctx, plan, track)
            # весь звук ролика — это трек: голоса, под который его опускают, нет.
            # `gain_db` трека («на 20 дБ ниже голоса») без голоса кладёт музыку на
            # −40 LUFS, и нормализация тянет её обратно на −14 — то есть вместе с
            # треком поднимает его шумовой пол на 26 дБ. Конечная громкость от
            # этого не зависит (loudnorm приводит к своей цели в любом случае),
            # зависит только запас до неё
            if muted_scenes and len(muted_scenes) == len(scenes) and not voice_expected:
                gain = 0.0
            duck = (
                plan.music.duck_db
                if plan.music and plan.music.duck_db is not None
                else track.duck_db
            )
            starts = {t.scene_id: t.start for t in timeline}
            # ступени по границам сцен и точечные ступени плана — в один список:
            # дроп случается внутри реплики, а не на границе сцены
            cues = list(plan.music.cues) if plan.music else []
            music_steps = [
                {
                    "scene_id": e.scene_id,
                    "at_sec": round(starts[e.scene_id], 3),
                    "gain_db": e.music_gain_db,
                }
                for e in plan.scenes
                if e.music_gain_db is not None and e.scene_id in starts
            ] + [{"scene_id": None, "at_sec": c.at_sec, "gain_db": c.gain_db} for c in cues]
            steps = tuple(
                sorted(
                    [
                        (starts[e.scene_id], e.music_gain_db - gain)
                        for e in plan.scenes
                        if e.music_gain_db is not None and e.scene_id in starts
                    ]
                    + [(c.at_sec, c.gain_db - gain) for c in cues]
                )
            )
            music = MusicMix(
                path=track.path(),
                gain_db=gain,
                duck_db=duck,
                loop=track.loop_ok,
                gain_steps=steps,
                start_sec=music_start(ctx, plan, track),
            )

        # подложка гаснет вместе с финалом: её край — fade out ролика
        bed = (
            BedMix(path=bed_track.path(), gain_db=bed_gain, fade_out_sec=fade_out)
            if bed_track is not None
            else None
        )
        effects = _effects(plan)
        ctx.log.info("финал: субтитры %s, музыка %s, подложка %s, эффектов %d, звуков %d",
                     "да" if ass_path else "нет", track.id if track else "нет",
                     bed_track.id if bed_track else "нет", len(effects), len(sounds))  # fmt: skip
        note = f"субтитры {'да' if ass_path else 'нет'}, музыка {track.id if track else 'нет'}"
        if bed_track is not None:
            note += f", подложка {bed_track.id} ({bed_gain:g} дБ)"
        if effects or sounds:
            note += f", эффектов {len(effects)}, звуков {len(sounds)}"
        if watermark is not None:
            note += f", знак {watermark.path.name}"
        if headline is not None:
            note += ", подпись над кадром"
        grade = grade_for(ctx)
        if grade and grade not in GRADES:
            return StageResult(
                status="failed",
                message=f"коррекции {grade!r} нет: есть {', '.join(sorted(GRADES))}",
            )
        if grade:
            note += f", коррекция {grade}"
        with ctx.step("финальный проход", note):
            mix: FinalMix = finalize(
                scenes_file,
                final,
                captions=ass_path,
                fonts_dir=fonts_dir,
                music=music,
                sounds=sounds,
                effects=effects,
                watermark=watermark,
                headline=headline,
                bed=bed,
                grade=grade,
                size=size,
                fade_in_sec=fade_in,
                fade_out_sec=fade_out,
                audio_tail_sec=cfg.scene_tail_sec,
                keep_video=keep_video,
                fps=fps,
            )
        with ctx.step("замеры финала"):
            measures = measure_final(final, skip_head_sec=fade_in, skip_tail_sec=fade_out)
            # частота готового файла — из самого файла, а не из того, что просили:
            # расхождение с частотой эпизода и есть пересчёт кадров
            result_final.fps = probe_fps(final)

        result_final.video = ctx.rel(final)
        result_final.duration_sec = round(mix.duration_sec, 3)
        result_final.inputs_fingerprint = fingerprint
        result_final.video_fingerprint = picture
        result_final.watermark = (
            ctx.project.watermark.file if watermark is not None and ctx.project else None
        )
        result_final.headline = headline.text if headline is not None else None
        result_final.frozen_sec = round(sum(s.measured_sec.get("frozen", 0.0) for s in scenes), 3)
        with ctx.step("дрейф звука"):
            result_final.av_drift_sec = measure_av_drift(final)
        result_final.music_track = track.id if track else None
        # то, что реально ушло в микс (план или точка входа трека), а не поле плана
        result_final.music_start_sec = music_start(ctx, plan, track) if track else 0.0
        result_final.loudness_lufs = mix.loudness_lufs
        result_final.true_peak_dbtp = mix.true_peak_dbtp
        result_final.voice_lufs = mix.voice_lufs
        result_final.music_lufs = mix.music_lufs
        result_final.bed_track = bed_track.id if bed_track else None
        result_final.bed_lufs = mix.bed_lufs
        result_final.black_sec = measures.black_sec
        result_final.freeze_detected_sec = measures.freeze_sec
        result_final.silence_sec = measures.silence_sec
        # окно замера кончается там же, где у measure_final: та же длительность
        # того же файла минус fade out
        result_final.silence_tail_sec = silence_tail(
            measures.silence, end=mix.duration_sec - fade_out
        )
        result_final.silence_gaps = silence_gaps(measures.silence, timeline)
        result_final.sentence_pause_sec = pauses.sentence_median_sec
        result_final.long_pauses = len(pauses.long) if pauses.words else None
        result_final.long_pause_gaps = sorted(
            (
                SilenceGap(scene_id=p.scene_id, at_sec=p.at_sec, sec=p.sec)
                for p in sorted(pauses.long, key=lambda p: p.sec, reverse=True)[:SILENCE_GAPS_KEPT]
            ),
            key=lambda g: g.at_sec,
        )
        result_final.cuts = max(0, len(clips) - 1)
        result_final.transitions = transitions
        result_final.effects = len(effects)
        result_final.sfx = len(sounds)
        result_final.outro_added_sec = outro_added
        result_final.edit_plan = (
            ctx.rel(plan_path(ctx.project_dir)) if plan_path(ctx.project_dir).exists() else None
        )
        result_final.edit_author = (
            plan.author if not plan_note.startswith("по умолчанию") else "default"
        )
        black_by_scene = _per_scene_seconds(measures.black, timeline)
        freeze_by_scene = _per_scene_seconds(measures.freeze, timeline)
        silence_by_scene = _per_scene_seconds(measures.silence, timeline)
        for scene in scenes:
            scene.montage.black_sec = black_by_scene[scene.id]
            scene.montage.freeze_sec = freeze_by_scene[scene.id]
            scene.montage.silence_sec = silence_by_scene[scene.id]
        ctx.save()
        (out_dir / TIMELINE_FILE).write_text(
            json.dumps(
                [
                    {
                        "scene_id": t.scene_id,
                        "start": round(t.start, 3),
                        "end": round(t.end, 3),
                        "lead": t.lead,
                    }  # fmt: skip
                    for t in timeline
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (out_dir / "montage.json").write_text(
            json.dumps(
                {
                    "plan": plan_note,
                    "final": result_final.model_dump(),
                    "music_steps": music_steps,
                    "effects": [e.model_dump() for e in plan.effects],
                    "sfx": [c.model_dump() for c in plan.sfx],
                    "outro": plan.outro.model_dump() if plan.outro else None,
                    "scenes": [
                        {
                            "id": s.id,
                            "duration_sec": s.measured_sec.get("clip"),
                            **s.montage.model_dump(),
                        }
                        for s in scenes
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        summary = format_final(
            result_final,
            scenes,
            captions=style is not None,
            music=track is not None,
            fps_expected=ctx.storyboard.fps,
        )
        if plan_note.startswith("по умолчанию — план"):
            summary += f"; ПЛАН: {plan_note}"
        if untranslated:
            summary += (
                f"; ПЕРЕВОД: у сцен {', '.join(untranslated)} нет субтитров на "
                f"{ctx.storyboard.captions_language} — вышли на языке речи (стадия translate)"
            )
        if muted_scenes:
            summary += f"; звук оригинала заглушён: {', '.join(muted_scenes)}"
        if notes:
            summary += "; ПРОВЕРЬ: " + "; ".join(notes)
        return StageResult(status="done", message=f"{ctx.rel(final)} ({summary})", changed=True)
