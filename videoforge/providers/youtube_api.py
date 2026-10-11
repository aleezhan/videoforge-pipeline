"""Загрузка ролика на YouTube: Data API v3, resumable upload.

Почему без google-api-python-client: весь протокол здесь — три HTTP-запроса
(обновить токен, начать сессию, залить байты), а httpx в проекте уже есть.
Лишний SDK тянет свои зависимости и прячет ровно то, что важно видеть: какие
поля уходят вместе с файлом.

**Доступ.** OAuth-токен человек получает один раз (см. `docs/PUBLISHING.md`),
и лежит он у аккаунта — `.secrets/<аккаунт>/youtube_token.json`:

    {"client_id": "...", "client_secret": "...", "refresh_token": "..."}

Папка под `.gitignore`; в `.env` токен не кладём — он не одна строка, а объект,
и живёт дольше сессии. Значения не печатаются нигде, только факт наличия.
Токен — на аккаунт, а не на сеть, потому что канал для Google и есть токен:
общий файл на всех второй вход перезаписал бы, и ролики обоих проектов уехали
бы на тот канал, который логинился последним. Поэтому провайдер строится под
аккаунт (`YoutubeUpload(account_id)`), а без аккаунта у проекта грузить нечем.

**Права берутся все сразу**, и это решение, а не привычка. `youtube.upload`
умеет ровно положить файл и обложку к нему; всё, что делается с роликом ПОСЛЕ
загрузки — открыть видимость, поправить язык, поставить обложку заново, узнать,
обработался ли он, — на нём запрещено (`videos.list` отвечает 403 «insufficient
scope», проверено 11.09.2026). Узкого «править только свои ролики» права у Data
API нет: наименьшее, которое это умеет, — `youtube.force-ssl`, то есть полный
доступ к каналу, включая удаление. Плюс два права на статистику: официальные
цифры — удержание, показы, CTR, источники трафика, доход — живут в YouTube
Analytics API, и со стороны (yt-dlp) их не видно вообще.

Почему не «минимально необходимое»: Google не расширяет права выданному
токену, поэтому каждое добавленное потом — это поход человека в консоль плюс
повторный вход в браузере. 11.09.2026 это стоило дня: права на одну загрузку
хватило, чтобы залить ролик, и не хватило, чтобы посмотреть, что с ним стало,
— язык и обложку пришлось искать по скриншотам. Подробности и чем это
ограничено — `docs/PUBLISHING.md`.

Что токен **реально** умеет, знает Google, а не наша запись о том, чего мы
просили: право отзывают на стороне Google, и файл об этом не узнаёт. Поэтому
список прав читается из ответа на обновление токена (`scope`), а не из файла.

**Пометка синтетического контента.** У `videos.insert` за это отвечает
`status.containsSyntheticMedia` (write-only). Поле молодое, и на части
аккаунтов API его ещё не принимает — тогда загрузка повторяется без него, а в
результат уходит предупреждение: галочку «изменённый или синтетический контент»
надо поставить руками в YouTube Studio. Молча грузить сгенерированный голос без
пометки нельзя — это нарушение правил площадки.

**Квота.** Одна загрузка стоит 1600 единиц из 10000 в сутки: примерно шесть
роликов в день на проект Google Cloud. Денег не стоит — `cost_usd = 0`.

**Статистика** (`analytics_snapshot`, `YoutubeUpload.analytics`) — тем же
токеном, YouTube Analytics API v2: окно последних дней (просмотры, минуты,
удержание, подписки, лайки, доход) целиком и по дням, плюс итоги канала «за
всё время» из `channels.list`. Квота у Analytics своя, загрузок не касается.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from videoforge.paths import secrets_dir
from videoforge.providers.base import (
    ProviderError,
    PublishMeta,
    PublishProvider,
    PublishResult,
    Rejected,
    landed_state,
)

log = logging.getLogger("videoforge.youtube_api")

TOKEN_FILE = "youtube_token.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
# Претензии Content ID Data API v3 не отдаёт никому: они живут в youtubePartner
# API, который выдают только правообладателям с CMS. Поэтому «обработан» от
# `videos.list` не значит «без претензий», и молчать об этом нельзя — человек
# прочитает «processed» как «всё чисто»
CONTENT_ID_NOTE = (
    "претензии Content ID через API не видны — смотреть в Studio: Контент → Ограничения"
)
# Ролик, найденный по названию после оборванной загрузки, должен быть загружен
# после её начала. Час запаса — на часы, которые расходятся, и на то, что
# отметка in_flight ставится до вызова, а площадка датирует по своим часам
FIND_SLACK = timedelta(hours=1)
UPLOADS_PAGE = 50  # сколько последних загрузок канала читает поиск по названию
SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"  # положить файл
SCOPE_MANAGE = "https://www.googleapis.com/auth/youtube.force-ssl"  # читать и править своё
SCOPE_STATS = "https://www.googleapis.com/auth/yt-analytics.readonly"  # удержание, показы, CTR
SCOPE_MONEY = "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"  # доход, RPM
# Права просятся все сразу и одним входом намеренно. Каждое добавленное потом —
# это поход человека в консоль Google плюс повторный вход в браузере, потому что
# Google не расширяет права выданному токену: узкий набор стоит не «меньше
# риска», а потерянного дня на каждую новую надобность. Так и вышло 11.09.2026 —
# права на одну загрузку хватило, чтобы залить ролик, и не хватило, чтобы
# посмотреть, что с ним стало.
SCOPES = (SCOPE_UPLOAD, SCOPE_MANAGE, SCOPE_STATS, SCOPE_MONEY)
RELOGIN = "нужен повторный вход: videoforge account login <аккаунт> --force"
ANALYTICS_DAYS = 28  # окно статистики по умолчанию — как «последние 28 дней» в Studio
# Свежие дни площадка досчитывает: цифры за вчера и позавчера меняются ещё
# двое-трое суток после. Ряд по дням у аккаунта append-only, поэтому день
# отдаётся как устоявшийся только с этого возраста — иначе первое, частичное
# число легло бы в файл навсегда
ANALYTICS_LAG_DAYS = 3
CHUNK = 8 * 1024 * 1024  # 8 МБ: кратно 256 КБ, как требует resumable upload
TIMEOUT = 600.0
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_MAX = 500  # суммарная длина тегов, знаков

PRIVACY = {"private", "unlisted", "public"}

# `videos.update` заменяет часть ЦЕЛИКОМ, а не сливает поля: прислать один
# `privacyStatus` значит обнулить лицензию, встраивание и показ статистики.
# Поэтому правка идёт поверх прочитанного, и переносятся ровно эти ключи —
# в прочитанном лежат и read-only поля вроде uploadStatus, которым в запросе
# не место.
DEFAULT_CATEGORY = "22"  # People & Blogs: раздел для ролика, чей проект молчит

CARRY_SNIPPET = (
    "title",
    "description",
    "tags",
    "categoryId",
    "defaultLanguage",
    "defaultAudioLanguage",
)
CARRY_STATUS = (
    "privacyStatus",
    "license",
    "embeddable",
    "publicStatsViewable",
    "selfDeclaredMadeForKids",
    "publishAt",
)


def token_path(account_id: str) -> Path:
    """Токен аккаунта: `.secrets/<аккаунт>/youtube_token.json`."""
    if not account_id:
        raise ProviderError(
            "токен YouTube лежит у аккаунта, а аккаунт не назван: "
            "videoforge account new <id> --network youtube --project <проект>"
        )
    return secrets_dir() / account_id / TOKEN_FILE


def legacy_token_path() -> Path:
    """Где токен лежал до 12.09.2026 — один на всех. Читать его нельзя: чей он,
    файл не знает. Путь нужен только чтобы сказать человеку, куда перенести."""
    return secrets_dir() / TOKEN_FILE


def known_client(*, prefer: str = "") -> tuple[str, str, Path] | None:
    """client_id и client_secret из любого уже лежащего токена: свой аккаунт
    первым, затем остальные, затем общий файл прежней раскладки. Клиент OAuth
    один на приложение, и второму каналу искать скачанный из консоли файл
    заново незачем; refresh-токен отсюда не берётся никогда — он и есть канал."""
    candidates: list[Path] = [token_path(prefer)] if prefer else []
    candidates += sorted(p for p in secrets_dir().glob(f"*/{TOKEN_FILE}") if p not in candidates)
    candidates.append(legacy_token_path())
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cid, secret = str(data.get("client_id") or ""), str(data.get("client_secret") or "")
        if cid and secret:
            return cid, secret, path
    return None


# ----------------------------------------------------------------- вход в Google

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# Google называет это «loopback flow» и разрешает его клиентам типа Desktop app:
# любой порт на 127.0.0.1, никаких заранее прописанных адресов. Поэтому вход
# делается здесь, а не через OAuth Playground: тому нужен клиент Web application
# со своим redirect URI, то есть ещё один экран консоли и ещё одна возможность
# ошибиться.
AUTH_PAGE = (
    "<html><meta charset='utf-8'><body style='font:16px sans-serif;padding:40px'>"
    "<h2>{head}</h2><p>{body}</p></body></html>"
)


def client_from_json(path: Path) -> tuple[str, str]:
    """client_id и client_secret из файла, который даёт консоль Google.

    Формат у неё свой: `{"installed": {...}}` для Desktop app, `{"web": {...}}`
    для Web application. Разбираем оба — человек скачивает файл как есть."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderError(f"{path} не читается: {exc}") from exc
    block = data.get("installed") or data.get("web") or data
    cid, secret = str(block.get("client_id", "")), str(block.get("client_secret", ""))
    if not cid or not secret:
        raise ProviderError(f"{path}: нет client_id/client_secret — это точно файл клиента OAuth?")
    return cid, secret


