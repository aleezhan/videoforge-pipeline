"""Лица в кадре: единственное место, где проект знает про OpenCV.

Нужно кадрированию нарезок (`stages/reframe.py`): длинное видео снято 16:9, а
шортс вертикальный, и окно кадра надо ставить туда, где человек, а не в
геометрический центр. Детектор — встроенный в OpenCV YuNet
(`cv2.FaceDetectorYN`) с моделью из `videoforge/assets/models/` (MIT, Shiqi Yu):
одна зависимость вместо MediaPipe, без проблем со сборками под свежий Python.

**Кадр уменьшается перед детектом.** Модель обучена на лицах примерно от 10 до
300 пикселей, а на 4K крупный план даёт лицо шириной под 600 — то есть за
пределами того, на чём она училась. Уменьшение большей стороны до
`DETECT_LONG_SIDE` возвращает лица в рабочий диапазон и заодно ускоряет детект;
координаты возвращаются в системе исходного кадра. Именно большей стороны, а
не ширины: до 14.09.2026 уменьшалась ширина, у 3840×2160 это одно и то же, а
вертикальный шортс 1080×1920 (ширина и так меньше 1280) шёл в детектор
целиком — 85 мс на кадр против 51 на уменьшенном, а с открытием ролика заново
на каждую секунду 0.25 с, и выбор обложки платил это восемь раз (замер
14.09.2026 на podcast_clips/src01_h08).

Два входа — секунды видео (`detect_at`, кадрирование нарезок) и готовые
файлы кадров (`detect_images`, выбор обложки: кадры уже вынуты ffmpeg-ом
одним проходом, открывать ролик заново ради каждого незачем).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

MODEL_FILE = "face_detection_yunet_2026may.onnx"
DETECT_LONG_SIDE = 1280  # большая сторона, до которой уменьшается кадр перед детектом
SCORE_MIN = 0.6  # ниже — обычно не лицо, а узор на фоне

# Отличить героя от собеседника детектор НЕ умеет и уметь не должен: YuNet
# отвечает на вопрос «где лицо», а не «чьё». Дешёвый отпечаток лица пробовали
# 11.09.2026 и опровергли замером — подробности в features/thumbnail_hero.


class FaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Face:
    """Лицо в координатах ИСХОДНОГО кадра."""

    x: float
    y: float
    w: float
    h: float
    score: float

    @property
    def center_x(self) -> float:
        return self.x + self.w / 2

    @property
    def center_y(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)


def model_path() -> Path:
    from videoforge.paths import models_dir

    path = models_dir() / MODEL_FILE
    if not path.exists():
        raise FaceError(
            f"модель детектора лиц не найдена: {path} — скачай "
            f"{MODEL_FILE} из opencv_zoo (MIT) в {path.parent}"
        )
    return path


@lru_cache(maxsize=1)
def _cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover — зависимость объявлена в pyproject
        raise FaceError("нет opencv-python-headless — переустанови зависимости (uv sync)") from exc
    return cv2


def fit(width: int, height: int) -> tuple[float, tuple[int, int]]:
    """Масштаб и размер, в котором кадр идёт в детектор: большая сторона не
    длиннее `DETECT_LONG_SIDE`, кадр меньше этого не трогается (масштаб 1.0)."""
    scale = min(1.0, DETECT_LONG_SIDE / max(width, height))
    return scale, (max(1, round(width * scale)), max(1, round(height * scale)))


def to_source(found: Any, scale: float) -> list[Face]:
    """Боксы YuNet с уменьшенного кадра → `Face` в пикселях исходника, крупные
    первыми. Строка детектора: x, y, w, h, пять точек лица, score последним."""
    return sorted(
        (
            Face(
                float(f[0]) / scale,
                float(f[1]) / scale,
                float(f[2]) / scale,
                float(f[3]) / scale,
                float(f[-1]),
            )
            for f in (found if found is not None else [])
        ),
        key=lambda f: -f.area,
    )


class _Detector:
    """YuNet, который сам уменьшает кадр и отдаёт лица в пикселях исходника.

    Один на пачку кадров: сеть создаётся раз, а размер входа переставляется,
    когда пришёл кадр другого размера — JPEG обложки и кадры видео разного
    размера идут через один и тот же объект."""

    def __init__(self, score_min: float) -> None:
        self._cv2 = _cv2()
        self._net = self._cv2.FaceDetectorYN.create(
            str(model_path()), "", (1, 1), score_threshold=score_min
        )
        self._size: tuple[int, int] | None = None

    def faces(self, frame: Any) -> list[Face]:
        height, width = frame.shape[:2]
        scale, small = fit(width, height)
        if scale < 1.0:
            frame = self._cv2.resize(frame, small, interpolation=self._cv2.INTER_AREA)
        if small != self._size:
            self._net.setInputSize(small)
            self._size = small
        _, found = self._net.detect(frame)
        return to_source(found, scale)


def detect_at(
    video: Path, seconds: list[float], *, score_min: float = SCORE_MIN
) -> list[list[Face]]:
    """Лица на кадрах в заданных секундах: по списку на каждую секунду.

    Кадр не прочитался — пустой список на этом месте, а не исключение: клип
    короче, чем думали, — это повод для предупреждения, а не для падения стадии."""
    cv2 = _cv2()
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise FaceError(f"OpenCV не открыл клип: {video}")
    try:
        detector = _Detector(score_min)
        out: list[list[Face]] = []
        for t in seconds:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000)
            ok, frame = cap.read()
            out.append(detector.faces(frame) if ok else [])
        return out
    finally:
        cap.release()


def detect_images(images: list[Path], *, score_min: float = SCORE_MIN) -> list[list[Face]]:
    """Лица на готовых файлах кадров: по списку на каждый файл.

    Для выбора обложки: кадры уже вынуты из ролика одним проходом ffmpeg
    (`frames_at`), и открывать видео заново ради каждого — как `detect_at` по
    секунде за вызов — значит восемь раз искать по файлу и восемь раз грузить
    сеть: 1.3 с на пять кадров шортса против 0.2 с по JPEG (замер 14.09.2026,
    podcast_clips/src01_h08). Файл не прочитался — пустой список на
    этом месте, а не исключение."""
    if not images:
        return []
    cv2 = _cv2()
    detector = _Detector(score_min)
    out: list[list[Face]] = []
    for path in images:
        frame = cv2.imread(str(path))
        out.append([] if frame is None else detector.faces(frame))
    return out
