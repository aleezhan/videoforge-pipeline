"""Стековые раскладки кадрирования: «двое друг над другом» и «вебка сверху,
экран снизу» (14.09.2026, features/reframe_two_up_punch_in, reframe_facecam_screen).

Правило выбора и геометрия окон проверяются на координатах без видео; рендер
`stack_crops` — на синтетическом исходнике с НАСТОЯЩИМ лицом (кадр из
library/caption_color/dark_plate), чтобы YuNet подтвердил: в каждой половине
готового кадра лицо есть. Замер 14.09.2026 на 1920×1080: лица (403,541)
170×218 и (1567,541) 160×218 → two_up, половины 816×726, в верхней лицо
206×284, в нижней 207×287; лицо (204,160) 81×109 в углу + «экран» → facecam,
верх 348×310 вокруг лица, низ 1214×1080 по центру экрана.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_test_video

from videoforge.core.faces import Face
from videoforge.core.ffmpeg import CropTrack, probe_size, probe_stream_duration, run, stack_crops
from videoforge.core.io import write_model
from videoforge.core.resources import CaptionStyle
from videoforge.core.storyboard import ReframeWindow, Scene, Storyboard
from videoforge.stages.assemble import over_webcam
from videoforge.stages.qa import stack_seam_findings
from videoforge.stages.reframe import (
    FACECAM_MAX_SHARE,
    FIT_SHARE,
    TWO_UP_SIZE_RATIO,
    DetectAt,
    Reframe,
    Window,
    WindowPlan,
    _crop_size,
    _render,
    choose_layout,
    facecam_seam,
    facecam_split,
    game_band,
    plan_windows,
    stacked_spans,
    webcam_face,
)

W, H = 3840, 2160
CROP_W = 1214  # окно 9:16 во всю высоту 4K-кадра
TARGET = (1080, 1920)
FACE_JPG = (
    Path(__file__).resolve().parents[1] / "library" / "caption_color" / "dark_plate" / "before.jpg"
)


def _face(cx: float, cy: float = 1000.0, w: float = 300.0, score: float = 0.9) -> Face:
    return Face(x=cx - w / 2, y=cy - w / 2, w=w, h=w, score=score)


def _box(cx: float, cy: float, w: float, h: float, score: float) -> Face:
    """Лицо как его отдал YuNet на живом кадре: центр, размер, уверенность."""
    return Face(x=cx - w / 2, y=cy - h / 2, w=w, h=h, score=score)


def test_layout_is_chosen_by_the_faces_alone() -> None:
    """Три случая правила и его пороги — по координатам, до единого рендера."""
    # общий план на двоих: лица сопоставимы и дальше ширины окна — стек
    assert choose_layout([_face(830), _face(2940)], W, H, crop_w=CROP_W) == "two_up"
    # те же двое рядом — влезают в одно окно, стек не нужен
    assert choose_layout([_face(1900), _face(2200)], W, H, crop_w=CROP_W) == "single"
    # второе лицо мелкое (гость в глубине кадра) — кадрируем по крупному, как раньше
    tiny = _face(2940, w=300 * TWO_UP_SIZE_RATIO * 0.9)
    assert choose_layout([_face(830), tiny], W, H, crop_w=CROP_W) == "single"
    # порог «далеко» — ровно тот, по которому window_for решает, что второе не влезло
    edge = 830 + CROP_W * FIT_SHARE
    assert choose_layout([_face(830), _face(edge + 2)], W, H, crop_w=CROP_W) == "two_up"
    assert choose_layout([_face(830), _face(edge - 2)], W, H, crop_w=CROP_W) == "single"

    # стример: маленькое уверенное лицо в углу — игра сверху, вебка снизу
    small = H * FACECAM_MAX_SHARE * 0.8
    assert choose_layout([_face(300, 250, w=small)], W, H, crop_w=CROP_W) == "facecam_screen"
    assert choose_layout([_face(3500, 1900, w=small)], W, H, crop_w=CROP_W) == "facecam_screen"
    # то же лицо посреди высоты — общий план издалека или вебка на перерыве
    assert choose_layout([_face(1900, 1000, w=small)], W, H, crop_w=CROP_W) == "single"
    assert choose_layout([_face(300, 1000, w=small)], W, H, crop_w=CROP_W) == "single"
    # крупное лицо в углу — человек сидит с краю, одно окно его накроет
    assert choose_layout([_face(300, 250, w=600)], W, H, crop_w=CROP_W) == "single"
    # неуверенное лицо в углу — иконка панели, а не человек
    assert choose_layout([_face(300, 250, w=small, score=0.75)], W, H, crop_w=CROP_W) == "single"

    assert choose_layout([], W, H, crop_w=CROP_W) == "single"
    assert choose_layout([_face(2610)], W, H) == "single"  # окно по умолчанию — 9:16 от кадра


def test_streamer_face_is_small_confident_and_in_the_corner() -> None:
    """Числа с живых стримов Dota 2 (tests/t05, t06, 1920×1080, 14.09.2026).

    Вебка 396×223 в точке (335, 857): лицо стримера (500,975) 50×65 со score
    0.9. YuNet ловит и героев игры — заставку 217×166 в углу (score 0.62),
    героя 83×105 у панели (0.65), портреты HUD 22–36 px (0.61–0.80) — и
    крупнейшее лицо кадра вебкой не является. Раскладка решается вебкой:
    маленькое уверенное лицо в углу побеждает всё остальное; пары иконок не
    дают two_up, а вебка во весь экран на перерыве — уже не вебка."""
    w, h = 1920, 1080
    cam = _box(500, 975, 50, 65, 0.91)
    assert choose_layout([cam], w, h) == "facecam_screen"
    assert webcam_face([cam], w, h) is cam
    # t06, 5 с: заставка героя в углу крупнее вебки — вебка всё равно решает
    splash = _box(477, 82, 217, 166, 0.62)
    assert choose_layout([splash, cam], w, h) == "facecam_screen"
    # t06, 37 с: герой у панели с вебкой раньше проходили как «двое»
    hero = _box(1371, 954, 83, 105, 0.65)
    assert choose_layout([hero, cam], w, h) == "facecam_screen"
    # портреты в верхней панели по краям — не двое собеседников
    hud = [_box(1081, 23, 29, 36, 0.70), _box(1269, 17, 23, 26, 0.67)]
    assert choose_layout(hud, w, h) == "single"
    assert choose_layout([*hud, _box(1527, 527, 33, 40, 0.69)], w, h) == "single"
    # перерыв: вебка во весь экран — крупное лицо посреди кадра, одно окно
    assert choose_layout([_box(960, 500, 300, 380, 0.93)], w, h) == "single"
    # подкаст 4K (h19, 8 с): головы на 0.30 высоты, 8.8–10% кадра — двое, не стрим
    podcast = [_box(2992, 637, 156, 224, 0.90), _box(849, 639, 139, 190, 0.92)]
    assert choose_layout(podcast, W, H, crop_w=CROP_W) == "two_up"
    assert webcam_face(podcast, W, H) is None
    # по приказу человека вебкой считается любое лицо в углу — и крупное
    big_cam = _box(300, 900, 200, 260, 0.9)
    assert webcam_face([big_cam], w, h) is None
    assert webcam_face([big_cam], w, h, strict=False) is big_cam


def test_webcam_of_any_size_up_to_a_sixth_and_anywhere_along_the_edge() -> None:
    """Первые восемь роликов stream_clips (17.09.2026, 1920×1080): лицо в вебке
    стримера A (0.10, 0.85) 10% кадра, стримера B (0.07, 0.90) 11%, стримера C (0.13,
    0.86) 7.4–8.2%, стример A в лобби (0.40, 0.82) 13% — прежние «< 8%» и «в крайних
    30% по ширине» (цифры вебки стримера D) теряли раскладку в пяти роликах из восьми."""
    w, h = 1920, 1080
    for cx, cy, share in (
        (0.10, 0.85, 0.10),
        (0.07, 0.90, 0.11),
        (0.13, 0.86, 0.082),
        (0.40, 0.82, 0.13),
    ):
        face = _box(cx * w, cy * h, share * h * 0.8, share * h, 0.91)
        assert choose_layout([face], w, h) == "facecam_screen", (cx, cy, share)
    # вебка во весь экран на перерыве — лицо крупное и посреди высоты
    assert choose_layout([_box(960, 500, 300, 380, 0.93)], w, h) == "single"
    # у нижнего края, но крупнее шестой части кадра — не вебка
    assert choose_layout([_box(500, 880, 180, 200, 0.92)], w, h) == "single"


def test_game_window_stays_clear_of_the_webcam() -> None:
    """Окно игры кончается над вебкой (или начинается под ней) с зазором: на
    стриме t05 лицо (500,975) 65 px → вебка 217 px → игра 0–823 — выше и
    вебки (857), и панели способностей (930)."""
    top, band = game_band(975.0, 217, height=1080)
    assert top == 0 and 810 <= band <= 830
    top, band = game_band(160.0, 243, height=1080)  # вебка в верхнем углу
    assert 320 <= top <= 330 and top + band == 1080


def test_manual_layout_beats_the_rule(tmp_path: Path) -> None:
    """Человек сказал «двое друг над другом» — детектор ставит окна, но
    раскладку не оспаривает; сказал «одно окно» — стек не включается, сколько
    бы лиц ни было."""
    clip = make_test_video(tmp_path / "podcast.mp4", seconds=6.0, size="640x360")

    def two(_clip: Path, seconds: list[float]) -> list[list[Face]]:
        return [[_face(830), _face(2940)] for _ in seconds]

    def one(_clip: Path, seconds: list[float]) -> list[list[Face]]:
        return [[_face(1900)] for _ in seconds]

    def never(_clip: Path, seconds: list[float]) -> list[list[Face]]:
        raise AssertionError("детектор не должен вызываться")

    def plan(
        detect: DetectAt,
        manual: ReframeWindow | None,
        *,
        crop: tuple[int, int] = (CROP_W, H),
        target: tuple[int, int] = TARGET,
    ) -> WindowPlan:
        return plan_windows(
            clip,
            duration=6.0,
            width=W,
            height=H,
            crop=crop,
            target=target,
            manual=manual,
            detect=detect,
        )

    auto = plan(two, None)
    assert [w.layout for w in auto.windows] == ["two_up"]
    assert auto.stack_top == auto.stack_bottom and auto.stack_top is not None
    top, bottom = auto.windows[0].top, auto.windows[0].bottom
    assert top and bottom and top[0] < bottom[0]  # левый сверху, правый снизу
    assert any("друг над другом" in w for w in auto.warnings)

    forced_single = plan(two, ReframeWindow(layout="single"))
    assert [w.layout for w in forced_single.windows] == ["single"]
    assert forced_single.stack_top is None and forced_single.by == "yunet"

    # приказ two_up на кадре, где второго лица нет, — одно окно: стекать нечего
    forced_two = plan(one, ReframeWindow(layout="two_up"))
    assert [w.layout for w in forced_two.windows] == ["single"]

    # всё руками: x и x2 — окна статичны, детектор не зовётся вовсе
    manual = plan(never, ReframeWindow(layout="two_up", x=0.2, x2=0.8, punch=2.0))
    assert manual.by == "manual" and len(manual.windows) == 1
    window = manual.windows[0]
    assert window.layout == "two_up" and window.top and window.bottom
    assert manual.stack_top == (1214, 1080)  # половина высоты 4K (punch 2) с пропорцией 9:8
    assert window.top[0] + 607 == pytest.approx(0.2 * W, abs=2)
    assert window.bottom[0] + 607 == pytest.approx(0.8 * W, abs=2)
    # стек — ответ вертикали: для 16:9 выхода тот же приказ остаётся одним окном
    wide = plan(
        never, ReframeWindow(layout="two_up", x=0.2, x2=0.8), crop=(W, H), target=(1920, 1080)
    )
    assert wide.windows[0].layout == "single" and any("вертикал" in w for w in wide.warnings)


def test_punch_only_zooms_in() -> None:
    """Отъезд из 16:9 в 9:16 невозможен без полос — окно и так во всю высоту."""
    with pytest.raises(ValueError, match="two_up"):
        ReframeWindow(punch=0.8)
    assert ReframeWindow(punch=1.0).punch == 1.0
    assert ReframeWindow(x=0.5, punch=1.5).layout is None


def test_stack_lives_only_in_its_shots(tmp_path: Path) -> None:
    """Крупный план одного говорящего между общими планами остаётся одним
    окном: второй половине в нём показывать нечего. Стек — по секундам."""
    clip = make_test_video(tmp_path / "podcast.mp4", seconds=12.0, size="640x360")

    def detector(_clip: Path, seconds: list[float]) -> list[list[Face]]:
        # 0–4 общий план, 4–8 крупный план одного, 8–12 снова общий
        return [[_face(830), _face(2940)] if not 4.0 <= t < 8.0 else [_face(1900)] for t in seconds]

    plan = plan_windows(
        clip,
        duration=12.0,
        width=W,
        height=H,
        crop=(CROP_W, H),
        target=TARGET,
        manual=None,
        detect=detector,
    )
    layouts = [w.layout for w in plan.windows]
    assert layouts.count("two_up") >= 2 and "single" in layouts
    spans = stacked_spans(plan.windows, 12.0)
    assert len(spans) == 2 and spans[0][0] == 0.0 and spans[-1][1] == 12.0
    assert abs(spans[0][1] - 4.0) <= 0.3 and abs(spans[1][0] - 8.0) <= 0.3


def test_break_with_fullscreen_webcam_falls_back_to_one_window(tmp_path: Path) -> None:
    """Стрим: игра с вебкой в углу, потом перерыв — вебка во весь экран. Стек
    только там, где вебка маленькая; на перерыве одно окно по лицу."""
    clip = make_test_video(tmp_path / "stream.mp4", seconds=8.0, size="640x360")

    def detector(_clip: Path, seconds: list[float]) -> list[list[Face]]:
        return [
            [_box(500, 975, 50, 65, 0.91)] if t < 4.0 else [_box(960, 520, 300, 380, 0.93)]
            for t in seconds
        ]

    plan = plan_windows(
        clip,
        duration=8.0,
        width=1920,
        height=1080,
        crop=(608, 1080),
        target=TARGET,
        manual=None,
        detect=detector,
    )
    layouts = [w.layout for w in plan.windows]
    assert layouts[0] == "facecam_screen" and layouts[-1] == "single"
    spans = stacked_spans(plan.windows, 8.0)
    assert len(spans) == 1 and spans[0][0] == 0.0 and abs(spans[0][1] - 4.0) <= 0.3
    # низ — вебка вокруг лица, верх — игра над ней с зазором
    first = plan.windows[0]
    assert first.top and first.bottom and plan.stack_top and plan.stack_bottom
    assert first.bottom[1] + plan.stack_bottom[1] >= 1075  # окно вебки у нижнего края
    assert first.top[1] + plan.stack_top[1] < first.bottom[1]  # игра кончается над вебкой


def test_facecam_is_recognised_only_on_a_stream(tmp_path: Path) -> None:
    """Студийный подкаст (studio_clips, 17.09.2026): в
    верхних углах общего плана — гипсовые бюсты на полках, YuNet ловит их
    маленькими уверенными лицами, и правило вебки клало бюст в нижнюю половину.
    Вне записи стрима вебку не ищем; приказ человека действует всегда."""
    bust = _box(260, 150, 48, 60, 0.91)  # маленькое, уверенное, в верхнем углу
    assert choose_layout([bust], 1920, 1080) == "facecam_screen"
    assert choose_layout([bust], 1920, 1080, facecam=False) == "single"

    clip = make_test_video(tmp_path / "studio.mp4", seconds=4.0, size="640x360")
    kwargs = dict(
        duration=4.0,
        width=1920,
        height=1080,
        crop=(608, 1080),
        target=TARGET,
        detect=lambda _clip, seconds: [[bust] for _ in seconds],
    )
    podcast = plan_windows(clip, manual=None, facecam=False, **kwargs)  # type: ignore[arg-type]
    assert [w.layout for w in podcast.windows] == ["single"]
    ordered = plan_windows(
        clip,
        manual=ReframeWindow(layout="facecam_screen"),
        facecam=False,
        **kwargs,  # type: ignore[arg-type]
    )
    assert [w.layout for w in ordered.windows] == ["facecam_screen"]


def test_facecam_keeps_the_whole_webcam_under_the_game(tmp_path: Path) -> None:
    """Стек facecam не пополам (T-34, 17.09.2026): вебка целиком в своей
    пропорции 16:9 во всю ширину (1080×608), игра — всё остальное (1080×1312),
    как в одобренных пробах t05/t06. Половина 9:8 вокруг лица растягивала окно
    244×217 в 4.4 раза и срезала подбородок."""
    assert facecam_split(TARGET) == 1312
    clip = make_test_video(tmp_path / "stream.mp4", seconds=4.0, size="640x360")
    cam = _box(500, 975, 50, 65, 0.91)  # лицо стримера t05 в вебке 396×223
    plan = plan_windows(
        clip,
        duration=4.0,
        width=1920,
        height=1080,
        crop=(608, 1080),
        target=TARGET,
        manual=None,
        detect=lambda _clip, seconds: [[cam] for _ in seconds],
    )
    assert [w.layout for w in plan.windows] == ["facecam_screen"]
    assert plan.stack_top and plan.stack_bottom
    bw, bh = plan.stack_bottom
    assert abs(bw / bh - 16 / 9) < 0.02 and 200 <= bh <= 230  # ≈ сама вебка
    tw, th = plan.stack_top
    assert abs(tw / th - 1080 / 1312) < 0.01
    assert plan.stack_seam == pytest.approx(1312 / 1920)

    manual = plan_windows(
        clip,
        duration=4.0,
        width=1920,
        height=1080,
        crop=(608, 1080),
        target=TARGET,
        manual=ReframeWindow(layout="facecam_screen", x=0.26, y=0.9, punch=5.0),
    )
    assert manual.by == "manual" and manual.stack_seam == pytest.approx(1312 / 1920)
    assert (
        manual.stack_bottom and abs(manual.stack_bottom[0] / manual.stack_bottom[1] - 16 / 9) < 0.02
    )


def _passport(video: Path, windows: list[Window], seam: float | None) -> None:
    video.parent.mkdir(parents=True, exist_ok=True)
    write_model(
        video.with_suffix(".json"),
        Reframe(
            source="03_video/s01.mp4",
            width=1920,
            height=1080,
            windows=windows,
            by="yunet",
            crop_w=608,
            crop_h=1080,
            stack_seam=seam,
            duration_sec=10.0,
        ),
    )


def test_captions_sit_over_the_webcam_only_when_every_scene_is_facecam(tmp_path: Path) -> None:
    """Стиль по центру клал реплику ровно на стык игры и вебки (QA трёх первых
    эпизодов stream_clips); над вебкой, внизу игры, — как в t05. Стек только в
    части планов — стиль как есть."""
    seam = 1312 / 1920
    cam = Window(start_sec=0.0, x=0, layout="facecam_screen", top=(0, 0), bottom=(0, 0))
    _passport(tmp_path / "all" / "s01.mp4", [cam], seam)
    _passport(tmp_path / "part" / "s01.mp4", [cam, Window(start_sec=5.0, x=0)], seam)
    assert facecam_seam(tmp_path / "all" / "s01.mp4") == pytest.approx(seam)
    assert facecam_seam(tmp_path / "part" / "s01.mp4") is None
    assert facecam_seam(tmp_path / "nope.mp4") is None

    def ctx(folder: str):  # type: ignore[no-untyped-def]
        scene = Scene(id="s01", order=1)
        scene.assets.video = "s01.mp4"
        sb = Storyboard(project_id="tw", scenes=[scene])
        return SimpleNamespace(storyboard=sb, abs=lambda rel: tmp_path / folder / rel)

    style = CaptionStyle(id="k", position="center", margin_pct=0.0)
    moved = over_webcam(ctx("all"), style)  # type: ignore[arg-type]
    assert moved.position == "bottom"
    assert moved.margin_pct == pytest.approx((1 - seam) * 100 + 2, abs=0.01)
    assert style.position == "center"  # ресурс не тронут — копия
    assert over_webcam(ctx("part"), style) == style  # type: ignore[arg-type]

    # QA сверяет полосу со стыком СЦЕНЫ: у facecam он над вебкой, не посередине
    stacked = {"s01": [(0.0, 10.0)]}
    assert stack_seam_findings((0.45, 0.55), stacked, seams={"s01": seam}) == []
    found = stack_seam_findings((0.64, 0.70), stacked, seams={"s01": seam})
    assert len(found) == 1 and "стык на 68%" in found[0].message


def test_stacked_spans_merge_neighbours() -> None:
    windows = [
        Window(start_sec=0.0, x=0, layout="two_up", top=(0, 0), bottom=(0, 0)),
        Window(start_sec=2.0, x=0, layout="two_up", top=(10, 0), bottom=(0, 0)),
        Window(start_sec=5.0, x=0),
        Window(start_sec=7.0, x=0, layout="facecam_screen", top=(0, 0), bottom=(0, 0)),
    ]
    assert stacked_spans(windows, 10.0) == [(0.0, 5.0), (7.0, 10.0)]
    assert stacked_spans([Window(start_sec=0.0, x=0)], 10.0) == []


def test_seam_finding_only_when_the_band_crosses_the_middle() -> None:
    """Полоса субтитров поперёк стыка половин — замечание с секундами стека;
    полоса внизу, как у стилей под соцсети, стык не трогает."""
    stacked = {"s01": [(2.1, 9.0), (32.5, 48.3)]}
    assert stack_seam_findings((0.7, 0.78), stacked) == []  # низ, стык на 50% не задет
    assert stack_seam_findings((0.45, 0.55), {}) == []  # стека в ролике нет
    found = stack_seam_findings((0.45, 0.55), stacked)
    assert len(found) == 1 and found[0].code == "caption_seam" and found[0].scene_id == "s01"
    assert "2.1–9.0 с" in found[0].message and "32.5–48.3 с" in found[0].message
    plates = stack_seam_findings(
        (0.45, 0.55), stacked, what="плашки", margin_field="overlay_margin_pct", code="overlay"
    )
    assert plates[0].code == "overlay_seam" and "overlay_margin_pct" in plates[0].message


# ------------------------------------------------------------------ рендер


def _head(out: Path, height: int) -> Path:
    """Голова из кадра библиотеки, масштаб до нужной высоты."""
    iw, ih = probe_size(FACE_JPG) or (608, 1080)
    run(
        [
            "-i", str(FACE_JPG),
            "-vf", f"crop={int(iw * 0.7)}:{int(ih * 0.56)}:{int(iw * 0.15)}:{int(ih * 0.14)},"
                   f"scale=-2:{height}",
            str(out),
        ]
    )  # fmt: skip
    return out


def _two_faces(out: Path, face: Path) -> Path:
    fw, fh = probe_size(face) or (0, 0)
    run(
        [
            "-f", "lavfi", "-i", "color=c=0x404040:size=1920x1080:rate=24:duration=3",
            "-i", str(face), "-i", str(face),
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3",
            "-filter_complex",
            f"[0:v][1:v]overlay={380 - fw // 2}:{(1080 - fh) // 2}[a];"
            f"[a][2:v]overlay={1540 - fw // 2}:{(1080 - fh) // 2}[v]",
            "-map", "[v]", "-map", "3:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(out),
        ]
    )  # fmt: skip
    return out


def _facecam(out: Path, face: Path) -> Path:
    fw, fh = probe_size(face) or (0, 0)
    run(
        [
            "-f", "lavfi", "-i", "color=c=0x2040a0:size=1920x1080:rate=24:duration=3",
            "-f", "lavfi", "-i", "color=c=0x30c060:size=900x500:rate=24:duration=3",
            "-i", str(face),
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3",
            "-filter_complex",
            f"[0:v][1:v]overlay={(1920 - 900) // 2}:{(1080 - 500) // 2}[s];"
            f"[s][2:v]overlay={192 - fw // 2}:{162 - fh // 2}[v]",
            "-map", "[v]", "-map", "3:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(out),
        ]
    )  # fmt: skip
    return out


def _faces_in_halves(
    video: Path, out_dir: Path, seam: float = 0.5
) -> tuple[list[Face], list[Face]]:
    """Лица выше и ниже стыка (`seam` — доля высоты): у two_up он посередине,
    у facecam — над вебкой."""
    from videoforge.core.faces import detect_images

    top, bottom = out_dir / "top.jpg", out_dir / "bottom.jpg"
    run(
        [
            "-ss",
            "1.5",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"crop=iw:ih*{seam}:0:0",
            str(top),
        ]
    )
    run(
        [
            "-ss",
            "1.5",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"crop=iw:ih*{1 - seam}:0:ih*{seam}",
            str(bottom),
        ]
    )
    found = detect_images([top, bottom])
    return found[0], found[1]


@pytest.mark.render
def test_two_up_puts_a_face_in_each_half(tmp_path: Path) -> None:
    """Синтетический общий план: одно и то же лицо слева и справа 1920×1080 →
    стадия сама выбирает two_up, и YuNet находит лицо в обеих половинах
    готового 1080×1920. Звук переживает стек."""
    pytest.importorskip("cv2")
    if not FACE_JPG.exists():
        pytest.skip("нет кадра с лицом в library/")
    src = _two_faces(tmp_path / "two.mp4", _head(tmp_path / "head.png", 300))
    crop = _crop_size(1920, 1080, TARGET)
    plan = plan_windows(
        src, duration=3.0, width=1920, height=1080, crop=crop, target=TARGET, manual=None
    )
    assert plan.by == "yunet" and [w.layout for w in plan.windows] == ["two_up"]
    assert plan.stack_top == plan.stack_bottom and plan.stack_top is not None
    out = tmp_path / "two_reframed.mp4"
    _render(src, out, plan, duration=3.0, size=TARGET)
    assert probe_size(out) == TARGET
    assert (probe_stream_duration(out, "0:a:0") or 0) > 2.5
    top, bottom = _faces_in_halves(out, tmp_path)
    assert top and bottom, (top, bottom)
    # лицо стоит по центру своей половины, а не у края: в каждой половине оно
    # заняло около трети высоты (TWO_UP_FACE_SHARE), как и просили
    for faces in (top, bottom):
        assert abs(faces[0].center_x - 540) < 120 and abs(faces[0].center_y - 480) < 120
        assert 0.2 < faces[0].h / 960 < 0.45


@pytest.mark.render
def test_screen_goes_on_top_and_the_facecam_below(tmp_path: Path) -> None:
    """Стример: маленькое лицо в верхнем углу и «экран» посреди кадра → игра
    сверху, вебка снизу; в верхней половине лица нет, и окно игры начинается
    ниже окна вебки."""
    pytest.importorskip("cv2")
    if not FACE_JPG.exists():
        pytest.skip("нет кадра с лицом в library/")
    src = _facecam(tmp_path / "stream.mp4", _head(tmp_path / "head.png", 100))
    crop = _crop_size(1920, 1080, TARGET)
    plan = plan_windows(
        src, duration=3.0, width=1920, height=1080, crop=crop, target=TARGET, manual=None
    )
    assert [w.layout for w in plan.windows] == ["facecam_screen"], plan.warnings
    window = plan.windows[0]
    assert window.top and window.bottom and plan.stack_top and plan.stack_bottom
    assert plan.stack_bottom[1] < 300  # вебка — окно вокруг лица, не полкадра
    assert window.bottom[1] < 100  # у верхнего края, где лицо
    assert window.top[1] > window.bottom[1] + plan.stack_bottom[1]  # игра ниже вебки
    assert abs(window.top[0] + plan.stack_top[0] / 2 - 960) <= 2  # игра по центру кадра
    out = tmp_path / "stream_reframed.mp4"
    _render(src, out, plan, duration=3.0, size=TARGET)
    seam = plan.stack_seam or 0.5
    assert seam == pytest.approx(1312 / 1920)  # вебка целиком внизу, а не половина
    top, bottom = _faces_in_halves(out, tmp_path, seam)
    assert bottom and not top
    # лицо в полосе вебки 1080×608: окно вебки у угла упирается в край кадра,
    # поэтому по горизонтали лицо не обязано быть по центру
    assert 0 < bottom[0].center_x < 1080 and abs(bottom[0].center_y - 304) < 150


@pytest.mark.render
def test_mixed_scene_stacks_only_between_its_seconds(tmp_path: Path) -> None:
    """Стек по времени в одном графе: до второй секунды одно окно, потом стек.
    Проверяется геометрией картинки, без детектора: у окна-одиночки кадр
    равномерный, у стека в верхней половине другой цвет, чем в нижней."""
    src = tmp_path / "src.mp4"
    run(
        [
            "-f", "lavfi", "-i", "color=c=red:size=1920x1080:rate=24:duration=6",
            "-f", "lavfi", "-i", "color=c=blue:size=720x1080:rate=24:duration=6",
            "-filter_complex", "[0:v][1:v]overlay=1200:0[v]", "-map", "[v]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src),
        ]
    )  # fmt: skip
    out = tmp_path / "mixed.mp4"
    stack_crops(
        src,
        out,
        top=CropTrack(600, 534, [(0.0, 0, 0)]),  # красное поле
        bottom=CropTrack(600, 534, [(0.0, 1300, 0)]),  # синяя полоса справа
        size=TARGET,
        single=CropTrack(608, 1080, [(0.0, 0, 0)]),  # только красное
        stacked=[(2.0, 6.0)],
    )
    assert probe_size(out) == TARGET
    from videoforge.core.ffmpeg import band_luminance

    lower = band_luminance(out, 0.55, 0.95, fps=2)  # низ кадра до и после стека
    before = [y for t, y in lower if t < 1.8]
    after = [y for t, y in lower if t > 2.2]
    # красный (Y≈81) → синий (Y≈41): нижняя половина сменилась ровно на второй секунде
    assert before and after and min(before) > 65 and max(after) < 55
