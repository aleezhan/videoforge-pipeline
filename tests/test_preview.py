"""Предпросмотр под площадку: зоны интерфейса поверх готового кадра.

В плеере ролик выглядит нормально, а в приложении низ кадра закрывает блок с
именем канала и звуком. Числа `qa` («субтитры на 12% при интерфейсе 15%») глазу
ничего не говорят — здесь те же числа рисуются на кадре.
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_test_video

from videoforge.core.ffmpeg import probe_size
from videoforge.core.preview import busiest_caption_sec, preview_frame, zone_filters

ASS = """[Script Info]
PlayResX: 1080
PlayResY: 1920

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\fad(60,60)}коротко
Dialogue: 0,0:00:10.00,0:00:14.00,Default,,0,0,0,,{\\fad(60,60)}самая длинная реплика ролика
Dialogue: 0,0:00:20.00,0:00:21.00,Default,,0,0,0,,{\\fad(60,60)}снова коротко
"""


def test_zones_are_drawn_from_the_platform_numbers() -> None:
    zone = {"top": 0.0, "bottom": 0.15, "right": 0.15}
    f = zone_filters(zone, (1080, 1920), label="YouTube Shorts")
    assert "drawbox=x=0:y=1632:w=1080:h=288" in f  # низ: 15% от 1920
    assert "drawbox=x=918:y=0:w=162:h=1920" in f  # право: 15% от 1080
    assert "drawtext" in f and "YouTube Shorts" in f
    # безопасная область очерчена, а не залита
    safe = [p for p in f.split(",") if "t=fill" not in p and p.startswith("drawbox")]
    assert len(safe) == 1 and "w=918:h=1632" in safe[0]

    # нулевая зона не рисуется вовсе
    empty = zone_filters({"top": 0.0, "bottom": 0.0, "right": 0.0}, (1080, 1920))
    assert empty.count("drawbox") == 1  # только рамка безопасной области


def test_busiest_caption_is_the_longest_line(tmp_path: Path) -> None:
    """Кадр берут там, где текста больше всего: риск попасть под интерфейс выше."""
    ass = tmp_path / "captions.ass"
    ass.write_text(ASS, encoding="utf-8")
    assert busiest_caption_sec(ass) == 12.0  # середина реплики 10–14
    assert busiest_caption_sec(tmp_path / "нет.ass") is None


def test_preview_frame_keeps_the_size_and_draws_on_top(tmp_path: Path) -> None:
    video = make_test_video(tmp_path / "v.mp4", seconds=4, size="1080x1920")
    out = preview_frame(
        video,
        tmp_path / "p.png",
        at_sec=2.0,
        zone={"top": 0.0, "bottom": 0.15, "right": 0.15},
        size=(1080, 1920),
        label="YouTube Shorts",
    )
    assert out.exists() and probe_size(out) == (1080, 1920)
