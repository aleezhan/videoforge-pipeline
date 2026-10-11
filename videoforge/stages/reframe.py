"""Stage `reframe`: a horizontal clip becomes a vertical frame that follows the speaker.

Your long-form source is 16:9 while a short is 9:16; a centre crop cuts two people
in half. The window is placed where the faces are: YuNet (`core/faces.py`) runs on
several probe frames and the 9:16 window sits at the median of face centres.

Key decisions:

* **The window is static within a shot, not within a scene.** A measured 28 s
  segment of a 4K podcast contained four shots with face centres far apart; no
  single window covers them. Shot changes come from `detect_cuts` and the crop
  becomes a piecewise-constant expression over time.
* **A shot also ends where a face jumps.** Two cameras in the same studio look
  alike, so the cut detector can miss a change; a face-centre jump larger than
  `FACE_JUMP_SHARE` of the width is treated as a cut and refined by bisection.
* **Layout is chosen per shot from the faces** (`choose_layout`):
  - `two_up`: two comparable faces too far apart for one window -> two 9:8
    halves stacked, left person on top (order by x, so people never swap halves);
  - `facecam_screen`: a small, confident face in a corner is a facecam over a
    screen -> screen on top, facecam below. Enabled automatically only for
    sources marked as live-stream recordings (`SourceInfo.stream`), because
    busts or posters on a studio shelf look like small confident faces too;
    elsewhere it can be forced with `layout: facecam_screen`;
  - `fit` and `circle`: fixed layouts set by the project or scene.
* Layout is voted per shot from the probes; everything is one ffmpeg graph
  (`stack_crops`), with stacks overlaid on the single window by time.

Deliberately not implemented: tracking a face inside a shot (a crawling window
reads as camera shake), zooming out when a face does not fit, and a side-by-side
layout for horizontal output.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path
from statistics import median

from pydantic import BaseModel, Field, ValidationError

from videoforge.core.circle import (
    background_start,
    circle_on,
    circle_video,
    face_center,
    square_crop,
)
from videoforge.core.faces import Face, FaceError, detect_at
from videoforge.core.ffmpeg import (
    FPS,
    CropTrack,
    FfmpegError,
    FitRect,
    aspect_size,
    detect_cuts,
    fit_rect,
    fit_video,
    probe_duration,
    probe_size,
    reframe_video,
    stack_crops,
)
from videoforge.core.io import read_model, write_model
from videoforge.core.resources import Background, load_resource
from videoforge.core.stage import Stage, StageContext, StageResult
from videoforge.core.storyboard import ReframeLayout, ReframeWindow, Scene

OUT_DIR = "03_video"
SUFFIX = "_reframed"
# раскладки, где окно задано целиком заранее, а детектор планов не зовётся:
# сменилось окно канала или сцены — клип устарел
FIXED_LAYOUTS: tuple[ReframeLayout, ...] = ("fit", "circle")
CIRCLE_PROBES = 5  # проб лиц на сцену у кружка: окно одно на всю сцену

# «где лица в эти секунды» — единственное, что стадии нужно от детектора.
# Отдельным именем, чтобы в тестах подставлялся кадр без OpenCV и без видео
DetectAt = Callable[[Path, list[float]], list[list["Face"]]]

# Порог детекта смен картинки. 0.3 — живое видео (0.05 в анализаторе поставлен
# под 2D-анимацию, где кадры соседних планов похожи); на живом отрезке подкаста
# 0.3 дал ровно четыре плана, которые видно глазами
SHOT_THRESHOLD = 0.3
FRAMES_PER_SHOT = 3
MAX_FRAMES = 60  # потолок на сцену: детект 4K-кадра стоит доли секунды, но их не должно быть сотни
MIN_SHOT_SEC = 0.4  # короче — это не план, а вспышка склейки
# Склейка между двумя камерами одной студии по картинке почти не отличается:
# та же комната, тот же свет, тот же костюм. `detect_cuts` её не видит, и на
# живом отрезке (h19) один «план» длиной 20 с накрыл две камеры — окно стояло
# по левому герою, а правый оказался разрезан краем. Признак, который это ловит:
# лицо не может переехать через кадр между соседними пробами. Прыжок центра
# больше SHARE ширины кадра — это склейка, а не поворот головы
FACE_JUMP_SHARE = 0.15
SAMPLE_SEC = 1.0  # шаг проб внутри плана: чаще незачем, реже пропустишь короткий план
REFINE_STEPS = 3  # делений пополам, чтобы найти саму секунду склейки (1 с → ~0.12 с)
# Соседние планы с почти одинаковым окном получают одно и то же: сдвиг на
# полпроцента кадра не выражает ничего, а на просмотре читается как дёрганье
SNAP_SHARE = 0.02
# Второе лицо «влезает» в окно, если центры лиц ближе этой доли ширины окна:
# лицо у самого края окна выглядит обрезанным. Тот же порог решает и обратное —
# раскладка «двое друг над другом» включается ровно там, где одно окно
# перестаёт справляться, а не по своему, второму числу
FIT_SHARE = 0.8
# Двое друг над другом — два лица сопоставимой величины: меньшее не уже
# половины большего. Собеседник за столом подкаста в общем плане — 0.6–0.9 от
# героя; лицо в глубине кадра (оператор, гость на заднем плане) — 0.2–0.3, и
# стек с ним показал бы половину экрана затылков
TWO_UP_SIZE_RATIO = 0.5
# Оба лица two_up — люди, а не иконки: не ниже 5% высоты кадра. Замер
# 14.09.2026 на стриме Dota 2 (tests/t06_stream_reaction, 1920×1080): портреты
# героев в верхней панели 22–36 px (2–3%), герои на карте 28–40 px (3–4%), и
# пары таких «лиц» по краям кадра давали two_up в 11 пробах из 25; лица
# собеседников в общем плане подкаста (h19, 3840×2160) — 190–224 px (9–10%)
TWO_UP_MIN_FACE_SHARE = 0.05
# Вебка стримера — маленькое лицо в углу, и оно ПОБЕЖДАЕТ более крупное лицо
# посреди кадра: крупное там — герой игры или заставка. Замер на двух стримах
# Dota 2 (t05, t06; 50 проб): лицо стримера (500,975) 47–59×58–77 px — 5–7%
# высоты кадра, x = 0.26, y = 0.90, score 0.89–0.92 в каждой пробе; лица игры
# и HUD — score 0.61–0.80, крупнейшее 217×166 (заставка героя в углу, 0.62).
# Признаки: score от 0.85 (между 0.80 и 0.89) и центр в нижних 30% либо верхних
# 22% высоты (голова собеседника за столом ниже 22% не поднимается: h19 —
# 0.29–0.30, а вебка у нижнего края стоит на 0.77–0.92, у верхнего — 0.10–0.18).
# До 17.09.2026 были ещё «лицо меньше 8% кадра» и «в крайних 30% по ширине» —
# это цифры одной маленькой вебки стримера D, а на подкасте они отделяли вебку от
# собеседников. Правило с тех пор включается только у записи стрима, и на
# первых восьми роликах четырёх стримеров они стоили раскладки в пяти: лицо в
# вебке стримера A 10% кадра, стримера B 11%, стримера C 7.4–8.2% (раскладка мигала),
# а в лобби стример A сидит с вебкой на x = 0.31–0.40. Порог величины — 16%:
# вебка во весь экран на перерыве — лицо от ~30% и посреди высоты
FACECAM_MAX_SHARE = 0.16
FACECAM_SCORE_MIN = 0.85
FACECAM_TOP_SHARE = 0.22
FACECAM_BOTTOM_SHARE = 0.30
# Половина стека two_up — 9:8 (1080×960 из 1080×1920). У two_up лицо занимает
# 30% высоты половины (крупный план говорящего), но окно не уже половины высоты
# исходника: дальше это увеличение пикселей.
# Стек facecam не делится пополам (17.09.2026, T-34): низ — вебка ЦЕЛИКОМ в
# своей пропорции 16:9 во всю ширину выхода (1080×608), верх — игра на всё
# остальное (1080×1312). Так собраны пробы t05/t06, которые одобрил владелец.
# Половина 9:8 вокруг лица брала из вебки 396×223 окно 244×217 и растягивала
# его в 4.4 раза: мыло, и подбородок срезан краем. Окно вебки — вокруг лица
# такого размера, чтобы лицо заняло 30% его высоты: на стриме t05 это 217 px
# при вебке 223, то есть сама вебка. Окно игры — самое большое в пропорции
# верха, которое кончается на GAME_GAP_SHARE высоты кадра выше окна вебки (или
# начинается ниже неё): иначе в игре второй раз видно лицо и панель под ним
FACECAM_RATIO = 16 / 9
FACECAM_FACE_SHARE = 0.30
GAME_GAP_SHARE = 0.04
TWO_UP_FACE_SHARE = 0.30
STACK_MIN_CROP_SHARE = 0.5
# Стык половин стека two_up — середина кадра; у facecam — над вебкой
# (`facecam_split`, в паспорте `stack_seam`). QA сверяет со стыком полосу
# субтитров: текст поперёк шва читается хуже, чем в любой из половин
SEAM_SHARE = 0.5


def facecam_split(target: tuple[int, int]) -> int:
    """Высота верха стека facecam (игры) в пикселях выхода: всё, что над вебкой
    во всю ширину в пропорции FACECAM_RATIO."""
    w, h = target
    return h - int(round(w / FACECAM_RATIO)) // 2 * 2


class Window(BaseModel):
    """Окно кадра одного плана: левый край и время, с которого оно действует.

    `layout` — раскладка плана: `single` — одно окно `x`; стек — верхняя и
    нижняя половины своими окнами (`top`, `bottom` — верхний левый угол в
    пикселях исходника; размеры окон — у паспорта сцены, они на сцену одни).
    two_up: `top` — левый собеседник, `bottom` — правый; facecam_screen:
    `top` — игра, `bottom` — вебка."""

    start_sec: float
    x: int
    faces: int = 0  # сколько лиц нашли в этом плане (0 — окно по центру)
    layout: ReframeLayout = "single"
    top: tuple[int, int] | None = None
    bottom: tuple[int, int] | None = None


class WindowPlan(BaseModel):
    """Что стадия решила по сцене: окна по планам и размеры окон.

    `crop_w`/`crop_h` — одиночное окно (с учётом наезда `punch`), `y` — его верх
    (None — центр: окно во всю высоту). `stack_top`/`stack_bottom` — окна
    половин стека (ширина, высота), None — стека в сцене нет."""

    windows: list[Window]
    # по умолчанию пусто, чтобы паспорта до 14.09.2026 читались: иначе стадия
    # сочла бы готовые кадрированные клипы живых эпизодов устаревшими
    warnings: list[str] = Field(default_factory=list)
    by: str  # yunet | manual | center
    crop_w: int
    crop_h: int
    y: int | None = None
    stack_top: tuple[int, int] | None = None
    stack_bottom: tuple[int, int] | None = None
    # где на выходе кончается верх стека, доля высоты: у facecam — над вебкой
    # (0.683 у 1080×1920). None — середина (SEAM_SHARE), как у two_up и у
    # паспортов до 17.09.2026
    stack_seam: float | None = None
    # раскладка `fit`: где кадр лежит на выходе (x, y, ширина, высота) и чем
    # залито поле. Сборка ставит подпись над этим прямоугольником, а субтитры —
    # под ним (`fit_frame`). None — раскладки fit в сцене нет. У `circle` —
    # квадрат, в который вписан кружок: подпись и субтитры встают так же
    frame: tuple[int, int, int, int] | None = None
    fill: str | None = None
    # `circle`: id фона из resources/backgrounds/ и с какой его секунды взят
    # кусок (своя у каждого эпизода). None — фона нет, поле `fill`
    background: str | None = None
    background_start: float | None = None


class Reframe(WindowPlan):
    """Паспорт кадрирования: из чего сделано и куда встали окна."""

    source: str
    width: int
    height: int
    # окно, по которому кадрировано, если его задал человек или проект (`fit`):
    # сменилось — клип устарел. None — окно ставил детектор или паспорт старше
    # поля; у таких свежесть, как раньше, только по файлам
    window: ReframeWindow | None = None
    duration_sec: float = 0.0  # конец последнего плана; 0 — паспорт до 14.09.2026
    # в какой частоте отрендерено окно (`Storyboard.fps` или 24). Сменилась
    # частота эпизода — клип устарел, иначе сборка пересчитывала бы кадры
    # дважды. None — паспорт до замера частоты: принят как есть
    fps: float | None = None


def _sidecar(path: Path) -> Path:
    return path.with_suffix(".json")


def _load(path: Path) -> Reframe | None:
    side = _sidecar(path)
    if not side.exists():
        return None
    try:
        return read_model(side, Reframe)
    except (ValidationError, ValueError):
        return None


def shot_bounds(clip: Path, duration: float, *, threshold: float = SHOT_THRESHOLD) -> list[float]:
    """Границы планов внутри клипа: [0, склейки…, конец]. Слишком короткие
    вспышки склеиваются с предыдущим планом."""
    try:
        cuts = detect_cuts(clip, threshold=threshold)
    except FfmpegError:
        cuts = []
    bounds = [0.0]
    for t in cuts:
        if t - bounds[-1] >= MIN_SHOT_SEC and duration - t >= MIN_SHOT_SEC:
            bounds.append(t)
    bounds.append(duration)
    return bounds


def window_for(faces: list[Face], *, width: int, crop_w: int) -> tuple[int, int]:
    """Левый край окна и число лиц, по которым он поставлен.

    Лица сортированы по площади: большее лицо ближе к камере, а значит скорее
    всего и есть говорящий. Окно накрывает столько лиц, сколько влезает, начиная
    с самого крупного; не влезло — остаётся крупное, и это повод предупредить
    человека, а не молча обрезать половину общего плана."""
    if not faces:
        return _clamp((width - crop_w) // 2, width, crop_w), 0
    lo = hi = faces[0].center_x
    used = 1
    for face in faces[1:]:
        new_lo, new_hi = min(lo, face.center_x), max(hi, face.center_x)
        if new_hi - new_lo <= crop_w * FIT_SHARE:
            lo, hi, used = new_lo, new_hi, used + 1
    center = (lo + hi) / 2
    return _clamp(round(center - crop_w / 2), width, crop_w), used


def _clamp(x: int, width: int, crop_w: int) -> int:
    return max(0, min(width - crop_w, x)) // 2 * 2  # чётный: yuv420p не режется по нечётной границе


def choose_layout(
    faces: list[Face],
    frame_w: int,
    frame_h: int,
    *,
    crop_w: int | None = None,
    facecam: bool = True,
) -> ReframeLayout:
    """Раскладка вертикали по лицам одного кадра — чистое правило, без рендера.

    Сначала вебка: маленькое уверенное лицо в углу (`webcam_face`) — это стрим,
    и что бы ни нашлось посреди кадра, там игра. Потом `two_up` — два самых
    крупных лица, оба не ниже `TWO_UP_MIN_FACE_SHARE` кадра, сопоставимой
    величины (`TWO_UP_SIZE_RATIO`), стоят дальше друг от друга, чем одно окно
    9:16 накрывает (`FIT_SHARE` от `crop_w`; по умолчанию — окно 9:16 во всю
    высоту кадра). Иначе `single` — как было. Порядок важен: на стриме вебка
    плюс герой игры проходили бы как «двое» (t06, 37 с: 83×105 с score 0.65
    рядом с вебкой 48×60), а два портрета в панели — как двое по краям.

    Лица идут крупными первыми (так их отдаёт `core.faces`); для two_up лица
    мельче второго не смотрим: третий человек на заднем плане раскладку не
    меняет. `facecam=False` — источник не стрим: вебку не ищем вовсе (бюсты
    на полках студии проходили за неё)."""
    if crop_w is None:
        crop_w = _crop_size(frame_w, frame_h, aspect_size("9:16"))[0]
    if not faces:
        return "single"
    if facecam and webcam_face(faces, frame_w, frame_h) is not None:
        return "facecam_screen"
    if len(faces) >= 2:
        big, second = faces[0], faces[1]
        people = min(big.h, second.h) >= frame_h * TWO_UP_MIN_FACE_SHARE
        comparable = second.w >= big.w * TWO_UP_SIZE_RATIO
        apart = abs(big.center_x - second.center_x) > crop_w * FIT_SHARE
        if people and comparable and apart:
            return "two_up"
    return "single"


def webcam_face(
    faces: list[Face], frame_w: int, frame_h: int, *, strict: bool = True
) -> Face | None:
    """Лицо стримера в вебке: уверенное, не крупнее FACECAM_MAX_SHARE высоты, у
    верхнего или нижнего края кадра (пороги — см. константы FACECAM_*). Несколько
    подходящих — самое уверенное; ничего — None. `strict=False` — по приказу
    человека (`layout: facecam_screen`): края достаточно, величина и score не
    сверяются — вебка бывает и в четверть кадра."""
    found = [
        f
        for f in faces
        if (not strict or (f.h < frame_h * FACECAM_MAX_SHARE and f.score >= FACECAM_SCORE_MIN))
        and (
            f.center_y < frame_h * FACECAM_TOP_SHARE
            or f.center_y > frame_h * (1 - FACECAM_BOTTOM_SHARE)
        )
    ]
    return max(found, key=lambda f: f.score) if found else None


def _layout_for(
    faces: list[Face],
    forced: ReframeLayout | None,
    *,
    width: int,
    height: int,
    crop_w: int,
    stack_ok: bool,
    facecam: bool = True,
) -> ReframeLayout:
    """Раскладка одной пробы: по правилу или по приказу человека. Приказ
    исполняется там, где лица позволяют (two_up — два лица, facecam — одно),
    остальные пробы — одним окном; стек только для вертикального выхода."""
    if not stack_ok:
        return "single"
    if forced is None:
        return choose_layout(faces, width, height, crop_w=crop_w, facecam=facecam)
    if forced == "two_up" and len(faces) >= 2:
        return "two_up"
    if forced == "facecam_screen" and webcam_face(faces, width, height, strict=False):
        return "facecam_screen"
    return "single"


def _pair(faces: list[Face]) -> tuple[Face, Face]:
    """Два крупнейших лица слева направо: левый — верхняя половина. Порядок по
    горизонтали, а не по величине, чтобы собеседники не менялись половинами
    между планами."""
    left, right = sorted(faces[:2], key=lambda f: f.center_x)
    return left, right


def _half_size(h2: float, *, ratio: float, width: int, height: int) -> tuple[int, int]:
    """Окно половины стека высотой `h2` (или меньше, если не влезает) с пропорцией
    половины выхода; стороны чётные."""
    h2 = min(h2, height, width / ratio)
    return max(2, int(h2 * ratio) // 2 * 2), max(2, int(h2) // 2 * 2)


def _corner(
    cx: float, cy: float, size: tuple[int, int], *, width: int, height: int
) -> tuple[int, int]:
    """Верхний левый угол окна `size` с центром в (cx, cy), не вылезая за кадр."""
    w2, h2 = size
    return _clamp(round(cx - w2 / 2), width, w2), _clamp(round(cy - h2 / 2), height, h2)


def _stack_sizes(
    frames: list[list[Face]],
    layouts: list[ReframeLayout],
    kind: ReframeLayout,
    *,
    width: int,
    height: int,
    ratio: float,
    punch: float,
    target: tuple[int, int],
    strict: bool = True,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Размеры окон половин на всю сцену — по медиане высоты лиц в пробах этой
    раскладки. Один размер на сцену: `crop` считает ширину и высоту при
    настройке графа, двигать по времени можно только положение."""
    if kind == "two_up":
        heights = [
            max(f.h for f in _pair(fs))
            for fs, lay in zip(frames, layouts, strict=True)
            if lay == kind
        ]
        h2 = median(heights) / TWO_UP_FACE_SHARE
        h2 = min(height, max(height * STACK_MIN_CROP_SHARE, h2)) / punch
        size = _half_size(h2, ratio=ratio, width=width, height=height)
        return size, size
    cams = [
        cam
        for fs, lay in zip(frames, layouts, strict=True)
        if lay == kind and (cam := webcam_face(fs, width, height, strict=strict)) is not None
    ]
    cam_h = median(c.h for c in cams) if cams else height * FACECAM_MAX_SHARE / 2
    cam_y = median(c.center_y for c in cams) if cams else float(height)
    bottom = _half_size(
        min(height, cam_h / FACECAM_FACE_SHARE) / punch,
        ratio=FACECAM_RATIO,
        width=width,
        height=height,
    )
    top = _half_size(
        game_band(cam_y, bottom[1], height=height)[1] / punch,
        ratio=target[0] / facecam_split(target),
        width=width,
        height=height,
    )
    return top, bottom


