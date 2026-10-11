"""Словарь произношения проекта: голосу — удобное написание, субтитрам — правильное.

`projects/<p>/pronunciation.yaml`, блок `words`: «как пишем → как отдаём голосу».
Часть имён и терминов TTS читает неверно, и лечится это написанием («Ка-рим»,
«ха-дис» — замер `research/_probes/pronunciation_2026-09-18`), но субтитры
строятся из слов, которые вернул сервис, и на экране появилось бы «Ка-рим».
Поэтому замена живёт только на пути к голосу: стадия озвучки подменяет слова в
тексте для TTS, а в словах `.srt` возвращает прежнее написание.

Слово в словаре — точная форма («Амир» не трогает «Амира»): голос ошибается не
во всех формах, и замена наугад сломала бы те, что звучали верно. Словарь
привязан к голосу проекта: у другого голоса ошибки другие, его перемеряют.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

PRONUNCIATION_FILE = "pronunciation.yaml"
# то, что сервис и сценарий лепят к слову: при сверке слов .srt снимается
_EDGES = " \t\n.,;:!?…«»\"'()—–"


def load(project_root: Path | None) -> dict[str, str]:
    """Словарь проекта; нет файла или проекта — пусто."""
    if project_root is None:
        return {}
    path = project_root / PRONUNCIATION_FILE
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    words = data.get("words") or {}
    return {str(k): str(v) for k, v in words.items() if str(k).strip() and str(v).strip()}


def _pattern(written: str) -> re.Pattern[str]:
    # границы слова с дефисом: «Амир» внутри «Амир-хана» не заменяется
    return re.compile(rf"(?<![\w-]){re.escape(written)}(?![\w-])")


def used(text: str, words: dict[str, str]) -> dict[str, str]:
    """Записи словаря, которые есть в этом тексте, — они и входят в отпечаток
    озвучки: новое слово в словаре не должно перегонять сцены без него."""
    return {k: v for k, v in sorted(words.items()) if _pattern(k).search(text)}


def spoken(text: str, words: dict[str, str]) -> str:
    """Текст, каким его получает голос. Длинные записи — первыми: «ин ша Аллах»
    раньше, чем «Аллах»."""
    for written in sorted(words, key=len, reverse=True):
        text = _pattern(written).sub(words[written], text)
    return text


def restore_map(words: dict[str, str]) -> dict[str, str]:
    """«Как прозвучало → как пишем» по одному слову: сервис возвращает слова
    по отдельности. Запись, где число слов слева и справа разное, обратно не
    разложить — она остаётся как прозвучала, и это видно в субтитрах."""
    back: dict[str, str] = {}
    for written, said in words.items():
        left, right = written.split(), said.split()
        if len(left) != len(right):
            continue
        for w, s in zip(left, right, strict=True):
            if w != s:
                back[s.strip(_EDGES)] = w.strip(_EDGES)
    return back


def restore(word: str, back: dict[str, str]) -> str:
    """Слово из .srt в написании сценария; знаки по краям остаются."""
    core = word.strip(_EDGES)
    if core not in back:
        return word
    start = word.index(core)
    return word[:start] + back[core] + word[start + len(core) :]
