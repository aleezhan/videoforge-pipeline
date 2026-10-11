"""Модели анализа: конфиг (analysis.yaml), снапшот канала, статистика.

Каждая стадия пишет свой JSON в папку анализа — это и есть контракт между
стадиями (аналог storyboard.json у эпизодов, только артефактов несколько:
01_collect/channel.json → 02_stats/stats.json → …).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from videoforge.core.ffmpeg import SCENE_THRESHOLD
from videoforge.core.playbook import PlaybookRule, Verdict

AnalysisMode = Literal["channel", "video", "competitors"]

# запасная вилка дохода, пока classify не оценил нишу и rpm_usd не заполнен
DEFAULT_RPM: dict[str, tuple[float, float]] = {"long": (0.5, 4.0), "shorts": (0.05, 0.15)}


class AnalysisConfig(BaseModel):
    """analysis.yaml — правит человек, читают стадии."""

    id: str
    mode: AnalysisMode = "channel"
    platform: Literal["youtube"] = "youtube"
    collector: str = "ytdlp"
    urls: list[str]
    goal: str = ""
    language: str = "ru"  # язык выводов анализа в отчёте (цитаты остаются в оригинале)
    max_videos: int = 0  # 0 — все видео канала
    # выброс: просмотры ≥ N × медиана соседей. "auto" — порог считается по
    # разбросу самой группы (см. statistics.auto_threshold): у ровного канала
    # фиксированные ×3 не находят ничего, у скачущего — половину видео.
    outlier_threshold: float | Literal["auto"] = "auto"
    deep_top: int = 5  # сколько лучших видео разбирать глубоко (стадия deep)
    # сколько слабых видео разбирать как контрольную группу. Без неё анализ
    # видит только победителей и не может отличить причины успеха от общих
    # черт стиля канала: приём, который есть и в хитах, и в провалах, ничего
    # не объясняет. 0 — не разбирать.
    deep_worst: int = 2
    deep_videos: list[str] = Field(default_factory=list)  # явные id для deep; пусто — топ по ratio
    # чувствительность детекта смен картинки (стадия visual, ffmpeg select=scene).
    # Зависит от материала, а не от нашего кода, поэтому живёт в настройках
    # анализа: плоская 2D-анимация меняет картинку мягко и требует 0.05, живое
    # видео с движением камеры на нём разваливается на «планы» по полсекунды.
    # Калибруется глазами: контактный лист раз в 1–2 с против detect_cuts.
    scene_threshold: float = SCENE_THRESHOLD
    # оценка дохода в отчёте: $ за 1000 просмотров, [мин, макс] по группам.
    # Пусто — берётся оценка LLM для ниши из classify (до classify — DEFAULT_RPM);
    # заполнено — твоё допущение побеждает. Реальный RPM YouTube не раскрывает.
    rpm_usd: dict[str, tuple[float, float]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _urls_match_mode(self) -> AnalysisConfig:
        if not self.urls:
            raise ValueError("нужна хотя бы одна ссылка (urls)")
        if self.mode in ("channel", "video") and len(self.urls) != 1:
            raise ValueError(f"режим '{self.mode}' — ровно одна ссылка, получено {len(self.urls)}")
        if self.mode == "competitors" and len(self.urls) < 2:
            raise ValueError("режим 'competitors' — минимум две ссылки")
        for group, (low, high) in self.rpm_usd.items():
            if low < 0 or high < low:
                raise ValueError(f"rpm_usd.{group}: диапазон [мин, макс], 0 ≤ мин ≤ макс")
        return self


# ---------------------------------------------------------------- 01_collect


class VideoMeta(BaseModel):
    """Метаданные одного видео, как их отдал коллектор."""

    id: str
    url: str = ""
    title: str = ""
    description: str = ""
    published_at: datetime | None = None
    duration_sec: float | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    thumbnail_url: str = ""
    is_short: bool | None = None  # None — определяется по длительности в статистике


class ChannelSnapshot(BaseModel):
    id: str
    url: str
    title: str = ""
    description: str = ""
    subscribers: int | None = None
    video_count: int | None = None
    created_at: datetime | None = None
    avatar_url: str = ""  # аватар канала: свой аккаунт в пульте показывается лицом
    collected_at: datetime
    collector: str  # кто собирал: сменился коллектор — снапшот устарел


class ChannelData(BaseModel):
    """01_collect/channel.json — сырьё для всех следующих стадий."""

    channel: ChannelSnapshot
    videos: list[VideoMeta] = Field(default_factory=list)


# ---------------------------------------------------------------- 02_stats


class VideoStats(BaseModel):
    """Строка статистики: просмотры видео против скользящей медианы соседей."""

    video_id: str
    title: str = ""
    group: str  # long | shorts — сравниваются только внутри группы
    published_at: datetime | None = None
    duration_sec: float | None = None
    views: int
    rolling_median_views: float  # медиана соседних по дате видео той же группы
    ratio: float  # views / rolling_median — нормализовано к росту канала
    outlier: bool
    # комментариев на лайк. Дёшево (данные уже собраны) и отделяет успех,
    # купленный призывом «напиши слово в комментарии», от успеха содержанием:
    # у роликов с воронкой в личку доходит до ~1.0, у сериала на своей
    # аудитории — сотые доли. None, когда лайков или комментариев нет в снапшоте
    engagement: float | None = None
    # комментариев непропорционально много против нормы самого канала —
    # повод не выводить производственный бриф из этого ролика вслепую
    engagement_outlier: bool = False


class GroupStats(BaseModel):
    count: int
    median_views: float
    mean_views: float
    max_views: int
    top_video_id: str
    outlier_threshold: float = 0.0  # порог, фактически применённый к этой группе
    # норма вовлечения этой группы и порог, выше которого оно ненормально.
    # None — лайков/комментариев в снапшоте не было, судить не из чего
    median_engagement: float | None = None
    engagement_threshold: float | None = None


class OutlierRef(BaseModel):
    video_id: str
    title: str = ""
    group: str
    views: int
    ratio: float


class StatsReport(BaseModel):
    """02_stats/stats.json — динамика канала и выбросы."""

    computed_at: datetime
    threshold: float | Literal["auto"]  # как задано в analysis.yaml
    total_videos: int  # сколько видео в снапшоте
    counted_videos: int  # у скольких есть просмотры (только они в строках)
    cadence_per_week: float | None = None  # частота публикаций по всем датированным видео
    groups: dict[str, GroupStats] = Field(default_factory=dict)
    videos: list[VideoStats] = Field(default_factory=list)  # хронологически
    outliers: list[OutlierRef] = Field(default_factory=list)  # по убыванию ratio


# ---------------------------------------------------------------- 03_classify


class FormatDef(BaseModel):
    """Формат контента, который LLM разглядела в названиях канала."""

    id: str
    name: str = ""
    description: str = ""


class Taxonomy(BaseModel):
    """Ниша, аудитория и форматы канала — ответ LLM на весь список видео разом."""

    niche: str = ""
    audience: str = ""
    formats: list[FormatDef] = Field(default_factory=list)
    # прикидка RPM для этой ниши/языка, тем же форматом, что rpm_usd в analysis.yaml
    rpm_suggestion: dict[str, tuple[float, float]] = Field(default_factory=dict)
    notes: str = ""


class VideoClass(BaseModel):
    """Классификация одного видео."""

    video_id: str
    format: str = ""  # id формата из таксономии
    topic: str = ""
    title_pattern: str = ""  # структура заголовка: «как/почему + факт + место»


class FormatStats(BaseModel):
    format: str
    name: str = ""
    count: int = 0
    median_views: float = 0.0
    mean_views: float = 0.0
    top_video_id: str = ""


class ClassifyReport(BaseModel):
    """03_classify/classify.json — таксономия + классификация + просмотры по форматам."""

    computed_at: datetime
    llm: str  # какой LLM классифицировал: сменился — результат устарел
    taxonomy: Taxonomy
    videos: list[VideoClass] = Field(default_factory=list)
    formats: list[FormatStats] = Field(default_factory=list)  # по убыванию count


# ---------------------------------------------------------------- 04_deep


class Hook(BaseModel):
    type: str = ""  # question | shock_fact | story | claim | visual | other
    text: str = ""  # цитата реальных первых строк
    why_it_works: str = ""


class Beat(BaseModel):
    """Один бит структуры видео."""

    t: str = ""  # MM:SS
    beat: str = ""
    purpose: str = ""  # что бит делает для удержания


class DeepVideoAnalysis(BaseModel):
    """04_deep/<video_id>/analysis.json — устройство одного разобранного видео."""

    video_id: str
    llm: str = ""  # кто разбирал: сменился — разбор устарел
    role: VideoRole = "hit"  # контрольные разбираются тем же промптом, сравнивает формула
    transcript_language: str = ""
    hook: Hook = Field(default_factory=Hook)
    structure: list[Beat] = Field(default_factory=list)
    narration_style: str = ""
    curiosity_gaps: list[str] = Field(default_factory=list)
    cta: str = ""
    takeaways: list[str] = Field(default_factory=list)  # переносимые приёмы


VideoRole = Literal["hit", "control"]  # победитель канала или контрольный провал


class DeepIndexEntry(BaseModel):
    video_id: str
    status: Literal["done", "no_transcript", "failed"]
    role: VideoRole = "hit"
    note: str = ""


class DeepReport(BaseModel):
    """04_deep/deep.json — индекс: какие видео разобраны и чем кончилось."""

    computed_at: datetime
    llm: str
    videos: list[DeepIndexEntry] = Field(default_factory=list)


# ---------------------------------------------------------------- 07_distill
# Сам свод (Playbook, PlaybookRule, RuleCheck) — контракт ядра, core/playbook.py:
# его читает и конвейер эпизода, отбирая правила канала в промпт. Здесь — только
# вклад одного анализа.


class RuleVerdict(BaseModel):
    """Что этот канал говорит о существующем правиле."""

    id: str
    verdict: Verdict
    why: str = ""
    # ниша, в которой правило всё-таки держится, — заполняется только вместе с
    # "contradicts". Опровержение обычно не отменяет правило целиком, а
    # показывает его границу: то, что считалось общим, оказалось приёмом одной
    # ниши. Сузить область по этому полю может только python (см. playbook.merge).
    scope: str = ""


class DistillProposal(BaseModel):
    """07_distill/proposal.json — вклад одного анализа в playbook."""

    computed_at: datetime
    llm: str
    verdicts: list[RuleVerdict] = Field(default_factory=list)
    new_rules: list[PlaybookRule] = Field(default_factory=list)


# ---------------------------------------------------------------- 06_visual


class ShotStats(BaseModel):
    """Ритм монтажа: считается ffmpeg-детектом склеек, без LLM и без денег."""

    threshold: float = 0.0  # чувствительность детектора — без неё числа не прочесть
    duration_sec: float = 0.0
    shot_count: int = 0
    mean_shot_sec: float = 0.0
    median_shot_sec: float = 0.0
    p90_shot_sec: float = 0.0  # длинные планы: сколько держится самый спокойный кадр
    cuts_per_min: float = 0.0
    # склеек в минуту по пяти равным отрезкам ролика: ускоряется ли монтаж к финалу
    density: list[float] = Field(default_factory=list)


class ThumbnailAnalysis(BaseModel):
    elements: str = ""  # что изображено
    text: str = ""  # надпись на обложке, в оригинале
    why_clickable: str = ""


class VisualVideoAnalysis(BaseModel):
    """06_visual/<video_id>/visual.json — как ролик снят, а не что в нём сказано."""

    video_id: str
    llm: str = ""
    role: VideoRole = "hit"
    frames_analyzed: int = 0
    thumbnail_seen: bool = False  # без обложки поля thumbnail недостоверны
    shots: ShotStats = Field(default_factory=ShotStats)
    shot_language: str = ""  # планы и их чередование — одна фраза
    camera_moves: list[str] = Field(default_factory=list)
    staging_patterns: list[str] = Field(default_factory=list)  # как подают числа и сравнения
    text_on_screen: str = ""
    palette: str = ""
    thumbnail: ThumbnailAnalysis = Field(default_factory=ThumbnailAnalysis)
    transferable: list[str] = Field(default_factory=list)  # что повторимо нашим конвейером


class VisualIndexEntry(BaseModel):
    video_id: str
    status: Literal["done", "no_video", "failed"]
    role: VideoRole = "hit"
    note: str = ""


class VisualReport(BaseModel):
    """06_visual/visual.json — индекс: какие ролики разобраны визуально."""

    computed_at: datetime
    llm: str
    videos: list[VisualIndexEntry] = Field(default_factory=list)


# ---------------------------------------------------------------- 05_formula


class FormulaStep(BaseModel):
    step: str
    purpose: str = ""


class TitleFormula(BaseModel):
    pattern: str
    example: str = ""


class FormatAdvice(BaseModel):
    format: str
    verdict: str = ""  # делать | масштабировать | избегать …
    why: str = ""


class VideoIdea(BaseModel):
    """Идея по формуле — не копия существующего ролика."""

    title: str
    format: str = ""
    hook: str = ""
    outline: str = ""


class ShotRecipe(BaseModel):
    """Производственные числа монтажа — то, ради чего затевался визуальный разбор.

    `scene_duration_sec` отсюда попадает в project.yaml нового канала: сейчас
    это значение выбирается на глаз."""

    scene_duration_sec: float = 0.0
    shot_range_sec: str = ""
    cuts_per_min: float = 0.0
    rhythm: str = ""
    camera_moves: list[str] = Field(default_factory=list)
    staging: list[str] = Field(default_factory=list)


class Formula(BaseModel):
    """05_formula/formula.json — извлечённая формула контента канала."""

    computed_at: datetime
    llm: str  # кто извлекал: сменился — формула устарела
    what_works: list[str] = Field(default_factory=list)
    content_formula: list[FormulaStep] = Field(default_factory=list)
    title_formulas: list[TitleFormula] = Field(default_factory=list)
    hook_rules: list[str] = Field(default_factory=list)
    format_advice: list[FormatAdvice] = Field(default_factory=list)
    shot_recipe: ShotRecipe = Field(default_factory=ShotRecipe)
    production_notes: str = ""
    video_ideas: list[VideoIdea] = Field(default_factory=list)
    # чем хиты отличаются от контрольных провалов того же канала; пусто — контроля не было
    hit_vs_control: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)  # честные ограничения анализа
