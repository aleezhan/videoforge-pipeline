"""Публикация через браузер: TikTok и Instagram.

У обеих площадок загрузка по API закрыта ревью приложения, которое личному
проекту не пройти. Остаётся то же, что делает человек руками, — форма загрузки
в браузере; Playwright открывает её в **постоянном профиле**
`accounts/<id>/.profile/<платформа>/`, поэтому логин делается один раз
(`videoforge account login <id>`), а дальше сессия живёт в этом профиле.

Два решения, без которых это опасная игрушка:

1. **Успех подтверждает сеть, а не экран.** «Опубликовано» на экране пишут и
   тогда, когда ролик ушёл в черновик, и тогда, когда форма просто перерисовалась.
   Поэтому мы слушаем ответы страницы (`page.on("response")`) и ждём POST на
   адрес публикации со статусом 2xx. Не дождались — это неудача, даже если на
   экране зелёная галочка.
2. **Провал оставляет след.** Скриншот в `accounts/<id>/publish/`: разбирать
   «почему не загрузилось» по тексту исключения бесполезно, а по картинке видно
   сразу — попросили капчу, слетела сессия, поменялась форма.

Селекторы форм площадки меняют без предупреждения. Это не повод прятать
поломку: не нашли поле — падаем с именем селектора и скриншотом, чтобы человек
поправил одну строку в `FLOWS`, а не искал причину в логах.

Playwright — необязательная зависимость: `uv sync --extra publish` и
`uv run playwright install chromium`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videoforge.paths import accounts_dir
from videoforge.providers.base import (
    ProviderError,
    PublishMeta,
    PublishProvider,
    PublishResult,
    Rejected,
)

log = logging.getLogger("videoforge.browser_publish")

PROFILE_DIR = ".profile"
SHOTS_DIR = "publish"
NAV_TIMEOUT_MS = 90_000
UPLOAD_TIMEOUT_MS = 300_000  # заливка файла в форму: минуты на большой ролик
CONFIRM_TIMEOUT_MS = 120_000  # ждём подтверждающий POST


@dataclass(frozen=True)
class Flow:
    """Как выглядит форма загрузки одной площадки.

    `post_markers` — куски URL, по которым узнаётся запрос публикации; именно
    его 2xx считается доказательством, что ролик ушёл."""

    url: str
    file_input: str
    caption: str
    publish_button: str
    post_markers: tuple[str, ...]
    login_markers: tuple[str, ...] = ()
    caption_clear: bool = True  # площадка подставляет имя файла в подпись
    settle_ms: int = 4000  # пауза после выбора файла: форма достраивается


FLOWS: dict[str, Flow] = {
    "tiktok": Flow(
        url="https://www.tiktok.com/tiktokstudio/upload",
        file_input="input[type='file']",
        caption="div[contenteditable='true']",
        publish_button="button:has-text('Post'), button:has-text('Опубликовать')",
        post_markers=("/api/v1/web/project/post", "/aweme/v1/web/aweme/post", "/api/post/item"),
        login_markers=("/login", "/signup"),
    ),
    "instagram": Flow(
        url="https://www.instagram.com/",
        file_input="input[type='file'][accept*='video']",
        caption="div[contenteditable='true'][role='textbox']",
        publish_button=(
            "div[role='button']:has-text('Поделиться'), div[role='button']:has-text('Share')"
        ),
        post_markers=("/api/v1/media/configure", "/graphql/query", "/create/configure"),
        login_markers=("/accounts/login",),
    ),
}


def profile_dir(account_id: str, platform: str) -> Path:
    """Постоянный профиль браузера аккаунта: логин живёт здесь, а не в коде."""
    return accounts_dir() / account_id / PROFILE_DIR / platform


def shots_dir(account_id: str) -> Path:
    return accounts_dir() / account_id / SHOTS_DIR


def _playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # noqa: F841 — сообщение важнее трассировки
        raise ProviderError(
            "playwright не установлен: uv sync --extra publish, "
            "потом uv run playwright install chromium"
        ) from exc
    return sync_playwright


def open_session(account_id: str, platform: str, *, url: str = "") -> None:
    """Открыть браузер в профиле аккаунта, чтобы человек вошёл руками.

    Делается один раз на площадку: пока окно открыто, человек логинится (в том
    числе через 2FA и капчу), после закрытия окна сессия остаётся в профиле.
    Ничего не публикует."""
    flow = FLOWS.get(platform)
    if flow is None and not url:
        raise ProviderError(f"нет формы для площадки {platform}; есть: {', '.join(FLOWS)}")
    target = url or (flow.url if flow else "")
    profile = profile_dir(account_id, platform)
    profile.mkdir(parents=True, exist_ok=True)
    with _playwright()() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(target, timeout=NAV_TIMEOUT_MS)
        log.info("вошли? закрой окно браузера, когда закончишь — профиль сохранится")
        try:
            page.wait_for_event("close", timeout=0)  # ждём, пока человек закроет вкладку
        except Exception:  # noqa: BLE001 — окно закрыли целиком, это и есть выход
            pass
        ctx.close()


class BrowserPublish(PublishProvider):
    """Загрузка формой площадки в постоянном профиле браузера."""

    name = "browser"
    platforms = ("tiktok", "instagram")

    def __init__(self, *, headless: bool = False, flows: dict[str, Flow] | None = None) -> None:
        # окно по умолчанию видимое: молчаливая загрузка в фоне — последнее, что
        # нужно площадке, которая в любой момент может спросить капчу
        self.headless = headless
        self.flows = dict(flows or FLOWS)

    def upload(self, video_path: Path, meta: PublishMeta) -> PublishResult:
        # всё ниже проверяется ДО того, как браузер открыт: файл никуда не
        # ушёл, поэтому исход известен и отметку «загрузка начата» можно снять
        if not video_path.exists():
            raise Rejected(f"файла нет: {video_path}")
        flow = self.flows.get(meta.platform)
        if flow is None:
            raise Rejected(f"{self.name}: площадка {meta.platform} не поддерживается")
        if not meta.account_id:
            raise Rejected(
                "браузерной публикации нужен аккаунт: в нём живёт профиль с логином "
                "(videoforge account login <id>)"
            )
        profile = profile_dir(meta.account_id, meta.platform)
        if not profile.exists():
            raise Rejected(
                f"профиля нет: {profile}. Войди один раз: "
                f"videoforge account login {meta.account_id}"
            )
        return self._run(flow, video_path, meta, profile)

    def _run(self, flow: Flow, video: Path, meta: PublishMeta, profile: Path) -> PublishResult:
        confirmed: list[dict[str, Any]] = []
        with _playwright()() as p:
            ctx = p.chromium.launch_persistent_context(str(profile), headless=self.headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def on_response(resp: Any) -> None:
                """Публикацию подтверждает сеть: POST на адрес публикации с 2xx."""
                try:
                    request = resp.request
                    if request.method != "POST":
                        return
                    if not any(mark in resp.url for mark in flow.post_markers):
                        return
                    if 200 <= resp.status < 300:
                        confirmed.append({"url": resp.url, "status": resp.status})
                except Exception as exc:  # noqa: BLE001 — обработчик не должен ронять загрузку
                    log.debug("ответ не разобран: %s", exc)

            page.on("response", on_response)
            shot = shots_dir(meta.account_id) / (
                f"{meta.platform}-{datetime.now(UTC):%Y%m%d-%H%M%S}.png"
            )
            try:
                page.goto(flow.url, timeout=NAV_TIMEOUT_MS)
                if any(mark in page.url for mark in flow.login_markers):
                    # логин-стена встретила ДО того, как файл выбран: ролик
                    # никуда не ушёл, и это известно
                    _screenshot(page, shot)  # видно, чего именно просят: пароль, 2FA, капчу
                    raise Rejected(
                        f"{meta.platform}: сессия слетела, площадка просит логин. "
                        f"videoforge account login {meta.account_id}"
                    )
                page.set_input_files(flow.file_input, str(video), timeout=UPLOAD_TIMEOUT_MS)
                page.wait_for_timeout(flow.settle_ms)
                caption = page.locator(flow.caption).first
                caption.wait_for(timeout=NAV_TIMEOUT_MS)
                if flow.caption_clear:
                    caption.click()
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Delete")
                caption.type(meta.tagged_description()[:2000], delay=8)
                page.locator(flow.publish_button).first.click(timeout=NAV_TIMEOUT_MS)
                page.wait_for_timeout(1000)
                deadline = CONFIRM_TIMEOUT_MS
                while deadline > 0 and not confirmed:
                    page.wait_for_timeout(1000)
                    deadline -= 1000
                if not confirmed:
                    # исход НЕИЗВЕСТЕН, и это обычный ProviderError намеренно:
                    # кнопку нажали, подтверждения не увидели — ролик мог уйти.
                    # `Rejected` здесь означал бы «его там нет», чего мы не знаем
                    _screenshot(page, shot)
                    raise ProviderError(
                        f"{meta.platform}: подтверждения публикации от площадки не пришло "
                        f"(ждали POST на {', '.join(flow.post_markers)}) — ролик мог уйти. "
                        f"Смотри профиль, не повторяй вслепую. Скриншот: {shot}"
                    )
                return PublishResult(
                    platform=meta.platform,
                    state="done",
                    video_id="",  # id формой не отдаётся: ролик ищется в профиле
                    url="",
                    cost_usd=0.0,
                    request_id=str(confirmed[0]["url"]),
                    warnings=[
                        "браузерная публикация: id ролика площадка не вернула — "
                        "ссылку возьми в приложении/профиле"
                    ],
                    meta={"confirmations": confirmed, "profile": str(profile)},
                )
            except ProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 — любая поломка формы = скриншот и текст
                _screenshot(page, shot)
                raise ProviderError(
                    f"{meta.platform}: загрузка не прошла ({exc}). Скриншот: {shot}"
                ) from exc
            finally:
                ctx.close()


def _screenshot(page: Any, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=True)
        log.warning("скриншот страницы: %s", path)
    except Exception as exc:  # noqa: BLE001 — скриншот не удался, дело не в нём
        log.warning("скриншот не сделан: %s", exc)
