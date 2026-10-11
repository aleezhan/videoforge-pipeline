"""Плашки (Scene.overlay): текст, который зритель читает, рисует сборка
поверх кадра — в картинке текста нет. На заглушках, без денег."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core.captions import (
    OverlayEvent,
    build_ass,
    caption_band,
    overlay_view,
    overlay_window,
    stretch_overlays,
)
from videoforge.core.manifest import load_manifest
from videoforge.core.qa import load_qa
from videoforge.core.report import render_report
from videoforge.core.resources import CaptionStyle
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import (
    OVERLAY_MAX_CHARS,
    Scene,
    SceneOverlay,
    SourceSegment,
    load_storyboard,
    save_storyboard,
)
from videoforge.providers.base import ProviderSet
from videoforge.stages.assemble import ASS_FILE, TimelineEntry, overlay_events
from videoforge.stages.planner import parse_overlay, parse_scenes
from videoforge.stages.publish import PublishPlan, hook_hint, render_page
from videoforge.stages.qa import overlay_windows

OPTS = RunOptions(auto_approve=True, raise_errors=True)


def _plate(text: str, kind: str = "caption", emphasis: tuple[str, ...] = ()) -> OverlayEvent:
    return OverlayEvent(0.0, 3.0, text, kind, frozenset(emphasis), "s1")


def test_overlays_live_in_the_same_ass_with_their_own_styles() -> None:
    """Один файл, один фильтр ffmpeg: субтитры внизу, плашки сверху своими
    стилями; ключевое слово — цветом подсветки, остальное — основным."""
    style = CaptionStyle(id="t")
    plates = [
        _plate("Могила — первая остановка", emphasis=("могила",)),
        _plate("ПУНКТ 1", kind="label"),
        _plate("Коран 55:15", kind="source"),
    ]
    ass = build_ass([], style, size=(1920, 1080), overlays=plates, overlay_right_share=0.15)
    styles = {ln.split(",")[0] for ln in ass.splitlines() if ln.startswith("Style:")}
    assert styles == {
        "Style: Default",
        "Style: Overlay",
        "Style: OverlayLabel",
        "Style: OverlaySource",
    }
    dialogues = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogues) == 3
    assert all(ln.startswith("Dialogue: 1,") for ln in dialogues)  # слой над субтитрами
    caption = next(ln for ln in dialogues if "Overlay,," in ln)
    assert "{\\1c&H0000D4FF}Могила{\\r}" in caption  # #FFD400 задом наперёд, как у субтитров
    assert "остановка" in caption and "{\\1c&H0000D4FF}остановка" not in caption
    # раскладка: вывод по центру сверху (8), ярлык слева (7), источник справа (9)
    align = {
        ln.split(",")[0]: ln.split(",")[18] for ln in ass.splitlines() if ln.startswith("Style:")
    }
    kinds = ("Style: Overlay", "Style: OverlayLabel", "Style: OverlaySource")
    assert tuple(align[k] for k in kinds) == ("8", "7", "9")
    # правое поле — колонка кнопок площадки (15% > боковое поле стиля 6%)
    source_style = next(ln for ln in ass.splitlines() if ln.startswith("Style: OverlaySource"))
    assert int(source_style.split(",")[20]) == int(round(1920 * 0.15))
    # без плашек стили плашек не пишутся — старые ASS не меняются
    assert "Overlay" not in build_ass([], style, size=(1920, 1080))


def test_overlay_view_reuses_the_caption_formulas() -> None:
    """Полоса плашки считается теми же формулами, что полоса субтитров:
    сверху, от overlay_margin_pct, высотой в строки предела знаков."""
    style = CaptionStyle(id="t", overlay_margin_pct=5.0, overlay_size_pct=3.6)
    view = overlay_view(style)
    assert view.position == "top" and view.max_chars == OVERLAY_MAX_CHARS
    top, bottom = caption_band(view, (1920, 1080))
    assert top == pytest.approx(0.05, abs=0.001)
    assert 0.08 < bottom < 0.2  # одна-две строки кеглем 3.6% плюс поля подложки
    assert view.box_opacity == style.overlay_box_opacity


def test_stretch_overlays_follows_the_slow_tail() -> None:
    plates = [OverlayEvent(1.0, 2.0, "a", "caption"), OverlayEvent(9.0, 10.0, "b", "caption")]
    out = stretch_overlays(plates, from_sec=8.0, speed=0.5)
    assert (out[0].start, out[0].end) == (1.0, 2.0)  # голова не двигается
    assert (out[1].start, out[1].end) == (10.0, 12.0)  # хвост вдвое медленнее
    assert stretch_overlays(plates, from_sec=8.0, speed=1.0) is plates


def test_adjacent_scenes_with_the_same_plate_become_one_event() -> None:
    """Ярлык пункта на всех его сценах и продолжения одного плана — одно
    событие, а не плашка, мигающая на каждом стыке."""
    from videoforge.core.storyboard import Scene

    label = SceneOverlay(text="ПУНКТ 2", kind="label")
    scenes = [
        Scene(id="s1", order=1, overlay=label),
        Scene(id="s2", order=2, overlay=label),
        Scene(id="s3", order=3, overlay=SceneOverlay(text="Коран 2:30", kind="source")),
        Scene(id="s4", order=4),
        Scene(id="s5", order=5, overlay=label),
    ]
    timeline = [TimelineEntry(f"s{i}", 3.0 * (i - 1), 3.0 * i) for i in range(1, 6)]
    events = overlay_events(scenes, timeline)
    assert [(e.scene_id, e.start, e.end) for e in events] == [
        ("s1", 0.0, 6.0),
        ("s3", 6.0, 9.0),
        ("s5", 12.0, 15.0),
    ]


# ---------------------------------------------------------------- окно плашки


def test_overlay_window_runs_forward_and_never_backwards() -> None:
    """Окно — секунды от начала сцены, вперёд и неотрицательные; None с любой
    стороны значит «до края сцены», как было до окна."""
    SceneOverlay(text="x", start_sec=0, end_sec=2)
    SceneOverlay(text="x", start_sec=1.5)  # с полутора секунд до конца сцены
    SceneOverlay(text="x", end_sec=2)  # с начала до второй секунды
    with pytest.raises(ValueError, match="больше start_sec"):
        SceneOverlay(text="x", start_sec=2, end_sec=2)
    with pytest.raises(ValueError, match="больше start_sec"):
        SceneOverlay(text="x", start_sec=3, end_sec=1)
    with pytest.raises(ValueError, match="отрицательным"):
        SceneOverlay(text="x", start_sec=-1)
    with pytest.raises(ValueError, match="отрицательным"):
        SceneOverlay(text="x", end_sec=-0.5)


def test_overlay_window_cuts_the_scene_box() -> None:
    """Хук «0–2 с» на сорокасекундной сцене нарезки — две секунды, а не сорок;
    окно шире сцены режется по сцене; окно за сценой — события нет."""

    def plate(**window: float) -> SceneOverlay:
        return SceneOverlay(text="x", **window)

    assert overlay_window(0.0, 40.0, plate()) == (0.0, 40.0)  # как раньше
    assert overlay_window(0.0, 40.0, plate(start_sec=0, end_sec=2)) == (0.0, 2.0)
    # сцена не с нуля финала: окно едет вместе с ней
    assert overlay_window(10.0, 16.0, plate(start_sec=1, end_sec=3)) == (11.0, 13.0)
    assert overlay_window(10.0, 16.0, plate(start_sec=4)) == (14.0, 16.0)
    assert overlay_window(10.0, 16.0, plate(end_sec=2.5)) == (10.0, 12.5)
    # конец за пределами сцены — по сцене
    assert overlay_window(10.0, 16.0, plate(start_sec=5, end_sec=99)) == (15.0, 16.0)
    # окно целиком за сценой: рисовать нечего
    assert overlay_window(10.0, 16.0, plate(start_sec=6)) is None
    assert overlay_window(10.0, 16.0, plate(start_sec=7, end_sec=8)) is None


def test_windowed_plate_becomes_a_short_event_on_the_timeline() -> None:
    """Сборка: событие плашки урезано окном, у сцены без окна — вся сцена;
    окно за сценой в счёт плашек финала не попадает; с окном соседние сцены с
    той же плашкой не сливаются — между событиями зазор, а не стык."""
    hook = SceneOverlay(text="Почему он ушёл?", start_sec=0, end_sec=2)
    scenes = [
        Scene(id="s1", order=1, overlay=hook),
        Scene(id="s2", order=2, overlay=SceneOverlay(text="Коран 2:30", kind="source")),
        Scene(id="s3", order=3, overlay=SceneOverlay(text="x", start_sec=9)),
        Scene(id="s4", order=4, overlay=SceneOverlay(text="ПУНКТ 1", kind="label", end_sec=1)),
        Scene(id="s5", order=5, overlay=SceneOverlay(text="ПУНКТ 1", kind="label", end_sec=1)),
    ]
    timeline = [TimelineEntry(f"s{i}", 6.0 * (i - 1), 6.0 * i) for i in range(1, 6)]
    events = overlay_events(scenes, timeline)
    assert [(e.scene_id, e.start, e.end) for e in events] == [
        ("s1", 0.0, 2.0),
        ("s2", 6.0, 12.0),
        ("s4", 18.0, 19.0),
        ("s5", 24.0, 25.0),
    ]
    # crossfade: начало сцены — после чужого хвоста, окно отложено от него
    lead = [TimelineEntry("s1", 0.0, 6.0), TimelineEntry("s2", 5.0, 12.0, lead=1.0)]
    late = [Scene(id="s1", order=1), Scene(id="s2", order=2, overlay=hook)]
    (ev,) = overlay_events(late, lead)
    assert (ev.start, ev.end) == (6.0, 8.0)
    # ASS рисует ровно окно
    ass = build_ass([], CaptionStyle(id="t"), size=(1080, 1920), overlays=events)
    dialogues = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert dialogues[0].split(",")[1:3] == ["0:00:00.00", "0:00:02.00"]


def test_windowed_plate_follows_the_slow_tail_like_any_other() -> None:
    """Замедленный хвост растягивает окно тем же преобразованием: хук в голове
    ролика не двигается, плашка в хвосте едет и растёт."""
    hook = OverlayEvent(0.0, 2.0, "hook", "caption", frozenset(), "s1")
    tail = OverlayEvent(9.0, 10.0, "tail", "caption", frozenset(), "s2")
    out = stretch_overlays([hook, tail], from_sec=8.0, speed=0.5)
    assert (out[0].start, out[0].end) == (0.0, 2.0)
    assert (out[1].start, out[1].end) == (10.0, 12.0)


def test_qa_measures_the_plate_in_the_seconds_it_is_on_screen() -> None:
    """Контраст плашки к фону считается по окну события, а не по всей сцене:
    иначе хук на 0–2 с мерился бы по сорока секундам фона, под которым текста нет."""
    scenes = [
        Scene(id="s1", order=1, overlay=SceneOverlay(text="hook", start_sec=0, end_sec=2)),
        Scene(id="s2", order=2, overlay=SceneOverlay(text="all")),
        Scene(id="s3", order=3, overlay=SceneOverlay(text="none", start_sec=50)),
        Scene(id="s4", order=4, overlay=SceneOverlay(text="hidden")),
    ]
    for s in scenes[:3]:
        s.montage.overlay = True  # сборка отметила: нарисовано
    timeline = [
        {"scene_id": "s1", "start": 0.0, "end": 40.0, "lead": 0.0},
        {"scene_id": "s2", "start": 40.0, "end": 46.0, "lead": 0.0},
        {"scene_id": "s3", "start": 46.0, "end": 52.0, "lead": 0.0},
        {"scene_id": "s4", "start": 52.0, "end": 58.0, "lead": 0.0},
    ]
    assert overlay_windows(scenes, timeline) == [(0.0, 2.0), (40.0, 46.0)]


def test_publish_reminds_a_clip_without_plates_about_the_hook() -> None:
    """Нарезке без текста поверх кадра publish.md подсказывает плашку-хук с
    готовым JSON на 0–2 с; ставить её — решение человека. У ai_video плашки
    ставит планнер, у нарезки с плашкой подсказка была бы шумом, без
    заголовка подсказывать нечего."""
    seg = SourceSegment(path="sources/vid1/h01.mp4", start_sec=20.0, end_sec=45.0)
    clip = [Scene(id="h01", order=1, source=seg)]
    hint = hook_hint(clip, 'Почему тренер "ушёл"?')
    assert '"start_sec": 0' in hint and '"end_sec": 2' in hint and "h01" in hint
    assert '\\"ушёл\\"' in hint  # JSON, который можно вставить как есть
    assert "длиннее" not in hint
    assert "длиннее" in hook_hint(clip, "слово " * 20)  # заголовок не влезает в плашку
    assert hook_hint(clip, "  ") == ""
    assert hook_hint([Scene(id="s1", order=1)], "Заголовок") == ""  # ai_video — не нарезка
    with_plate = [Scene(id="h01", order=1, source=seg, overlay=SceneOverlay(text="есть"))]
    assert hook_hint(with_plate, "Заголовок") == ""
    page = render_page(PublishPlan(video="f.mp4", title="Заголовок", hook_hint=hint))
    assert "**Плашка-хук:**" in page and "--only assemble" in page
    assert "Плашка-хук" not in render_page(PublishPlan(video="f.mp4", title="Заголовок"))


def test_parse_overlay_rejects_what_cannot_be_read_in_a_second() -> None:
    assert parse_overlay(None, 1) is None and parse_overlay({}, 1) is None
    assert parse_overlay({"text": "  "}, 1) is None
    plain = parse_overlay("Коран 55:15", 1)
    assert plain is not None and plain.kind == "caption" and plain.text == "Коран 55:15"
    with pytest.raises(ValueError, match="overlay.kind"):
        parse_overlay({"kind": "banner", "text": "x"}, 2)
    with pytest.raises(ValueError, match="длиннее"):
        parse_overlay({"kind": "caption", "text": "слово " * 20}, 3)
    # подсвечиваются только слова из самого текста, регистр и знаки не мешают
    plate = parse_overlay(
        {"kind": "caption", "text": "Могила — первая остановка.", "emphasis": ["МОГИЛА", "рай"]},
        4,
    )
    assert plate is not None and plate.emphasis == ["МОГИЛА"]
    # частицы и местоимения цветом ничего не говорят: на ep001 модель красила
    # «не» и «только»; число — ключевое слово, «три слова» держится на числе
    raw = {"kind": "caption", "text": "Он не был ангелом, только 3 слова"}
    plate = parse_overlay({**raw, "emphasis": ["не", "только", "3"]}, 5)
    assert plate is not None and plate.emphasis == ["3"]
    # через parse_scenes плашка доезжает до сцены
    scenes = parse_scenes(
        [{"narration": "x", "visual_prompt": "grave", "overlay": {"kind": "label", "text": "1"}}],
        known_cast=set(),
        default_duration=6.0,
    )
    assert scenes[0].overlay == SceneOverlay(text="1", kind="label")


@pytest.mark.render
def test_plates_are_burned_in_and_measured(
    episode: Path, mock_providers: ProviderSet, tmp_path: Path
) -> None:
    """Сквозной путь: плашка в раскадровке → ASS → финал → монтажный лист,
    QA меряет её контраст, отчёт показывает её в карточке сцены."""
    sb = load_storyboard(episode)
    sb.scenes[0].overlay = SceneOverlay(text="Первая сцена", kind="caption", emphasis=["Первая"])
    sb.scenes[1].overlay = SceneOverlay(text="Коран 55:15", kind="source")
    save_storyboard(episode, sb)

    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    sb = load_storyboard(episode)
    assert sb.final.overlay_events == 2
    assert all(s.montage.overlay for s in sb.scenes)
    ass = (episode / "05_assemble" / ASS_FILE).read_text(encoding="utf-8")
    assert ass.count("Dialogue: 1,") == 2 and "OverlaySource,," in ass
    assert "плашек: 2" in load_manifest(episode).stages["assemble"].message
    qa = load_qa(episode)
    assert qa is not None  # QA отработал поверх плашек и не упал
    # верх кадра у площадок свободен — плашки в зону интерфейса не попадают
    assert not [f for f in qa.findings if f.code == "overlay_zone"]
    html = render_report(episode, sb, load_manifest(episode)).read_text(encoding="utf-8")
    assert "Первая сцена" in html and "источник" in html

    # плашка — вход сборки: изменился текст → финал устарел
    fingerprint = sb.final.inputs_fingerprint
    sb.scenes[0].overlay = SceneOverlay(text="Другой текст", kind="caption")
    save_storyboard(episode, sb)
    only_assemble = RunOptions(auto_approve=True, raise_errors=True, only="assemble")
    run_episode(episode, mock_providers, only_assemble)
    assert load_storyboard(episode).final.inputs_fingerprint != fingerprint


@pytest.mark.render
def test_plates_are_drawn_even_when_captions_are_off(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    """Канал без субтитров под речью всё равно получает то, что зритель должен
    прочитать: плашки идут своим ASS, а покрытие речи субтитрами не считается."""
    cfg = projects_root / "t" / "project.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace("captions: true", "captions: false"),
        encoding="utf-8",
    )
    sb = load_storyboard(episode)
    sb.scenes[0].overlay = SceneOverlay(text="ПУНКТ 1", kind="label")
    save_storyboard(episode, sb)
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    sb = load_storyboard(episode)
    assert sb.final.captions is None and sb.final.caption_events == 0
    assert sb.final.overlay_events == 1
    assert (episode / "05_assemble" / ASS_FILE).exists()