def authorize(
    client_id: str,
    client_secret: str,
    *,
    account_id: str,
    scopes: Sequence[str] = SCOPES,
    timeout_sec: float = 300.0,
    open_browser: bool = True,
    log_fn: Any = log.info,
) -> Path:
    """Вход в Google один раз: браузер → код на 127.0.0.1 → refresh-токен в файл.

    Возвращает путь к `.secrets/<аккаунт>/youtube_token.json`. Значения не печатаются:
    наружу уходит только путь и факт успеха.

    `scopes` — что просим. По умолчанию оба права: загрузку и правку уже
    загруженного. Сузить до одной загрузки можно явно — тогда всё, что делается
    с роликом после, останется ручным действием в Studio."""
    import http.server
    import secrets as secrets_mod
    import urllib.parse
    import webbrowser

    state = secrets_mod.token_urlsafe(16)
    caught: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
            query = urllib.parse.urlparse(self.path).query
            params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
            if "code" in params or "error" in params:
                caught.update(params)
                head = "Готово" if "code" in params else "Не вышло"
                body = (
                    "Вкладку можно закрыть, дальше видно в терминале."
                    if "code" in params
                    else f"Google ответил: {params.get('error')}"
                )
            else:  # браузер заодно просит /favicon.ico — это не ответ Google
                head, body = "Жду ответа Google", "Не закрывай эту вкладку."
            page = AUTH_PAGE.format(head=head, body=body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args: Any) -> None:  # тишина: свой лог у нас есть
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 5.0
    redirect = f"http://127.0.0.1:{server.server_address[1]}/"
    url = (
        AUTH_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": " ".join(scopes),
                # offline + consent — единственный способ получить refresh-токен:
                # без них Google отдаёт только часовой access и молча, без ошибки
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
    )
    log_fn(f"открываю браузер; если не открылся — вставь адрес сам:\n{url}")
    if open_browser:
        webbrowser.open(url)
    waited = 0.0
    while not caught and waited < timeout_sec:
        server.handle_request()
        waited += server.timeout
    server.server_close()
    if not caught:
        raise ProviderError(f"Google не ответил за {timeout_sec:.0f} с — попробуй ещё раз")
    if caught.get("error"):
        raise ProviderError(f"вход отклонён: {caught['error']}")
    if caught.get("state") != state:
        raise ProviderError("ответ пришёл с чужим state — вход не засчитан")

    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": caught["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise ProviderError(f"Google не отдал токен ({resp.status_code}): {resp.text[:200]}")
    refresh = str(resp.json().get("refresh_token") or "")
    if not refresh:
        raise ProviderError(
            "Google вернул доступ без refresh-токена. Обычно это значит, что "
            "приложению уже дали доступ раньше: отзови его на "
            "myaccount.google.com/permissions и повтори вход"
        )
    return save_token(account_id, client_id, client_secret, refresh)


