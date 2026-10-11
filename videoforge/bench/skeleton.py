"""Скелет из захвата движений → управляющее видео для IC-LoRA (сигнал `raw`).

Кот слушается только скелета (cat_punch_mimic, 13.09.2026): глубина и контуры
переносят силуэт человека. Поэтому «3D-объект» для нас — суставы человека в
пространстве, а не модель. Источник — BVH (захват движений, например бокс из
базы CMU); суставы проецируются в кадр под нужным углом, скелет рисуется в
раскладке и цветах OpenPose/DWPose (18 точек), на которых учили адаптер.

Что задаётся числами, а не съёмкой: угол камеры (azimuth: 0 — лицом,
90 — профиль), зеркало, рост и место фигуры в кадре, какой отрезок записи и с
какой скоростью, паузы до и после. Кадров всегда 8n+1, размер кратен 64.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# --- OpenPose COCO-18: порядок точек, звенья и цвета — как рисует DWPose
POSE_POINTS = [
    "nose", "neck", "rsho", "relb", "rwri", "lsho", "lelb", "lwri",
    "rhip", "rkne", "rank", "lhip", "lkne", "lank", "reye", "leye", "rear", "lear",
]  # fmt: skip
LIMBS = [
    (1, 2), (1, 5), (2, 3), (3, 4), (5, 6), (6, 7), (1, 8), (8, 9), (9, 10),
    (1, 11), (11, 12), (12, 13), (1, 0), (0, 14), (14, 16), (0, 15), (15, 17),
]  # fmt: skip
COLORS = [
    (255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0), (85, 255, 0),
    (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255),
    (0, 0, 255), (85, 0, 255), (170, 0, 255), (255, 0, 255), (255, 0, 170), (255, 0, 85),
]  # fmt: skip

# имена суставов BVH → точки OpenPose; две раскладки конвертации CMU (daz / motionbuilder)
JOINT_ALIASES: dict[str, tuple[str, ...]] = {
    "head": ("Head", "head"),
    "neck": ("Neck", "neck", "Neck1"),
    "rsho": ("RightArm", "rShldr"),
    "relb": ("RightForeArm", "rForeArm"),
    "rwri": ("RightHand", "rHand"),
    "lsho": ("LeftArm", "lShldr"),
    "lelb": ("LeftForeArm", "lForeArm"),
    "lwri": ("LeftHand", "lHand"),
    "rhip": ("RightUpLeg", "rThigh"),
    "rkne": ("RightLeg", "rShin"),
    "rank": ("RightFoot", "rFoot"),
    "lhip": ("LeftUpLeg", "lThigh"),
    "lkne": ("LeftLeg", "lShin"),
    "lank": ("LeftFoot", "lFoot"),
}


@dataclass
class BvhJoint:
    name: str
    offset: np.ndarray
    channels: list[str]
    parent: int | None
    children: list[int] = field(default_factory=list)


@dataclass
class Bvh:
    joints: list[BvhJoint]
    frames: np.ndarray  # (кадры, каналы)
    frame_time: float

    @property
    def fps(self) -> float:
        return 1.0 / self.frame_time

    def index(self, name: str) -> int:
        for i, j in enumerate(self.joints):
            if j.name == name:
                return i
        raise KeyError(name)


def parse_bvh(path: Path) -> Bvh:
    """BVH: иерархия (OFFSET/CHANNELS) и MOTION (кадры значений каналов)."""
    tokens = path.read_text(encoding="utf-8", errors="replace").split()
    joints: list[BvhJoint] = []
    stack: list[int] = []
    i = 0
    n = len(tokens)
    while i < n and tokens[i] != "MOTION":
        t = tokens[i]
        if t in ("ROOT", "JOINT"):
            name = tokens[i + 1]
            parent = stack[-1] if stack else None
            joints.append(BvhJoint(name, np.zeros(3), [], parent))
            idx = len(joints) - 1
            if parent is not None:
                joints[parent].children.append(idx)
            stack.append(idx)
            i += 2
        elif t == "End":
            # End Site: смещение конца кости; в отдельный сустав не выделяем
            depth = 0
            i += 2
            while i < n:
                if tokens[i] == "{":
                    depth += 1
                elif tokens[i] == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
        elif t == "OFFSET":
            joints[stack[-1]].offset = np.array([float(x) for x in tokens[i + 1 : i + 4]])
            i += 4
        elif t == "CHANNELS":
            k = int(tokens[i + 1])
            joints[stack[-1]].channels = tokens[i + 2 : i + 2 + k]
            i += 2 + k
        elif t == "}":
            stack.pop()
            i += 1
        else:
            i += 1
    # MOTION
    assert tokens[i] == "MOTION"
    frames_n = int(tokens[i + 2])
    frame_time = float(tokens[i + 5])
    values = np.array([float(x) for x in tokens[i + 6 :]], dtype=np.float64)
    channels = sum(len(j.channels) for j in joints)
    frames = values[: frames_n * channels].reshape(frames_n, channels)
    return Bvh(joints, frames, frame_time)


def _rot(axis: str, deg: float) -> np.ndarray:
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    if axis == "X":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "Y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def world_positions(bvh: Bvh, frame: int) -> dict[str, np.ndarray]:
    """Прямая кинематика: мировые координаты каждого сустава в кадре."""
    out: dict[str, np.ndarray] = {}
    row = bvh.frames[frame]
    col = 0
    mats: list[np.ndarray] = []
    poss: list[np.ndarray] = []
    for j in bvh.joints:
        local_pos = j.offset.copy()
        rot = np.eye(3)
        for ch in j.channels:
            v = row[col]
            col += 1
            if ch.endswith("position"):
                axis = "XYZ".index(ch[0].upper())
                local_pos[axis] = v if j.parent is None else local_pos[axis] + v
            else:
                rot = rot @ _rot(ch[0].upper(), v)
        if j.parent is None:
            m, p = rot, local_pos
        else:
            pm, pp = mats[j.parent], poss[j.parent]
            p = pp + pm @ local_pos
            m = pm @ rot
        mats.append(m)
        poss.append(p)
        out[j.name] = p
    return out


def openpose_points(world: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Суставы BVH → 18 точек OpenPose (глаза и уши — от головы, чуть вперёд и в стороны)."""

    def get(key: str) -> np.ndarray:
        for alias in JOINT_ALIASES[key]:
            if alias in world:
                return world[alias]
        raise KeyError(f"в BVH нет сустава для {key}: {JOINT_ALIASES[key]}")

    pts: dict[str, np.ndarray] = {k: get(k) for k in JOINT_ALIASES if k != "head"}
    head = get("head")
    neck = pts["neck"]
    up = head - neck
    height = float(np.linalg.norm(up)) or 1.0
    # лицо: нос выше шеи на ~1.3 длины шеи, глаза/уши — по бокам, ширина от плеч
    shoulder_w = float(np.linalg.norm(pts["rsho"] - pts["lsho"])) or 1.0
    side = (pts["lsho"] - pts["rsho"]) / shoulder_w
    forward = np.cross(side, up / height)
    nose = neck + up * 1.35 + forward * shoulder_w * 0.25
    pts["nose"] = nose
    pts["reye"] = nose + up * 0.12 - side * shoulder_w * 0.08 + forward * -0.05 * shoulder_w
    pts["leye"] = nose + up * 0.12 + side * shoulder_w * 0.08 + forward * -0.05 * shoulder_w
    pts["rear"] = neck + up * 1.2 - side * shoulder_w * 0.2
    pts["lear"] = neck + up * 1.2 + side * shoulder_w * 0.2
    return pts