def game_band(cam_y: float, cam_h: int, *, height: int) -> tuple[int, int]:
    """Полоса кадра для окна игры: (верх, высота) — по ту сторону от окна вебки
    высотой `cam_h` с центром на `cam_y`, с зазором `GAME_GAP_SHARE`. Вебка в
    нижней половине — игра над ней, в верхней — под ней."""
    gap = height * GAME_GAP_SHARE
    if cam_y >= height / 2:
        return 0, max(2, int(cam_y - cam_h / 2 - gap))
    top = int(cam_y + cam_h / 2 + gap)
    return top, max(2, height - top)


def _game_corner(
    cam_y: float,
    game: tuple[int, int],
    cam: tuple[int, int],
    *,
    width: int,
    height: int,
    x2: float | None,
    y2: float | None,
) -> tuple[int, int]:
    """Угол окна игры: по горизонтали центр кадра, по вертикали середина полосы
    над (под) вебкой; `x2`/`y2` от человека побеждают — в долях кадра."""
    band_top, band_h = game_band(cam_y, cam[1], height=height)
    cx = width / 2 if x2 is None else x2 * width
    cy = band_top + band_h / 2 if y2 is None else y2 * height
    return _corner(cx, cy, game, width=width, height=height)


def stacked_spans(windows: list[Window], duration: float) -> list[tuple[float, float]]:
    """Секунды сцены (от, до), где кадр собран стеком; соседние планы стека
    слиты в один отрезок."""
    out: list[tuple[float, float]] = []
    for window, nxt in zip(windows, [*windows[1:], None], strict=True):
        if window.layout in ("single", "fit", "circle"):  # один кадр, стыка половин нет
            continue
        end = nxt.start_sec if nxt is not None else duration
        if out and out[-1][1] >= window.start_sec:
            out[-1] = (out[-1][0], end)
        else:
            out.append((window.start_sec, end))
    return out


