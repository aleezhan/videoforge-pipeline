"""Стадия edit: материал → 05_edit/sources.json + edit_plan.json.

Монтажёр — модель, но решает она немного и под присмотром. Она получает
измеренный материал (`sources.json`: коробка, клип, речь, хвост тишины), карту
смен сценариста (`01_script/retention.json`), правила канала, blueprint и
приёмы библиотеки по своим точкам решения (`decisions`; условия ролика —
`library.conditions` из раскадровки, project.yaml и workflow) — и
возвращает только ОТКЛОНЕНИЯ от заготовки «hard cut везде»: переходы, акценты,
ступени музыки и — с 14.09.2026 (`features/edit_model_effects`) — четыре
списка таймлайна: эффекты кадра, звуки из `resources/sfx/`, ступени музыки в
произвольных секундах и замедленный хвост; каждое с `why`. Python сливает их
в полный план и проверяет линтом (`edit_lint`). Блок линта → один повторный
вызов с текстом причин (выдуманный id звука — блок, и это как раз такой
случай); блоки после повтора → план сохраняется как вернула модель, стадия
`failed`, решает человек. Модель никогда не управляет ffmpeg: сборка исполняет
файл, а не ответ.

Свежесть:
- `author: manual` — стадия не переписывает никогда, только проверяет и чинит
  очевидное: правка человека побеждает модель;
- `author: llm` — пропуск, если файл новее своих входов (`sources.json`,
  `retention.json`) и сделан текущей моделью, как у сценариста; `--force`
  обнуляет. `storyboard.json` входом по mtime быть не может — раннер
  пересохраняет его после каждой стадии, — поэтому его представляет
  `sources.json`: стадия переписывает его только когда материал реально
  изменился (набор сцен, клипы, длины, текст);
- всё остальное (плана нет, заготовка `default` старого образца) → модель.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from videoforge.core.editplan import (
    EDIT_DIR,
    EditPlan,
    EffectCue,
    MusicPlan,
    Outro,
    SceneEdit,
    SfxCue,
    SourceClip,
    Sources,
    default_plan,
    load_plan,
    plan_path,
    save_plan,
    sources_path,
)
from videoforge.core.ffmpeg import FfmpegError, probe_duration
from videoforge.core.io import read_model, write_model
from videoforge.core.library import StageBrief, conditions, for_stage
from videoforge.core.motion import MotionProfile, load_or_measure
from videoforge.core.pauses import scene_lead_in
from videoforge.core.project import SCRIPT_DIR
from videoforge.core.resources import (
    CaptionStyle,
    MusicTrack,
    Sfx,
    list_resources,
    load_resource,
)
from videoforge.core.script import RETENTION_FILE, Retention
from videoforge.core.stage import Stage, StageContext, StageResult
from videoforge.core.storyboard import Scene, Storyboard
from videoforge.providers.base import ProviderError
from videoforge.stages import edit_prompts as p
from videoforge.stages.edit_lint import LintResult, lint_plan, silent_tail
from videoforge.stages.llm_json import ask_json

LLM_ATTEMPTS = 2  # ответ + один повтор по блокам линта; третьего нет — дальше человек
# что модель вправе менять в сцене. Отрезки (`segments`) — этап 4, когда у сцены
# появятся варианты клипов; порядок и длительность — никогда
SCENE_FIELDS = ("transition_in", "captions", "emphasis", "music_gain_db", "why")
NOTES_LLM = (
    "План монтажа от модели: только отклонения от заготовки «hard cut везде» (переходы, "
    "акценты, ступени музыки, эффекты, звуки, замедленный хвост), слитые в полный план и "
    "проверенные линтом стадии edit. Правь руками и ставь author: manual — иначе стадия "
    "перепишет файл, когда изменится материал, retention.json или модель."
)


def describe_sources(ctx: StageContext, scenes: list[Scene]) -> Sources:
    clips: list[SourceClip] = []
    for s in scenes:
        if not s.assets.video:
            continue
        path = ctx.abs(s.assets.video)
        length = probe_duration(path) if path.exists() else 0.0
        speech = s.measured_sec.get("audio", 0.0)
        profile = MotionProfile()
        if path.exists():
            try:
                profile = load_or_measure(path)
            except FfmpegError as exc:
                ctx.log.warning("сцена %s: движение не измерено: %s", s.id, exc)
        clips.append(
            SourceClip(
                scene_id=s.id,
                path=s.assets.video,
                duration_sec=round(length, 3),
                box_sec=s.duration_sec,
                speech_sec=round(speech, 3),
                lead_in_sec=round(scene_lead_in(s), 3),
                silent_tail_sec=round(silent_tail(s), 3),
                provider=s.provenance.get("video", ""),
                narration=s.narration,
                motion_step_sec=profile.step_sec,
                motion=profile.energy,
            )
        )
    return Sources(generated_at=datetime.now(UTC), clips=clips)


def write_sources(ctx: StageContext, sources: Sources) -> bool:
    """Переписать sources.json, только если материал изменился: его mtime — вход
    свежести плана, и переписывать его каждый прогон значило бы звать модель
    каждый прогон."""
    path = sources_path(ctx.project_dir)
    if path.exists():
        try:
            if read_model(path, Sources).clips == sources.clips:
                return False
        except (ValidationError, ValueError):
            pass
    write_model(path, sources)
    return True


def lint_with_material(
    plan: EditPlan,
    storyboard: Storyboard,
    *,
    episode_dir: Path,
    resource_dirs: list[Path],
) -> LintResult:
    """Линт с доступом к материалу: длины клипов меряются, ресурсы ищутся там же,
    где их ищут остальные стадии.

    Отдельно от `run_lint`, потому что зовёт его не только стадия: правку плана
    из пульта проверяет ТОТ ЖЕ линт, иначе браузер стал бы вторым, более
    добрым судьёй — и в сборку поехало бы то, что стадия не пропустила бы."""

    def abs_path(rel: str) -> Path:
        return episode_dir / rel

    def duration_of(rel: str) -> float | None:
        path = abs_path(rel)
        try:
            path.resolve().relative_to(episode_dir.resolve())
        except ValueError:
            return None  # источник вне папки эпизода — не наш материал
        return probe_duration(path) if path.exists() else None

    def has(kind: str, rid: str, model: type) -> bool:
        try:
            load_resource(kind, rid, model, resource_dirs)
            return True
        except FileNotFoundError:
            return False

    def motion_of(rel: str) -> MotionProfile | None:
        path = abs_path(rel)
        if not path.exists():
            return None
        try:
            return load_or_measure(path)
        except FfmpegError:
            return None

    return lint_plan(
        plan,
        storyboard,
        duration_of=duration_of,
        style_exists=lambda rid: has("caption_styles", rid, CaptionStyle),
        track_exists=lambda rid: has("music", rid, MusicTrack),
        sfx_exists=lambda rid: has("sfx", rid, Sfx),
        motion_of=motion_of,
    )


def run_lint(ctx: StageContext, plan: EditPlan) -> LintResult:
    """Тот же линт, но по контексту стадии."""
    return lint_with_material(
        plan,
        ctx.storyboard,
        episode_dir=ctx.project_dir,
        resource_dirs=ctx.resource_dirs,
    )


# ------------------------------------------------------------ ответ модели → план


def merge_deviations(base: EditPlan, raw: Any, *, llm: str) -> EditPlan:
    """Отклонения из ответа модели → полный план поверх заготовки.

    Модель отвечает только тем, что меняет: так ответ короче, а значит быстрее.
    Неизвестная сцена здесь не отбрасывается — её назовёт линт, и модель получит
    это причиной повтора. Форму проверяет pydantic; кривое поле — ValueError с
    текстом, который тоже уходит модели."""
    if not isinstance(raw, dict):
        raise ValueError(f"ожидался объект с отклонениями, пришло {type(raw).__name__}")
    plan = base.model_copy(deep=True)
    plan.author, plan.llm = "llm", llm
    plan.generated_at, plan.notes = datetime.now(UTC), NOTES_LLM
    try:
        if raw.get("caption_style"):
            plan.caption_style = str(raw["caption_style"])
        if isinstance(raw.get("music"), dict):
            plan.music = MusicPlan.model_validate(raw["music"])
        for key in ("fade_in_sec", "fade_out_sec"):
            if raw.get(key) is not None:
                setattr(plan, key, float(raw[key]))
        # таймлайн: списки заготовки пусты, хвоста нет — пропущенное так и
        # остаётся «ничего». Неизвестный вид эффекта или кривая секунда — ошибка
        # схемы с текстом для повтора; выдуманный id звука схему проходит, его
        # назовёт линт (`unknown_sfx`), и модель получит это причиной повтора
        for key, model in (("effects", EffectCue), ("sfx", SfxCue)):
            items = raw.get(key) or []
            if not isinstance(items, list):
                raise ValueError(f"`{key}` должен быть списком записей по секундам ролика")
            setattr(plan, key, [model.model_validate(item) for item in items])
        if raw.get("outro") is not None:
            if not isinstance(raw["outro"], dict):
                raise ValueError("`outro` — объект с take_sec и speed или null")
            plan.outro = Outro.model_validate(raw["outro"])
        scenes = raw.get("scenes") or []
        if not isinstance(scenes, list):
            raise ValueError("`scenes` должен быть списком записей по сценам")
        index = {e.scene_id: i for i, e in enumerate(plan.scenes)}
        for item in scenes:
            if not isinstance(item, dict) or not item.get("scene_id"):
                raise ValueError(f"запись сцены без scene_id: {str(item)[:80]}")
            sid = str(item["scene_id"])
            if sid not in index:
                index[sid] = len(plan.scenes)
                plan.scenes.append(SceneEdit(scene_id=sid))
            entry = plan.scenes[index[sid]]
            patch = {k: item[k] for k in SCENE_FIELDS if item.get(k) is not None}
            if isinstance(patch.get("transition_in"), str):
                patch["transition_in"] = {"type": patch["transition_in"]}
            if isinstance(patch.get("emphasis"), str):
                patch["emphasis"] = [patch["emphasis"]]
            if entry.why and patch.get("why") and patch["why"] != entry.why:
                patch["why"] = f"{entry.why}; {patch['why']}"  # сцена названа дважды
            plan.scenes[index[sid]] = SceneEdit.model_validate({**entry.model_dump(), **patch})
    except ValidationError as exc:
        raise ValueError(f"ответ модели не по схеме плана: {_explain(exc)}") from exc
    return plan


def _explain(exc: ValidationError) -> str:
    return "; ".join(".".join(str(x) for x in e["loc"]) + f": {e['msg']}" for e in exc.errors()[:3])


def _retention(ctx: StageContext) -> Retention | None:
    """Карта смен сценариста. Её нет у раскадровок, написанных руками, — стадия
    работает и без неё, модель опирается на один текст."""
    path = ctx.project_dir / SCRIPT_DIR / RETENTION_FILE
    if not path.exists():
        return None
    try:
        return read_model(path, Retention)
    except (ValidationError, ValueError):
        ctx.log.warning("%s не читается — монтажёр работает без карты смен", path.name)
        return None


def _episode_text(ctx: StageContext) -> str:
    sb = ctx.storyboard
    video = ctx.project.video if ctx.project else None
    channel = ctx.project.channel if ctx.project else None
    platforms = ", ".join(channel.platforms) if channel and channel.platforms else "—"
    lines = [
        f"title: {sb.title or '—'}",
        f"language: {sb.language}",
        f"aspect ratio: {sb.aspect_ratio}",
        f"platforms: {platforms}",
        f"target duration: {video.target_duration_sec if video else '—'} s",
    ]
    if sb.brief.strip():
        lines.append(f"brief: {sb.brief.strip()}")
    return "\n".join(lines)


def _available(ctx: StageContext) -> tuple[list[str], list[str], list[Sfx]]:
    """Стили субтитров, треки и звуки, которые модель вправе назвать: по всем
    папкам ресурсов эпизода (проектные + общие), текущие помечены. У трека с
    развитием рядом секунда дропа — без неё правило «start_sec = кульминация −
    дроп» модели не выполнить. Звуки — целиком ресурсами: промпту нужны вид и
    длина (пролёт ставят ДО склейки на его длину), а при совпадении id
    побеждает проектный, как везде (`load_resource` идёт по папкам по порядку)."""
    styles: dict[str, None] = {}
    tracks: dict[str, None] = {}
    sfx_ids: dict[str, None] = {}
    for base in ctx.resource_dirs:
        listed = list_resources(base)
        styles.update(dict.fromkeys(listed.get("caption_styles", [])))
        tracks.update(dict.fromkeys(listed.get("music", [])))
        sfx_ids.update(dict.fromkeys(listed.get("sfx", [])))

    def mark(ids: dict[str, None], current: str | None) -> list[str]:
        return [f"{rid} (current)" if rid == current else rid for rid in ids]

    def with_drop(entry: str) -> str:
        rid = entry.split(" ", 1)[0]
        track = load_resource("music", rid, MusicTrack, ctx.resource_dirs)
        return f"{entry} (drop at {track.drop_sec:g}s)" if track.drop_sec else entry

    refs = ctx.storyboard.resources
    music_on = ctx.project.video.music if ctx.project else False
    track_list = [with_drop(t) for t in mark(tracks, refs.music if music_on else None)]
    if not music_on:
        track_list.append("(music is off in this project)")
    sounds = [load_resource("sfx", rid, Sfx, ctx.resource_dirs) for rid in sfx_ids]
    return mark(styles, refs.caption_style), track_list, sounds


@dataclass
class LlmOutcome:
    plan: EditPlan  # как вернула модель, до починок линта
    lint: LintResult
    calls: int
    seconds: float
    library: StageBrief  # что из библиотеки приёмов легло в промпт


class EditStage(Stage):
    name = "edit"
    needs = ("llm",)  # claude_cli работает без ключа
    # Одна точка, и это граница, а не недоделка: у модели есть поля, которыми
    # выражаются слои звука (`music_gain_db`, `music`), и замер, по которому их
    # ставить (карта смен, речь и тишина по сценам). `ending_audio` не отдана:
    # поле `fade_out_sec` есть, но чем кончается кусок трека, модель не слышит —
    # подключится, когда стадия измерит последнюю секунду трека под ролик.
    # `cut_rhythm` — отрезки `segments` модели не отданы (SCENE_FIELDS).
    decisions = ("audio_layers",)

    def run(self, ctx: StageContext) -> StageResult:
        scenes = ctx.storyboard.ordered_scenes()
        missing = [
            s.id for s in scenes if not (s.assets.video and ctx.abs(s.assets.video).exists())
        ]
        if missing:
            return StageResult(
                status="failed", message=f"монтаж идёт после видео: нет клипов {missing}"
            )

        ctx.stage_dir(EDIT_DIR)
        with ctx.step("замер материала", f"клипов {len(scenes)}"):
            sources = describe_sources(ctx, scenes)
            write_sources(ctx, sources)

        plan = load_plan(ctx.project_dir)
        outcome: LlmOutcome | None = None
        # замок — `pinned`, а не авторство. `author == "manual"` оставлен запасным
        # ходом для планов, написанных до появления замка: они правились руками
        # с расчётом, что стадия их не тронет, и молча переписать их нельзя
        if plan is not None and (plan.pinned or plan.author == "manual"):
            note = "план закреплён" if plan.pinned else "план от manual"
            result = run_lint(ctx, plan)
        elif plan is not None and _fresh(ctx, plan):
            note = f"план от llm ({plan.llm}), актуален"
            result = run_lint(ctx, plan)
        else:
            try:
                outcome = self._plan_by_llm(ctx, sources)
            except (ProviderError, ValidationError, ValueError, TypeError) as exc:
                return StageResult(status="failed", message=f"монтажёр: {exc}")
            plan, result = outcome.plan, outcome.lint
            note = (
                f"план от llm ({plan.llm}): вызовов модели {outcome.calls}, {outcome.seconds:.0f} с"
            )

        rel = ctx.rel(plan_path(ctx.project_dir))
        if result.blocking:
            reasons = "; ".join(f.message for f in result.blocking)
            if outcome is not None:
                # как вернула модель, без починок: человек должен видеть, что именно
                # не прошло, а следующий прогон — упереться в то же, не звать модель
                save_plan(ctx.project_dir, plan)
            return StageResult(
                status="failed",
                message=(
                    f"план монтажа не прошёл проверку ({note}): {reasons} — поправь {rel} "
                    "руками (author: manual) или запусти edit с --force"
                ),
                changed=outcome is not None,
            )

        for fix in result.fixes:
            ctx.log.info("линт починил: %s", fix)
        changed = False
        if outcome is not None or result.fixes:
            if outcome is None:
                result.plan.generated_at = plan.generated_at
            save_plan(ctx.project_dir, result.plan)
            changed = True

        fancy = sum(1 for e in result.plan.scenes if e.transition_in.type != "cut")
        accents = sum(1 for e in result.plan.scenes if e.emphasis)
        steps = sum(1 for e in result.plan.scenes if e.music_gain_db is not None)
        steps += len(result.plan.music.cues) if result.plan.music else 0
        # таймлайн называется всегда, и нулями тоже: план с эффектами и план
        # без них в логе иначе не отличить, а модель теперь пишет и то и другое
        outro = result.plan.outro
        tail = f"замедленный хвост {outro.take_sec:g}s" if outro else "хвоста нет"
        parts = [
            f"{rel}: {note}",
            f"сцен {len(scenes)}, переходов не-cut {fancy}, акцентов {accents}, "
            f"ступеней музыки {steps}, эффектов {len(result.plan.effects)}, "
            f"звуков {len(result.plan.sfx)}, {tail}",
        ]
        if outcome is not None:
            parts.append(outcome.library.describe())
        if result.fixes:
            parts.append(f"починено: {len(result.fixes)}")
        if result.warnings:
            parts.append("предупреждения: " + "; ".join(f.message for f in result.warnings))
        return StageResult(status="done", message=" · ".join(parts), changed=changed)

    @staticmethod
    def _plan_by_llm(ctx: StageContext, sources: Sources) -> LlmOutcome:
        """Модель → отклонения → полный план → линт; блок → один повтор с причинами."""
        llm = ctx.providers.llm.name
        styles, tracks, sounds = _available(ctx)
        library = for_stage(
            EditStage.decisions,
            conditions(ctx.storyboard, ctx.project, ctx.manifest.workflow),
        )
        common: dict[str, Any] = {
            "sources": sources,
            "retention": _retention(ctx),
            "rules": ctx.rules,
            "blueprint": ctx.blueprint,
            "library": library.text,
            "episode": _episode_text(ctx),
            "styles": styles,
            "tracks": tracks,
            "sfx": sounds,
        }
        rejected: list[str] | None = None
        seconds = 0.0
        plan: EditPlan | None = None
        result: LintResult | None = None
        for attempt in range(1, LLM_ATTEMPTS + 1):
            ctx.log.info(
                "план монтажа: вызов модели %d/%d, сцен %d",
                attempt,
                LLM_ATTEMPTS,
                len(sources.clips),
            )
            started = time.monotonic()
            with ctx.step("вызов модели", f"{llm}, попытка {attempt}"):
                raw = ask_json(ctx, p.plan_prompt(**common, rejected=rejected), system=p.SYSTEM)
            seconds += time.monotonic() - started
            try:
                plan = merge_deviations(default_plan(ctx.storyboard), raw, llm=llm)
            except ValueError as exc:
                if attempt == LLM_ATTEMPTS:
                    raise
                rejected = [str(exc)]
                ctx.log.warning("план монтажа: %s — повтор", exc)
                continue
            result = run_lint(ctx, plan)
            if not result.blocking:
                return LlmOutcome(plan, result, attempt, seconds, library)
            rejected = [f.message for f in result.blocking]
            ctx.log.warning("план монтажа: линт заблокировал — %s", "; ".join(rejected))
        assert plan is not None and result is not None
        return LlmOutcome(plan, result, LLM_ATTEMPTS, seconds, library)


def _fresh(ctx: StageContext, plan: EditPlan) -> bool:
    """План модели актуален: новее своих входов и сделан текущей моделью.
    Ручная правка без смены author тоже проходит — она новее и побеждает."""
    if ctx.options.force or plan.author != "llm" or plan.llm != ctx.providers.llm.name:
        return False
    stamp = plan_path(ctx.project_dir).stat().st_mtime
    inputs = [sources_path(ctx.project_dir), ctx.project_dir / SCRIPT_DIR / RETENTION_FILE]
    return not any(path.exists() and path.stat().st_mtime > stamp for path in inputs)
