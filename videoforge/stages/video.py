"""Видео: картинка + motion_prompt → 03_video/<scene>_vNN.mp4.

Самая дорогая стадия. Проверка — по 6 кадрам, вытащенным ffmpeg: та же
vision-LLM, что и для картинок, плюс консистентность между кадрами.

Не каждая сцена обязана быть нейроклипом (16.09.2026,
`features/mixed_still_and_clip_scenes`): при `video.clip_share` < 1 проекта
часть сцен идёт картинкой — готовым кадром с программным наездом или
панорамой (`ProviderSet.still`, remotion, $0). Какие — решает python один раз
(`storyboard.plan_render`) и пишет в `Scene.render`; клипы по кадрам у таких
сцен не проверяются — кадр уже проверен стадией images, а ход камеры
детерминирован.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from videoforge.config import vision_check_enabled
from videoforge.core.ffmpeg import FfmpegError, extract_frames, probe_duration
from videoforge.core.motion import DEAD_ENERGY, load_or_measure
from videoforge.core.stage import (
    Stage,
    StageContext,
    StageResult,
    adopt_old_assets,
    check_or_refusal,
    keep_best_attempt,
    record_check,
    refused_note,
)
from videoforge.core.storyboard import (
    CLIP_OPENING_SHARE,
    Attempt,
    Scene,
    SceneRender,
    chain_links,
    clip_share_of,
    fingerprint,
    plan_render,
)
from videoforge.providers.base import MotionGuide, VideoProvider
from videoforge.stages.images import ImagesStage
from videoforge.stages.prompts import cast_checklist, cast_references, motion_prompt

OUT_DIR = "03_video"
CHECK_FRAMES = 6
DURATION_TOLERANCE_SEC = 1.0  # провайдер отдаёт «6 секунд» с точностью до кадров

# Проверка целостности клипа: чеклисты каста и стиля ловят «кто в кадре» и
# «как нарисовано», но не «что модель дорисовала». Замерено 31.08.2026: i2v
# способен вырастить в кадре объект, которого нет на исходной картинке, — и
# такой клип проходил все прежние пункты.
CLIP_INTEGRITY_CHECKS = [
    "Между кадрами не появляется объектов, которых нет на первом кадре; "
    "формы не деформируются и не превращаются в другие",
]


class VideoStage(Stage):
    name = "video"
    # check — контролёр: проверка клипа по кадрам (тот же, что у картинок);
    # still — картинки-сцены (`Scene.render: still`): кадр с программным наездом
    needs = ("video", "still", "check")

    @staticmethod
    def _inputs(ctx: StageContext, scene: Scene, mode: SceneRender | None = None) -> str:
        """Из чего сделан клип: движение, темп, стиль камеры и сам кадр.

        Отпечаток кадра входит сюда целиком, поэтому цепочка держится сама:
        правка `visual_prompt` делает устаревшим кадр, а устаревший кадр —
        клип, который из него анимировали.

        Темп — только явное поле `tempo`, и только когда оно задано. Сам текст
        промпта (блок TEMPO_ACTION, вывод эвристики `scene_tempo`) в отпечаток
        не входит: иначе первое же обновление с правилом темпа сделало бы
        устаревшими все готовые клипы во всех проектах — переплата за то, что
        сделано честно (см. `asset_ready`). Пустое поле отпечаток не меняет
        (`fingerprint` хеширует части через разделитель, лишняя пустая часть —
        другой хеш), поэтому у старых раскадровок он остаётся прежним; правило
        темпа получают новые клипы, а старый переделывается, когда человек или
        планнер поставил сцене `tempo` — это правка входа, как правка
        `motion_prompt`.

        Место в цепочке продолжений (`Scene.continues`) — вход только у
        провайдера с программной камерой (`chains_camera`): клип нарисован с
        этого места хода, и правка длины соседнего звена его сдвигает. У
        генеративного клипа цепочка входом не является — стык сшивает сборка
        кропом, — и часть не добавляется, иначе первое же обновление
        перегенерировало бы все продолжения во всех проектах за деньги.

        Референс движения (`Scene.motion_reference`) — вход, только когда
        задан: сигнал, сила и САМ ФАЙЛ по содержимому (`_reference_stamp`).
        Без поля часть не добавляется по той же причине, что темп: иначе все
        готовые клипы устарели бы разом.

        Картинка-сцена (`mode` still — назначение стадии или `Scene.render`)
        — своя часть: клип и наезд по одному кадру — разные активы, даже если
        провайдер у них один (заглушки, VF_VIDEO_PROVIDER=remotion). Режим
        `clip` части не добавляет: он и есть умолчание, и запись решения
        «клип» в раскадровку не должна делать готовые клипы устаревшими."""
        mode = mode or scene.render or "clip"
        style = ctx.resources.style
        video = ctx.providers.still if mode == "still" else ctx.providers.video
        link = (
            chain_links(ctx.storyboard.ordered_scenes()).get(scene.id)
            if video is not None and video.chains_camera
            else None
        )
        return fingerprint(
            scene.motion_prompt,
            scene.visual_prompt,
            getattr(style, "motion_hint", ""),
            ctx.storyboard.aspect_ratio,
            scene.assets.image or "",
            scene.inputs_fingerprint.get("image", ""),
            *([scene.tempo] if scene.tempo else []),
            *([f"chain:{link.before_sec:g}/{link.total_sec:g}/{link.head_sec:g}"] if link else []),
            *([_reference_stamp(ctx, scene)] if scene.motion_reference is not None else []),
            *(["render:still"] if mode == "still" else []),
        )

    @classmethod
    def _ready(cls, ctx: StageContext, scene: Scene, mode: SceneRender = "clip") -> bool:
        video = provider_for(ctx, mode)
        if video is None:
            return False
        if not (
            scene.asset_ready("video", video.name, cls._inputs(ctx, scene, mode))
            and ctx.abs(scene.assets.video or "").exists()
        ):
            return False
        # длительность сцены могла поменяться после перегенерации озвучки:
        # клип на 6 с под 9-секундную речь устарел так же, как при смене провайдера
        made = scene.measured_sec.get("video")
        wanted = video.pick_duration(scene.duration_sec)
        if made is None or abs(made - wanted) <= DURATION_TOLERANCE_SEC:
            return True
        # клип ДЛИННЕЕ нужного — не брак: лишнее сборка срежет. Платный клип
        # из-за этого не переделываем (19.09.2026, ep003: голос сменили на более
        # быстрый, сцены стали короче на полсекунды, и десять готовых клипов LTX
        # ушли бы на перегенерацию с картой); бесплатный наезд перерисуем — у
        # него ход камеры рассчитан на длину
        return made > wanted and video.price_for(wanted) > 0

    def _todo(self, ctx: StageContext, modes: dict[str, SceneRender]) -> list[Scene]:
        return [
            s
            for s in ctx.storyboard.ordered_scenes()
            if ctx.options.force or not self._ready(ctx, s, modes[s.id])
        ]

    def estimate_cost(self, ctx: StageContext) -> float:
        modes = render_modes(ctx)
        total = 0.0
        for s in self._todo(ctx, modes):
            video = provider_for(ctx, modes[s.id])
            if video is None:
                continue  # картинка-сцена без провайдера: стадия откажет до денег
            total += video.price_for(
                video.pick_duration(s.duration_sec), guided=s.motion_reference is not None
            )
        return total

    @staticmethod
    def _assign(ctx: StageContext, modes: dict[str, SceneRender]) -> int:
        """Записать назначение в раскадровку — один раз, только пустые поля.

        Пишется, только когда проект смешивает (`clip_share` < 1): иначе все
        сцены клипами и решать нечего, а поле на каждой сцене — шум в файле.
        Записанное дальше не пересчитывается (`plan_render` уважает поле):
        озвучка меняет длительности, и новая раскладка по времени сменила бы
        отпечатки — клипы перегенерировались бы за деньги."""
        share = ctx.project.video.clip_share if ctx.project else 1.0
        if share >= 1:
            return 0
        fresh = [s for s in ctx.storyboard.ordered_scenes() if s.render is None]
        for scene in fresh:
            scene.render = modes[scene.id]
        if fresh:
            stills = sum(1 for s in fresh if s.render == "still")
            ctx.log.info(
                "назначено сцен: %d (клипами %d, картинками %d) при доле клипов %.0f%%",
                len(fresh),
                len(fresh) - stills,
                stills,
                share * 100,
            )
            ctx.save()
        return len(fresh)

    def run(self, ctx: StageContext) -> StageResult:
        modes = render_modes(ctx)
        assigned = self._assign(ctx, modes)
        todo = self._todo(ctx, modes)
        # клипы, которые оставляем, помечаются принятыми (см. adopt_old_assets):
        # отметку ставит стадия, а не предикат, который читают и estimate с пультом
        keep = [s for s in ctx.storyboard.ordered_scenes() if s not in todo]
        adopt_old_assets(ctx, "video", [(s, self._inputs(ctx, s, modes[s.id])) for s in keep])
        mix = mix_note(ctx, modes)
        if not todo:
            return StageResult(
                status="done", message=f"все клипы уже на месте{mix}", changed=bool(assigned)
            )

        missing = [s.id for s in todo if not (s.assets.image and ctx.abs(s.assets.image).exists())]
        if missing:
            return StageResult(status="failed", message=f"нет картинок для сцен: {missing}")

        casts, errors = ImagesStage._resolve_casts(ctx, todo)
        if errors:
            return StageResult(status="failed", message="каст не разрешён: " + "; ".join(errors))

        # провайдер картинок-сцен проверяется до первого платного вызова: они
        # идут вперемешку с клипами, и непоставленный Node.js на сороковой
        # сцене — это тридцать девять оплаченных клипов до отказа
        still_ids = [s.id for s in todo if modes[s.id] == "still"]
        if still_ids:
            shown = ", ".join(still_ids[:8]) + (
                f" (+{len(still_ids) - 8})" if len(still_ids) > 8 else ""
            )
            if ctx.providers.still is None:
                return StageResult(
                    status="failed",
                    message=f"картинки-сцены {shown} без провайдера — набор собран без still "
                    "(VF_STILL_PROVIDER)",
                )
            if problem := ctx.providers.still.not_ready():
                return StageResult(
                    status="failed",
                    message=f"провайдер картинок-сцен {ctx.providers.still.name} не готов: "
                    f"{problem} — сцены {shown}",
                )

        # референс движения проверяется до первого платного вызова, как каст:
        # опечатка в пути или провайдер без адаптера не должны стоить соседних
        # сцен. Позу стартового кадра python не видит — это предупреждение в
        # сообщении стадии, а не проверка
        lost = [
            f"{s.id} ({s.motion_reference.file})"
            for s in todo
            if s.motion_reference is not None and motion_reference_path(ctx, s) is None
        ]
        if lost:
            return StageResult(
                status="failed",
                message="референса движения нет на диске (путь — от папки эпизода или проекта): "
                + "; ".join(lost),
            )
        guided_ids = [s.id for s in todo if s.motion_reference is not None]
        still_guided = [i for i in guided_ids if modes[i] == "still"]
        if still_guided:
            return StageResult(
                status="failed",
                message=f"референс движения у картинок-сцен {still_guided} (render: still): "
                "программная камера за скелетом не пойдёт — поставь сцене render: clip",
            )
        if guided_ids and not ctx.providers.video.follows_reference:
            return StageResult(
                status="failed",
                message=f"референс движения у сцен {guided_ids}, а провайдер "
                f"{ctx.providers.video.name} его не читает — нужен comfy "
                "(VF_VIDEO_PROVIDER=comfy, на поде iclora_setup.sh)",
            )

        out_dir = ctx.stage_dir(OUT_DIR)
        res = ctx.resources
        # продолжения плана: программная камера получает место в цепочке и
        # начинает ход там, где кончилось предыдущее звено; генеративной модели
        # это сказать нечем — её стык сшивает сборка (`video.zoom_per_sec`)
        links = chain_links(ctx.storyboard.ordered_scenes())

        failed: list[str] = []
        refused = 0  # контролёр отказался смотреть — проверка не состоялась
        changed = bool(assigned)
        for scene in todo:
            assert scene.assets.image
            mode = modes[scene.id]
            video = provider_for(ctx, mode)
            assert video is not None  # картинки-сцены без провайдера отсеяны выше
            cast = casts[scene.id]
            refs = cast_references(cast)
            items = cast_checklist(cast, res)
            image = ctx.abs(scene.assets.image)
            prompt = motion_prompt(scene, res)
            duration = video.pick_duration(scene.duration_sec)
            link = links.get(scene.id) if video.chains_camera else None
            guide = _motion_guide(ctx, scene)
            (out_dir / f"{scene.id}.prompt.txt").write_text(prompt, encoding="utf-8")
            passed = False
            made: list[Attempt] = []  # попытки этого прогона — из них выбирается лучшая
            for _ in range(ctx.options.max_attempts):
                n = scene.attempts.get("video", 0) + 1
                scene.attempts["video"] = n
                path = out_dir / f"{scene.id}_v{n:02d}.mp4"
                what = "картинка с наездом" if mode == "still" else "видео"
                ctx.log.info("сцена %s: %s %ds, попытка %d", scene.id, what, duration, n)
                ctx.afford(
                    video.price_for(duration, guided=guide is not None), f"клип {scene.id} v{n}"
                )
                gen = video.animate(
                    image,
                    prompt,
                    duration_sec=duration,
                    aspect_ratio=ctx.storyboard.aspect_ratio,
                    out_path=path,
                    chain=link,
                    motion_reference=guide,
                )
                ctx.add_cost(gen.cost_usd, video.name, scene.id, f"video v{n}")
                scene.cost_usd["video"] = round(scene.cost_usd.get("video", 0.0) + gen.cost_usd, 6)
                scene.assets.video = ctx.rel(path)
                scene.provenance["video"] = video.name
                scene.chained = link is not None
                scene.inputs_fingerprint["video"] = self._inputs(ctx, scene, mode)
                scene.measured_sec["video"] = round(probe_duration(path), 3)
                _note_motion(ctx, scene, path)
                changed = True

                issues: list[str] = []
                check_cost = 0.0
                if mode == "still":
                    # кадр уже проверен стадией images, а ход программной камеры
                    # детерминирован: дорисовать ей нечего. Проверки не было —
                    # так и пишется; мёртвый ход ловит профиль движения выше
                    scene.checks.video = "skipped"
                    scene.checks.notes.pop("video", None)
                elif items and vision_check_enabled():
                    frames_dir = out_dir / f"{scene.id}_v{n:02d}_frames"
                    frames = extract_frames(path, frames_dir, CHECK_FRAMES)
                    # смотрит контролёр (`providers.check`), как у картинок
                    checker = ctx.providers.check
                    # оценка сверху от провайдера, а не ноль: см. stage.afford
                    ctx.afford(checker.check_price_usd, f"проверка клипа {scene.id}")
                    check = check_or_refusal(
                        checker,
                        frames,
                        references=refs,
                        checklist=items + CLIP_INTEGRITY_CHECKS,
                        context=f"{scene.visual_prompt}\nMotion: {prompt}",
                    )
                    ctx.add_cost(check.cost_usd, checker.name, scene.id, "video check")
                    check_cost = check.cost_usd
                    issues = list(check.issues)
                    if record_check(scene, "video", check):
                        refused += 1
                        ctx.log.warning("сцена %s: %s", scene.id, scene.checks.notes["video"])
                else:
                    scene.checks.video = "skipped"
                    if items:
                        scene.checks.notes["video"] = "проверка отключена (VF_VISION_CHECK=off)"
                # попытка записывается с вердиктом ДО решения «продолжать ли»:
                # последняя не значит лучшая, и после цикла выбор идёт по журналу
                attempt = Attempt(
                    n=n,
                    path=ctx.rel(path),
                    passed=scene.checks.video != "fail",
                    issues=issues,
                    cost_usd=round(gen.cost_usd + check_cost, 6),
                    provider=video.name,
                )
                scene.attempt_log.setdefault("video", []).append(attempt)
                made.append(attempt)
                ctx.save()

                if scene.checks.video != "fail":
                    passed = True
                    break
                ctx.log.warning(
                    "сцена %s: проверка не пройдена: %s",
                    scene.id,
                    scene.checks.notes.get("video", ""),
                )
            if not passed:
                # ни одна не прошла — остаётся не последняя, а с наименьшим
                # числом замечаний (та же ловушка, что у кадров: см. images)
                failed.append(keep_best_attempt(scene, "video", made))
                # длина и профиль движения — у выбранного клипа, а не у последнего
                kept = ctx.abs(scene.assets.video or "")
                scene.measured_sec["video"] = round(probe_duration(kept), 3)
                _note_motion(ctx, scene, kept)
                ctx.save()

        # позу кадра python не сверяет: замер mocap_follow_8s — стартовый кадр
        # «стоит прямо», скелет с первого кадра в стойке, и первые полсекунды
        # модель «встаёт в стойку» призраком. Напоминание — и при провале проверки
        # клипа: там оно нужнее, а пропадало (прогон t07_lora_motion_ref, 16.09.2026)
        pose = (
            f" · по референсу движения: {len(guided_ids)} сцен {guided_ids} — ПРОВЕРЬ: "
            "стартовый кадр стоит в первой позе референса? иначе призрак в первые "
            "полсекунды (mocap_follow_8s)"
            if guided_ids
            else ""
        )
        if failed:
            return StageResult(
                status="failed",
                message="не прошли проверку: " + "; ".join(failed) + refused_note(refused) + pose,
                changed=changed,
            )
        dead = [s.id for s in todo if "motion" in s.checks.notes]
        note = f" · без движения: {dead} (см. checks.notes)" if dead else ""
        note += refused_note(refused)
        if chained := sum(1 for s in todo if s.chained and s.continues):
            note += f" · продолжений плана начато с места хода: {chained}"
        note += pose
        return StageResult(
            status="done", message=f"готово сцен: {len(todo)}{mix}{note}", changed=changed
        )


def render_modes(ctx: StageContext) -> dict[str, SceneRender]:
    """Кем рендерить каждую сцену: `Scene.render`, а где поля нет — назначение
    по доле клипов проекта (`plan_render`). Чистое чтение: пишет стадия."""
    video = ctx.project.video if ctx.project else None
    clips = ctx.providers.video
    longest = max(clips.supported_durations) if clips and clips.supported_durations else None
    return plan_render(
        ctx.storyboard.ordered_scenes(),
        clip_share=video.clip_share if video else 1.0,
        opening_share=video.clip_opening_share if video else CLIP_OPENING_SHARE,
        max_clip_sec=longest,
    )


def provider_for(ctx: StageContext, mode: SceneRender) -> VideoProvider | None:
    """Клип — видеопровайдер эпизода, картинка-сцена — `providers.still`.
    Выбор по сцене, а не фолбэк: решение принято до вызова, упавший не заменяется."""
    return ctx.providers.still if mode == "still" else ctx.providers.video


def mix_note(ctx: StageContext, modes: dict[str, SceneRender]) -> str:
    """Хвост сообщения: сколько сцен клипами и сколько картинками и какая вышла
    доля по времени. Пусто, когда картинок-сцен нет, — сообщение как до смешивания."""
    scenes = ctx.storyboard.ordered_scenes()
    stills = [s for s in scenes if modes.get(s.id) == "still"]
    if not stills:
        return ""
    clips = [s for s in scenes if modes.get(s.id) != "still"]
    share = clip_share_of(scenes, modes)
    wanted = ctx.project.video.clip_share if ctx.project else 1.0
    return (
        f" · клипами {len(clips)} сцен ({sum(s.duration_sec for s in clips):.0f} с), "
        f"картинками {len(stills)} ({sum(s.duration_sec for s in stills):.0f} с) — "
        f"доля клипов по времени {share:.0%} (в проекте {wanted:.0%})"
    )


def motion_reference_path(ctx: StageContext, scene: Scene) -> Path | None:
    """Где лежит референс движения сцены. Абсолютный путь — как есть; иначе от
    папки эпизода, а нет там — от папки проекта: библиотека движений копится в
    проекте, один джеб годится всем эпизодам. None — файла нет нигде."""
    ref = scene.motion_reference
    if ref is None:
        return None
    raw = Path(ref.file)
    if raw.is_absolute():
        return raw if raw.is_file() else None
    roots = [ctx.project_dir, *([ctx.project_root] if ctx.project_root else [])]
    return next((root / raw for root in roots if (root / raw).is_file()), None)


def _reference_stamp(ctx: StageContext, scene: Scene) -> str:
    """Часть отпечатка клипа про референс: сигнал, сила и содержимое файла.

    По содержимому, а не по пути или mtime: файл переезжает из inbox в проект,
    выкачивается из git, копируется — путь и время меняются, движение нет, и
    перегенерация за деньги тут не за что. Отсутствующий файл — своя метка:
    стадия на нём откажет до платного вызова, а отпечаток обязан считаться и
    для прогноза, и для пульта."""
    ref = scene.motion_reference
    assert ref is not None
    path = motion_reference_path(ctx, scene)
    if path is None:
        digest = "missing"
    else:
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()[:16]
    strength = 1.0 if ref.strength is None else ref.strength
    return f"ref:{ref.signal}:{strength:g}:{digest}"


def _motion_guide(ctx: StageContext, scene: Scene) -> MotionGuide | None:
    """Референс сцены для провайдера: найденный файл, сигнал, сила. Файл к этому
    моменту проверен (`run` отказывает до цикла), поэтому None здесь значит
    только «у сцены референса нет»."""
    ref = scene.motion_reference
    if ref is None:
        return None
    path = motion_reference_path(ctx, scene)
    assert path is not None, f"референс сцены {scene.id} пропал между проверкой и вызовом"
    return MotionGuide(
        path=path, signal=ref.signal, strength=1.0 if ref.strength is None else ref.strength
    )


def _note_motion(ctx: StageContext, scene: Scene, clip: Path) -> None:
    """Профиль движения рядом с клипом; мёртвый клип — предупреждение, не провал.

    Проверка кадров ловит «кто в кадре» и «что дорисовано», но не «ничего не
    происходит»: статичный клип проходил все её пункты, а в финале детекторы
    честно считали его застывшим. Считать это должен python, и до сборки."""
    try:
        profile = load_or_measure(clip)
    except FfmpegError as exc:
        ctx.log.warning("сцена %s: движение не измерено: %s", scene.id, exc)
        return
    if profile.is_dead():
        scene.checks.notes["motion"] = (
            f"клип без движения: энергия {profile.peak:.2f} < {DEAD_ENERGY} — "
            "брак генерации или статичный кадр"
        )
        ctx.log.warning("сцена %s: %s", scene.id, scene.checks.notes["motion"])
    else:
        scene.checks.notes.pop("motion", None)