def clip_stacked_spans(video: Path) -> list[tuple[float, float]]:
    """То же по паспорту рядом с кадрированным клипом — для QA, которому нужно
    знать, где в сцене стык половин. Без паспорта или до стека — пусто."""
    made = _load(video)
    return stacked_spans(made.windows, made.duration_sec) if made else []


def clip_seam(video: Path) -> float:
    """Где у кадрированного клипа стык верха и низа стека, доля высоты выхода."""
    made = _load(video)
    return made.stack_seam if made is not None and made.stack_seam else SEAM_SHARE


def facecam_seam(video: Path) -> float | None:
    """Стык «игра / вебка», если клип ЦЕЛИКОМ собран раскладкой facecam_screen;
    иначе None. Сборка ставит по нему субтитры над вебкой: при стеке только в
    части планов реплика там легла бы на одиночное окно посреди лица."""
    made = _load(video)
    if made is None or not made.windows:
        return None
    if any(w.layout != "facecam_screen" for w in made.windows):
        return None
    return made.stack_seam or SEAM_SHARE


def sample_times(shots: list[tuple[float, float]]) -> list[list[float]]:
    """Секунды проб внутри каждого плана: не реже `SAMPLE_SEC`, не меньше
    `FRAMES_PER_SHOT` на план и не больше `MAX_FRAMES` на всю сцену."""
    counts = [
        max(FRAMES_PER_SHOT, int((b - a) / SAMPLE_SEC) or 1) if b > a else 1 for a, b in shots
    ]
    total = sum(counts)
    if total > MAX_FRAMES:  # ужимаем пропорционально, но по пробе на план оставляем
        counts = [max(1, round(n * MAX_FRAMES / total)) for n in counts]
    return [
        [a + (b - a) * (k + 0.5) / n for k in range(n)]
        for (a, b), n in zip(shots, counts, strict=True)
    ]


