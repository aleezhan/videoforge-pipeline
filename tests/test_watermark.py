"""Водяной знак канала в углу кадра: настройки проекта, положение по зоне
интерфейса площадки, отпечаток сборки, замечание QA и сам рендер."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat

from videoforge.core.captions import caption_band
from videoforge.core.editplan import default_plan
from videoforge.core.ffmpeg import Watermark, aspect_size, frame_at, watermark_xy
from videoforge.core.manifest import load_manifest
from videoforge.core.project import PLATFORMS, ProjectSpec, WatermarkConfig, create_project
from videoforge.core.project import load_project as load_project_cfg
from videoforge.core.resources import CaptionStyle, load_resources, search_dirs
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions, StageContext
from videoforge.core.storyboard import load_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.stages.assemble import inputs_fingerprint, platform_zone, watermark_for
from videoforge.stages.qa import watermark_findings

OPTS = RunOptions(auto_approve=True, raise_errors=True)
FRAME = (1080, 1920)
SHORTS_ZONE = PLATFORMS["youtube_shorts"]["ui_zone"]  # снизу 15%, справа 15%


def make_mark(path: Path, *, size: tuple[int, int] = (300, 100)) -> Path:
    """PNG с альфой: левая половина белая, правая чёрная, оба непрозрачные.
    Две крайности разом — какой бы цвет ни был под знаком, хотя бы одна
    половина от него далеко, и «пиксели изменились» не зависит от заглушки."""
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    img.paste((255, 255, 255, 255), (0, 0, w // 2, h))
    img.paste((0, 0, 0, 255), (w // 2, 0, w, h))
    img.save(path)
    return path


def enable_mark(pdir: Path, file: str = "watermark.png") -> None:
    """Вписать файл знака в project.yaml проекта (шаблон кладёт `file: null`);
    строка `file:` в шаблоне одна — у блока watermark."""
    cfg = pdir / "project.yaml"
    text = re.sub(r"(?m)^  file: \S+", f"  file: {file}", cfg.read_text(encoding="utf-8"))
    assert f"file: {file}" in text
    cfg.write_text(text, encoding="utf-8")


def test_watermark_config_comes_from_project_yaml(projects_root: Path) -> None:
    """Шаблон проекта несёт блок с комментариями и выключенным знаком; человек
    вписывает файл — конфиг читает его с умолчаниями угла, ширины и отступа."""
    pdir, _ = create_project(ProjectSpec(id="p", platforms=["youtube_shorts"]))
    text = (pdir / "project.yaml").read_text(encoding="utf-8")
    assert "watermark:" in text and "# Водяной знак канала" in text
    cfg = load_project_cfg("p")
    assert cfg.watermark.file is None and not cfg.watermark.enabled
    assert cfg.watermark == WatermarkConfig()  # умолчания шаблона совпадают с моделью

    enable_mark(pdir)
    cfg = load_project_cfg("p")
    assert cfg.watermark.enabled and cfg.watermark.file == "watermark.png"
    assert cfg.watermark.corner == "top_right"
    assert cfg.watermark.width_pct == 12 and cfg.watermark.opacity == 0.7
    assert cfg.watermark.margin_pct == 3

    with pytest.raises(ValueError):
        WatermarkConfig(corner="center")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WatermarkConfig(opacity=1.5)


def test_watermark_sits_in_the_corner_outside_the_ui_zone() -> None:
    """Угол минус отступ — и вне зоны интерфейса: правые углы левее колонки
    кнопок, нижние выше блока «канал / описание / звук»."""
    box, margin = (130, 33), 32
    assert watermark_xy("top_left", frame=FRAME, box=box, margin=margin) == (32, 32)
    assert watermark_xy("top_right", frame=FRAME, box=box, margin=margin) == (1080 - 32 - 130, 32)
    assert watermark_xy("bottom_left", frame=FRAME, box=box, margin=margin) == (32, 1920 - 32 - 33)

    right, bottom = int(1080 * SHORTS_ZONE["right"]), int(1920 * SHORTS_ZONE["bottom"])
    x, y = watermark_xy("top_right", frame=FRAME, box=box, margin=margin, zone=SHORTS_ZONE)
    assert (x, y) == (1080 - right - 32 - 130, 32)
    x, y = watermark_xy("bottom_right", frame=FRAME, box=box, margin=margin, zone=SHORTS_ZONE)
    assert (x, y) == (1080 - right - 32 - 130, 1920 - bottom - 32 - 33)
    x, y = watermark_xy("bottom_left", frame=FRAME, box=box, margin=margin, zone=SHORTS_ZONE)
    assert (x, y) == (32, 1920 - bottom - 32 - 33)
    # знак шире свободного места прижимается к краю, а не уезжает за кадр
    assert watermark_xy("top_right", frame=FRAME, box=(2000, 33), margin=margin) == (0, 32)

    # зона проекта — объединение площадок: TikTok закрывает снизу больше Shorts
    zone = platform_zone(["youtube_shorts", "tiktok"])
    assert zone["bottom"] == PLATFORMS["tiktok"]["ui_zone"]["bottom"] == 0.17
    assert zone["right"] == 0.15
    assert platform_zone([]) == {"top": 0.0, "bottom": 0.0, "right": 0.0}


def _context(episode: Path, providers: ProviderSet, projects_root: Path) -> StageContext:
    sb = load_storyboard(episode)
    pdir = projects_root / "t"
    return StageContext(
        stage_name="assemble",
        project_dir=episode,
        storyboard=sb,
        manifest=load_manifest(episode),
        providers=providers,
        resources=load_resources(sb.resources, search_dirs(pdir)),
        options=RunOptions(),
        project=load_project_cfg("t"),
        project_root=pdir,
        resource_dirs=search_dirs(pdir),
    )


def test_watermark_file_and_placement_change_the_fingerprint(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    """Смена файла знака или его места пересобирает финал: без этого новый
    логотип канала лежал бы в project.yaml, а в роликах оставался старый.
    Без знака в отпечаток не идёт ничего — собранные ролики не устаревают."""
    pdir = projects_root / "t"
    ctx = _context(episode, mock_providers, projects_root)
    scenes = ctx.storyboard.ordered_scenes()
    plan = default_plan(ctx.storyboard)

    def fp(mark: Watermark | None) -> str:
        return inputs_fingerprint(
            ctx,
            scenes,
            plan=plan,
            style=None,
            track=None,
            cfg=ctx.project.video,
            watermark=mark,  # type: ignore[union-attr]
        )

    assert watermark_for(ctx, FRAME) == (None, None)  # знак выключен — слоя нет
    plain = fp(None)

    png = make_mark(pdir / "watermark.png")
    enable_mark(pdir)
    ctx.project = load_project_cfg("t")
    mark, error = watermark_for(ctx, FRAME)
    assert error is None and mark is not None
    assert (mark.width, mark.height) == (130, 43)  # 12% ширины, высота по картинке 3:1
    assert (mark.x, mark.y) == watermark_xy(
        "top_right", frame=FRAME, box=(130, 43), margin=32, zone=SHORTS_ZONE
    )
    assert mark.x + mark.width <= 1080 * (1 - SHORTS_ZONE["right"])
    marked = fp(mark)
    assert marked != plain
    assert fp(mark) == marked  # тот же файл на том же месте — тот же отпечаток

    make_mark(png, size=(300, 150))  # перерисовали файл под тем же именем
    mark2, _ = watermark_for(ctx, FRAME)
    assert mark2 is not None and mark2.height == 65
    assert fp(mark2) != marked

    shutil.copy2(png, pdir / "other.png")
    enable_mark(pdir, "other.png")
    ctx.project = load_project_cfg("t")
    mark3, _ = watermark_for(ctx, FRAME)
    assert mark3 is not None and mark3.path.name == "other.png"
    assert fp(mark3) == fp(mark2)  # тот же файл байт в байт на том же месте — имя не входы

    moved = Watermark(png, 0, 0, mark2.width, mark2.height, mark2.opacity)
    assert fp(moved) != fp(mark2)  # другой угол — другой финал

    (pdir / "other.png").unlink()
    none, missing = watermark_for(ctx, FRAME)
    assert none is None and missing is not None and "не найден" in missing


def test_qa_notices_a_watermark_under_text_or_under_the_ui() -> None:
    """Замечание, не блок: знак в полосе субтитров, плашек или в зоне интерфейса."""
    style = CaptionStyle(id="t")  # низ, margin 22%
    band = caption_band(style, FRAME)
    clean = (756, 32, 130, 33)  # правый верх, левее колонки кнопок
    assert watermark_findings(clean, FRAME, ["youtube_shorts"], captions=band) == []

    y = int(band[0] * 1920) + 5  # верхний край знака ушёл в полосу субтитров
    found = watermark_findings((32, y, 130, 33), FRAME, ["youtube_shorts"], captions=band)
    assert [f.code for f in found] == ["watermark_band"]
    assert "субтитров" in found[0].message and found[0].severity == "warn"

    under_buttons = (1080 - 32 - 130, 32, 130, 33)  # правый верх БЕЗ учёта зоны
    found = watermark_findings(under_buttons, FRAME, ["youtube_shorts", "tiktok"], captions=band)
    assert [f.code for f in found] == ["watermark_zone", "watermark_zone"]
    assert "колонку кнопок" in found[0].message and "YouTube Shorts" in found[0].message

    low = (32, 1920 - 40, 130, 33)  # левый низ в блоке «канал / описание / звук»
    found = watermark_findings(low, FRAME, ["youtube_shorts"])
    assert len(found) == 1 and "нижний блок" in found[0].message

    # плашки сверху: знак в их полосе — то же замечание с другим словом
    top_band = (0.01, 0.10)
    found = watermark_findings(clean, FRAME, [], overlays=top_band, file="logo.png")
    assert len(found) == 1 and "плашек" in found[0].message and "logo.png" in found[0].message
    assert watermark_findings(clean, FRAME, [], captions=band, overlays=None) == []


def _mean_diff(a: Image.Image, b: Image.Image, box: tuple[int, int, int, int]) -> float:
    diff = ImageChops.difference(a.crop(box), b.crop(box))
    return sum(ImageStat.Stat(diff).mean) / 3


@pytest.mark.render
def test_watermark_is_burned_in_the_corner_and_not_in_the_caption_band(
    episode: Path, mock_providers: ProviderSet, projects_root: Path, tmp_path: Path
) -> None:
    """Замер по кадру: сборка со знаком отличается от сборки без него в
    прямоугольнике знака и не отличается в полосе субтитров. Отсутствующий
    файл при заданном знаке — отказ сборки, а не молчаливый ролик без знака."""
    pdir = projects_root / "t"
    run_episode(episode, mock_providers, OPTS)
    sb = load_storyboard(episode)
    assert sb.final.video and sb.final.watermark is None
    plain_fp = sb.final.inputs_fingerprint
    plain = shutil.copy2(episode / sb.final.video, tmp_path / "plain.mp4")

    make_mark(pdir / "watermark.png")
    enable_mark(pdir)
    report = run_episode(episode, mock_providers, RunOptions(only="assemble", raise_errors=True))
    assert report.ok, report.reason
    sb = load_storyboard(episode)
    assert sb.final.watermark == "watermark.png"
    assert sb.final.inputs_fingerprint != plain_fp
    marked = episode / sb.final.video  # type: ignore[operator]
    assert "знак: watermark.png" in load_manifest(episode).stages["assemble"].message

    # 12% ширины при картинке 3:1 → 130×43, отступ 3% → 32 px, правый верх
    # левее колонки кнопок Shorts — та же арифметика, что у сборки
    size = aspect_size(sb.aspect_ratio)
    w, h = 130, 43
    x, y = watermark_xy("top_right", frame=size, box=(w, h), margin=32, zone=SHORTS_ZONE)
    assert x + w <= size[0] * (1 - SHORTS_ZONE["right"])
    at = 1.0  # после fade in, внутри первой сцены с репликой
    before = Image.open(frame_at(plain, at, tmp_path / "plain.png")).convert("RGB")
    after = Image.open(frame_at(marked, at, tmp_path / "marked.png")).convert("RGB")
    assert _mean_diff(before, after, (x, y, x + w, y + h)) > 25
    # полоса субтитров (стиль default: низ) — те же пиксели, что и без знака
    style = load_resources(sb.resources, search_dirs(pdir)).caption_style or CaptionStyle(
        id="default"
    )
    top, bottom = caption_band(style, size)
    band = (0, int(top * size[1]), size[0], int(bottom * size[1]))
    assert _mean_diff(before, after, band) < 3
    assert y + h < top * size[1]  # и сам знак выше полосы

    enable_mark(pdir, "missing.png")
    report = run_episode(episode, mock_providers, RunOptions(only="assemble"))
    assert not report.ok and report.reason and "missing.png" in report.reason
    assert "не найден" in report.reason