@dataclass
class Camera:
    azimuth_deg: float = 0.0  # 0 — фигура лицом к камере, +90 — правым боком
    elevation_deg: float = 0.0
    mirror: bool = False


def _facing_yaw(pts: dict[str, np.ndarray]) -> float:
    """На сколько градусов повернуть вокруг Y, чтобы фигура смотрела в камеру
    (вперёд = −Z после поворота, правое плечо слева на экране)."""
    side = pts["lsho"] - pts["rsho"]
    up = np.array([0.0, 1.0, 0.0])
    forward = np.cross(side, up)  # для фигуры лицом к +Z даёт +Z
    yaw = math.degrees(math.atan2(forward[0], forward[2]))
    # хотим forward → +Z (к зрителю при ортографии со взглядом вдоль −Z)
    return -yaw


def project(pts: dict[str, np.ndarray], cam: Camera) -> dict[str, tuple[float, float]]:
    """Ортографическая проекция после поворота фигуры: BVH — Y вверх."""
    ry = _rot("Y", cam.azimuth_deg)
    rx = _rot("X", cam.elevation_deg)
    out: dict[str, tuple[float, float]] = {}
    for k, p in pts.items():
        q = rx @ (ry @ p)
        x = -q[0] if cam.mirror else q[0]
        out[k] = (float(x), float(-q[1]))  # экран: y вниз
    if cam.mirror:
        # зеркальный человек: отражаются координаты И меняются стороны тела —
        # иначе правое плечо (свой цвет) окажется справа на экране, и модель
        # прочитает фигуру как стоящую спиной
        for r, left in (
            ("rsho", "lsho"), ("relb", "lelb"), ("rwri", "lwri"), ("rhip", "lhip"),
            ("rkne", "lkne"), ("rank", "lank"), ("reye", "leye"), ("rear", "lear"),
        ):  # fmt: skip
            out[r], out[left] = out[left], out[r]
    return out


