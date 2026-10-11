"""Раскадровка — единственный контракт между стадиями.

Режиссёр выдаёт сценарий, планнер превращает его в Storyboard, остальные
стадии заполняют `assets` и `checks` своих сцен. Сборщик читает готовую
раскадровку. Пути в `assets` — относительные к папке проекта, с прямыми слешами.

Поле `source` нужно второму workflow (нарезки из длинного видео): там сцена —
это сегмент исходника, а не промпт. Оно в схеме с самого начала, чтобы ядро
не пришлось переделывать.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field, model_validator

from videoforge.core.io import read_model, write_model

STORYBOARD_FILE = "storyboard.json"
# приставка отпечатка, принятого задним числом: «актив старый, из чего сделан —
# не проверялось». Символ, который в шестнадцатеричном хеше не встречается
ADOPTED = "~"

CheckStatus = Literal["pending", "pass", "fail", "skipped"]
AspectRatio = Literal["9:16", "16:9", "1:1"]
# темп клипа: "action" — непрерывное действие на всю длину клипа, "calm" —
# выдох, пейзаж, статичный предмет (см. Scene.tempo)
SceneTempo = Literal["action", "calm"]
# чем сцена становится клипом: "clip" — видеопровайдер эпизода (нейроклип),
# "still" — готовый кадр с программным наездом или панорамой (см. Scene.render)
SceneRender = Literal["clip", "still"]
# частота кадров сборки по умолчанию: клипы LTX-2.5 и remotion идут в 24, и
# ai_video своей частоты не задаёт (Storyboard.fps пуст). Живёт здесь, а не в
# core/ffmpeg: раскадровка — контракт, и «пусто значит 24» должно читаться из
# него, а ffmpeg её импортирует и так
FPS = 24


class SceneAssets(BaseModel):
    image: str | None = None
    video: str | None = None
    audio: str | None = None
    captions: str | None = None
    # реплики субтитров на языке упаковки (стадия translate): речь остаётся на
    # языке источника, а текст на экране — на языке зрителя
    translation: str | None = None
    clip: str | None = None  # сцена с наложенным звуком, готовая к склейке
    thumbs: list[str] = Field(default_factory=list)  # кадры собранной сцены для монтажного листа


class SceneChecks(BaseModel):
    image: CheckStatus = "pending"
    video: CheckStatus = "pending"
    audio: CheckStatus = "pending"
    notes: dict[str, str] = Field(default_factory=dict)  # замечания проверяющего по ключу


class SourceSegment(BaseModel):
    """Сегмент исходного видео — для workflow нарезок."""

    path: str
    start_sec: float
    end_sec: float


ReframeLayout = Literal["single", "two_up", "facecam_screen", "fit", "circle"]
# Чем залито поле вокруг кадра в раскладке `fit` (и вокруг кружка `circle` без
# фона): чёрным (так делает лучший по медиане референсный канал нарезок)
# или размытой копией того же кадра (два других референсных канала;
# разбор референсных каналов нарезок, 17.09.2026)
FitFill = Literal["black", "blur"]


class ReframeWindow(BaseModel):
    """Окно кадрирования, заданное человеком: побеждает детектор лиц.

    Доля от ширины (высоты) исходного кадра, где стоит ЦЕНТР окна: `x: 0.3` —
    треть слева. Задано руками — окно статично на всю сцену, детектор не
    зовётся: человек уже посмотрел и решил.

    `layout` — раскладка вертикали (14.09.2026, `features/reframe_two_up_punch_in`
    и `reframe_facecam_screen`): `single` — одно окно 9:16, как было; `two_up`
    — двое друг над другом: верхняя половина вокруг (x, y), нижняя вокруг
    (x2, y2); `facecam_screen` — игра сверху, вебка стримера снизу: (x, y) —
    центр лица в вебке, (x2, y2) — центр куска игры (пусто — по центру кадра,
    над вебкой или под ней). None — раскладку выбирает стадия по лицам
    (`stages.reframe.choose_layout`).
    Раскладка без `x` — приказ детектору («это общий план на двоих»), окна он
    ставит сам; с `x` — всё руками и статично.

    `punch` — наезд: во сколько раз окно меньше самого большого, которое влезает
    в исходник (1.0 — как есть, 1.3 — кадр крупнее на треть; у стека — каждая
    половина). Только ≥ 1: «отъезд», когда лицо не влезает, из 16:9 в 9:16
    невозможен без полос — окно 9:16 уже во всю высоту исходника, шире взять
    нечего. Кому не влезает вдвоём — тому `two_up`.

    `fit` — «с полосами» (17.09.2026, `features/reframe_fit`): кадр исходника
    целиком по ширине выхода на поле `fill` (чёрное или размытая копия кадра),
    место над ним — под подпись `Storyboard.headline`, под ним — под субтитры.
    Так собраны хиты мультсериальных нарезок, где шутка держится на всём кадре,
    а лиц, по которым ставить окно, детектор не видит. `punch` здесь — насколько
    кадр крупнее полного: 1.0 — весь 16:9, 4/3 ≈ 1.33 — 4:3, 16/9 ≈ 1.78 —
    квадрат; `x` — центр вырезанного куска по ширине исходника (пусто — центр).
    `zoom` — наезд с сохранением пропорции куска вокруг (`x`, `y`): 1.15 срезает
    по 7% кадра с краёв. Так уходит логотип, вшитый каналом-источником в угол
    (у склеек телеканала — логотип на 90–93% высоты): `zoom: 1.15, y: 0.44`
    обрезает низ кадра выше логотипа без пятна, которое оставляет `delogo`.
    Детектор не зовётся: окно статично на всю сцену.

    `circle` — «кружок» (22.09.2026): квадрат из исходника в круге с белой
    обводкой и тенью, на весь кадр под ним — `background` (id ресурса
    `backgrounds/`, любое фоновое видео). Нет фона — поле `fill`. `x`, `y` — центр
    квадрата в долях исходника; пусто — по лицам, без лиц — чуть выше середины
    (`core.circle.DEFAULT_Y`). `zoom` — квадрат меньше самого большого."""

    x: float | None = None
    y: float | None = None
    layout: ReframeLayout | None = None
    x2: float | None = None
    y2: float | None = None
    punch: float | None = None
    fill: FitFill | None = None  # `fit` и `circle` без фона; пусто — чёрное поле
    zoom: float | None = None  # `fit` и `circle`; пусто — кусок во всю высоту
    background: str | None = None  # только у `circle`: id из resources/backgrounds/

    @model_validator(mode="after")
    def _punch_only_zooms_in(self) -> ReframeWindow:
        if self.punch is not None and self.punch < 1.0:
            raise ValueError(
                f"reframe.punch {self.punch} < 1: отъезд из 16:9 в 9:16 невозможен без полос — "
                "окно и так во всю высоту исходника; двоим, которые не влезают, нужен "
                "layout: two_up"
            )
        if self.zoom is not None and self.zoom < 1.0:
            raise ValueError(f"reframe.zoom {self.zoom} < 1: кадр fit и так целиком")
        return self


# что адаптер управления читает с референса: `pose` — скелет снимается с видео
# человека аннотатором (DWPose), `canny` — контуры, `depth` — глубина, `raw` —
# файл уже и есть сигнал (скелет из захвата движений, `bench/skeleton.py`).
# Тот же словарь, что `comfy_http.CONTROL_SIGNALS`: граф один на полигон и конвейер
MotionSignal = Literal["pose", "canny", "depth", "raw"]


class MotionReference(BaseModel):
    """Референс движения: клип ведёт скелет или видео человека, а не промпт.

    Промпт и кадр не умеют ни прицелить удар, ни задать его темп (замеры
    cat_punch_speed и cat_punch_hit, 12.09.2026); адаптер IC-LoRA Union
    Control ведёт генерацию LTX-2.5 по референсу — в ту секунду и с той
    скоростью, что задал человек (`features/motion_control_iclora`,
    `skeleton_control`). Ставит ЧЕЛОВЕК на ключевые планы — удар, жест,
    кульминация, — как `Scene.reframe` у нарезок; планнер поле не пишет:
    референс — это файл, который надо снять или отрендерить из захвата
    движений, а не слова.

    `file` — путь к скелету или видео относительно папки эпизода, а не найдётся
    там — папки проекта: библиотека движений копится в проекте, один джеб
    годится всем эпизодам. Стадия video режет или дотягивает его под длину
    сцены (8n+1 кадров при 24 fps), поэтому длина файла не обязана совпадать.
    Что обязано: референс отзеркален под сторону цели и снят с ракурса кадра,
    а стартовый кадр стоит в ПЕРВОЙ позе референса — иначе первые полсекунды
    модель «встаёт в стойку», и это читается как призрак (mocap_follow_8s).
    Это предупреждение человеку в сообщении стадии, а не проверка: позу кадра
    python не видит.

    `strength` — сила направляющих адаптера (0..1; None — 1.0, как на замерах).
    Для кота годится только `raw`/`pose`: глубина и контуры переносят силуэт
    человека (cat_punch_mimic)."""

    file: str
    signal: MotionSignal
    strength: float | None = Field(default=None, ge=0.0, le=1.0)


OverlayKind = Literal["caption", "source", "label"]
# длиннее плашка не читается за секунду: две строки по ~28 знаков кеглем 3.6%
# на 16:9 — предел, дальше это абзац, а не плашка
OVERLAY_MAX_CHARS = 56


class SceneOverlay(BaseModel):
    """Текст, который зритель должен ПРОЧИТАТЬ в этой сцене. Рисует сборка
    кодом поверх кадра (ASS по стилю субтитров), в картинке текста нет.

    Появилось после ep001 (08.09.2026): правило проекта «текст — слой сборки»
    было, а слоя не было, и планнер подменял надписи «бороздами без букв».
    Три вида, по замеру референсного канала A: `caption` — верхняя плашка с
    выводом реплики, ключевое слово (`emphasis`) цветом подсветки; `source` —
    ссылка на источник («Коран 55:15», «Бухари 3336»); `label` — ярлык пункта
    списка («ПУНКТ 3»). Одна плашка на сцену: за шесть секунд читают одно."""

    text: str
    kind: OverlayKind = "caption"
    emphasis: list[str] = Field(default_factory=list)  # слова из text, подсвеченные цветом
    # Окно показа — секунды ОТ НАЧАЛА СЦЕНЫ, не финала: при пересборке сцена
    # может переехать, и окно обязано переехать с ней. None — вся сцена, как
    # было до 14.09.2026. Нужно нарезкам: у канала-эталона (research/yuka_mma,
    # blueprint — правила хука) первые две секунды перекрыты крупным
    # заголовком-вопросом, а сцена у нарезки одна и равна всему ролику — без
    # окна хук висел бы сорок секунд. Конец за пределами сцены режется по сцене
    start_sec: float | None = None
    end_sec: float | None = None

    @model_validator(mode="after")
    def _window_runs_forward(self) -> SceneOverlay:
        for name, value in (("start_sec", self.start_sec), ("end_sec", self.end_sec)):
            if value is not None and value < 0:
                raise ValueError(f"overlay.{name} не бывает отрицательным: {value}")
        if self.start_sec is not None and self.end_sec is not None:
            if self.end_sec <= self.start_sec:
                raise ValueError(
                    f"overlay: end_sec {self.end_sec} должен быть больше start_sec {self.start_sec}"
                )
        return self


class SceneMontage(BaseModel):
    """Что монтаж сделал со сценой — строка монтажного листа. Пока рендер по
    умолчанию: один план и hard cut; план монтажа (стадия edit) это расширит."""

    shots: int = 1
    transition_in: str = "cut"
    lead_sec: float = (
        0.0  # crossfade: столько секунд в начале клипа сцены ещё показан хвост прошлой
    )
    caption_events: int = 0  # реплик субтитров внутри сцены
    overlay: bool = False  # плашка сцены нарисована в финале
    music: bool = False
    cropped_share: float = 0.0  # доля кадра, срезанная кропом под формат эпизода
    # во сколько раз сборка увеличила кадр продолжения плана, чтобы его стартовый
    # масштаб совпал с конечным масштабом предыдущего звена (`video.zoom_per_sec`
    # проекта). 1.0 — не трогали: не продолжение, наезд не задан или клип уже
    # сшит провайдером (`Scene.chained`)
    zoom: float = 1.0
    black_sec: float = 0.0  # чёрных кадров по детектору внутри сцены
    freeze_sec: float = 0.0  # застывших кадров по детектору внутри сцены
    silence_sec: float = 0.0  # тишины дольше секунды по детектору внутри сцены


class Attempt(BaseModel):
    """Одна попытка сгенерировать актив сцены — с вердиктом проверки.

    Нужна потому, что последняя попытка ≠ лучшая. Вердикт vision-модели плывёт
    между прогонами (замерено на шортсе: субтитры то «нечитаемы», то нет), и на
    ep002 кадр «медь льётся вверх» проверка забраковала дважды, а на третьей
    пропустила — в ролик ушёл именно тот кадр с потоком вверх. Поэтому
    попытки хранятся все, и после исчерпания `max_attempts` стадия берёт ту,
    у которой замечаний меньше (`best_attempt`), а не ту, что была последней.

    `passed` — попытка принята: проверка пройдена или отключена
    (VF_VISION_CHECK=off). `cost_usd` — сама генерация плюс её проверка.
    Файл на диске (`<scene>_vNN`) остаётся у каждой попытки: взять другую
    руками — переписать `assets` на её `path`."""

    n: int
    path: str  # относительный, как assets
    passed: bool
    issues: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    provider: str = ""


def best_attempt(attempts: list[Attempt]) -> Attempt:
    """Какую из непрошедших попыток оставить: меньше замечаний — лучше.

    При равном счёте — самая ПОЗДНЯЯ из равных: модель на повторе получает
    причины отказа прошлой попытки, и более поздняя их учитывала; при том же
    числе замечаний это единственное, чем попытки отличаются. Прошедшая
    попытка, если она есть, побеждает всегда — но до выбора дело тогда не
    доходит: цикл стадии обрывается на ней."""
    if not attempts:
        raise ValueError("нет попыток, из которых выбирать")
    return min(attempts, key=lambda a: (not a.passed, len(a.issues), -a.n))


# значение подачи голоса в формате edge-tts: знак, число, единица — "+10%",
# "-15%", "+2Hz", "-4.5Hz". Без знака сервис отвечает ошибкой, а не молчит,
# поэтому формат проверяется здесь, до платного (по времени) вызова
PROSODY_RE = re.compile(r"^[+-]\d+(\.\d+)?(%|Hz)$")
PROSODY_UNITS = {"rate": "%", "pitch": "Hz", "volume": "%"}


def check_prosody(field: str, value: str) -> str:
    """Проверить значение подачи (`rate`, `pitch`, `volume`) по формату edge-tts."""
    unit = PROSODY_UNITS[field]
    if not PROSODY_RE.match(value) or not value.endswith(unit):
        raise ValueError(
            f"{field}: ожидается знак, число и {unit!r} (например {'+0' + unit!r}), а не {value!r}"
        )
    return value


class SceneVoice(BaseModel):
    """Подача голоса в ЭТОЙ сцене поверх ресурса голоса: шёпот в выдохе,
    ускорение на перечислении, тише перед ответом. Формат — тот же, что у
    ресурса (`resources/voices/<id>.yaml`) и у edge-tts: `rate: "+10%"`,
    `pitch: "-4Hz"`, `volume: "-6%"`; None — как у ресурса.

    Пишет человек. Планнер это поле НЕ пишет намеренно
    (`features/voice_prosody`): разницу между живой подачей и кривляньем не
    слышит ни замер, ни модель — только ухо. Поэтому сначала витрина
    (`resource gen-example voice <id> --variants`) и правка руками, а параметр в
    промпт планнера — после того, как ухо выбрало.

    Оговорка про ритм: WORDS_PER_SEC (`core/script.py`) замерен на
    rate "+0%". Сцена с другой скоростью длится не столько, сколько считал
    планнер по словам, — стадия озвучки об этом предупреждает."""

    rate: str | None = None
    pitch: str | None = None
    volume: str | None = None

    @model_validator(mode="after")
    def _edge_format(self) -> SceneVoice:
        for field in PROSODY_UNITS:
            value = getattr(self, field)
            if value is not None:
                check_prosody(field, value)
        return self


class FrameReview(BaseModel):
    """Вердикт проверки кадра ПО ОПИСАНИЮ (стадия images, пачкой одним вызовом
    модели): показывает ли кадр то, что просила сцена.

    `severity`: ok — про то; note — детали расходятся, человеку на ворота;
    block — не про то, кадр переделан с `fix` в описании и, если нашёлся,
    с референсом композиции (`reference` — путь от папки эпизода; пусто —
    не искали или не нашли). Хранится вердикт ПОСЛЕДНЕЙ проверки этого актива:
    после удачного повтора здесь ok, а что было до — в `attempt_log`."""

    severity: str  # ok | note | block
    see: str = ""
    issue: str = ""
    fix: str = ""
    search: str = ""
    reference: str = ""
    model: str = ""


class Scene(BaseModel):
    id: str
    order: int
    duration_sec: float = 6.0  # желаемая; провайдер видео округлит до поддерживаемой
    # сколько сцена просила у истории: пол для стадии озвучки. Без него тихая
    # сцена-выдох схлопывается до длины реплики, и ритм становится ровным
    planned_sec: float | None = None
    # id сцены, которую этот план продолжает: длинный бит не влезает в один клип
    # (провайдер умеет 6 или 10 с) и разбивается на несколько сцен одного плана
    continues: str | None = None
    # клип продолжения начат с того места хода камеры, где кончилось предыдущее
    # звено, — так его нарисовал провайдер с программной камерой (remotion,
    # `VideoProvider.chains_camera`). False — клип начат с исходного масштаба
    # (i2v: движение задаёт модель, и каждая генерация стартует с кадра как
    # есть), и стык сшивает сборка кропом по `video.zoom_per_sec`. Пишет стадия
    # video; сборка по этому полю знает, что стык уже сшит и кропать нечего
    chained: bool = False
    narration: str = ""  # текст озвучки (язык проекта)
    visual_prompt: str = ""  # что в кадре (обычно по-английски — так модели работают лучше)
    motion_prompt: str = ""  # как двигается кадр/камера
    # темп клипа. None — стадия video решает сама по словам motion_prompt и
    # visual_prompt; "calm" — выдох, пейзаж, статичный предмет: правило
    # непрерывного действия в промпт не добавляется; "action" — принудительно.
    # Появилось после замера 09.09.2026 (tests/t02_cat_realtime): промпт с одним
    # событием модель растягивает на весь клип, и выходит слоумо — медиана
    # энергии движения 8.8 и 24% тихих окон; с непрерывной серией действий и
    # словами про темп — 14.5 и 6%
    tempo: SceneTempo | None = None
    # кто и что в кадре: "characters/timur", "props/compass", "locations/grandpa_house".
    # Пусто → главный персонаж проекта. Описания и референсы подставит стадия картинок.
    cast: list[str] = Field(default_factory=list)
    # картинки «как надо» для этой сцены — пути от папки эпизода
    # (02_images/refs/s19.jpg): художник берёт из них расстановку и перспективу,
    # предметы и стиль — из промпта (`providers.base.Reference`, role
    # composition). Пишет стадия картинок, найдя референс по вердикту проверки;
    # положенный руками файл принимается так же. Файлы входят в отпечаток кадра
    # по содержимому. До 14.09.2026 поле было в схеме и никем не читалось
    reference_assets: list[str] = Field(default_factory=list)
    # что поправить в кадре: фразы-описания из вердикта проверки (`FrameReview.fix`),
    # стадия дописывает их к описанию сцены при повторе; правит и человек. В отпечатке
    fixes: list[str] = Field(default_factory=list)
    # вердикт проверки по описанию по ключу актива (image): что модель увидела,
    # что не так и что сделано. Пишет стадия, читают отчёт, пульт и ворота
    review: dict[str, FrameReview] = Field(default_factory=dict)
    # текст поверх кадра (плашка): всё, что зритель должен прочитать, — здесь,
    # картинка показывает предмет. None — плашки нет
    overlay: SceneOverlay | None = None
    # подача голоса в этой сцене поверх ресурса (скорость, высота, громкость).
    # Пишет человек, планнер — нет: см. SceneVoice
    voice: SceneVoice | None = None
    source: SourceSegment | None = None
    reframe: ReframeWindow | None = None  # окно кадрирования от человека (нарезки)
    # заглушить собственный звук сцены: True — сцена идёт без дорожки клипа, и в
    # ролике остаётся только музыка проекта (например, в клипе играет трек без
    # лицензии). Ставит человек в раскадровке. None — не решено: звук берётся
    # как есть
    mute: bool | None = None
    # референс движения: клип ведёт скелет или видео, а не промпт. Ставит
    # человек на ключевые планы, планнер не пишет — см. MotionReference. Входит
    # в отпечаток клипа (файл — по содержимому), только когда задан
    motion_reference: MotionReference | None = None
    # чем сцена становится клипом: "clip" — видеопровайдер эпизода (нейроклип на
    # карте), "still" — кадр с программным наездом или панорамой
    # (`ProviderSet.still`, remotion, $0). None — не назначено: клип, как было до
    # 16.09.2026. При `video.clip_share` < 1 проекта назначает стадия video ОДИН
    # раз (`plan_render`) и пишет сюда: пересчитывать решение нельзя — озвучка
    # меняет длительности, доля по времени плывёт, и перетасовка перегенерировала
    # бы клипы за деньги. Поставленное руками побеждает всегда
    render: SceneRender | None = None
    assets: SceneAssets = Field(default_factory=SceneAssets)
    checks: SceneChecks = Field(default_factory=SceneChecks)
    montage: SceneMontage = Field(default_factory=SceneMontage)
    attempts: dict[str, int] = Field(default_factory=dict)  # сколько раз генерили по ключу
    # все попытки по ключу (image | video) с вердиктами: вердикт проверки плывёт,
    # и последняя попытка не значит лучшая (см. Attempt). Старые раскадровки
    # без поля читаются — у них попыток не записано, а не ноль
    attempt_log: dict[str, list[Attempt]] = Field(default_factory=dict)
    measured_sec: dict[str, float] = Field(default_factory=dict)  # фактические длительности
    cost_usd: dict[str, float] = Field(default_factory=dict)
    # каким провайдером сделан актив; сменился провайдер (mock → gemini) — актив устарел
    provenance: dict[str, str] = Field(default_factory=dict)
    # из чего актив сделан: отпечаток входов на момент генерации. Провайдера мало —
    # человек правит `narration` и `visual_prompt` руками («поправил JSON —
    # перезапустил»), и без отпечатка повтор такую правку не замечает.
    # Приставка `~` у значения — «принят как есть»: см. `adopt_inputs`
    inputs_fingerprint: dict[str, str] = Field(default_factory=dict)

    def asset_ready(self, kind: str, provider: str, inputs: str | None = None) -> bool:
        """Актив `kind` есть, прошёл проверку, сделан текущим провайдером и из тех же входов.

        `inputs` — отпечаток входов, которые стадия собирается подать сейчас.
        Не совпал с записанным — актив устарел ровно так же, как при смене
        провайдера. `None` — стадия входы не отслеживает.

        **Чистый предикат: модель не меняет.** Отметку о принятии старого
        актива ставит стадия (`adopt_inputs`), и только когда она реально
        работает. До 12.09.2026 запись жила здесь, то есть `estimate`, отчёт и
        пульт молча правили раскадровку одним чтением.

        Отсутствующий отпечаток НЕ означает «устарел»: активы, сделанные до
        того, как отпечатки появились, переделывать не за что — они честно
        сделаны из тогдашних входов, а первый же запуск после обновления иначе
        переплатил бы за всё готовое."""
        path = getattr(self.assets, kind, None)
        status = getattr(self.checks, kind, "pending")
        if not (
            bool(path) and status in ("pass", "skipped") and self.provenance.get(kind) == provider
        ):
            return False
        if inputs is None:
            return True
        known = self.inputs_fingerprint.get(kind)
        if known is None:
            return True  # старый актив: принимаем, отметку поставит стадия
        return known.lstrip(ADOPTED) == inputs

    def adopt_inputs(self, kind: str, inputs: str) -> bool:
        """Принять старый актив как исходную точку и начать следить за входами.

        Пишется с приставкой `~`: «принят как есть, соответствие входам не
        проверялось». Отпечаток, посчитанный при создании актива, — это
        происхождение («сделан вот из этого»); принятый задним числом им не
        является, и читать его так нельзя. Дальше сравнение работает одинаково,
        поэтому следующая правка ловится обоими.

        Случай «вход успели изменить до первого запуска новой версии»
        детектировать нечем: записи о прежних входах не существует. Это
        сознательный риск миграции, а не доказательство — потому и приставка
        (политика принята 12.09.2026, `features/asset_migration_policy.yaml`).

        Возвращает True, если отметка появилась именно сейчас."""
        if kind in self.inputs_fingerprint:
            return False
        self.inputs_fingerprint[kind] = f"{ADOPTED}{inputs}"
        return True

    def adopted(self, kind: str) -> bool:
        """Отпечаток у актива — принятый задним числом, а не посчитанный при
        создании. Значит «из чего сделан» мы не знаем."""
        return self.inputs_fingerprint.get(kind, "").startswith(ADOPTED)

    def retried(self) -> dict[str, list[Attempt]]:
        """Активы, которых пробовали больше одного раза, — с журналом попыток.
        Одна попытка выбора не давала, и показывать её незачем: отчёт и пульт
        перечисляют попытки только там, где было из чего выбирать."""
        return {kind: log for kind, log in self.attempt_log.items() if len(log) > 1}


def fingerprint(*parts: object) -> str:
    """Короткий отпечаток входов актива.

    Считается от того, что человек правит руками и что реально уходит
    провайдеру. Хеш, а не сравнение полей: полей у сцены много, и список
    «что сравнивать» разъезжается с кодом молча, а отпечаток либо совпал,
    либо нет."""
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:16]


class ChainLink(NamedTuple):
    """Место сцены в цепочке продолжений одного плана (`Scene.continues`).

    Один план, разрезанный по потолку провайдера, — один ход камеры, и звено
    проходит его отрезок: `before_sec` секунд плана уже прошло, впереди
    `own_sec`. Скорость хода задана головой: клип головы едет так же, как ехал
    бы один (`head_sec` — её длина), остальные звенья продолжают с той же
    скоростью, и на стыке масштаб не отпрыгивает."""

    head: str  # id первого звена
    before_sec: float  # секунд плана до этого звена (0 у головы)
    own_sec: float  # длина этого звена по раскадровке
    total_sec: float  # длина всего плана — сумма всех звеньев
    head_sec: float  # длина головы: по ней задана скорость хода камеры одного клипа


def shots(scenes: list[Scene]) -> list[list[Scene]]:
    """Сцены по планам: голова и её продолжения (`Scene.continues`) — один план.

    Продолжение примыкает к предыдущей сцене по порядку (так же группирует лист
    раскадровки в отчёте): длинный бит, разрезанный по потолку провайдера, —
    один удержанный кадр, и решения о нём принимаются одно на план."""
    groups: list[list[Scene]] = []
    for scene in sorted(scenes, key=lambda s: s.order):
        if scene.continues and groups:
            groups[-1].append(scene)
        else:
            groups.append([scene])
    return groups


def chain_links(scenes: list[Scene]) -> dict[str, ChainLink]:
    """Цепочки продолжений → место каждого звена, включая голову.

    Цепочка — сцены подряд по порядку: сцена с `continues` примыкает к
    предыдущей (`shots`). Сцены без продолжений в ответ не попадают: у них нет
    стыка, который надо сшивать."""
    links: dict[str, ChainLink] = {}
    for group in shots(scenes):
        if len(group) < 2:
            continue
        total = sum(s.duration_sec for s in group)
        before = 0.0
        for scene in group:
            links[scene.id] = ChainLink(
                head=group[0].id,
                before_sec=round(before, 3),
                own_sec=scene.duration_sec,
                total_sec=round(total, 3),
                head_sec=group[0].duration_sec,
            )
            before += scene.duration_sec
    return links


# доля хронометража, которая с самого начала идёт клипами при `clip_share` < 1:
# зритель в первые минуты видит движение и «затягивается» (решение владельца
# 16.09.2026, `features/mixed_still_and_clip_scenes`)
CLIP_OPENING_SHARE = 0.1
# на столько сцена может быть длиннее клипа: заморозка в четверть секунды глазу
# не видна, а 6.5 с сцены под 6 с клипа — уже видна (ep003, 18.09.2026)
CLIP_FREEZE_TOLERANCE_SEC = 0.3


def _too_long_for_clip(group: list[Scene], max_clip_sec: float) -> bool:
    """Хоть одна сцена плана длиннее самого длинного клипа провайдера."""
    return any(s.duration_sec > max_clip_sec + CLIP_FREEZE_TOLERANCE_SEC for s in group)


def plan_render(
    scenes: list[Scene],
    *,
    clip_share: float,
    opening_share: float = CLIP_OPENING_SHARE,
    max_clip_sec: float | None = None,
) -> dict[str, SceneRender]:
    """Какой сцене быть клипом, какой — кадром с программным наездом.

    Считает python, а не модель: это арифметика по секундам. Решение — одно на
    план (`shots`): продолжения одного кадра клипом наполовину не бывают.

    1. Решённое не трогается: `Scene.render`, поставленный руками или прошлым
       назначением, побеждает, и сцена плана без поля наследует решение
       соседа по плану. План с референсом движения — клип: человек поставил
       скелет на удар, программная камера за ним не пойдёт.
    2. `clip_share` ≥ 1 — все остальные клипами, как до 16.09.2026.
    3. Иначе первые `opening_share` хронометража (не больше самой доли) —
       клипами целиком: план, начатый внутри окна, идёт клипом.
    4. Остаток доли — по времени равномерно: хвост ролика делится на столько
       равных окон, сколько планов средней длины в остатке, и в окне берётся
       один план — с действием (`tempo: action`), потом с участниками в кадре
       (`cast`), потом ближе к середине окна. Окно, где уже стоит решённый
       клип, пропускается. Не «лучшие по баллу», а по окну: сцены с действием
       бывают кучей (битва в середине), и клипы легли бы туда все.
    5. Доводка до доли: пока добавление или снятие одного плана приближает
       сумму к цели — добавляется лучший из дальних от соседних клипов или
       снимается слабейший из стоящих теснее. Попасть ровно нельзя — план не
       режется, — поэтому доля выходит с точностью до половины плана.

    Сцена длиннее самого длинного клипа провайдера (`max_clip_sec`) клипом не
    назначается: остаток доигрывает замороженный кадр. На ep003 (18.09.2026)
    так замёрзло 25 с — 14 клипов по 6 с под сцены 6.5–10 с; у картинки с
    наездом длина любая. Решённое руками это правило не переписывает: человек
    вправе поставить клип и с заморозкой; готовый актив — тоже (иначе
    обновление кода перевело бы сцену в картинки и перерисовало её за деньги).

    Чистая функция: раскадровку не меняет. Пишет решение стадия video, и
    только один раз — см. `Scene.render`."""
    groups = shots(scenes)
    starts: list[float] = []
    lengths: list[float] = []
    decided: dict[int, SceneRender] = {}
    free: list[int] = []
    clock = 0.0
    for i, group in enumerate(groups):
        starts.append(clock)
        lengths.append(sum(s.duration_sec for s in group))
        clock += lengths[-1]
        chosen = next((s.render for s in group if s.render is not None), None)
        if chosen is None and any(s.motion_reference is not None for s in group):
            chosen = "clip"
        if (
            chosen is None
            and max_clip_sec
            and not any(s.assets.video for s in group)
            and _too_long_for_clip(group, max_clip_sec)
        ):
            chosen = "still"  # клип короче сцены — остаток доигрывал бы заморозкой
        if chosen is None:
            free.append(i)
        else:
            decided[i] = chosen
    total = clock
    if clip_share >= 1 or total <= 0:
        decided.update({i: "clip" for i in free})
    elif free:
        _share_out(groups, starts, lengths, decided, free, clip_share, opening_share, total)
    return {
        scene.id: scene.render or decided[i] for i, group in enumerate(groups) for scene in group
    }


def _share_out(
    groups: list[list[Scene]],
    starts: list[float],
    lengths: list[float],
    decided: dict[int, SceneRender],
    free: list[int],
    clip_share: float,
    opening_share: float,
    total: float,
) -> None:
    """Шаги 3–5 `plan_render`: окно начала, окна хвоста, доводка до доли."""
    share = max(0.0, clip_share)
    goal = share * total
    opening = min(max(0.0, opening_share), share) * total
    fixed = set(decided)
    for i in free:
        if starts[i] < opening - 1e-6:
            decided[i] = "clip"
    mids = [starts[i] + lengths[i] / 2 for i in range(len(groups))]

    def score(i: int) -> tuple[bool, bool]:
        group = groups[i]
        return any(s.tempo == "action" for s in group), any(s.cast for s in group)

    def clip_sec() -> float:
        return sum(lengths[i] for i, mode in decided.items() if mode == "clip")

    tail = [i for i in range(len(groups)) if starts[i] >= opening - 1e-6]
    head_clips = sum(
        lengths[i] for i, mode in decided.items() if mode == "clip" and starts[i] < opening - 1e-6
    )
    wanted = goal - head_clips
    if tail and wanted > 0:
        low = starts[tail[0]]

        def pick(count: int) -> tuple[list[int], float]:
            """Хвост на `count` равных окон, по плану в окне; сколько вышло секунд
            вместе с решёнными клипами хвоста."""
            step = (total - low) / count
            # секунд клипа на окно: при равенстве сигнала берётся план ближе к
            # этой длине — иначе в одном окне двухсекундный клип, в соседнем
            # двенадцатисекундный, и движение идёт волнами
            each = wanted / count
            windows: list[list[int]] = [[] for _ in range(count)]
            for i in tail:
                k = int((mids[i] - low) / step) if step > 0 else 0
                windows[min(count - 1, max(0, k))].append(i)
            chosen: list[int] = []
            for k, window in enumerate(windows):
                if any(i in fixed and decided[i] == "clip" for i in window):
                    continue
                candidates = [i for i in window if i not in fixed]
                if not candidates:
                    continue
                center = low + step * (k + 0.5)
                chosen.append(
                    max(
                        candidates,
                        key=lambda i: (
                            score(i),
                            -abs(lengths[i] - each),
                            -abs(mids[i] - center),
                            -i,
                        ),
                    )
                )
            fixed_sec = sum(lengths[i] for i in tail if i in fixed and decided[i] == "clip")
            return chosen, fixed_sec + sum(lengths[i] for i in chosen)

        # число окон — подбором, а не делением на среднюю длину плана: в окне
        # выигрывает сцена с действием, и её длина не средняя. Недобор, добранный
        # потом по одному плану, ложился кучей туда, где действия больше
        mean = sum(lengths[i] for i in tail) / len(tail)
        count = max(1, min(len(tail), round(wanted / mean))) if mean > 0 else 1
        tried: dict[int, tuple[list[int], float]] = {count: pick(count)}
        direction = 1 if tried[count][1] < wanted else -1
        while 1 <= count + direction <= len(tail):
            count += direction
            tried[count] = pick(count)
            if (tried[count][1] >= wanted) == (direction == 1):
                break
        chosen, _ = min(tried.values(), key=lambda row: abs(row[1] - wanted))
        decided.update({i: "clip" for i in chosen})

    movable = [i for i in free if starts[i] >= opening - 1e-6]
    placed = clip_sec()
    # середины клипов по порядку: у эпизода на 24 минуты под три сотни планов, и
    # ближайший сосед ищется делением пополам, а не проходом по всем
    clip_mids = sorted(mids[i] for i, mode in decided.items() if mode == "clip")

    def nearest(i: int) -> float:
        """Расстояние от середины плана до ближайшего ДРУГОГО клипа."""
        pos = bisect.bisect_left(clip_mids, mids[i])
        mine = pos < len(clip_mids) and decided.get(i) == "clip" and clip_mids[pos] == mids[i]
        after = pos + 1 if mine else pos
        near = [clip_mids[j] for j in (pos - 1, after) if 0 <= j < len(clip_mids)]
        return min((abs(mids[i] - m) for m in near), default=total)

    while True:  # недобор: добавить план, который приближает к цели
        gap = goal - placed
        options = [
            i for i in movable if decided.get(i) != "clip" and abs(gap - lengths[i]) < abs(gap)
        ]
        if not options:
            break
        best = max(options, key=lambda i: (score(i), nearest(i), -i))
        decided[best] = "clip"
        bisect.insort(clip_mids, mids[best])
        placed += lengths[best]
    while True:  # перебор: снять слабейший из стоящих теснее
        gap = placed - goal
        options = [
            i for i in movable if decided.get(i) == "clip" and abs(gap - lengths[i]) < abs(gap)
        ]
        if not options:
            break
        worst = min(options, key=lambda i: (score(i), nearest(i), i))
        del decided[worst]
        clip_mids.remove(mids[worst])
        placed -= lengths[worst]
    for i in free:
        decided.setdefault(i, "still")


def clip_share_of(scenes: list[Scene], modes: dict[str, SceneRender]) -> float:
    """Доля хронометража клипами по назначению: то, что стадия называет человеку."""
    total = sum(s.duration_sec for s in scenes)
    if total <= 0:
        return 1.0
    return sum(s.duration_sec for s in scenes if modes.get(s.id, "clip") == "clip") / total


class ResourceRefs(BaseModel):
    """Ссылки на ресурсы по id. Код про конкретного персонажа не знает."""

    character: str | None = None
    style: str | None = None
    voice: str | None = None
    niche: str | None = None
    caption_style: str | None = None  # resources/caption_styles/: как рисовать субтитры
    music: str | None = None  # resources/music/: фоновый трек (с лицензией)


class SilenceGap(BaseModel):
    """Пауза в готовом миксе: в какой сцене, с какой секунды, сколько.

    Хранятся только самые длинные (`SILENCE_GAPS_KEPT`): у ep001 их
    92, и список из 92 строк на воротах не читает никто, а пять самых больших
    дыр говорят, куда смотреть. Сцена — та, на которую пришлась большая часть
    паузы: дыра на стыке (хвост одной сцены + начало следующей) называется по
    большей доле, а не по секунде начала."""

    scene_id: str
    at_sec: float  # секунда готового файла, где пауза начинается
    sec: float


class FinalOutput(BaseModel):
    video: str | None = None
    captions: str | None = None  # ASS-файл субтитров, вшитых в финал
    duration_sec: float | None = None
    inputs_fingerprint: str | None = None  # из чего собран: сборка не повторяется без изменений
    # отпечаток ТОЛЬКО картинки: субтитры, эффекты, клипы, затухания. Совпал, а
    # общий нет — значит поменялся один звук, и дорожку картинки можно взять из
    # готового файла вместо перекодирования (2.4 с против 14–19)
    video_fingerprint: str | None = None
    # водяной знак канала, вшитый в финал: файл относительно папки проекта
    # (`project.yaml` → watermark.file). None — слоя нет. Пишется, чтобы QA и
    # пульт знали, что искать в углу, не перечитывая настройки проекта, которые
    # с тех пор могли смениться
    watermark: str | None = None
    # подпись над кадром, вшитая в финал (`Storyboard.headline`); None — слоя нет
    headline: str | None = None
    # сколько секунд финала — замороженный последний кадр (речь оказалась длиннее
    # клипа). Аварийный случай, который до сих пор никто не считал
    frozen_sec: float = 0.0
    # звук минус картинка в готовом файле; None — прочитать дорожки не удалось
    av_drift_sec: float | None = None
    # измеренная частота кадров готового файла (из `ffmpeg -i`, а не из того,
    # что просили у рендера): расходится с `Storyboard.fps` — значит по дороге
    # был пересчёт кадров, и его должно быть видно в таблице замеров. None —
    # финал собран до того, как это стали мерить
    fps: float | None = None

    # --- замеры готового файла: что монтаж сделал, а не что собирался ---
    music_track: str | None = None
    # в какую секунду ролика начался трек; отрицательное — сколько секунд трека
    # пропущено. То же число, что `music.start_sec` плана, но уже про ГОТОВЫЙ
    # файл: план у ролика может быть удалён или его не быть вовсе (эдит собран
    # скриптом), а без этого числа волна музыки и слова песни лягут на таймлайн
    # с начала трека — то есть не туда
    music_start_sec: float = 0.0
    # громкость микса по EBU R128: площадки приводят к −14 LUFS, пик выше −1 dBTP
    # после их перекодирования клиппит. None — не измерено
    loudness_lufs: float | None = None
    true_peak_dbtp: float | None = None
    voice_lufs: float | None = None  # голос отдельно — он главнее музыки
    music_lufs: float | None = None  # музыка после гейна, до даккинга
    # подложка — слой отдельный от музыки (project.yaml → audio.bed, план →
    # bed): ровный эмбиенс под всей дорожкой. None — слоя в этом финале нет
    bed_track: str | None = None
    bed_lufs: float | None = None  # подложка после гейна; даккинга у неё нет
    black_sec: float = 0.0  # чёрные кадры вне fade in/out
    # застывший кадр по freezedetect — независимый счёт того же, что frozen_sec:
    # два способа посчитать одно и то же ловят баг друг в друге
    freeze_detected_sec: float = 0.0
    silence_sec: float = 0.0  # тишина дольше секунды в готовом миксе, всего
    # тишина, упирающаяся в конец ролика (до fade out): «ролик молчит последние
    # 15.6 с» — из суммы 267 с этого не видно. None — финал собран до того, как
    # это стали мерить; «0.0» о старом файле было бы выдумкой
    silence_tail_sec: float | None = None
    # самые длинные паузы с именем сцены — куда смотреть на воротах
    silence_gaps: list[SilenceGap] = Field(default_factory=list)
    # паузы между СЛОВАМИ речи на шкале финала (слова .srt со смещением сцены):
    # детектор тишины «от 1 с» их не видит, а на слух пауза 0.9 с после каждого
    # предложения — провал. Медиана паузы на конце предложения; None — финал
    # собран до замера (16.09.2026) или слов нет
    sentence_pause_sec: float | None = None
    # сколько пауз между словами дольше LONG_PAUSE_SEC (хвост после последнего
    # слова не в счёт — у него своя строка). None — не измерено
    long_pauses: int | None = None
    long_pause_gaps: list[SilenceGap] = Field(default_factory=list)  # самые длинные из них
    caption_events: int = 0  # реплик субтитров
    caption_coverage: float | None = None  # доля секунд речи, накрытых субтитрами
    overlay_events: int = 0  # плашек (Scene.overlay), нарисованных поверх кадра
    cuts: int = 0  # склеек между сценами
    transitions: int = 0  # из них не hard cut
    effects: int = 0  # эффектов кадра из плана монтажа (наезд, тряска, вспышка…)
    sfx: int = 0  # звуковых эффектов из плана
    outro_added_sec: float = 0.0  # насколько замедленный хвост удлинил ролик
    edit_plan: str | None = None  # по какому плану собрано; None — по умолчанию
    edit_author: str = "default"  # default | manual | llm

    @property
    def silence_share(self) -> float:
        """Доля хронометража в тишине дольше секунды: у ep001 267.5 с из 506.6 —
        0.53. Выводится, а не хранится: старый финал (собранный до этой проверки)
        со стороны поля показывал бы 0% при 267 с в `silence_sec`."""
        return self.silence_sec / max(self.duration_sec or 0.0, 0.001)

    @property
    def middle_gaps(self) -> list[SilenceGap]:
        """Сохранённые дыры без хвоста: хвост — последняя по времени дыра той же
        длины, что `silence_tail_sec`. У него свой порог (заставка до 6 с), и
        посреди ролика его считать нельзя — иначе законная заставка горела бы
        «самой длинной паузой»."""
        gaps = sorted(self.silence_gaps, key=lambda g: g.at_sec)
        tail = self.silence_tail_sec
        if gaps and tail and abs(gaps[-1].sec - tail) < 0.05:
            gaps = gaps[:-1]
        return gaps

    @property
    def silence_longest_sec(self) -> float | None:
        """Самая длинная пауза посреди ролика — из сохранённых дыр (они и есть
        самые длинные), без хвоста. None — дыры не считались: финал старше
        этой проверки; 0.0 — дыры были только в хвосте."""
        if not self.silence_gaps:
            return None
        return max((g.sec for g in self.middle_gaps), default=0.0)

    def checks(
        self,
        *,
        captions_expected: bool,
        music_expected: bool,
        filmed: bool = False,
        fps_expected: float | None = None,
    ) -> list[FinalCheck]:
        """Замеры против порогов — одна таблица для сообщения стадии и отчёта.
        Пороги — те, к которым площадки приводят ролик сами; выйти за них
        значит отдать нормализацию YouTube и получить непредсказуемый звук.

        `filmed` — эпизод собран из снятого материала (нарезка чужого видео), а
        не из сгенерированных клипов. Тогда неподвижность в кадре — это человек,
        который сидит смирно, а не заморозка рендера: детектор честно её ловит,
        но замечанием она быть перестаёт. Проверка, которая горит на каждом
        ролике, перестаёт быть сигналом — и человек перестаёт читать весь QA.

        `fps_expected` — `Storyboard.fps` эпизода (None — 24, как и там):
        частота готового файла обязана быть ею, иначе где-то по дороге кадры
        пересчитали — выброшены или продублированы, — и это видно только
        здесь. Строки нет у финала, собранного до замера частоты."""
        rows: list[FinalCheck] = []
        total = max(self.duration_sec or 0.0, 0.001)
        drift = self.av_drift_sec
        rows.append(
            FinalCheck(
                "дорожки (звук − картинка)",
                f"{drift:+.3f}s" if drift is not None else "не измерено",
                drift is not None and abs(drift) <= AV_DRIFT_LIMIT_SEC,
                f"|Δ| ≤ {AV_DRIFT_LIMIT_SEC}s",
            )
        )
        if self.fps is not None:
            wanted = fps_expected or FPS
            rows.append(
                FinalCheck(
                    "частота кадров",
                    f"{self.fps:g} (эпизод {wanted:g})",
                    abs(self.fps - wanted) <= FPS_TOLERANCE,
                    "= частоте эпизода: пересчёт кадров виден здесь",
                )
            )
        share = self.frozen_sec / total
        rows.append(
            FinalCheck(
                "замороженный кадр",
                f"{self.frozen_sec:.1f}s ({share:.0%})",
                share <= FROZEN_SHARE_LIMIT,
                f"≤ {FROZEN_SHARE_LIMIT:.0%} хронометража",
            )
        )
        lufs = self.loudness_lufs
        rows.append(
            FinalCheck(
                "громкость микса",
                f"{lufs:.1f} LUFS" if lufs is not None else "тишина / не измерено",
                lufs is not None and abs(lufs - LUFS_TARGET) <= LUFS_TOLERANCE,
                f"{LUFS_TARGET:.0f} ± {LUFS_TOLERANCE} LUFS",
            )
        )
        peak = self.true_peak_dbtp
        rows.append(
            FinalCheck(
                "истинный пик",
                f"{peak:.1f} dBTP" if peak is not None else "не измерено",
                peak is None or peak <= TRUE_PEAK_LIMIT_DBTP,
                f"≤ {TRUE_PEAK_LIMIT_DBTP:.0f} dBTP",
            )
        )
        if music_expected:
            v, m = self.voice_lufs, self.music_lufs
            if v is None or m is None:
                value, ok = "голос или музыка не измерены", m is not None
            else:
                value, ok = f"голос {v:.1f} / музыка {m:.1f} LUFS", m <= v - MUSIC_BELOW_VOICE_DB
            rows.append(
                FinalCheck(
                    "музыка под голосом",
                    value,
                    ok,
                    f"музыка ≤ голос − {MUSIC_BELOW_VOICE_DB:.0f} дБ",
                )
            )
        # подложка ожидается тогда, когда сборка её положила: у слоя нет
        # своего флага в проекте, кроме самого id трека
        if self.bed_track is not None:
            v, b = self.voice_lufs, self.bed_lufs
            if v is None or b is None:
                value, ok = "голос или подложка не измерены", b is not None
            else:
                value, ok = f"голос {v:.1f} / подложка {b:.1f} LUFS", b <= v - BED_BELOW_VOICE_DB
            rows.append(
                FinalCheck(
                    "подложка под голосом",
                    value,
                    ok,
                    f"подложка ≤ голос − {BED_BELOW_VOICE_DB:.0f} дБ",
                )
            )
        rows.append(
            FinalCheck("чёрные кадры вне fade", f"{self.black_sec:.1f}s", self.black_sec == 0, "0s")
        )
        rows.append(
            FinalCheck(
                "застывший кадр (детектор)",
                f"{self.freeze_detected_sec:.1f}s при расчётных {self.frozen_sec:.1f}s",
                filmed or self.freeze_detected_sec <= self.frozen_sec + FREEZE_AGREEMENT_SEC,
                "неподвижность снятого материала — не рендер"
                if filmed
                else "не больше расчётной заморозки",
            )
        )
        # Тишина — три числа, а не сумма. Прежняя строка «тишина дольше секунды
        # ≤ 1.5 с» на длинном ролике горела всегда (у ep001 — 267.5 с), её
        # перестали читать, и ролик с 53% тишины прошёл ворота. Для нарезки
        # (`filmed`) пороги те же: звук там приходит из самого исходника, и
        # тишины в выбранном моменте нет по построению (0.0 у всех шортсов
        # podcast_clips), а если появилась — это дефект отрезка: момент вырезан
        # в паузу или дорожка не дошла до финала (шортс, собравшийся немым, уже
        # был). Неподвижность снятого материала — не дефект; тишина — дефект
        share = self.silence_share
        rows.append(
            FinalCheck(
                "доля тишины",
                f"{share:.0%} ({self.silence_sec:.1f}s)",
                share <= SILENCE_SHARE_LIMIT,
                f"≤ {SILENCE_SHARE_LIMIT:.0%} хронометража",
            )
        )
        longest = self.silence_longest_sec
        rows.append(
            FinalCheck(
                "самая длинная пауза",
                f"{longest:.1f}s" if longest is not None else "не измерено",
                longest is None or longest <= SILENCE_LONGEST_LIMIT_SEC,
                f"≤ {SILENCE_LONGEST_LIMIT_SEC:g}s",
            )
        )
        tail = self.silence_tail_sec
        rows.append(
            FinalCheck(
                "тишина в конце",
                f"{tail:.1f}s" if tail is not None else "не измерено",
                tail is None or tail <= SILENCE_TAIL_LIMIT_SEC,
                f"≤ {SILENCE_TAIL_LIMIT_SEC:g}s",
            )
        )
        # паузы речи — строки есть только у финалов, где их мерили: у старого
        # «не измерено» ничего не говорит, а шума в таблице прибавляет. У нарезки
        # (`filmed`) строк нет: темп живой речи — не наш рендер, и тайминги
        # автосубтитров YouTube стыкуют конец слова с началом следующего, то есть
        # паузу между словами не меряют вовсе (медиана 0.0 на всех 15 шортсах
        # podcast_clips). Тишину нарезки ловят строки выше
        if self.sentence_pause_sec is not None and not filmed:
            rows.append(
                FinalCheck(
                    "пауза на конце предложения",
                    f"медиана {self.sentence_pause_sec:.2f}s",
                    self.sentence_pause_sec <= SENTENCE_PAUSE_LIMIT_SEC,
                    f"≤ {SENTENCE_PAUSE_LIMIT_SEC:g}s",
                )
            )
        if self.long_pauses is not None and not filmed:
            per_min = self.long_pauses / (total / 60)
            # одна пауза допустима всегда: у шортса на 36 с она уже «1.7 в
            # минуту», а поворот мысли в ролике бывает хотя бы один
            allowed = max(1.0, total / 60 * LONG_PAUSES_PER_MIN_LIMIT)
            rows.append(
                FinalCheck(
                    f"паузы речи дольше {LONG_PAUSE_SEC:g}s",
                    f"{self.long_pauses} ({per_min:.1f} в минуту)",
                    self.long_pauses <= allowed,
                    f"≤ {LONG_PAUSES_PER_MIN_LIMIT:g} в минуту (одна — всегда)",
                )
            )
        if captions_expected:
            cov = self.caption_coverage
            rows.append(
                FinalCheck(
                    "субтитры",
                    f"{self.caption_events} реплик, покрытие речи "
                    + (f"{cov:.0%}" if cov is not None else "—"),
                    self.caption_events > 0 and (cov is None or cov >= CAPTION_COVERAGE_MIN),
                    f"покрытие ≥ {CAPTION_COVERAGE_MIN:.0%}",
                )
            )
        return rows


class FinalCheck(NamedTuple):
    """Строка таблицы «замер / значение / порог» — не хранится, только показывается."""

    label: str
    value: str
    ok: bool
    target: str


# Пороги готового файла. Площадки нормализуют к −14 LUFS (YouTube, TikTok,
# Instagram); пик выше −1 dBTP после их перекодирования клиппит
LUFS_TARGET = -14.0
LUFS_TOLERANCE = 1.5
TRUE_PEAK_LIMIT_DBTP = -1.0
MUSIC_BELOW_VOICE_DB = 6.0  # музыка до даккинга уже тише голоса хотя бы на столько
# подложка без даккинга: ближе к голосу она читается как музыка, а не как
# воздух между репликами (умолчание проекта −28, линт держит −32…−12)
BED_BELOW_VOICE_DB = 12.0
# расхождение дорожек, выше которого это уже слышно как рассинхрон губ
AV_DRIFT_LIMIT_SEC = 0.1
# `ffmpeg -i` печатает дробную частоту двумя знаками: 24000/1001 → «23.98» при
# 23.976 в эпизоде. Это одна и та же частота, а не пересчёт
FPS_TOLERANCE = 0.05
# доля замороженного кадра, выше которой ролик перестал быть видео и стал
# слайдшоу с озвучкой. Заморозка задумана как аварийный выход, и порог тут
# затем, чтобы аварийный выход не стал тихой нормой
FROZEN_SHARE_LIMIT = 0.15
FREEZE_AGREEMENT_SEC = 0.6  # детектор и расчёт считают одно и то же — расходиться им не с чего
# --- тишина. Замер measure_final по ep001 (506.6 с): 267.5 с тишины
# дольше секунды — 53%, 92 паузы с медианой 1.82 с, самая длинная 17.3 с,
# последние 15.6 с ролик молчит; в паузах −91 дБ, то есть цифровой ноль, а не
# тихая комната. Прежний порог «сумма ≤ 1.5 с» это видел и горел — как и на
# любом ролике длиннее минуты, поэтому сигналом быть перестал (до 14.09.2026)
# Пороги ужаты 16.09.2026 по замеру конкурентов doc_channel тем же детектором
# (−40 дБ, от 1 с; research/_probes/speech_pauses_2026-09-16): 7 роликов, ~85 мин.
# Доля хронометража в тишине: у конкурентов 0–1.8%, и половина из этого — хвост
# под конечной заставкой; у шортсов нарезок 0.0, у ep001 52%. Было 25%
SILENCE_SHARE_LIMIT = 0.05
# одна пауза посреди ролика длиннее этого — дыра, а не выдох: у конкурентов
# самая длинная 2.1 с на ~85 минутах звука, пауз длиннее 3 с — ни одной. Было 5 с
SILENCE_LONGEST_LIMIT_SEC = 2.5
# тишина, упирающаяся в конец (до fade out): у референсного канала A 3.7–6.4 с под конечной
# заставкой, и прежние 3 с ругались бы на хиты; 15.6 с у ep001 — всё ещё нет
SILENCE_TAIL_LIMIT_SEC = 6.0
SILENCE_GAPS_KEPT = 5  # сколько самых длинных пауз сборка называет по сценам
# --- паузы речи по словам на шкале финала (`core/pauses.py`). Медиана паузы на
# конце предложения у конкурентов 0.14–0.58 с, у ep001 1.51. Порог над верхом
# коридора: пауза класса 0.45 ± 0.1 с добивкой планнера до 0.3 с
# (`stages/audio.MAX_PAD_SEC`) доходит до 0.85, а медиана 0.8 — уже «долго думает»
SENTENCE_PAUSE_LIMIT_SEC = 0.8
# пауза дольше секунды посреди речи: у референсного канала A 0.4–0.6 в минуту, у ep001 ≈ 9.
# Без этой строки william со стыком ~1.1 с (0.42 + 0.5 + 0.19) проходит детектор
# тишины «от 1 с» почти чисто, а на слух паузы вдвое длиннее, чем у конкурентов
LONG_PAUSE_SEC = 1.0
LONG_PAUSES_PER_MIN_LIMIT = 1.0
CAPTION_COVERAGE_MIN = 0.95


class PublishInfo(BaseModel):
    """Упаковка ролика под площадку: то, что зритель видит до просмотра.

    Заполняется там, где о ролике известно больше всего: у нарезок — критиком
    моментов, который читал текст целиком. Публикация её только использует;
    править руками — тоже здесь, в раскадровке."""

    title: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)


def is_filmed(scenes: list[Scene]) -> bool:
    """Эпизод собран из снятого материала (нарезка чужого видео), а не из
    сгенерированных клипов. Меняет только чтение замеров: неподвижность в снятом
    кадре — это человек, который сидит смирно, а не заморозка рендера."""
    return bool(scenes) and all(s.source is not None for s in scenes)


class Storyboard(BaseModel):
    version: int = 1
    project_id: str  # проект (канал/серия)
    episode_id: str = ""  # эпизод внутри проекта
    workflow: str = "ai_video"
    title: str = ""
    language: str = "ru"
    # язык субтитров, если он не совпадает с языком речи: русский подкаст с
    # английским текстом на экране. None — субтитры на языке речи, перевода нет
    captions_language: str | None = None

    @property
    def text_language(self) -> str:
        """Язык всего, что зритель ЧИТАЕТ: субтитры, заголовок, описание, теги.

        Не то же, что `language` — тот про речь. У канала нарезок речь русская,
        а весь текст английский, и путаница между этими двумя стоила ролика с
        русским заголовком на английском канале и пометки «язык названия:
        русский» у площадки — то есть выдачи не тем зрителям."""
        return self.captions_language or self.language

    aspect_ratio: AspectRatio = "9:16"
    # частота кадров сборки. None — 24 (`FPS`): клипы LTX-2.5 и remotion идут в
    # 24, и у ai_video поле пусто. У нарезки его пишет стадия cut по исходнику
    # (25, 30, 60 — что снято), при нескольких источниках в одном эпизоде —
    # частота самого длинного, с предупреждением; кадрирование и сборка
    # рендерят в неё, и пересчёта кадров нет: до 14.09.2026 всё шло в 24, и у
    # 25-кадрового подкаста каждый 25-й кадр выбрасывался (h13: 1055 кадров
    # вместо 1099 на 43.96 с). Задать руками — сборка приведёт к этой частоте
    # (`fps` в mux_scene), пересчёт один и его видно в замере «частота кадров»
    fps: float | None = None
    brief: str = ""  # вход для режиссёра: тема, длительность, настроение
    resources: ResourceRefs = Field(default_factory=ResourceRefs)
    publish: PublishInfo = Field(default_factory=PublishInfo)  # заголовок, описание, хэштеги
    # подпись над кадром на весь ролик (17.09.2026, `features/clip_headline`):
    # у нарезчиков мультсериалов это реплика зрителя со сленгом и эмодзи («Jerry
    # was genuinely INCHES from death 😭») — не заголовок ролика, а реакция на
    # него. Рисует сборка, только если проект её включил (`video.headline`);
    # пишет критик моментов. Пусто — подписи нет
    headline: str = ""
    # цветокоррекция ЭТОГО ролика (имя из `core.ffmpeg.GRADES`); пусто — как у
    # проекта (`video.grade`). Своё поле нужно пробам: один материал в разных
    # коррекциях лежит соседними эпизодами одного проекта
    grade: str = ""
    scenes: list[Scene] = Field(default_factory=list)
    final: FinalOutput = Field(default_factory=FinalOutput)

    @model_validator(mode="after")
    def _unique_scene_ids(self) -> Storyboard:
        ids = [s.id for s in self.scenes]
        if len(ids) != len(set(ids)):
            raise ValueError("id сцен в раскадровке должны быть уникальны")
        return self

    def ordered_scenes(self) -> list[Scene]:
        return sorted(self.scenes, key=lambda s: s.order)

    def scene(self, scene_id: str) -> Scene:
        for s in self.scenes:
            if s.id == scene_id:
                return s
        raise KeyError(f"сцены '{scene_id}' нет в раскадровке")


def storyboard_path(project_dir: Path) -> Path:
    return project_dir / STORYBOARD_FILE


def load_storyboard(project_dir: Path) -> Storyboard:
    return read_model(storyboard_path(project_dir), Storyboard)


def save_storyboard(project_dir: Path, storyboard: Storyboard) -> None:
    write_model(storyboard_path(project_dir), storyboard)
