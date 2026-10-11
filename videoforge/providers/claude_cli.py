"""LLM через Claude Code CLI (`claude -p`) — работает на подписке, без API-ключа.

Тот же интерфейс LlmProvider, что у gemini: стадиям всё равно, кто отвечает.
Плюс: доплат нет (расход — лимиты подписки Claude). Минусы: медленнее API,
нужен установленный и залогиненный Claude Code, у подписки свои лимиты.
Модель по умолчанию — та, что настроена у пользователя; переопределяется
переменной окружения VF_CLAUDE_CLI_MODEL (например, "haiku").

Промпт передаётся через stdin: у командной строки Windows лимит ~32K символов,
а список видео канала легко его превышает.

Про скорость (замерено на этом провайдере): вход почти ничего не стоит, всё
время уходит на генерацию ответа — ~6 с на запуск процесса плюс ~90 символов в
секунду. Значит экономить надо объём ОТВЕТА, а не промпта: класть в промпт
blueprint и playbook целиком дёшево, а вот просить одним вызовом раскадровку на
сотню сцен — это минуты.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from videoforge.core.proc import NO_WINDOW
from videoforge.paths import ROOT
from videoforge.providers.base import (
    CheckResult,
    FrameToReview,
    LlmProvider,
    LlmResult,
    ProviderError,
    Reference,
    ReviewResult,
)
from videoforge.providers.review import parse_review, review_prompt

# Сколько ждём ответа. Время вызова определяется почти целиком объёмом ОТВЕТА:
# замер — 6 с на запуск процесса плюс ~90 символов в секунду генерации, причём
# размер промпта почти бесплатен (51 КБ на входе добавили 0.3 с). Раскадровка на
# 10 минут видео — это ~36 000 символов JSON, то есть около 7 минут одной
# генерации; прежние 600 с убивали её у самого финиша. Держим запас, а не
# впритык: таймаут нужен против зависшего процесса, а не против длинного ответа.
TIMEOUT_SEC = int(os.environ.get("VF_CLAUDE_CLI_TIMEOUT", "2400"))


def strip_fences(text: str) -> str:
    """Снять ```json-ограду, если модель всё-таки её добавила."""
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        t = t[first_nl + 1 :] if first_nl != -1 else ""
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


class ClaudeCli(LlmProvider):
    name = "claude_cli"
    check_price_usd = 0.0  # подписка: проверка кадров денег не стоит

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        images: list[Path] | None = None,
    ) -> LlmResult:
        exe = shutil.which("claude")
        if exe is None:
            raise ProviderError(
                "claude CLI не найден в PATH — установи Claude Code "
                "или выбери другой LLM-провайдер (--llm gemini)"
            )
        args = [exe, "-p"]
        model = os.environ.get("VF_CLAUDE_CLI_MODEL")
        if model:
            args += ["--model", model]
        text = prompt if system is None else f"{system}\n\n{prompt}"
        if images:
            # Claude Code читает картинки сам, если дать абсолютный путь в промпте —
            # отдельного API для вложений у `claude -p` нет, и не нужно.
            listing = "\n".join(f"- {p.resolve()}" for p in images)
            text = f"Open and look at these local image files:\n{listing}\n\n{text}"
        if json_mode:
            text += "\n\nReturn ONLY valid JSON. No markdown fences, no commentary."
        try:
            # cwd — корень репозитория явно: `claude -p` в неинтерактивном режиме
            # не читает файлы вне своей рабочей папки и вместо вердиктов честно
            # отвечает отказом (замер 14.09.2026: запуск из projects/_bench не
            # увидел ни одного кадра эпизода). Наследовать папку вызывающего
            # значит зависеть от того, откуда человек открыл терминал
            proc = subprocess.run(
                args,
                input=text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT_SEC,
                cwd=str(ROOT),
                **NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"claude -p не ответил за {TIMEOUT_SEC} с") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise ProviderError(f"claude -p завершился с кодом {proc.returncode}: {tail}")
        out = proc.stdout.strip()
        if not out:
            raise ProviderError("claude -p вернул пустой ответ")
        return LlmResult(text=strip_fences(out) if json_mode else out, cost_usd=0.0)

    def check_images(
        self, images: list[Path], *, references: list[Reference], checklist: list[str], context: str
    ) -> CheckResult:
        # Проверка кадра по референсам персонажа — по одному кадру с портфолио
        # на входе: сотни вызовов по ~16 с против ~2 с у API. Держим её на gemini
        # осознанно. Пачкой по описанию claude -p смотрит — review_frames ниже.
        raise ProviderError(
            "claude_cli не проверяет кадры по референсам (медленно) — используй gemini; "
            "проверку по описанию он делает пачкой (review_frames)"
        )

    reviews_frames = True

    def review_frames(self, frames: list[FrameToReview]) -> ReviewResult:
        """Пачка кадров одним вызовом: ≈7 с на кадр при любой пачке и модели
        (`_bench/test9`), $0 на подписке. Пути к файлам идут списком в промпте,
        картинки Claude Code открывает сам."""
        res = self.complete(review_prompt(frames), json_mode=True, images=[f.path for f in frames])
        return ReviewResult(verdicts=parse_review(res.text, frames), cost_usd=0.0, raw=res.text)