def save_token(account_id: str, client_id: str, client_secret: str, refresh: str) -> Path:
    path = token_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh},
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load_token(account_id: str) -> dict[str, str]:
    path = token_path(account_id)
    if not path.exists():
        old = legacy_token_path()
        if old.exists():
            raise ProviderError(
                f"нет токена аккаунта {account_id}: {path}. Есть общий {old} из прежней "
                f"раскладки — если он от этого канала, перенеси его в {path.parent}; "
                f"если не уверен — videoforge account login {account_id}"
            )
        raise ProviderError(
            f"нет токена YouTube у аккаунта {account_id}: {path}. Войти — videoforge account "
            f"login {account_id} --client-json <файл>; как получить клиент — docs/PUBLISHING.md "
            f"(права {', '.join(s.rsplit('/', 1)[-1] for s in SCOPES)})"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderError(f"{path} не читается: {exc}") from exc
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not data.get(k)]
    if missing:
        raise ProviderError(f"{path}: не хватает полей {', '.join(missing)}")
    return {k: str(v) for k, v in data.items()}


class YoutubeUpload(PublishProvider):
    name = "youtube_api"
    platforms = ("youtube",)

    def __init__(self, account_id: str) -> None:
        # токен лежит у аккаунта, поэтому и провайдер строится под аккаунт:
        # без него не «грузим куда-нибудь», а грузить нечем
        if not account_id:
            raise ProviderError(
                "YouTube грузит от имени аккаунта, а аккаунта youtube у проекта нет: "
                "videoforge account new <id> --network youtube --project <проект>"
            )
        self.account_id = account_id
        self._token: str = ""
        # что Google дал этому токену. Пусто — не спрашивали или он промолчал;
        # пустое НЕ значит «прав нет», иначе мы бы запрещали то, что разрешено
        self.granted: frozenset[str] = frozenset()

    # ------------------------------------------------------------ доступ

    def access_token(self) -> str:
        """Свежий access-токен по refresh-токену. Живёт час, кэшируем на вызов."""
        if self._token:
            return self._token
        creds = load_token(self.account_id)
        resp = httpx.post(
            TOKEN_URL,
            data={
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": creds["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=60.0,
        )
        if resp.status_code != 200:
            # ничего ещё не отправляли — исход известен
            raise Rejected(
                f"обновить токен YouTube не удалось ({resp.status_code}): {_error(resp)}"
            )
        data = resp.json()
        token = str(data.get("access_token") or "")
        if not token:
            raise Rejected("ответ Google без access_token")
        self._token = token
        self.granted = frozenset(str(data.get("scope") or "").split())
        return token

    def manages(self) -> bool:
        """Даёт ли токен править уже загруженное.

        Ответ утвердительный и при неизвестных правах: запрещать по незнанию
        значит выдумать отказ там, где площадка разрешает. Настоящий отказ
        придёт от неё, и `_denied` превратит его в понятную строку."""
        self.access_token()  # обновление токена заодно приносит список прав
        return not self.granted or SCOPE_MANAGE in self.granted

    # ------------------------------------------------------------ загрузка

    def upload(self, video_path: Path, meta: PublishMeta) -> PublishResult:
        # проверки ДО отправки поднимают `Rejected`: файл не ушёл, и это точно
        if not video_path.exists():
            raise Rejected(f"файла нет: {video_path}")
        if meta.privacy not in PRIVACY:
            raise Rejected(f"privacy {meta.privacy!r}; допустимы: {', '.join(sorted(PRIVACY))}")
        body = self.request_body(meta)
        warnings: list[str] = []
        token = self.access_token()
        size = video_path.stat().st_size
        try:
            session = self._start(token, body, video_path, size)
        except _SyntheticFieldRejected:
            # поле пометки API не принял: грузим без него и говорим человеку,
            # что галочку надо поставить руками — молчать здесь нельзя
            warnings.append(
                "API не принял пометку синтетического контента "
                "(status.containsSyntheticMedia): поставь галочку «изменённый или "
                "синтетический контент» в YouTube Studio сразу после загрузки"
            )
            body["status"].pop("containsSyntheticMedia", None)
            session = self._start(token, body, video_path, size)
        data = self._send(token, session, video_path, size)
        vid = str(data.get("id") or "")
        if not vid:
            raise ProviderError(f"YouTube не вернул id ролика: {data}")
        cover = ""
        if meta.thumbnail:
            # Обложка — отдельный вызов уже после загрузки, и её провал не
            # отменяет уехавший ролик: ролик есть, обложка — предупреждение
            try:
                self.set_thumbnail(vid, Path(meta.thumbnail), token=token)
                cover = "done"
            except (ProviderError, OSError) as exc:
                warnings.append(f"обложку поставить не вышло ({exc}) — поставь в Studio")
        return PublishResult(
            platform="youtube",
            state="done",
            video_id=vid,
            url=f"https://www.youtube.com/watch?v={vid}",
            cost_usd=0.0,  # платим квотой (1600 единиц), не деньгами
            request_id=vid,
            warnings=warnings,
            # `thumbnail` — принята ли обложка. Отдельным полем, а не по тексту
            # предупреждения: разбирать свои же сообщения — способ однажды
            # разойтись с ними молча
            meta={"privacy": meta.privacy, "quota_units": 1600, "thumbnail": cover},
        )

    def set_thumbnail(self, video_id: str, image: Path, *, token: str = "") -> None:
        """Своя обложка ролика (`thumbnails.set`, 50 единиц квоты).

        Без неё площадка берёт кадр сама, и на шортсе это чаще всего середина
        слова с открытым ртом. Тот же scope `youtube.upload`, отдельный вызов:
        API принимает картинку только к уже существующему ролику."""
        if not image.exists():
            raise ProviderError(f"обложки нет: {image}")
        mime = mimetypes.guess_type(image.name)[0] or "image/jpeg"
        resp = httpx.post(
            THUMBNAIL_URL,
            params={"videoId": video_id, "uploadType": "media"},
            headers={
                "Authorization": f"Bearer {token or self.access_token()}",
                "Content-Type": mime,
            },
            content=image.read_bytes(),
            timeout=TIMEOUT,
        )
        if resp.status_code >= 400:
            raise ProviderError(f"thumbnails.set {resp.status_code}: {resp.text[:200]}")

    def request_body(self, meta: PublishMeta) -> dict[str, Any]:
        """Тело `videos.insert`. Отдельным методом — его проверяет тест и читает
        человек, которому важно, что именно уходит вместе с файлом."""
        tags: list[str] = []
        used = 0
        for tag in meta.hashtags:
            clean = tag.lstrip("#").strip()
            if not clean or used + len(clean) > TAGS_MAX:
                continue
            tags.append(clean)
            used += len(clean) + 1
        return {
            "snippet": {
                "title": meta.title[:TITLE_MAX],
                "description": meta.tagged_description()[:DESCRIPTION_MAX],
                "tags": tags,
                # два разных языка, и площадка различает их: по первому она
                # решает, кому показывать ролик в поиске и рекомендациях, по
                # второму — какие автоперево­ды субтитров предлагать
                "defaultLanguage": meta.text_language or meta.language,
                "defaultAudioLanguage": meta.language,
                # раздел объявляет проект (`channel.category`); не объявлен —
                # People & Blogs, нейтральная полка, меняется в Studio
                "categoryId": meta.category or DEFAULT_CATEGORY,
            },
            "status": {
                "privacyStatus": meta.privacy,
                # «сделано для детей» ставится осознанно: неверное значение —
                # претензия площадки, а не мелочь
                "selfDeclaredMadeForKids": meta.made_for_kids,
                "containsSyntheticMedia": meta.synthetic,
            },
        }

    def _start(self, token: str, body: dict[str, Any], video: Path, size: int) -> str:
        """Открыть resumable-сессию: ответ — URL, по которому льются байты.

        Байты ещё не пошли, поэтому любой исход здесь известен: отказ площадки
        — `Rejected`, не дозвонились — тоже `Rejected` (ролика на канале нет)."""
        mime = mimetypes.guess_type(video.name)[0] or "video/mp4"
        try:
            resp = httpx.post(
                UPLOAD_URL,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Length": str(size),
                    "X-Upload-Content-Type": mime,
                },
                json=body,
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise Rejected(f"сессия загрузки не открылась: {exc}") from exc
        if resp.status_code == 400 and "containsSyntheticMedia" in resp.text:
            raise _SyntheticFieldRejected
        if resp.status_code not in (200, 201):
            raise Rejected(f"YouTube отказал в загрузке ({resp.status_code}): {_error(resp)}")
        location = resp.headers.get("location") or resp.headers.get("Location")
        if not location:
            raise Rejected("YouTube не вернул адрес сессии загрузки (Location)")
        return location

    def _send(self, token: str, session: str, video: Path, size: int) -> dict[str, Any]:
        """Байты кусками по 8 МБ. Обрыв в середине — не потеря: адрес сессии
        живёт неделю, и продолжить можно с того же места (Content-Range).

        Здесь исход уже НЕ всегда известен, и типы исключений это различают.
        Ответ с кодом ошибки — `Rejected`: ролик создаётся только по окончании
        заливки, и отказ на куске означает, что его нет. Оборванное соединение
        или таймаут — обычный `ProviderError`, то есть «ответа не было»:
        последний кусок мог быть принят, а ответ потерян, и повторять вслепую
        нельзя."""
        sent = 0
        with httpx.Client(timeout=TIMEOUT) as client, video.open("rb") as fh:
            while sent < size:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                end = sent + len(chunk) - 1
                try:
                    resp = client.put(
                        session,
                        content=chunk,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {sent}-{end}/{size}",
                        },
                    )
                except httpx.HTTPError as exc:
                    raise ProviderError(
                        f"связь оборвалась на {sent}/{size} байт ({exc}) — ответа не было"
                    ) from exc
                if resp.status_code in (200, 201):
                    return dict(resp.json())
                if resp.status_code != 308:  # 308 — «кусок принят, шли дальше»
                    raise Rejected(
                        f"загрузка отклонена на {sent}/{size} байт "
                        f"({resp.status_code}): {_error(resp)}"
                    )
                sent = end + 1
                log.info("YouTube: залито %.0f%%", 100 * sent / max(size, 1))
        raise ProviderError("файл кончился, а YouTube не подтвердил загрузку")

    # ------------------------------------------------------------ правка

    def describe(self, video_id: str) -> dict[str, str]:
        """Что лежит на канале прямо сейчас: заголовок, описание, видимость,
        языки, ход обработки.

        Плоскими строками, потому что это читает человек в сухом прогоне
        правки. И это же первая половина самой правки: менять поля можно
        только поверх прочитанного."""
        item = self._video(video_id)
        snippet = item.get("snippet") or {}
        status = item.get("status") or {}
        return {
            "title": str(snippet.get("title") or ""),
            "description": str(snippet.get("description") or ""),
            "tags": ", ".join(str(t) for t in (snippet.get("tags") or [])),
            "language": str(snippet.get("defaultAudioLanguage") or ""),
            "text_language": str(snippet.get("defaultLanguage") or ""),
            "category": str(snippet.get("categoryId") or ""),
            "privacy": str(status.get("privacyStatus") or ""),
            "upload_status": str(status.get("uploadStatus") or ""),
            "processing": str((item.get("processingDetails") or {}).get("processingStatus") or ""),
        }

    def update(self, video_id: str, meta: PublishMeta) -> PublishResult:
        """Привести уже опубликованный ролик к упаковке. Файл не перезагружается.

        Читаем — сливаем — пишем. `videos.update` заменяет часть целиком, а не
        сливает поля: «поменять видимость» на языке API означает «прислать
        status целиком», и прислать один `privacyStatus` значит обнулить
        лицензию, встраивание и показ статистики. Поверх прочитанного ложится
        только то, чем распоряжаются наши файлы."""
        if meta.privacy not in PRIVACY:
            raise ProviderError(
                f"privacy {meta.privacy!r}; допустимы: {', '.join(sorted(PRIVACY))}"
            )
        token = self.access_token()
        item = self._video(video_id)
        snippet = {k: v for k, v in (item.get("snippet") or {}).items() if k in CARRY_SNIPPET}
        status = {k: v for k, v in (item.get("status") or {}).items() if k in CARRY_STATUS}
        fresh = self.request_body(meta)
        # раздел правка навязывает, ТОЛЬКО если проект его объявил: это решение,
        # записанное в файле, и файл — источник правды. Не объявлен — в `insert`
        # уходила нейтральная заглушка, а в Studio человек мог выбрать осознанно,
        # и затирать его выбор нечем: своего мнения у нас в этом случае нет
        if snippet.get("categoryId") and not meta.category:
            fresh["snippet"].pop("categoryId", None)
        snippet.update(fresh["snippet"])
        status.update(fresh["status"])
        warnings: list[str] = []
        try:
            self._write(token, video_id, snippet, status)
        except _SyntheticFieldRejected:
            # пометка ИИ — поле write-only: прочитать её нельзя, а промолчать о
            # ней в правке значит снять её молча. Не принимают — говорим человеку
            status.pop("containsSyntheticMedia", None)
            self._write(token, video_id, snippet, status)
            warnings.append(
                "API не принял пометку синтетического контента: проверь галочку "
                "«изменённый или синтетический контент» в YouTube Studio"
            )
        units = 50
        cover = ""
        if meta.thumbnail:
            try:
                self.set_thumbnail(video_id, Path(meta.thumbnail), token=token)
                cover = "done"
                units += 50
            except (ProviderError, OSError) as exc:
                warnings.append(f"обложку поставить не вышло ({exc}) — поставь в Studio")
        return PublishResult(
            platform="youtube",
            state="done",
            video_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            cost_usd=0.0,
            request_id=video_id,
            warnings=warnings,
            meta={"privacy": meta.privacy, "quota_units": units, "thumbnail": cover},
        )

    def _video(self, video_id: str) -> dict[str, Any]:
        """Ролик целиком, как его отдаёт `videos.list` (50 единиц квоты)."""
        resp = httpx.get(
            VIDEOS_URL,
            params={"part": "snippet,status,processingDetails", "id": video_id},
            headers={"Authorization": f"Bearer {self.access_token()}"},
            timeout=60.0,
        )
        if resp.status_code >= 400:
            raise ProviderError(f"ролик {video_id} не прочитан: {_denied(resp)}")
        items = resp.json().get("items") or []
        if not items:
            raise ProviderError(f"ролика {video_id} на канале нет — или он не этого аккаунта")
        return dict(items[0])

    def _write(
        self, token: str, video_id: str, snippet: dict[str, Any], status: dict[str, Any]
    ) -> None:
        resp = httpx.put(
            VIDEOS_URL,
            params={"part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={"id": video_id, "snippet": snippet, "status": status},
            timeout=60.0,
        )
        if resp.status_code == 400 and "containsSyntheticMedia" in resp.text:
            raise _SyntheticFieldRejected
        if resp.status_code >= 400:
            raise ProviderError(f"правка не прошла ({resp.status_code}): {_denied(resp)}")

    # ------------------------------------------------------------ состояние

    def check_status(self, request_id: str) -> PublishResult:
        """Что площадка сделала с роликом после загрузки (`videos.list`, 1
        единица квоты): принят, обрабатывается, обработан, отклонён — и почему.
        `request_id` здесь — id ролика. Только чтение.

        Части: `status` — uploadStatus, privacyStatus и причины отказа
        (rejectionReason: copyright, claim, duplicate, …; failureReason: codec,
        conversion, …), `processingDetails` — processingStatus и
        processingFailureReason, `contentDetails` — где ролик заблокирован
        (regionRestriction). Претензий Content ID здесь нет ни в одной части —
        см. CONTENT_ID_NOTE. Разбор ответа — `video_status`, без сети."""
        try:
            resp = httpx.get(
                VIDEOS_URL,
                params={"part": "status,processingDetails,contentDetails", "id": request_id},
                headers={"Authorization": f"Bearer {self.access_token()}"},
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"состояние ролика {request_id} не прочитано: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"состояние ролика {request_id} не прочитано: {_denied(resp)}")
        items = resp.json().get("items") or []
        if not items:
            # пустой список — не ошибка API, а ответ: ролика с таким id у этого
            # аккаунта нет (удалён, или id от другого канала)
            return PublishResult(
                platform="youtube",
                state="failed",
                video_id=request_id,
                request_id=request_id,
                meta={"found": False, "quota_units": 1, "notes": [CONTENT_ID_NOTE]},
            )
        return video_status(request_id, dict(items[0]))

    def find_video(self, title: str, *, since: datetime) -> list[PublishResult]:
        """Свои ролики с таким названием, загруженные после `since`: плейлист
        загрузок канала (`channels.list` + `playlistItems.list`, по 1 единице).

        Не `search.list`: тот стоит 100 единиц и индексируется с опозданием —
        свежий ролик он ещё не знает, а ищут здесь именно свежий. Плейлист
        загрузок владельцу отдаёт и приватные ролики, а отдан он им как раз
        приватными. Разбор — `match_uploads`, без сети."""
        token = self.access_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = httpx.get(
                CHANNELS_URL,
                params={"part": "contentDetails", "mine": "true"},
                headers=headers,
                timeout=60.0,
            )
            if resp.status_code >= 400:
                raise ProviderError(f"плейлист загрузок не прочитан: {_denied(resp)}")
            channels = resp.json().get("items") or []
            details = (channels[0].get("contentDetails") or {}) if channels else {}
            uploads = str((details.get("relatedPlaylists") or {}).get("uploads") or "")
            if not uploads:
                raise ProviderError("channels.list: у токена нет своего канала")
            resp = httpx.get(
                PLAYLIST_ITEMS_URL,
                params={
                    "part": "snippet,contentDetails",
                    "playlistId": uploads,
                    "maxResults": UPLOADS_PAGE,
                },
                headers=headers,
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"загрузки канала не прочитаны: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"загрузки канала не прочитаны: {_denied(resp)}")
        return match_uploads(list(resp.json().get("items") or []), title, since=since)

    # ------------------------------------------------------------ статистика

    def analytics(self, *, days: int = ANALYTICS_DAYS) -> AnalyticsSnapshot:
        """Официальная статистика канала: окно последних `days` дней, то же по
        дням и итоги канала. Права — из ответа Google на обновление токена,
        а не из нашей записи о том, чего просили."""
        try:
            token = self.access_token()
        except httpx.HTTPError as exc:
            raise ProviderError(f"обновить токен YouTube не вышло: {exc}") from exc
        return analytics_snapshot(token, granted=self.granted, days=days)

    def video_analytics(self, video_id: str) -> VideoAnalytics:
        """Один ролик за всё время: итоги, кривая удержания, источники."""
        try:
            token = self.access_token()
        except httpx.HTTPError as exc:
            raise ProviderError(f"обновить токен YouTube не вышло: {exc}") from exc
        return video_analytics(token, video_id)


def video_status(video_id: str, item: dict[str, Any]) -> PublishResult:
    """Ролик из `videos.list` → что с ним. Отдельной функцией: сети здесь не
    нужно, а разбор ответа проверяет тест на записанных ответах площадки.

    В `meta` — слова площадки как есть (`upload_status`, `processing`,
    `reason`, `privacy`), итог по ним — `landed_state`. Причина одна: отказ
    загрузки, провал загрузки и провал обработки взаимоисключающие, и первая
    непустая — та самая."""
    status = item.get("status") or {}
    processing = item.get("processingDetails") or {}
    content = item.get("contentDetails") or {}
    upload = str(status.get("uploadStatus") or "")
    stage = str(processing.get("processingStatus") or "")
    reason = str(
        status.get("rejectionReason")
        or status.get("failureReason")
        or processing.get("processingFailureReason")
        or ""
    )
    notes = [CONTENT_ID_NOTE]
    region = content.get("regionRestriction") or {}
    if region.get("blocked"):
        notes.append(f"заблокирован в странах: {', '.join(str(c) for c in region['blocked'])}")
    elif region.get("allowed"):
        notes.append(f"доступен только в: {', '.join(str(c) for c in region['allowed'])}")
    progress = processing.get("processingProgress") or {}
    if stage == "processing" and progress.get("partsTotal"):
        notes.append(
            f"обработано частей: {progress.get('partsProcessed', 0)}/{progress['partsTotal']}"
        )
    return PublishResult(
        platform="youtube",
        state=landed_state(upload, stage),
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        request_id=video_id,
        meta={
            "found": True,
            "upload_status": upload,
            "processing": stage,
            "reason": reason,
            "privacy": str(status.get("privacyStatus") or ""),
            "quota_units": 1,
            "notes": notes,
        },
    )


def _same_title(a: str, b: str) -> bool:
    """Одно ли это название. Наше режется до TITLE_MAX ещё в `request_body`, а
    площадка схлопывает пробелы — сравнивать надо после того же."""

    def norm(value: str) -> str:
        return " ".join(value[:TITLE_MAX].split())

    return norm(a) == norm(b)


def _parse_time(value: Any) -> datetime | None:
    """`2026-09-13T07:00:45Z` площадки → datetime с зоной; мусор — None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def match_uploads(
    items: list[dict[str, Any]], title: str, *, since: datetime
) -> list[PublishResult]:
    """Элементы плейлиста загрузок → те, что с нашим названием и не старше
    `since` (с запасом FIND_SLACK). `publishedAt` элемента плейлиста загрузок —
    момент загрузки, и у приватного ролика он есть, в отличие от
    `videoPublishedAt`. Без даты — тоже подходит: отбросить ролик, не зная
    когда он загружен, значило бы спрятать удачную загрузку."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    out: list[PublishResult] = []
    for item in items:
        snippet = item.get("snippet") or {}
        vid = str(
            (item.get("contentDetails") or {}).get("videoId")
            or (snippet.get("resourceId") or {}).get("videoId")
            or ""
        )
        if not vid or not _same_title(str(snippet.get("title") or ""), title):
            continue
        at = _parse_time(snippet.get("publishedAt"))
        if at is not None and at < since - FIND_SLACK:
            continue
        out.append(
            PublishResult(
                platform="youtube",
                state="done",
                video_id=vid,
                url=f"https://www.youtube.com/watch?v={vid}",
                request_id=vid,
                meta={"title": str(snippet.get("title") or ""), "at": at},
            )
        )
    return out


class _SyntheticFieldRejected(Exception):
    """API не знает поля пометки ИИ — грузим без него и предупреждаем человека."""


def _denied(resp: httpx.Response) -> str:
    """То же сообщение, но на отказ по правам — ещё и что с этим делать.

    Голый «403: Request had insufficient authentication scopes» заставляет
    человека гадать, что он сделал не так, хотя ответ один и тот же."""
    message = f"{resp.status_code}: {_error(resp)}"
    if resp.status_code == 403 and "SCOPE" in resp.text.upper():
        return f"{message} — прав у токена не хватает, {RELOGIN}"
    return message


def _error(resp: httpx.Response) -> str:
    """Сообщение Google из ответа, без токенов и заголовков."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:300]
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or payload)[:300]


# ----------------------------------------------------------------- статистика

# Метрика Analytics API → поле снимка. Ответ разбирается по `columnHeaders`,
# а не по позиции: порядок колонок задаёт запрос, но сверять по именам дешевле,
# чем однажды перепутать лайки с комментариями
ANALYTICS_METRICS: dict[str, str] = {
    "views": "views",
    "estimatedMinutesWatched": "watch_minutes",
    "averageViewDuration": "avg_view_sec",
    "averageViewPercentage": "avg_view_pct",
    "subscribersGained": "subs_gained",
    "subscribersLost": "subs_lost",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
}
MONEY_METRIC = "estimatedRevenue"  # → revenue_usd; отдельное право SCOPE_MONEY
_INT_FIELDS = frozenset({"views", "subs_gained", "subs_lost", "likes", "comments", "shares"})


@dataclass
class AnalyticsRow:
    """Числа за период — окно целиком или один день. None — площадка не
    отдала; нуль на его месте был бы враньём в графике."""

    views: int | None = None
    watch_minutes: float | None = None
    avg_view_sec: float | None = None
    avg_view_pct: float | None = None
    subs_gained: int | None = None
    subs_lost: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    revenue_usd: float | None = None


@dataclass
class AnalyticsSnapshot:
    """Что отдал YouTube по токену: окно [start, end] целиком, то же по дням
    и итоги канала «за всё время» (`channels.list`). Итоги — отдельные поля,
    потому что Analytics считает только окна, а без них ряд youtube_api не с
    чем сравнить с тем, что снимал yt-dlp."""

    start: date
    end: date
    totals: AnalyticsRow
    days: list[tuple[date, AnalyticsRow]]
    settled_until: date  # дни по эту дату включительно площадка досчитала
    subscribers: int | None = None
    views_total: int | None = None
    video_count: int | None = None
    granted: frozenset[str] = frozenset()
    warnings: list[str] = field(default_factory=list)

    @property
    def window_days(self) -> int:
        return (self.end - self.start).days + 1


def parse_report(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """`columnHeaders` + `rows` отчёта → словари {имя колонки: значение}."""
    headers = [str(h.get("name") or "") for h in payload.get("columnHeaders") or []]
    return [dict(zip(headers, row, strict=False)) for row in payload.get("rows") or []]


def analytics_row(record: dict[str, Any]) -> AnalyticsRow:
    """Строка отчёта → числа снимка. Чего в строке нет — None, не нуль."""
    values: dict[str, Any] = {}
    for metric, name in {**ANALYTICS_METRICS, MONEY_METRIC: "revenue_usd"}.items():
        raw = record.get(metric)
        if raw is None:
            values[name] = None
        elif name in _INT_FIELDS:
            values[name] = int(round(float(raw)))
        else:
            values[name] = float(raw)
    return AnalyticsRow(**values)


def analytics_snapshot(
    token: str,
    *,
    granted: frozenset[str] = frozenset(),
    days: int = ANALYTICS_DAYS,
    today: date | None = None,
) -> AnalyticsSnapshot:
    """Официальная статистика канала по access-токену: окно последних `days`
    дней по вчера включительно, то же по дням, итоги канала.

    Доход (`estimatedRevenue`) спрашивается, когда токен даёт на него право —
    или когда права неизвестны, по тому же правилу, что `manages()`: запрет по
    незнанию выдумал бы отказ там, где площадка разрешает. Отказ именно по
    доходу (канал не в партнёрке, право отозвано) снимок не роняет: доход
    остаётся None, причина уходит в `warnings`. Отказ по остальному — ошибка:
    без просмотров снимок пуст, и писать его незачем.

    Три-четыре запроса: окно, окно по дням, при отказе по доходу — окно ещё
    раз без него, и `channels.list` (1 единица Data API)."""
    if days < 1:
        raise ValueError(f"окно статистики — хотя бы один день, получено {days}")
    today = today or datetime.now(UTC).date()
    start, end = today - timedelta(days=days), today - timedelta(days=1)
    warnings: list[str] = []
    metrics = list(ANALYTICS_METRICS)
    if not granted or SCOPE_MONEY in granted:
        metrics.append(MONEY_METRIC)
    resp = _report(token, start=start, end=end, metrics=metrics, by_day=False)
    if resp.status_code >= 400 and MONEY_METRIC in metrics:
        # доход спрашивался вместе со всем, и отказ может быть только про него:
        # повтор без него говорит, про него ли
        warnings.append(f"доход не отдан ({_denied(resp)}) — revenue_usd пуст")
        metrics.remove(MONEY_METRIC)
        resp = _report(token, start=start, end=end, metrics=metrics, by_day=False)
    if resp.status_code >= 400:
        raise ProviderError(f"Analytics API отказал: {_denied(resp)}")
    rows = parse_report(resp.json())
    totals = analytics_row(rows[0]) if rows else AnalyticsRow()
    if not rows:
        warnings.append(f"за {start}…{end} площадка не отдала ни строки")
    daily = _report(token, start=start, end=end, metrics=metrics, by_day=True)
    if daily.status_code >= 400:
        raise ProviderError(f"Analytics API отказал по дням: {_denied(daily)}")
    by_day = [
        (date.fromisoformat(str(r["day"])), analytics_row(r))
        for r in parse_report(daily.json())
        if r.get("day")
    ]
    snap = AnalyticsSnapshot(
        start=start,
        end=end,
        totals=totals,
        days=by_day,
        settled_until=today - timedelta(days=ANALYTICS_LAG_DAYS),
        granted=granted,
        warnings=warnings,
    )
    _channel_totals(token, snap)
    return snap


def _report(
    token: str, *, start: date, end: date, metrics: Sequence[str], by_day: bool
) -> httpx.Response:
    params = {
        "ids": "channel==MINE",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "metrics": ",".join(metrics),
    }
    if by_day:
        params["dimensions"] = "day"
        params["sort"] = "day"
    try:
        return httpx.get(
            ANALYTICS_URL,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        raise ProviderError(f"Analytics API не ответил: {exc}") from exc


# ------------------------------------------------------------- по одному ролику

# Самое раннее, откуда спрашивать статистику ролика «за всё время»: у Analytics
# нет «с публикации», окно задаётся датами, а даты раньше канала — законны
VIDEO_SINCE = date(2020, 1, 1)


@dataclass
class VideoAnalytics:
    """Что YouTube отдал по одному ролику за всё время.

    `retention` — кривая удержания: 100 точек `(доля ролика, доля зрителей,
    относительно похожих роликов)`. Доля зрителей бывает больше 1 — это
    пересмотры куска. Кривая пустая, пока устоявшихся просмотров мало: площадка
    её не считает, и это «ещё нет», а не «все ушли». `traffic` — источник →
    (просмотры, минуты): отличает «ролик плохой» от «ролик никому не показали»."""

    video_id: str
    start: date
    end: date
    totals: AnalyticsRow
    retention: list[tuple[float, float, float | None]]
    traffic: dict[str, tuple[int, float]]


def video_analytics(token: str, video_id: str, *, today: date | None = None) -> VideoAnalytics:
    """Три запроса: итоги ролика, кривая удержания, источники трафика. Доход не
    спрашивается: по ролику он не нужен, а отказ по нему ронял бы остальное."""
    today = today or datetime.now(UTC).date()
    start, end = VIDEO_SINCE, today
    base = {
        "ids": "channel==MINE",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "filters": f"video=={video_id}",
    }
    totals = _video_report(token, {**base, "metrics": ",".join(ANALYTICS_METRICS)})
    curve = _video_report(
        token,
        {
            **base,
            "dimensions": "elapsedVideoTimeRatio",
            "metrics": "audienceWatchRatio,relativeRetentionPerformance",
        },
    )
    sources = _video_report(
        token,
        {
            **base,
            "dimensions": "insightTrafficSourceType",
            "metrics": "views,estimatedMinutesWatched",
        },
    )
    return VideoAnalytics(
        video_id=video_id,
        start=start,
        end=end,
        totals=analytics_row(totals[0]) if totals else AnalyticsRow(),
        retention=[
            (
                float(r["elapsedVideoTimeRatio"]),
                float(r["audienceWatchRatio"]),
                None
                if r.get("relativeRetentionPerformance") is None
                else float(r["relativeRetentionPerformance"]),
            )
            for r in curve
        ],
        traffic={
            str(r["insightTrafficSourceType"]): (
                int(round(float(r.get("views") or 0))),
                float(r.get("estimatedMinutesWatched") or 0),
            )
            for r in sources
        },
    )


def _video_report(token: str, params: dict[str, str]) -> list[dict[str, Any]]:
    try:
        resp = httpx.get(
            ANALYTICS_URL, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=60.0
        )
    except httpx.HTTPError as exc:
        raise ProviderError(f"Analytics API не ответил: {exc}") from exc
    if resp.status_code >= 400:
        raise ProviderError(f"Analytics API отказал по ролику: {_denied(resp)}")
    return parse_report(resp.json())


def _channel_totals(token: str, snap: AnalyticsSnapshot) -> None:
    """Подписчики, просмотры и число роликов «за всё время» — `channels.list`
    (Data API, 1 единица). Отказ — предупреждение, не ошибка: окно важнее
    итогов, а итоги есть и у yt-dlp."""
    try:
        resp = httpx.get(
            CHANNELS_URL,
            params={"part": "statistics", "mine": "true"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        snap.warnings.append(f"итоги канала не прочитаны ({exc})")
        return
    if resp.status_code >= 400:
        snap.warnings.append(f"итоги канала не отданы ({_denied(resp)})")
        return
    items = resp.json().get("items") or []
    if not items:
        snap.warnings.append("channels.list: у токена нет своего канала")
        return
    stats = items[0].get("statistics") or {}
    # скрытый счётчик подписчиков — это None, а не нуль: площадка его прячет
    hidden = str(stats.get("hiddenSubscriberCount", "")).lower() == "true"
    snap.subscribers = None if hidden else _int(stats.get("subscriberCount"))
    snap.views_total = _int(stats.get("viewCount"))
    snap.video_count = _int(stats.get("videoCount"))


def _int(value: Any) -> int | None:
    """Число из ответа Google, где счётчики приходят строками ("18")."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
