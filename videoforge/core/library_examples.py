"""Рецепты примеров «было → стало» для приёмов библиотеки.

Пример — часть приёма (`core/library.find_example`), и он обязан собираться
заново одной командой, а не жить одноразовым скриптом: сменился материал или
уточнили приём — `videoforge lib render-example <id>` пересобирает пару. Рецепт
= функция «папка вывода → файлы before/after» на материале из `projects/` и
`resources/`. Материала нет — `MissingMaterial` с человеческим текстом, а не
трассировка: на другой машине может не быть Re:Zero и подкаста.

Правила примера. Тот же кадр или отрезок, обработанный одинаково во всём,
кроме самого приёма (водяной знак убирается у обоих). Материал — тот, для
которого приём написан: коррекция для аниме — на кадре аниме, где чёрный стоит
на 18, а не на тест-карте с чёрным на нуле. Это витрина, не рендер: видео до
восьми секунд, ширина 1280. Звуковые приёмы показываются волной дорожки —
видео со звуком, чтобы «музыка проседает под речь» было видно, а не только
слышно.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from videoforge.core.captions import CaptionEvent, OverlayEvent, Word, build_ass
from videoforge.core.ffmpeg import filter_path, run
from videoforge.core.resources import CaptionStyle
from videoforge.paths import episode_dir, fonts_dir, projects_dir, resources_dir


class MissingMaterial(RuntimeError):
    """Файла, на котором строится пример, нет на этой машине."""


@dataclass(frozen=True)
class Rendered:
    after: Path
    before: Path | None = None


Recipe = Callable[[Path], Rendered]
RECIPES: dict[str, Recipe] = {}


def recipe(technique_id: str) -> Callable[[Recipe], Recipe]:
    def wrap(fn: Recipe) -> Recipe:
        RECIPES[technique_id] = fn
        return fn

    return wrap


def render(technique_id: str, out_dir: Path) -> Rendered:
    fn = RECIPES.get(technique_id)
    if fn is None:
        raise KeyError(f"рецепта для {technique_id!r} нет; есть: {', '.join(RECIPES)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    return fn(out_dir)


# ---------------------------------------------------------------- материалы и общие куски

WIDTH = 1280
SIZE = (1280, 720)
SCALE = f"scale={WIDTH}:-2"
# водяной знак релиз-группы (1XBET, правый верхний угол) после scale=1280 —
# убирается и у «было», и у «стало»: сравнивать надо приём, а не знак
DELOGO = "delogo=x=1096:y=14:w=170:h=54"
FACE_AT = "960"  # e15: крупный план лица — детали, тени, небо
SKY_AT = "600"  # e15: героиня на фоне неба — ровное поле для наезда
SHOWCASE_SEC = 8.0
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
GRADIENT = "gradients=s=1280x720:c0=0x24344d:c1=0xb86b3a:c2=0x2f6f5a:nb_colors=3:seed=7:speed=0.002"


def _need(path: Path, what: str) -> Path:
    if not path.exists():
        raise MissingMaterial(f"{what}: {path}")
    return path


def _rezero(rel: str) -> Path:
    return _need(projects_dir() / "anime_edits" / "sources" / "rezero_s4" / rel, "материал Re:Zero")


def _music(name: str) -> Path:
    return _need(resources_dir() / "music" / name, "трек из resources/music")


def _frame(src: Path, at: str, vf: str, out: Path) -> Path:
    run(["-ss", at, "-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "3", str(out)])
    return out


def _clip(src: Path, start: float, seconds: float, out: Path, *, vf: str = SCALE) -> Path:
    """Немой отрезок для галереи: короткий, 1280 по ширине."""
    run(
        [
            "-ss",
            f"{start:.3f}",
            "-i",
            str(src),
            "-t",
            f"{seconds:.3f}",
            "-vf",
            vf,
            "-an",
            *ENCODE,
            str(out),
        ]
    )
    return out


def _ass_filter(ass: Path) -> str:
    return f"ass=filename={filter_path(ass)}:fontsdir={filter_path(fonts_dir())}"


def _style(**overrides: object) -> CaptionStyle:
    base: dict[str, object] = {
        "id": "example",
        "name": "пример",
        "position": "bottom",
        "margin_pct": 12.0,
        "size_pct": 6.0,
        "fade_ms": 0,  # стоп-кадр берётся с нулевого кадра — появление ему мешает
    }
    base.update(overrides)
    return CaptionStyle(**base)  # type: ignore[arg-type]


def _words(text: str, *, start: float = 0.0, per_word: float = 0.5) -> list[Word]:
    out: list[Word] = []
    t = start
    for w in text.split():
        out.append(Word(w, t, t + per_word * 0.9))
        t += per_word
    return out


def _event(words: list[Word], end: float) -> CaptionEvent:
    return CaptionEvent(start=words[0].start, end=end, words=words, scene_id="s1")


def _still(inputs: list[str], ass: Path, out: Path, at: float = 0.0) -> Path:
    """Один кадр с субтитрами: первый кадр не раньше `at` секунды."""
    vf = f"{_ass_filter(ass)},select='gte(t,{at:.2f})'"
    run([*inputs, "-vf", vf, "-frames:v", "1", str(out)])
    return out


def _gradient_inputs(seconds: float) -> list[str]:
    return ["-f", "lavfi", "-i", f"{GRADIENT}:d={seconds:.1f}"]


def _wave_video(out: Path, lanes: list[tuple[Path, str, str]], seconds: float) -> Path:
    """Дорожки одна под другой, как в звуковом редакторе: волна всего отрезка
    целиком и бегущий курсор, в звуке — микс дорожек. lanes: (wav, цвет, подпись).

    Именно вся волна, а не «осциллограф» текущих 40 мс (showwaves): провал
    музыки под речью виден одним взглядом, слушать не обязательно."""
    font = filter_path(fonts_dir() / "PTSans-Bold.ttf")
    height = 720 // len(lanes)
    work = out.parent  # промежуточные файлы рядом, с именем результата в префиксе
    pics: list[Path] = []
    for i, (wav, color, _) in enumerate(lanes):
        pic = work / f"{out.stem}.lane{i}.png"
        wave = f"[0:a]showwavespic=s={WIDTH}x{height}:colors={color}"
        run(["-i", str(wav), "-filter_complex", wave, "-frames:v", "1", str(pic)])
        pics.append(pic)
    labels = ",".join(
        f"drawtext=fontfile={font}:text='{label}':x=16:y={i * height + 12}"
        ":fontsize=28:fontcolor=white@0.85"
        for i, (_, _, label) in enumerate(lanes)
    )
    stack = f"vstack=inputs={len(lanes)}," if len(lanes) > 1 else ""
    sheet = work / f"{out.stem}.sheet.png"
    run([*(a for p in pics for a in ("-i", str(p))), "-filter_complex",
         "".join(f"[{i}]" for i in range(len(pics))) + f"{stack}{labels},format=rgb24",
         "-frames:v", "1", str(sheet)])  # fmt: skip
    mix = work / f"{out.stem}.mix.wav"
    run([*(a for wav, _, _ in lanes for a in ("-i", str(wav))), "-filter_complex",
         "".join(f"[{i}:a]" for i in range(len(lanes)))
         + f"amix=inputs={len(lanes)}:normalize=0:duration=longest[a]",
         "-map", "[a]", "-t", f"{seconds:.3f}", "-c:a", "pcm_s16le", str(mix)])  # fmt: skip
    # курсор — белая полоска поверх листа; overlay умеет `t` в выражении, drawbox нет
    cursor = f"[0:v][1:v]overlay=x='t*{WIDTH}/{seconds:.3f}':y=0:eval=frame[v]"
    run(["-loop", "1", "-framerate", "25", "-i", str(sheet),
         "-f", "lavfi", "-i", "color=c=white@0.9:s=3x720:r=25", "-i", str(mix),
         "-filter_complex", cursor, "-map", "[v]", "-map", "2:a",
         "-t", f"{seconds:.3f}", *ENCODE, "-c:a", "aac", "-b:a", "128k", str(out)])  # fmt: skip
    return out


def _wav(
    src: Path, out: Path, *extra: str, seconds: float = SHOWCASE_SEC, loop: bool = False
) -> Path:
    pre = ["-stream_loop", "-1"] if loop else []
    run([*pre, "-i", str(src), "-t", f"{seconds:.3f}", *extra, "-ac", "2", "-ar", "44100",
         "-c:a", "pcm_s16le", str(out)])  # fmt: skip
    return out


# ---------------------------------------------------------------- картинка


@recipe("anime_punch")
def _anime_punch(out: Path) -> Rendered:
    """Тени 18→1, света 240→250 кривой, насыщенность ×1.08 после кривой."""
    src = _rezero("source/e15.mp4")
    base = f"{SCALE},{DELOGO}"
    punch = base + ",curves=all='0/0 0.072/0.005 0.94/0.979 1/1',eq=saturation=1.08"
    return Rendered(
        before=_frame(src, FACE_AT, base, out / "before.jpg"),
        after=_frame(src, FACE_AT, punch, out / "after.jpg"),
    )


@recipe("delogo_burned_watermark")
def _delogo(out: Path) -> Rendered:
    """Правый верхний угол кадра крупно: знак есть → знака нет."""
    src = _rezero("source/e15.mp4")
    corner = "crop=640:360:640:0"
    return Rendered(
        before=_frame(src, FACE_AT, f"{SCALE},{corner}", out / "before.jpg"),
        after=_frame(src, FACE_AT, f"{SCALE},{DELOGO},{corner}", out / "after.jpg"),
    )


@recipe("slow_zoom_static")
def _slow_zoom(out: Path) -> Rendered:
    """Неподвижный кадр 4 с: как есть → наезд на 6% через апскейл lanczos."""
    src = _rezero("source/e15.mp4")
    still = _frame(src, SKY_AT, f"{SCALE},{DELOGO}", out / "still.png")
    frames = 96
    run(["-loop", "1", "-framerate", "24", "-i", str(still), "-frames:v", str(frames),
         "-vf", "scale=1280:720,format=yuv420p", *ENCODE, str(out / "before.mp4")])  # fmt: skip
    zoom = (
        "scale=2560:1440:flags=lanczos,"
        f"zoompan=z='1+0.06*on/{frames}':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        ":s=1280x720:fps=24,format=yuv420p"
    )
    run(["-i", str(still), "-vf", zoom, "-frames:v", str(frames), *ENCODE, str(out / "after.mp4")])
    return Rendered(before=out / "before.mp4", after=out / "after.mp4")


@recipe("keep_native_fps")
def _keep_native_fps(out: Path) -> Rendered:
    """Панорама по кадру 4 с: пересчитанная в 30 к/с — каждый четвёртый кадр
    удвоен, движение спотыкается; в родных 24 — ровная. Панорама искусственная
    (окно едет по увеличенному кадру), кадр настоящий."""
    src = _rezero("source/e15.mp4")
    still = _frame(
        src, SKY_AT, f"scale=1600:-2,{DELOGO.replace('x=1096', 'x=1370')}", out / "still.png"
    )
    pan = "crop=1280:720:x='min(iw-1280,t*80)':y=(ih-720)/2"
    run(["-loop", "1", "-framerate", "24", "-i", str(still), "-t", "4",
         "-vf", f"{pan},fps=30,format=yuv420p", *ENCODE, str(out / "before.mp4")])  # fmt: skip
    run(["-loop", "1", "-framerate", "24", "-i", str(still), "-t", "4",
         "-vf", f"{pan},format=yuv420p", *ENCODE, str(out / "after.mp4")])  # fmt: skip
    return Rendered(before=out / "before.mp4", after=out / "after.mp4")


@recipe("scene_recut_realtime")
def _scene_recut(out: Path) -> Rendered:
    """Первые секунды нашего эдита: одна сцена, паузы вынуты, скорость ×1."""
    src = _rezero("edits/e15_training_season.mp4")
    return Rendered(after=_clip(src, 0.0, SHOWCASE_SEC, out / "after.mp4"))


@recipe("smart_placement")
def _smart_placement(out: Path) -> Rendered:
    """Кадр эдита «боль»: надпись ушла в свободный угол мимо двух лиц."""
    src = _rezero("edits/e14_forgotten.mp4")
    return Rendered(after=_frame(src, "3.0", SCALE, out / "after.jpg"))


def _single_word_frame(out: Path) -> Rendered:
    """Кадр нашего эдита: одно слово по центру поверх неба, покрашенное
    разностью с кадром. Один и тот же кадр служит двум приёмам — размеру и
    месту (captions) и способу окраски (caption_color)."""
    src = _rezero("edits/e15_training_season.mp4")
    return Rendered(after=_frame(src, "13.3", SCALE, out / "after.jpg"))


RECIPES["single_word_center"] = _single_word_frame
RECIPES["difference_blend"] = _single_word_frame


@recipe("social_captions")
def _karaoke(out: Path) -> Rendered:
    """Шортс из подкаста: сцена до субтитров → финал с караоке, те же 4 секунды."""
    ep = episode_dir("podcast_clips", "src01_h08")
    scene = _need(ep / "05_assemble" / "s01.mp4", "сцена шортса без субтитров")
    final = _need(ep / "05_assemble" / "final.mp4", "финал шортса")
    vf = "scale=-2:720"
    return Rendered(
        before=_clip(scene, 3.0, 4.0, out / "before.mp4", vf=vf),
        after=_clip(final, 3.0, 4.0, out / "after.mp4", vf=vf),
    )


@recipe("overlay_plates")
def _overlay(out: Path) -> Rendered:
    """Кадр эпизода: чистый → с плашкой-выводом сверху, источником и субтитром."""
    images = sorted((episode_dir("doc_channel", "ep001") / "02_images").glob("s0*_v01.png"))
    if not images:
        raise MissingMaterial("кадры эпизода doc_channel/ep001")
    frame = images[0]
    style = _style()
    words = _words("Иблис не был ангелом", per_word=0.5)
    overlays = [
        OverlayEvent(
            0.0, 3.0, "Иблис был джинном, а не ангелом", "caption", frozenset({"джинном"})
        ),
        OverlayEvent(0.0, 3.0, "Коран 18:50", "source"),
        OverlayEvent(0.0, 3.0, "ПУНКТ 2", "label"),
    ]
    ass = out / "overlay.ass"
    ass.write_text(
        build_ass([_event(words, 3.0)], style, size=SIZE, overlays=overlays), encoding="utf-8"
    )
    return Rendered(
        before=_image_frame(frame, out / "before.png"),
        after=_image_frame(frame, out / "after.png", ass=ass),
    )


def _image_frame(image: Path, out: Path, *, ass: Path | None = None) -> Path:
    """Картинка → кадр 1280×720, при желании с субтитрами поверх."""
    vf = "scale=1280:720" + (f",{_ass_filter(ass)}" if ass else "")
    run(
        [
            "-loop",
            "1",
            "-framerate",
            "24",
            "-i",
            str(image),
            "-t",
            "1",
            "-vf",
            vf,
            "-frames:v",
            "1",
            str(out),
        ]
    )
    return out


# ---------------------------------------------------------------- текст: шрифт, цвет, анимация

PANGRAM = "Съешь же ещё этих мягких французских булок, да выпей чаю"
LATIN = "The quick brown fox jumps over the lazy dog"


def _font_sample(out: Path, **font: object) -> Rendered:
    """Панграмма кириллицей кеглем субтитров внизу, латиница плашкой сверху."""
    style = _style(mode="phrase", **font)
    words = _words(PANGRAM)
    ass = out / "font.ass"
    ass.write_text(
        build_ass(
            [_event(words, 2.0)],
            style,
            size=SIZE,
            overlays=[OverlayEvent(0.0, 2.0, LATIN, "caption")],
        ),
        encoding="utf-8",
    )
    return Rendered(after=_still(_gradient_inputs(2.0), ass, out / "after.png"))


@recipe("pt_sans")
def _pt_sans(out: Path) -> Rendered:
    return _font_sample(out, font="PT Sans", font_file="PTSans-Bold.ttf", bold=True)


@recipe("russo_one")
def _russo_one(out: Path) -> Rendered:
    return _font_sample(
        out, font="Russo One", font_file="RussoOne-Regular.ttf", bold=False, uppercase=True
    )


def _color_sample(out: Path, **colors: object) -> Rendered:
    """Караоке на второй секунде: два слова уже произнесены — видны оба цвета."""
    style = _style(mode="karaoke", **colors)
    words = _words("каждое слово подсвечивается по очереди", per_word=0.5)
    ass = out / "color.ass"
    ass.write_text(build_ass([_event(words, 3.0)], style, size=SIZE), encoding="utf-8")
    return Rendered(after=_still(_gradient_inputs(3.0), ass, out / "after.png", at=1.1))


@recipe("white_yellow")
def _white_yellow(out: Path) -> Rendered:
    return _color_sample(
        out, color="#FFFFFF", highlight="#FFD400", outline_color="#000000", outline=3.0
    )


@recipe("dark_plate")
def _dark_plate(out: Path) -> Rendered:
    """Кадр шортса со светлой футболкой: обводка → тёмная подложка. Материал
    тот, на котором приём и замерен, — белый текст на белом там сливается."""
    clip = _need(
        episode_dir("podcast_clips", "src01_h08") / "03_video" / "s01_reframed.mp4",
        "клип шортса podcast_clips/src01_h08",
    )
    words = _words("НА СВЕТЛОМ ФОНЕ", per_word=0.5)
    made: dict[str, Path] = {}
    for name, box in (("before", 0.0), ("after", 0.6)):
        style = _style(
            font="Russo One", font_file="RussoOne-Regular.ttf", bold=False, uppercase=True,
            size_pct=6.5, outline=5.0, box_opacity=box, margin_pct=22.0, side_margin_pct=15.0,
        )  # fmt: skip
        ass = out / f"{name}.ass"
        ass.write_text(build_ass([_event(words, 2.0)], style, size=(608, 1080)), encoding="utf-8")
        made[name] = _frame(clip, "5", f"scale=608:-2,{_ass_filter(ass)}", out / f"{name}.jpg")
        ass.unlink(missing_ok=True)
    return Rendered(before=made["before"], after=made["after"])


@recipe("cream_on_glass")
def _cream_on_glass(out: Path) -> Rendered:
    return _color_sample(
        out, color="#F5E6C8", highlight="#FFD400", box_opacity=0.6, box_color="#000000"
    )


def _animation_clip(out: Path, **anim: object) -> Rendered:
    """Три секунды: слово за словом на градиенте — видно, как появляется каждое."""
    style = _style(mode="karaoke", position="center", margin_pct=0.0, size_pct=7.0, **anim)
    words = _words("КАЖДОЕ СЛОВО ВЫХОДИТ ПО ОЧЕРЕДИ", start=0.3, per_word=0.6)
    ass = out / "anim.ass"
    ass.write_text(build_ass([_event(words, 3.0)], style, size=SIZE), encoding="utf-8")
    run(
        [
            *_gradient_inputs(3.0),
            "-vf",
            _ass_filter(ass),
            "-t",
            "3",
            *ENCODE,
            str(out / "after.mp4"),
        ]
    )
    return Rendered(after=out / "after.mp4")


@recipe("pop")
def _pop(out: Path) -> Rendered:
    return _animation_clip(
        out, font="Russo One", font_file="RussoOne-Regular.ttf", bold=False, uppercase=True,
        animation="pop", pop_scale=124, pop_ms=260, pop_undershoot=94, side_margin_pct=10.0,
    )  # fmt: skip


@recipe("words_in_place")
def _words_in_place(out: Path) -> Rendered:
    """Реплика разложена заранее: слова проявляются, строка не двигается."""
    return _animation_clip(
        out, font="Russo One", font_file="RussoOne-Regular.ttf", bold=False, uppercase=True,
        animation="reveal", reveal_ms=220, reveal_rise_pct=35, side_margin_pct=10.0,
    )  # fmt: skip


@recipe("line_grows")
def _line_grows(out: Path) -> Rendered:
    """Тот же текст и те же числа, что у `words_in_place`: в галерее сравнивают
    именно движение строки, а не шрифт с цветом."""
    return _animation_clip(
        out, font="Russo One", font_file="RussoOne-Regular.ttf", bold=False, uppercase=True,
        animation="grow", reveal_ms=220, reveal_rise_pct=35, side_margin_pct=10.0,
    )  # fmt: skip


@recipe("karaoke_recolor")
def _karaoke_recolor(out: Path) -> Rendered:
    return _animation_clip(out, animation="none", uppercase=True)


# ---------------------------------------------------------------- звук: волна дорожек

MUSIC = "0xFFD400"
VOICE = "0xFFFFFF"
SOURCE = "0x7FD4FF"
BED = "0x9AE6A4"
SFX = "0xFF8A65"
# ep001: речь кончается на 139.4 с, дальше 16.6 с цифрового нуля. Окно берётся
# так, чтобы в кадр попал сам обрыв, — витрина показывает переход, а не дыру
HOLE_AT = "136.0"


def _speech(out: Path) -> Path:
    """Две реплики озвучки эпизода подряд — живая речь с паузами."""
    audio = episode_dir("doc_channel", "ep001") / "04_audio"
    parts = [_need(audio / f"s0{i}.mp3", "озвучка эпизода doc_channel/ep001") for i in (1, 2)]
    run(["-i", str(parts[0]), "-i", str(parts[1]), "-filter_complex",
         "[0:a][1:a]concat=n=2:v=0:a=1,aformat=sample_rates=44100:channel_layouts=stereo[a]",
         "-map", "[a]", "-t", f"{SHOWCASE_SEC:.1f}", "-c:a", "pcm_s16le", str(out)])  # fmt: skip
    return out


def _hole(out: Path) -> Path:
    """Дорожка готового ep001 на входе в паузу: речь обрывается в цифровой ноль."""
    final = episode_dir("doc_channel", "ep001") / "05_assemble" / "final.mp4"
    _need(final, "готовый ролик doc_channel/ep001")
    run(["-ss", HOLE_AT, "-i", str(final), "-t", f"{SHOWCASE_SEC:.3f}", "-vn",
         "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(out)])  # fmt: skip
    return out


def _mute(out: Path) -> Path:
    """Немая дорожка на всю витрину: пустая полоса «было» напротив полной «стало».

    Без неё у «было» на одну дорожку меньше, полосы становятся вдвое выше, и
    глаз читает разницу в масштабе, а не в приёме."""
    run(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={SHOWCASE_SEC:.3f}",
         "-c:a", "pcm_s16le", str(out)])  # fmt: skip
    return out


@recipe("ambient_bed")
def _ambient_bed(out: Path) -> Rendered:
    """Речь ep001 на входе в дыру 16.6 с: пустая полоса → подложка до края.

    Подложка в витрине нарочно громче рабочих −20 дБ (там она рисуется
    волоском): показываем, что фон ЕСТЬ, а не каким он должен быть в миксе."""
    speech = _hole(out / "speech.wav")
    bed = _wav(_music("soft_pad.wav"), out / "bed.wav", "-af", "volume=0.5", loop=True)
    silent = _mute(out / "silent.wav")
    return Rendered(
        before=_wave_video(
            out / "before.mp4",
            [(speech, VOICE, "речь"), (silent, BED, "подложки нет — цифровой ноль")],
            SHOWCASE_SEC,
        ),
        after=_wave_video(
            out / "after.mp4",
            [(speech, VOICE, "речь"), (bed, BED, "подложка (в миксе −20 дБ к голосу)")],
            SHOWCASE_SEC,
        ),
    )


@recipe("sfx_on_cut")
def _sfx_on_cut(out: Path) -> Rendered:
    """Склейка в тишине: пусто → удар ровно в кадр смены (замерено +48.8 дБ к фону)."""
    speech = _hole(out / "speech.wav")
    hit = out / "hit.wav"
    src = _need(resources_dir() / "sfx" / "impact_deep.wav", "звук из resources/sfx")
    run(["-i", str(src), "-af",
         f"volume=0.7,adelay=5600|5600,apad=whole_dur={SHOWCASE_SEC:.3f}",
         "-t", f"{SHOWCASE_SEC:.3f}", "-ac", "2", "-ar", "44100",
         "-c:a", "pcm_s16le", str(hit)])  # fmt: skip
    silent = _mute(out / "silent.wav")
    return Rendered(
        before=_wave_video(
            out / "before.mp4",
            [(speech, VOICE, "речь"), (silent, SFX, "склейка без звука")],
            SHOWCASE_SEC,
        ),
        after=_wave_video(
            out / "after.mp4",
            [(speech, VOICE, "речь"), (hit, SFX, "удар на склейке")],
            SHOWCASE_SEC,
        ),
    )


@recipe("duck_music_under_dialogue")
def _duck(out: Path) -> Rendered:
    """Музыка под речью: ровная → проседает там, где говорят (ключ — сама речь)."""
    speech = _speech(out / "speech.wav")
    # музыка нарочно громкая: в витрине важен видимый провал, а не баланс микса
    music = _wav(_music("drive_dark.wav"), out / "music.wav", "-af", "volume=1.6", loop=True)
    ducked = out / "ducked.wav"
    run(["-i", str(music), "-i", str(speech), "-filter_complex",
         "[0:a][1:a]sidechaincompress=threshold=0.012:ratio=20:attack=10:release=300[a]",
         "-map", "[a]", "-c:a", "pcm_s16le", str(ducked)])  # fmt: skip
    return Rendered(
        before=_wave_video(
            out / "before.mp4", [(music, MUSIC, "музыка"), (speech, VOICE, "речь")], SHOWCASE_SEC
        ),
        after=_wave_video(
            out / "after.mp4", [(ducked, MUSIC, "музыка"), (speech, VOICE, "речь")], SHOWCASE_SEC
        ),
    )


@recipe("source_audio_under_music")
def _source_under_music(out: Path) -> Rendered:
    """Музыка одна → музыка плюс звук исходника на 0.3: крики и удары остаются."""
    src = _rezero("source/e15.mp4")
    source = out / "source.wav"
    run(["-ss", FACE_AT, "-i", str(src), "-t", f"{SHOWCASE_SEC:.1f}", "-vn", "-af", "volume=0.3",
         "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(source)])  # fmt: skip
    music = _wav(_music("drive_dark.wav"), out / "music.wav", "-af", "volume=0.5", loop=True)
    return Rendered(
        before=_wave_video(out / "before.mp4", [(music, MUSIC, "музыка")], SHOWCASE_SEC),
        after=_wave_video(
            out / "after.mp4",
            [(music, MUSIC, "музыка"), (source, SOURCE, "звук исходника")],
            SHOWCASE_SEC,
        ),
    )


@recipe("fade_out_soft")
def _fade_out(out: Path) -> Rendered:
    """Конец ролика: обрыв → последние 0.6 с уходят в ноль."""
    track = _music("drive_dark.wav")
    hard = _wav(track, out / "hard.wav", seconds=6.0, loop=True)
    soft = _wav(track, out / "soft.wav", "-af", "afade=t=out:st=5.4:d=0.6", seconds=6.0, loop=True)
    return Rendered(
        before=_wave_video(out / "before.mp4", [(hard, MUSIC, "музыка")], 6.0),
        after=_wave_video(out / "after.mp4", [(soft, MUSIC, "музыка")], 6.0),
    )


@recipe("cut_on_beat")
def _cut_on_beat(out: Path) -> Rendered:
    """Петля 100 BPM, доля каждые 0.6 с: обрыв между долями → ровно на доле + 0.2 с тишины."""
    track = _music("drive_dark.wav")
    off = _wav(track, out / "off.wav", "-af", "apad=pad_dur=0.2", seconds=5.83, loop=True)
    on = _wav(track, out / "on.wav", "-af", "apad=pad_dur=0.2", seconds=6.0, loop=True)
    return Rendered(
        before=_wave_video(out / "before.mp4", [(off, MUSIC, "музыка")], 6.1),
        after=_wave_video(out / "after.mp4", [(on, MUSIC, "музыка")], 6.3),
    )


@recipe("let_track_end")
def _let_track_end(out: Path) -> Rendered:
    """Последние секунды трека, который кончается сам: затухания не нужно."""
    track = _music("black_vortex.mp3")
    tail = out / "tail.wav"
    run(
        [
            "-sseof",
            "-8",
            "-i",
            str(track),
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(tail),
        ]
    )
    return Rendered(after=_wave_video(out / "after.mp4", [(tail, MUSIC, "музыка")], SHOWCASE_SEC))
