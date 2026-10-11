"""Чтение/запись pydantic-моделей в JSON. Запись атомарная."""

from __future__ import annotations

import os
import time
from pathlib import Path

from pydantic import BaseModel

# Windows не даёт подменить файл, пока его держит открытым другой процесс:
# пульт читает раскадровку на каждый запрос, соседняя сессия — на каждый
# статус. Чтение длится миллисекунды, поэтому подмена ждёт и повторяет, а не
# роняет стадию: ep003 (18.09.2026) сборка упала на PermissionError в самом
# начале, пока пульт читал storyboard.json
REPLACE_TRIES = 40
REPLACE_PAUSE_SEC = 0.05

# ссылка на модуль, чтобы тест подменял ожидание, не засыпая
_sleep = time.sleep


def read_model[M: BaseModel](path: Path, model: type[M]) -> M:
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def replace_file(tmp: Path, path: Path) -> None:
    """`os.replace` с повтором на занятом файле (до ~2 с)."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            _sleep(REPLACE_PAUSE_SEC)


def write_model(path: Path, data: BaseModel) -> None:
    """Сначала во временный файл, потом подмена — упавший процесс не оставит полуфайла."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data.model_dump_json(indent=2), encoding="utf-8")
    replace_file(tmp, path)
