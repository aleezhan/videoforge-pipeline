"""Программное движение вместо генеративного: кадр сцены -> клип через Remotion.

Для стилей типа flat2d_explainer генеративное i2v ломает саму суть кадра
(замерено 31.08.2026: морфинг схемы, выдуманные элементы), а нужное им движение —
«один медленный ход камеры по неподвижной иллюстрации» — детерминировано.
Провайдер разбирает motion_prompt на примитивы (наезд/отъезд/панорама) и
рендерит их Remotion-композицией `remotion/` в корне репозитория. Стоимость 0,
любая длительность — потолок клипа и заморозка кадра исчезают по построению.

Требует Node.js и `npm install` в папке remotion/ (см. doctor). Отсутствие —
честная ошибка, не фолбэк на другого провайдера.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from videoforge.core.proc import NO_WINDOW
from videoforge.core.storyboard import ChainLink
from videoforge.providers.base import GenResult, MotionGuide, ProviderError, VideoProvider

COMPOSITION = "SceneMotion"
RENDER_TIMEOUT_SEC = 600
FPS = 24

# aspect_ratio раскадровки -> размер кадра
SIZES: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

# сила движения: доля зума за клип
STRENGTH_DEFAULT = 0.10
STRENGTH_SLOW = 0.08
STRENGTH_FAST = 0.16
# потолок силы — тот же, что у схемы композиции (`strength: max(0.5)`). Цепочка
# продолжений держит скорость головы, и на длинной цепочке ход растёт с её
# длиной: fast 0.16 на четырёх звеньях по 6 с — 0.64. Выше потолка ход
# ограничивается: медленнее, но ровно — потолок один на все звенья, и стыка нет
STRENGTH_MAX = 0.5


def _forms(verb: str, tail: str) -> tuple[str, ...]:
    """Глагол во всех формах, какими его пишет планнер: pull/pulls/pulling back."""
    stem = verb.rstrip("e")  # move -> moving
    return (f"{verb} {tail}", f"{verb}-{tail}", f"{verb}s {tail}", f"{stem}ing {tail}")


# порядок важен: «pull out» должен победить «out», «pan left» — просто «left»
_MOTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "pull_out",
        _forms("pull", "out")
        + _forms("pull", "back")
        + _forms("pull", "away")
        + _forms("zoom", "out")
        + _forms("dolly", "out"),
    ),
    (
        "push_in",
        _forms("push", "in")
        + _forms("push", "toward")
        + _forms("zoom", "in")
        + _forms("dolly", "in")
        + _forms("move", "in"),
    ),
    ("pan_left", _forms("pan", "left") + ("pan to the left",)),
    ("pan_right", _forms("pan", "right") + ("pan to the right",)),
    ("tilt_up", _forms("tilt", "up") + _forms("crane", "up")),
    ("tilt_down", _forms("tilt", "down") + _forms("crane", "down")),
    ("static", ("static", "locked", "no camera movement", "camera holds", "fixed camera")),
)


@dataclass(frozen=True)
class MotionParams:
    motion: str = "push_in"
    strength: float = STRENGTH_DEFAULT

    def props(self) -> dict[str, float | str]:
        return dict(asdict(self))


def parse_motion(prompt: str) -> MotionParams:
    """motion_prompt раскадровки -> примитив камеры.

    Промпт пишется планнером для генеративных моделей; здесь из него берётся
    только тип движения и темп. Нераспознанное — медленный наезд: по замерам
    kurzgesagt_de это основной ход стиля.
    """
    # блок «Tempo:» стадии video адресован i2v-модели («fast sharp movements,
    # no slow motion»), а не программной камере: без этой строки каждая сцена
    # с действием получала бы быстрый наезд, хотя её motion_prompt о темпе молчит
    text = "\n".join(
        line for line in prompt.splitlines() if not line.lower().startswith("tempo:")
    ).lower()
    motion = "push_in"
    for name, needles in _MOTIONS:
        if any(needle in text for needle in needles):
            motion = name
            break
    strength = STRENGTH_DEFAULT
    if any(word in text for word in ("slow", "gentle", "subtle", "barely")):
        strength = STRENGTH_SLOW
    if any(word in text for word in ("fast", "quick", "rapid")):
        strength = STRENGTH_FAST
    return MotionParams(motion=motion, strength=strength)


def motion_props(
    prompt: str, *, duration_sec: int, size: tuple[int, int], chain: ChainLink | None = None
) -> dict[str, object]:
    """Параметры композиции SceneMotion без файла кадра: что именно поедет.

    Продолжение плана (`chain`) — тот же ход камеры, что у одного клипа, но
    длиной во весь план: сила умножается на `total/head`, чтобы скорость
    осталась скоростью головы (голова в цепочке едет ровно так, как ехала бы
    одна), а клип проходит свой отрезок пути `[before, before + клип] / total`.
    Верхняя граница считается по длине КЛИПА, а не звена: клип на хвост
    округления длиннее звена (`pick_duration` — ceil), сборка режет его по
    звену, и в точке среза ход ровно там, откуда стартует следующее звено.
    У последнего звена отрезок выходит за 1.0 на тот же хвост — его отрезают.

    До 14.09.2026 каждое звено шло по всему пути `[0, 1]`: наезд начинался
    заново с исходного масштаба, и на стыке кадр отпрыгивал на всю силу хода
    (8–16% масштаба за один кадр) — QA дважды называл это дефектом. Замер
    14.09.2026 (два звена по 3 с, slow push in, кадр из цветных клеток 540×960,
    разность яркости соседних кадров 0..255): стык 15.9 → 0.33 при 0.44 между
    соседними кадрами внутри звена; последний кадр головы в цепочке совпал с
    последним кадром одиночного клипа — 0.00."""
    params = parse_motion(prompt)
    width, height = size
    props: dict[str, object] = {
        "durationSec": duration_sec,
        "fps": FPS,
        "width": width,
        "height": height,
        **params.props(),
    }
    if chain is not None:
        if chain.total_sec <= 0 or chain.head_sec <= 0:
            raise ProviderError(f"цепочка продолжений без длины: {chain}")
        props["strength"] = round(
            min(STRENGTH_MAX, params.strength * chain.total_sec / chain.head_sec), 6
        )
        props["pathFrom"] = round(chain.before_sec / chain.total_sec, 6)
        props["pathTo"] = round((chain.before_sec + duration_sec) / chain.total_sec, 6)
    return props


def _default_project_dir() -> Path:
    # videoforge/providers/remotion_video.py -> корень репозитория -> remotion/
    return Path(__file__).resolve().parents[2] / "remotion"


class RemotionVideo(VideoProvider):
    name = "remotion"
    chains_camera = True  # камера программная: продолжение плана начинает ход с места
    # Рендер программный, потолка длительности клипа нет; 60 с — санитарный
    # предел одной сцены. Стадия озвучки читает max(supported_durations) как
    # потолок и обрезает по нему длительности сцен — заглушка (6,) на живом
    # прогоне 01.09.2026 обрезала 7-секундную реплику до 6 с.
    supported_durations = (60,)

    def __init__(self, project_dir: Path | None = None) -> None:
        env_dir = os.environ.get("VF_REMOTION_DIR")
        self.project_dir = project_dir or (Path(env_dir) if env_dir else _default_project_dir())

    def pick_duration(self, wanted: float) -> int:
        """Любая длительность: клип ровно под речь, без потолка провайдера."""
        return max(1, math.ceil(wanted))

    def price_for(self, duration: int, *, guided: bool = False) -> float:
        return 0.0

    def _npx(self) -> str:
        npx = shutil.which("npx")
        if not npx:
            raise ProviderError("Node.js не найден (нужен npx) — установи с nodejs.org")
        return npx

    def _check_project(self) -> None:
        if not (self.project_dir / "package.json").exists():
            raise ProviderError(f"нет Remotion-проекта в {self.project_dir}")
        if not (self.project_dir / "node_modules").exists():
            raise ProviderError(
                f"зависимости Remotion не установлены: cd {self.project_dir}; npm install"
            )

    def not_ready(self) -> str:
        """Node.js и `npm install` в remotion/ — то же, что проверяет `animate`
        перед рендером, только без рендера."""
        try:
            self._npx()
            self._check_project()
        except ProviderError as exc:
            return str(exc)
        return ""

    def animate(
        self,
        image: Path,
        prompt: str,
        *,
        duration_sec: int,
        aspect_ratio: str,
        out_path: Path,
        chain: ChainLink | None = None,
        motion_reference: MotionGuide | None = None,
    ) -> GenResult:
        if motion_reference is not None:
            # программная камера двигает неподвижный кадр — скелету в ней
            # следовать нечему; подменять референс наездом молча нельзя
            raise ProviderError(
                f"{self.name}: референс движения не поддерживается "
                f"({motion_reference.path.name}) — нужен провайдер comfy"
            )
        npx = self._npx()
        self._check_project()
        if aspect_ratio not in SIZES:
            raise ProviderError(f"неизвестный aspect_ratio '{aspect_ratio}': {sorted(SIZES)}")
        motion = motion_props(
            prompt, duration_sec=duration_sec, size=SIZES[aspect_ratio], chain=chain
        )

        # staticFile() читает только public/ — кладём кадр туда под уникальным именем
        scene_copy = self.project_dir / "public" / f"__scene__{uuid.uuid4().hex}{image.suffix}"
        shutil.copyfile(image, scene_copy)
        props: dict[str, object] = {"imageSrc": scene_copy.name, **motion}
        fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="vf_motion_")
        os.close(fd)  # Windows: незакрытый дескриптор не даст удалить файл в finally
        props_file = Path(tmp_name)
        props_file.write_text(json.dumps(props), encoding="utf-8")
        try:
            run = subprocess.run(
                [
                    npx,
                    "remotion",
                    "render",
                    "src/index.ts",
                    COMPOSITION,
                    str(out_path.resolve()),
                    f"--props={props_file}",
                    "--codec=h264",
                    "--log=error",
                ],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=RENDER_TIMEOUT_SEC,
                **NO_WINDOW,
            )
            if run.returncode != 0 or not out_path.exists():
                tail = (run.stderr or run.stdout or "").strip()[-500:]
                raise ProviderError(f"remotion render упал: {tail}")
        finally:
            # зачистки независимы: сбой одной не должен оставить хвост от другой
            for leftover in (props_file, scene_copy):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
        meta = {
            k: v for k, v in motion.items() if k in ("motion", "strength", "pathFrom", "pathTo")
        }
        return GenResult(out_path, 0.0, {**meta, "duration": duration_sec})