def jump_splits(xs: list[int], *, width: int) -> list[int]:
    """Индексы проб, с которых начинается новый план: центр окна прыгнул больше
    чем на `FACE_JUMP_SHARE` ширины кадра. Между двумя камерами одной студии
    картинка почти не меняется, а лицо переезжает через полкадра — по этому
    прыжку склейка и находится."""
    limit = width * FACE_JUMP_SHARE
    return [i for i in range(1, len(xs)) if abs(xs[i] - xs[i - 1]) > limit]


def _refine_cut(
    detect: DetectAt,
    clip: Path,
    *,
    before: float,
    after: float,
    x_before: int,
    width: int,
    crop_w: int,
) -> float:
    """Секунда склейки между двумя пробами — делением пополам. Без уточнения
    окно едет по старому месту до следующей пробы, и это ровно те полсекунды
    разрезанного лица, ради которых всё затевалось."""
    limit = width * FACE_JUMP_SHARE
    lo, hi = before, after
    for _ in range(REFINE_STEPS):
        mid = (lo + hi) / 2
        faces = detect(clip, [mid])[0]
        x, _used = window_for(faces, width=width, crop_w=crop_w)
        if abs(x - x_before) > limit:
            hi = mid  # на середине уже новый план
        else:
            lo = mid
    return round(hi, 2)