@dataclass
class Layout:
    width: int = 576
    height: int = 1024
    figure_height: float = 0.7  # доля высоты кадра от макушки до пят
    center_x: float = 0.42  # центр фигуры по ширине
    bottom_y: float = 0.95  # где стоят ноги
    stick: int = 4


def _fit(
    frames_2d: list[dict[str, tuple[float, float]]], layout: Layout
) -> list[dict[str, tuple[float, float]]]:
    """Масштаб и сдвиг общие для всех кадров: фигура не «дышит» по размеру."""
    ys = [y for f in frames_2d for _, y in f.values()]
    xs = [x for f in frames_2d for x, _ in f.values()]
    top, bottom = min(ys), max(ys)
    scale = layout.figure_height * layout.height / max(bottom - top, 1e-6)
    cx = (min(xs) + max(xs)) / 2
    out = []
    for f in frames_2d:
        out.append(
            {
                k: (
                    layout.center_x * layout.width + (x - cx) * scale,
                    layout.bottom_y * layout.height - (bottom - y) * scale,
                )
                for k, (x, y) in f.items()
            }
        )
    return out


def draw_pose(points: dict[str, tuple[float, float]], layout: Layout) -> Image.Image:
    """Скелет в стиле DWPose: цветные звенья-эллипсы и точки на чёрном."""
    im = Image.new("RGB", (layout.width, layout.height), (0, 0, 0))
    d = ImageDraw.Draw(im)
    xy = [points[k] for k in POSE_POINTS]
    w = layout.stick
    for (a, b), color in zip(LIMBS, COLORS[: len(LIMBS)], strict=True):
        (x1, y1), (x2, y2) = xy[a], xy[b]
        d.line([(x1, y1), (x2, y2)], fill=color, width=w * 2)
    for (x, y), color in zip(xy, COLORS, strict=True):
        d.ellipse([x - w, y - w, x + w, y + w], fill=color)
    return im


def resample(bvh: Bvh, start_sec: float, end_sec: float, speed: float, fps: int) -> list[int]:
    """Индексы кадров BVH под выходной fps: отрезок [start, end] со скоростью speed."""
    duration = (end_sec - start_sec) / speed
    n = max(1, int(round(duration * fps)))
    return [
        min(bvh.frames.shape[0] - 1, int(round((start_sec + i / fps * speed) * bvh.fps)))
        for i in range(n)
    ]


def render_control(
    bvh_path: Path,
    out_path: Path,
    *,
    start_sec: float,
    end_sec: float,
    speed: float = 1.0,
    lead_sec: float = 0.7,
    total_frames: int = 73,
    fps: int = 24,
    cam: Camera | None = None,
    layout: Layout | None = None,
    ffmpeg: str | None = None,
) -> Path:
    """BVH → mp4 со скелетом: пауза, движение, удержание последнего кадра."""
    from videoforge.core.ffmpeg import ffmpeg_exe

    ffmpeg = ffmpeg or ffmpeg_exe()
    cam = cam or Camera()
    layout = layout or Layout()
    if (total_frames - 1) % 8:
        raise ValueError(f"кадров должно быть 8n+1, а не {total_frames}")
    bvh = parse_bvh(bvh_path)
    idx = resample(bvh, start_sec, end_sec, speed, fps)
    lead = int(round(lead_sec * fps))
    seq = [idx[0]] * lead + idx
    seq = seq[:total_frames] + [seq[-1]] * max(0, total_frames - len(seq))
    # «0° — лицом к камере» считается от самой фигуры, а не от осей записи: в
    # cat_punch_mocap запись CMU смотрела от камеры, и кот при «20°» к концу
    # разворачивался спиной. Поворот — по линии плеч первого кадра движения
    first = openpose_points(world_positions(bvh, idx[0]))
    yaw = _facing_yaw(first)
    cam = Camera(cam.azimuth_deg + yaw, cam.elevation_deg, cam.mirror)
    frames_2d = [project(openpose_points(world_positions(bvh, i)), cam) for i in seq]
    frames_2d = _fit(frames_2d, layout)
    tmp = out_path.with_suffix("")
    tmp.mkdir(parents=True, exist_ok=True)
    for n, f in enumerate(frames_2d):
        draw_pose(f, layout).save(tmp / f"{n:04d}.png")
    subprocess.run(
        [
            ffmpeg, "-loglevel", "error", "-y", "-framerate", str(fps),
            "-i", str(tmp / "%04d.png"), "-frames:v", str(total_frames),
            "-c:v", "libx264", "-preset", "slow", "-crf", "12", "-pix_fmt", "yuv420p",
            str(out_path),
        ],
        check=True,
    )  # fmt: skip
    for p in tmp.glob("*.png"):
        p.unlink()
    tmp.rmdir()
    return out_path
