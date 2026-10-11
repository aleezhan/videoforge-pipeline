"""Форма волны звука: громкость по окнам, чтобы дорожку было видно глазом.

Нужна пульту. Все замеры звука до сих пор были числами в JSON — «−14 LUFS»,
«тишины 6.3 с», — и по ним нельзя ответить на вопрос, который человек задаёт
глядя на монтаж: **вот здесь поют или молчат, и попадает ли сюда слово**.
Волна отвечает на него за секунду.

Считается тем же ffmpeg-ом, что и всё остальное, и тем же приёмом, что
`motion_energy` считает движение: фильтр меряет окна, `ametadata` печатает
числа, python их собирает. Своих зависимостей (numpy, librosa) не заводит.

Кэш — `<файл>.peaks.json` рядом с файлом, как `<клип>.motion.json` рядом с
клипом: пересчитывается, когда файл новее кэша. Ролик на 26 секунд меряется за
0.14 с, десятиминутный — за пару секунд, так что кэш здесь ради страницы,
которую открывают десять раз подряд, а не ради экономии минут.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from videoforge.core.ffmpeg import FfmpegError, ffmpeg_exe
from videoforge.core.proc import NO_WINDOW

log = logging.getLogger(__name__)

# Точек на секунду. 25 — окно 40 мс, кадр при 25 fps: рассинхрон слова и звука
# меньше кадра всё равно не виден, а больше точек только тяжелее гонять в JSON.
PEAKS_PER_SEC = 25
RATE = 8000  # Гц, моно: для картинки волны хватает, декодируется мгновенно
FLOOR_DB = -60.0  # тише этого — тишина; иначе шум дорожки рисуется как сигнал
CACHE_VERSION = 1

_LEVEL = re.compile(
    r"pts_time:([\d.]+)\s*\r?\nlavfi\.astats\.Overall\.RMS_level=(-?[\d.]+|-inf|nan)"
)


@dataclass(frozen=True)
class Envelope:
    """Огибающая дорожки: `values[i]` — громкость окна i (0..1, RMS)."""

    per_sec: int
    values: list[float]

    @property
    def duration_sec(self) -> float:
        return round(len(self.values) / self.per_sec, 3) if self.per_sec else 0.0

    def dict(self) -> dict[str, object]:
        return {"per_sec": self.per_sec, "values": self.values}


def _amplitude(level: str) -> float:
    """dBFS от astats → 0..1. `-inf` и `nan` — цифровая тишина."""
    if level in ("-inf", "nan"):
        return 0.0
    db = float(level)
    if db <= FLOOR_DB:
        return 0.0
    return round(min(1.0, 10 ** (min(db, 0.0) / 20)), 3)


def envelope(path: Path, *, per_sec: int = PEAKS_PER_SEC) -> Envelope:
    """Громкость дорожки по окнам 1/`per_sec` секунды.

    Одна печатающая ветка фильтра намеренно: два `ametadata=print` в один
    дескриптор перемешиваются буферами, и строка «сколько» перестаёт надёжно
    относиться к строке «когда». Пик отдельно от RMS картинке ничего не
    добавляет, а перепутанные числа — это враньё на дорожке."""
    samples = max(1, round(RATE / per_sec))
    proc = subprocess.run(
        [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
            "-af", (
                f"aformat=sample_fmts=fltp:channel_layouts=mono,aresample={RATE},"
                f"asetnsamples=n={samples}:p=0,astats=metadata=1:reset=1,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
            ),
            "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )  # fmt: skip
    if proc.returncode != 0:
        raise FfmpegError(f"волна не измерена ({path}):\n{proc.stderr.strip()[-400:]}")
    return Envelope(per_sec, [_amplitude(level) for _, level in _LEVEL.findall(proc.stdout)])


def cached_envelope(path: Path, *, per_sec: int = PEAKS_PER_SEC) -> Envelope | None:
    """Волна из кэша рядом с файлом или заново. Нет ffmpeg или битый файл —
    None: страница живёт без волны, как галерея живёт без обложки."""
    if not path.exists():
        return None
    cache = path.with_suffix(path.suffix + ".peaks.json")
    st = path.stat()
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if (
            isinstance(data, dict)
            and data.get("version") == CACHE_VERSION
            and data.get("per_sec") == per_sec
            and data.get("size") == st.st_size
            and data.get("mtime_ns") == st.st_mtime_ns
        ):
            return Envelope(per_sec, list(data.get("values") or []))
    try:
        env = envelope(path, per_sec=per_sec)
    except (FfmpegError, OSError) as exc:
        log.warning("волна для %s не измерена: %s", path, exc)
        return None
    try:
        cache.write_text(
            json.dumps(
                {
                    "version": CACHE_VERSION,
                    "per_sec": per_sec,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "values": env.values,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    except OSError as exc:  # ридонли-папка не повод не показать волну
        log.warning("кэш волны %s не записан: %s", cache, exc)
    return env
