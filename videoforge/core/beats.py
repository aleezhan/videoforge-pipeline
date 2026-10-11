"""Удары трека: где на самом деле бьёт пульс.

Зачем. Эдит под музыку режется НА УДАРЫ, и первый соблазн — посчитать темп и
разложить сетку долей. На живой записи это враньё: барабанщик играет без клика,
и сетка расходится с музыкой уже к третьему такту. Видно это по самим
кандидатам темпа: на концертнике они встали вплотную (122.3 / 129.2 / 115.6
BPM), то есть выбирается «темп» там монеткой. Поэтому склейки ставят на
НАЙДЕННЫЕ удары, а сетку не строят вовсе.

Метод — тот же, что у волны и движения: ffmpeg меряет окна, `ametadata` печатает
числа, python считает. Своих зависимостей (librosa, numpy) нет и не надо: удар
это скачок энергии в низу спектра, а скачок считается вычитанием.

Чего здесь НЕТ и почему. Была попытка числом отвечать «живая игра или машина»
— по согласию автокорреляции. Опровергнуто замером 10.09.2026 на трёх треках:
машинные дали 0.397 и 0.161, живой концертник 0.419, то есть живая запись
набрала БОЛЬШЕ обеих машинных. Величина отличает не живое от машинного, а
ровный бас от неровного, и как признак не годится. Осталась только оценка
темпа (`tempo`), у которой есть внешнее подтверждение: первый кандидат 122.4
BPM совпал с 122.3, посчитанными прежним, ныне удалённым скриптом.

Это НЕ восстановление того скрипта. Его нет, остался только его вывод —
`onsets.json` одного эпизода, и сверка показала расхождение: из 44 прежних
ударов здесь совпало 27, постоянный сдвиг положения не спасает (−65 мс поднимает
до 32). Который набор вернее, по числам не решить: мерить нечем, кроме готового
ролика. Поэтому t04 остаётся на своих прежних числах, а этот модуль — новая
реализация со своими тестами, а не замена задним числом.
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

# Удар держит низ: бочка и малый барабан живут до 250 Гц, а вокал и гитары выше
# только мешают — на них флюкс скачет от каждого слова
BAND_HZ = 250
PER_SEC = 100  # окно 10 мс: удар короче кадра, по 40 мс его край не поймать
MIN_GAP_SEC = 0.12  # ближе — это один удар, а не два: так звучит атака, а не доля
NEAR_SEC = 0.5  # окно, по которому считается местный порог
MAD_K = 2.5  # во сколько отклонений порог выше медианы — как в статистике анализа
BPM_RANGE = (60, 180)  # где вообще бывает танцевальный и песенный темп
CACHE_VERSION = 1
RATE = 8000

_LEVEL = re.compile(
    r"pts_time:([\d.]+)\s*\r?\nlavfi\.astats\.Overall\.RMS_level=(-?[\d.]+|-inf|nan)"
)
FLOOR_DB = -60.0


@dataclass(frozen=True)
class Beat:
    """Удар: когда и насколько сильный (0..1 относительно самого сильного)."""

    at: float
    strength: float


@dataclass(frozen=True)
class Tempo:
    """Кандидаты темпа по убыванию согласия автокорреляции.

    `agreement` 0..1 — насколько повторяется низ на лучшем периоде. Читать его
    как «живое или машинное» НЕЛЬЗЯ: замерено, что живой концертник набирает
    больше машинного трека. Полезен он иначе — когда кандидаты стоят вплотную
    (122.3 / 129.2 / 115.6), выбирать между ними нечем, и сетку долей строить
    не по чему; тогда режут по найденным ударам."""

    agreement: float
    bpm: list[float]


def _amplitude(level: str) -> float:
    if level in ("-inf", "nan"):
        return 0.0
    db = float(level)
    return 0.0 if db <= FLOOR_DB else round(min(1.0, 10 ** (min(db, 0.0) / 20)), 4)


def low_energy(path: Path, *, band_hz: int = BAND_HZ, per_sec: int = PER_SEC) -> list[float]:
    """Энергия низа по окнам. Одна печатающая ветка фильтра — как в `waveform`:
    два `ametadata=print` в один дескриптор перемешиваются буферами."""
    samples = max(1, round(RATE / per_sec))
    proc = subprocess.run(
        [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
            "-af", (
                f"aformat=sample_fmts=fltp:channel_layouts=mono,aresample={RATE},"
                f"lowpass=f={band_hz},"
                f"asetnsamples=n={samples}:p=0,astats=metadata=1:reset=1,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
            ),
            "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )  # fmt: skip
    if proc.returncode != 0:
        raise FfmpegError(f"удары не измерены ({path}):\n{proc.stderr.strip()[-400:]}")
    return [_amplitude(level) for _, level in _LEVEL.findall(proc.stdout)]


def flux(energy: list[float]) -> list[float]:
    """Скачок энергии вверх. Падение — не удар: звук затухает всегда, а бьёт
    только рост, поэтому отрицательная разность обнуляется."""
    return [0.0, *[max(0.0, b - a) for a, b in zip(energy, energy[1:], strict=False)]]


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def pick(
    values: list[float], *, per_sec: int = PER_SEC, min_gap_sec: float = MIN_GAP_SEC
) -> list[Beat]:
    """Пики флюкса → удары. Порог местный, а не общий на трек.

    Общий порог теряет тихое вступление и тонет в громком припеве: у живой
    записи разница между ними десятки децибел. Местный (медиана окрестности
    плюс отклонения) одинаково работает и там и там — тот же приём, которым
    анализатор ищет выбросы просмотров."""
    if not values:
        return []
    near = max(1, round(NEAR_SEC * per_sec))
    gap = max(1, round(min_gap_sec * per_sec))
    peak = max(values) or 1.0
    found: list[Beat] = []
    last = -gap
    for i, value in enumerate(values):
        if value <= 0 or i - last < gap:
            continue
        window = values[max(0, i - near) : i + near + 1]
        base = _median(window)
        spread = _median([abs(v - base) for v in window]) or 1e-6
        if value < base + MAD_K * spread:
            continue
        if value < max(values[max(0, i - gap) : i + gap + 1]):  # не местный максимум
            continue
        found.append(Beat(round(i / per_sec, 3), round(value / peak, 3)))
        last = i
    return found


def tempo(values: list[float], *, per_sec: int = PER_SEC) -> Tempo:
    """Кандидаты темпа по автокорреляции флюкса.

    Считается по самому сигналу, а не по найденным ударам: пропущенный удар
    рвёт очередь, а корреляция его переживает. Нужен, чтобы увидеть, СТОЯТ ЛИ
    кандидаты вплотную — тогда темпа как такового нет и сетку долей строить не
    по чему."""
    total = len(values)
    if total < per_sec:
        return Tempo(0.0, [])
    mean = sum(values) / total
    centred = [v - mean for v in values]
    norm = sum(v * v for v in centred) or 1e-9
    scores: list[tuple[float, float]] = []
    slow, fast = BPM_RANGE
    for lag in range(round(per_sec * 60 / fast), round(per_sec * 60 / slow) + 1):
        pairs = total - lag
        if pairs <= per_sec:
            break
        score = sum(centred[i] * centred[i + lag] for i in range(pairs)) / norm
        scores.append((score, round(60 * per_sec / lag, 1)))
    if not scores:
        return Tempo(0.0, [])
    scores.sort(reverse=True)
    best = max(0.0, scores[0][0])
    return Tempo(round(best, 3), [bpm for _, bpm in scores[:3]])


SNAP_SEC = 0.05  # дальше этого склейка на удар уже не попала: полтора кадра при 30 fps


@dataclass(frozen=True)
class Snap:
    """Куда встала склейка и на сколько она промахнулась мимо удара."""

    at: float
    beat: float | None
    error: float | None  # None — рядом вообще нет удара

    @property
    def hit(self) -> bool:
        return self.error is not None and abs(self.error) <= SNAP_SEC


def snap(times: list[float], found: list[Beat], *, tolerance: float = SNAP_SEC) -> list[Snap]:
    """Притянуть склейки к ближайшим ударам и сказать, кто не дотянулся.

    Монтаж под музыку держится на том, что склейка стоит НА ударе, а не рядом:
    промах в полкадра ухо прощает, в три — уже слышно как «не в такт». Раньше
    это считал разовый скрипт эпизода, и проверить чужой ролик было нечем.

    Возвращает по записи на каждую склейку, включая непопавшие: молчать о них
    нельзя — именно они и портят ритм."""
    out: list[Snap] = []
    for at in times:
        near = min(found, key=lambda b: abs(b.at - at), default=None)
        if near is None:
            out.append(Snap(round(at, 3), None, None))
            continue
        error = round(at - near.at, 3)
        fits = abs(error) <= tolerance
        out.append(Snap(round(near.at if fits else at, 3), near.at if fits else None, error))
    return out


def beats(path: Path, *, band_hz: int = BAND_HZ, per_sec: int = PER_SEC) -> list[Beat]:
    """Удары трека. Кэш `<файл>.beats.json` рядом, как волна и профиль движения."""
    cache = path.with_suffix(path.suffix + ".beats.json")
    st = path.stat()
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if (
            isinstance(data, dict)
            and data.get("version") == CACHE_VERSION
            and data.get("mtime_ns") == st.st_mtime_ns
            and data.get("band_hz") == band_hz
        ):
            return [Beat(float(b["at"]), float(b["strength"])) for b in data.get("beats") or []]
    values = flux(low_energy(path, band_hz=band_hz, per_sec=per_sec))
    found = pick(values, per_sec=per_sec)
    try:
        cache.write_text(
            json.dumps(
                {
                    "version": CACHE_VERSION,
                    "mtime_ns": st.st_mtime_ns,
                    "band_hz": band_hz,
                    "tempo_bpm": tempo(values, per_sec=per_sec).bpm,
                    "beats": [{"at": b.at, "strength": b.strength} for b in found],
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("кэш ударов %s не записан: %s", cache, exc)
    return found
