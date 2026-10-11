"""Картинки: visual_prompt + каст сцены (персонажи/предметы/места) + стиль →
02_images/<scene>_vNN.png.

Каждая картинка проверяется дважды. Контролёр (`providers.check`) сверяет её
с референсами каста и чеклистами — «тот ли человек» — по одному кадру. Потом
кадры круга уходят llm пачками по двадцать (`stages/frame_review.py`) — «то ли
нарисовано»: кадр «не про то» получает фразу `fix` в описание и, если нашёлся,
референс композиции (`stages/reference_search.py`) — и идёт на повтор. Слепой
перекат сида остаётся запасным ходом: он не лечит кадр, где вместо «ряда,
уходящего в дымку» нарисована куча (s19 ep001, 14.09.2026). Кругов —
`max_attempts`, потом остаётся лучшая попытка. Круг за кругом, а не сцена за
сценой, потому что проверка пачкой дешевле проверки по одному вдвое.

Каст всех сцен разрешается ДО генерации: опечатка в cast не должна стоить
денег. Тем же правилом промпт сверяется со стилем по словам
(`stages/style_conflict.py`): «inscription» при запрете текста в кадре — это
провал проверки, который виден до платного вызова.
"""

from __future__ import annotations

import hashlib

from videoforge.config import frame_review_enabled, vision_check_enabled
from videoforge.core.resources import CastMember
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
from videoforge.core.storyboard import Attempt, Scene, fingerprint
from videoforge.providers.base import ProviderError
from videoforge.stages.frame_review import apply_verdict, review_scenes
from videoforge.stages.prompts import cast_checklist, cast_references, image_prompt, scene_cast
from videoforge.stages.reference_search import find_reference, scene_references
from videoforge.stages.style_conflict import describe, find_conflicts

OUT_DIR = "02_images"


