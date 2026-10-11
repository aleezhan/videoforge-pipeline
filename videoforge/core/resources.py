"""Ресурсы: персонажи, места, предметы, стили, голоса, ниши.

Два уровня поиска:
- глобальные — `resources/<kind>/<id>.yaml` (персонаж может играть в разных проектах);
- проектные — `projects/<проект>/resources/<kind>/<id>.yaml` (каст конкретной
  серии: второстепенные герои, места, предметы-фишки). При совпадении id
  проектный побеждает глобальный.

Визуальные ресурсы (персонаж/место/предмет) держат «портфолио»: картинки из
КОРНЯ папки `…/<kind>/<id>/` подхватываются как референсы автоматически —
это ядро, оно идёт в каждую сцену. Подпапки (`_candidates/`, `looks/`,
`archive/`) не подхватываются: там кандидаты и образы, которые к сцене
подключают явно. Явный список `reference_images` в YAML, если задан,
побеждает автопоиск (куратор выбрал кадры сам).
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr

from videoforge.core.storyboard import ResourceRefs
from videoforge.paths import resources_dir

log = logging.getLogger("videoforge.resources")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


class Resource(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    # Приёмы библиотеки, из которых собран ресурс: "<точка решения>/<id>"
    # (`caption_color/white_yellow`). Библиотека — меню: там условия и замеры,
    # по которым выбирают. Ресурс — заказ: именованная сборка выбранного, на
    # которую ссылаются проект и раскадровка. Связь двусторонняя, и обе стороны
    # показывает пульт: у ресурса — из чего собран, у приёма — где применён.
    # Ничего не проверяет и никуда не подставляется: это происхождение, не
    # исполнение (иначе стиль пришлось бы собирать из приёмов на рендере).
    techniques: list[str] = Field(default_factory=list)
    _base: Path = PrivateAttr(default=Path("."))  # папка, откуда загружен YAML
    _scope: str = PrivateAttr(default="global")  # global | project


class VisualResource(Resource):
    """То, что должно выглядеть одинаково во всех эпизодах: персонаж, место, предмет."""

    prompt_block: str = ""  # вставляется в промпт картинки, когда ресурс в касте сцены
    reference_images: list[str] = Field(default_factory=list)  # явный выбор кадров (относит. YAML)
    checklist: list[str] = Field(default_factory=list)  # что проверять на каждом кадре

    def folder(self) -> Path:
        return self._base / self.id

    def reference_paths(self) -> list[Path]:
        if self.reference_images:
            paths: list[Path] = []
            for rel in self.reference_images:
                p = self._base / rel
                if p.exists():
                    paths.append(p)
                else:
                    log.warning("референс %s не найден: %s", self.id, p)
            return paths
        folder = self.folder()
        if folder.is_dir():
            return sorted(
                p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            )
        return []


class LoraRef(BaseModel):
    """Обученные веса персонажа для художника с LoRA (ComfyUI на своей карте).

    Файл — относительно папки вида (`amir/lora/amir_klein4b_v2.safetensors`).
    Когда художник с LoRA, в промпт идёт `trigger`, а не паспорт: паспорт лица
    тянет лицо в кадр и ломает ракурсы «со спины», «профиль» (замер 08.09.2026)."""

    file: str
    trigger: str = ""
    base: str = "klein4b"  # для какой базовой модели обучена
    strength: float = 1.0


class Character(VisualResource):
    lora: LoraRef | None = None

    def lora_path(self) -> Path | None:
        return self._base / self.lora.file if self.lora else None


class Location(VisualResource):
    pass


class Prop(VisualResource):
    pass


class Style(Resource):
    prompt_prefix: str = ""
    prompt_suffix: str = ""
    negative: str = ""
    motion_hint: str = ""  # добавляется к motion_prompt
    checklist: list[str] = Field(default_factory=list)
    # Фразы, которых в ПРОМПТЕ сцены быть не должно — как написаны, словом
    # целиком. Страж до генерации (`stages/style_conflict.py`): планнер и
    # стадия картинок падают до платного вызова. `negative` уходит художнику и
    # проверяется по словам только через карту синонимов очевидных терминов;
    # сюда — запреты этого стиля, которых карта не знает («crucifix», «wine
    # glass»). Художнику не подставляется — это не негатив
    forbid: list[str] = Field(default_factory=list)


class Voice(Resource):
    provider: str = "edge"
    voice: str = "ru-RU-DmitryNeural"
    # подача по умолчанию в формате edge-tts; сцена вправе переопределить
    # (`Scene.voice`). WORDS_PER_SEC замерен на rate "+0%" — см. features/voice_prosody
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    language: str = "ru"
    # озвучивать фразами (несколько сцен одним вызовом, резка по словам), а не
    # по сцене: интонация не начинается заново на каждой склейке. Ложь —
    # прежнее поведение, готовая озвучка голосов без поля не перегоняется
    phrase: bool = False


class Niche(Resource):
    audience: str = ""
    tone: str = ""
    hooks: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class CaptionStyle(Resource):
    """Как выглядят субтитры: шрифт, размер, позиция, подсветка слова.

    Ресурс, а не константа: документальному каналу нужен небольшой текст в нижней
    трети, шортсам — крупные слова по центру. Сборка превращает стиль в ASS-файл;
    тайминги слов приходят из озвучки, стиль их только рисует. Цвета — `#RRGGBB`."""

    font: str = "PT Sans"  # семейство, как его знает libass (имя внутри файла шрифта)
    font_file: str = "PTSans-Bold.ttf"  # файл в videoforge/assets/fonts/ или абсолютный путь
    # кегль в процентах КОРОТКОЙ стороны кадра: 5.2% → 56 px и на 1920×1080, и на
    # 1080×1920 — так один стиль читается одинаково в горизонтали и вертикали
    size_pct: float = 5.2
    bold: bool = True
    uppercase: bool = False
    color: str = "#FFFFFF"  # слова, которые ещё не произнесены
    highlight: str = "#FFD400"  # произносимое слово (караоке); равен color → без подсветки
    # Градиент заливки сверху вниз: буква вверху своего цвета (`color`,
    # `highlight`, акцент), внизу — этого. Пусто — заливка ровная. ASS градиента
    # не умеет: реплика рисуется полосами `\clip`, у каждой свой цвет
    # (`captions.gradient_lines`), поэтому градиент считается по ОДНОЙ строке —
    # стилю с ним ставят `max_words` 1–2, у перенесённой реплики верхняя строка
    # останется верхним цветом. Замер erickerhead 19.09.2026: жёлтый → оранжевый
    gradient_to: str = ""
    # Знаки препинания на краях слова («RICK,» → «RICK»). Стилю «одно слово на
    # экране» они не нужны: запятая после одинокого слова читается как мусор, у
    # каналов этого формата её нет. Апостроф и дефис внутри слова остаются
    strip_punctuation: bool = False
    outline_color: str = "#000000"
    outline: float = 3.0
    # Тень ASS — это ВТОРАЯ копия текста со смещением, а не размытие. Поверх
    # трёхпиксельной обводки она читается как второй субтитр за первым: на
    # светлом фоне и на затухании реплики видно два одинаковых слова со сдвигом
    # (найдено человеком на готовом шортсе, подтверждено рендером того же кадра
    # с тенью и без). Обводка отделяет текст от фона сама; тень включают
    # осознанно и делают заметной, а не однопиксельной
    shadow: float = 0.0
    # Подложка: полупрозрачный прямоугольник под репликой (ASS BorderStyle 3).
    # 0 — нет; 0.6 — тёмное стекло, сквозь которое кадр ещё виден. Нужна там,
    # где обводка не спасает: белый текст на белой футболке (QA на первом
    # шортсе podcast_clips: «сливается» на 4 кадрах из 4). С подложкой у
    # текста нет обводки — `outline` становится полями подложки
    box_opacity: float = 0.0
    box_color: str = "#000000"
    position: Literal["bottom", "center", "top"] = "bottom"
    # Отступ от края по вертикали, % высоты кадра. 22% — под вертикальные
    # соцсети: самая строгая зона интерфейса (TikTok, 17%) плюс запас. Ниже
    # строку закроет блок с именем канала и звуком, и в плеере этого не видно —
    # смотреть `videoforge preview`. Горизонтальному видео нужно меньше
    margin_pct: float = 22.0
    side_margin_pct: float = 6.0  # отступ от боков, % ширины — здесь ASS переносит строки
    max_words: int = 5  # слов в одной реплике субтитров
    max_chars: int = 24  # символов в реплике; длиннее — новая реплика
    max_gap_sec: float = 0.55  # пауза в речи длиннее этого начинает новую реплику
    mode: Literal["phrase", "karaoke"] = "karaoke"  # phrase — фраза целиком, без подсветки
    fade_ms: int = 60  # появление/исчезание реплики
    emphasis_color: str = "#FF5A36"  # слова-акценты (их выбирает монтаж, не стиль)
    emphasis_scale: int = 112  # % кегля для акцентов
    # Как реплика оживает. `none` — статичная строка, произносимое слово только
    # перекрашивается (караоке-теги `\k`). `pop` — на каждом слове строка
    # перерисовывается заново, и произносимое слово подпрыгивает пружиной:
    # рывок вверх, недолёт, возврат. Слово выезжает снизу и проявляется из
    # прозрачного, замедляясь к концу, в двух видах: `reveal` — реплика
    # разложена целиком, слова проявляются на своих местах и текст не
    # двигается; `grow` — строка состоит только из произнесённых слов и растёт,
    # сдвигая прежние к краю. Анимацию рисуют `\t` и `\move`, а они живут
    # внутри реплики ASS — поэтому у всех, кроме `none`, на слово приходится
    # своя реплика
    animation: Literal["none", "pop", "reveal", "grow"] = "none"
    pop_scale: int = 124  # % кегля на пике прыжка
    pop_ms: int = 260  # вся пружина: рывок + недолёт + возврат
    pop_undershoot: int = 94  # % кегля в нижней точке отскока; 100 — без отскока
    reveal_ms: int = 220  # появление слова целиком: подъём + проявление
    reveal_rise_pct: int = 35  # откуда слово едет, % кегля ниже своего места
    # --- плашки (Scene.overlay): текст, который зритель читает, поверх кадра.
    # Тот же шрифт и цвета, что у субтитров, но свой кегль и всегда СВЕРХУ:
    # субтитры живут внизу, два текста в одной полосе не читаются. Замер
    # референсного канала (07.09.2026): тёмная полупрозрачная плашка,
    # ключевое слово жёлтым
    overlay_size_pct: float = 3.6  # кегль плашки, % короткой стороны кадра
    overlay_margin_pct: float = 5.0  # отступ от верхнего края, % высоты
    overlay_box_opacity: float = 0.6  # тёмное стекло под текстом; 0 — обводка вместо подложки
    overlay_box_color: str = "#000000"
    # В каком формате рисовать пример стиля для пульта (`resource gen-example`).
    # На рендер не влияет: отступы и кегль считаются от кадра, поэтому один и
    # тот же стиль в шортсе и в горизонтальном ролике выглядит по-разному, и
    # витрина обязана показывать тот формат, для которого стиль написан
    preview_format: Literal["9:16", "16:9", "1:1"] = "9:16"

    def font_path(self) -> Path:
        from videoforge.paths import fonts_dir

        p = Path(self.font_file)
        return p if p.is_absolute() else fonts_dir() / self.font_file


LYRICS_EXTS = (".vtt", ".srt", ".lrc", ".txt")


class MusicTrack(Resource):
    """Фоновая музыка. Права обязательны: без `license` и `source` сборка трек не
    берёт — случайный файл снимает монетизацию или блокирует ролик.

    Уровни относительно голоса: `gain_db` — трек до даккинга (−12 → на 12 дБ тише
    речи; трек сначала приводится к уровню голоса, поэтому число не зависит от
    того, как смастерён файл), `duck_db` — насколько глубже он уходит под речью.
    Голос всегда главнее: VOICE > MUSIC > SFX."""

    file: str = ""  # путь к аудиофайлу относительно YAML
    license: str = ""  # например "CC0", "CC BY 4.0 (автор …)", "куплена: …"
    source: str = ""  # откуда взят: URL или описание
    # Слова песни с таймингами (.vtt с пословными тайм-кодами, .srt) или просто
    # текстом (.txt). Не для рендера: пульт кладёт их дорожкой под волну трека,
    # чтобы было видно, попадает ли текст на экране в то, что поют. Пусто —
    # ищется файл того же имени рядом с треком
    lyrics: str = ""
    # Готовая строка кредита, если лицензия требует упоминания (CC BY и родня).
    # Её дословно кладёт в описание стадия publish: лицензия, о которой знает
    # только YAML, — это нарушение, о котором человек узнаёт от площадки
    attribution: str = ""
    mood: str = ""  # настроение словами: спокойная, тревожная, бодрая
    energy: float = 0.5  # 0..1
    bpm: int | None = None
    # Секунда, с которой у трека начинается основная часть («дроп»). Не для
    # рендера, а для монтажа: план выравнивает трек так, чтобы дроп совпал с
    # кульминацией ролика (`music.start_sec`). None — трек ровный, выравнивать нечего
    drop_sec: float | None = None
    loop_ok: bool = True  # можно ли зациклить, если ролик длиннее трека
    # Точки входа: секунды трека, с которых его можно начинать (начало фразы, а
    # не середина такта). Канал, у которого под каждым роликом один трек, звучит
    # одинаково с первой секунды; с точками сборка берёт каждому ролику свою —
    # детерминированно по id эпизода (`assemble.music_start`). План монтажа со
    # своим `music.start_sec` побеждает. Пусто — трек всегда с начала
    starts: list[float] = Field(default_factory=list)
    # Подложка: ровный зацикливаемый эмбиенс без развития — для слоя подложки
    # сборки (project.yaml → audio.bed), который лежит под всей дорожкой и
    # держит один уровень, пока музыка с развитием ещё не вступила. Трек с
    # развитием сюда не годится: его тихое вступление и есть та тишина, которую
    # слой закрывает. Уровень слоя — `audio.bed_gain_db` проекта, не gain_db
    bed: bool = False
    tags: list[str] = Field(default_factory=list)
    gain_db: float = -12.0
    duck_db: float = 10.0

    def path(self) -> Path:
        return self._base / self.file

    def lyrics_path(self) -> Path | None:
        """Файл со словами песни: явный `lyrics` или файл того же имени рядом."""
        if self.lyrics:
            path = self._base / self.lyrics
            return path if path.exists() else None
        audio = self.path()
        for ext in LYRICS_EXTS:
            candidate = audio.with_suffix(ext)
            if candidate.exists():
                return candidate
        return None


class Sfx(Resource):
    """Звуковой эффект: удар, пролёт, нагнетание, глитч.

    Права обязательны ровно как у музыки — Content ID ловит и трёхсекундный
    свист. Библиотеку по умолчанию проект синтезирует сам
    (`videoforge resource gen-sfx`, см. `core/audio_synth.py`): у синтезированного звука
    автора нет. Купленный или записанный кладётся сюда же файлом.

    `gain_db` — уровень в миксе ОТНОСИТЕЛЬНО голоса, как у музыки. Порядок
    дорожек не меняется: VOICE > MUSIC > SFX, и эффект, который перекрикивает
    речь, — это брак, а не акцент."""

    file: str = ""  # путь к аудиофайлу относительно YAML
    license: str = ""
    source: str = ""
    kind: Literal["impact", "whoosh", "riser", "sub_drop", "click", "glitch", "other"] = "other"
    duration_sec: float = 0.0  # справочно: сколько звук длится
    gain_db: float = -6.0
    tags: list[str] = Field(default_factory=list)

    def path(self) -> Path:
        return self._base / self.file


class Background(Resource):
    """Фон раскладки `circle`: видео на весь кадр под кружком. Звук фона не берётся.

    Файл длиннее любого ролика: каждый эпизод берёт свой кусок — детерминированно
    по id эпизода (`core.circle.background_start`), иначе у всех роликов канала
    один и тот же первый кадр. Файл тяжёлый и под .gitignore, поэтому
    `source` описывает, откуда он взят (своя запись или лицензированный сток)."""

    file: str = ""  # видеофайл рядом с YAML
    license: str = ""
    source: str = ""  # откуда: URL и какой кусок взят
    tags: list[str] = Field(default_factory=list)

    def path(self) -> Path:
        return self._base / self.file


KINDS: dict[str, type[Resource]] = {
    "characters": Character,
    "locations": Location,
    "props": Prop,
    "styles": Style,
    "voices": Voice,
    "niches": Niche,
    "caption_styles": CaptionStyle,
    "music": MusicTrack,
    "sfx": Sfx,
    "backgrounds": Background,
}

VISUAL_KINDS: tuple[str, ...] = ("characters", "locations", "props")

# Единственное и множественное число — чтобы CLI и cast принимали оба.
KIND_ALIASES: dict[str, str] = {}
for _plural in KINDS:
    KIND_ALIASES[_plural] = _plural
    KIND_ALIASES[_plural.rstrip("s")] = _plural

_TEMPLATE_BY_KIND = {
    "characters": "character",
    "locations": "location",
    "props": "prop",
    "styles": "style",
    "voices": "voice",
    "niches": "niche",
    "caption_styles": "caption_style",
    "music": "music",
    "sfx": "sfx",
    "backgrounds": "background",
}


def resource_kind(name: str) -> str:
    try:
        return KIND_ALIASES[name.lower()]
    except KeyError:
        raise ValueError(f"неизвестный вид ресурса '{name}'; допустимы: {sorted(KINDS)}") from None


def search_dirs(project_dir: Path | None = None) -> list[Path]:
    """Где искать ресурсы: сначала проектные, потом глобальные."""
    dirs: list[Path] = []
    if project_dir is not None:
        dirs.append(project_dir / "resources")
    dirs.append(resources_dir())
    return dirs


def load_resource[R: Resource](
    kind: str, resource_id: str, model: type[R], dirs: list[Path] | None = None
) -> R:
    dirs = dirs or [resources_dir()]
    for base in dirs:
        path = base / kind / f"{resource_id}.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            data.setdefault("id", resource_id)
            obj = model.model_validate(data)
            obj._base = path.parent
            obj._scope = "global" if base == resources_dir() else "project"
            return obj
    searched = ", ".join(str(d / kind / f"{resource_id}.yaml") for d in dirs)
    raise FileNotFoundError(f"ресурс {kind}/{resource_id} не найден (искал: {searched})")


@dataclass
class ResourceBundle:
    character: Character | None = None
    style: Style | None = None
    voice: Voice | None = None
    niche: Niche | None = None
    caption_style: CaptionStyle | None = None
    music: MusicTrack | None = None


def load_resources(refs: ResourceRefs, dirs: list[Path] | None = None) -> ResourceBundle:
    return ResourceBundle(
        character=load_resource("characters", refs.character, Character, dirs)
        if refs.character
        else None,
        style=load_resource("styles", refs.style, Style, dirs) if refs.style else None,
        voice=load_resource("voices", refs.voice, Voice, dirs) if refs.voice else None,
        niche=load_resource("niches", refs.niche, Niche, dirs) if refs.niche else None,
        caption_style=load_resource("caption_styles", refs.caption_style, CaptionStyle, dirs)
        if refs.caption_style
        else None,
        music=load_resource("music", refs.music, MusicTrack, dirs) if refs.music else None,
    )


# ---------------------------------------------------------------- каст сцены


def parse_entity_ref(ref: str) -> tuple[str, str]:
    """'characters/timur' | 'prop/compass' → (kind во множественном, id)."""
    parts = ref.strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"участник каста пишется как <вид>/<id>, например props/compass: {ref!r}")
    kind = resource_kind(parts[0])
    if kind not in VISUAL_KINDS:
        raise ValueError(f"в касте сцены могут быть только {VISUAL_KINDS}, а не '{kind}' ({ref!r})")
    return kind, parts[1]


CastMember = tuple[str, VisualResource]  # (kind во множественном, ресурс)

_VISUAL_MODELS: dict[str, type[VisualResource]] = {
    "characters": Character,
    "locations": Location,
    "props": Prop,
}


def load_cast(refs: list[str], dirs: list[Path] | None = None) -> list[CastMember]:
    cast: list[CastMember] = []
    for ref in refs:
        kind, rid = parse_entity_ref(ref)
        cast.append((kind, load_resource(kind, rid, _VISUAL_MODELS[kind], dirs)))
    return cast


# ---------------------------------------------------------------- создание


def create_resource(
    kind: str, resource_id: str, *, name: str = "", base: Path | None = None
) -> Path:
    """Кладёт шаблон YAML с комментариями в <base>/<kind>/<id>.yaml и проверяет его.
    Для визуальных ресурсов заодно создаёт папку-портфолио под референсы."""
    from videoforge.core.templates import render, yaml_str

    kind = resource_kind(kind)
    base = base or resources_dir()
    path = base / kind / f"{resource_id}.yaml"
    if path.exists():
        raise FileExistsError(f"ресурс уже существует: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render(
        f"{_TEMPLATE_BY_KIND[kind]}.yaml",
        {"id": resource_id, "name": yaml_str(name or resource_id)},
    )
    path.write_text(text, encoding="utf-8")
    if kind in VISUAL_KINDS:
        (path.parent / resource_id).mkdir(exist_ok=True)
    load_resource(kind, resource_id, KINDS[kind], [base])  # проверка схемы
    return path


_NAME_LINE = re.compile(r"^name:[^\n#]*(?P<comment>#.*)?$", re.MULTILINE)


def rename_resource(kind: str, resource_id: str, name: str, dirs: list[Path] | None = None) -> Path:
    """Сменить название ресурса: правится одна строка `name:`, файл в остальном
    как был (комментарии шаблона — для человека, `model_dump` стёр бы их).

    Меняется только название, `id` остаётся: на него ссылаются `project.yaml`,
    раскадровки и планы, и переименование не должно рвать эти ссылки. Название
    же — единственное, что человек читает в списке, и меняется оно чаще всего:
    «Ава» становится «рассказчик шортсов», когда голос выбран на слух.
    """
    from videoforge.core.templates import yaml_str

    kind = resource_kind(kind)
    name = name.strip()
    if not name:
        raise ValueError("пустое название: без имени ресурс в списке не отличить")
    obj = load_resource(kind, resource_id, KINDS[kind], dirs)  # заодно ищем файл
    path = obj._base / f"{resource_id}.yaml"

    def sub(m: re.Match[str]) -> str:
        comment = m.group("comment")
        return f"name: {yaml_str(name)}" + (f"  {comment}" if comment else "")

    text, n = _NAME_LINE.subn(sub, path.read_text(encoding="utf-8"), count=1)
    if n == 0:
        text = text.rstrip("\n") + f"\nname: {yaml_str(name)}\n"
    path.write_text(text, encoding="utf-8")
    load_resource(kind, resource_id, KINDS[kind], [path.parent.parent])  # проверка схемы
    return path


def add_reference_image(
    kind: str,
    resource_id: str,
    image: Path,
    dirs: list[Path] | None = None,
    *,
    rename: str | None = None,
) -> Path:
    """Положить фото в портфолио ресурса (папка рядом с YAML). Возвращает путь копии.

    `rename` — имя по содержимому кадра (`face_front`, `body_full_back`): почти
    все пайплайны передают модели подпись «что на референсе», и осмысленное имя
    файла обеспечивает её бесплатно. Занятое имя — ошибка, а не тихий суффикс:
    замена кадра ядра должна быть осознанной (сначала удалить старый).
    """
    kind = resource_kind(kind)
    if kind not in VISUAL_KINDS:
        raise ValueError(f"портфолио есть только у {VISUAL_KINDS}")
    if not image.exists():
        raise FileNotFoundError(f"файла нет: {image}")
    if image.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"не картинка ({', '.join(sorted(IMAGE_EXTS))}): {image}")
    obj = load_resource(kind, resource_id, _VISUAL_MODELS[kind], dirs)
    folder = obj.folder()
    folder.mkdir(parents=True, exist_ok=True)
    if rename is not None:
        dest = folder / f"{rename}{image.suffix.lower()}"
        if dest.exists():
            raise FileExistsError(f"кадр уже в портфолио: {dest} — удали старый, чтобы заменить")
    else:
        dest = folder / image.name
        n = 2
        while dest.exists():
            dest = folder / f"{image.stem}_{n}{image.suffix}"
            n += 1
    shutil.copy2(image, dest)
    return dest


def list_resources(base: Path | None = None) -> dict[str, list[str]]:
    base = base or resources_dir()
    return {
        kind: sorted(p.stem for p in (base / kind).glob("*.yaml")) if (base / kind).exists() else []
        for kind in KINDS
    }
