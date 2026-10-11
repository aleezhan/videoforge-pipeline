"""Проект и эпизод.

Проект = канал/серия: `projects/<id>/project.yaml` (настройки), `rules.md`
(правила для режиссёра/планнера), `episodes/` (эпизоды). Эпизод = одно видео:
`storyboard.json`, `manifest.json` и папки стадий. Эпизод наследует настройки
проекта при создании; дальше его раскадровка живёт сама по себе.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from videoforge.core.ffmpeg import SCENE_THRESHOLD
from videoforge.core.manifest import Manifest, load_manifest, now, save_manifest
from videoforge.core.playbook import PromptRules, load_playbook, prompt_rules
from videoforge.core.resources import search_dirs
from videoforge.core.storyboard import (
    CLIP_OPENING_SHARE,
    STORYBOARD_FILE,
    AspectRatio,
    ReframeWindow,
    ResourceRefs,
    Storyboard,
    save_storyboard,
)
from videoforge.core.templates import render, yaml_opt, yaml_str
from videoforge.core.workflow import load_workflow
from videoforge.paths import (
    EPISODES_DIR,
    PROJECT_FILE,
    blueprint_path,
    episode_dir,
    episodes_dir,
    playbook_json_path,
    project_dir,
    projects_dir,
    resources_dir,
)

SCRIPT_DIR = "01_script"
RULES_FILE = "rules.md"
# «что этим роликом проверяли» — рядом с эпизодом, правится руками в редакторе.
# Файл, а не поле в JSON: человек дописывает его после просмотра, а раскадровку
# перезаписывают стадии.
CHECK_FILE = "check.md"

# Вид проекта. `channel` — канал или серия, ролики которой уходят зрителю;
# `test` — полигон качества: ролики делаются, чтобы посмотреть на них глазами и
# понять, что инструмент уже умеет. Публикации у теста нет, в «живых» проектах
# пульта он не показывается — у него своя страница-галерея.
ProjectKind = Literal["channel", "test"]

# Видимость ролика на площадке. Названия — общие для всех трёх сетей: у YouTube
# это `status.privacyStatus`, у TikTok и Instagram — переключатель в форме.
Privacy = Literal["private", "unlisted", "public"]

# Дефолты площадок — их предлагает опрос, пользователю не нужно помнить лимиты.
# `ui_zone` — доли кадра, которые интерфейс площадки закрывает своими кнопками,
# подписью и звуковой строкой (сверху, снизу, справа): субтитры туда ставить
# нельзя.
#
# Числа сняты 04.09.2026 со скриншотов приложений на телефоне пользователя
# (YouTube Shorts, Instagram Reels, TikTok, экран 20:9). Ключевая поправка к
# прежним «ориентирам по гайдам»: вертикальное видео 9:16 на таком экране НЕ
# занимает его целиком — оно вписано по ширине и отцентровано, поэтому системная
# строка сверху и панель приложения снизу лежат ВНЕ кадра, а закрывают ролик
# только правая колонка кнопок и нижний блок «канал / описание / звук». Сверху
# у всех трёх пусто. Смотреть глазами — `videoforge preview`; площадки двигают
# интерфейс без предупреждения, так что числа стоит перепроверять.
PLATFORMS: dict[str, dict[str, Any]] = {
    "youtube_shorts": {
        "title": "YouTube Shorts",
        "aspect_ratio": "9:16",
        "max_sec": 180,
        "sweet_spot_sec": (30, 60),
        # снизу: строка канала с кнопкой «Подписаться», описание и звук
        "ui_zone": {"top": 0.0, "bottom": 0.15, "right": 0.15},
    },
    "tiktok": {
        "title": "TikTok",
        "aspect_ratio": "9:16",
        "max_sec": 600,
        "sweet_spot_sec": (15, 60),
        # снизу: имя, подпись, «Перевод» и строка музыки — блок выше, чем у Shorts
        "ui_zone": {"top": 0.0, "bottom": 0.17, "right": 0.15},
    },
    "instagram_reels": {
        "title": "Instagram Reels",
        "aspect_ratio": "9:16",
        "max_sec": 180,
        "sweet_spot_sec": (15, 60),
        "ui_zone": {"top": 0.0, "bottom": 0.15, "right": 0.14},
    },
    "youtube": {
        "title": "YouTube (длинное видео)",
        "aspect_ratio": "16:9",
        "max_sec": None,
        "sweet_spot_sec": (300, 900),
        "ui_zone": {"top": 0.0, "bottom": 0.10, "right": 0.0},
    },
}


# Тематический раздел канала: площадка кладёт ролик на полку и по ней же решает,
# среди чего его показывать. Ключ — наше слово, `youtube` — id раздела в Data
# API. Числа глобальные и не меняются; перечислены только назначаемые
# (`videoCategories.list`, assignable) — раздел, который площадка принимать
# отказывается, в файле настроек выглядел бы как рабочая настройка.
CATEGORIES: dict[str, dict[str, str]] = {
    "autos": {"title": "Авто", "youtube": "2"},
    "comedy": {"title": "Юмор", "youtube": "23"},
    "education": {"title": "Образование", "youtube": "27"},
    "entertainment": {"title": "Развлечения", "youtube": "24"},
    "film": {"title": "Кино и анимация", "youtube": "1"},
    "gaming": {"title": "Игры", "youtube": "20"},
    "howto": {"title": "Стиль и советы", "youtube": "26"},
    "music": {"title": "Музыка", "youtube": "10"},
    "news": {"title": "Новости и политика", "youtube": "25"},
    "nonprofit": {"title": "Общество", "youtube": "29"},
    "people": {"title": "Люди и блоги", "youtube": "22"},
    "pets": {"title": "Животные", "youtube": "15"},
    "science": {"title": "Наука и техника", "youtube": "28"},
    "sports": {"title": "Спорт", "youtube": "17"},
    "travel": {"title": "Путешествия", "youtube": "19"},
}


def category_id(category: str, network: str) -> str:
    """Id раздела у площадки по нашему слову. Пусто — раздел не объявлен или
    эта площадка разделов не знает: тогда его выбирает загрузка по-своему."""
    return CATEGORIES.get(category, {}).get(network, "")


def category_title(platform_id: str, network: str) -> str:
    """Обратно: id площадки → название по-русски, для отчётов человеку.
    Неизвестный id возвращается как есть — врать про чужой раздел хуже."""
    for spec in CATEGORIES.values():
        if spec.get(network) == platform_id:
            return spec["title"]
    return platform_id


def platform_defaults(platforms: list[str]) -> tuple[AspectRatio, int]:
    """Формат и целевая длительность по первой площадке (все вертикальные совпадают)."""
    if not platforms:
        return "9:16", 60
    first = PLATFORMS[platforms[0]]
    ratios = {PLATFORMS[p]["aspect_ratio"] for p in platforms}
    aspect: AspectRatio = first["aspect_ratio"] if len(ratios) == 1 else "9:16"
    return aspect, int(first["sweet_spot_sec"][1])


class ChannelConfig(BaseModel):
    name: str = ""
    platforms: list[str] = Field(default_factory=list)
    language: str = "ru"
    # с какой видимостью ролик ложится на площадку. `private` — по умолчанию:
    # первый ролик канала смотрят глазами уже НА площадке и открывают руками.
    # Канал, который вышел на каденс, ставит `public`: иначе каждый день
    # добавляется ручное действие, а забытое действие = ролик, которого никто
    # не увидел. `--privacy` у команды publish побеждает это значение
    privacy: Privacy = "private"
    # тематический раздел канала (`CATEGORIES`): полка, на которую площадка
    # кладёт ролик. Пусто — не объявлен, и тогда загрузка ставит нейтральный
    # раздел, а у уже уехавшего ролика правка раздел не трогает: в Studio его
    # мог выбрать человек. Объявлен — это решение, записанное в файле, и оно
    # побеждает: файл остаётся единственным источником правды
    category: str = ""

    @field_validator("platforms")
    @classmethod
    def _known_platforms(cls, value: list[str]) -> list[str]:
        unknown = [p for p in value if p not in PLATFORMS]
        if unknown:
            raise ValueError(f"неизвестные площадки {unknown}; допустимы: {sorted(PLATFORMS)}")
        return value

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        value = value.strip()
        if value and value not in CATEGORIES:
            raise ValueError(f"неизвестный раздел {value!r}; допустимы: {sorted(CATEGORIES)}")
        return value


class VideoConfig(BaseModel):
    aspect_ratio: AspectRatio = "9:16"
    target_duration_sec: int = 60
    scene_duration_sec: float = 6.0
    # сколько кадр живёт после последнего слова: движение доигрывает, но паузы нет
    scene_tail_sec: float = 0.5
    captions: bool = True  # субтитры в сборке (стиль — resources.caption_style)
    # язык субтитров, если не совпадает с языком речи (channel.language): канал
    # на английскую аудиторию с русскими источниками — речь ru, текст en. None —
    # субтитры на языке речи. Включает стадию translate в workflow нарезок
    captions_language: str | None = None
    # чувствительность детектора смен картинки: 0.05 — 2D-анимация (соседние
    # кадры почти одинаковы, склейка заметна слабо), 0.3 — живое видео. Не замер,
    # а настройка материала: пульт по нему разбирает готовый ролик на планы,
    # когда раскадровки нет. Калибруется контактным листом
    scene_threshold: float = SCENE_THRESHOLD
    music: bool = False  # фоновая музыка в сборке (трек — resources.music)
    fade_in_sec: float = 0.3  # затемнение на входе и выходе эпизода
    fade_out_sec: float = 0.8
    # наезд генеративного клипа: на сколько растёт масштаб за секунду (0.02 —
    # 12% за шестисекундный клип, как у заглушки). Нужен одному месту — стыку
    # продолжений плана (`Scene.continues`): i2v начинает каждый клип с
    # исходного масштаба, и на стыке кадр отпрыгивал; сборка кропает
    # продолжение так, чтобы его старт совпал с концом предыдущего звена. Не
    # замер, а настройка провайдера и стиля: у модели своя скорость, её
    # смотрят глазами на первом длинном плане. 0 — стык не сшивать. У remotion
    # не нужен: программная камера продолжает ход сама (`Scene.chained`)
    zoom_per_sec: float = 0.0
    # доля хронометража настоящими клипами видеопровайдера; остальные сцены —
    # кадр с программным наездом или панорамой (`ProviderSet.still`, remotion,
    # $0). 1.0 — все сцены клипами, как до 16.09.2026. Какие именно — решает
    # стадия video один раз и пишет в раскадровку (`Scene.render`,
    # `storyboard.plan_render`); отдельной настройки у эпизода нет — эпизод
    # правит само поле сцены, и поставленное руками побеждает
    clip_share: float = Field(default=1.0, ge=0.0, le=1.0)
    # первые столько хронометража идут клипами целиком (не больше `clip_share`):
    # в начале зритель видит движение; остаток доли рассыпается по ролику
    clip_opening_share: float = Field(default=CLIP_OPENING_SHARE, ge=0.0, le=1.0)
    # правила канала для проверки кадров (по-английски, одно правило — строка):
    # нарушение — брак и повтор кадра, как «не про то». Тема канала решает, что
    # нельзя рисовать (лица пророков, крест на мечети), и описание сцены этого не
    # скажет — проверка по описанию пропускала такое в ep003 (28.09.2026)
    frame_rules: list[str] = Field(default_factory=list)
    # --- нарезки (clips_from_long); у ai_video не читаются ---
    # окно кадрирования для сцен, у которых нет своего `Scene.reframe`: раскладка
    # канала, а не решение по сцене. `{layout: fit}` — кадр целиком на поле, как
    # у мультсериальных нарезок (`features/reframe_fit`). None — окно по лицам
    reframe: ReframeWindow | None = None
    # подпись над кадром на весь ролик (`Storyboard.headline`): критик моментов
    # пишет её выжившим, сборка рисует картинкой с цветными эмодзи над кадром
    # (`features/clip_headline`). Выключено — критик не пишет, сборка не рисует
    headline: bool = False
    # выбрасывать скучные реплики внутри момента: критик называет, какие
    # предложения момента лишние, и эпизод собирается из нескольких отрезков
    # источника жёсткими склейками (`features/clip_tighten`). Выключено — момент
    # идёт одним куском, как раньше
    tighten: bool = False
    # цветокоррекция готового ролика: имя из `core.ffmpeg.GRADES` (lift | punch |
    # vivid | warm). Идёт по картинке до субтитров. Пусто — кадр как есть; у
    # эпизода своё поле `Storyboard.grade`, оно побеждает
    grade: str = ""


class AudioConfig(BaseModel):
    """Звук сборки сверх музыки: подложка.

    Подложка — второй, независимый от трека слой: ровный эмбиенс под всей
    дорожкой, поверх которого музыка с развитием вступает к кульминации. Трек
    с развитием сам эту работу не делает: его тихое вступление и есть тишина
    между репликами (у devastation_and_revenge первые 10 с на 23 дБ ниже его
    интеграла, при gain −16 это 39 дБ под голосом — глубже детектора тишины;
    на ep002 уровень секунды между фразами падал до −44…−48 дБ, а поднятие
    трека с −20 до −16 дало 2–3 дБ). По умолчанию слоя нет: это решение
    канала, а не свойство инструмента."""

    # Уровень трека относительно голоса ДЛЯ ЭТОГО КАНАЛА; None — как у ресурса
    # (`MusicTrack.gain_db`). Нужен потому, что один трек стоит у нескольких
    # проектов сразу, а «насколько громко» — решение канала, а не свойство
    # файла: у нарезок мультсериала со своим плотным звуком трек просят громче,
    # чем у начитки. План монтажа (`music.gain_db`) по-прежнему впереди обоих
    music_gain_db: float | None = None
    # id ресурса music с `bed: true` (ровный, зацикливаемый); None — слоя нет
    bed: str | None = None
    # уровень подложки относительно голоса. −28: выше вступления трека и
    # детектора тишины (−40 дБ), на 21 дБ ниже пика дропа — дроп остаётся
    # событием, а под речью слой прибавляет 0.007 дБ, то есть не слышен и
    # даккинга не требует. Обоснование числами — `ffmpeg.BedMix`
    bed_gain_db: float = -28.0


# Угол кадра, в котором живёт водяной знак канала
Corner = Literal["top_left", "top_right", "bottom_left", "bottom_right"]


class WatermarkConfig(BaseModel):
    """Водяной знак канала в углу кадра — необязательный слой сборки.

    У нарезок это единственный собственный элемент графики (rules.md канала):
    ролик остаётся узнаваемым, даже если его скачают и выложат другие. Файл рисует
    человек (PNG с альфой), лежит в папке проекта; код знает только путь, угол
    и размеры. Положение считает сборка: угол минус отступ и вне зоны
    интерфейса площадок канала — той же `ui_zone`, по которой QA судит
    субтитры. Пустой `file` — слоя нет, и это умолчание: знак — решение
    канала, а не свойство инструмента."""

    file: str | None = None  # путь относительно папки проекта; пусто — слоя нет
    corner: Corner = "top_right"
    width_pct: float = Field(default=12.0, gt=0, le=100)  # ширина знака, % ширины кадра
    opacity: float = Field(default=0.7, ge=0, le=1)
    # отступ от краёв кадра (и от зоны интерфейса), % короткой стороны кадра:
    # у вертикали и горизонтали одна и та же короткая сторона 1080, поэтому
    # «3%» — одни и те же 32 px в обоих форматах
    margin_pct: float = Field(default=3.0, ge=0, le=50)

    @property
    def enabled(self) -> bool:
        return bool(self.file and self.file.strip())


class ProjectConfig(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    workflow: str = "ai_video"
    # канал/серия или полигон качества: `test` — ролики «посмотреть, что умеем».
    # Пульт показывает их отдельной галереей и не считает живыми даже с аккаунтом
    kind: ProjectKind = "channel"
    channel: ChannelConfig = Field(default_factory=ChannelConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    # подложка звука под всей дорожкой; по умолчанию слоя нет (bed пуст)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    # водяной знак канала в углу кадра; по умолчанию выключен (file пуст)
    watermark: WatermarkConfig = Field(default_factory=WatermarkConfig)
    resources: ResourceRefs = Field(default_factory=ResourceRefs)
    budget_per_episode_usd: float = 5.0
    rules_file: str = RULES_FILE
    # id анализов из research/, на которых основан канал: их blueprint.md
    # получает планнер. Пусто — канал придуман без анализа чужого контента.
    research: list[str] = Field(default_factory=list)
    # образец или полигон, а не канал: пульт не показывает его в списке проектов
    # (страница по прямой ссылке открывается), CLI и конвейер видят как обычно.
    # Так живёт demo — его раскадровку читают тесты и мок-прогон.
    hidden: bool = False

    @property
    def scenes_estimate(self) -> int:
        return max(1, round(self.video.target_duration_sec / self.video.scene_duration_sec))


def load_project_from_dir(pdir: Path) -> ProjectConfig:
    path = pdir / PROJECT_FILE
    if not path.exists():
        raise FileNotFoundError(f"проект не найден: нет {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("id", pdir.name)
    return ProjectConfig.model_validate(data)


def load_project(project_id: str) -> ProjectConfig:
    return load_project_from_dir(project_dir(project_id))


def load_rules_from_dir(pdir: Path, cfg: ProjectConfig) -> str:
    path = pdir / cfg.rules_file
    return path.read_text(encoding="utf-8") if path.exists() else ""


# Потолок blueprint в знаках: брифы анализов канала + его правила playbook.
# Замер claude_cli (llm-call-cost-model, тот же, что у CHUNK_MAX_CHARS выбора
# моментов): вход в 51 тыс. знаков добавил к вызову 0.3 с, в 64 тысячи — ~7 с, а
# 345 тысяч дали 158 и 569 с на двух одинаковых вызовах. Blueprint — самая
# большая постоянная часть промпта сценариста, планнера и монтажёра, и уходит он
# в КАЖДЫЙ вызов (сценарий по главам — ~14 на 24 минуты), поэтому сам по себе
# бесплатную зону не превышает. Брифы не режутся и считаются первыми: у doc_channel
# их 40 тыс. знаков, у podcast_clips — 54 тысячи; playbook получает остаток.
BLUEPRINT_MAX_CHARS = 60_000
BLUEPRINT_SEPARATOR = "\n\n---\n\n"


@dataclass
class Blueprint:
    """Что уходит в промпт из research/ и сколько правил playbook туда не попало."""

    text: str
    rules: PromptRules | None = None  # None — свода нет или канал без анализов

    @property
    def note(self) -> str:
        """Строка для лога и сообщения стадии; "" — говорить не о чем."""
        return self.rules.summary() if self.rules is not None else ""


def project_blueprint(cfg: ProjectConfig) -> Blueprint:
    """Брифы анализов, на которых стоит канал, и правила playbook его ниши.

    Отсутствующий анализ не ошибка: проект мог быть создан до него или бриф
    ещё не собран. Планнер работает и без брифа, просто хуже. Проект без
    `research` получает пустой blueprint: канал придуман без анализа чужого
    контента, и ниши, по которой отбирать правила, у него нет. Свод правил
    целиком (research/playbook.md) остаётся человеку и distill, в промпт идут
    только правила ниши канала (`core.playbook.prompt_rules`)."""
    if not cfg.research:
        return Blueprint("")
    parts: list[str] = []
    for slug in cfg.research:
        path = blueprint_path(slug)
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    if not playbook_json_path().exists():  # анализы ещё не дошли до distill
        return Blueprint(BLUEPRINT_SEPARATOR.join(parts))
    briefs = len(BLUEPRINT_SEPARATOR.join(parts)) + (len(BLUEPRINT_SEPARATOR) if parts else 0)
    rules = prompt_rules(load_playbook(), cfg.research, max_chars=BLUEPRINT_MAX_CHARS - briefs)
    if rules.text:
        parts.append(rules.text)
    return Blueprint(BLUEPRINT_SEPARATOR.join(parts), rules)


def load_blueprint(cfg: ProjectConfig) -> str:
    """Текст blueprint для промпта (см. `project_blueprint`)."""
    return project_blueprint(cfg).text


def list_projects() -> list[str]:
    base = projects_dir()
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if (p / PROJECT_FILE).exists())


def list_episodes(project_id: str) -> list[str]:
    base = episodes_dir(project_id)
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if (p / STORYBOARD_FILE).exists())


def missing_resources(refs: ResourceRefs, dirs: list[Path] | None = None) -> list[str]:
    """Ссылки проекта, за которыми нет файла. `dirs` — тот же двухуровневый
    поиск, что у стадий (проектные ресурсы, потом глобальные): проверка обязана
    смотреть туда же, куда смотрит конвейер, иначе проектный стиль субтитров
    помечается как пропавший."""
    dirs = dirs or [resources_dir()]
    kinds = {
        "characters": refs.character,
        "styles": refs.style,
        "voices": refs.voice,
        "niches": refs.niche,
        "caption_styles": refs.caption_style,
        "music": refs.music,
    }
    return [
        f"{kind}/{rid}"
        for kind, rid in kinds.items()
        if rid and not any((d / kind / f"{rid}.yaml").exists() for d in dirs)
    ]


@dataclass
class ProjectSpec:
    """Ответы опроса — ровно то, что нужно шаблону project.yaml."""

    id: str
    name: str = ""
    description: str = ""
    workflow: str = "ai_video"  # workflows/<имя>.yaml: ai_video | clips_from_long
    kind: ProjectKind = "channel"  # channel — канал; test — полигон качества
    channel_name: str = ""
    platforms: list[str] = field(default_factory=lambda: ["youtube_shorts"])
    language: str = "ru"
    aspect_ratio: AspectRatio | None = None  # None → из площадки
    target_duration_sec: int | None = None  # None → из площадки
    scene_duration_sec: float = 6.0
    character: str | None = None
    style: str | None = None
    voice: str | None = None
    niche: str | None = None
    caption_style: str | None = "default"  # стиль субтитров из resources/caption_styles/
    music: str | None = None  # трек из resources/music/; None — без музыки
    budget_per_episode_usd: float = 5.0
    research: list[str] = field(default_factory=list)


def create_project(spec: ProjectSpec) -> tuple[Path, list[str]]:
    """Создаёт projects/<id>/ с project.yaml, rules.md и episodes/.
    Возвращает путь и предупреждения (например, про отсутствующие ресурсы)."""
    pdir = project_dir(spec.id)
    if (pdir / PROJECT_FILE).exists():
        raise FileExistsError(f"проект уже существует: {pdir / PROJECT_FILE}")
    unknown = [p for p in spec.platforms if p not in PLATFORMS]
    if unknown:
        raise ValueError(f"неизвестные площадки {unknown}; допустимы: {sorted(PLATFORMS)}")
    if spec.kind not in ("channel", "test"):  # проверяем до записи файлов, а не после
        raise ValueError(f"неизвестный вид проекта {spec.kind!r}; допустимы: channel, test")

    aspect, duration = platform_defaults(spec.platforms)
    aspect = spec.aspect_ratio or aspect
    duration = spec.target_duration_sec or duration
    scenes_estimate = max(1, round(duration / spec.scene_duration_sec))
    name = spec.name or spec.id
    values = {
        "id": spec.id,
        "name": yaml_str(name),
        "name_plain": name,
        "workflow": spec.workflow,
        "kind": spec.kind,
        "description": yaml_str(spec.description),
        "description_plain": spec.description or "_(описание не заполнено)_",
        "channel_name": yaml_str(spec.channel_name),
        "platforms": ", ".join(spec.platforms),
        "language": spec.language,
        "aspect_ratio": aspect,
        "target_duration_sec": duration,
        "scene_duration_sec": spec.scene_duration_sec,
        "scenes_estimate": scenes_estimate,
        "character": yaml_opt(spec.character),
        "style": yaml_opt(spec.style),
        "voice": yaml_opt(spec.voice),
        "niche": yaml_opt(spec.niche),
        "caption_style": yaml_opt(spec.caption_style),
        "music": yaml_opt(spec.music),
        "music_on": "true" if spec.music else "false",
        "budget": spec.budget_per_episode_usd,
        "research": json.dumps(spec.research),  # JSON-список — валидный inline-YAML
    }
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / EPISODES_DIR).mkdir(exist_ok=True)
    (pdir / PROJECT_FILE).write_text(render("project.yaml", values), encoding="utf-8")
    (pdir / RULES_FILE).write_text(render("rules.md", values), encoding="utf-8")

    cfg = load_project_from_dir(pdir)  # проверка: шаблон отрендерился в валидный YAML
    warnings = [
        f"ресурс {m} не найден — создай его (/new-character, /new-style, …) "
        f"или поправь resources в {PROJECT_FILE}"
        for m in missing_resources(cfg.resources, search_dirs(pdir))
    ]
    return pdir, warnings


def parse_ref(ref: str) -> tuple[str, str]:
    """'<проект>/<эпизод>' → (project_id, episode_id)."""
    parts = ref.strip().strip("/").replace("\\", "/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            f"ссылка на эпизод пишется как <проект>/<эпизод>, например country_telling/ep001; "
            f"получено: {ref!r}"
        )
    return parts[0], parts[1]


def resolve_episode(ref: str) -> Path:
    project_id, episode_id = parse_ref(ref)
    edir = episode_dir(project_id, episode_id)
    if not (edir / STORYBOARD_FILE).exists():
        raise FileNotFoundError(f"эпизода {ref} нет: {edir}")
    return edir


def load_check(edir: Path) -> str:
    """`check.md` эпизода — что этим роликом проверяли. Пусто, если файла нет."""
    path = edir / CHECK_FILE
    return path.read_text(encoding="utf-8") if path.exists() else ""


def check_question(text: str) -> str:
    """Первая содержательная строка `check.md` — одна фраза для карточки.

    Заголовки пропускаются: «# Что проверяем» на карточке галереи не говорит
    ничего, нужна следующая строка — сам вопрос."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line
    return ""


