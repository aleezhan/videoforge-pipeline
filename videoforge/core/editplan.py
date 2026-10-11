"""План монтажа — как смонтировать уже готовый материал внутри заданной структуры.

Лежит в `05_edit/edit_plan.json`. Его пишет стадия `edit` (по умолчанию — «монтажёр
без мнения», позже — LLM) или человек руками, исполняет стадия `assemble`
детерминированно через ffmpeg. Модель никогда не управляет ffmpeg напрямую.

Чем план НЕ владеет — и это не обсуждается: порядком сцен, `duration_sec` сцены
(её меряет озвучка), сценарием, таймингами голоса, `retention.json`. План
отвечает только на вопрос «что внутри коробки сцены и на её границах»:
какие отрезки каких клипов её заполняют, каким переходом она начинается, какие
слова подсветить, где музыке стать тише.

Отсутствующий или невалидный план не останавливает сборку, ПОКА готового ролика
ещё нет: она собирает по умолчанию (hard cut везде) и говорит об этом в
сообщении — остаться совсем без файла хуже, чем получить его без правок. Но
ролик, который уже собран, битый план не заменит: сборка отказывается и не
трогает файл. Иначе опечатка в плане молча стирает работу — файл на месте,
играет, а монтажа в нём нет, и заметить это можно только просмотром.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from videoforge.core.io import read_model, write_model
from videoforge.core.motion import MOTION_STEP_SEC
from videoforge.core.storyboard import Storyboard

EDIT_DIR = "05_edit"
PLAN_FILE = "edit_plan.json"
SOURCES_FILE = "sources.json"

# Переходов намеренно четыре. Hard cut — умолчание; остальные — исключение,
# которое надо обосновать в `why`. Crossfade съедает только хвост тишины
# предыдущей сцены (`scene_tail_sec`), поэтому измеренные длины речи не трогает.
TransitionType = Literal["cut", "crossfade", "dip_black", "fade_in"]
Author = Literal["default", "manual", "llm"]


class Segment(BaseModel):
    """Отрезок источника, который идёт в коробку сцены."""

    source: str | None = None  # путь клипа относительно папки эпизода; None → клип сцены
    in_sec: float = 0.0
    out_sec: float | None = None  # None → до конца источника, не дальше коробки сцены


class Transition(BaseModel):
    type: TransitionType = "cut"
    duration_sec: float = 0.0


class SceneEdit(BaseModel):
    scene_id: str
    # пусто → один отрезок: клип сцены с нуля. Несколько отрезков — монтаж внутри
    # сцены; сумма их длин должна заполнять коробку (иначе кадр замрёт)
    segments: list[Segment] = Field(default_factory=list)
    transition_in: Transition = Field(default_factory=Transition)
    captions: bool = True
    emphasis: list[str] = Field(default_factory=list)  # слова-акценты из narration, ≤ 2
    music_gain_db: float | None = None  # с этой сцены музыка на этом уровне (отн. голоса)
    why: str = ""  # обоснование отклонения от умолчания — читает человек на воротах


class MusicCue(BaseModel):
    at_sec: float
    gain_db: float  # относительно голоса, как и music.gain_db
    why: str = ""


class MusicPlan(BaseModel):
    enabled: bool = True
    track: str | None = None  # None → трек проекта
    gain_db: float | None = None  # None → из ресурса трека
    duck_db: float | None = None
    # В какую секунду ролика начинается трек. Отрицательное — пропустить столько
    # секунд самого трека. Так дроп трека (`MusicTrack.drop_sec`) сажают ровно на
    # кульминацию монтажа: start_sec = кульминация − drop_sec. Без этого «сперва
    # тихо, потом эпично» пришлось бы подгонять монтаж под трек, а не наоборот
    start_sec: float = 0.0
    # ступени громкости в произвольных секундах финала (не только на границах
    # сцен): дроп — это когда музыка входит внутри реплики, а не после неё
    cues: list[MusicCue] = Field(default_factory=list)


class BedPlan(BaseModel):
    """Подложка — ровный эмбиенс под всей дорожкой, слой отдельный от музыки
    (`AudioConfig.bed`). В плане нет ступеней и посадки: у подложки один
    уровень на весь ролик, это и есть её смысл. Отсутствующий `bed` в плане —
    как в проекте; `{"track": null}` — слой выключен для этого ролика."""

    track: str | None = None  # id music с bed: true; None — подложки нет
    gain_db: float | None = None  # относительно голоса; None → audio.bed_gain_db проекта


# Эффекты кадра — то, что ffmpeg делает детерминированно и что видно на 9:16
# в телефоне. Их шесть, и это не «все, какие бывают», а те, у которых понятно,
# на что смотреть в замерах: `zoom_punch` — рывок наезда, `shake` — тряска
# окна, `flash` — вспышка яркости, `desaturate` — уход цвета, `vignette` —
# затемнение краёв, `glitch` — сдвиг цветовых каналов на пару кадров.
EffectKind = Literal["zoom_punch", "shake", "flash", "desaturate", "vignette", "glitch"]


class EffectCue(BaseModel):
    """Эффект на секунде финала. Время — по готовому таймлайну, а не по сцене:
    эффект живёт поверх всего ролика и на границах сцен тоже."""

    at_sec: float
    duration_sec: float = 0.4
    kind: EffectKind
    strength: float = 1.0  # 0..2 — множитель силы; 1.0 — как задумано по умолчанию
    why: str = ""


class SfxCue(BaseModel):
    """Звуковой эффект на секунде финала: id ресурса из `resources/sfx/`."""

    at_sec: float
    id: str
    gain_db: float | None = None  # None → из ресурса
    why: str = ""


class Outro(BaseModel):
    """Замедленный хвост: последние `take_sec` секунд идут в `speed` раз медленнее.

    Единственное место, кроме crossfade, где план меняет длину ролика — и
    единственное, где он это делает НАМЕРЕННО. Замедлять середину нельзя (уедет
    всё, что после), а конец — можно: после него ничего нет. Хронометраж растёт
    на `take_sec/speed − take_sec`, субтитры замедленного куска растягиваются
    вместе с речью.

    `pitch_drop` роняет голос вместе со скоростью (asetrate — как замедленная
    плёнка), без него голос остаётся в своей высоте (atempo). Первое — тот самый
    приём «сигмы», второе — честное slow-motion."""

    take_sec: float = 3.0
    speed: float = 0.5  # 0.25–1.0; 0.5 — вдвое медленнее
    pitch_drop: bool = True
    zoom: float = 0.0  # наезд за время хвоста: 0.15 → +15% к концу
    desaturate: float = 0.0  # 0..1 — насколько уходит цвет к концу хвоста
    vignette: bool = False
    why: str = ""


class EditPlan(BaseModel):
    version: int = 1
    author: Author = "default"  # default — заготовка стадии; manual — правил человек; llm
    # «не переписывать автоматикой». Отдельно от `author` намеренно: раньше эти
    # два вопроса — КТО написал план и МОЖНО ЛИ его трогать — решало одно поле,
    # и первая же правка мышкой в пульте навсегда выключала монтажёра-модель, а
    # снять это можно было только руками в файле. Теперь замок виден и снимается
    pinned: bool = False
    llm: str = ""
    generated_at: datetime | None = None
    caption_style: str | None = None  # None → стиль проекта
    music: MusicPlan | None = None  # None → как в проекте
    bed: BedPlan | None = None  # None → как в проекте; {track: null} — выключить
    fade_in_sec: float | None = None  # None → как в проекте
    fade_out_sec: float | None = None
    scenes: list[SceneEdit] = Field(default_factory=list)
    # Эффекты и звуки живут не в сцене, а на таймлайне готового ролика: удар
    # ставят НА склейку, пролёт — ДО неё, и в коробку одной сцены это не
    # укладывается. Пусто — обычный монтаж; это умолчание, а не недоделка
    effects: list[EffectCue] = Field(default_factory=list)
    sfx: list[SfxCue] = Field(default_factory=list)
    outro: Outro | None = None
    notes: str = ""

    def scene(self, scene_id: str) -> SceneEdit | None:
        for s in self.scenes:
            if s.scene_id == scene_id:
                return s
        return None


class SourceClip(BaseModel):
    """Что есть у сцены для монтажа: измерено, а не предположено. Это то, из
    чего монтажёр (человек или модель) выбирает отрезки, не гадая вслепую."""

    scene_id: str
    path: str  # клип относительно папки эпизода
    duration_sec: float  # длина клипа
    box_sec: float  # коробка сцены — столько секунд надо заполнить
    speech_sec: float  # измеренная озвучка
    silent_tail_sec: float  # тишина в конце коробки: потолок crossfade в следующую сцену
    # заход: тишина в НАЧАЛЕ коробки, до озвучки (склейка посреди паузы,
    # `core/pauses.scene_lead_in`). Слово звучит на at + заход + место в реплике.
    # По умолчанию 0: sources.json, записанный до поля, читается равным новому, и
    # готовые планы модели от этого не пересобираются
    lead_in_sec: float = 0.0
    provider: str = ""
    narration: str = ""
    # энергия движения по окнам (`core/motion.py`): где клип тихий, где пик,
    # не мёртвый ли он. Пусто — не измерено
    motion_step_sec: float = MOTION_STEP_SEC
    motion: list[float] = Field(default_factory=list)


class Sources(BaseModel):
    generated_at: datetime | None = None
    clips: list[SourceClip] = Field(default_factory=list)


NOTES_DEFAULT = (
    "Заготовка стадии edit: hard cut везде, каждый клип с нуля. Правь руками и ставь "
    "author: manual, иначе стадия перепишет файл. Можно: segments (отрезки клипов, "
    "источник — путь относительно папки эпизода), transition_in "
    "(cut | crossfade | dip_black | fade_in), captions, emphasis (слова из narration, "
    "не больше двух), music_gain_db (уровень музыки относительно голоса с этой сцены), "
    "caption_style, music (+ music.cues: ступени громкости в секундах финала), "
    "bed (подложка: track — id из resources/music/ с bed: true, gain_db относительно голоса; "
    "track: null — выключить), "
    "fade_in_sec/fade_out_sec, effects (zoom_punch | shake | flash | desaturate | "
    "vignette | glitch в секундах финала), sfx (id из resources/sfx/ в секундах финала), "
    "outro (замедленный хвост). Нельзя: порядок сцен и их длительность — это раскадровка. "
    "Лишнее и невозможное поправит линт стадии edit."
)


def default_plan(storyboard: Storyboard) -> EditPlan:
    """«Монтажёр без мнения»: полная заготовка по всем сценам, чтобы человеку было
    что править, а линту — что проверять."""
    scenes = [
        SceneEdit(
            scene_id=s.id,
            segments=[Segment(source=s.assets.video)] if s.assets.video else [],
        )
        for s in storyboard.ordered_scenes()
    ]
    return EditPlan(
        author="default", generated_at=datetime.now(UTC), scenes=scenes, notes=NOTES_DEFAULT
    )


def plan_path(episode_dir: Path) -> Path:
    return episode_dir / EDIT_DIR / PLAN_FILE


def sources_path(episode_dir: Path) -> Path:
    return episode_dir / EDIT_DIR / SOURCES_FILE


def load_plan(episode_dir: Path) -> EditPlan | None:
    path = plan_path(episode_dir)
    return read_model(path, EditPlan) if path.exists() else None


def save_plan(episode_dir: Path, plan: EditPlan) -> Path:
    path = plan_path(episode_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_model(path, plan)
    return path
