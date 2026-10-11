"""Gemini: картинки (Nano Banana) и текст/зрение для проверок.

Один ключ GEMINI_API_KEY на оба. Цены — ориентир для бюджета, правятся здесь.

Контролёр кадров (`VF_CHECK_PROVIDER=gemini`) живёт на бесплатном тире, а у
того лимиты на модель — 15 запросов в минуту и 500 в день (flash-lite,
замер 07.09.2026). Поэтому здесь темп (`Pace`: не больше N вызовов за любые
60 с) и повтор на 429/503 с паузой, которую просит сервер. Отказ модели
смотреть (`block_reason`) — не ошибка HTTP и не вердикт: он возвращается как
`CheckResult.refused`, и стадия пишет «проверка не состоялась».
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from videoforge.providers.base import (
    CheckResult,
    FrameToReview,
    GenResult,
    ImageProvider,
    LlmProvider,
    LlmResult,
    ProviderError,
    Reference,
    ReviewResult,
)
from videoforge.providers.review import parse_review, review_prompt

log = logging.getLogger("videoforge.gemini")


def _labeled_references(references: list[Reference]) -> list:
    """Референсы с подписями: перед картинками каждой сущности — текст, что на них.
    Без подписей модель путает, чей референс чей."""
    contents: list = []
    current = None
    for ref in references:
        if ref.label != current:
            contents.append(f"Reference images for {ref.label}:")
            current = ref.label
        contents.append(Image.open(ref.path))
    return contents


# USD за картинку (сверено с ai.google.dev/gemini-api/docs/pricing 2026-08-29).
# У 3.x цена зависит от разрешения; здесь 1K (1024px) — дефолт генерации.
IMAGE_PRICES: dict[str, float] = {
    "gemini-2.5-flash-image": 0.039,  # отключают 2026-10-02
    "gemini-3.1-flash-image": 0.067,  # 0.5K $0.045 / 2K $0.101 / 4K $0.151
    "gemini-3.1-flash-lite-image": 0.0336,
    "gemini-3-pro-image": 0.134,  # 4K $0.24
}
# USD за миллион токенов (вход, выход). 2.5-flash сверено с прайсом 2026-08-29;
# остальные — ОРИЕНТИР по ai.google.dev/gemini-api/docs/pricing на 14.09.2026,
# СВЕРИТЬ перед первым платным прогоном (у 3.6-flash цена объявлена «до
# 31.12.2026»). Контролёр на бесплатном тире не платит ничего; цифра здесь —
# что стоило бы то же на платном, чтобы расход эпизода не был нулём молча.
# Модели, которой здесь нет, `_usage_cost` пишет $0 и предупреждает — doctor
# тоже: ноль на месте неизвестной цены читается как «бесплатно»
TEXT_PRICES: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),  # контролёр: 15 RPM / 500 RPD на бесплатном тире
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.6-flash": (0.75, 3.75),  # до 31.12.2026
}
# Оценка СВЕРХУ одной проверки кадров: бюджетному стражу нужно число до вызова,
# а настоящая цена приходит в ответе. Считано по прайсу 2.5-flash: кадр и
# несколько референсов — тысячи входных токенов ($0.30/М) плюс сотни выходных
# ($2.50/М), то есть десятые доли цента. Взято с запасом почти вдесятеро:
# задача числа — не пропустить платный вызов при кончившемся бюджете, а не
# предсказать счёт.
CHECK_PRICE_USD = 0.01


def _client():  # noqa: ANN202 — тип клиента приходит из SDK
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ProviderError("GEMINI_API_KEY не задан (см. .env.example)")
    from google import genai

    return genai.Client(api_key=key)


_unpriced_warned: set[str] = set()


def price_known(model: str) -> bool:
    """Есть ли у текстовой модели цена в таблице. Нет — расход пишется как $0,
    и это надо сказать вслух (doctor, лог), а не считать бесплатным."""
    return model in TEXT_PRICES


def _usage_cost(response, model: str) -> float:  # noqa: ANN001
    prices = TEXT_PRICES.get(model)
    usage = getattr(response, "usage_metadata", None)
    if not prices:
        if model not in _unpriced_warned:
            _unpriced_warned.add(model)
            log.warning(
                "цена модели %s неизвестна: расходы пишутся как $0 — "
                "добавь её в TEXT_PRICES (providers/gemini.py)",
                model,
            )
        return 0.0
    if not usage:
        return 0.0
    p_in, p_out = prices
    tokens_in = getattr(usage, "prompt_token_count", 0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0
    return (tokens_in * p_in + tokens_out * p_out) / 1_000_000


# ------------------------------------------------------------- темп и повтор

RPM_DEFAULT = 15  # бесплатный тир flash-lite: 15 запросов в минуту на модель
WINDOW_SEC = 60.0
RETRIES = 3
BACKOFF_SEC = 2.0  # своя пауза: 2 с, потом 4 с; пауза, которую просит сервер, побеждает
MAX_PAUSE_SEC = 60.0  # дольше не ждём: лимит дня так не переждать, решает человек
RETRY_CODES = (429, 503)

# sleep — через модульную ссылку, чтобы тесты подменяли ожидание, не засыпая
_sleep: Callable[[float], None] = time.sleep


def rpm_limit() -> int:
    """Потолок вызовов в минуту на модель: `VF_GEMINI_RPM`, 0 — без ограничения."""
    raw = os.environ.get("VF_GEMINI_RPM", "").strip()
    try:
        return int(raw) if raw else RPM_DEFAULT
    except ValueError:
        return RPM_DEFAULT


class Pace:
    """Не больше `rpm` вызовов за любые 60 секунд.

    Окно по времени последних `rpm` вызовов, а не ведро с пополнением: лимит
    бесплатного тира сформулирован именно так — «15 запросов в минуту», и
    16-й внутри той же минуты приходит 429-м. Память процесса: пульт и
    терминал друг о друге не знают, но одну модель одновременно они не
    дёргают — замок эпизода один. `rpm <= 0` — без ограничения."""

    def __init__(self, rpm: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.rpm = rpm
        self._clock = clock
        self._calls: deque[float] = deque()

    def _forget_old(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= WINDOW_SEC:
            self._calls.popleft()

    def wait(self) -> float:
        """Дождаться своей очереди и занять место. Возвращает, сколько ждали."""
        if self.rpm <= 0:
            return 0.0
        now = self._clock()
        self._forget_old(now)
        waited = 0.0
        if len(self._calls) >= self.rpm:
            waited = WINDOW_SEC - (now - self._calls[0])
            log.info("темп Gemini: %d вызовов за минуту, жду %.1f с", self.rpm, waited)
            _sleep(waited)
            # часы могли не сдвинуться (подменённый sleep в тестах) — место в
            # окне всё равно считается с момента, когда ожидание кончилось
            now = max(self._clock(), now + waited)
            self._forget_old(now)
        self._calls.append(now)
        return waited


_paces: dict[str, Pace] = {}


def pace(model: str) -> Pace:
    """Темп на модель — лимиты тира считаются по модели, а не по ключу.
    Два экземпляра провайдера с одной моделью (llm и контролёр) делят окно."""
    if model not in _paces:
        _paces[model] = Pace(rpm_limit())
    return _paces[model]


def _retry_after(exc: BaseException) -> float | None:
    """Сколько сервер просит подождать: заголовок `retry-after` или
    `retryDelay` («7s») в деталях ошибки. Нет — None, пауза берётся своя."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None and (value := headers.get("retry-after")):
        try:
            return float(value)
        except ValueError:
            pass
    details = getattr(exc, "details", None)
    error = details.get("error", details) if isinstance(details, dict) else {}
    for item in (error.get("details") if isinstance(error, dict) else None) or []:
        delay = item.get("retryDelay") if isinstance(item, dict) else None
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                pass
    return None


