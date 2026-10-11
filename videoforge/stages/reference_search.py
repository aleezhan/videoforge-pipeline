"""Референс композиции для кадра, который не совпал с описанием
(`features/scene_reference_image`).

Кадр «не про то» слепым перекатом сида не лечится: модель нарисует ту же кучу
крыльев другим сидом. Лечит картинка, где нужная расстановка уже есть: стадия
берёт из вердикта проверки запрос про КОМПОЗИЦИЮ («ряд одинаковых фигур,
уходящий вдаль»), ищет по нему в открытом источнике (`ImageSearchProvider`),
раскладывает найденное одним листом с номерами и просит модель выбрать
глазами один или «ни один». Выбранный ложится в `02_images/refs/<scene>.<ext>`
и в `Scene.reference_assets`, происхождение — в `refs.json` рядом (ссылка,
страница, лицензия, запрос, почему выбран): ссылка важнее файла, папку чистят.

Человек в этом не участвует (решение владельца 14.09.2026): он видит на
воротах строку «кадр не совпал → референс → v02 прошёл» и референс рядом с
кадром в отчёте и пульте. Файл, положенный в `reference_assets` руками,
стадия принимает так же.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from videoforge.core.stage import StageContext
from videoforge.core.storyboard import Scene
from videoforge.paths import fonts_dir
from videoforge.providers.base import ImageCandidate, ProviderError, Reference

OUT_DIR = "02_images"
REFS_DIR = "refs"
REFS_FILE = "refs.json"
CANDIDATES = 6  # кандидатов на лист: больше — мельче на листе, а выбирать по нему
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")

# подпись референса для художника, который принимает картинки с подписями
# (gemini): фото задаёт раскладку, а не предметы и не реализм
COMPOSITION_LABEL = (
    "composition of the scene (copy its layout, perspective and spacing; "
    "not its subjects, colors or realism)"
)

PICK_STEP = "VIDEOFORGE-STEP: reference_pick"
PICK_PROMPT = """{step}
You choose a COMPOSITION reference for an illustrator. The scene needs: "{description}".
The contact sheet shows {n} candidate photos found for the query "{query}", numbered 1..{n}
(the number is drawn in the corner of each photo).
Pick the ONE whose arrangement, perspective and framing best match what the scene needs.
The subjects in the photo do not matter — they will be replaced; prefer photos with fewer
extra objects, people and text. If none fits, answer 0.
Return ONLY JSON: {{"pick": <number>, "why": "<one sentence>"}}"""


def refs_dir(ctx: StageContext) -> Path:
    return ctx.stage_dir(OUT_DIR) / REFS_DIR


def load_refs(folder: Path) -> dict[str, dict]:
    path = folder / REFS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_refs(folder: Path, data: dict[str, dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / REFS_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def scene_references(ctx: StageContext, scene: Scene) -> list[Reference]:
    """Картинки «как надо» сцены — референсами с ролью composition. Пропавший
    файл — ошибка, а не молчание: человек положил путь, значит ждёт кадр по нему."""
    refs: list[Reference] = []
    for rel in scene.reference_assets:
        path = ctx.abs(rel)
        if not path.exists():
            raise ProviderError(f"сцена {scene.id}: референс {rel} не найден в папке эпизода")
        refs.append(Reference(label=COMPOSITION_LABEL, path=path, role="composition"))
    return refs


def contact_sheet(paths: list[Path], out: Path, *, cols: int = 3, thumb: int = 448) -> Path:
    """Лист кандидатов с номерами в углу — по нему выбирает модель."""
    rows = (len(paths) + cols - 1) // cols
    pad = 12
    sheet = Image.new(
        "RGB", (cols * (thumb + pad) + pad, rows * (thumb + pad) + pad), (245, 245, 240)
    )
    draw = ImageDraw.Draw(sheet)
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        font = ImageFont.truetype(str(fonts_dir() / "PTSans-Bold.ttf"), 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    for i, path in enumerate(paths):
        im = Image.open(path).convert("RGB")
        im.thumbnail((thumb, thumb))
        x = pad + (i % cols) * (thumb + pad)
        y = pad + (i // cols) * (thumb + pad)
        sheet.paste(im, (x + (thumb - im.width) // 2, y + (thumb - im.height) // 2))
        label = str(i + 1)
        draw.rectangle((x, y, x + 64, y + 60), fill=(20, 20, 20))
        draw.text((x + 14, y + 2), label, fill=(255, 220, 80), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def _suffix(candidate: ImageCandidate) -> str:
    suffix = Path(candidate.url.split("?")[0]).suffix.lower()
    return suffix if suffix in IMAGE_SUFFIXES else ".jpg"


def _parse_pick(text: str, n: int) -> tuple[int, str]:
    try:
        data = json.loads(text)
        pick = int(data.get("pick", 0))
        why = str(data.get("why") or "").strip()
    except (ValueError, TypeError, AttributeError):
        return 0, f"ответ не разобран: {text[:120]!r}"
    return (pick if 1 <= pick <= n else 0), why


def find_reference(ctx: StageContext, scene: Scene, query: str) -> dict | None:
    """Найти и выбрать референс композиции по запросу. Возвращает запись
    refs.json (`file` — путь от папки эпизода, уже дописанный в
    `scene.reference_assets`, `url`, `page`, `license`, `why`) или None — не
    нашлось или модель никого не выбрала; и то и другое записано в refs.json."""
    search = ctx.providers.search
    if search is None or not query.strip():
        return None
    folder = refs_dir(ctx)
    refs = load_refs(folder)
    entry: dict = {
        "query": query,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "search": search.name,
    }
    candidates = search.search(query, limit=CANDIDATES)
    entry["found"] = len(candidates)
    if not candidates:
        refs[scene.id] = entry
        save_refs(folder, refs)
        return None

    cand_dir = folder / scene.id
    files: list[Path] = []
    kept: list[ImageCandidate] = []
    for i, cand in enumerate(candidates, 1):
        try:
            files.append(search.fetch(cand, cand_dir / f"cand_{i}{_suffix(cand)}"))
            kept.append(cand)
        except ProviderError as exc:
            ctx.log.warning("сцена %s: кандидат %d не скачался: %s", scene.id, i, exc)
    if not files:
        entry["found"] = 0
        refs[scene.id] = entry
        save_refs(folder, refs)
        return None
    sheet = contact_sheet(files, cand_dir / "candidates.png")

    llm = ctx.providers.llm
    ctx.afford(llm.check_price_usd, f"выбор референса {scene.id}")
    res = llm.complete(
        PICK_PROMPT.format(
            step=PICK_STEP, description=scene.visual_prompt, n=len(files), query=query
        ),
        json_mode=True,
        images=[sheet],
    )
    ctx.add_cost(res.cost_usd, llm.name, scene.id, "reference pick")
    pick, why = _parse_pick(res.text, len(files))
    entry["candidates"] = [{"url": c.url, "page": c.page, "license": c.license} for c in kept]
    entry["why"] = why
    if not pick:
        entry["pick"] = 0
        refs[scene.id] = entry
        save_refs(folder, refs)
        return None

    chosen, cand = files[pick - 1], kept[pick - 1]
    dst = folder / f"{scene.id}{chosen.suffix}"
    shutil.copy2(chosen, dst)
    rel = ctx.rel(dst)
    entry.update(
        {
            "pick": pick,
            "file": rel,
            "url": cand.url,
            "page": cand.page,
            "license": cand.license,
            "title": cand.title,
        }
    )
    refs[scene.id] = entry
    save_refs(folder, refs)
    if rel not in scene.reference_assets:
        scene.reference_assets.append(rel)
    return entry
