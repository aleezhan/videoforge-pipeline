"""Стадия cut: сцена-отрезок исходника → клип со звуком речи и словами отрезка.

Первая стадия workflow нарезок. У сцены нет промпта — у неё есть `source`
(какой источник и какие секунды), и вся работа стадии в том, чтобы достать эти
секунды в полном разрешении и положить рядом слова отрезка.

Отличие от `ai_video`, из которого растут остальные стадии: **звук берётся из
клипа, а не синтезируется**. Поэтому `assets.audio` остаётся пустым (озвучки
нет и не будет), а `assets.captions` заполняется здесь же — из общего пословного
`.srt` источника, со сдвигом к нулю отрезка. Сборка это уже понимает:
`mux_scene` с `audio=None` берёт дорожку самого клипа, если она есть.

Два файла на сцену в `03_video/`:

- `<scene>_src.mp4` — докачанный кусок с запасом (`DOWNLOAD_PAD_SEC`), рядом
  `<scene>_src.json` с окном, по которому его качали: изменилось окно — файл
  устарел, и это видно без гаданий по mtime;
- `<scene>.mp4` — точный отрезок, тот, что пойдёт в сборку.

Запас нужен потому, что `yt-dlp --download-sections` режет по кейфреймам, а не
по секундам: точную границу ставит `cut_segment` уже локально.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError

from videoforge.clips.source import SOURCE_FILE, WORDS_FILE, BlurBox, SourceInfo, load_source
from videoforge.core.captions import parse_srt_words, slice_words, words_to_srt
from videoforge.core.ffmpeg import FPS, cut_segment, probe_duration, probe_fps
from videoforge.core.io import read_model, write_model
from videoforge.core.stage import Stage, StageContext, StageResult
from videoforge.core.storyboard import Scene
from videoforge.providers.base import ProviderError

OUT_DIR = "03_video"
AUDIO_DIR = "04_audio"

# Запас на докачку: `--download-sections` режет по кейфреймам, до нескольких
# секунд мимо. Точный срез делает ffmpeg из этого куска
DOWNLOAD_PAD_SEC = 2.0
# Запас на самом отрезке. Тайминги автосубтитров опережают звук на 0.1–0.3 с,
# поэтому граница «по словам» проходит чуть раньше самого слова: без запаса
# первое слово подрезано, последнее оборвано
EDGE_PAD_SEC = 0.3
MIN_CUT_SEC = 1.0  # короче — не момент, а опечатка в границах


def cut_window(scene: Scene, info: SourceInfo) -> tuple[float, float]:
    """Точные границы отрезка: момент плюс запас с обеих сторон, но не за
    пределы записи. Одна функция и для проверки свежести, и для самого среза —
    иначе они разъезжаются, и стадия режет заново каждый прогон."""
    assert scene.source is not None
    start = max(0.0, scene.source.start_sec - EDGE_PAD_SEC)
    end = scene.source.end_sec + EDGE_PAD_SEC
    return start, min(end, info.duration_sec) if info.duration_sec else end


class SourceCut(BaseModel):
    """Паспорт нарезанной сцены: что именно и откуда вырезано.

    Держит два окна, потому что их два: точное окно отрезка (`start`/`end` — по
    нему стадия понимает, что момент не менялся и резать заново нечего) и окно
    скачанного куска (`src_*` — по нему понятно, что сто мегабайт уже лежат на
    диске). У локального файла второе окно — весь файл."""

    source_id: str = ""
    url: str = ""
    file: str = ""  # локальный исходник, если не по ссылке
    start_sec: float
    end_sec: float
    src_start_sec: float = 0.0
    src_end_sec: float = 0.0
    # что размыто на отрезке (`SourceInfo.blur`): поменяли прямоугольники в
    # source.json — отрезок режется заново. Пусто — паспорт до размытия
    blur: list[BlurBox] = []

    def same_window(self, start_sec: float, end_sec: float) -> bool:
        return abs(self.start_sec - start_sec) < 0.05 and abs(self.end_sec - end_sec) < 0.05


def episode_fps(clips: list[tuple[str, float, float]]) -> tuple[float, str | None]:
    """Частота кадров эпизода по его отрезкам: (id сцены, частота, секунды).

    Один источник — его частота, и кадры не пересчитываются вовсе: до
    14.09.2026 сборка шла в 24, и у 25-кадрового подкаста каждый 25-й кадр
    выбрасывался ещё на нарезке (h13: 1055 кадров вместо 1099 на 43.96 с) —
    движение рук и губ спотыкалось раз в секунду. Несколько источников с
    разной частотой — частота той, на которую приходится больше секунд
    материала: пересчёт достаётся меньшей части ролика, и второе значение —
    предупреждение с именами сцен, которые сборка приведёт к чужой частоте."""
    if not clips:
        return float(FPS), None
    seconds: dict[float, float] = {}
    for _, rate, sec in clips:
        seconds[rate] = seconds.get(rate, 0.0) + sec
    chosen = max(seconds, key=lambda r: seconds[r])
    if len(seconds) == 1:
        return chosen, None
    others = ", ".join(f"{sid} ({rate:g})" for sid, rate, _ in clips if rate != chosen)
    return chosen, (
        f"источники с разной частотой кадров: эпизод идёт в {chosen:g} (самый длинный "
        f"материал), сборка пересчитает {others}"
    )


def _cut_path(out_dir: Path, scene_id: str) -> Path:
    return out_dir / f"{scene_id}_src.json"


def _load_cut(out_dir: Path, scene_id: str) -> SourceCut | None:
    path = _cut_path(out_dir, scene_id)
    if not path.exists():
        return None
    try:
        return read_model(path, SourceCut)
    except (ValidationError, ValueError):
        return None


class CutStage(Stage):
    name = "cut"
    needs = ()  # ни одного провайдера с ключом: yt-dlp и ffmpeg

    def estimate_cost(self, ctx: StageContext) -> float:
        return 0.0  # трафик и диск, но не деньги

    @staticmethod
    def _source_dir(ctx: StageContext, scene: Scene) -> Path:
        """`Scene.source.path` — путь от папки ПРОЕКТА, а не эпизода.

        Из одного подкаста выходит десяток шортсов, копировать его в каждый
        эпизод незачем. `ctx.abs()` здесь не годится: он считает от эпизода."""
        assert scene.source is not None
        root = ctx.project_root or ctx.project_dir.resolve().parent.parent
        return root / scene.source.path

    def _ready(self, ctx: StageContext, scene: Scene, out_dir: Path, info: SourceInfo) -> bool:
        """Отрезок нарезан на то самое окно из того же транскрипта, и оба файла
        на месте. Момент поправили руками в раскадровке — окно разошлось, режем
        заново; перечитали транскрипт источника — слова отрезка устарели, и
        шортс остался бы со старыми субтитрами."""
        if scene.source is None or not scene.asset_ready("video", self.name):
            return False
        if not (scene.assets.video and ctx.abs(scene.assets.video).exists()):
            return False
        if not (scene.assets.captions and ctx.abs(scene.assets.captions).exists()):
            return False
        made = _load_cut(out_dir, scene.id)
        start, end = cut_window(scene, info)
        if made is None or not made.same_window(start, end):
            return False
        if made.blur != info.blur:
            return False  # размытие поменяли — вывеска в старом отрезке осталась
        words = self._source_dir(ctx, scene) / WORDS_FILE
        srt = ctx.abs(scene.assets.captions)
        return srt.stat().st_mtime >= words.stat().st_mtime

    def _plan(
        self, ctx: StageContext, scenes: list[Scene], out_dir: Path
    ) -> tuple[list[tuple[Scene, Path, SourceInfo]], str | None]:
        """Что резать: сцена, папка её источника и его метаданные. Источник
        читается один раз на папку — из одного подкаста может выйти несколько
        сцен одного эпизода."""
        cache: dict[Path, SourceInfo] = {}
        todo: list[tuple[Scene, Path, SourceInfo]] = []
        for scene in scenes:
            sdir = self._source_dir(ctx, scene)
            info = cache.get(sdir)
            if info is None:
                loaded = load_source(sdir)
                if loaded is None:
                    return [], (
                        f"сцена {scene.id}: нет {sdir / SOURCE_FILE} — источник не подготовлен "
                        f"(videoforge source add)"
                    )
                cache[sdir] = info = loaded
            if not (sdir / WORDS_FILE).exists():
                return [], (
                    f"сцена {scene.id}: нет {sdir / WORDS_FILE} — у источника нет транскрипта"
                )
            if ctx.options.force or not self._ready(ctx, scene, out_dir, info):
                todo.append((scene, sdir, info))
        return todo, None

    def run(self, ctx: StageContext) -> StageResult:
        scenes = ctx.storyboard.ordered_scenes()
        with_source = [s for s in scenes if s.source is not None]
        if not with_source:
            return StageResult(
                status="failed",
                message=(
                    "ни у одной сцены нет source: эпизод-нарезка создаётся из выбранного "
                    "момента (videoforge source highlights → source pick), а не вручную"
                ),
            )

        out_dir = ctx.stage_dir(OUT_DIR)
        audio_dir = ctx.stage_dir(AUDIO_DIR)
        todo, error = self._plan(ctx, with_source, out_dir)
        if error:
            return StageResult(status="failed", message=error)
        skipped = [s.id for s in scenes if s.source is None]
        note = f" · без source пропущены: {skipped}" if skipped else ""
        if not todo:
            return StageResult(status="done", message=f"все отрезки уже нарезаны{note}")

        changed = False
        for scene, sdir, info in todo:
            assert scene.source is not None
            start, end = cut_window(scene, info)
            if end - start < MIN_CUT_SEC:
                return StageResult(
                    status="failed",
                    message=f"сцена {scene.id}: отрезок {end - start:.1f} с — нечего резать",
                    changed=changed,
                )

            try:
                with ctx.step("докачка куска", f"{scene.id}: {end - start:.0f} с") as fetched:
                    src, window = self._fetch(ctx, scene, info, out_dir, start=start, end=end)
                    fetched.note += f", {src.stat().st_size / 1e6:.0f} МБ"
                clip = out_dir / f"{scene.id}.mp4"
                # отрезок режется в частоте самого исходника: 25 остаётся 25, а
                # не приводится к 24 с потерей кадра в секунду. Частоту эпизода
                # из этого выводит `episode_fps` ниже; не прочиталась — 24
                src_fps = probe_fps(src)
                if src_fps is None:
                    ctx.log.warning(
                        "сцена %s: частота кадров %s не прочиталась — режу в %d",
                        scene.id, src.name, FPS,
                    )  # fmt: skip
                with ctx.step("точный отрезок", f"{scene.id}: {src_fps or FPS:g} к/с"):
                    length = cut_segment(
                        src,
                        clip,
                        start_sec=start - window[0],
                        duration_sec=end - start,
                        fps=src_fps or FPS,
                        blur=[(b.x, b.y, b.w, b.h) for b in info.blur] or None,
                    )
            except ProviderError as exc:
                return StageResult(
                    status="failed", message=f"сцена {scene.id}: {exc}", changed=changed
                )

            with ctx.step("слова отрезка", scene.id):
                words = slice_words(
                    parse_srt_words((sdir / WORDS_FILE).read_text(encoding="utf-8")), start, end
                )
                # Запас по краям нужен звуку, а не субтитрам: слово, которое
                # целиком лежит в запасе, принадлежит соседней мысли — обрывок
                # «сравнение.» на нулевой секунде и «да, да, я» после панчлайна.
                # На караоке это мелькало по слову, фразой-переводом стало
                # строкой на экране. Слово на границе момента остаётся: оно
                # подрезано и звучит в кадре
                lead = scene.source.start_sec - start
                trail = end - scene.source.end_sec
                words = [w for w in words if lead <= (w.start + w.end) / 2 <= (end - start) - trail]
                srt = audio_dir / f"{scene.id}.srt"
                srt.write_text(words_to_srt(words), encoding="utf-8")
            write_model(
                _cut_path(out_dir, scene.id),
                SourceCut(
                    source_id=info.id,
                    url=info.url,
                    file=info.file,
                    start_sec=start,
                    end_sec=end,
                    src_start_sec=window[0],
                    src_end_sec=window[1],
                    blur=info.blur,
                ),
            )

            scene.assets.video = ctx.rel(clip)
            scene.assets.captions = ctx.rel(srt)
            scene.assets.audio = None  # речь в клипе; синтезировать нечего
            scene.provenance["video"] = self.name
            scene.checks.video = "pass" if length > 0 else "fail"
            scene.checks.audio = "skipped"
            scene.measured_sec["video"] = round(length, 3)
            # докуда в клипе идёт речь: отсюда монтажёр берёт хвост тишины,
            # а сборка — покрытие субтитрами
            scene.measured_sec["audio"] = round(words[-1].end, 3) if words else 0.0
            scene.duration_sec = round(length, 2)
            scene.attempts["video"] = scene.attempts.get("video", 0) + 1
            if not words:
                ctx.log.warning(
                    "сцена %s: в отрезке %.0f–%.0f с нет слов транскрипта — субтитров не будет",
                    scene.id,
                    start,
                    end,
                )
            changed = True
            ctx.save()

        # частота эпизода — по клипам, которые лежат на диске (только что
        # нарезанные и старые): это и есть то, что сборка склеит. Пишется
        # только когда стадия резала — у эпизода, нарезанного до этого поля,
        # клипы уже в 24, и пустое поле говорит о них правду
        rates = [
            (s.id, rate, s.duration_sec)
            for s in with_source
            if s.assets.video
            and ctx.abs(s.assets.video).exists()
            and (rate := probe_fps(ctx.abs(s.assets.video))) is not None
        ]
        fps, mixed = episode_fps(rates)
        if mixed:
            ctx.log.warning(mixed)
        ctx.storyboard.fps = fps
        ctx.save()

        total = sum(s.duration_sec for s in with_source)
        note = f" · без source пропущены: {skipped}" if skipped else ""
        if mixed:
            note += f" · ПРОВЕРЬ: {mixed}"
        return StageResult(
            status="done",
            message=f"нарезано сцен: {len(todo)} · {total:.1f} с материала · {fps:g} к/с{note}",
            changed=changed,
        )

    @staticmethod
    def _fetch(
        ctx: StageContext,
        scene: Scene,
        info: SourceInfo,
        out_dir: Path,
        *,
        start: float,
        end: float,
    ) -> tuple[Path, tuple[float, float]]:
        """Материал для точного среза: (файл, какое окно исходника в нём лежит).

        Локальный файл режется на месте — качать нечего, и в нём лежит всё.
        Ссылка докачивается куском с запасом и переиспользуется, пока окно
        накрывает нужное: второй прогон не должен снова тянуть сто мегабайт."""
        if info.file:
            path = Path(info.file)
            if not path.exists():
                raise ProviderError(f"локального исходника нет: {path}")
            return path, (0.0, info.duration_sec or probe_duration(path))
        if not info.url:
            raise ProviderError("у источника нет ни ссылки, ни файла")

        from videoforge.providers.youtube import download_section

        src = out_dir / f"{scene.id}_src.mp4"
        lo = max(0.0, start - DOWNLOAD_PAD_SEC)
        hi = end + DOWNLOAD_PAD_SEC
        made = _load_cut(out_dir, scene.id)
        if (
            src.exists()
            and made is not None
            and made.src_start_sec <= lo
            and made.src_end_sec >= hi
        ):
            ctx.log.info("сцена %s: кусок уже скачан (%s)", scene.id, src.name)
            return src, (made.src_start_sec, made.src_end_sec)
        src.unlink(missing_ok=True)
        ctx.log.info("сцена %s: качаю %.0f–%.0f с из %s", scene.id, lo, hi, info.url)
        download_section(info.url, src, start_sec=lo, end_sec=hi)
        ctx.log.info("сцена %s: скачано %.1f с", scene.id, probe_duration(src))
        return src, (lo, hi)
