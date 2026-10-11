"""Текст песни из LRCLIB — чтобы не просить его у человека.

Зачем. Распознавалка на пении путает слова: «homie» там, где поют другое.
Времена она даёт верные (свой звук), слова — нет. Раньше правильный текст
приносил человек руками, и это не инструмент, а ручная работа на каждый ролик.

LRCLIB отдаёт тексты без ключа и без регистрации, $0. Времена он тоже отдаёт, и
мы их НЕ БЕРЁМ: они от студийного релиза, а режут обычно живое выступление, где
та же строка звучит в другую секунду. Слова отсюда, секунды — из своего звука.

Не нашлось — нормальный ответ. Живая версия, редкий трек, свой микс: тогда
слова остаются на совести распознавания, и об этом надо сказать вслух, а не
молча выдать догадку за текст.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from videoforge.providers.base import LyricsProvider, LyricsResult, ProviderError

log = logging.getLogger("videoforge.providers")

API = "https://lrclib.net/api"
# сервис просит представляться — это условие бесплатного доступа, не формальность
AGENT = "videoforge"
TIMEOUT_SEC = 15
# «(live)», «[Official Video]», «feat. …» — пометки записи, а не название песни;
# по ним поиск не находит ничего, а без них находит
NOISE = re.compile(r"\s*[\(\[][^)\]]*[)\]]|\s*[-–—]\s*(live|official|remaster).*$", re.I)


def _get(path: str, params: dict[str, str]) -> object | None:
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # не нашлось — это ответ, а не сбой
        raise ProviderError(f"LRCLIB ответил {exc.code} на {path}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ProviderError(f"LRCLIB недоступен: {exc}") from exc


def _plain(row: object) -> str:
    if not isinstance(row, dict) or row.get("instrumental"):
        return ""
    return str(row.get("plainLyrics") or "").strip()


class LrcLib(LyricsProvider):
    name = "lrclib"

    def fetch(self, *, artist: str, track: str, duration_sec: float = 0.0) -> LyricsResult | None:
        clean = NOISE.sub("", track).strip()
        # точное совпадение с длительностью — для студийного релиза; у живой
        # версии длительность своя, поэтому дальше идёт поиск без неё
        attempts: list[tuple[str, dict[str, str]]] = []
        if duration_sec:
            attempts.append(
                (
                    "get",
                    {
                        "artist_name": artist,
                        "track_name": clean,
                        "duration": str(int(round(duration_sec))),
                    },
                )
            )
        attempts.append(("get", {"artist_name": artist, "track_name": clean}))
        attempts.append(("search", {"artist_name": artist, "track_name": clean}))
        for path, params in attempts:
            answer = _get(path, params)
            rows = answer if isinstance(answer, list) else [answer]
            for row in rows:
                text = _plain(row)
                if text:
                    found = row if isinstance(row, dict) else {}
                    where = f"{found.get('artistName', artist)} — {found.get('trackName', clean)}"
                    log.info("LRCLIB: текст найден (%s, %s)", path, where)
                    return LyricsResult(text=text, source=f"LRCLIB · {where}", cost_usd=0.0)
        log.info("LRCLIB: текста нет для %s — %s", artist, clean)
        return None