def _call[T](what: str, request: Callable[[], T]) -> T:
    """Вызов API с повтором на 429/503: до `RETRIES` попыток, пауза — та, что
    просит сервер, иначе экспонента от `BACKOFF_SEC`. Остальные ошибки API
    заворачиваются в `ProviderError` сразу: стадия про транспорт не знает."""
    from google.genai import errors

    for attempt in range(1, RETRIES + 1):
        try:
            return request()
        except errors.APIError as exc:
            code = getattr(exc, "code", None)
            if code not in RETRY_CODES or attempt == RETRIES:
                tries = f" после {attempt} попыток" if attempt > 1 else ""
                raise ProviderError(
                    f"Gemini, {what}: {code} {exc.status or ''}{tries} — {exc.message}"
                ) from exc
            asked = _retry_after(exc)
            delay = min(
                asked if asked is not None else BACKOFF_SEC * 2 ** (attempt - 1), MAX_PAUSE_SEC
            )
            log.warning(
                "Gemini, %s: %s %s — повтор %d/%d через %.0f с%s",
                what,
                code,
                exc.status or "",
                attempt + 1,
                RETRIES,
                delay,
                " (пауза от сервера)" if asked is not None else "",
            )
            _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _refusal(response: Any) -> str:
    """Почему ответа нет — словами Google. Пусто — ответ есть.

    Блок приходит без ошибки HTTP: `prompt_feedback.block_reason` на запросе
    или `finish_reason` кандидата без текста. Пустой `text` без причины —
    тоже отказ: молчание нельзя читать ни как «прошёл», ни как «провал»."""
    reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
    name = getattr(reason, "name", reason)
    if name and name != "BLOCKED_REASON_UNSPECIFIED":
        return f"block_reason={name}"
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "пустой ответ: ни одного кандидата"
    if (getattr(response, "text", None) or "").strip():
        return ""
    finish = getattr(candidates[0], "finish_reason", None)
    finish_name = getattr(finish, "name", finish)
    if finish_name and finish_name not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
        return f"finish_reason={finish_name}"
    return "пустой ответ"


