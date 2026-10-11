"""Профиль движения клипа: где в нём что-то происходит, а где кадр стоит.

Считает ffmpeg, а не модель (`ffmpeg.motion_energy`): разность соседних кадров,
усреднённая по окнам 0.5 с. Откалибровано 04.09.2026 на живых клипах:
статичный кадр — 0.03–0.05 (шум кодека), медленный наезд Remotion — 0.5–0.9,
заглушка Ken Burns — ~1.0; сцена «пустое небо» из sci_mini/ep001, которую
детекторы финала ловили как застывшую, здесь даёт 0.04.

Зачем: (а) мёртвый клип генеративного провайдера — брак, и его видно ещё в
стадии `video`, до сборки; (б) монтажёру нужно знать, где клип тихий, а где
пик, — переход ставят в тихий момент, а не поперёк движения, и отрезок сцены не
должен выбрасывать пик. Профиль лежит рядом с клипом (`<clip>.motion.json`) и
меряется один раз.
"""

from __future__ import annotations

from pathlib import Path
from statistics import median

from pydantic import BaseModel, Field

from videoforge.core.ffmpeg import motion_energy
from videoforge.core.io import read_model, write_model

MOTION_STEP_SEC = 0.5
DEAD_ENERGY = 0.15  # ниже во всех окнах — статичный кадр (шум кодека 0.03–0.05)
PEAK_RATIO = 1.5  # max/медиана ниже — движение ровное, пика нет (наезд, панорама)
PEAK_SHARE = 0.7  # окна не ниже этой доли максимума — пик
QUIET_SHARE = 0.3  # окна ниже этой доли максимума — тихо

Window = tuple[float, float]


class MotionProfile(BaseModel):
    step_sec: float = MOTION_STEP_SEC
    energy: list[float] = Field(default_factory=list)  # по окнам, 0..255

    @property
    def duration_sec(self) -> float:
        return round(len(self.energy) * self.step_sec, 3)

    @property
    def peak(self) -> float:
        return max(self.energy, default=0.0)

    def is_dead(self) -> bool:
        return bool(self.energy) and self.peak < DEAD_ENERGY

    def is_steady(self) -> bool:
        """Движение без пиков: наезд или панорама одной скорости."""
        if not self.energy or self.is_dead():
            return False
        return self.peak <= PEAK_RATIO * max(median(self.energy), 1e-6)

    def peak_window(self) -> Window | None:
        """Окно вокруг максимума, где энергия держится не ниже PEAK_SHARE от него."""
        if not self.energy or self.is_dead() or self.is_steady():
            return None
        top = self.peak
        i = self.energy.index(top)
        lo, hi = i, i
        while lo > 0 and self.energy[lo - 1] >= PEAK_SHARE * top:
            lo -= 1
        while hi + 1 < len(self.energy) and self.energy[hi + 1] >= PEAK_SHARE * top:
            hi += 1
        return (round(lo * self.step_sec, 2), round((hi + 1) * self.step_sec, 2))

    def quiet_windows(self) -> list[Window]:
        """Отрезки, где почти ничего не происходит: сюда ставят переход."""
        if not self.energy:
            return []
        if self.is_dead():
            return [(0.0, self.duration_sec)]
        limit = QUIET_SHARE * self.peak
        out: list[Window] = []
        start: int | None = None
        for i, value in enumerate([*self.energy, None]):
            quiet = value is not None and value < limit
            if quiet and start is None:
                start = i
            elif not quiet and start is not None:
                out.append((round(start * self.step_sec, 2), round(i * self.step_sec, 2)))
                start = None
        return out

    def describe(self) -> str:
        """Одна строка для промпта монтажёра и для человека."""
        if not self.energy:
            return "not measured"
        if self.is_dead():
            return "static (no motion)"
        if self.is_steady():
            return f"steady ({sum(self.energy) / len(self.energy):.2f})"
        parts = []
        if quiet := self.quiet_windows():
            parts.append("quiet " + ", ".join(f"{a:g}–{b:g}s" for a, b in quiet))
        if peak := self.peak_window():
            parts.append(f"peak {peak[0]:g}–{peak[1]:g}s")
        return ", ".join(parts) or "uneven"


def motion_path(video: Path) -> Path:
    return video.with_suffix(".motion.json")


def motion_profile(video: Path, *, step_sec: float = MOTION_STEP_SEC) -> MotionProfile:
    return MotionProfile(step_sec=step_sec, energy=motion_energy(video, step_sec=step_sec))


def load_or_measure(video: Path) -> MotionProfile:
    """Профиль из файла рядом с клипом, если он не старее клипа; иначе замер и
    запись. Замер — секунды ffmpeg на клип, повторять его каждый прогон незачем."""
    cache = motion_path(video)
    if cache.exists() and cache.stat().st_mtime >= video.stat().st_mtime:
        try:
            return read_model(cache, MotionProfile)
        except ValueError:
            pass
    profile = motion_profile(video)
    write_model(cache, profile)
    return profile