class ImagesStage(Stage):
    name = "images"
    # check — контролёр: сверка кадра с референсами каста. llm — проверка по
    # описанию пачкой и выбор референса по листу кандидатов
    needs = ("image", "check", "llm")

    @staticmethod
    def _reference_digest(ctx: StageContext, scene: Scene) -> str:
        """Референсы композиции сцены — по содержимому файлов: подменили
        картинку под тем же именем — кадр устарел. Пропавший файл тоже меняет
        отпечаток, а сама генерация об этом скажет ошибкой."""
        parts = []
        for rel in scene.reference_assets:
            path = ctx.abs(rel)
            digest = (
                hashlib.sha1(path.read_bytes()).hexdigest()[:12] if path.exists() else "missing"
            )
            parts.append(f"{rel}:{digest}")
        return ";".join(parts)

    @classmethod
    def _inputs(cls, ctx: StageContext, scene: Scene, weights: str) -> str:
        """Из чего сделан кадр: описание, правки к нему, референсы композиции,
        каст, формат, визуальный стиль и веса художника.

        Полный промпт сюда не годится: он собирается уже после того, как каст
        разрешён, а решать «переделывать ли» надо раньше. Поэтому берутся
        поля, которые правит человек, и id ресурсов, которые он переставляет.

        `weights` — чем художник настроен сверх промпта (`ImageProvider.weights`):
        у ComfyUI это LoRA персонажа и её сила, залитые на под. Они лежат не в
        раскадровке и не в проекте, а на карте, и до 12.09.2026 в отпечаток не
        входили: сменили LoRA — готовые кадры считались свежими, и ролик
        собирался из двух лиц (`features/lora_in_fingerprint`). Подаёт их
        вызывающий, потому что провайдер может их и не знать (карта не поднята),
        и тогда отпечатка нет вовсе."""
        style = ctx.resources.style
        return fingerprint(
            scene.visual_prompt,
            sorted(scene.cast),
            ctx.storyboard.aspect_ratio,
            getattr(style, "id", ""),
            getattr(style, "prompt_prefix", ""),
            weights,
            scene.fixes,
            cls._reference_digest(ctx, scene),
        )

    @classmethod
    def _ready(cls, ctx: StageContext, scene: Scene, weights: str | None) -> bool:
        """`weights is None` — художник не знает, чем рендерил бы (карта не
        поднята): входы не сверяются, готовый проверенный кадр остаётся. Это не
        «свежий», а «переделать нечем, и переделывать не за что»: иначе `run`
        после `gpu down` (пересборка, публикация) падал бы на стадии, которой
        делать нечего. Сверка случится, когда карта поднимется."""
        inputs = None if weights is None else cls._inputs(ctx, scene, weights)
        return (
            scene.asset_ready("image", ctx.providers.image.name, inputs)
            and ctx.abs(scene.assets.image or "").exists()
        )

    def _todo(self, ctx: StageContext, weights: str | None) -> list[Scene]:
        return [
            s
            for s in ctx.storyboard.ordered_scenes()
            if ctx.options.force or not self._ready(ctx, s, weights)
        ]

    def estimate_cost(self, ctx: StageContext) -> float:
        todo = self._todo(ctx, ctx.providers.image.weights())
        return len(todo) * ctx.providers.image.price_usd

    def _adopt_kept(self, ctx: StageContext, todo: list[Scene], weights: str | None) -> None:
        """Кадры, которые оставляем, помечаются принятыми — если отпечатка у них
        ещё нет. Отметку ставит стадия, а не предикат: читать раскадровку
        (`estimate`, отчёт, пульт) она менять не должна. Веса неизвестны —
        помечать нечем: отпечаток без них был бы отпечатком других входов."""
        if weights is None:
            return
        keep = [s for s in ctx.storyboard.ordered_scenes() if s not in todo]
        adopt_old_assets(ctx, "image", [(s, self._inputs(ctx, s, weights)) for s in keep])

    @staticmethod
    def _resolve_casts(
        ctx: StageContext, scenes: list[Scene]
    ) -> tuple[dict[str, list[CastMember]], list[str]]:
        casts: dict[str, list[CastMember]] = {}
        errors: list[str] = []
        for scene in scenes:
            try:
                casts[scene.id] = scene_cast(ctx, scene)
            except (ValueError, FileNotFoundError) as exc:
                errors.append(f"{scene.id}: {exc}")
        return casts, errors

    @staticmethod
    def _warn_no_portfolio(ctx: StageContext, casts: dict[str, list[CastMember]]) -> None:
        warned: set[str] = set()
        for cast in casts.values():
            for kind, member in cast:
                if not member.reference_paths() and member.id not in warned:
                    warned.add(member.id)
                    ctx.log.warning(
                        "у %s '%s' нет референсов в портфолио — внешний вид будет плавать; "
                        "положи кадры в %s (персонажу ядро генерирует "
                        "`videoforge resource gen-portfolio`)",
                        kind,
                        member.id,
                        member.folder(),
                    )

    def _attempt(
        self,
        ctx: StageContext,
        scene: Scene,
        cast: list[CastMember],
        weights: str | None,
        lora_mode: bool,
        made: list[Attempt],
    ) -> int:
        """Одна попытка кадра: генерация, сверка с кастом у контролёра, запись в
        журнал попыток. Возвращает 1, если контролёр отказался смотреть."""
        res = ctx.resources
        out_dir = ctx.stage_dir(OUT_DIR)
        cast_refs = cast_references(cast)
        # референсы композиции идут художнику вместе с портфолио, но контролёру
        # — нет: он сверяет лицо, а не раскладку
        refs = cast_refs + scene_references(ctx, scene)
        items = cast_checklist(cast, res)
        # художник с LoRA (ComfyUI на своей карте) держит лицо весами: промпт
        # без паспорта и без референсов каста, иначе ракурсы ломаются
        prompt = image_prompt(
            scene,
            res,
            cast,
            has_references=bool(cast_refs),
            lora_mode=lora_mode,
            fixes=scene.fixes,
        )
        (out_dir / f"{scene.id}.prompt.txt").write_text(prompt, encoding="utf-8")
        n = scene.attempts.get("image", 0) + 1
        scene.attempts["image"] = n
        path = out_dir / f"{scene.id}_v{n:02d}.png"
        ctx.log.info(
            "сцена %s: картинка, попытка %d (референсов: %d%s%s)",
            scene.id,
            n,
            len(refs),
            f", композиция: {len(refs) - len(cast_refs)}" if len(refs) > len(cast_refs) else "",
            f", веса: {weights}" if weights else "",
        )
        ctx.afford(ctx.providers.image.price_usd, f"картинка {scene.id} v{n}")
        gen = ctx.providers.image.generate(
            prompt,
            references=refs,
            aspect_ratio=ctx.storyboard.aspect_ratio,
            out_path=path,
        )
        ctx.add_cost(gen.cost_usd, ctx.providers.image.name, scene.id, f"image v{n}")
        scene.cost_usd["image"] = round(scene.cost_usd.get("image", 0.0) + gen.cost_usd, 6)
        scene.assets.image = ctx.rel(path)
        scene.provenance["image"] = ctx.providers.image.name
        # после генерации художник свои веса знает (comfy без карты сюда
        # не дошёл бы); не знает — отпечатка нет, а не отпечаток без весов
        used = ctx.providers.image.weights()
        if used is None:
            scene.inputs_fingerprint.pop("image", None)
        else:
            scene.inputs_fingerprint["image"] = self._inputs(ctx, scene, used)
        # прежний вердикт по описанию относится к прежнему кадру
        scene.review.pop("image", None)

        refused = 0
        issues: list[str] = []
        check_cost = 0.0
        if items and vision_check_enabled():
            # смотрит контролёр (`providers.check`), не llm: без
            # VF_CHECK_PROVIDER это один и тот же провайдер
            checker = ctx.providers.check
            # цену проверки провайдер называет оценкой сверху: ноль на
            # её месте означал «бесплатно», и при кончившемся бюджете
            # платный вызов проходил
            ctx.afford(checker.check_price_usd, f"проверка кадра {scene.id}")
            check = check_or_refusal(
                checker,
                [path],
                references=cast_refs,
                checklist=items,
                context=scene.visual_prompt,
            )
            ctx.add_cost(check.cost_usd, checker.name, scene.id, "image check")
            check_cost = check.cost_usd
            issues = list(check.issues)
            if record_check(scene, "image", check):
                refused = 1
                ctx.log.warning("сцена %s: %s", scene.id, scene.checks.notes["image"])
        else:
            scene.checks.image = "skipped"
            if items:
                scene.checks.notes["image"] = "проверка отключена (VF_VISION_CHECK=off)"
            else:
                scene.checks.notes.pop("image", None)
        # попытка записывается с вердиктом ДО решения «продолжать ли»:
        # последняя не значит лучшая, и после цикла выбор идёт по журналу
        attempt = Attempt(
            n=n,
            path=ctx.rel(path),
            passed=scene.checks.image != "fail",
            issues=issues,
            cost_usd=round(gen.cost_usd + check_cost, 6),
            provider=ctx.providers.image.name,
        )
        scene.attempt_log.setdefault("image", []).append(attempt)
        made.append(attempt)
        ctx.save()
        if scene.checks.image == "fail":
            ctx.log.warning(
                "сцена %s: проверка не пройдена: %s", scene.id, scene.checks.notes.get("image", "")
            )
        return refused

    @staticmethod
    def _review_note(
        reason: str,
        blocked: int,
        found: list[str],
        unfound: list[str],
        noted: list[str],
        unreviewed: list[str],
    ) -> str:
        """Хвост сообщения стадии про проверку по описанию — иначе её в прогоне
        не отличить от её отсутствия."""
        if reason:
            return f" · проверка по описанию: нет ({reason})"
        parts = [f"по описанию не про то: {blocked}" if blocked else "по описанию все про то"]
        if found:
            parts.append("референсы: " + ", ".join(found))
        if unfound:
            parts.append("референс не нашёлся: " + ", ".join(unfound))
        if noted:
            parts.append("детали в заметках: " + ", ".join(noted))
        if unreviewed:
            parts.append("без вердикта: " + ", ".join(unreviewed))
        return " · " + "; ".join(parts)

    def run(self, ctx: StageContext) -> StageResult:
        weights = ctx.providers.image.weights()
        todo = self._todo(ctx, weights)
        self._adopt_kept(ctx, todo, weights)
        if not todo:
            note = (
                " (карта не поднята: веса художника неизвестны, входы кадров не сверялись)"
                if weights is None
                else ""
            )
            return StageResult(status="done", message="все картинки уже на месте" + note)

        casts, errors = self._resolve_casts(ctx, todo)
        if errors:
            return StageResult(status="failed", message="каст не разрешён: " + "; ".join(errors))
        # промпт против стиля — второй страж после планнера: раскадровку правят
        # руками («поправил JSON — перезапустил»), и правка тоже не должна
        # стоить кадра. Только по словам и $0; мнение модели здесь не
        # спрашивается — оно совет планнеру, а не ворота
        if conflicts := find_conflicts(todo, ctx.resources.style):
            return StageResult(
                status="failed",
                message=(
                    "промпт против стиля: "
                    + describe(conflicts)
                    + " — поправь промпт сцены в storyboard.json или negative/forbid стиля"
                ),
            )
        self._warn_no_portfolio(ctx, casts)
        lora_mode = bool(getattr(ctx.providers.image, "uses_lora", False))

        # проверка по описанию: llm, который писал сценарий, смотрит кадры пачкой
        review_off = ""
        if not frame_review_enabled():
            review_off = "VF_FRAME_REVIEW=off"
        elif not getattr(ctx.providers.llm, "reviews_frames", False):
            review_off = f"{ctx.providers.llm.name} не смотрит кадры пачкой"

        made: dict[str, list[Attempt]] = {s.id: [] for s in todo}
        refused = 0
        blocked = 0
        found: list[str] = []
        unfound: list[str] = []
        noted: list[str] = []
        unreviewed: list[str] = []
        pending = list(todo)
        for round_no in range(ctx.options.max_attempts):
            for scene in pending:
                refused += self._attempt(
                    ctx, scene, casts[scene.id], weights, lora_mode, made[scene.id]
                )
            if not review_off:
                # кадры, не прошедшие сверку каста, и так идут на повтор —
                # смотреть их по описанию незачем
                to_review = [s for s in pending if s.checks.image != "fail"]
                verdicts = review_scenes(ctx, to_review)
                for scene in to_review:
                    verdict = verdicts.get(scene.id)
                    if verdict is None:
                        if scene.id not in unreviewed:
                            unreviewed.append(scene.id)
                        continue
                    apply_verdict(scene, verdict, ctx.providers.llm.name)
                    last = made[scene.id][-1]
                    if verdict.severity == "block":
                        blocked += 1
                        last.passed = False
                        last.issues = [*last.issues, f"не про то: {verdict.issue or '—'}"]
                        ctx.log.warning(
                            "сцена %s: кадр не про то — %s", scene.id, verdict.issue or "—"
                        )
                    elif verdict.severity == "note" and verdict.issue and scene.id not in noted:
                        noted.append(scene.id)
                ctx.save()
            pending = [s for s in pending if s.checks.image == "fail"]
            if not pending or round_no == ctx.options.max_attempts - 1:
                break
            # повтор: кадру «не про то» — фраза fix в описание и референс
            # композиции по запросу вердикта; провалу сверки каста — тот же
            # промпт с новым сидом, как раньше
            for scene in pending:
                rv = scene.review.get("image")
                if rv is None or rv.severity != "block":
                    continue
                if rv.fix and rv.fix not in scene.fixes:
                    scene.fixes.append(rv.fix)
                if rv.search and not scene.reference_assets:
                    # референс — подсказка к повтору, а не условие: отказ поиска
                    # (ep003, 17.09.2026: Commons ответил 429 на 11-м запросе и
                    # уронил стадию с поднятой картой) — повтор без референса
                    try:
                        with ctx.step(f"поиск референса {scene.id}"):
                            entry = find_reference(ctx, scene, rv.search)
                    except ProviderError as exc:
                        ctx.log.warning("сцена %s: референс не найден — %s", scene.id, exc)
                        entry = None
                    if entry:
                        rv.reference = str(entry.get("file", ""))
                        found.append(f"{scene.id} → {entry.get('url') or rv.reference}")
                        ctx.log.info("сцена %s: референс композиции — %s", scene.id, rv.reference)
                    else:
                        unfound.append(scene.id)
            ctx.save()

        failed: list[str] = []
        for scene in pending:
            if made[scene.id]:
                # ни одна не прошла — остаётся не последняя, а с наименьшим
                # числом замечаний (ep002: третья попытка «прошла» тот же поток
                # вверх, который две первые забраковали)
                failed.append(keep_best_attempt(scene, "image", made[scene.id]))
        if failed:
            ctx.save()

        note = refused_note(refused) + self._review_note(
            review_off, blocked, found, unfound, noted, unreviewed
        )
        if failed:
            return StageResult(
                status="failed",
                message="не прошли проверку: " + "; ".join(failed) + note,
                changed=True,
            )
        return StageResult(status="done", message=f"готово сцен: {len(todo)}{note}", changed=True)
