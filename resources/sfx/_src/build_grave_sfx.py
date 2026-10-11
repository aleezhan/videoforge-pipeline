"""Сборка звуков для ролика о могиле (doc_channel/ep003), 19.09.2026.

Запуск из корня репозитория:  uv run python resources/sfx/_src/build_grave_sfx.py [id …]

Исходники — hq-превью freesound (CC0) рядом, `freesound_<id>.mp3`; слои низа и
гул синтезированы ffmpeg (aevalsrc / anoisesrc) — автора у них нет. Музыки и
инструментов нет ни в одном звуке: только предметные записи и шум.
Каждый звук: 48 кГц, стерео, s16, пик −3 dBFS, короткие fade на краях.
Рядом с wav кладётся картинка `_preview/<id>.png` (волна + спектрограмма).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
SRC = Path(__file__).resolve().parent
OUT = SRC.parent
PREVIEW = OUT / "_preview"
PEAK = -3.0
FMT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


def ff(args: list[str]) -> str:
    done = subprocess.run([FF, "-hide_banner", "-y", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")  # fmt: skip
    if done.returncode:
        raise SystemExit(done.stderr[-3000:])
    return done.stderr


def src(sound_id: int) -> str:
    return str(SRC / f"freesound_{sound_id}.mp3")


def render(sound_id: str, inputs: list[str], graph: str, duration: float) -> None:
    """Граф → raw.wav → замер пика → гейн до PEAK, fade на краях → <id>.wav."""
    raw = OUT / f"{sound_id}.raw.wav"
    args: list[str] = []
    for item in inputs:
        args += ["-i", item]
    ff([*args, "-filter_complex", graph, "-map", "[out]", "-t", f"{duration:g}",
        "-c:a", "pcm_f32le", str(raw)])  # fmt: skip
    # volumedetect считает в s16 и режет всё выше 0 дБ, а сумма слоёв во float
    # бывает громче — поэтому замер идёт на 30 дБ тише, и они возвращаются
    log = ff(["-i", str(raw), "-af", "volume=-30dB,volumedetect", "-f", "null", "-"])
    peak = 30 + float(re.search(r"max_volume: (-?[\d.]+) dB", log).group(1))  # type: ignore[union-attr]
    out = OUT / f"{sound_id}.wav"
    ff(["-i", str(raw), "-af",
        f"volume={PEAK - peak:.2f}dB,afade=t=in:d=0.008,"
        f"afade=t=out:st={duration - 0.03:g}:d=0.03,{FMT}",
        "-c:a", "pcm_s16le", str(out)])  # fmt: skip
    raw.unlink()
    PREVIEW.mkdir(exist_ok=True)
    ff(["-i", str(out), "-filter_complex",
        "[0:a]asplit[a][b];"
        "[a]aformat=channel_layouts=mono,showwavespic=s=1116x240:colors=0x2a9d8f,format=rgb24,"
        "pad=1400:240:142:0:black[w];"
        "[b]aformat=channel_layouts=mono,showspectrumpic=s=1116x400:legend=1:fscale=log,"
        "format=rgb24[s];"
        "[w][s]vstack[v]",
        "-map", "[v]", "-frames:v", "1", str(PREVIEW / f"{sound_id}.png")])  # fmt: skip
    print(sound_id, "готов")


def thump(f_end: float, f_drop: float, decay: float, dur: float, sweep: float = 18) -> str:
    """Низкий удар: синус, частота падает с f_end+f_drop к f_end, плюс вторая
    гармоника на четверть уровня — без неё удар пропадает на динамике телефона."""
    ph = f"2*PI*({f_end:g}+{f_drop:g}*exp(-{sweep:g}*t))*t"
    return (f"aevalsrc='exp(-{decay:g}*t)*(sin({ph})+0.25*sin(2*{ph}))'"
            f":d={dur:g}:s=48000,afade=t=out:st={dur - 0.1:g}:d=0.1,{FMT}")  # fmt: skip


def steps_heavy_earth() -> None:
    # четыре настоящих шага по земле и сухим листьям (freesound 854190), каждый
    # замедлен в 0.7 раза (asetrate) — тяжелее и ниже; под каждым синтезированный
    # низ 42–92 Гц. Приближение: каждый следующий громче и ярче (lowpass выше).
    cuts = [(0.09, 0.50), (0.66, 0.50), (1.17, 0.50), (1.79, 0.50)]
    at = [0.20, 1.42, 2.60, 3.83]  # интервалы 1.22 / 1.18 / 1.23 с
    gain = [-13, -9, -5, -1]
    lowpass = [900, 1500, 2600, 5000]
    parts, names = [], []
    parts.append(f"[0:a]{FMT},asplit=4" + "".join(f"[s{i}]" for i in range(4)))
    for i, ((start, dur), t, g, lp) in enumerate(zip(cuts, at, gain, lowpass, strict=True)):
        ms = int(t * 1000)
        parts.append(
            f"[s{i}]atrim={start}:{start + dur},asetpts=N/SR/TB,asetrate=48000*0.7,"
            f"aresample=48000,afade=t=in:d=0.004,afade=t=out:st=0.45:d=0.26,"
            f"lowpass=f={lp},volume={g + 9}dB,adelay={ms}:all=1[c{i}]"
        )
        parts.append(f"{thump(42, 50, 7, 0.9)},volume={g}dB,adelay={ms}:all=1[t{i}]")
        names += [f"[c{i}]", f"[t{i}]"]
    parts.append(
        "".join(names) + "amix=inputs=8:normalize=0:dropout_transition=0,"
        "aecho=0.85:0.8:37|73|113|167:0.28|0.22|0.16|0.10,lowpass=f=7000,apad[out]"
    )
    render("steps_heavy_earth", [src(854190)], ";".join(parts), 5.6)


def slice_peak(path: str, start: float, dur: float) -> float:
    log = ff(["-ss", f"{start:g}", "-t", f"{dur:g}", "-i", path, "-af", "volumedetect",
              "-f", "null", "-"])  # fmt: skip
    return float(re.search(r"max_volume: (-?[\d.]+) dB", log).group(1))  # type: ignore[union-attr]


def steps_sandals_away() -> None:
    # восемь настоящих шагов по песку с гравием (freesound 384082), в своём
    # темпе (~0.55 с). Шаги в записи разной силы, поэтому каждый вырезан и
    # выровнен по пику отдельно, а удаление задано числами: каждый следующий на
    # 2 дБ тише и глуше (lowpass 8000 → 1300 Гц) — шаги затухают и теряют верх.
    onsets = [2.79, 3.37, 3.92, 4.48, 5.00, 5.50, 6.09, 6.65]
    lowpass = [8000, 6500, 5000, 3800, 2900, 2200, 1700, 1300]
    n = len(onsets)
    parts, names = [], []
    parts.append(f"[0:a]{FMT},asplit={n}" + "".join(f"[s{i}]" for i in range(n)))
    for i, (t, lp) in enumerate(zip(onsets, lowpass, strict=True)):
        start, dur = t - 0.03, 0.50
        gain = -2.0 * i - slice_peak(src(384082), start, dur)
        parts.append(
            f"[s{i}]atrim={start:g}:{start + dur:g},asetpts=N/SR/TB,afade=t=in:d=0.01,"
            f"afade=t=out:st=0.3:d=0.2,lowpass=f={lp},volume={gain:.1f}dB,"
            f"adelay={int((t - 2.70) * 1000)}:all=1[c{i}]"
        )
        names.append(f"[c{i}]")
    parts.append(
        "".join(names) + f"amix=inputs={n}:normalize=0:dropout_transition=0,"
        "aecho=0.9:0.6:45|90:0.18|0.10,apad,afade=t=out:st=4.5:d=0.5[out]"
    )
    render("steps_sandals_away", [src(384082)], ";".join(parts), 5.0)


def earth_fall() -> None:
    # пять настоящих падений земли в яму (freesound 384362: «soil falling to the
    # bottom of the hole»), россыпью; каждое следующее тише и глуше, под каждым
    # короткий синтезированный глухой удар — доска под землёй.
    cuts = [(23.72, 1.00), (7.98, 0.90), (12.08, 0.90), (3.86, 0.85), (16.86, 0.80)]
    at = [0.10, 1.15, 2.02, 3.08, 3.92]
    gain = [0, -3, -6, -9, -12]
    lowpass = [3200, 2200, 1500, 1000, 700]
    parts, names = [], []
    parts.append(f"[0:a]{FMT},asplit=5" + "".join(f"[s{i}]" for i in range(5)))
    for i, ((start, dur), t, g, lp) in enumerate(zip(cuts, at, gain, lowpass, strict=True)):
        ms = int(t * 1000)
        parts.append(
            f"[s{i}]atrim={start}:{start + dur},asetpts=N/SR/TB,highpass=f=60,"
            f"afade=t=in:d=0.03,afade=t=out:st={dur - 0.35:g}:d=0.35,"
            f"lowpass=f={lp},volume={g}dB,adelay={ms}:all=1[c{i}]"
        )
        parts.append(
            f"{thump(55, 40, 14, 0.4, sweep=25)},volume={g - 7}dB,adelay={ms + 60}:all=1[t{i}]"
        )
        names += [f"[c{i}]", f"[t{i}]"]
    parts.append(
        "".join(names) + "amix=inputs=10:normalize=0:dropout_transition=0,"
        "aecho=0.9:0.7:28|55:0.25|0.15,apad[out]"
    )
    render("earth_fall", [src(384362)], ";".join(parts), 5.0)


def heartbeat_slow() -> None:
    # синтез: 9 ударов «тук-тук» по 0.9 с (66.7 уд/мин) = 8.1 с ровно, файл
    # начинается за 0.1 с до удара и кончается в тишине — петля без шва.
    lub = "mod(t+0.8,0.9)"
    dub = "mod(t+0.52,0.9)"  # второй тон через 0.28 с после первого

    def tone(p: str, f: float, decay: float, level: float) -> str:
        ph = f"2*PI*({f:g}+25*exp(-30*{p}))*{p}"
        return f"{level:g}*exp(-{decay:g}*{p})*(sin({ph})+0.3*sin(2*{ph}))"

    expr = tone(lub, 48, 20, 1.0) + "+" + tone(dub, 62, 26, 0.6)
    graph = f"aevalsrc='{expr}':d=8.1:s=48000,lowpass=f=220,{FMT}[out]"
    render("heartbeat_slow", [], graph, 8.1)


def wind_soft() -> None:
    # настоящий мягкий ветер (freesound 844818, холмы под Будапештом), кусок
    # 10–18 с; гул задувания в микрофон снят highpass 260, воздух чуть поднят,
    # вход 2.5 с и выход 3 с.
    graph = (
        f"[0:a]atrim=10:18,asetpts=N/SR/TB,{FMT},highpass=f=260,highpass=f=260,"
        "treble=g=5:f=3000,lowpass=f=9000,"
        "volume=eval=frame:volume='0.8+0.2*sin(2*PI*t/5.3)',"
        "afade=t=in:d=2.5:curve=qsin,afade=t=out:st=5:d=3:curve=qsin[out]"
    )
    render("wind_soft", [src(844818)], graph, 8.0)


def rumble_squeeze() -> None:
    # синтез: коричневый шум под lowpass 160 + два низких тона с биением
    # (36 и 43 Гц) + «скрежет грунта» — шум 180–600 Гц, дрожащий неровно (7.3 и 12.1 Гц).
    # Уровень растёт квадратично и обрывается на 3.95 с за 40 мс.
    graph = (
        f"anoisesrc=color=brown:d=4:r=48000:seed=7,lowpass=f=160,lowpass=f=160,volume=13dB,{FMT}[n];"
        "aevalsrc='0.4*sin(2*PI*36*t)+0.3*sin(2*PI*43*t+2*sin(2*PI*0.7*t))"
        f"+0.1*sin(2*PI*72*t)':d=4:s=48000,{FMT}[s];"
        "anoisesrc=color=pink:d=4:r=48000:seed=11,highpass=f=180,lowpass=f=600,"
        f"volume=eval=frame:volume='0.9*(0.5+0.3*sin(2*PI*7.3*t)+0.2*sin(2*PI*12.1*t))',{FMT}[g];"
        "[n][s][g]amix=inputs=3:normalize=0,"
        "volume=eval=frame:volume='0.03+pow(t/3.95,2)',"
        "afade=t=in:d=0.3,afade=t=out:st=3.95:d=0.04[out]"
    )
    render("rumble_squeeze", [], graph, 4.0)


def hammer_iron() -> None:
    # настоящий удар кувалдой по железному колу (freesound 351312, удар на
    # 8.71 с), замедлен в 0.8 раза — тяжелее; уличный фон записи уходит
    # затуханием; под ударом синтезированный низкий хвост 45–85 Гц.
    graph = (
        f"[0:a]atrim=8.695:10.30,asetpts=N/SR/TB,{FMT},asetrate=48000*0.8,aresample=48000,"
        "afade=t=in:d=0.003,highpass=f=90,"
        "volume=eval=frame:volume='min(1,exp(-1.8*(t-0.15)))',adelay=20:all=1[h];"
        f"{thump(45, 40, 4.5, 1.95, sweep=10)},volume=-12dB,adelay=20:all=1[t];"
        "[h][t]amix=inputs=2:normalize=0,aecho=0.9:0.6:23|41|67:0.18|0.13|0.08,apad,"
        "afade=t=out:st=1.5:d=0.5[out]"
    )
    render("hammer_iron", [src(351312)], graph, 2.0)


def whoosh_soft() -> None:
    # настоящий пролёт воздуха (freesound 701104: насос, стерео): тело пролёта
    # 0.20–0.84 с без длинного хвоста реверберации, замедлено в 0.8 раза —
    # ниже и мягче, ровно 0.8 с.
    graph = (
        f"[0:a]atrim=0.20:0.84,asetpts=N/SR/TB,{FMT},asetrate=48000*0.8,aresample=48000,"
        "afade=t=in:d=0.1:curve=qsin,afade=t=out:st=0.5:d=0.3:curve=qsin[out]"
    )
    render("whoosh_soft", [src(701104)], graph, 0.8)


ALL = {
    f.__name__: f
    for f in (
        steps_heavy_earth,
        steps_sandals_away,
        earth_fall,
        heartbeat_slow,
        wind_soft,
        rumble_squeeze,
        hammer_iron,
        whoosh_soft,
    )  # fmt: skip
}

if __name__ == "__main__":
    for name in sys.argv[1:] or list(ALL):
        ALL[name]()
