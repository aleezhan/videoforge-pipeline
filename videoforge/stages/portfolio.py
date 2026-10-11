"""Генерация ядра портфолио персонажа — вне конвейера эпизода.

Это не стадия workflow: команда работает над ресурсом, а не над папкой эпизода.
Живёт в stages, потому что этому слою можно видеть core и providers.base.

Ядро — кадры одной линии происхождения: сначала несколько кандидатов
`face_front` (пользователь выбирает лицо), потом остальные кадры генерируются
уже с одобренным портфолио как референсами — иначе семь независимых генераций
дадут семь разных людей. Кандидаты пишутся в подпапку `_candidates/`: подпапки
портфолио в сцены не подхватываются, кадр попадает в ядро только через
`videoforge resource add-ref --as <имя-кадра>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from videoforge.core.resources import Character, Style
from videoforge.providers.base import GenResult, ImageProvider
from videoforge.stages.prompts import join_parts, resource_references

CANDIDATES_DIR = "_candidates"

# Гигиена референса: фон, свет и одежда протекают в кадры сцен так же, как
# лицо. Гардероб задаёт сцена, поэтому в портфолио — нарочно неприметная одежда.
HYGIENE = (
    "Plain light-gray studio background, soft even lighting, no dramatic "
    "shadows. Wearing simple plain casual clothes in muted colors. No props, "
    "no text, no watermarks, exactly one character in frame"
)


@dataclass(frozen=True)
class Shot:
    name: str  # имя файла в ядре — оно же подпись референса для модели
    prompt: str
    aspect_ratio: str


CORE_SHOTS: tuple[Shot, ...] = (
    Shot(
        "face_front",
        "head-and-shoulders portrait, facing the camera directly, neutral expression",
        "1:1",
    ),
    Shot(
        "face_three_quarter",
        "head-and-shoulders portrait, head turned three-quarters to the left",
        "1:1",
    ),
    Shot(
        "face_profile",
        "head-and-shoulders portrait in strict side profile, facing left",
        "1:1",
    ),
    Shot(
        "body_full_front",
        "full-body shot, standing straight, facing the camera, "
        "the whole figure visible head to toe",
        "2:3",
    ),
    Shot(
        "body_full_back",
        "full-body shot from behind, standing straight, the whole figure visible head to toe",
        "2:3",
    ),
    Shot(
        "body_waist_up",
        "waist-up shot, standing, facing the camera, arms relaxed",
        "3:4",
    ),
    Shot(
        "face_expression_smile",
        "head-and-shoulders portrait, facing the camera, broad happy smile",
        "1:1",
    ),
)
SHOTS_BY_NAME: dict[str, Shot] = {s.name: s for s in CORE_SHOTS}
CORE_SHOT_NAMES: tuple[str, ...] = tuple(s.name for s in CORE_SHOTS)


def missing_core_shots(character: Character) -> list[str]:
    """Кадры ядра, которых ещё нет в корне портфолио (по имени файла)."""
    have = {p.stem for p in character.reference_paths()}
    return [s.name for s in CORE_SHOTS if s.name not in have]


def shot_prompt(
    character: Character, shot: Shot, style: Style | None, *, has_references: bool
) -> str:
    parts: list[str] = []
    if style:
        parts.append(style.prompt_prefix)
    parts.append(character.prompt_block)
    if has_references:
        parts.append("The exact same character as in the attached reference images")
    parts.append(shot.prompt)
    parts.append(HYGIENE)
    if style:
        parts.append(style.prompt_suffix)
    prompt = join_parts(parts)
    if style and style.negative.strip():
        prompt += f" Avoid: {style.negative.strip()}."
    return prompt


def _next_version(folder: Path, shot: str) -> int:
    versions = [
        int(p.stem.removeprefix(f"{shot}_v"))
        for p in folder.glob(f"{shot}_v*.png")
        if p.stem.removeprefix(f"{shot}_v").isdigit()
    ]
    return max(versions, default=0) + 1


def generate_shots(
    provider: ImageProvider,
    character: Character,
    shot_names: list[str],
    *,
    style: Style | None = None,
    count: int = 1,
) -> list[GenResult]:
    """Сгенерировать кандидатов кадров в `<портфолио>/_candidates/`.

    Текущее ядро портфолио идёт референсами — так новые кадры остаются той же
    линией происхождения. Пустое портфолио → генерация по паспорту (prompt_block).
    """
    unknown = [n for n in shot_names if n not in SHOTS_BY_NAME]
    if unknown:  # опечатка не должна стоить денег: проверяем всё до генерации
        raise ValueError(f"неизвестные кадры: {unknown}; ядро: {list(CORE_SHOT_NAMES)}")
    refs = resource_references("character", character)
    out_dir = character.folder() / CANDIDATES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[GenResult] = []
    for name in shot_names:
        shot = SHOTS_BY_NAME[name]
        prompt = shot_prompt(character, shot, style, has_references=bool(refs))
        for _ in range(count):
            path = out_dir / f"{name}_v{_next_version(out_dir, name):02d}.png"
            results.append(
                provider.generate(
                    prompt, references=refs, aspect_ratio=shot.aspect_ratio, out_path=path
                )
            )
    return results
