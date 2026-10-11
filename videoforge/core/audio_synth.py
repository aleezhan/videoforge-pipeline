"""Звуковые ассеты, синтезированные локально ffmpeg: эффекты и музыкальные подложки.

Зачем синтез, а не готовые сэмплы. Эффект перехода — короткий звук, который
слышно в каждом ролике; скачанный «бесплатный whoosh» без внятной лицензии
однажды снимет монетизацию, и разбираться придётся на канале, а не здесь.
Синтезированный звук — public domain по построению: у него нет автора, кроме
формулы, и формула лежит рядом.

Каждый эффект — одна строка выражения `aevalsrc` (плюс фильтры после неё) и
одно предложение о том, где его ставят. Звук пишется в `resources/sfx/<id>.wav`,
паспорт — в `<id>.yaml` рядом; после генерации файл выравнивается по пику
(`PEAK_DBFS`), чтобы «сила» эффекта в плане монтажа значила уровень в миксе, а
не то, насколько громко получилась формула.

Синтез — не замена настоящей библиотеке: это то, с чем можно начать сегодня,
не заводя счетов и не рискуя правами. Купленный или записанный звук кладётся
тем же ресурсом (`videoforge resource new sfx <id>` + файл рядом) и работает
без единой правки в коде.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from videoforge.core.ffmpeg import measure_loudness, run

PEAK_DBFS = -3.0  # к какому пику приводится каждый звук пакета
SAMPLE_RATE = 44100


@dataclass(frozen=True)
class Recipe:
    """Рецепт одного эффекта: выражение, длительность, где применять."""

    id: str
    name: str
    kind: str  # impact | whoosh | riser | sub_drop | click | glitch | music | bed
    duration: float
    expr: str  # выражение aevalsrc: t — секунды, random(0) ∈ 0..1
    filters: str = ""  # цепочка -af после генерации
    gain_db: float = -6.0  # уровень в миксе относительно голоса
    where: str = ""  # человеческое «куда его ставят»


# Амплитуды в выражениях подобраны так, чтобы формула не клиппила сама по себе;
# точный уровень всё равно выставляет выравнивание по пику.
PACK: tuple[Recipe, ...] = (
    Recipe(
        id="impact_deep",
        name="Удар (низкий)",
        kind="impact",
        duration=0.9,
        # синус, чья частота падает с 90 к 55 Гц, плюс щелчок атаки
        expr="0.9*exp(-6*t)*sin(2*PI*(55+35*exp(-14*t))*t)+0.25*exp(-40*t)*(2*random(0)-1)",
        gain_db=-4.0,
        where="на сам кадр смены: удар совпадает со склейкой, а не следует за ней",
    ),
    # «Пролёт» (whoosh) и «Нагнетание» (riser) удалены 18.09.2026 по слову
    # владельца: оба звучат дёшево («эти два — говно, удалить и не давать
    # использовать»). Рецепты убраны, чтобы gen-sfx не вернул их в resources/
    Recipe(
        id="sub_drop",
        name="Падение вниз",
        kind="sub_drop",
        duration=1.4,
        # частота падает со 150 к 35 Гц по экспоненте — «провал» под ногами
        expr="0.85*exp(-1.5*t)*sin(2*PI*75*(1-exp(-2*t)))",
        gain_db=-4.0,
        where="в момент замедления: падение вниз читается как «время поехало»",
    ),
    Recipe(
        id="hit_snap",
        name="Щелчок-акцент",
        kind="click",
        duration=0.25,
        expr="exp(-30*t)*(0.7*(2*random(0)-1)+0.3*sin(2*PI*220*t))",
        filters="highpass=f=400",
        gain_db=-12.0,
        where="на слово-акцент: короткий, его задача — подчеркнуть, а не быть услышанным",
    ),
    Recipe(
        id="glitch",
        name="Глитч",
        kind="glitch",
        duration=0.3,
        expr="(2*random(0)-1)*0.5*gt(sin(2*PI*40*t),0)*exp(-4*t)",
        filters="highpass=f=800",
        gain_db=-10.0,
        where="вместе с визуальным глитчем, кадр в кадр — иначе это просто помеха",
    ),
)


def _yaml(recipe: Recipe) -> str:
    return "\n".join(
        [
            f"# {recipe.name}. {recipe.where}",
            "#",
            "# Звук синтезирован локально (videoforge/core/audio_synth.py, videoforge resource",
            "# gen-sfx): у него нет автора, кроме формулы, поэтому прав ни у кого нет.",
            f"# Формула: aevalsrc='{recipe.expr}'"
            + (f", фильтры: {recipe.filters}" if recipe.filters else ""),
            f"id: {recipe.id}",
            f'name: "{recipe.name}"',
            f"kind: {recipe.kind}",
            f"file: {recipe.id}.wav",
            'license: "public domain — синтезирован локально ffmpeg, чужого материала нет"',
            'source: "videoforge resource gen-sfx"',
            f"duration_sec: {recipe.duration:g}",
            f"gain_db: {recipe.gain_db:g}    # уровень в миксе относительно голоса",
            "",
        ]
    )


def synthesize(recipe: Recipe, out: Path) -> Path:
    """Рецепт → wav, выровненный по пику `PEAK_DBFS`.

    Два прохода: сначала формула, потом замер пика и гейн. Без выравнивания
    «сила» эффекта в плане монтажа означала бы разное для разных формул."""
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_suffix(".raw.wav")
    args = ["-f", "lavfi", "-i",
            f"aevalsrc='{recipe.expr}':d={recipe.duration:g}:s={SAMPLE_RATE}"]  # fmt: skip
    if recipe.filters:
        args += ["-af", recipe.filters]
    run([*args, "-ac", "1", str(raw)])
    _, peak = measure_loudness(raw, "0:a:0")
    gain = PEAK_DBFS - peak if peak is not None else 0.0
    run(["-i", str(raw), "-af", f"volume={gain:.2f}dB", "-ac", "1", "-ar", str(SAMPLE_RATE),
         str(out)])  # fmt: skip
    raw.unlink(missing_ok=True)
    return out


def build_pack(base: Path, *, force: bool = False) -> list[tuple[str, str]]:
    """Собрать всю библиотеку в `<base>/sfx/`. Возвращает (id, что сделано)."""
    return _build(PACK, base / "sfx", _yaml, force=force)


def _build(
    recipes: tuple[Recipe, ...],
    out_dir: Path,
    to_yaml: Callable[[Recipe], str],
    *,
    force: bool,
) -> list[tuple[str, str]]:
    done: list[tuple[str, str]] = []
    for recipe in recipes:
        wav = out_dir / f"{recipe.id}.wav"
        yaml_path = out_dir / f"{recipe.id}.yaml"
        if wav.exists() and yaml_path.exists() and not force:
            done.append((recipe.id, "уже есть"))
            continue
        synthesize(recipe, wav)
        yaml_path.write_text(to_yaml(recipe), encoding="utf-8")
        done.append((recipe.id, f"{recipe.duration:g} с"))
    return done


# ---------------------------------------------------------------- музыкальные подложки

# Петля, а не трек: `loop_ok` у ресурса музыки уже умеет повторять её столько,
# сколько идёт ролик. Длина петли — целое число тактов, иначе на стыке слышен
# сбой доли. Инструменты складываются в одно выражение: удар (частота падает —
# это и есть «бочка»), рабочий на 2 и 4, хэт на восьмых, суббас и минорное трезвучие.


def _kick(period: float, decay: float = 9.0, level: float = 0.9) -> str:
    beat = f"mod(t,{period:g})"
    return f"{level:g}*exp(-{decay:g}*{beat})*sin(2*PI*(50+40*exp(-20*{beat}))*{beat})"


def _hat(period: float, level: float = 0.12) -> str:
    return f"{level:g}*exp(-60*mod(t,{period:g}))*(2*random(0)-1)"


def _snare(period: float, offset: float, level: float = 0.35) -> str:
    return f"{level:g}*exp(-25*mod(t-{offset:g},{period:g}))*(2*random(0)-1)"


def _chord(freqs: tuple[float, ...], level: float) -> str:
    return f"{level:g}*(" + "+".join(f"sin(2*PI*{f:g}*t)" for f in freqs) + ")"


MUSIC_PACK: tuple[Recipe, ...] = (
    Recipe(
        id="drive_dark",
        name="Тёмный драйв (100 BPM)",
        kind="music",
        duration=9.6,  # 16 долей по 0.6 с
        expr="+".join(
            [
                _kick(0.6),
                _snare(1.2, 0.6),
                _hat(0.3),
                "0.25*sin(2*PI*55*t)",
                _chord((220.0, 261.6, 329.6), 0.09),  # ля минор
            ]
        ),
        filters="lowpass=f=12000",
        gain_db=-11.0,
        where="под эпичный монтаж: ровный пульс, на который ложатся удары и наезды",
    ),
    Recipe(
        id="sigma_drop",
        name="Тяжёлый дроп (70 BPM)",
        kind="music",
        duration=6.86,  # 8 долей по 0.857 с
        expr="+".join(
            [
                _kick(0.857, decay=6.0),
                _hat(0.857, level=0.08),
                "0.3*sin(2*PI*41.2*t)",  # суббас: ми контроктавы
                _chord((164.8, 196.0, 246.9), 0.11),  # ми минор, выше баса
            ]
        ),
        filters="lowpass=f=9000",
        gain_db=-9.0,
        where="на дроп: вступает там, где голос договорил свою мысль",
    ),
)


def _music_yaml(recipe: Recipe) -> str:
    bpm = int(round(60 / (recipe.duration / 8))) if recipe.id == "sigma_drop" else 100
    return "\n".join(
        [
            f"# {recipe.name}. {recipe.where}",
            "#",
            "# Петля синтезирована локально (videoforge/core/audio_synth.py,",
            "# videoforge resource gen-music): автора нет, значит нет и прав.",
            "# Это не замена настоящему треку, а то, с чем можно собрать эпизод",
            "# сегодня и услышать монтаж, не заводя счетов и не рискуя Content ID.",
            f"# Формула: aevalsrc='{recipe.expr}'",
            f"id: {recipe.id}",
            f'name: "{recipe.name}"',
            f"file: {recipe.id}.wav",
            'license: "public domain — синтезирована локально ffmpeg, чужого материала нет"',
            'source: "videoforge resource gen-music"',
            f'mood: "{recipe.where.split(":")[0]}"',
            "energy: 0.8",
            f"bpm: {bpm}",
            "loop_ok: true                 # петля, её и надо повторять",
            "tags: [synth, loop, drums]",
            f"gain_db: {recipe.gain_db:g}                # относительно голоса, до даккинга",
            "duck_db: 9.0",
            "",
        ]
    )


def build_music_pack(base: Path, *, force: bool = False) -> list[tuple[str, str]]:
    """Собрать музыкальные петли в `<base>/music/`."""
    return _build(MUSIC_PACK, base / "music", _music_yaml, force=force)


# ---------------------------------------------------------------- ровная подложка

# Подложка — слой сборки под ВСЕЙ дорожкой (`ffmpeg.BedMix`), и от неё требуется
# то, чего у музыки нет: держать один уровень и зацикливаться без шва. Готовый
# soft_pad для этого не годится — его «медленная модуляция» качает моментальную
# громкость на 20.7 дБ (−38.1…−17.4 LUFS, замер 14.09.2026), то есть он сам
# создаёт провалы, которые слой должен закрывать.
#
# Как получается ровно. Пад — две группы синусов, тёмная (D2–F3) и яркая
# (A3–D5), ре минор; «медленный фильтр» — не lowpass, а перекрёстное затухание
# групп с весами cos θ / sin θ: cos² + sin² = 1, и суммарная мощность постоянна
# ПО ПОСТРОЕНИЮ, тембр при этом ходит тёмный ↔ яркий с периодом петли. Внутри
# групп интервалы не ближе 27 Гц — биения двух близких тонов и есть модуляция
# уровня, а расстройка «для шири» здесь запрещена. Яркая группа на 1 дБ тише
# тёмной: K-взвешивание ebur128 считает верх громче низа, и при равной
# мощности групп громкость всё же качалась на 0.9 дБ; с поправкой — 0.4.
# Фильтров после формулы нет намеренно: IIR стартует с нуля, и его переходный
# процесс — щелчок на стыке петли.
#
# Как получается петля. Частоты округлены до 1/BED_LOOP_SEC Гц (сдвиг ≤ 0.03 Гц,
# на слух ничего), поэтому каждый синус укладывается в петлю целым числом
# периодов, и LFO тембра — тоже: сэмпл после последнего равен первому, шва нет
# и кроссфейд не нужен. Замер: |последний − первый| = 1630 при максимальном
# шаге между соседними сэмплами 1572, разброс громкости на стыке двух петель
# 0.3 дБ.
BED_LOOP_SEC = 16.0
BED_GAIN_DB = -28.0  # относительно голоса; обоснование — `ffmpeg.BedMix`
_BED_DARK = ((73.416, 0.5), (110.0, 0.35), (146.832, 0.3), (174.614, 0.25))
_BED_BRIGHT = (
    (220.0, 0.4083),
    (293.665, 0.3403),
    (349.228, 0.2722),
    (440.0, 0.2041),
    (587.33, 0.1361),
)


def _bed_chord(parts: tuple[tuple[float, float], ...]) -> str:
    q = BED_LOOP_SEC
    return "+".join(f"{a:g}*sin(2*PI*{round(f * q) / q:g}*t)" for f, a in parts)


_BED_THETA = f"(PI/4+PI/8*sin(2*PI*t/{BED_LOOP_SEC:g}))"

PRESETS: dict[str, Recipe] = {
    "flat_pad": Recipe(
        id="flat_pad",
        name="Ровная подложка",
        kind="bed",
        duration=BED_LOOP_SEC,
        expr=(
            f"cos({_BED_THETA})*({_bed_chord(_BED_DARK)})"
            f"+sin({_BED_THETA})*({_bed_chord(_BED_BRIGHT)})"
        ),
        gain_db=BED_GAIN_DB,
        where="под всей дорожкой: закрывает паузы между репликами, пока музыка ещё не вступила",
    ),
}


def _bed_yaml(recipe: Recipe, preset: str) -> str:
    return "\n".join(
        [
            f"# {recipe.name}. {recipe.where}",
            "#",
            "# Синтезирована локально (videoforge/core/audio_synth.py, videoforge resource",
            f"# gen-music {recipe.id} --preset {preset}): автора нет, значит нет и прав.",
            "# Ровная по построению (разброс моментальной громкости 0.4 дБ) и зациклена",
            "# без шва — это слой подложки сборки (project.yaml → audio.bed), не музыка.",
            f"# Формула: aevalsrc='{recipe.expr}'",
            f"id: {recipe.id}",
            f'name: "{recipe.name}"',
            f"file: {recipe.id}.wav",
            'license: "public domain — синтезирована локально ffmpeg, чужого материала нет"',
            'source: "videoforge resource gen-music"',
            'mood: "ровная, нейтральная"',
            "energy: 0.1",
            "bpm: null",
            "drop_sec: null",
            "loop_ok: true",
            "bed: true                     # подложка: ровный зацикливаемый эмбиенс без развития",
            "tags: [ambient, pad, bed, synth]",
            f"gain_db: {recipe.gain_db:g}                # относительно голоса; слой подложки",
            "                              # берёт audio.bed_gain_db проекта, это — для музыки",
            "duck_db: 0.0",
            "",
        ]
    )


def build_preset(
    base: Path, preset: str, resource_id: str | None = None, *, force: bool = False
) -> tuple[str, str]:
    """Один пресет → ресурс `<base>/music/<id>` (id по умолчанию — имя пресета).
    Возвращает (id, что сделано)."""
    if preset not in PRESETS:
        raise KeyError(f"пресета {preset!r} нет; есть: {', '.join(sorted(PRESETS))}")
    recipe = replace(PRESETS[preset], id=resource_id or preset)
    return _build((recipe,), base / "music", lambda r: _bed_yaml(r, preset), force=force)[0]
