"""Примеры ресурсов: «как это выглядит» и «как это звучит».

Стиль субтитров, названный «Кинетические (шортсы)», глазу не говорит ничего:
ни имя, ни тридцать чисел не показывают, как текст ведёт себя на экране.
Пример — короткий клип, собранный ТЕМ ЖЕ рендерером, что и финал ролика
(`core/captions` → ASS → ffmpeg): витрину не рисуют руками, поэтому она не
может соврать про стиль, и пересобирается одной командой, когда стиль
поправили.

Живёт рядом с ресурсом: `<вид>/<id>/example.<расширение>` — та же папка, что у
портфолио персонажа. Пример есть не у всех видов, и это намеренно: у музыки и
звуков витрина — сам файл, у персонажей — портфолио, у визуального стиля кадр
стоит денег (платная генерация), поэтому его здесь нет.

Фон примера — синтетический градиент, а не кадр из проекта: пример должен
собираться на любой машине, где есть ffmpeg и шрифты репозитория, а материал
конкретного эпизода может быть удалён.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from videoforge.core.captions import CaptionEvent, OverlayEvent, Word, build_ass, group_words
from videoforge.core.ffmpeg import filter_path, run
from videoforge.core.library import EXAMPLE_KINDS
from videoforge.core.resources import CaptionStyle, Resource, Voice
from videoforge.paths import fonts_dir

EXAMPLE_STEM = "example"

# формат кадра витрины → размер. Отступы субтитров считаются от высоты кадра,
# поэтому один и тот же стиль в шортсе и в горизонтальном ролике выглядит
# по-разному: показываем в том формате, для которого стиль написан
FORMATS: dict[str, tuple[int, int]] = {
    "9:16": (608, 1080),
    "16:9": (1280, 720),
    "1:1": (900, 900),
}

# фраза витрины: шесть слов — видно и перенос реплики по правилам стиля, и
# подсветку слова; текст нейтральный, чтобы не тянуть за собой нишу
SAMPLE_TEXT = "Так этот стиль выглядит на экране"
SAMPLE_OVERLAY = "Плашка сверху с ключевым словом"
SAMPLE_OVERLAY_EMPHASIS = frozenset({"ключевым"})
# Шрифт без кириллицы (мультяшные латинские) русскую фразу не нарисует: libass
# молча подставит запасной, и витрина покажет ЧУЖОЙ шрифт — три разных стиля
# вышли на ней одинаковыми (19.09.2026). Такому стилю — фраза латиницей
SAMPLE_TEXT_LATIN = "This is how the style looks on screen"
SAMPLE_OVERLAY_LATIN = "A label on top with a key word"
SAMPLE_OVERLAY_EMPHASIS_LATIN = frozenset({"key"})
SAMPLE_SPEECH = "Так звучит этот голос: обычная фраза из ролика, без спешки."

PER_WORD = 0.52  # секунд на слово: примерно темп edge-tts (1.9 слова/с)
TAIL_SEC = 0.6  # сколько реплика висит после последнего слова
GRADIENT = "gradients=c0=0x24344d:c1=0xb86b3a:c2=0x2f6f5a:nb_colors=3:seed=7:speed=0.002"
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]


@dataclass(frozen=True)
class ResourceExample:
    """Витрина ресурса: один файл. «Было → стало» здесь не бывает — сравнивать
    ресурс не с чем, он и есть выбранное."""

    path: Path
    kind: str  # image | video | audio


@dataclass(frozen=True)
class Variant:
    """Проба подачи рядом с витриной голоса: та же фраза с другой скоростью или
    высотой. Файл — `example.<suffix>.mp3`, подпись — для пульта."""

    suffix: str  # rate-10, pitch+4Hz — хвост имени файла
    field: str  # rate | pitch — что переопределено у голоса
    value: str  # значение в формате edge-tts: "-10%", "+4Hz"
    label: str


# Пробы подачи голоса (`resource gen-example voice <id> --variants`). Разницу
# между «живой подачей» и «кривляньем» слышит только ухо, поэтому первый шаг
# записи features/voice_prosody — витрина на выбор, а не параметр в конвейере.
# Шаг ±10% скорости — граница заметного без «ускоренной записи»; ±4 Hz высоты —
# у edge-tts ±10 Hz уже звучит другим человеком. Скорость в имени без «%»:
# знак процента в адресе файла кодируется, Hz — нет
VOICE_VARIANTS: tuple[Variant, ...] = (
    Variant("rate-10", "rate", "-10%", "скорость −10%"),
    Variant("rate+10", "rate", "+10%", "скорость +10%"),
    Variant("pitch-4Hz", "pitch", "-4Hz", "высота −4 Hz"),
    Variant("pitch+4Hz", "pitch", "+4Hz", "высота +4 Hz"),
)


@dataclass(frozen=True)
class ExampleVariant:
    path: Path
    label: str


class NoRecipe(RuntimeError):
    """У этого вида ресурсов примера не бывает — см. модуль."""


class NeedsSynth(RuntimeError):
    """Пример надо озвучить, а провайдер не передан (`core` их не импортирует)."""


# синтез речи приходит снаружи: слой `core` не знает про провайдеров
Synth = Callable[[str, Voice, Path], None]
Recipe = Callable[["Resource", Path, Synth | None], Path]


def example_dir(obj: Resource) -> Path:
    """Папка ресурса — та же, где лежит портфолио визуального ресурса."""
    return obj._base / obj.id


def find_example(obj: Resource) -> ResourceExample | None:
    folder = example_dir(obj)
    if not folder.is_dir():
        return None
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.stem == EXAMPLE_STEM and path.suffix.lower() in EXAMPLE_KINDS:
            return ResourceExample(path=path, kind=EXAMPLE_KINDS[path.suffix.lower()])
    return None


def render_example(kind: str, obj: Resource, *, synth: Synth | None = None) -> Path:
    """Собрать витрину ресурса заново. Старый пример заменяется: он один."""
    recipe = RECIPES.get(kind)
    if recipe is None:
        raise NoRecipe(
            f"у вида {kind!r} примера не бывает; есть: {', '.join(RECIPES)} "
            "(у музыки и звуков витрина — сам файл, у персонажей — портфолио)"
        )
    folder = example_dir(obj)
    folder.mkdir(parents=True, exist_ok=True)
    existing = find_example(obj)
    out = recipe(obj, folder, synth)
    if existing and existing.path != out:
        existing.path.unlink(missing_ok=True)  # пример один: старый формат не залёживается
    return out


# ---------------------------------------------------------------- субтитры


def _words(text: str) -> list[Word]:
    out: list[Word] = []
    t = 0.3  # четверть секунды до первого слова: видно, как реплика появляется
    for w in text.split():
        out.append(Word(w, t, t + PER_WORD * 0.9))
        t += PER_WORD
    return out


def _events(style: CaptionStyle, words: list[Word]) -> tuple[list[CaptionEvent], float]:
    """Реплики по правилам самого стиля: витрина показывает и то, как стиль
    рвёт строку, а не только цвет."""
    groups = group_words(
        words,
        max_words=style.max_words,
        max_chars=style.max_chars,
        max_gap_sec=style.max_gap_sec,
    )
    events: list[CaptionEvent] = []
    for i, group in enumerate(groups):
        end = groups[i + 1][0].start if i + 1 < len(groups) else group[-1].end + TAIL_SEC
        events.append(CaptionEvent(start=group[0].start, end=end, words=group, scene_id="s1"))
    return events, (events[-1].end if events else TAIL_SEC)


def has_cyrillic(font: Path) -> bool:
    """Есть ли в шрифте кириллица: отсутствующую букву шрифт рисует тем же
    знаком-заглушкой, что и заведомо пустой код. Файла нет — считаем, что есть:
    о пропавшем шрифте скажет рендер, а не выбор фразы."""
    from PIL import ImageFont

    try:
        face = ImageFont.truetype(str(font), 64)
    except OSError:
        return True
    return bytes(face.getmask("Ж")) != bytes(face.getmask("￿"))


def _caption_example(obj: Resource, folder: Path, _synth: Synth | None) -> Path:
    assert isinstance(obj, CaptionStyle)
    width, height = FORMATS.get(obj.preview_format, FORMATS["9:16"])
    cyrillic = has_cyrillic(obj.font_path())
    events, total = _events(obj, _words(SAMPLE_TEXT if cyrillic else SAMPLE_TEXT_LATIN))
    overlays = [
        OverlayEvent(0.0, total, SAMPLE_OVERLAY, "caption", SAMPLE_OVERLAY_EMPHASIS)
        if cyrillic
        else OverlayEvent(
            0.0, total, SAMPLE_OVERLAY_LATIN, "caption", SAMPLE_OVERLAY_EMPHASIS_LATIN
        ),
    ]
    ass = folder / f"{EXAMPLE_STEM}.ass"
    ass.write_text(
        build_ass(events, obj, size=(width, height), overlays=overlays), encoding="utf-8"
    )
    out = folder / f"{EXAMPLE_STEM}.mp4"
    run([
        "-f", "lavfi", "-i", f"{GRADIENT}:s={width}x{height}:d={total:.2f}",
        "-vf", f"ass=filename={filter_path(ass)}:fontsdir={filter_path(fonts_dir())}",
        "-t", f"{total:.2f}", *ENCODE, str(out),
    ])  # fmt: skip
    ass.unlink(missing_ok=True)  # промежуточный файл: в витрине лежит только клип
    return out


# ---------------------------------------------------------------- голос


def _voice_example(obj: Resource, folder: Path, synth: Synth | None) -> Path:
    assert isinstance(obj, Voice)
    if synth is None:
        raise NeedsSynth("пример голоса нужно озвучить: вызывай через CLI (resource gen-example)")
    out = folder / f"{EXAMPLE_STEM}.mp3"
    synth(SAMPLE_SPEECH, obj, out)
    return out


def _variant_path(folder: Path, variant: Variant) -> Path:
    return folder / f"{EXAMPLE_STEM}.{variant.suffix}.mp3"


def render_variants(kind: str, obj: Resource, *, synth: Synth | None = None) -> list[Path]:
    """Пробы подачи рядом с витриной: та же фраза, что в `example.mp3`, с
    каждой настройкой из VOICE_VARIANTS. Только у голоса: у стиля субтитров
    «варианты» — это другие стили, и они лежат отдельными ресурсами."""
    if kind != "voices" or not isinstance(obj, Voice):
        raise NoRecipe(f"пробы подачи бывают только у голоса, а не у вида {kind!r}")
    if synth is None:
        raise NeedsSynth("пробы голоса нужно озвучить: вызывай через CLI (resource gen-example)")
    folder = example_dir(obj)
    folder.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for variant in VOICE_VARIANTS:
        path = _variant_path(folder, variant)
        synth(SAMPLE_SPEECH, obj.model_copy(update={variant.field: variant.value}), path)
        out.append(path)
    return out


def find_variants(obj: Resource) -> list[ExampleVariant]:
    """Готовые пробы подачи в порядке VOICE_VARIANTS; нет файла — нет и пробы."""
    folder = example_dir(obj)
    return [
        ExampleVariant(path=path, label=variant.label)
        for variant in VOICE_VARIANTS
        if (path := _variant_path(folder, variant)).is_file()
    ]


RECIPES: dict[str, Recipe] = {
    "caption_styles": _caption_example,
    "voices": _voice_example,
}
