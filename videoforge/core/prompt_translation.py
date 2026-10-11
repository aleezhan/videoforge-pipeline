"""Перевод промптов сцен для чтения человеком: `prompts.<lang>.json` в папке эпизода.

Промпты картинок и движения пишутся по-английски — так модели работают лучше,
и это правило репозитория. Но раскадровку читает человек, и ему нужен свой
язык. Перевод — не часть контракта раскадровки (в `storyboard.json` его нет
намеренно: он ничего не производит), а справочный файл рядом, который
интерфейс показывает вместо оригинала, если он есть и свеж.

Свежесть — по отпечатку оригинала: изменился промпт сцены — её перевод
устарел и не показывается, пока не пересчитан. Переводит `stages/prompt_translate.py`
(LLM, для claude_cli — $0); здесь только модель файла и проверка свежести —
ядро от провайдеров не зависит.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from videoforge.core.io import read_model, write_model
from videoforge.core.storyboard import Scene, Storyboard

FILE_TEMPLATE = "prompts.{lang}.json"

LANGUAGE_NAMES: dict[str, str] = {
    "ru": "Russian",
    "en": "English",
    "kk": "Kazakh",
    "uz": "Uzbek",
    "tr": "Turkish",
    "ar": "Arabic",
    "de": "German",
    "es": "Spanish",
    "fr": "French",
}


class SceneTranslation(BaseModel):
    visual: str = ""
    motion: str = ""
    source: str  # отпечаток оригинала: изменился промпт — перевод устарел


class PromptTranslations(BaseModel):
    language: str
    llm: str = ""  # кто переводил
    generated_at: datetime | None = None
    scenes: dict[str, SceneTranslation] = Field(default_factory=dict)


def translation_path(episode_dir: Path, lang: str) -> Path:
    return episode_dir / FILE_TEMPLATE.format(lang=lang)


def source_hash(visual: str, motion: str) -> str:
    return hashlib.sha1(f"{visual}\n---\n{motion}".encode()).hexdigest()[:16]


def scene_hash(scene: Scene) -> str:
    return source_hash(scene.visual_prompt, scene.motion_prompt)


def load_translations(episode_dir: Path, lang: str) -> PromptTranslations | None:
    path = translation_path(episode_dir, lang)
    if not path.exists():
        return None
    try:
        return read_model(path, PromptTranslations)
    except ValueError:
        return None


def save_translations(episode_dir: Path, data: PromptTranslations) -> Path:
    path = translation_path(episode_dir, data.language)
    write_model(path, data)
    return path


def fresh_scenes(data: PromptTranslations, storyboard: Storyboard) -> dict[str, SceneTranslation]:
    """Переводы, которые ещё соответствуют промптам раскадровки."""
    return {
        s.id: data.scenes[s.id]
        for s in storyboard.scenes
        if s.id in data.scenes and data.scenes[s.id].source == scene_hash(s)
    }


def stale_scenes(data: PromptTranslations, storyboard: Storyboard) -> list[str]:
    """Сцены, у которых перевода нет или он устарел (и есть что переводить)."""
    fresh = fresh_scenes(data, storyboard)
    return [
        s.id
        for s in storyboard.scenes
        if s.id not in fresh and (s.visual_prompt.strip() or s.motion_prompt.strip())
    ]
