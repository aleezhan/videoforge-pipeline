"""YouTube Studio глазами браузера: то, чего нет в Analytics API.

Показы значка и CTR («сколько раз ролик показали и сколько раз по нему
кликнули») YouTube отдаёт только в Studio — ни Data API, ни Analytics API их не
знают. Поэтому здесь браузер: Playwright открывает страницы аналитики ролика в
**постоянном профиле** `accounts/<id>/.profile/studio/` (тот же приём, что у
публикации в TikTok), снимает скриншот и видимый текст каждой вкладки и
вытаскивает из текста числа.

Только чтение: ни одного нажатия, кроме перехода по адресу. Вход в Google
делает человек один раз (`videoforge account login <id> --studio`), дальше
сессия живёт в профиле. Встроенный Chromium Playwright Google для входа не
пускает («браузер небезопасен»), поэтому берётся установленный Chrome
(`channel="chrome"`) без флага автоматизации.

Вёрстка Studio меняется без предупреждения. Поэтому текст и скриншоты
сохраняются всегда, а разбор — отдельная функция над сохранённым текстом:
не нашлось числа — поле None, страница на диске, и чинится одна строка в
`LABELS`, а не весь сбор заново.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videoforge.core.accounts import StudioStats
from videoforge.providers.base import ProviderError
from videoforge.providers.browser_publish import _playwright, profile_dir

log = logging.getLogger("videoforge.youtube_studio")

PLATFORM = "studio"  # имя профиля: accounts/<id>/.profile/studio/
STUDIO_URL = "https://studio.youtube.com"
CHROME = "chrome"
# без этого флага Google видит navigator.webdriver и не пускает вход
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
NAV_TIMEOUT_MS = 90_000
SETTLE_MS = 6_000  # графики Studio дорисовываются после загрузки страницы

# вкладка аналитики ролика → адрес; порядок — порядок обхода
TABS: dict[str, str] = {
    "overview": "tab-overview",
    "reach": "tab-reach_viewers",
    "engagement": "tab-interest_viewers",
}

# поле → подписи, после которых Studio печатает число (ru и en интерфейс)
LABELS: dict[str, tuple[str, ...]] = {
    "impressions": ("Показы", "Impressions"),
    "ctr_pct": ("CTR для значков видео", "Impressions click-through rate", "CTR значков"),
    "views": ("Просмотры", "Views"),
    "avg_view_sec": ("Средняя продолжительность просмотра", "Average view duration"),
}

_NUMBER = re.compile(r"^\s*(\d[\d\s\u00a0\u202f.,]*)\s*(тыс\.?|млн|K|M)?\s*(%)?\s*$")
_DURATION = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*$")


def video_url(video_id: str, tab: str) -> str:
    return f"{STUDIO_URL}/video/{video_id}/analytics/{TABS[tab]}/period-default"


def _launch(p: Any, profile: Path, *, headless: bool) -> Any:
    return p.chromium.launch_persistent_context(
        str(profile),
        channel=CHROME,
        headless=headless,
        args=LAUNCH_ARGS,
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1600, "height": 1200},
    )


def open_login(account_id: str) -> Path:
    """Открыть Studio в профиле аккаунта, чтобы человек вошёл в Google руками.
    Ждёт, пока окно закроют; ничего не читает и не нажимает."""
    profile = profile_dir(account_id, PLATFORM)
    profile.mkdir(parents=True, exist_ok=True)
    with _playwright()() as p:
        ctx = _launch(p, profile, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(STUDIO_URL, timeout=NAV_TIMEOUT_MS)
        log.info("войди в Google и закрой окно — сессия останется в профиле")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:  # noqa: BLE001 — окно закрыли целиком, это и есть выход
            pass
        ctx.close()
    return profile


def read_videos(
    account_id: str, video_ids: list[str], *, shots_dir: Path, headless: bool = False
) -> list[StudioStats]:
    """Страницы аналитики каждого ролика → числа + скриншоты + текст.

    Окно по умолчанию видимое: Google заметнее относится к безголовому Chrome,
    а сессия в профиле одна, и терять её из-за экономии окна незачем."""
    profile = profile_dir(account_id, PLATFORM)
    if not profile.exists():
        raise ProviderError(
            f"профиля Studio нет: {profile}. Войди один раз: "
            f"videoforge account login {account_id} --studio"
        )
    shots_dir.mkdir(parents=True, exist_ok=True)
    out: list[StudioStats] = []
    with _playwright()() as p:
        ctx = _launch(p, profile, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for vid in video_ids:
                texts: dict[str, str] = {}
                shots: list[str] = []
                for tab in TABS:
                    page.goto(video_url(vid, tab), timeout=NAV_TIMEOUT_MS)
                    if "accounts.google.com" in page.url:
                        raise ProviderError(
                            "Studio просит вход — сессия слетела: "
                            f"videoforge account login {account_id} --studio"
                        )
                    page.wait_for_timeout(SETTLE_MS)
                    shot = shots_dir / f"{vid}.studio.{tab}.png"
                    page.screenshot(path=str(shot), full_page=True)
                    shots.append(shot.name)
                    texts[tab] = page.inner_text("body")
                stats = parse_studio(vid, texts)
                stats.screenshots = shots
                out.append(stats)
        finally:
            ctx.close()
    return out


def parse_studio(video_id: str, texts: dict[str, str]) -> StudioStats:
    """Текст вкладок → числа. Ищется подпись, число — на ближайшей следующей
    строке, которая целиком число (Studio печатает подпись и значение
    отдельными строками). Первое попадание побеждает: обзор идёт раньше
    вкладки охвата, и числа там одного окна."""
    lines = [ln.strip() for tab in TABS for ln in texts.get(tab, "").splitlines() if ln.strip()]
    found: dict[str, float | None] = {}
    for field, labels in LABELS.items():
        found[field] = _after_label(lines, labels, duration=field == "avg_view_sec")
    return StudioStats(
        video_id=video_id,
        at=datetime.now(UTC),
        impressions=None if found["impressions"] is None else int(found["impressions"]),
        ctr_pct=found["ctr_pct"],
        views=None if found["views"] is None else int(found["views"]),
        avg_view_sec=found["avg_view_sec"],
        text=texts,
    )


def _after_label(lines: list[str], labels: tuple[str, ...], *, duration: bool) -> float | None:
    for i, line in enumerate(lines):
        if not any(line.lower().startswith(label.lower()) for label in labels):
            continue
        # подпись и число бывают в одной строке: «Показы 1 234»
        tail = [line[len(lab) :] for lab in labels if line.lower().startswith(lab.lower())]
        for candidate in [*tail, *lines[i + 1 : i + 4]]:
            value = parse_duration(candidate) if duration else parse_number(candidate)
            if value is not None:
                return value
    return None


def parse_number(text: str) -> float | None:
    """«1 234», «3,0 %», «1,2 тыс.», «12.5K» → число; остальное — None."""
    m = _NUMBER.match(text)
    if not m:
        return None
    raw = m.group(1).replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")  # 1,234.5 — английская запись
    else:
        raw = raw.replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    scale = {"тыс": 1e3, "тыс.": 1e3, "K": 1e3, "млн": 1e6, "M": 1e6}.get(m.group(2) or "", 1)
    return value * scale


def parse_duration(text: str) -> float | None:
    """«3:51», «1:02:05» → секунды."""
    m = _DURATION.match(text)
    if not m:
        return None
    hours, minutes, seconds = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return float(hours * 3600 + minutes * 60 + seconds)