def create_episode(
    project_id: str, episode_id: str, *, title: str = "", brief: str = "", check: str = ""
) -> Path:
    """Эпизод с настройками проекта: формат, язык, ресурсы, бюджет, workflow.

    `check` — «что этим роликом проверяем»: одна фраза, которая ляжет в
    `check.md` рядом с раскадровкой. Нужна тестовым проектам, где ролик делается
    ради ответа на вопрос, а не ради зрителя; дальше человек дописывает файл
    руками, посмотрев результат."""
    cfg = load_project(project_id)
    load_workflow(cfg.workflow)  # проверка, что такой workflow есть
    edir = episode_dir(project_id, episode_id)
    if edir.exists():
        raise FileExistsError(f"эпизод уже существует: {edir}")
    edir.mkdir(parents=True)
    (edir / SCRIPT_DIR).mkdir()
    if check.strip():
        (edir / CHECK_FILE).write_text(
            f"# Что проверяем\n\n{check.strip()}\n\n"
            "## Что увидели\n\n_(заполнить после просмотра)_\n",
            encoding="utf-8",
        )
    save_storyboard(
        edir,
        Storyboard(
            project_id=project_id,
            episode_id=episode_id,
            workflow=cfg.workflow,
            title=title,
            brief=brief,
            language=cfg.channel.language,
            captions_language=cfg.video.captions_language,
            aspect_ratio=cfg.video.aspect_ratio,
            resources=cfg.resources.model_copy(),
        ),
    )
    save_manifest(
        edir,
        Manifest(
            project_id=project_id,
            episode_id=episode_id,
            workflow=cfg.workflow,
            created_at=now(),
            budget_usd=cfg.budget_per_episode_usd,
        ),
    )
    return edir


def approve_stage(edir: Path, stage: str) -> None:
    manifest = load_manifest(edir)
    if stage not in manifest.approvals:
        manifest.approvals.append(stage)
    if manifest.awaiting_approval == stage:
        manifest.awaiting_approval = None
    save_manifest(edir, manifest)
