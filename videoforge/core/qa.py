"""QA готового файла — совет человеку на воротах, не водитель цикла.

Лежит в `05_assemble/qa.json`, пишет стадия `qa` после сборки. Четыре источника:
замеры сборки вне порогов (уже посчитаны в `FinalOutput`), зона интерфейса
площадки против полосы субтитров (детерминированно, по числам стиля), разбор
стыков vision-моделью — два кадра по обе стороны каждой склейки с коротким
чеклистом (виден ли шов, читаемы ли субтитры, не закрывают ли они предмет) — и
отдельные кадры внутри эпизода.

Кадры внутри появились после первого шортса: разбор стыков смотрит только на
СОСЕДЕЙ СКЛЕЕК, а у шортса сцена одна — стыков ноль, и картинку не проверял
никто. Нечитаемые субтитры на светлом кадре нашёл человек глазами, а не
конвейер. Середина длинной сцены в эпизоде из многих сцен была той же слепой
зоной, просто менее заметной.

Почему совет, а не цикл: рендер десятиминутного эпизода — минуты, вердикт
vision-модели плавает между прогонами. Любой дефект, который можно измерить,
переводится в детерминированное правило рендерера или линта; здесь остаётся то,
что измерить нельзя, и решает по нему человек.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from videoforge.core.io import read_model

ASSEMBLE_DIR = "05_assemble"
QA_FILE = "qa.json"
QA_FRAMES_DIR = "qa"  # кадры вокруг стыков, относительно 05_assemble/

Severity = Literal["warn", "info"]
SeamVerdict = Literal["ok", "visible", "not_assessed"]
CaptionVerdict = Literal["ok", "hard_to_read", "covers_subject", "none", "not_assessed"]
FramingVerdict = Literal["ok", "subject_cut", "no_subject", "not_assessed"]
# Реклама ставок и казино в кадре исходника (например, баннер спонсора на
# записи стрима). Момент при этом не выбрасывается — рекламу обрезают кропом
# или размывают (`BlurBox` источника)
AdVerdict = Literal["none", "betting", "not_assessed"]


class QaFinding(BaseModel):
    code: str
    severity: Severity = "warn"
    message: str
    scene_id: str | None = None


class CutReview(BaseModel):
    """Один стык: кадр до и кадр после, вердикт модели."""

    prev: str
    next: str
    at_sec: float
    transition: str = "cut"  # как стык собран: cut | crossfade | dip_black | fade_in
    frames: list[str] = Field(default_factory=list)  # до / после, относительно папки эпизода
    seam: SeamVerdict = "not_assessed"
    captions: CaptionVerdict = "not_assessed"
    note: str = ""

    @property
    def cut_id(self) -> str:
        return f"{self.prev}>{self.next}"

    @property
    def clean(self) -> bool:
        return self.seam in ("ok", "not_assessed") and self.captions in (
            "ok",
            "none",
            "not_assessed",
        )


class FrameReview(BaseModel):
    """Один кадр внутри эпизода — там, где стыков нет и смотреть больше некому."""

    scene_id: str = ""
    at_sec: float
    frame: str = ""  # относительно папки эпизода
    captions: CaptionVerdict = "not_assessed"
    framing: FramingVerdict = "not_assessed"
    ad: AdVerdict = "not_assessed"
    note: str = ""

    @property
    def name(self) -> str:
        return Path(self.frame).name

    @property
    def clean(self) -> bool:
        return (
            self.captions in ("ok", "none", "not_assessed")
            and self.framing in ("ok", "not_assessed")
            and self.ad != "betting"
        )


class QaReport(BaseModel):
    llm: str = ""
    generated_at: datetime | None = None
    final_fingerprint: str | None = None  # к какой сборке относится
    findings: list[QaFinding] = Field(default_factory=list)
    cuts: list[CutReview] = Field(default_factory=list)
    frames: list[FrameReview] = Field(default_factory=list)

    @property
    def warnings(self) -> list[QaFinding]:
        return [f for f in self.findings if f.severity == "warn"]


def qa_path(episode_dir: Path) -> Path:
    return episode_dir / ASSEMBLE_DIR / QA_FILE


def load_qa(episode_dir: Path) -> QaReport | None:
    path = qa_path(episode_dir)
    return read_model(path, QaReport) if path.exists() else None
