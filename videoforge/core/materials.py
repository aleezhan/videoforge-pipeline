"""Материалы ролика: из чего он собран и откуда это взято.

Файл — `05_assemble/materials.json`. Одно слово на всё: файл `materials.json`,
модуль `core/materials.py`, команда `videoforge episode materials`, поле ответа
API `materials`, раздел пульта «Материалы». Искать не приходится.

**Кому он нужен.** У ролика, прошедшего конвейер, ответ «из чего собран» уже
есть в файлах: чем сделан каждый кадр — в `Scene.provenance`, почём — в
`cost_usd`, права на трек — в его ресурсе, исходное видео нарезки — в
`sources/<id>/source.json`. Пульт собирает это сам (`api/montage.py:_materials`),
и второй файл с тем же содержимым был бы вторым ответом на один вопрос: они
разойдутся, и молча.

Этот файл — для роликов, у которых такого ответа НЕТ:
- собранных скриптом мимо конвейера (эдит под трек: восемь чужих клипов, маска,
  кусок трека — всё это живёт кодом внутри `build.py`, и прочитать его пульту
  неоткуда);
- копий чужого готового файла (ролик в галерее тестов — это `final.mp4`,
  принесённый из другого эпизода, и больше рядом нет ничего).

**Почему ссылка важнее пути.** У t04 папки `src/`, `music/`, `masks/` удалены
уборкой — медиа под `.gitignore`. От ролика остался только `final.mp4`, и
локальные пути в паспорте указывали бы в пустоту. Поэтому у материала есть
`link` — откуда его взяли в мире, — и он переживает уборку, а `size`/`mtime_ns`
записываются, чтобы паспорт умел честно протухать: файл подменили — видно.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from videoforge.core.io import read_model, write_model

MATERIALS_FILE = "materials.json"
ASSEMBLE_DIR = "05_assemble"

# Роль материала в ролике. Список короткий намеренно: он для чтения глазами,
# а не для машины — рендерить по нему ничего нельзя и не нужно.
Role = Literal[
    "clip",  # кусок видео
    "image",  # кадр, фон, картинка
    "audio",  # звук или озвучка файлом
    "music",  # трек
    "mask",  # маска, оверлей, PNG-последовательность
    "font",  # шрифт, если он не из assets/fonts
    "captions",  # файл субтитров
    "script",  # код сборки: build.py и его спутники
    "data",  # замеры рядом: onsets.json, тайминги слов
    "source",  # исходное видео, из которого резали
    "copy_of",  # этот ролик — копия готового файла другого эпизода
    "other",
]

ROLE_TITLES: dict[str, str] = {
    "clip": "Клип",
    "image": "Картинка",
    "audio": "Звук",
    "music": "Музыка",
    "mask": "Маска",
    "font": "Шрифт",
    "captions": "Субтитры",
    "script": "Скрипт сборки",
    "data": "Замеры",
    "source": "Исходное видео",
    "copy_of": "Копия ролика",
    "other": "Прочее",
}


class Material(BaseModel):
    """Один материал: что это, где лежит и откуда взято."""

    role: Role = "other"
    title: str = ""  # человеку: «Дождливая улица», «Dua Lipa — Training Season (live)»
    path: str = ""  # относительно папки эпизода, либо от корня репозитория
    link: str = ""  # откуда взято в мире: URL. Переживает уборку, в отличие от пути
    license: str = ""
    credit: str = ""  # строка упоминания, если лицензия его требует
    note: str = ""  # чем обработано, какое окно взято, что с ним сделали
    # снимок файла на момент записи: подменили или пересобрали — паспорт протух
    size: int | None = None
    mtime_ns: int | None = None


class Materials(BaseModel):
    """Паспорт ролика. Ничего не рендерит и ничем не управляет: это опись."""

    version: int = 1
    generated_at: datetime | None = None
    made_by: str = ""  # чем собран ролик: «build.py рядом», «копия», «руками в редакторе»
    rebuild: str = ""  # как пересобрать — команда; или почему пересобрать нельзя
    note: str = ""
    items: list[Material] = Field(default_factory=list)


def materials_path(episode_dir: Path) -> Path:
    return episode_dir / ASSEMBLE_DIR / MATERIALS_FILE


def load_materials(episode_dir: Path) -> Materials | None:
    path = materials_path(episode_dir)
    return read_model(path, Materials) if path.exists() else None


def save_materials(episode_dir: Path, materials: Materials) -> Path:
    path = materials_path(episode_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    materials.generated_at = datetime.now(UTC)
    write_model(path, materials)
    return path


def resolve(episode_dir: Path, root: Path, rel: str) -> Path | None:
    """Путь материала → файл на диске. Ищем сначала рядом с эпизодом, потом от
    корня репозитория: чужой клип может лежать в источниках другого проекта."""
    if not rel:
        return None
    for base in (episode_dir, root):
        candidate = base / rel
        if candidate.exists():
            return candidate
    return None


def stamp(material: Material, path: Path | None) -> Material:
    """Записать снимок файла в материал (или снять его, если файла нет)."""
    if path is None or not path.exists():
        return material.model_copy(update={"size": None, "mtime_ns": None})
    st = path.stat()
    return material.model_copy(update={"size": st.st_size, "mtime_ns": st.st_mtime_ns})


def state(material: Material, path: Path | None) -> str:
    """Что стало с материалом с тех пор, как его записали: `ok` | `changed` | `gone`.

    Пустая строка — про этот материал сказать нечего: он без пути (ссылка на
    чужое видео) или снимок не записывали."""
    if not material.path:
        return ""
    if path is None:
        return "gone"
    if material.size is None or material.mtime_ns is None:
        return ""
    st = path.stat()
    return "ok" if (st.st_size, st.st_mtime_ns) == (material.size, material.mtime_ns) else "changed"
