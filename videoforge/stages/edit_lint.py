"""Проверка плана монтажа до рендера — арифметика и квоты, а не мнение.

Тот же порядок, что у проверки раскадровки на однообразие (`variation.py`):
очевидные технические ошибки чинятся сами (`fixes`: отрицательный in, out за
концом клипа, crossfade длиннее хвоста тишины, лишние акценты), содержательные
ошибки — `block` (неизвестная сцена или источник, перегруз переходами), сомнения —
`warn` для человека на воротах. Заведомо неправильный план в рендер не идёт.

Квоты существуют потому, что модель, которой дали переходы, поставит их
везде: 90% hard cut и 10% осмысленных переходов лучше, чем эффект на каждой
склейке. Считать это должен python — перечитывать свой ответ модель умеет плохо.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from videoforge.core.editplan import EditPlan, SceneEdit, Segment
from videoforge.core.motion import MotionProfile
from videoforge.core.pauses import scene_lead_in
from videoforge.core.storyboard import Scene, Storyboard

Severity = Literal["block", "warn"]

EMPHASIS_MAX = 2  # акцентов на сцену
TRANSITION_SHARE_WARN = 0.2  # доля сцен с не-cut переходом
TRANSITION_SHARE_BLOCK = 0.4
SAME_TRANSITION_RUN_WARN = 3  # один и тот же переход столько раз подряд
MIN_SCENES_FOR_QUOTAS = 4  # меньше — квоты не о чем
CROSSFADE_MAX_SEC = 0.5
DIP_MAX_SEC = 1.5
TRANSITION_DEFAULT_SEC = 0.3
FADE_MAX_SEC = 3.0
MUSIC_GAIN_RANGE_DB = (-30.0, 0.0)  # относительно голоса: громче голоса музыке нельзя
# подложка относительно голоса: громче −12 она уже читается как музыка; тише
# −32 её не видит детектор тишины (порог −40 dBFS по пику), то есть слоя как бы
# нет. Замер 14.09.2026 на синтетическом голосе с паузами (tests/test_ambient_bed):
# при −28 пик подложки в паузе −36.9 dBTP и тишины 0%, при −34 доля тишины
# снова 45% — та же, что без подложки
BED_GAIN_RANGE_DB = (-32.0, -12.0)
MUSIC_STEPS_WARN = 6
SEGMENT_SLACK_SEC = 0.05  # допуск на округление кадров
# Эффекты — та же история, что и переходы: модель, которой дали вспышку,
# поставит её везде. Порог считается на минуту хронометража, потому что эффект
# живёт на таймлайне, а не в сцене, и от числа сцен не зависит
EFFECTS_PER_MIN_WARN = 12.0
EFFECTS_PER_MIN_BLOCK = 25.0
SFX_PER_MIN_WARN = 10.0
SFX_PER_MIN_BLOCK = 20.0
# Пол квоты: столько эффектов и звуков ролику можно всегда, независимо от
# длины. Без него восьмисекундный клип не имеет права ни на что: четыре удара
# по пикам движения дают 30 в минуту и попадают под блок, хотя это ровно то,
# ради чего клип снят.
EFFECTS_FLOOR = 4
SFX_FLOOR = 6
EFFECT_MAX_SEC = 6.0  # дольше — это уже не эффект, а цветокоррекция всего ролика
SFX_GAIN_RANGE_DB = (-30.0, 0.0)
OUTRO_SPEED_RANGE = (0.25, 1.0)
OUTRO_TAKE_RANGE_SEC = (0.5, 15.0)


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    scene_id: str | None = None


class LintResult(BaseModel):
    plan: EditPlan  # с автопочинками
    findings: list[Finding]
    fixes: list[str]

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]


def _norm(word: str) -> str:
    return re.sub(r"[^\wё-]+", "", word.lower())


def _narration_words(scene: Scene) -> set[str]:
    return {_norm(w) for w in scene.narration.split() if _norm(w)}


def silent_tail(scene: Scene) -> float:
    """Тишина в конце коробки сцены — после её озвучки. Заход
    (`core/pauses.scene_lead_in`) стоит в начале коробки, и хвостом он не
    является: crossfade съедает конец уходящей сцены, а не её тишину до слова."""
    speech = scene.measured_sec.get("audio", 0.0)
    return max(0.0, scene.duration_sec - scene_lead_in(scene) - speech)


def resolve_segments(
    edit: SceneEdit,
    scene: Scene,
    duration_of: Callable[[str], float | None],
) -> tuple[list[Segment], list[str], list[str]]:
    """Отрезки сцены с раскрытыми умолчаниями и починенными границами.

    Возвращает (отрезки, ошибки, починки). out=None раскрывается до конца
    источника, но не дальше остатка коробки; сумма длиннее коробки режется с
    конца; короче — остаётся (кадр замрёт, и это посчитает сборка)."""
    errors: list[str] = []
    fixes: list[str] = []
    segments = edit.segments or [Segment(source=scene.assets.video)]
    box = scene.duration_sec
    filled = 0.0
    resolved: list[Segment] = []
    for i, seg in enumerate(segments, start=1):
        source = seg.source or scene.assets.video
        if not source:
            errors.append(f"сцена {scene.id}: у отрезка {i} нет источника, а клипа у сцены нет")
            continue
        length = duration_of(source)
        if length is None:
            errors.append(f"сцена {scene.id}: источник отрезка {i} не найден: {source}")
            continue
        start = seg.in_sec
        if start < 0:
            fixes.append(f"сцена {scene.id}: отрезок {i} in {start:g} → 0")
            start = 0.0
        remaining = max(0.0, box - filled)
        end = seg.out_sec if seg.out_sec is not None else min(length, start + remaining)
        if end > length + SEGMENT_SLACK_SEC:
            fixes.append(f"сцена {scene.id}: отрезок {i} out {end:g} → конец клипа {length:.2f}")
            end = length
        if end <= start:
            errors.append(f"сцена {scene.id}: отрезок {i} пустой (in {start:g} ≥ out {end:g})")
            continue
        if filled >= box - SEGMENT_SLACK_SEC:
            fixes.append(f"сцена {scene.id}: отрезок {i} за пределами коробки {box:g}s — убран")
            continue
        if end - start > remaining + SEGMENT_SLACK_SEC:
            fixes.append(
                f"сцена {scene.id}: отрезок {i} длиннее остатка коробки — "
                f"обрезан до {remaining:.2f}s"
            )
            end = start + remaining
        resolved.append(Segment(source=source, in_sec=round(start, 3), out_sec=round(end, 3)))
        filled += end - start
    return resolved, errors, fixes


def _check_motion_peak(
    edit: SceneEdit,
    scene: Scene,
    motion_of: Callable[[str], MotionProfile | None] | None,
    warn: Callable[[str, str, str | None], None],
) -> None:
    """Отрезки сцены не должны выбрасывать пик движения её клипа.

    Клип i2v начинается с одобренного кадра, и голова — не мусор; но если план
    всё же сдвинул окно, пусть хотя бы не режет то единственное, ради чего клип
    генерировали. Пик за пределами коробки недостижим любым окном с нуля — про
    него не предупреждаем: это не вина плана."""
    if motion_of is None or not scene.assets.video:
        return
    profile = motion_of(scene.assets.video)
    peak = profile.peak_window() if profile else None
    if peak is None or peak[0] >= scene.duration_sec:
        return
    a, b = peak
    covered = any(
        s.source == scene.assets.video and s.in_sec < b and (s.out_sec or 0.0) > a
        for s in edit.segments
    )
    if not covered:
        warn(
            "motion_peak_cut",
            f"сцена {scene.id}: отрезки не включают пик движения {a:g}–{b:g} с клипа",
            scene.id,
        )


def _lint_timeline(
    plan: EditPlan,
    total: float,
    *,
    sfx_exists: Callable[[str], bool],
    block: Callable[[str, str], None],
    warn: Callable[[str, str], None],
    fixes: list[str],
) -> None:
    """Эффекты, звуки и замедленный хвост — то, что живёт на таймлайне ролика,
    а не в сцене. Проверяется против измеренной длины эпизода: эффект за концом
    ролика молча не сработает, и человек будет искать его глазами.

    Квоты здесь по той же причине, что и у переходов: вспышка на каждом слове —
    это не монтаж, а припадок. Считает python, потому что перечитывать свой
    ответ модель умеет плохо."""
    minutes = max(total, 1.0) / 60
    kept_effects = []
    for e in plan.effects:
        if e.at_sec < 0 or (total and e.at_sec > total):
            fixes.append(f"эффект {e.kind} на {e.at_sec:g}s вне ролика ({total:.1f}s) — убран")
            continue
        if e.duration_sec > EFFECT_MAX_SEC:
            fixes.append(f"эффект {e.kind} {e.duration_sec:g}s → {EFFECT_MAX_SEC}s")
            e.duration_sec = EFFECT_MAX_SEC
        if e.duration_sec <= 0:
            fixes.append(f"эффект {e.kind} без длительности → 0.4s")
            e.duration_sec = 0.4
        if not 0 <= e.strength <= 2:
            fixed = min(2.0, max(0.0, e.strength))
            fixes.append(f"эффект {e.kind}: сила {e.strength:g} → {fixed:g}")
            e.strength = fixed
        kept_effects.append(e)
    plan.effects = kept_effects

    kept_sfx = []
    for cue in plan.sfx:
        if not sfx_exists(cue.id):
            block("unknown_sfx", f"звук '{cue.id}' не найден в resources/sfx/")
            continue
        if cue.at_sec < 0 or (total and cue.at_sec > total):
            fixes.append(f"звук {cue.id} на {cue.at_sec:g}s вне ролика ({total:.1f}s) — убран")
            continue
        if cue.gain_db is not None:
            lo, hi = SFX_GAIN_RANGE_DB
            if not lo <= cue.gain_db <= hi:
                fixed = min(hi, max(lo, cue.gain_db))
                fixes.append(f"звук {cue.id}: gain_db {cue.gain_db:g} → {fixed:g}")
                cue.gain_db = fixed
        kept_sfx.append(cue)
    plan.sfx = kept_sfx

    for name, items, warn_rate, block_rate, floor in (
        ("эффектов", plan.effects, EFFECTS_PER_MIN_WARN, EFFECTS_PER_MIN_BLOCK, EFFECTS_FLOOR),
        ("звуков", plan.sfx, SFX_PER_MIN_WARN, SFX_PER_MIN_BLOCK, SFX_FLOOR),
    ):
        rate = len(items) / minutes
        # Ставка «в минуту» на коротком ролике врёт: пять ударов за восемь
        # секунд драки — это жанр, а не припадок, но арифметика выдаёт 37 в
        # минуту и блокирует план (замерено на tests/t02_cat_realtime).
        # Поэтому у квоты есть пол: столько-то событий ролику можно всегда,
        # какой бы короткий он ни был, и только сверх этого считается ставка.
        if len(items) <= floor:
            continue
        if rate > block_rate:
            block(
                "too_many_effects",
                f"{name} {len(items)} на {total:.0f}s ({rate:.0f} в минуту) — это не монтаж, "
                f"а припадок; предел {block_rate:.0f} в минуту (или {floor} на ролик)",
            )
        elif rate > warn_rate:
            warn("many_effects", f"{name} {len(items)} на {total:.0f}s ({rate:.0f} в минуту)")

    if plan.music and total and plan.music.start_sec >= total:
        fixes.append(
            f"music.start_sec {plan.music.start_sec:g} → 0 (трек начинался бы после конца "
            f"ролика {total:.1f}s — его бы просто не было слышно)"
        )
        plan.music.start_sec = 0.0
    if plan.music and plan.music.cues:
        lo, hi = MUSIC_GAIN_RANGE_DB
        for step in plan.music.cues:
            if not lo <= step.gain_db <= hi:
                fixed = min(hi, max(lo, step.gain_db))
                fixes.append(f"ступень музыки на {step.at_sec:g}s: {step.gain_db:g} → {fixed:g}")
                step.gain_db = fixed
        plan.music.cues = [c for c in plan.music.cues if not total or c.at_sec <= total]

    if plan.outro is not None:
        lo, hi = OUTRO_SPEED_RANGE
        if not lo <= plan.outro.speed <= hi:
            fixed = min(hi, max(lo, plan.outro.speed))
            fixes.append(f"outro.speed {plan.outro.speed:g} → {fixed:g}")
            plan.outro.speed = fixed
        lo, hi = OUTRO_TAKE_RANGE_SEC
        ceiling = min(hi, total / 2) if total else hi
        if plan.outro.take_sec > ceiling:
            fixes.append(
                f"outro.take_sec {plan.outro.take_sec:g} → {ceiling:.1f} "
                f"(замедлять больше половины ролика — это уже не хвост)"
            )
            plan.outro.take_sec = round(ceiling, 2)
        if plan.outro.take_sec < lo:
            fixes.append(f"outro.take_sec {plan.outro.take_sec:g} → {lo:g}")
            plan.outro.take_sec = lo

    # Порядок по времени — для глаза и ровного diff: модель отвечает в порядке
    # мысли, а человек читает план по секундам. Рендеру порядок безразличен
    # (`between(it, …)` у эффектов, amix у звуков, ступени сборка сортирует
    # сама), поэтому это не починка и в `fixes` не попадает
    plan.effects.sort(key=lambda e: e.at_sec)
    plan.sfx.sort(key=lambda c: c.at_sec)
    if plan.music:
        plan.music.cues.sort(key=lambda c: c.at_sec)


def lint_plan(
    plan: EditPlan,
    storyboard: Storyboard,
    *,
    duration_of: Callable[[str], float | None],
    style_exists: Callable[[str], bool],
    track_exists: Callable[[str], bool],
    sfx_exists: Callable[[str], bool] | None = None,
    motion_of: Callable[[str], MotionProfile | None] | None = None,
) -> LintResult:
    """Проверить план против раскадровки и материала. Возвращает починенную копию
    плана, находки (block/warn) и список починок. `motion_of` — профиль движения
    источника: без него проверка пика пропускается."""
    plan = plan.model_copy(deep=True)
    findings: list[Finding] = []
    fixes: list[str] = []
    scenes = storyboard.ordered_scenes()
    by_id = {s.id: s for s in scenes}
    order = {s.id: i for i, s in enumerate(scenes)}

    def block(code: str, message: str, scene_id: str | None = None) -> None:
        findings.append(Finding(code=code, severity="block", message=message, scene_id=scene_id))

    def warn(code: str, message: str, scene_id: str | None = None) -> None:
        findings.append(Finding(code=code, severity="warn", message=message, scene_id=scene_id))

    # --- глобальное
    if plan.caption_style and not style_exists(plan.caption_style):
        block("unknown_style", f"стиль субтитров '{plan.caption_style}' не найден")
    if plan.music and plan.music.track and not track_exists(plan.music.track):
        block("unknown_track", f"трек '{plan.music.track}' не найден в resources/music/")
    # подложка — тот же вид ресурса, что музыка: выдуманный id блокируется так же
    if plan.bed and plan.bed.track and not track_exists(plan.bed.track):
        block("unknown_bed", f"подложка '{plan.bed.track}' не найдена в resources/music/")
    if plan.bed and plan.bed.gain_db is not None:
        lo, hi = BED_GAIN_RANGE_DB
        if not lo <= plan.bed.gain_db <= hi:
            fixed = min(hi, max(lo, plan.bed.gain_db))
            fixes.append(
                f"bed.gain_db {plan.bed.gain_db:g} → {fixed:g} (подложка слышна, но не музыка)"
            )
            plan.bed.gain_db = fixed
    if plan.music and plan.music.gain_db is not None:
        lo, hi = MUSIC_GAIN_RANGE_DB
        if not lo <= plan.music.gain_db <= hi:
            fixed = min(hi, max(lo, plan.music.gain_db))
            fixes.append(
                f"music.gain_db {plan.music.gain_db:g} → {fixed:g} (музыка не громче голоса)"
            )
            plan.music.gain_db = fixed
    for field in ("fade_in_sec", "fade_out_sec"):
        value = getattr(plan, field)
        if value is not None and (value < 0 or value > FADE_MAX_SEC):
            fixed = min(FADE_MAX_SEC, max(0.0, value))
            fixes.append(f"{field} {value:g} → {fixed:g}")
            setattr(plan, field, fixed)

    # --- сцены
    seen: Counter[str] = Counter(e.scene_id for e in plan.scenes)
    for sid, n in seen.items():
        if n > 1:
            block("duplicate_scene", f"сцена {sid} встречается в плане {n} раз", sid)
    kept: list[SceneEdit] = []
    for edit in plan.scenes:
        scene = by_id.get(edit.scene_id)
        if scene is None:
            block("unknown_scene", f"в плане сцена '{edit.scene_id}', которой нет в раскадровке")
            continue
        segments, errors, seg_fixes = resolve_segments(edit, scene, duration_of)
        fixes.extend(seg_fixes)
        for err in errors:
            block("bad_segment", err, scene.id)
        if not errors:
            filled = sum((s.out_sec or 0) - s.in_sec for s in segments)
            if filled < scene.duration_sec - SEGMENT_SLACK_SEC:
                warn(
                    "underfilled",
                    f"сцена {scene.id}: отрезки дают {filled:.2f}s из {scene.duration_sec:g}s — "
                    f"кадр замрёт на {scene.duration_sec - filled:.2f}s",
                    scene.id,
                )
            edit.segments = segments
            _check_motion_peak(edit, scene, motion_of, warn)

        # переходы: первая сцена — только cut (вход эпизода делает fade_in_sec)
        tr = edit.transition_in
        idx = order[scene.id]
        if tr.type != "cut":
            if idx == 0:
                fixes.append(f"сцена {scene.id}: переход на первой сцене → cut")
                tr.type, tr.duration_sec = "cut", 0.0
            elif tr.duration_sec <= 0:
                default = TRANSITION_DEFAULT_SEC
                fixes.append(f"сцена {scene.id}: переход {tr.type} без длительности → {default}s")
                tr.duration_sec = default
        if tr.type == "crossfade":
            prev = scenes[idx - 1]
            tail = silent_tail(prev)
            ceiling = min(CROSSFADE_MAX_SEC, tail)
            if ceiling < 0.1:
                fixes.append(
                    f"сцена {scene.id}: crossfade невозможен — у {prev.id} нет хвоста тишины "
                    f"({tail:.2f}s) → dip_black"
                )
                tr.type, tr.duration_sec = "dip_black", TRANSITION_DEFAULT_SEC
            elif tr.duration_sec > ceiling:
                fixes.append(
                    f"сцена {scene.id}: crossfade {tr.duration_sec:g}s → {ceiling:.2f}s "
                    f"(только хвост тишины {prev.id})"
                )
                tr.duration_sec = round(ceiling, 3)
        elif tr.type in ("dip_black", "fade_in") and tr.duration_sec > DIP_MAX_SEC:
            fixes.append(f"сцена {scene.id}: {tr.type} {tr.duration_sec:g}s → {DIP_MAX_SEC}s")
            tr.duration_sec = DIP_MAX_SEC

        # акценты — только слова из narration, не больше двух
        if edit.emphasis:
            words = _narration_words(scene)
            good = [w for w in edit.emphasis if _norm(w) in words]
            dropped = [w for w in edit.emphasis if _norm(w) not in words]
            if dropped:
                fixes.append(f"сцена {scene.id}: акценты не из narration убраны: {dropped}")
            if len(good) > EMPHASIS_MAX:
                fixes.append(f"сцена {scene.id}: акцентов {len(good)} → {EMPHASIS_MAX}")
                good = good[:EMPHASIS_MAX]
            edit.emphasis = good

        if edit.music_gain_db is not None:
            lo, hi = MUSIC_GAIN_RANGE_DB
            if not lo <= edit.music_gain_db <= hi:
                fixed = min(hi, max(lo, edit.music_gain_db))
                fixes.append(f"сцена {scene.id}: music_gain_db {edit.music_gain_db:g} → {fixed:g}")
                edit.music_gain_db = fixed
        kept.append(edit)
    plan.scenes = kept

    # --- квоты по всему эпизоду
    if len(scenes) >= MIN_SCENES_FOR_QUOTAS:
        kinds: list[str] = []
        for s in scenes:
            entry = plan.scene(s.id)
            kinds.append(entry.transition_in.type if entry is not None else "cut")
        fancy = sum(1 for k in kinds if k != "cut")
        share = fancy / len(scenes)
        if share > TRANSITION_SHARE_BLOCK:
            block(
                "too_many_transitions",
                f"переходов не-cut {fancy} из {len(scenes)} ({share:.0%}) — это набор эффектов, "
                f"а не монтаж; hard cut должен быть основным",
            )
        elif share > TRANSITION_SHARE_WARN:
            warn("many_transitions", f"переходов не-cut {fancy} из {len(scenes)} ({share:.0%})")
        run, longest, prev_kind = 0, 0, ""
        for k in kinds:
            run = run + 1 if (k != "cut" and k == prev_kind) else (1 if k != "cut" else 0)
            longest = max(longest, run)
            prev_kind = k
        if longest >= SAME_TRANSITION_RUN_WARN:
            warn("same_transition_run", f"один и тот же переход {longest} раз подряд")
    steps = sum(1 for e in plan.scenes if e.music_gain_db is not None)
    steps += len(plan.music.cues) if plan.music else 0
    if steps > MUSIC_STEPS_WARN:
        warn("many_music_steps", f"ступеней громкости музыки {steps} — больше {MUSIC_STEPS_WARN}")

    # --- таймлайн: эффекты, звуки, замедленный хвост
    _lint_timeline(
        plan,
        sum(s.duration_sec for s in scenes),
        sfx_exists=sfx_exists or (lambda _: True),
        block=lambda code, message: block(code, message),
        warn=lambda code, message: warn(code, message),
        fixes=fixes,
    )

    order_sev = {"block": 0, "warn": 1}
    findings.sort(key=lambda f: order_sev[f.severity])
    return LintResult(plan=plan, findings=findings, fixes=fixes)