def _shrunk(crop: tuple[int, int], punch: float) -> tuple[int, int]:
    """Одиночное окно после наезда: в `punch` раз меньше, стороны чётные."""
    return max(2, int(crop[0] / punch) // 2 * 2), max(2, int(crop[1] / punch) // 2 * 2)


def _manual_plan(
    manual: ReframeWindow,
    *,
    width: int,
    height: int,
    crop: tuple[int, int],
    ratio: float,
    stack_ok: bool,
    target: tuple[int, int],
) -> WindowPlan:
    """Окна от человека: статичны на всю сцену, детектор не зовётся."""
    assert manual.x is not None
    cw, ch = crop
    punch = manual.punch or 1.0
    y_share = 0.5 if manual.y is None else manual.y
    x = _clamp(round(manual.x * width - cw / 2), width, cw)
    y = None if ch >= height else _clamp(round(y_share * height - ch / 2), height, ch)
    layout: ReframeLayout = manual.layout or "single"
    warnings: list[str] = []
    if layout != "single" and not stack_ok:
        warnings.append(f"раскладка {layout} только для вертикального выхода — одно окно")
        layout = "single"
    if layout == "single":
        window = Window(start_sec=0.0, x=x, faces=0)
        return WindowPlan(
            windows=[window], warnings=warnings, by="manual", crop_w=cw, crop_h=ch, y=y
        )
    # окно вокруг (x, y) — в punch раз меньше полной высоты: у facecam это
    # величина вебки (punch 4 — четверть кадра). two_up: верх — (x, y), низ —
    # (x2, y2) того же размера. facecam: НИЗ — вебка вокруг (x, y), ВЕРХ — игра:
    # самое большое окно над (под) вебкой, по центру кадра или вокруг (x2, y2)
    own = _half_size(height / punch, ratio=ratio, width=width, height=height)
    cx, cy = manual.x * width, y_share * height
    seam: float | None = None
    if layout == "two_up":
        if manual.x2 is None:
            warnings.append("x2 не задан — нижняя половина по центру кадра")
        x2 = 0.5 if manual.x2 is None else manual.x2
        y2 = 0.5 if manual.y2 is None else manual.y2
        top_size = bottom_size = own
        top = _corner(cx, cy, own, width=width, height=height)
        bottom = _corner(x2 * width, y2 * height, own, width=width, height=height)
    else:
        split = facecam_split(target)
        seam = split / target[1]
        own = bottom_size = _half_size(
            height / punch, ratio=FACECAM_RATIO, width=width, height=height
        )
        top_size = _half_size(
            float(game_band(cy, own[1], height=height)[1]),
            ratio=target[0] / split,
            width=width,
            height=height,
        )
        bottom = _corner(cx, cy, own, width=width, height=height)
        top = _game_corner(
            cy, top_size, own, width=width, height=height, x2=manual.x2, y2=manual.y2
        )
    window = Window(start_sec=0.0, x=x, faces=0, layout=layout, top=top, bottom=bottom)
    return WindowPlan(
        windows=[window],
        warnings=warnings,
        by="manual",
        crop_w=cw,
        crop_h=ch,
        y=y,
        stack_top=top_size,
        stack_bottom=bottom_size,
        stack_seam=seam,
    )


def plan_windows(
    clip: Path,
    *,
    duration: float,
    width: int,
    height: int,
    crop: tuple[int, int],
    target: tuple[int, int],
    manual: ReframeWindow | None,
    detect: DetectAt = detect_at,
    facecam: bool = True,
) -> WindowPlan:
    """Окна по планам клипа, предупреждения и то, чем они поставлены.

    Планы берутся из детектора смен картинки И из прыжков лица между пробами:
    первый ловит монтажные склейки, второй — те, где камеры сняты одинаково.
    Раскладка каждого плана — голосование его проб (`_layout_for`): стек
    получает план, где за него больше половины проб, спорный план остаётся
    одним окном. Раскладка стека на сцену одна (чаще встреченная): у половин
    один размер на весь граф, и two_up с facecam в одной сцене не уживаются.

    `manual` — окно от человека: с `x` — всё статично, без `x` — приказ о
    раскладке и наезд, окна ставит детектор. `facecam` — узнавать вебку
    правилом (только у записи стрима; приказ человека действует всегда)."""
    ratio = target[0] / max(1, target[1] // 2)  # пропорция половины стека
    stack_ok = target[1] > target[0]  # стек — ответ вертикали
    punch = manual.punch if manual is not None and manual.punch else 1.0
    crop = _shrunk(crop, punch)
    if manual is not None and manual.x is not None:
        return _manual_plan(
            manual,
            width=width,
            height=height,
            crop=crop,
            ratio=ratio,
            stack_ok=stack_ok,
            target=target,
        )
    crop_w, crop_h = crop
    forced = manual.layout if manual is not None else None

    bounds = shot_bounds(clip, duration)
    shots = list(zip(bounds[:-1], bounds[1:], strict=True))
    times = sample_times(shots)
    frames = detect(clip, [t for shot in times for t in shot])
    layouts = [
        _layout_for(
            fs,
            forced,
            width=width,
            height=height,
            crop_w=crop_w,
            stack_ok=stack_ok,
            facecam=facecam,
        )
        for fs in frames
    ]
    stacked_votes = Counter(lay for lay in layouts if lay != "single")
    kind: ReframeLayout | None = stacked_votes.most_common(1)[0][0] if stacked_votes else None
    stack_top = stack_bottom = None
    if kind is not None:
        stack_top, stack_bottom = _stack_sizes(
            frames,
            layouts,
            kind,
            width=width,
            height=height,
            ratio=ratio,
            punch=punch,
            target=target,
            strict=forced is None,
        )
    # вертикаль одиночного окна: центр лиц по сцене, если окно уже не во всю
    # высоту (наезд); иначе ставить некуда
    centers_y = [fs[0].center_y for fs in frames if fs]
    y = (
        None
        if crop_h >= height
        else _clamp(
            round((median(centers_y) if centers_y else height / 2) - crop_h / 2), height, crop_h
        )
    )

    windows: list[Window] = []
    warnings: list[str] = []
    found_any = False
    hidden_cuts = 0
    at = 0
    for (a, b), shot_times in zip(shots, times, strict=True):
        shot_frames = frames[at : at + len(shot_times)]
        shot_layouts = layouts[at : at + len(shot_times)]
        at += len(shot_times)
        xs: list[int] = []
        counts: list[int] = []
        crowded: list[bool] = []
        for faces in shot_frames:
            x, used = window_for(faces, width=width, crop_w=crop_w)
            xs.append(x)
            counts.append(len(faces))
            crowded.append(len(faces) > used)
        # план делится там, где лицо прыгнуло: это склейка, которую не увидел
        # детектор картинки. Секунду склейки уточняют отдельные пробы
        splits = jump_splits(xs, width=width)
        starts = [a]
        for i in splits:
            starts.append(
                _refine_cut(
                    detect,
                    clip,
                    before=shot_times[i - 1],
                    after=shot_times[i],
                    x_before=xs[i - 1],
                    width=width,
                    crop_w=crop_w,
                )
            )
            hidden_cuts += 1
        pieces = [0, *splits, len(xs)]
        for start, end, lo, hi in zip(
            starts, [*starts[1:], b], pieces[:-1], pieces[1:], strict=True
        ):
            part = xs[lo:hi]
            best = (
                round(median(part)) // 2 * 2
                if part
                else _clamp((width - crop_w) // 2, width, crop_w)
            )
            seen = max(counts[lo:hi], default=0)
            found_any = found_any or seen > 0
            votes = sum(1 for lay in shot_layouts[lo:hi] if lay == kind)
            layout: ReframeLayout = kind if kind and votes * 2 > hi - lo else "single"
            top = bottom = None
            if layout != "single":
                assert stack_top and stack_bottom
                voted = [
                    fs
                    for fs, lay in zip(shot_frames[lo:hi], shot_layouts[lo:hi], strict=True)
                    if lay == kind
                ]
                if layout == "two_up":
                    pairs = [_pair(fs) for fs in voted]
                    top = _corner(
                        median(p[0].center_x for p in pairs),
                        median(p[0].center_y for p in pairs),
                        stack_top,
                        width=width,
                        height=height,
                    )
                    bottom = _corner(
                        median(p[1].center_x for p in pairs),
                        median(p[1].center_y for p in pairs),
                        stack_bottom,
                        width=width,
                        height=height,
                    )
                    warnings.append(f"{start:.1f}–{end:.1f} с: двое друг над другом")
                else:
                    cams = [
                        c
                        for fs in voted
                        if (c := webcam_face(fs, width, height, strict=forced is None))
                    ]
                    cam_x = median(c.center_x for c in cams)
                    cam_y = median(c.center_y for c in cams)
                    # низ — вебка вокруг лица, верх — игра над (под) ней
                    bottom = _corner(cam_x, cam_y, stack_bottom, width=width, height=height)
                    top = _game_corner(
                        cam_y,
                        stack_top,
                        stack_bottom,
                        width=width,
                        height=height,
                        x2=manual.x2 if manual else None,
                        y2=manual.y2 if manual else None,
                    )
                    warnings.append(f"{start:.1f}–{end:.1f} с: игра сверху, вебка снизу")
            elif seen == 0:
                warnings.append(f"{start:.1f}–{end:.1f} с: лиц не видно — окно по центру")
            elif any(crowded[lo:hi]):
                warnings.append(
                    f"{start:.1f}–{end:.1f} с: в кадре {seen} лица, в вертикаль влезает не всё — "
                    f"кадрировано по крупному"
                )
            if windows and abs(windows[-1].x - best) <= width * SNAP_SHARE:
                best = windows[-1].x
            windows.append(
                Window(start_sec=start, x=best, faces=seen, layout=layout, top=top, bottom=bottom)
            )
    if hidden_cuts:
        warnings.append(
            f"склеек, которые детектор картинки не увидел, найдено по лицам: {hidden_cuts}"
        )
    used_stack = any(w.layout != "single" for w in windows)
    return WindowPlan(
        windows=windows,
        warnings=warnings,
        by="yunet" if found_any else "center",
        crop_w=crop_w,
        crop_h=crop_h,
        y=y,
        stack_top=stack_top if used_stack else None,
        stack_bottom=stack_bottom if used_stack else None,
        stack_seam=(
            facecam_split(target) / target[1] if used_stack and kind == "facecam_screen" else None
        ),
    )


class ReframeStage(Stage):
    name = "reframe"
    needs = ()  # ни одного провайдера: OpenCV и ffmpeg на месте

    def estimate_cost(self, ctx: StageContext) -> float:
        return 0.0

    @staticmethod
    def _fresh(ctx: StageContext, scene: Scene, out: Path) -> bool:
        made = _load(out)
        if made is None or not out.exists():
            return False
        if scene.assets.video != ctx.rel(out):
            return False
        if made.fps is not None and abs(made.fps - (ctx.storyboard.fps or FPS)) > 1e-3:
            return False  # частоту эпизода сменили — окно отрендерено не в неё
        window = window_for_scene(ctx, scene)
        fixed = window is not None and window.layout in FIXED_LAYOUTS
        if (made.window is not None or fixed) and made.window != window:
            return False  # раскладку канала или сцены сменили — кадр не тот
        source = ctx.abs(made.source)
        if not (source.exists() and out.stat().st_mtime >= source.stat().st_mtime):
            return False
        if made.background is not None:
            try:  # файл фона заменили — кусок под ролик уже другой
                bg, bg_sec = circle_background(ctx, made.background)
            except (FileNotFoundError, ValueError):
                return True  # фона нет на диске — готовый клип не хуже пустоты
            ref = circle_ref(ctx, scene)
            if background_start(ref, bg_sec, scene.duration_sec) != made.background_start:
                return False
            return out.stat().st_mtime >= bg.stat().st_mtime
        return True

    def run(self, ctx: StageContext) -> StageResult:
        scenes = ctx.storyboard.ordered_scenes()
        missing = [
            s.id for s in scenes if not (s.assets.video and ctx.abs(s.assets.video).exists())
        ]
        if missing:
            return StageResult(
                status="failed", message=f"кадрирование идёт после нарезки: нет клипов {missing}"
            )

        out_dir = ctx.stage_dir(OUT_DIR)
        target = aspect_size(ctx.storyboard.aspect_ratio)
        fps = ctx.storyboard.fps or FPS  # частота исходника, которую поставил cut
        done: list[str] = []
        notes: list[str] = []
        changed = False
        for scene in scenes:
            assert scene.assets.video
            out = out_dir / f"{scene.id}{SUFFIX}.mp4"
            if not ctx.options.force and self._fresh(ctx, scene, out):
                continue
            source = ctx.abs(scene.assets.video)
            made = _load(out)
            if (
                made is not None
                and scene.assets.video == ctx.rel(out)
                and ctx.abs(made.source).exists()
            ):
                # клип сцены — уже наше окно (перерисовка: сменили частоту
                # эпизода, --force): исходник — тот, что записан в паспорте,
                # иначе окно 9:16 считалось бы «уже вертикальным» и осталось
                # бы как есть
                source = ctx.abs(made.source)
            size = probe_size(source)
            if size is None:
                return StageResult(
                    status="failed", message=f"сцена {scene.id}: не читается {source}"
                )
            width, height = size
            window = window_for_scene(ctx, scene)
            fit = window is not None and window.layout == "fit"
            circle = window is not None and window.layout == "circle"
            crop_w, crop_h = _crop_size(width, height, target)
            if not (fit or circle) and crop_w >= width and crop_h >= height:
                scene.checks.notes.pop("reframe", None)
                notes.append(f"сцена {scene.id}: клип уже {ctx.storyboard.aspect_ratio}")
                continue
            try:
                if circle:
                    assert window is not None
                    with ctx.step("кружок", scene.id) as step:
                        plan, background = circle_plan(
                            ctx, scene, window, source, width=width, height=height, target=target
                        )
                        summary = _summary(plan)
                        step.note += f": {summary}"
                        circle_video(
                            source,
                            out,
                            crop=(plan.crop_w, plan.windows[0].x, plan.y or 0),
                            size=target,
                            duration=scene.duration_sec,
                            fps=fps,
                            background=background,
                            background_start=plan.background_start or 0.0,
                            fill=plan.fill or "black",
                        )
                elif fit:
                    assert window is not None
                    plan = fit_plan(window, width=width, height=height, target=target)
                    summary = _summary(plan)
                else:
                    with ctx.step("поиск лиц", scene.id) as detect:
                        plan = plan_windows(
                            source,
                            duration=scene.duration_sec,
                            width=width,
                            height=height,
                            crop=(crop_w, crop_h),
                            target=target,
                            manual=window,
                            facecam=_is_stream(ctx, scene),
                        )
                        summary = _summary(plan)
                        detect.note += f": {summary}"
                if not circle:
                    with ctx.step("рендер кадрирования", scene.id):
                        _render(
                            source, out, plan, duration=scene.duration_sec, size=target, fps=fps
                        )
            except (FaceError, FfmpegError, FileNotFoundError) as exc:
                return StageResult(
                    status="failed", message=f"сцена {scene.id}: {exc}", changed=changed
                )
            write_model(
                _sidecar(out),
                Reframe(
                    source=ctx.rel(source),
                    width=width,
                    height=height,
                    duration_sec=scene.duration_sec,
                    fps=fps,
                    window=window if fit or circle else None,
                    **plan.model_dump(),
                ),
            )
            scene.assets.video = ctx.rel(out)
            scene.provenance["reframe"] = plan.by
            for text in plan.warnings:
                ctx.log.warning("сцена %s: %s", scene.id, text)
            if plan.warnings:
                scene.checks.notes["reframe"] = "; ".join(plan.warnings)
            else:
                scene.checks.notes.pop("reframe", None)
            notes.append(f"сцена {scene.id}: {summary}")
            done.append(scene.id)
            changed = True
            ctx.save()

        if not done:
            return StageResult(
                status="done", message="; ".join(notes) or "все клипы уже кадрированы"
            )
        return StageResult(
            status="done",
            message=f"кадрировано сцен: {len(done)} · " + "; ".join(notes),
            changed=changed,
        )


def window_for_scene(ctx: StageContext, scene: Scene) -> ReframeWindow | None:
    """Окно сцены: своё (`Scene.reframe`) побеждает, иначе окно канала
    (`video.reframe` проекта), иначе None — окна ставит детектор."""
    if scene.reframe is not None:
        return scene.reframe
    return ctx.project.video.reframe if ctx.project else None


def fit_plan(
    window: ReframeWindow, *, width: int, height: int, target: tuple[int, int]
) -> WindowPlan:
    """Раскладка `fit`: кусок исходника (весь кадр при `punch` 1.0) по ширине
    выхода на поле. Детектор не нужен — окно статично на всю сцену."""
    rect = fit_rect(
        (width, height),
        target,
        punch=window.punch or 1.0,
        x_share=window.x,
        zoom=window.zoom or 1.0,
        y_share=window.y,
    )
    return WindowPlan(
        windows=[Window(start_sec=0.0, x=rect.crop_x, layout="fit")],
        by="fit",
        crop_w=rect.crop_w,
        crop_h=rect.crop_h,
        y=rect.crop_y,
        frame=(rect.x, rect.y, rect.width, rect.height),
        fill=window.fill or "black",
    )


def circle_ref(ctx: StageContext, scene: Scene) -> str:
    """Ключ, по которому сцене достаётся свой кусок фона."""
    sb = ctx.storyboard
    return f"{sb.project_id}/{sb.episode_id}/{scene.id}"


def circle_background(ctx: StageContext, background_id: str) -> tuple[Path, float]:
    """Файл фона и его длина. Ресурса или файла нет — FileNotFoundError с тем,
    откуда его взять заново: файл под .gitignore и уборку не переживает."""
    res = load_resource("backgrounds", background_id, Background, ctx.resource_dirs)
    path = res.path()
    if not path.exists():
        raise FileNotFoundError(f"нет файла фона {path} — скачать заново: {res.source or '?'}")
    return path, probe_duration(path)


def circle_plan(
    ctx: StageContext,
    scene: Scene,
    window: ReframeWindow,
    source: Path,
    *,
    width: int,
    height: int,
    target: tuple[int, int],
) -> tuple[WindowPlan, Path | None]:
    """Раскладка `circle`: квадрат исходника (по `x`/`y` окна, иначе по лицам,
    иначе чуть выше середины) и кусок фона. Окно одно на всю сцену: кружок,
    который переезжает между планами, читается как дрожь."""
    x_share, y_share, by = window.x, window.y, "manual"
    faces = 0
    if x_share is None and y_share is None:
        by = "default"
        span = scene.duration_sec
        times = [span * (i + 0.5) / CIRCLE_PROBES for i in range(CIRCLE_PROBES)]
        try:
            found = detect_at(source, times)
        except FaceError as exc:  # без OpenCV кружок всё равно ставится — по умолчанию
            ctx.log.warning("сцена %s: лица не искались (%s) — квадрат по умолчанию", scene.id, exc)
            found = []
        faces = sum(1 for fs in found if fs)
        center = face_center(
            [[(f.center_x, f.center_y, f.w * f.h) for f in fs] for fs in found],
            width=width,
            height=height,
        )
        if center is not None:
            x_share, y_share, by = *center, "yunet"
    side, x, y = square_crop(
        width, height, x_share=x_share, y_share=y_share, zoom=window.zoom or 1.0
    )
    circle = circle_on(target)
    background: Path | None = None
    start: float | None = None
    if window.background:
        background, bg_sec = circle_background(ctx, window.background)
        start = background_start(circle_ref(ctx, scene), bg_sec, scene.duration_sec)
    plan = WindowPlan(
        windows=[Window(start_sec=0.0, x=x, faces=faces, layout="circle")],
        by=f"circle/{by}",
        crop_w=side,
        crop_h=side,
        y=y,
        frame=(circle.x, circle.y, circle.d, circle.d),
        fill=None if background is not None else (window.fill or "black"),
        background=window.background if background is not None else None,
        background_start=start,
    )
    return plan, background


def fit_frame(video: Path) -> tuple[int, int, int, int] | None:
    """Где лежит кадр `fit` на выходе — по паспорту рядом с кадрированным
    клипом: (x, y, ширина, высота). None — клип кадрирован не раскладкой fit."""
    made = _load(video)
    return made.frame if made is not None else None


def _is_stream(ctx: StageContext, scene: Scene) -> bool:
    """The scene's source is a live-stream recording: only there a corner face is a facecam."""
    if scene.source is None:
        return False
    from videoforge.clips.source import load_source
    from videoforge.stages.cut import CutStage

    info = load_source(CutStage._source_dir(ctx, scene))
    return info is not None and info.stream


def _crop_size(width: int, height: int, target: tuple[int, int]) -> tuple[int, int]:
    """Самое большое окно нужной пропорции внутри исходного кадра."""
    tw, th = target
    if width * th > height * tw:  # исходник шире цели — режем по бокам
        return (round(height * tw / th) // 2 * 2, height // 2 * 2)
    return (width // 2 * 2, round(width * th / tw) // 2 * 2)


def _summary(plan: WindowPlan) -> str:
    """Строка для сообщения стадии: планов, чем поставлено, сколько из них стеком."""
    if plan.by.startswith("circle/"):
        assert plan.frame is not None
        under = (
            f"поверх {plan.background} с {plan.background_start or 0:.1f} с"
            if plan.background
            else f"на поле {plan.fill or 'black'}"
        )
        return (
            f"кружок {plan.frame[2]} px из квадрата {plan.crop_w} (x={plan.windows[0].x}, "
            f"y={plan.y}) по {plan.by.split('/', 1)[1]} {under}"
        )
    if plan.frame is not None:
        _, y, w, h = plan.frame
        return f"кадр целиком {w}×{h} на поле {plan.fill or 'black'} с y={y}"
    stacked = Counter(w.layout for w in plan.windows if w.layout != "single")
    text = f"планов {len(plan.windows)}, окно по {plan.by}"
    for layout, n in stacked.most_common():
        text += f", {layout} в {n} из них"
    return text


def _render(
    source: Path,
    out: Path,
    plan: WindowPlan,
    *,
    duration: float,
    size: tuple[int, int],
    fps: float = FPS,
) -> None:
    """Сцена без стека — одно окно, как было; со стеком — `stack_crops`, и
    одиночная ветка уходит в граф только там, где стек не на всю сцену;
    раскладка `fit` — кадр на поле (`fit_video`)."""
    if plan.frame is not None:
        x, y, w, h = plan.frame
        rect = FitRect(plan.windows[0].x, plan.crop_w, plan.crop_h, x, y, w, h, plan.y or 0)
        fit_video(source, out, rect=rect, size=size, fill=plan.fill or "black", fps=fps)
        return
    spans = stacked_spans(plan.windows, duration)
    if not spans:
        reframe_video(
            source,
            out,
            crop=(plan.crop_w, plan.crop_h),
            offsets=[(w.start_sec, w.x) for w in plan.windows],
            size=size,
            fps=fps,
            y=plan.y,
        )
        return
    assert plan.stack_top is not None and plan.stack_bottom is not None
    top = CropTrack(*plan.stack_top, [(w.start_sec, *w.top) for w in plan.windows if w.top])
    bottom = CropTrack(
        *plan.stack_bottom, [(w.start_sec, *w.bottom) for w in plan.windows if w.bottom]
    )
    single = None
    if any(w.layout == "single" for w in plan.windows):
        y = plan.y if plan.y is not None else 0
        single = CropTrack(plan.crop_w, plan.crop_h, [(w.start_sec, w.x, y) for w in plan.windows])
    stack_crops(
        source,
        out,
        top=top,
        bottom=bottom,
        size=size,
        fps=fps,
        single=single,
        stacked=spans,
        split=round(plan.stack_seam * size[1]) if plan.stack_seam else None,
    )
