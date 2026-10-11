"""Стадия translate: субтитры на языке зрителя поверх речи на языке источника.

Канал на английскую аудиторию с русскими источниками (модель yuka_mma из
разбора ниши 07.09.2026): речь героя остаётся русской, а текст на экране —
английский. Речь не переозвучивается никогда — своя озвучка есть только у
контролей с ratio 0.05, у хитов её нет.

Что делает: слова отрезка (`assets.captions`, пословный .srt) группируются в
реплики по стилю субтитров — тем же `group_words`, что рисует караоке, — и
каждая реплика переводится моделью. Реплики идут в модель все сразу, с полным
текстом сцены как контекстом, и возвращаются 1:1 с номерами: перевод строки n
обязан говорить ровно то, что сказано в строке n, иначе текст разъедется с
речью. Считает и проверяет python: число строк, номера, длина, пустые ответы;
модель только переводит. Результат — `04_audio/<scene>.captions.<lang>.json`
(`TranslatedCaptions`), ссылка в `assets.translation`; сборка берёт реплики
оттуда по времени и рисует фразой целиком, без караоке.

Пропускается, если язык субтитров проекта не задан или совпадает с языком
речи. Идемпотентна: перевод свежее своего .srt и сделан текущей моделью — не
переделывается (`--force` — исключение).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from videoforge.core.captions import (
    TranslatedCaptions,
    TranslatedLine,
    Word,
    group_words,
    parse_srt_words,
)
from videoforge.core.io import read_model, write_model
from videoforge.core.resources import CaptionStyle, load_resource
from videoforge.core.stage import Stage, StageContext, StageResult
from videoforge.core.storyboard import Scene
from videoforge.stages.cut import AUDIO_DIR
from videoforge.stages.llm_json import ask_json

DEFAULT_STYLE_ID = "default"
# перевод длиннее оригинала бывает, короче — обычно; запас к лимиту символов
# стиля, чтобы модель не резала смысл ради длины
LENGTH_SLACK = 1.5
ATTEMPTS = 2  # второй заход — с перечислением того, что не сошлось

SYSTEM = (
    "You translate subtitle lines for short vertical videos. The speech stays in the "
    "original language; only the on-screen text is translated. Each numbered line is "
    "shown exactly while its original words are spoken, so line n must say what line n "
    "says — no merging, no moving content between lines. Natural spoken language, no "
    "quotes, no brackets, no notes. Keep names as they are commonly written in the "
    "target language (people, places, brands). Unfinished fragments stay unfinished: "
    "the sentence continues in the next line. Answer with JSON only."
)


def translation_path(ctx: StageContext, scene: Scene, lang: str) -> Path:
    return ctx.stage_dir(AUDIO_DIR) / f"{scene.id}.captions.{lang}.json"


def _style(ctx: StageContext) -> CaptionStyle:
    if ctx.resources.caption_style is not None:
        return ctx.resources.caption_style
    try:
        return load_resource("caption_styles", DEFAULT_STYLE_ID, CaptionStyle, ctx.resource_dirs)
    except FileNotFoundError:
        return CaptionStyle(id=DEFAULT_STYLE_ID)


def build_prompt(
    groups: list[list[Word]], *, source_language: str, language: str, max_chars: int, note: str
) -> str:
    """Все реплики сцены одним запросом: контекст — весь текст, ответ — по номерам.
    Первая строка — маркер шага для заглушки; строка `LINES:` — вход одной
    строкой JSON, её же читает заглушка."""
    texts = [" ".join(w.text for w in g) for g in groups]
    lines = [{"n": i + 1, "t": t} for i, t in enumerate(texts)]
    full_text = " ".join(texts)
    return "\n".join(
        [
            "VIDEOFORGE-STEP: translate",
            f"Translate subtitle lines from '{source_language}' to '{language}'.",
            f"Each translated line must be at most {max_chars} characters; split long "
            "thoughts across the lines they are spoken in, never into extra lines.",
            "Return exactly one translation per input line, same numbers, none skipped.",
            f"Full scene text for context: {full_text}",
            f"LINES: {json.dumps(lines, ensure_ascii=False)}",
            'Answer: {"lines": [{"n": 1, "text": "..."}, ...]}' + (f"\n{note}" if note else ""),
        ]
    )


def parse_answer(raw: Any, count: int, *, max_chars: int) -> tuple[list[str], list[str]]:
    """Ответ модели → тексты по номерам и список того, что не сошлось. Длина —
    предупреждение, а не ошибка: обрезать смысл ради лимита хуже, чем перенести
    строку; блокируют только пропуски, дубли и пустые строки."""
    problems: list[str] = []
    items = raw.get("lines") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return [], ["answer must be an object with a 'lines' array"]
    texts: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            problems.append("every line must be an object {n, text}")
            continue
        n, text = item.get("n"), item.get("text")
        if not isinstance(n, int) or not 1 <= n <= count:
            problems.append(f"unknown line number {n!r}")
            continue
        if n in texts:
            problems.append(f"line {n} given twice")
            continue
        if not isinstance(text, str) or not text.strip():
            problems.append(f"line {n} is empty")
            continue
        texts[n] = " ".join(text.split())
    missing = [n for n in range(1, count + 1) if n not in texts]
    if missing:
        problems.append(f"missing lines: {missing}")
    if problems:
        return [], problems
    long = [n for n, t in texts.items() if len(t) > max_chars * 1.3]
    return [texts[n] for n in range(1, count + 1)], (
        [f"lines longer than {max_chars} chars: {long}"] if long else []
    )


def translate_groups(
    ctx: StageContext,
    groups: list[list[Word]],
    *,
    source_language: str,
    language: str,
    max_chars: int,
) -> tuple[list[str], list[str]]:
    """Реплики → переводы 1:1. Второй заход получает список расхождений."""
    note = ""
    warnings: list[str] = []
    for attempt in range(ATTEMPTS):
        prompt = build_prompt(
            groups,
            source_language=source_language,
            language=language,
            max_chars=max_chars,
            note=note,
        )
        raw = ask_json(ctx, prompt, system=SYSTEM)
        texts, problems = parse_answer(raw, len(groups), max_chars=max_chars)
        if texts:
            return texts, problems
        ctx.log.warning("перевод не сошёлся (попытка %d): %s", attempt + 1, "; ".join(problems))
        note = "Your previous answer had problems: " + "; ".join(problems) + ". Fix them."
        warnings = problems
    raise ValueError("модель дважды вернула перевод не по строкам: " + "; ".join(warnings))


class TranslateStage(Stage):
    name = "translate"
    needs = ("llm",)

    def _fresh(self, ctx: StageContext, scene: Scene, path: Path, lang: str) -> bool:
        if ctx.options.force or not path.exists() or not scene.assets.captions:
            return False
        try:
            made = read_model(path, TranslatedCaptions)
        except (ValidationError, ValueError):
            return False
        srt = ctx.abs(scene.assets.captions)
        if not srt.exists() or path.stat().st_mtime < srt.stat().st_mtime:
            return False
        return (
            made.language == lang
            and made.source_language == ctx.storyboard.language
            and made.llm == ctx.providers.llm.name
            and bool(made.lines)
        )

    def run(self, ctx: StageContext) -> StageResult:
        lang = ctx.storyboard.captions_language
        source_language = ctx.storyboard.language
        if not lang or lang == source_language:
            for scene in ctx.storyboard.scenes:
                scene.assets.translation = None
            ctx.save()
            return StageResult(
                status="skipped",
                message=f"субтитры на языке речи ({source_language}) — переводить нечего",
            )
        scenes = [s for s in ctx.storyboard.ordered_scenes() if s.narration.strip() or s.source]
        if not scenes:
            return StageResult(status="skipped", message="в раскадровке нет сцен с речью")
        style = _style(ctx)
        max_chars = int(round(style.max_chars * LENGTH_SLACK))
        llm = ctx.providers.llm.name

        done: list[str] = []
        kept: list[str] = []
        lines_total = 0
        notes: list[str] = []
        for scene in scenes:
            if not (scene.assets.captions and ctx.abs(scene.assets.captions).exists()):
                return StageResult(
                    status="failed",
                    message=f"у сцены {scene.id} нет слов речи (assets.captions): сначала cut",
                )
            path = translation_path(ctx, scene, lang)
            if self._fresh(ctx, scene, path, lang):
                scene.assets.translation = ctx.rel(path)
                kept.append(scene.id)
                continue
            words = parse_srt_words(ctx.abs(scene.assets.captions).read_text(encoding="utf-8"))
            groups = group_words(
                words,
                max_words=style.max_words,
                max_chars=style.max_chars,
                max_gap_sec=style.max_gap_sec,
            )
            if not groups:
                scene.assets.translation = None
                notes.append(f"{scene.id}: слов нет")
                continue
            with ctx.step("перевод", f"{scene.id}: реплик {len(groups)}"):
                try:
                    texts, problems = translate_groups(
                        ctx,
                        groups,
                        source_language=source_language,
                        language=lang,
                        max_chars=max_chars,
                    )
                except ValueError as exc:
                    return StageResult(status="failed", message=f"{scene.id}: {exc}")
            if problems:
                notes.append(f"{scene.id}: {'; '.join(problems)}")
            write_model(
                path,
                TranslatedCaptions(
                    language=lang,
                    source_language=source_language,
                    llm=llm,
                    style_id=style.id,
                    lines=[
                        TranslatedLine(
                            start=g[0].start,
                            end=g[-1].end,
                            source=" ".join(w.text for w in g),
                            text=t,
                        )
                        for g, t in zip(groups, texts, strict=True)
                    ],
                ),
            )
            scene.assets.translation = ctx.rel(path)
            scene.provenance["translation"] = llm
            lines_total += len(groups)
            done.append(scene.id)
            ctx.save()

        if not done:
            return StageResult(
                status="skipped",
                message=f"перевод {source_language}→{lang} актуален для {len(kept)} сцен ({llm})",
            )
        summary = (
            f"перевод {source_language}→{lang}: сцен {len(done)}, реплик {lines_total} ({llm})"
        )
        if kept:
            summary += f"; без изменений: {len(kept)}"
        if notes:
            summary += "; ПРОВЕРЬ: " + " · ".join(notes)
        return StageResult(status="done", message=summary, changed=True)