class GeminiImage(ImageProvider):
    name = "gemini"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
        self.price_usd = IMAGE_PRICES.get(self.model, 0.039)
        # клиент лениво: без ключа провайдер строится и отдаёт цены (estimate),
        # ключ нужен только в момент генерации
        self._client = None

    @property
    def client(self):  # noqa: ANN202 — тип клиента приходит из SDK
        if self._client is None:
            self._client = _client()
        return self._client

    def generate(
        self, prompt: str, *, references: list[Reference], aspect_ratio: str, out_path: Path
    ) -> GenResult:
        from google.genai import types

        contents: list = _labeled_references(references)
        contents.append(prompt)
        pace(self.model).wait()
        response = _call(
            "картинка",
            lambda: self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
                ),
            ),
        )
        for candidate in response.candidates or []:
            for part in (candidate.content.parts if candidate.content else None) or []:
                data = getattr(part.inline_data, "data", None) if part.inline_data else None
                if data:
                    out_path.write_bytes(data)
                    return GenResult(out_path, self.price_usd, {"model": self.model})
        text = (getattr(response, "text", None) or "")[:300]
        raise ProviderError(f"Gemini не вернул изображение. Ответ: {text!r}")


CHECK_INSTRUCTION = """You are a strict quality-control reviewer for frames of a short video.
You receive: a scene description, a checklist, labeled REFERENCE images (characters,
props, locations — each labeled with what it depicts), and one or more CANDIDATE
frames to review.

Reply with JSON only: {"pass": true|false, "issues": ["..."]}.
Fail (pass=false) if ANY of these hold:
- a referenced character does not match its reference images in identity: face,
  hairstyle and hair color, eye color, age, body build, skin tone, distinctive
  marks. Clothing is NOT part of identity: wardrobe comes from the scene
  description and the checklist, so clothes different from the references are fine,
- a referenced prop or location does not match its reference images (shape,
  colors, materials, distinctive details),
- any checklist item is violated,
- visible artifacts: extra/deformed fingers or limbs, distorted face, garbled text,
  watermarks, duplicated character,
- the frame clearly does not match the scene description,
- for video frames: a character or object morphs or changes appearance between frames.
Be concise: each issue is one short sentence. If everything is fine, issues is [].
"""


