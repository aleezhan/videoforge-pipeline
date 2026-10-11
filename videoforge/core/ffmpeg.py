"""Обёртка над ffmpeg. Бинарь берётся из PATH, иначе из пакета imageio-ffmpeg.

ffprobe не используется намеренно: imageio-ffmpeg его не поставляет,
длительность читается из stderr `ffmpeg -i`.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from videoforge.core.io import replace_file
from videoforge.core.proc import NO_WINDOW
from videoforge.core.storyboard import FPS, AspectRatio

if TYPE_CHECKING:  # картинку подписи рисует Pillow; рендеру нужен только её путь и место
    from videoforge.core.headline import Headline

SIZES: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    # портретные форматы кадров портфолио (референсы, не кадры видео)
    "2:3": (1080, 1620),
    "3:4": (1080, 1440),
}
# FPS — умолчание из контракта (`Storyboard.fps` пуст → 24), импортировано
# сюда же: рендер и тесты берут его отсюда. Частота эпизода приходит
# параметром `fps` каждой функции, константа — только умолчание параметра:
# до 14.09.2026 она была зашита в ~30 местах, и у 25-кадрового подкаста
# каждый 25-й кадр выбрасывался ещё на нарезке

# дробные частоты NTSC: `ffmpeg -i` печатает их двумя знаками («29.97»,
# «23.98»), а фильтру нужна точная дробь — по округлённой сетка кадров
# расходится с исходником, и `fps` начинает дублировать и выбрасывать кадры
_NTSC_BASES = (24, 30, 48, 60, 120)


def fps_expr(fps: float) -> str:
    """Частота для аргументов ffmpeg: `fps=…`, `-r …`, `zoompan … fps=…`.

    Целая → «30». Дробная NTSC (23.976 / 29.97 / 59.94 и то, как их печатает
    `ffmpeg -i`) → «24000/1001»: ffmpeg принимает дробь, и только она точна.
    Прочая дробь — до трёх знаков: точнее `ffmpeg -i` всё равно не сообщает,
    а брать частоту неоткуда, кроме как из него."""
    if abs(fps - round(fps)) < 1e-6:
        return str(int(round(fps)))
    for base in _NTSC_BASES:
        if abs(fps - base * 1000 / 1001) < 0.02:
            return f"{base}000/1001"
    return f"{fps:.3f}".rstrip("0").rstrip(".")


class FfmpegError(RuntimeError):
    pass


def aspect_size(aspect_ratio: AspectRatio | str) -> tuple[int, int]:
    return SIZES[aspect_ratio]


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # noqa: BLE001
        raise FfmpegError(
            "ffmpeg не найден: поставь ffmpeg в PATH или пакет imageio-ffmpeg (uv sync)"
        ) from exc


def run(args: list[str]) -> None:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW
    )
    if proc.returncode != 0:
        raise FfmpegError(f"ffmpeg завершился с кодом {proc.returncode}:\n{proc.stderr.strip()}")


def _run_capture(args: list[str]) -> str:
    """ffmpeg на уровне info: детекторы (blackdetect, silencedetect, ebur128,
    loudnorm) пишут результаты только в лог, и читать их надо оттуда."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-loglevel", "info", "-y", *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW
    )
    if proc.returncode != 0:
        tail = proc.stderr.strip()[-600:]
        raise FfmpegError(f"ffmpeg завершился с кодом {proc.returncode}:\n{tail}")
    return proc.stderr


_FILTER_LINE = re.compile(r"^\s*[A-Z.]{3}\s+(\S+)\s", re.M)


@lru_cache(maxsize=1)
def available_filters() -> frozenset[str]:
    """Имена фильтров в этой сборке ffmpeg: libass и компрессор есть не везде."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-filters"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )  # fmt: skip
    return frozenset(_FILTER_LINE.findall(proc.stdout))


_FILTER_OPTION = re.compile(r"^\s+(\w+)\s+<", re.M)


@lru_cache(maxsize=8)
def filter_options(name: str) -> frozenset[str]:
    """Имена опций фильтра в этой сборке ffmpeg.

    Сборки расходятся не только набором фильтров, но и их опциями: в 7.1 у
    `crop` больше нет `eval` (выражения и так считаются на каждом кадре), а в
    более старых без `eval=frame` то же выражение посчиталось бы один раз — и
    окно кадра молча застыло бы вместо того, чтобы следовать за планами."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-h", f"filter={name}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )  # fmt: skip
    return frozenset(_FILTER_OPTION.findall(proc.stdout))


def filter_path(path: Path) -> str:
    """Путь внутри аргумента фильтра (`ass=filename=…`).

    Граф фильтров разбирается в два уровня: сначала граф (запятые, точки с
    запятой, скобки; одинарные кавычки берут текст буквально), потом опции
    фильтра (двоеточие — разделитель, `\\:` — экранированное). Поэтому значение
    идёт в кавычках, а двоеточие диска — с одним обратным слэшем: без кавычек
    первый уровень снимает слэш, и `C:/…` читается как опция `C`."""
    text = path.resolve().as_posix()
    if "'" in text:
        raise FfmpegError(f"путь с апострофом нельзя передать в фильтр ffmpeg: {text}")
    return "'" + text.replace("\\", "\\\\").replace(":", "\\:") + "'"


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **NO_WINDOW,
    )
    m = _DURATION.search(proc.stderr)
    if not m:
        raise FfmpegError(f"не удалось прочитать длительность {path}:\n{proc.stderr.strip()}")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


_STREAM_SIZE = re.compile(r"Video:.*?\s(\d{2,5})x(\d{2,5})[\s,\[]")


def probe_size(path: Path) -> tuple[int, int] | None:
    """Размер видеодорожки из stderr `ffmpeg -i` (ffprobe в проекте нет)."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )  # fmt: skip
    m = _STREAM_SIZE.search(proc.stderr)
    return (int(m.group(1)), int(m.group(2))) if m else None


_STREAM_FPS = re.compile(r"Video:.*?\s(\d+(?:\.\d+)?) fps")


def probe_fps(path: Path) -> float | None:
    """Частота кадров видеодорожки из stderr `ffmpeg -i` — тем же способом,
    что размер: ffprobe в проекте нет."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )  # fmt: skip
    m = _STREAM_FPS.search(proc.stderr)
    return float(m.group(1)) if m else None


_OUT_TIME_US = re.compile(r"out_time_us=(\d+)")


def probe_stream_duration(path: Path, stream: str) -> float | None:
    """Длительность одной дорожки (`0:v:0` / `0:a:0`), а не контейнера.

    Длительность контейнера — одно число на файл, и рассинхрон дорожек она
    прячет: файл, где звук на секунду длиннее картинки, отчитается нормально.
    ffprobe в проекте нет (imageio-ffmpeg его не кладёт), поэтому дорожка
    прогоняется в null-муксер, а длительность читается из `-progress`.
    None — дорожки в файле нет."""
    proc = subprocess.run(
        [
            ffmpeg_exe(), "-hide_banner", "-nostats", "-loglevel", "error",
            "-i", str(path), "-map", stream, "-f", "null", "-progress", "-", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )  # fmt: skip
    marks = _OUT_TIME_US.findall(proc.stdout)
    if not marks:
        return None
    return int(marks[-1]) / 1_000_000


def image_to_video(
    image: Path, out: Path, *, duration: float, size: tuple[int, int], fps: float = FPS
) -> None:
    """Картинка → клип с медленным наездом (Ken Burns). Заглушка вместо image-to-video."""
    w, h = size
    frames = max(1, int(round(duration * fps)))
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"zoompan=z='1+0.12*on/{frames}':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps_expr(fps)},"
        f"format=yuv420p"
    )
    run(
        [
            "-loop", "1", "-i", str(image),
            "-vf", vf, "-frames:v", str(frames), "-r", fps_expr(fps),
            "-c:v", "libx264", "-preset", "ultrafast", str(out),
        ]
    )  # fmt: skip


_PTS_TIME = re.compile(r"pts_time:(\d+(?:\.\d+)?)")
# Стандартные для игрового видео 0.3 на анимации почти ничего не находят: у неё
# плоские цвета и мягкие переходы вместо жёстких склеек. 0.05 откалиброван по
# Kurzgesagt глазами (контактный лист раз в 2 с): даёт медиану ~6.7 с при
# ~6.4 с реальной смены картинки. Нас интересует именно смена картинки — под
# неё наш конвейер генерирует отдельный кадр, — а не монтажная склейка.
SCENE_THRESHOLD = 0.05


