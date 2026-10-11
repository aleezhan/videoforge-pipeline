"""Поиск картинки-референса на Wikimedia Commons: без ключа, с лицензией у
каждого файла.

Зачем именно Commons: у каждой картинки есть страница с лицензией и автором,
API открыт, ключей нет, и у выдачи есть размер — референс композиции нужен
около мегапикселя. Сеть здесь трогается только по делу стадии (кадр не совпал
с описанием), никаких фоновых запросов.

Commons просит называть себя в User-Agent; без него и с чужим агентом отвечает
403 — на это напоролся загрузчик Qwen в `_bench/test8`.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from videoforge.providers.base import ImageCandidate, ImageSearchProvider, ProviderError

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "videoforge"
TIMEOUT = 30.0
THUMB_WIDTH = 1024  # референс композиции: около мегапикселя, больше не нужно
EXTENSIONS = (".jpg", ".jpeg", ".png")


RETRIES = 3
BACKOFF_SEC = 5.0  # своя пауза: 5 с, потом 10 с; Retry-After от сервера побеждает
MAX_PAUSE_SEC = 30.0

# sleep — через модульную ссылку, чтобы тесты подменяли ожидание, не засыпая
_sleep = time.sleep


def _get(client: httpx.Client, params: dict[str, str | int]) -> dict:
    """GET к API с повтором на 429/503: ep003 (17.09.2026) словил 429 на
    одиннадцатом поиске подряд, и без повтора стадия падала с поднятой картой."""
    for attempt in range(1, RETRIES + 1):
        try:
            r = client.get(API, params={**params, "format": "json"})
            if r.status_code in (429, 503) and attempt < RETRIES:
                asked = r.headers.get("retry-after", "")
                delay = float(asked) if asked.isdigit() else BACKOFF_SEC * 2 ** (attempt - 1)
                _sleep(min(delay, MAX_PAUSE_SEC))
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"Wikimedia Commons: {type(exc).__name__}: {exc}") from exc
    raise AssertionError("unreachable")  # pragma: no cover


class CommonsSearch(ImageSearchProvider):
    name = "commons"

    def __init__(self) -> None:
        self._http: httpx.Client | None = None

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True
            )
        return self._http

    def search(self, query: str, *, limit: int = 8) -> list[ImageCandidate]:
        """Полнотекстовый поиск по файлам, потом сведения о найденных: ссылка на
        уменьшенную копию, размер, лицензия, страница источника."""
        found = _get(
            self.http,
            {
                "action": "query",
                "list": "search",
                "srnamespace": 6,
                "srlimit": max(limit * 3, 10),
                "srsearch": query,
            },
        )
        titles = [
            str(row.get("title", ""))
            for row in found.get("query", {}).get("search", [])
            if str(row.get("title", "")).lower().endswith(EXTENSIONS)
        ][: limit * 2]
        if not titles:
            return []
        info = _get(
            self.http,
            {
                "action": "query",
                "prop": "imageinfo",
                "iiprop": "url|size|extmetadata",
                "iiurlwidth": THUMB_WIDTH,
                "titles": "|".join(titles),
            },
        )
        pages = info.get("query", {}).get("pages", {})
        order = {t: i for i, t in enumerate(titles)}
        out: list[ImageCandidate] = []
        for page in pages.values():
            ii = (page.get("imageinfo") or [{}])[0]
            url = str(ii.get("thumburl") or ii.get("url") or "").split("?")[0]
            if not url:
                continue
            meta = ii.get("extmetadata") or {}
            out.append(
                ImageCandidate(
                    url=url,
                    title=str(page.get("title", "")),
                    page=str(ii.get("descriptionurl") or ""),
                    license=str((meta.get("LicenseShortName") or {}).get("value", "")),
                    width=int(ii.get("width") or 0),
                    height=int(ii.get("height") or 0),
                )
            )
        out.sort(key=lambda c: order.get(c.title, len(order)))
        return out[:limit]

    def fetch(self, candidate: ImageCandidate, out_path: Path) -> Path:
        try:
            r = self.http.get(candidate.url)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Wikimedia Commons, скачивание: {exc}") from exc
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(r.content)
        return out_path