class GeminiLlm(LlmProvider):
    name = "gemini"
    check_price_usd = CHECK_PRICE_USD

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
        self._client = None  # лениво, как у GeminiImage

    @property
    def client(self):  # noqa: ANN202 — тип клиента приходит из SDK
        if self._client is None:
            self._client = _client()
        return self._client

    @property
    def price_known(self) -> bool:
        """Знает ли таблица цену этой модели — иначе расход эпизода будет нулём
        молча; doctor об этом предупреждает."""
        return price_known(self.model)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        images: list[Path] | None = None,
    ) -> LlmResult:
        from google.genai import types

        contents: list = [prompt]
        if images:
            contents.extend(Image.open(p) for p in images)
        pace(self.model).wait()
        response = _call(
            "текст",
            lambda: self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json" if json_mode else None,
                ),
            ),
        )
        if refused := _refusal(response):
            # текстовой стадии пустая строка не поможет — она упадёт на разборе
            # JSON строкой ниже и без причины; причина здесь
            raise ProviderError(f"Gemini не ответил ({refused})")
        return LlmResult(text=response.text or "", cost_usd=_usage_cost(response, self.model))

    def check_images(
        self, images: list[Path], *, references: list[Reference], checklist: list[str], context: str
    ) -> CheckResult:
        from google.genai import types

        contents: list = [
            CHECK_INSTRUCTION,
            f"SCENE DESCRIPTION:\n{context}",
            "CHECKLIST:\n" + "\n".join(f"- {item}" for item in checklist),
        ]
        contents.extend(_labeled_references(references))
        contents.append("CANDIDATE frames to review:")
        contents.extend(Image.open(p) for p in images)

        pace(self.model).wait()
        response = _call(
            "проверка кадров",
            lambda: self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            ),
        )
        cost = _usage_cost(response, self.model)
        if refused := _refusal(response):
            # отказ смотреть — не вердикт: ни pass, ни fail. Входные токены
            # при этом посчитаны, и на платном тире они стоят денег
            return CheckResult(passed=False, cost_usd=cost, raw="", refused=refused)
        raw = response.text or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return CheckResult(
                passed=False, issues=["проверяющий вернул не JSON"], cost_usd=cost, raw=raw
            )
        return CheckResult(
            passed=bool(data.get("pass")),
            issues=[str(i) for i in data.get("issues", [])],
            cost_usd=cost,
            raw=raw,
        )

    reviews_frames = True

    def review_frames(self, frames: list[FrameToReview]) -> ReviewResult:
        """Пачка кадров одним вызовом: тот же промпт и разбор, что у claude_cli
        (`providers/review.py`); картинки идут телом запроса, каждая подписана
        путём и id сцены — на них ссылается промпт."""
        from google.genai import types

        contents: list = [review_prompt(frames)]
        for f in frames:
            contents.append(f"Image file {f.path.resolve()} (scene {f.id}):")
            contents.append(Image.open(f.path))
        pace(self.model).wait()
        response = _call(
            "проверка кадров по описанию",
            lambda: self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            ),
        )
        cost = _usage_cost(response, self.model)
        if refused := _refusal(response):
            raise ProviderError(f"Gemini не стал смотреть кадры ({refused})")
        raw = response.text or ""
        return ReviewResult(verdicts=parse_review(raw, frames), cost_usd=cost, raw=raw)