def detect_cuts(video: Path, *, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Таймкоды склеек: ffmpeg сам считает «непохожесть» соседних кадров.

    Ни LLM, ни сети — чистая обработка видео, поэтому ритм монтажа чужого
    ролика достаётся бесплатно."""
    proc = subprocess.run(
        [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(video),
            "-vf", f"select='gt(scene,{threshold})',metadata=print:file=-",
            "-an", "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )  # fmt: skip
    if proc.returncode != 0:
        raise FfmpegError(f"детект склеек не удался ({video}):\n{proc.stderr.strip()[-400:]}")
    return sorted(float(t) for t in _PTS_TIME.findall(proc.stdout))


def frame_at(video: Path, seconds: float, out: Path) -> Path:
    """Один кадр в заданной секунде — для разбора конкретного бита."""
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-ss", f"{max(seconds, 0):.3f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "3", str(out)])  # fmt: skip
    return out


def frames_at(video: Path, seconds: list[float], out_dir: Path, *, width: int = 320) -> list[Path]:
    """Много кадров в заданных секундах — ОДНИМ проходом по файлу.

    По кадру за вызов было бы честно и просто, но каждый вызов — это запуск
    процесса: 46 кадров с двадцатидевятисекундного ролика заняли 20.9 с против
    4.5 с одним проходом (замерено). Разница видна человеку, который открыл
    страницу и ждёт.

    Отбор — «первый кадр не раньше секунды T»: `gte(t,T) * lt(prev_pts*TB,T)`.
    Окно шириной в кадр (`between`) не годится: у 30-кадрового ролика в окно
    1/24 попадает ДВА кадра, и нумерация уезжает — 8 файлов на 4 запрошенные
    секунды (замерено). Если ffmpeg всё же отдал не столько кадров, сколько
    просили, возвращается пустой список: пронумеровать их неправильно хуже,
    чем не показать вовсе."""
    if not seconds:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink(missing_ok=True)
    expr = "+".join(rf"(gte(t\,{s:.3f})*lt(prev_pts*TB\,{s:.3f}))" for s in seconds)
    run(
        [
            "-i", str(video), "-vf", f"select='{expr}',scale={width}:-2",
            "-vsync", "0", "-q:v", "3", str(out_dir / "%03d.jpg"),
        ]
    )  # fmt: skip
    got = sorted(out_dir.glob("*.jpg"))
    return got if len(got) == len(seconds) else []


def extract_frames(video: Path, out_dir: Path, n: int = 6) -> list[Path]:
    """n равномерных кадров из клипа — для визуальной проверки."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()
    duration = probe_duration(video)
    step = max(duration / n, 0.05)
    run(["-i", str(video), "-vf", f"fps=1/{step:.4f}", "-frames:v", str(n), "-q:v", "3",
         str(out_dir / "frame_%02d.jpg")])  # fmt: skip
    return sorted(out_dir.glob("frame_*.jpg"))


_YAVG = re.compile(r"pts_time:([\d.]+)\s*\n\s*lavfi\.signalstats\.YAVG=([\d.]+)")


def motion_energy(video: Path, *, step_sec: float = 0.5) -> list[float]:
    """Энергия движения по окнам `step_sec`: среднее межкадровое изменение
    яркости (0..255) внутри окна. `tblend` даёт разность соседних кадров,
    `signalstats` — её среднюю яркость; окно усредняет по кадрам.

    Шкала маленькая и это нормально: статичный кадр — шум кодека 0.03–0.05,
    медленный наезд — 0.5–1.0, живое движение — единицы. Пороги — в
    `core/motion.py`."""
    proc = subprocess.run(
        [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(video),
            "-vf", "tblend=all_mode=difference,signalstats,"
                   "metadata=print:key=lavfi.signalstats.YAVG:file=-",
            "-an", "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )  # fmt: skip
    if proc.returncode != 0:
        raise FfmpegError(f"замер движения не удался ({video}):\n{proc.stderr.strip()[-400:]}")
    bins: dict[int, list[float]] = {}
    for t, value in _YAVG.findall(proc.stdout):
        bins.setdefault(int(float(t) // step_sec), []).append(float(value))
    if not bins:
        return []
    return [
        round(sum(bins[i]) / len(bins[i]), 3) if bins.get(i) else 0.0 for i in range(max(bins) + 1)
    ]


def trim_audio(source: Path, out: Path, *, start: float, end: float) -> float:
    """Отрезок `[start, end)` звука → WAV (PCM), длина результата в секундах.

    Фильтр `atrim`, а не `-ss`/`-to`: он режет по сэмплу, а не по кадру mp3
    (26 мс). Выход без сжатия намеренно: перекодирование mp3 добавило бы свою
    задержку кодера на краях, и срез по таймингам слов уехал бы на неё же.
    Пишется рядом и подменяется в конце — источник и выход могут совпадать."""
    tmp = out.with_name(out.stem + ".trim.tmp.wav")
    try:
        run(
            [
                "-i", str(source),
                "-af", f"atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS",
                "-c:a", "pcm_s16le", str(tmp),
            ]
        )  # fmt: skip
        replace_file(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return probe_duration(out)


class MuxedScene(NamedTuple):
    duration_sec: float
    frozen_sec: float  # сколько в конце висит замороженный последний кадр
    cropped_share: float  # доля площади клипа, срезанная кропом под формат эпизода


# короткие fade на краях дорожки сцены: стык всегда тишина к тишине, но обрыв
# сэмпла не в нуле всё равно щёлкает — 20 мс убирают это, не меняя длину
EDGE_FADE_SEC = 0.02


def mux_scene(
    video: Path,
    audio: Path | None,
    out: Path,
    *,
    size: tuple[int, int],
    fps: float = FPS,
    target_sec: float | None = None,
    zoom: float = 1.0,
    audio_delay: float = 0.0,
    mute: bool = False,
) -> MuxedScene:
    """Клип + озвучка → сцена длиной `target_sec` (её задаёт раскадровка по
    измеренной озвучке): остаток клипа отрезается, чтобы в кадре не висело
    мёртвое время; клип короче коробки (отрезки плана монтажа не заполнили её)
    достраивается замороженным последним кадром. Озвучку не режем никогда — если
    она длиннее коробки, коробка растёт до неё. Без `target_sec` — максимум из
    двух длин.

    `audio_delay` — заход (`core/pauses.scene_lead_in`): озвучка начинается не с первого
    кадра сцены, а через столько секунд, и картинка до неё идёт в тишине. Так
    склейка стоит посреди паузы, а не вплотную к слову. Длина звука в сцене —
    заход плюс файл; коробка, которую посчитала озвучка, его уже включает.
    Только для отдельной озвучки: звук, живущий в самом клипе (нарезка), с
    картинкой одно целое и не сдвигается.

    `zoom` — во сколько раз увеличить кадр на всю сцену (кроп центра и тот же
    scale, что приводит к формату: одна передискретизация, не две). Нужен
    продолжению длинного плана: i2v начинает каждый клип с исходного
    масштаба, а предыдущее звено кончилось на 1 + k·t — кроп ставит старт на
    то же место, и наезд самого клипа дальше умножается на него, то есть ход
    продолжается с той же скоростью. Кроп ПОСТОЯННЫЙ на звено намеренно:
    плавный «возврат» к 1.0 гасил бы наезд клипа ровно в ноль, и звено стояло
    бы. Центр кропа — центр кадра: куда едет модель, неизвестно, а заглушка и
    remotion едут в центр. Не `zoompan`: он навязывает свою частоту кадров и
    переписывает метки времени (см. `slow_tail`), а здесь нужен только кроп.

    `audio=None` не значит «тишина»: у нарезки речь живёт в самом клипе, и она
    берётся оттуда. Тишина подставляется, только если звуковой дорожки в клипе
    нет (сгенерированное i2v). Иначе первый же шортс собрался бы немым.

    `mute=True` — наоборот, тишина намеренная: собственный звук клипа
    выбрасывается, хотя он есть (`Scene.mute`: в клипе трек без
    лицензии — вместо него в финале играет музыка проекта). Отдельную
    озвучку не трогает: её и так берут вместо дорожки клипа. Сцена всё равно
    получает дорожку тишины, а не остаётся без звука: склейка сцен `concat`
    требует одинакового набора дорожек у всех кусков.

    Кадр приводится к формату эпизода кропом, а не полями: клип генерируется в
    формате эпизода, и расхождение — единицы пикселей округления; чёрные поля
    прятали бы их ценой рамки в каждом кадре. Большой кроп — признак того, что
    провайдер отдал не тот формат; доля срезанного возвращается, чтобы стадия
    предупредила.

    Возвращает длительность сцены и длину заморозки. Заморозка — аварийный
    случай, а не приём, и пока её никто не считал, она могла тихо стать нормой:
    вернуть её здесь дешевле, чем потом искать глазами по готовому ролику."""
    w, h = size
    vdur = probe_duration(video)
    own_audio = probe_stream_duration(video, "0:a:0") if audio is None and not mute else None
    delay = max(0.0, audio_delay) if audio else 0.0
    adur = (delay + probe_duration(audio)) if audio else (own_audio or 0.0)
    # коробка сцены побеждает клип: длиннее — режем, короче — замораживаем
    # последний кадр (и считаем это). Озвучку не режем никогда
    target = max(vdur, adur) if target_sec is None else max(adur, target_sec)
    src = probe_size(video)
    cropped = 0.0
    if src:
        sw, sh = src
        visible = (
            min(1.0, (w / h) / (sw / sh)) if (sw / sh) > (w / h) else min(1.0, (sw / sh) / (w / h))
        )
        cropped = round(1.0 - visible, 4)
    # чётные стороны: yuv420p хранит цвет на пары пикселей, нечётный кроп
    # ffmpeg округлит сам, но молча и не всегда в ту же сторону по осям
    zoom_crop = (
        f"crop=w='2*floor(iw/{zoom:.6f}/2)':h='2*floor(ih/{zoom:.6f}/2)'," if zoom > 1.0 else ""
    )
    vchain = (
        f"[0:v]{zoom_crop}scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,fps={fps_expr(fps)},"
        f"tpad=stop_mode=clone:stop_duration={max(0.0, target - vdur) + 0.05:.3f}[v]"
    )
    fade_out_at = max(0.0, target - EDGE_FADE_SEC)
    if audio or own_audio:
        inputs = ["-i", str(video)] + (["-i", str(audio)] if audio else [])
        source = "[1:a]" if audio else "[0:a]"
        # край файла гасится ДО сдвига: после `adelay` в начале уже тишина, и
        # fade по ней не спас бы первый сэмпл озвучки от щелчка
        ms = int(round(delay * 1000))
        shift = f"afade=t=in:d={EDGE_FADE_SEC},adelay={ms}:all=1," if ms > 0 else ""
        edge_in = "" if ms > 0 else f"afade=t=in:d={EDGE_FADE_SEC},"
        achain = (
            f"{source}aresample=44100,{shift}apad=pad_dur={max(0.0, target - adur) + 0.05:.3f},"
            f"{edge_in}afade=t=out:st={fade_out_at:.3f}:d={EDGE_FADE_SEC}[a]"
        )
    else:
        inputs = ["-i", str(video), "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        achain = "[1:a]anull[a]"
    run(
        [
            *inputs,
            "-filter_complex", f"{vchain};{achain}",
            "-map", "[v]", "-map", "[a]", "-t", f"{target:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            str(out),
        ]
    )  # fmt: skip
    return MuxedScene(
        duration_sec=target, frozen_sec=max(0.0, target - vdur), cropped_share=cropped
    )


def band_luminance(
    video: Path, top: float, bottom: float, *, width: float = 1.0, fps: float = 2.0
) -> list[tuple[float, float]]:
    """(секунда, средняя яркость 0..255) полосы кадра между `top` и `bottom`
    в долях высоты.

    Нужно проверке читаемости субтитров: контраст текста зависит от того, что под
    ним. Vision-модель это видит, но её вердикт плывёт между прогонами — на одном
    и том же ролике «плохо читается» появлялось и исчезало. Яркость фона измерима,
    значит должна измеряться, а не оцениваться."""
    lo = max(0.0, min(top, bottom))
    height = max(0.01, min(1.0, max(top, bottom)) - lo)
    across = max(0.05, min(1.0, width))  # доля ширины по центру: где вообще бывает текст
    proc = subprocess.run(
        [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(video),
            "-vf", f"fps={fps:g},"
                   f"crop=w=iw*{across:.4f}:h=ih*{height:.4f}:x=(iw-ow)/2:y=ih*{lo:.4f},"
                   "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
            "-an", "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
    )  # fmt: skip
    if proc.returncode != 0:
        raise FfmpegError(
            f"замер яркости полосы не удался ({video}):\n{proc.stderr.strip()[-400:]}"
        )
    return [(float(t), float(v)) for t, v in _YAVG.findall(proc.stdout)]


def crop_x_expr(offsets: list[tuple[float, int]]) -> str:
    """Кусочно-постоянное положение окна кадра: `if(lt(t,2.9),260,…)`.

    Одно окно — просто число. Несколько — цепочка `if` по времени: `crop` умеет
    считать x на каждом кадре (`eval=frame`), и это дешевле, чем резать клип на
    планы, кропать по отдельности и склеивать обратно."""
    if not offsets:
        return "(iw-ow)/2"
    ordered = sorted(offsets)
    expr = str(ordered[-1][1])
    for start, x in reversed(ordered[:-1]):
        nxt = next(s for s, _ in ordered if s > start)
        expr = f"if(lt(t,{nxt:.3f}),{x},{expr})"
    return expr


def reframe_video(
    src: Path,
    out: Path,
    *,
    crop: tuple[int, int],
    offsets: list[tuple[float, int]],
    size: tuple[int, int],
    fps: float = FPS,
    y: int | None = None,
) -> None:
    """Кроп окном `crop` (положение — по времени) с приведением к `size`.

    `y` — верхний край окна; None — по центру высоты. Число нужно только окну
    меньше полной высоты (наезд `reframe.punch`): полное окно по вертикали
    ставить некуда.

    Звук копируется как есть: кадрирование трогает только картинку, а у нарезки
    в этой дорожке вся речь."""
    cw, ch = crop
    w, h = size
    x = crop_x_expr(offsets)
    y_expr = "(ih-oh)/2" if y is None else str(y)
    per_frame = ":eval=frame" if "eval" in filter_options("crop") else ""
    run(
        [
            "-i", str(src),
            "-filter_complex",
            f"[0:v]crop=w={cw}:h={ch}:x='{x}':y={y_expr}{per_frame},"
            f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"setsar=1,fps={fps_expr(fps)}[v]",
            "-map", "[v]", "-map", "0:a:0?", "-c:a", "copy",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(out),
        ]
    )  # fmt: skip


# Раскладка «кадр целиком на поле» (`ReframeWindow.layout: fit`): где по высоте
# выхода стоит центр кадра. Чуть выше середины: над кадром остаётся место под
# подпись (`Storyboard.headline`), под ним — под субтитры, и всё это выше нижней
# зоны интерфейса площадок (15–17% высоты). У референсного канала кадр 16:9 стоит на 37–73%
# высоты под подписью (разбор референсных каналов нарезок, 17.09.2026); при 0.45 кадр
# 16:9 занимает 29–61%, квадрат — 17–73%, субтитры под ним влезают в оба
FIT_CENTER_SHARE = 0.45
# размытое поле: во сколько раз уменьшить кадр перед размытием (дёшево и мягко)
# и насколько притемнить — резкий кадр должен читаться поверх своей копии
FIT_BLUR_SCALE = 4
FIT_BLUR_DARKEN = -0.12


class FitRect(NamedTuple):
    """Раскладка `fit` в пикселях: кусок исходника и где он лежит на выходе."""

    crop_x: int
    crop_w: int
    crop_h: int
    x: int  # левый верхний угол кадра на выходе
    y: int
    width: int  # размер кадра на выходе
    height: int
    crop_y: int = 0  # верх куска в исходнике: не ноль только при `zoom`


def fit_rect(
    source: tuple[int, int],
    size: tuple[int, int],
    *,
    punch: float = 1.0,
    x_share: float | None = None,
    zoom: float = 1.0,
    y_share: float | None = None,
    center_share: float = FIT_CENTER_SHARE,
) -> FitRect:
    """Кусок исходника и его место на выходе для раскладки `fit`.

    `punch` сужает кусок по ширине при полной высоте исходника: 1.0 — весь кадр,
    16/9 — квадрат из середины (или вокруг `x_share`). `zoom` уменьшает кусок с
    обеих сторон, сохраняя пропорцию, вокруг (`x_share`, `y_share`): так из кадра
    уходит полоса у края вместе с вшитым логотипом канала-источника, и кадр
    выходит крупнее. Кусок растягивается на ширину выхода; выше выхода не
    бывает — тогда он вписывается по высоте. Все стороны чётные: yuv420p не
    режется по нечётной границе."""
    sw, sh = source
    ow, oh = size
    punch = max(1.0, punch)
    zoom = max(1.0, zoom)
    crop_w = max(2, int(sw / punch / zoom) // 2 * 2)
    crop_h = max(2, int(sh / zoom) // 2 * 2)
    width = ow // 2 * 2
    height = int(round(width * crop_h / crop_w)) // 2 * 2
    if height > oh:
        height = oh // 2 * 2
        width = int(round(height * crop_w / crop_h)) // 2 * 2
    center = sw / 2 if x_share is None else x_share * sw
    crop_x = max(0, min(sw - crop_w, int(round(center - crop_w / 2)))) // 2 * 2
    middle = sh / 2 if y_share is None else y_share * sh
    crop_y = max(0, min(sh - crop_h, int(round(middle - crop_h / 2)))) // 2 * 2
    x = (ow - width) // 2 // 2 * 2
    y = max(0, min(oh - height, int(round(oh * center_share - height / 2)))) // 2 * 2
    return FitRect(crop_x, crop_w, crop_h, x, y, width, height, crop_y)


def fit_video(
    src: Path,
    out: Path,
    *,
    rect: FitRect,
    size: tuple[int, int],
    fill: str = "black",
    fps: float = FPS,
) -> None:
    """Кадр исходника целиком (или кусок `rect`) на поле размера `size`.

    `fill`: `black` — чёрное поле (`pad`), `blur` — размытая и притемнённая
    копия того же кадра во весь выход, резкий кадр поверх. Звук копируется,
    как у `reframe_video`: у нарезки в нём вся речь."""
    ow, oh = size
    fg = (
        f"crop=w={rect.crop_w}:h={rect.crop_h}:x={rect.crop_x}:y={rect.crop_y},"
        f"scale={rect.width}:{rect.height},setsar=1"
    )
    if fill == "blur":
        bw, bh = max(2, ow // FIT_BLUR_SCALE // 2 * 2), max(2, oh // FIT_BLUR_SCALE // 2 * 2)
        graph = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=10:2,scale={ow}:{oh},eq=brightness={FIT_BLUR_DARKEN}[back];"
            f"[fg]{fg}[front];"
            f"[back][front]overlay=x={rect.x}:y={rect.y},setsar=1,fps={fps_expr(fps)}[v]"
        )
    else:
        graph = f"[0:v]{fg},pad={ow}:{oh}:{rect.x}:{rect.y}:color=black,fps={fps_expr(fps)}[v]"
    run(
        [
            "-i", str(src),
            "-filter_complex", graph,
            "-map", "[v]", "-map", "0:a:0?", "-c:a", "copy",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(out),
        ]
    )  # fmt: skip


class CropTrack(NamedTuple):
    """Окно одной дорожки стека: размер и положение по времени.

    `offsets` — (секунда, x, y) верхнего левого угла; между отметками окно
    стоит, как у `crop_x_expr`. Размер окна на всю сцену один: `crop` считает
    ширину и высоту при настройке графа, а не на каждом кадре, — по времени
    двигается только положение."""

    width: int
    height: int
    offsets: list[tuple[float, int, int]]


def _half_chain(label: str, track: CropTrack, *, size: tuple[int, int], per_frame: str) -> str:
    w, half = size
    x = crop_x_expr([(t, x) for t, x, _ in track.offsets])
    y = crop_x_expr([(t, y) for t, _, y in track.offsets])
    return (
        f"[{label}]crop=w={track.width}:h={track.height}:x='{x}':y='{y}'{per_frame},"
        f"scale={w}:{half}:force_original_aspect_ratio=increase,crop={w}:{half},setsar=1"
    )


def stack_crops(
    src: Path,
    out: Path,
    *,
    top: CropTrack,
    bottom: CropTrack,
    size: tuple[int, int],
    fps: float = FPS,
    single: CropTrack | None = None,
    stacked: list[tuple[float, float]] | None = None,
    split: int | None = None,
) -> None:
    """Два окна исходника друг над другом: верхнее и нижнее — каждое в половину
    высоты `size` (9:16 → две половины 9:8), `vstack`; `split` — высота
    верхнего в пикселях выхода, если не пополам (facecam: игра 1312, вебка
    608). Раскладка «двое друг над другом» и «игра сверху, вебка снизу» (14.09.2026,
    `features/reframe_two_up_punch_in`, `reframe_facecam_screen`).

    `single` со `stacked` — смешанная сцена: в планах вне `stacked` (секунды
    от–до) идёт обычное одно окно `single`, стек только в своих планах. Это
    один граф: три ветки кропа из `split`, стек ложится поверх одиночного окна
    `overlay` с `enable` по времени — дешевле, чем резать сцену на планы,
    рендерить по-разному и склеивать (склейка перекодирует ещё раз, и на
    каждом стыке появился бы шанс на кадровый зазор, см. `concat`). Цена —
    три кропа с масштабом вместо одного; на 4K-отрезке это секунды, не минуты.

    Звук — как у `reframe_video`: копируется, у нарезки в нём вся речь."""
    w, h = size
    upper = split if split else h // 2
    per_frame = ":eval=frame" if "eval" in filter_options("crop") else ""
    parts = [
        _half_chain("t", top, size=(w, upper), per_frame=per_frame) + "[top]",
        _half_chain("b", bottom, size=(w, h - upper), per_frame=per_frame) + "[bot]",
    ]
    if single is None or not stacked:
        graph = (
            f"[0:v]split=2[t][b];{';'.join(parts)};[top][bot]vstack=inputs=2,fps={fps_expr(fps)}[v]"
        )
    else:
        x = crop_x_expr([(t, x) for t, x, _ in single.offsets])
        y = crop_x_expr([(t, y) for t, _, y in single.offsets])
        one = (
            f"[s]crop=w={single.width}:h={single.height}:x='{x}':y='{y}'{per_frame},"
            f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[one]"
        )
        enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in stacked)
        graph = (
            f"[0:v]split=3[s][t][b];{one};{';'.join(parts)};[top][bot]vstack=inputs=2[stack];"
            f"[one][stack]overlay=x=0:y=0:enable='{enable}',fps={fps_expr(fps)}[v]"
        )
    run(
        [
            "-i", str(src),
            "-filter_complex", graph,
            "-map", "[v]", "-map", "0:a:0?", "-c:a", "copy",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(out),
        ]
    )  # fmt: skip


def merge_tracks(video: Path, audio: Path, out: Path) -> None:
    """Картинка одного файла + звук другого → один файл, без перекодирования.
    Дорожки, скачанные по отдельности, сшиваются здесь; резать их будет
    `cut_segment`, поэтому качество трогать незачем."""
    run(
        [
            "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest",
            str(out),
        ]
    )  # fmt: skip


def blur_graph(boxes: list[tuple[float, float, float, float]], size: tuple[int, int]) -> str:
    """Граф размытия прямоугольников кадра (доли кадра x, y, w, h) → метка [v].

    Каждый прямоугольник вырезается, размывается и кладётся на место. Радиус —
    от меньшей стороны прямоугольника: у boxblur он обязан влезть в плоскость
    цвета, а она вдвое меньше яркости, иначе фильтр не встаёт."""
    width, height = size
    chain = f"[0:v:0]split={len(boxes) + 1}[base]" + "".join(f"[b{i}]" for i in range(len(boxes)))
    parts = [chain]
    current = "base"
    for i, (x, y, w, h) in enumerate(boxes):
        px, py = int(x * width) // 2 * 2, int(y * height) // 2 * 2
        pw = max(4, min(int(w * width), width - px) // 2 * 2)
        ph = max(4, min(int(h * height), height - py) // 2 * 2)
        luma = max(1, min(pw, ph) // 4)
        chroma = max(1, min(pw, ph) // 8)
        parts.append(f"[b{i}]crop={pw}:{ph}:{px}:{py},boxblur={luma}:3:{chroma}:3[k{i}]")
        parts.append(f"[{current}][k{i}]overlay={px}:{py}[t{i}]")
        current = f"t{i}"
    parts.append(f"[{current}]null[v]")
    return ";".join(parts)


def cut_segment(
    src: Path,
    out: Path,
    *,
    start_sec: float,
    duration_sec: float,
    fps: float = FPS,
    blur: list[tuple[float, float, float, float]] | None = None,
) -> float:
    """Точный отрезок исходника — со звуком, если он в исходнике есть.

    `-ss` перед `-i` — быстрый поиск по кейфреймам, но при перекодировании
    ffmpeg доводит его до кадра, поэтому граница точная. `-t` на выходе, а не
    `-to` на входе: `-to` рядом с `-ss` считается от начала файла, и на
    двухчасовом подкасте эта разница — часы. Возвращает измеренную длину: сколько
    получилось на самом деле, а не сколько просили.

    `fps` — стадия cut передаёт частоту самого исходника (`probe_fps`): `-r`
    здесь только приводит запись к постоянной частоте, а не пересчитывает её.
    С константой 24 отрезок 25-кадрового подкаста терял кадр в секунду ещё до
    кадрирования и сборки (h13: 1055 кадров вместо 1099 на 43.96 с).

    `blur` — прямоугольники кадра, размытые тем же проходом (`blur_graph`):
    вывеска ставок в комнате стримера не должна дожить ни до одной стадии."""
    size = probe_size(src) if blur else None
    video = ["-map", "0:v:0"]
    if blur and size:
        video = ["-filter_complex", blur_graph(blur, size), "-map", "[v]"]
    run(
        [
            "-ss", f"{max(0.0, start_sec):.3f}", "-i", str(src), "-t", f"{duration_sec:.3f}",
            *video, "-map", "0:a:0?",  # звука может и не быть — не повод падать
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", fps_expr(fps),
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            str(out),
        ]
    )  # fmt: skip
    return probe_duration(out)


def concat(clips: list[Path], out: Path) -> None:
    """Склейка сцен. Перекодируем, чтобы не зависеть от совпадения параметров."""
    list_file = out.with_suffix(".txt")
    lines = [f"file '{p.resolve().as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) * 2)}'"
             for p in clips]  # fmt: skip
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(
        [
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            str(out),
        ]
    )  # fmt: skip


def scene_thumbs(
    clip: Path, out_dir: Path, stem: str, *, n: int = 3, width: int = 320
) -> list[Path]:
    """n кадров собранной сцены для монтажного листа — увидеть, что монтаж сделал,
    не пересматривая ролик."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(clip)
    paths: list[Path] = []
    for i in range(n):
        t = duration * (i + 0.5) / n
        p = out_dir / f"{stem}_{i + 1}.jpg"
        run(["-ss", f"{t:.3f}", "-i", str(clip), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-q:v", "4", str(p)])  # fmt: skip
        paths.append(p)
    return paths


# ------------------------------------------------------------ эффекты кадра


@dataclass(frozen=True)
class Effect:
    """Эффект на секунде готового ролика: что, когда, как долго и как сильно."""

    kind: str  # zoom_punch | shake | flash | desaturate | vignette | glitch
    at: float
    duration: float = 0.4
    strength: float = 1.0


# Силы «при strength=1» подобраны так, чтобы эффект был заметен на телефоне и не
# выглядел аварией: наезд на 18% — это удар, а не полёт; тряска ±20 px на
# вертикали 1080 — толчок, а не землетрясение.
ZOOM_PUNCH = 0.18  # прибавка к масштабу в момент удара
SHAKE_INSET = 0.08  # запас по краям на время тряски: внутри него окно и ездит
SHAKE_PX = 20.0  # амплитуда тряски по короткой стороне 1080
SHAKE_HZ = (13.0, 9.0)  # разные частоты по осям — иначе тряска идёт по диагонали
FLASH_AMP = 0.55  # прибавка к яркости в момент вспышки (eq brightness, −1..1)
DESATURATE_AMP = 0.85  # насколько уходит цвет при strength=1
VIGNETTE_ANGLE = 0.8  # радианы: 0 — нет виньетки
GLITCH_SHIFT_PX = 14  # сдвиг красного и синего каналов
STRENGTH_MAX = 2.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _gate(at: float, duration: float, var: str = "t") -> str:
    return f"between({var},{at:.3f},{at + max(duration, 0.05):.3f})"


def _decay(at: float, duration: float, var: str = "t") -> str:
    """Окно «мгновенный рывок → спад к нулю»: 1 в начале, 0 в конце."""
    d = max(duration, 0.05)
    return f"max(0,1-({var}-{at:.3f})/{d:.3f})*{_gate(at, duration, var)}"


def _windows(effects: list[Effect]) -> str:
    """Окна нескольких эффектов одним выражением для `enable`.

    `eq` и `vignette` считают на каждый кадр, и `vignette` — по каждому пикселю:
    на вертикали 1080×1920 это дороже всего остального вместе взятого (замерено:
    финальный проход эпичного шортса шёл 2 мин 27 с). Вне своих окон фильтр
    должен просто пропускать кадр, а не пересчитывать нейтральное значение."""
    return "+".join(_gate(e.at, e.duration) for e in effects)


RAMP_IN_SEC = 0.2  # за столько эффект-состояние набирает полную силу
RAMP_OUT_SEC = 0.35  # и за столько отпускает


def _ramp(at: float, duration: float, var: str = "t") -> str:
    """Окно-трапеция: быстро вошло, держится, отпустило.

    Состояния кадра (обесцвечивание, виньетка) включаются на длинном куске, и
    щелчок на входе-выходе виден как брак склейки — в отличие от удара, где
    резкость и есть смысл."""
    end = at + max(duration, 0.1)
    rise = min(RAMP_IN_SEC, max(duration, 0.1) / 3)
    fall = min(RAMP_OUT_SEC, max(duration, 0.1) / 3)
    shape = f"min(min(({var}-{at:.3f})/{rise:.3f},({end:.3f}-{var})/{fall:.3f}),1)"
    return f"max(0,{shape})*{_gate(at, duration, var)}"


def effect_filters(effects: list[Effect], *, size: tuple[int, int], fps: float = FPS) -> list[str]:
    """Эффекты → цепочка фильтров ffmpeg. Пусто на входе — пусто на выходе.

    Геометрия (наезд и тряска) идёт одним `zoompan`: он умеет и масштаб, и
    положение окна, а два фильтра подряд пересэмплировали бы кадр дважды.
    Цвет (вспышка и обесцвечивание) — одним `eq` по той же причине. Тряске
    нужен запас по краям (`SHAKE_INSET`), иначе окно упрётся в край кадра и
    толчок срежется; вне своего окна эффект не добавляет ничего, и там
    `zoompan` работает один к одному.

    Перед `zoompan` обязателен `fps=…:start_time=0`. `zoompan` не читает метки
    времени входа — он считает кадры и кладёт их на свою сетку подряд с нуля,
    поэтому любая дыра в метках исчезает, и картинка становится короче ровно
    на сумму дыр. А склейка сцен дыры даёт: у сцены картинка — целое число
    кадров, звук на доли кадра длиннее (s02 живого ep001: 115 кадров = 4.7917 с
    при звуке 4.8065), concat-демуксер ставит следующую сцену на длительность
    контейнера, и ffmpeg, накопив больше 0.6 кадра сдвига, оставляет в дорожке
    дыру в целый кадр; плюс первый кадр склейки стоит на 0.041 с, а не на нуле.
    Замерено 14.09.2026 на 105 сценах: 12161 кадр на 506.958 с — пять дыр по
    два кадра и одна на старте; после `zoompan` 12161/24 = 506.708 с, av_drift
    +0.267 при пороге 0.1, тогда как без эффектов тот же файл давал +0.034.
    `fps` заполняет дыры клонами по меткам, `start_time=0` велит начать сетку
    с нуля (без него сетка начинается с первого кадра, и стартовая дыра
    остаётся: 293 кадра вместо 294 на синтетике из 12 сцен), счёт кадров
    сходится с длительностью, и заодно `it` внутри `zoompan` перестаёт
    отставать от звука на сумму дыр — до этого удар на 304-й секунде стоял на
    четверть секунды раньше своего слова. Заменить `zoompan` на `scale`+`crop`
    по `t` не стали: это оставило бы файл VFR как есть, а дыры — в нём.

    Времена — по таймлайну готового файла; эффекты применяются ДО субтитров,
    поэтому текст не трясётся и не мылится вместе с картинкой."""
    if not effects:
        return []
    w, h = size
    short = min(w, h)
    chain: list[str] = []

    zooms = [e for e in effects if e.kind == "zoom_punch"]
    shakes = [e for e in effects if e.kind == "shake"]
    if zooms or shakes:
        chain.append(f"fps={fps_expr(fps)}:start_time=0")
        z = "1"
        for e in zooms:
            amp = ZOOM_PUNCH * _clamp(e.strength, 0.0, STRENGTH_MAX)
            z += f"+{amp:.4f}*{_decay(e.at, e.duration, 'it')}"
        for e in shakes:
            z += f"+{SHAKE_INSET:.4f}*{_gate(e.at, e.duration, 'it')}"
        # запас на тряску есть только внутри её окна, поэтому и амплитуда
        # ограничена им же: половина того, что даёт SHAKE_INSET
        room = short * SHAKE_INSET / 2 * 0.9
        dx, dy = "0", "0"
        for e in shakes:
            amp = min(room, SHAKE_PX * short / 1080 * _clamp(e.strength, 0.0, STRENGTH_MAX))
            fx, fy = SHAKE_HZ
            dx += f"+{amp:.1f}*sin(2*PI*{fx:g}*it)*{_gate(e.at, e.duration, 'it')}"
            dy += f"+{amp:.1f}*sin(2*PI*{fy:g}*it+1.7)*{_gate(e.at, e.duration, 'it')}"
        x = f"iw/2-(iw/zoom/2)+({dx})"
        y = f"ih/2-(ih/zoom/2)+({dy})"
        chain.append(f"zoompan=z='{z}':d=1:x='{x}':y='{y}':s={w}x{h}:fps={fps_expr(fps)}")

    flashes = [e for e in effects if e.kind == "flash"]
    desat = [e for e in effects if e.kind == "desaturate"]
    if flashes or desat:
        parts: list[str] = []
        if flashes:
            expr = "+".join(
                f"{FLASH_AMP * _clamp(e.strength, 0.0, STRENGTH_MAX):.4f}"
                f"*{_decay(e.at, e.duration)}"
                for e in flashes
            )
            parts.append(f"brightness='{expr}'")
        if desat:
            expr = "-".join(
                [
                    "1",
                    *(
                        f"{DESATURATE_AMP * _clamp(e.strength, 0.0, STRENGTH_MAX):.4f}"
                        f"*{_ramp(e.at, e.duration)}"
                        for e in desat
                    ),
                ]
            )
            parts.append(f"saturation='max(0,{expr})'")
        chain.append("eq=" + ":".join(parts) + f":eval=frame:enable='{_windows(flashes + desat)}'")

    vignettes = [e for e in effects if e.kind == "vignette"]
    if vignettes:
        expr = "+".join(
            f"{VIGNETTE_ANGLE * _clamp(e.strength, 0.0, STRENGTH_MAX):.4f}"
            f"*{_ramp(e.at, e.duration)}"
            for e in vignettes
        )
        chain.append(f"vignette=a='min(1.4,{expr})':eval=frame:enable='{_windows(vignettes)}'")

    for e in (x for x in effects if x.kind == "glitch"):
        shift = int(round(GLITCH_SHIFT_PX * _clamp(e.strength, 0.0, STRENGTH_MAX)))
        chain.append(f"rgbashift=rh=-{shift}:bh={shift}:enable='{_gate(e.at, e.duration)}'")
    return chain


@dataclass(frozen=True)
class SlowTail:
    """Замедленный хвост ролика: последние `take_sec` секунд в `speed` раз медленнее."""

    take_sec: float
    speed: float = 0.5
    pitch_drop: bool = True  # голос падает вместе со скоростью (замедленная плёнка)
    zoom: float = 0.0  # наезд за время хвоста: 0.15 → +15% к его концу
    desaturate: float = 0.0  # 0..1: насколько уходит цвет к концу хвоста
    vignette: bool = False


def slow_tail(video: Path, out: Path, tail: SlowTail, *, fps: float = FPS) -> float:
    """Замедлить конец ролика. Возвращает новую длительность.

    Голова остаётся кадр в кадр, поэтому всё, что было до `take_sec` с конца, не
    двигается — субтитры и замеры до этой точки остаются верными.

    Порядок фильтров в хвосте важен: `zoompan` навязывает свою частоту кадров и
    переписывает временные метки, поэтому растяжение (`setpts`) идёт ПОСЛЕ него.
    Наоборот — и хвост вернётся к исходной длине (проверено: картинка 24.5 с при
    звуке 29 с). Обе дорожки в конце принудительно доводятся до одной длины:
    исходное расхождение дорожек замедление умножает."""
    total_v = probe_stream_duration(video, "0:v:0") or probe_duration(video)
    speed = _clamp(tail.speed, 0.1, 1.0)
    take = _clamp(tail.take_sec, 0.2, total_v)
    cut = max(0.0, total_v - take)
    exact = cut + (total_v - cut) / speed

    vt = [f"trim={cut:.3f},setpts=PTS-STARTPTS"]
    if tail.zoom > 0:
        w, h = probe_size(video) or SIZES["9:16"]
        z = f"min({1 + tail.zoom:.4f},1+{tail.zoom:.4f}*it/{take:.3f})"
        vt.append(
            f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={w}x{h}:fps={fps_expr(fps)}"
        )
    if tail.desaturate > 0:
        amount = _clamp(tail.desaturate, 0.0, 1.0)
        vt.append(f"eq=saturation='max(0,1-{amount:.3f}*min(1,t/{take:.3f}))':eval=frame")
    if tail.vignette:
        vt.append(f"vignette=a='{VIGNETTE_ANGLE:.3f}*min(1,t/{take / 2:.3f})':eval=frame")
    # растянуть метки времени и тут же вернуть постоянную частоту кадров: без
    # `fps` хвост остаётся с исходным числом кадров на вдвое большем интервале,
    # то есть файл становится VFR. Любой следующий фильтр, который переставляет
    # метки (`zoompan` в эффектах), пересчитает такой хвост обратно — картинка
    # окажется на длину замедления короче звука (поймано замером av_drift:
    # +2.354 с при хвосте 2.3 с)
    vt.append(f"setpts=PTS/{speed:.4f},fps={fps_expr(fps)}")

    if tail.pitch_drop:
        # asetrate — это замедленная плёнка: вместе со скоростью падает высота
        at = f"asetrate=44100*{speed:.4f},aresample=44100"
    else:
        at = f"atempo={speed:.4f}"
    graph = (
        f"[0:v]trim=0:{cut:.3f},setpts=PTS-STARTPTS[vh];"
        f"[0:v]{','.join(vt)}[vt];"
        f"[0:a]atrim=0:{cut:.3f},asetpts=PTS-STARTPTS[ah];"
        f"[0:a]atrim={cut:.3f},asetpts=PTS-STARTPTS,{at}[at];"
        f"[vh][ah][vt][at]concat=n=2:v=1:a=1[vc][ac];"
        f"[vc]tpad=stop_mode=clone:stop_duration=0.3[v];"
        f"[ac]apad=pad_dur=0.3[a]"
    )
    run([
        "-i", str(video), "-filter_complex", graph,
        "-map", "[v]", "-map", "[a]", "-t", f"{exact:.3f}", *_ENC, str(out),
    ])  # fmt: skip
    return probe_duration(out)


# ------------------------------------------------------------ финальный проход


_EBU_I = re.compile(r"I:\s+(-?[\d.]+|-inf)\s+LUFS")
_EBU_TP = re.compile(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS")


# ebur128 не пишет -inf для цифровой тишины — он упирается в пол −70 LUFS. Число
# на полу это не уровень, а «звука нет»: приняв его за уровень голоса, микс
# опустил музыку на 60 дБ (поймано тестом на заглушках, где озвучка — тишина)
SILENCE_FLOOR_LUFS = -69.0


def _finite(text: str) -> float | None:
    value = float(text)
    return value if math.isfinite(value) and value > SILENCE_FLOOR_LUFS else None


def measure_loudness(path: Path, stream: str = "0:a:0") -> tuple[float | None, float | None]:
    """Интегральная громкость (LUFS) и истинный пик (dBTP) по EBU R128.
    None — дорожка беззвучна (пол −70 LUFS) или не измерилась."""
    err = _run_capture(["-i", str(path), "-map", stream, "-af", "ebur128=peak=true",
                        "-f", "null", "-"])  # fmt: skip
    lufs = _EBU_I.findall(err)
    peak = _EBU_TP.findall(err)
    return (_finite(lufs[-1]) if lufs else None, _finite(peak[-1]) if peak else None)


# строка журнала ebur128 каждые 100 мс: «t: 1.2  TARGET:-23 LUFS  M: -20.3 S: …»
_EBU_M = re.compile(r"t:\s*([\d.]+)\s+TARGET:[^\n]*?M:\s*(-?[\d.]+|-inf)")
EBU_MOMENTARY_SEC = 0.4  # окно моментальной громкости: раньше него замер не полон


def momentary_loudness(path: Path, stream: str = "0:a:0") -> list[tuple[float, float]]:
    """Моментальная громкость (окно 400 мс) по времени: (секунда, LUFS) каждые
    100 мс, начиная с первого полного окна. Тишина (пол −70) пропускается.

    Это то, чем меряется РОВНОСТЬ дорожки: интегральная громкость у пада с
    провалами и у ровного пада одна, а разброс моментальной — 20 дБ против
    0.4 (замер подложек 14.09.2026, `core/audio_synth.py`)."""
    err = _run_capture(["-i", str(path), "-map", stream, "-af", "ebur128", "-f", "null", "-"])
    out: list[tuple[float, float]] = []
    for t, m in _EBU_M.findall(err):
        value = _finite(m) if m != "-inf" else None
        if value is not None and float(t) >= EBU_MOMENTARY_SEC:
            out.append((float(t), value))
    return out


_JSON_BLOCK = re.compile(r"\{[^{}]*\}", re.S)


def _loudnorm_stats(
    path: Path, *, target_i: float, target_tp: float, lra: float
) -> dict[str, float] | None:
    """Первый проход loudnorm: измерения, по которым второй проход нормализует
    линейно (без накачки динамики). None — тишина или не измерилось."""
    err = _run_capture(["-i", str(path), "-af",
                        f"loudnorm=I={target_i}:TP={target_tp}:LRA={lra}:print_format=json",
                        "-f", "null", "-"])  # fmt: skip
    blocks = _JSON_BLOCK.findall(err)
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
        stats = {
            k: float(data[k])
            for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        }
    except (ValueError, KeyError, TypeError):
        return None
    return stats if all(math.isfinite(v) for v in stats.values()) else None


@dataclass(frozen=True)
class MusicMix:
    path: Path
    # уровень трека ОТНОСИТЕЛЬНО голоса до даккинга: −12 → на 12 дБ тише речи.
    # Трек сначала приводится к уровню голоса, потом опускается — так один и тот
    # же gain даёт один и тот же баланс независимо от того, как смастерён файл
    gain_db: float = -12.0
    duck_db: float = 10.0  # насколько глубже под речью
    fade_in_sec: float = 1.0
    fade_out_sec: float = 2.0
    loop: bool = True
    # ступени громкости по плану монтажа: (секунда финала, дБ относительно gain_db).
    # Между ступенями — линейный подъезд за GAIN_RAMP_SEC, а не скачок
    gain_steps: tuple[tuple[float, float], ...] = ()
    # с какой секунды ролика играет трек; отрицательное — пропустить столько
    # секунд трека. Так дроп трека сажают на кульминацию монтажа
    start_sec: float = 0.0


@dataclass(frozen=True)
class BedMix:
    """Подложка: ровный эмбиенс под всей дорожкой, отдельный от музыки.

    Зачем второй слой, когда есть музыка. Трек с развитием начинается тихо, и
    его вступление — это и есть тишина: у devastation_and_revenge первые 10 с
    на 23 дБ ниже интеграла трека (первые 20 с — на 17.6), то есть при
    `gain_db: -16` они лежат на 39 дБ под голосом, ниже детектора тишины; на
    ep002 уровень секунды между фразами падал до −44…−48 дБ, а поднятие трека с
    −20 до −16 дало 2–3 дБ. Подложка не зависит от трека: она зациклена на всю
    длину и держит один уровень, музыка вступает поверх неё к кульминации.

    `gain_db` — относительно голоса, как у музыки (−28 по умолчанию,
    `AudioConfig.bed_gain_db`): выше вступления трека и детектора тишины, на
    21 дБ ниже пика дропа (дроп остаётся событием), а под речью прибавляет к
    ней 10·lg(1 + 10^(−2.8)) = 0.007 дБ — её там не слышно. Поэтому даккинга у
    подложки нет: под речью ему нечего убирать, а в паузах он бы качал уровень
    вслед за словами (attack 15 мс / release 400 мс) — то самое «дыхание»,
    ради устранения которого слой и заведён."""

    path: Path
    gain_db: float = -28.0
    fade_in_sec: float = 0.5  # чтобы первое окно ролика не начиналось со ступеньки
    fade_out_sec: float = 0.8  # вместе с финалом (fade_out_sec проекта или плана)


class FinalMix(NamedTuple):
    duration_sec: float
    loudness_lufs: float | None
    true_peak_dbtp: float | None
    voice_lufs: float | None
    music_lufs: float | None  # после гейна, до даккинга
    bed_lufs: float | None = None  # подложка после гейна


# порог даккинга ставится на столько дБ ниже среднего уровня голоса: между словами
# в дорожке озвучки цифровая тишина, шума под порогом нет, а речь любой громкости
# над ним. Глубина при среднем голосе ≈ duck_db, на пиках чуть больше
DUCK_RANGE_DB = 30.0
DEFAULT_VOICE_LUFS = -20.0
GAIN_RAMP_SEC = 1.0
# Битрейт звука готового файла. 256k, а не 160k, потому что кодировщик AAC на
# скупом битрейте ПОДНИМАЕТ пики: ролик с одной музыкой на дорожке
# (23.09.2026) выходил из фильтров чистым — −3.6 dBTP по float, — а из
# кодировщика +2.2 dBTP с обрезанными сэмплами. Замер на том же миксе: 160k
# +2.2, 192k +0.9, 224k −0.2, 256k −0.3, 320k −2.4; сэмплы перестают упираться
# в ноль на 224k. Ограничитель перед кодировщиком не помогает — перегруз родом
# из него самого. Цена: ~35 КБ/с против 21 КБ/с
FINAL_AUDIO_KBPS = "256k"


def gain_expr(steps: tuple[tuple[float, float], ...], *, ramp: float = GAIN_RAMP_SEC) -> str:
    """Множитель громкости как выражение от t: 1 плюс сумма плавных ступеней.

    Каждая ступень — линейный подъезд от прежнего множителя к новому за `ramp`
    секунд: `clip((t-t0)/ramp,0,1)`. Сумма, а не вложенные if — выражение
    остаётся плоским при любом числе ступеней."""
    expr = "1"
    prev = 1.0
    for t, delta_db in sorted(steps):
        factor = 10 ** (delta_db / 20)
        expr += f"+({factor - prev:.5f})*clip((t-{t:.3f})/{ramp:.3f},0,1)"
        prev = factor
    return expr


# ------------------------------------------------------------ план монтажа: отрезки и переходы

_ENC = [
    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
]  # fmt: skip
# crossfade: следующая сцена начинается с EPS секунд хвоста предыдущей до начала
# растворения — чтобы xfade получил первый вход длиннее offset+duration и не
# оборвался на кадре округления. Общая длина: сумма коробок минус d
XFADE_EPS = 0.05


@dataclass(frozen=True)
class Cut:
    source: Path
    in_sec: float
    out_sec: float

    @property
    def seconds(self) -> float:
        return max(0.0, self.out_sec - self.in_sec)


def build_scene_video(
    cuts: list[Cut], out: Path, *, size: tuple[int, int], fps: float = FPS
) -> float:
    """Несколько отрезков (из одного или разных клипов) → один клип сцены без звука.
    Каждый отрезок приводится к формату эпизода кропом. Возвращает длительность."""
    w, h = size
    inputs: list[str] = []
    parts: list[str] = []
    for i, c in enumerate(cuts):
        inputs += ["-ss", f"{c.in_sec:.3f}", "-t", f"{c.seconds:.3f}", "-i", str(c.source)]
        parts.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"setsar=1,fps={fps_expr(fps)},setpts=PTS-STARTPTS[v{i}]"
        )
    labels = "".join(f"[v{i}]" for i in range(len(cuts)))
    total = sum(c.seconds for c in cuts)
    # точная длина: на границах отрезков seek и fps теряют по кадру (замерено:
    # 5.88 с вместо 6.0 на двух отрезках), поэтому хвост подкладывается клоном
    # последнего кадра и режется ровно по сумме длин
    graph = (
        ";".join(parts)
        + f";{labels}concat=n={len(cuts)}:v=1:a=0,tpad=stop_mode=clone:stop_duration=0.5[v]"
    )
    run([*inputs, "-filter_complex", graph, "-map", "[v]", "-t", f"{total:.3f}", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)])  # fmt: skip
    return probe_duration(out)


def fade_edges(clip: Path, out: Path, *, fade_in: float = 0.0, fade_out: float = 0.0) -> None:
    """Затемнение на краях клипа сцены (dip_black / fade_in). Длина не меняется,
    звук не трогается."""
    length = probe_duration(clip)
    vf: list[str] = []
    if fade_in > 0:
        vf.append(f"fade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        vf.append(f"fade=t=out:st={max(0.0, length - fade_out):.3f}:d={fade_out:.3f}")
    run(["-i", str(clip), "-vf", ",".join(vf) or "null", "-c:v", "libx264", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-c:a", "copy", str(out)])  # fmt: skip


def crossfade_pair(
    prev: Path,
    nxt: Path,
    duration: float,
    *,
    out_prev: Path,
    out_next: Path,
    fps: float = FPS,
) -> tuple[float, float]:
    """Растворение между двумя клипами сцен, не трогая измеренную речь.

    Предыдущая сцена укорачивается на d + EPS (это её хвост тишины — линт не
    пустит crossfade длиннее хвоста), следующая получает в начало этот хвост и
    растворяет его в себя: xfade со смещением EPS. Звук следующей сцены
    сдвигается на EPS, звук предыдущей просто обрезан (там тишина). Итог —
    сумма коробок минус d, и это единственное место, где план меняет длину.
    Возвращает новые длины (prev, next)."""
    prev_len = probe_duration(prev)
    next_len = probe_duration(nxt)
    tail = duration + XFADE_EPS
    keep = max(0.0, prev_len - tail)
    run(["-i", str(prev), "-t", f"{keep:.3f}", *_ENC, str(out_prev)])
    ms = int(round(XFADE_EPS * 1000))
    graph = (
        # хвост подложен клоном последнего кадра: xfade требует, чтобы первый вход
        # жил дольше offset+duration, а хвост после сдвига ровно такой длины.
        # fps обязателен: после seek и setpts частота помечена неизвестной, а
        # xfade работает только с постоянной
        f"[0:v]setpts=PTS-STARTPTS,fps={fps_expr(fps)},tpad=stop_mode=clone:stop_duration=0.3[a];"
        f"[1:v]setpts=PTS-STARTPTS,fps={fps_expr(fps)}[b];"
        f"[a][b]xfade=transition=fade:duration={duration:.3f}:offset={XFADE_EPS:.3f}[v];"
        f"[1:a]adelay={ms}|{ms}[au]"
    )
    run(["-ss", f"{keep:.3f}", "-i", str(prev), "-i", str(nxt),
         "-filter_complex", graph, "-map", "[v]", "-map", "[au]",
         "-t", f"{next_len + XFADE_EPS:.3f}", *_ENC, str(out_next)])  # fmt: skip
    return probe_duration(out_prev), probe_duration(out_next)


@dataclass(frozen=True)
class Sound:
    """Звуковой эффект на секунде готового ролика.

    `gain_db` — прямой гейн файла, а не уровень относительно голоса: библиотека
    выровнена по пику (`core/audio_synth.py`), поэтому «−6 дБ» означает одно и то же для
    любого эффекта из неё. Свой ненормированный файл надо либо выровнять, либо
    подобрать ему gain руками."""

    path: Path
    at: float
    gain_db: float = -6.0


def sfx_bus(sounds: list[Sound], total: float, out: Path) -> Path:
    """Звуковые эффекты → одна дорожка длиной `total` (стерео 44.1 кГц).

    Без `adelay` на входе у каждого: звуки раскладываются по полосам, где они
    не перекрываются, и полоса — склейка «тишина нужной длины → звук → тишина →
    звук» (`concat`), так что каждый звук встаёт в свою секунду по сумме длин,
    а не по задержке. Полос обычно одна-две — перекрываются только удар и
    тряска на одной склейке. Почему не `adelay`: ep003 (18.09.2026) — из восьми
    входов с задержками до 1051.8 с ffmpeg положил последний в начало ролика."""
    rate = 44100
    lanes: list[list[tuple[Sound, float]]] = []
    ends: list[float] = []
    for sound in sorted(sounds, key=lambda s: s.at):
        start = max(0.0, sound.at)
        if start >= total:
            continue
        length = min(probe_duration(sound.path), total - start)
        free = next((i for i, end in enumerate(ends) if end <= start + 1e-6), None)
        if free is None:
            lanes.append([])
            ends.append(0.0)
            free = len(lanes) - 1
        lanes[free].append((sound, length))
        ends[free] = start + length
    if not lanes:  # всё за краем ролика: дорожка тишины той же длины
        run(["-f", "lavfi", "-t", f"{total:.3f}", "-i", f"anullsrc=r={rate}:cl=stereo",
             "-c:a", "pcm_s16le", str(out)])  # fmt: skip
        return out
    inputs: list[str] = []
    parts: list[str] = []
    lane_outs: list[str] = []
    k = 0
    for li, lane in enumerate(lanes):
        cursor = 0.0
        pieces: list[str] = []
        for sound, length in lane:
            gap = max(0.0, sound.at) - cursor
            if gap > 1e-4:
                parts.append(
                    f"anullsrc=r={rate}:cl=stereo,atrim=duration={gap:.6f},"
                    f"asetpts=PTS-STARTPTS[g{li}_{k}]"
                )
                pieces.append(f"[g{li}_{k}]")
            inputs += ["-i", str(sound.path)]
            parts.append(
                f"[{k}:a]aformat=sample_rates={rate}:channel_layouts=stereo,"
                f"atrim=0:{length:.6f},asetpts=PTS-STARTPTS,volume={sound.gain_db:.2f}dB[s{k}]"
            )
            pieces.append(f"[s{k}]")
            cursor = max(0.0, sound.at) + length
            k += 1
        parts.append(
            f"{''.join(pieces)}concat=n={len(pieces)}:v=0:a=1,"
            f"apad=whole_dur={total:.3f},atrim=0:{total:.3f}[lane{li}]"
        )
        lane_outs.append(f"[lane{li}]")
    if len(lane_outs) > 1:
        parts.append(
            f"{''.join(lane_outs)}amix=inputs={len(lane_outs)}:duration=longest"
            ":dropout_transition=0:normalize=0[bus]"
        )
    else:
        parts.append(f"{lane_outs[0]}anull[bus]")
    run([*inputs, "-filter_complex", ";".join(parts), "-map", "[bus]",
         "-t", f"{total:.3f}", "-c:a", "pcm_s16le", str(out)])  # fmt: skip
    return out


def _mix_audio(
    video: Path,
    out: Path,
    *,
    music: MusicMix | None,
    sounds: list[Sound],
    total: float,
    voice_lufs: float | None,
    bed: BedMix | None = None,
) -> tuple[float | None, float | None]:
    """Голос + музыка под ним + подложка + эффекты → одна дорожка. Возвращает
    уровни музыки и подложки (после гейна, до даккинга).

    Порядок дорожек не обсуждается: VOICE > MUSIC > BED > SFX. Голос не
    трогается ничем; музыка получает гейн, ступени и даккинг голосом как
    ключом; подложка — гейн и края, без даккинга (см. `BedMix`); эффекты
    приходят одной дорожкой (`sfx_bus`) и просто подмешиваются — они короткие,
    и даккинг под них превратил бы речь в рванину."""
    ref = voice_lufs if voice_lufs is not None else DEFAULT_VOICE_LUFS
    inputs = ["-i", str(video)]
    voice = "[0:a]aformat=sample_rates=44100:channel_layouts=stereo"
    # ключ для даккинга — копия голоса; без музыки копия не нужна, а лишний
    # выход фильтра ffmpeg не прощает («unconnected output»)
    parts = [f"{voice},asplit=2[vo][key]" if music is not None else f"{voice}[vo]"]
    mixed = ["[vo]"]
    index = 1
    meter: Path | None = None
    bed_meter: Path | None = None
    if music is not None:
        threshold_db = max(-60.0, min(-6.0, ref - DUCK_RANGE_DB))
        ratio = DUCK_RANGE_DB / max(1.0, DUCK_RANGE_DB - music.duck_db)
        threshold = 10 ** (threshold_db / 20)
        track_lufs, _ = measure_loudness(music.path)
        volume_db = (ref + music.gain_db) - track_lufs if track_lufs is not None else music.gain_db
        fi, fo = music.fade_in_sec, music.fade_out_sec
        if music.loop:
            inputs += ["-stream_loop", "-1"]
        inputs += ["-i", str(music.path)]
        automation = (
            f"volume='{gain_expr(music.gain_steps)}':eval=frame," if music.gain_steps else ""
        )
        # выравнивание трека: сначала пропускаем его начало (отрицательный
        # start_sec), потом сдвигаем в ролике (положительный). Задержка стоит
        # ДО автоматики громкости: после неё время дорожки совпадает с временем
        # ролика, и ступени из плана попадают в те секунды, что в нём написаны
        skip = max(0.0, -music.start_sec)
        delay = max(0.0, music.start_sec)
        align = f"atrim={skip:.3f},asetpts=PTS-STARTPTS," if skip else ""
        if delay:
            align += f"adelay={int(round(delay * 1000))}:all=1,"
        parts += [
            f"[{index}:a]aformat=sample_rates=44100:channel_layouts=stereo,{align}"
            f"apad=whole_dur={total:.3f},atrim=0:{total:.3f},"
            f"asetpts=PTS-STARTPTS,volume={volume_db:.2f}dB,{automation}"
            f"afade=t=in:st={delay:.3f}:d={fi:.2f},"
            f"afade=t=out:st={max(0.0, total - fo):.3f}:d={fo:.2f},asplit=2[mus][meter]",
            f"[mus][key]sidechaincompress=threshold={threshold:.6f}:ratio={ratio:.3f}"
            ":attack=15:release=400:makeup=1:level_sc=1[ducked]",
        ]
        mixed.append("[ducked]")
        meter = out.with_name(out.stem + ".music.wav")
        index += 1
    if bed is not None:
        # тот же расчёт уровня, что у музыки: сначала к голосу, потом gain_db
        # вниз — число значит одно и то же при любом мастеринге файла
        bed_lufs_in, _ = measure_loudness(bed.path)
        bed_db = (ref + bed.gain_db) - bed_lufs_in if bed_lufs_in is not None else bed.gain_db
        fi, fo = bed.fade_in_sec, bed.fade_out_sec
        # подложка зациклена всегда: она под ВСЕЙ дорожкой, а не «пока хватит»
        inputs += ["-stream_loop", "-1", "-i", str(bed.path)]
        parts.append(
            f"[{index}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"apad=whole_dur={total:.3f},atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
            f"volume={bed_db:.2f}dB,afade=t=in:st=0:d={fi:.2f},"
            f"afade=t=out:st={max(0.0, total - fo):.3f}:d={fo:.2f},asplit=2[bed][bedmeter]"
        )
        mixed.append("[bed]")
        bed_meter = out.with_name(out.stem + ".bed.wav")
        index += 1
    if sounds:
        # все эффекты — одной готовой дорожкой, а не входом на каждый со своим
        # `adelay`: ep003 (18.09.2026) — из восьми звуков последний (1051.8 с)
        # ffmpeg положил в начало ролика, хотя тот же `adelay` в одиночку
        # ставит его верно (воспроизведено на тишине)
        bus = sfx_bus(sounds, total, out.with_name(out.stem + ".sfx.wav"))
        inputs += ["-i", str(bus)]
        parts.append(f"[{index}:a]aformat=sample_rates=44100:channel_layouts=stereo[sfx]")
        mixed.append("[sfx]")
        index += 1
    if len(mixed) > 1:
        parts.append(
            f"{''.join(mixed)}amix=inputs={len(mixed)}:duration=first"
            ":dropout_transition=0:normalize=0[mix]"
        )
    else:
        parts.append("[vo]anull[mix]")
    args = [*inputs, "-filter_complex", ";".join(parts),
            "-map", "[mix]", "-t", f"{total:.3f}", "-c:a", "pcm_s16le", str(out)]  # fmt: skip
    if meter is not None:
        args += ["-map", "[meter]", "-t", f"{total:.3f}", "-c:a", "pcm_s16le", str(meter)]
    if bed_meter is not None:
        args += ["-map", "[bedmeter]", "-t", f"{total:.3f}", "-c:a", "pcm_s16le", str(bed_meter)]
    run(args)
    music_lufs = bed_lufs = None
    if meter is not None:
        music_lufs, _ = measure_loudness(meter)
        meter.unlink(missing_ok=True)
    if bed_meter is not None:
        bed_lufs, _ = measure_loudness(bed_meter)
        bed_meter.unlink(missing_ok=True)
    return music_lufs, bed_lufs


@dataclass(frozen=True)
class Watermark:
    """Водяной знак канала: картинка с альфой в углу кадра, уже в пикселях финала.

    Где и какого размера — решает стадия из настроек проекта и зоны интерфейса
    площадки (`watermark_xy`); здесь только рисуется. Знак ложится ПОД
    субтитрами: текст зритель читает поверх всего, а знаку в полосе субтитров
    делать нечего — за этим следит QA, не рендер."""

    path: Path
    x: int
    y: int
    width: int
    height: int
    opacity: float = 0.7  # множитель альфы: 1 — как нарисовано, 0 — невидим


def watermark_xy(
    corner: str,
    *,
    frame: tuple[int, int],
    box: tuple[int, int],
    margin: int,
    zone: dict[str, float] | None = None,
) -> tuple[int, int]:
    """Левый верхний угол знака: угол кадра минус отступ — и вне зоны интерфейса.

    `zone` — доли кадра сверху, снизу и справа, которые закрывает интерфейс
    площадки (те же числа, по которым QA судит субтитры). Знак в углу под
    колонкой кнопок или под блоком «канал / описание / звук» — знак, которого
    зритель не увидит: поэтому правые углы отступают ещё и от колонки, нижние
    — от блока, верхние — от верхней полосы (сегодня у всех площадок она
    нулевая). Знак шире свободного места прижимается к краю, а не уезжает за
    него — что он попал в зону, скажет QA."""
    w, h = frame
    bw, bh = box
    z = zone or {}
    top = int(round(h * max(0.0, z.get("top", 0.0))))
    bottom = int(round(h * max(0.0, z.get("bottom", 0.0))))
    right = int(round(w * max(0.0, z.get("right", 0.0))))
    x = margin if corner.endswith("left") else w - right - margin - bw
    y = top + margin if corner.startswith("top") else h - bottom - margin - bh
    return max(0, x), max(0, y)


# Цветокоррекция готового ролика: имя → цепочка фильтров. Идёт по картинке ДО
# знака и субтитров — текст не красится. Числа — отправные точки, а не подгонка
# под релиз (та живёт в папке эпизода, `library/grade`): `lift` — замер
# erickerhead 19.09.2026 (яркость +7–10%, насыщенность ≈ +5%), `punch` — форма
# приёма `grade/anime_punch` (тени в ноль, света вверх, середина на месте)
GRADES: dict[str, str] = {
    "lift": "eq=brightness=0.03:contrast=1.04:saturation=1.05",
    # у мультсериала чёрный и белый уже на краях: 6% с каждой стороны пережигали
    # светлую кожу в белое (проба t09, 19.09.2026) — поэтому 3%
    "punch": "curves=all='0/0 0.03/0 0.5/0.5 0.97/1 1/1',eq=saturation=1.08",
    "vivid": "eq=contrast=1.08:saturation=1.25,unsharp=5:5:0.6:5:5:0.0",
    "warm": "colorbalance=rs=0.04:bs=-0.05:rm=0.03:bm=-0.03,eq=saturation=1.10",
}


def finalize(
    video: Path,
    out: Path,
    *,
    captions: Path | None = None,
    fonts_dir: Path | None = None,
    music: MusicMix | None = None,
    sounds: list[Sound] | None = None,
    effects: list[Effect] | None = None,
    watermark: Watermark | None = None,
    headline: Headline | None = None,
    bed: BedMix | None = None,
    grade: str = "",
    size: tuple[int, int] | None = None,
    fade_in_sec: float = 0.3,
    fade_out_sec: float = 0.8,
    audio_tail_sec: float = 0.5,
    target_lufs: float = -14.0,
    target_tp: float = -1.5,
    lra: float = 11.0,
    keep_video: Path | None = None,
    fps: float = FPS,
) -> FinalMix:
    """Склеенные сцены → готовый файл: эффекты кадра, водяной знак канала,
    субтитры (ASS через libass), музыка под голосом (sidechaincompress голосом
    как ключом), звуковые эффекты, нормализация громкости (loudnorm в два
    прохода, линейно), fade на краях.

    Порядок дорожек: VOICE > MUSIC > BED > SFX. Голос не трогается ничем, кроме
    общей нормализации; музыка получает гейн, даккинг и фейды; подложка (`bed`)
    — ровный эмбиенс, зацикленный на всю длину, с гейном и краями; эффекты
    подмешиваются в свои секунды. Длина файла — длина склейки: ни один шаг здесь её не меняет,
    иначе разъедутся субтитры и замеры (хвост замедляют ДО этого места —
    `slow_tail`)."""
    total = probe_duration(video)
    voice_lufs, _ = measure_loudness(video)
    mix = out.with_name(out.stem + ".mix.wav")
    sounds = sounds or []
    if music is not None or sounds or bed is not None:
        music_lufs, bed_lufs = _mix_audio(
            video, mix, music=music, sounds=sounds, total=total, voice_lufs=voice_lufs, bed=bed
        )
    else:
        music_lufs = bed_lufs = None
        run(["-i", str(video), "-map", "0:a:0", "-t", f"{total:.3f}",
             "-af", "aformat=sample_rates=44100:channel_layouts=stereo",
             "-c:a", "pcm_s16le", str(mix)])  # fmt: skip
    stats = _loudnorm_stats(mix, target_i=target_lufs, target_tp=target_tp, lra=lra)

    # эффекты идут до субтитров: текст не трясётся вместе с кадром и не мылится
    # наездом — он и так самый мелкий объект в кадре. Водяной знак — между
    # ними: он часть картинки (трясётся и темнеет с ней), но лежит под текстом
    # `fps` — частота эпизода: сетка `fps` перед `zoompan` обязана совпадать с
    # частотой склейки, иначе эффекты сами станут пересчётом кадров
    vf: list[str] = effect_filters(
        effects or [], size=size or (probe_size(video) or SIZES["9:16"]), fps=fps
    )
    if grade:
        vf.append(GRADES[grade])  # неизвестное имя стадия отсеяла раньше
    over: list[str] = []  # после знака: субтитры, затухания, формат
    if captions is not None:
        ass = f"ass=filename={filter_path(captions)}"
        if fonts_dir is not None:
            ass += f":fontsdir={filter_path(fonts_dir)}"
        over.append(ass)
    if fade_in_sec > 0:
        over.append(f"fade=t=in:st=0:d={fade_in_sec:.3f}")
    if fade_out_sec > 0:
        over.append(f"fade=t=out:st={max(0.0, total - fade_out_sec):.3f}:d={fade_out_sec:.3f}")
    over.append("format=yuv420p")
    # Знак — третий вход (`-i`), а не `movie=` внутри графа: путь тогда идёт
    # обычным аргументом, без экранирования `filter_path`, и PNG с альфой
    # читается тем же декодером, что любой вход. Один кадр картинки overlay
    # держит до конца ролика сам (eof_action=repeat — умолчание). Альфа
    # умножается на непрозрачность в rgba до наложения: overlay читает её как
    # есть, и знак не становится «наполовину серым», а становится прозрачным
    # Подпись над кадром (`core.headline`) — такой же картинкой и тем же путём,
    # что знак: четвёртым входом (или третьим, если знака нет), без эффектов —
    # текст не трясётся вместе с кадром, — но под субтитрами
    extra_inputs: list[str] = []
    if watermark is None and headline is None:
        video_graph = f"[0:v]{','.join(vf + over)}[v]"
    else:
        chain = [f"[0:v]{','.join(vf) or 'null'}[base]"]
        last = "base"
        index = 2
        if watermark is not None:
            extra_inputs += ["-i", str(watermark.path)]
            alpha = (
                f",colorchannelmixer=aa={watermark.opacity:.3f}" if watermark.opacity < 1 else ""
            )
            chain.append(
                f"[{index}:v]scale={watermark.width}:{watermark.height},format=rgba{alpha}[wm];"
                f"[{last}][wm]overlay=x={watermark.x}:y={watermark.y}[marked]"
            )
            last, index = "marked", index + 1
        if headline is not None:
            extra_inputs += ["-i", str(headline.path)]
            chain.append(
                f"[{index}:v]scale={headline.width}:{headline.height},format=rgba[hl];"
                f"[{last}][hl]overlay=x={headline.x}:y={headline.y}[titled]"
            )
            last = "titled"
        video_graph = ";".join(chain) + f";[{last}]{','.join(over)}[v]"
    # Края гасятся ДО нормализации, и это не вкусовщина, а замер (23.09.2026).
    # Фейд ПОСЛЕ loudnorm ломает кодировку: на ролике с заглушённым
    # оригиналом (музыка одна на всю дорожку, −20.9 LUFS / −10.3 dBTP) тот же
    # микс давал −0.4 dBTP без фейда и +3.8 dBTP с фейдом после него, причём в
    # float дорожка в обоих случаях чистая (−3.2) — клиппинг рождался на выходе
    # AAC. До loudnorm фейды дают −0.3 dBTP и max −0.5 дБ, то есть просто
    # правильно. Громкость от переноса не едет: 30 мс края весят меньше сотой доли
    # дБ в интеграле, а замеры первого прохода сняты с того же микса
    af: list[str] = ["afade=t=in:d=0.03"]
    # затухание звука не длиннее хвоста тишины сцены: иначе оно съест конец речи
    a_out = min(fade_out_sec, audio_tail_sec)
    if a_out > 0:
        af.append(f"afade=t=out:st={max(0.0, total - a_out):.3f}:d={a_out:.3f}")
    if stats is not None:
        af.append(
            f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={lra}"
            f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}:linear=true"
        )
    af.append("aresample=44100")  # loudnorm отдаёт 192 кГц — вернуть частоту
    # Быстрая ветка: картинка не менялась, поменялся только звук. Дорожка
    # картинки берётся из готового файла как есть — субтитры, эффекты и
    # затухания в ней уже нарисованы, и перекодировать их заново незачем.
    # Решает НЕ этот код, а стадия: она сверяет отпечаток видеостороны и
    # передаёт `keep_video` только когда та совпала. Здесь важно другое —
    # ветка не имеет права молча подсунуть старую картинку к новому звуку,
    # поэтому её включает вызывающий, а не догадка внутри.
    if keep_video is not None:
        tmp = out.with_name(out.name + ".tmp.mp4")
        try:
            run(
                [
                    "-i", str(keep_video), "-i", str(mix),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-af", ",".join(af), "-t", f"{total:.3f}",
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", FINAL_AUDIO_KBPS, "-ar", "44100", "-ac", "2",
                    "-movflags", "+faststart",
                    str(tmp),
                ]
            )  # fmt: skip
            replace_file(tmp, out)
        finally:
            mix.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
        lufs, peak = measure_loudness(out)
        return FinalMix(
            duration_sec=probe_duration(out),
            loudness_lufs=lufs,
            true_peak_dbtp=peak,
            voice_lufs=voice_lufs,
            music_lufs=music_lufs,
            bed_lufs=bed_lufs,
        )

    graph = f"{video_graph};[1:a]{','.join(af)}[a]"
    # Рендерим рядом и подменяем в конце — как `core/io.py` пишет JSON. Прерванный
    # проход (Ctrl+C, нажали «пересобрать» и передумали, упало электричество)
    # оставляет играбельный обрубок: половина ролика, которая открывается, и по
    # виду не отличается от целого. Такой обрубок пульт покажет, а `publish` зальёт
    tmp = out.with_name(out.name + ".tmp.mp4")
    try:
        run(
            [
                "-i", str(video), "-i", str(mix), *extra_inputs,
                "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                "-t", f"{total:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", FINAL_AUDIO_KBPS, "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart",
                str(tmp),
            ]
        )  # fmt: skip
        replace_file(tmp, out)
    finally:
        mix.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
    lufs, peak = measure_loudness(out)
    return FinalMix(
        duration_sec=probe_duration(out),
        loudness_lufs=lufs,
        true_peak_dbtp=peak,
        voice_lufs=voice_lufs,
        music_lufs=music_lufs,
        bed_lufs=bed_lufs,
    )


Interval = tuple[float, float]


class FinalMeasures(NamedTuple):
    black_sec: float
    freeze_sec: float
    silence_sec: float
    black: list[Interval]  # интервалы в секундах финала — чтобы назвать сцену
    freeze: list[Interval]
    silence: list[Interval]


_BLACK = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")
_FREEZE_START = re.compile(r"freeze_start:\s*([\d.]+)")
_FREEZE_END = re.compile(r"freeze_end:\s*([\d.]+)")
_SILENCE_START = re.compile(r"silence_start:\s*([\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([\d.]+)")


def _pairs(starts: list[str], ends: list[str], total: float) -> list[Interval]:
    """start/end из лога → интервалы; событие без конца тянется до конца файла."""
    out: list[Interval] = []
    for i, s in enumerate(starts):
        e = float(ends[i]) if i < len(ends) else total
        out.append((float(s), e))
    return out


def _window(intervals: list[Interval], lo: float, hi: float) -> list[Interval]:
    """Оставить только то, что попадает между fade in и fade out."""
    kept = [(max(s, lo), min(e, hi)) for s, e in intervals]
    return [(s, e) for s, e in kept if e - s > 0]


def measure_final(
    path: Path, *, skip_head_sec: float = 0.0, skip_tail_sec: float = 0.0
) -> FinalMeasures:
    """Чёрные и застывшие кадры, тишина — одним проходом по готовому файлу.
    Fade in/out по краям чёрные и тихие по замыслу и из счёта исключаются."""
    total = probe_duration(path)
    err = _run_capture(["-i", str(path),
                        "-vf", "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=0.001:d=0.5",
                        "-af", "silencedetect=n=-40dB:d=1.0",
                        "-f", "null", "-"])  # fmt: skip
    lo, hi = skip_head_sec, total - skip_tail_sec
    black = _window([(float(s), float(e)) for s, e in _BLACK.findall(err)], lo, hi)
    freeze = _window(_pairs(_FREEZE_START.findall(err), _FREEZE_END.findall(err), total), lo, hi)
    silence = _window(_pairs(_SILENCE_START.findall(err), _SILENCE_END.findall(err), total), lo, hi)
    return FinalMeasures(
        round(sum(e - s for s, e in black), 3),
        round(sum(e - s for s, e in freeze), 3),
        round(sum(e - s for s, e in silence), 3),
        black,
        freeze,
        silence,
    )
