"""Обложка: кадр, который зритель видит до того, как нажал.

Проверяется то, из-за чего обложка бывает не той: слова заголовка, порядок
кандидатов и совпадение подписи «на экране такое-то слово» с кадром.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_test_video

from videoforge.core.ffmpeg import run
from videoforge.core.thumbnail import THUMB_FILE, choose, keywords, moments_with

# Настоящее лицо в репозитории: вертикальный кадр 608×1080 из примера приёма
# библиотеки. Синтетическую картинку YuNet лицом не считает, и это правильно
FACE_FRAME = Path(__file__).resolve().parents[1] / "library/caption_color/dark_plate/before.jpg"
# The face photo is a third-party frame and is not part of the public repository.
needs_face = pytest.mark.skipif(not FACE_FRAME.exists(), reason="face fixture image not included")

ASS = """[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,social,,0,0,0,,I WALK INTO MY GYM
Dialogue: 0,0:00:04.00,0:00:06.00,social,,0,0,0,,AND YAMAL, MBAPPE AND
Dialogue: 0,0:00:07.00,0:00:09.00,social,,0,0,0,,HAALAND ARE THERE
"""


def test_title_keywords_are_promises_not_prepositions() -> None:
    """Кадр ищется по словам, которые обещают ролик: именам, а не предлогам."""
    words = keywords('Coach: "I walk into my gym and Yamal, Mbappé and Haaland are there"')

    assert words[:3] == ["yamal", "mbappe", "haaland"]  # имена собственные впереди
    assert "and" not in words and "the" not in words  # служебные не обещают ничего
    assert "walk" in words  # длинное значимое слово годится, просто позже имён


def test_diacritics_do_not_hide_a_word() -> None:
    """Mbappé в заголовке и MBAPPE на экране — одно слово."""
    found = moments_with(ASS, ["mbappe"])

    assert found and abs(found[0][0] - 5.0) < 0.01  # середина реплики 4–6 с


def test_one_line_with_three_names_does_not_break_the_batch(tmp_path: Path) -> None:
    """Три слова заголовка в одной реплике — это одна секунда, а не три.

    Выемка кадров пачкой возвращает пустой список, если кадров вышло меньше,
    чем просили, и обложка тогда не выбиралась вовсе. А ещё она отдаёт кадры по
    возрастанию времени: без сортировки подпись «на экране Ямаль» уезжала к
    чужому кадру."""
    video = make_test_video(tmp_path / "final.mp4", seconds=10.0, size="360x640")

    pick = choose(video, title="Yamal, Mbappé and Haaland are there", ass_text=ASS)

    assert pick is not None
    assert pick.path.name == THUMB_FILE and pick.path.exists()
    assert pick.keyword in {"yamal", "mbappe", "haaland"}
    # подпись должна совпасть с кадром: слово стоит на экране в эту секунду
    assert any(
        abs(sec - pick.at_sec) < 0.01 and word == pick.keyword
        for sec, word in moments_with(ASS, keywords("Yamal, Mbappé and Haaland are there"))
    )


def test_without_captions_the_cover_is_still_made(tmp_path: Path) -> None:
    """Нет субтитров — берём кадры по всей длине: обложка нужна всё равно."""
    video = make_test_video(tmp_path / "final.mp4", seconds=6.0, size="360x640")

    pick = choose(video, title="Без субтитров", ass_text="")

    assert pick is not None and pick.keyword == "" and pick.path.exists()
    assert 0 < pick.at_sec < 6.0  # не первый и не последний кадр: там fade


@needs_face
def test_faces_on_a_tall_frame_come_back_in_source_pixels(tmp_path: Path) -> None:
    """Кадр обложки 1080×1920 идёт в детектор уменьшенным по БОЛЬШЕЙ стороне,
    а координаты выходят в пикселях исходника.

    До 14.09.2026 уменьшалась только ширина, а она у шортса и так меньше 1280:
    кадр шёл в YuNet целиком, 0.25 с вместо 0.06, и лицо шириной 500+ px было
    за пределами того, на чём модель училась (на этом же кадре в полный размер
    она даёт score 0.71 и бокс, обрезанный на 145 px; уменьшенный — 0.91).
    Контракт координат: боксы на 1080×1920 равны боксам на том же кадре
    720×1280, умноженным на 1.5, — а лицо стоит там же, где на исходных 608×1080."""
    import cv2

    from videoforge.core import faces

    src = cv2.imread(str(FACE_FRAME))
    tall = cv2.resize(src, (1080, 1920), interpolation=cv2.INTER_CUBIC)
    big = tmp_path / "tall.png"  # PNG: без потерь, чтобы сравнивать пиксель в пиксель
    cv2.imwrite(str(big), tall)
    scale, small_size = faces.fit(1080, 1920)
    assert (scale, small_size) == (2 / 3, (720, 1280))
    small = tmp_path / "small.png"
    cv2.imwrite(str(small), cv2.resize(tall, small_size, interpolation=cv2.INTER_AREA))

    on_big, on_small = faces.detect_images([big, small])

    assert on_big and len(on_big) == len(on_small)
    for a, b in zip(on_big, on_small, strict=True):
        assert (a.x, a.y, a.w, a.h) == pytest.approx(
            (b.x / scale, b.y / scale, b.w / scale, b.h / scale), abs=1.0
        )
        assert a.score == pytest.approx(b.score, abs=1e-3)
    (orig,) = faces.detect_images([FACE_FRAME])  # 608×1080: меньше 1280, не уменьшается
    k = 1080 / 608
    assert abs(on_big[0].center_x - orig[0].center_x * k) < 0.03 * 1080
    assert abs(on_big[0].center_y - orig[0].center_y * k) < 0.03 * 1920


@needs_face
def test_cover_sees_the_face_on_a_vertical_video(tmp_path: Path) -> None:
    """Вертикальный ролик с настоящим лицом: обложка знает, что лицо в кадре.

    Лицо ищется на вынутом JPEG-кадре, а не заново в видео на той же секунде:
    ролик не открывается по разу на кандидата (замер 14.09.2026 на живом
    шортсе: 2.4 с → 1.0 с на выбор обложки, кадр выбран тот же)."""
    import cv2

    still = tmp_path / "face.png"
    cv2.imwrite(
        str(still),
        cv2.resize(cv2.imread(str(FACE_FRAME)), (1080, 1920), interpolation=cv2.INTER_CUBIC),
    )
    video = tmp_path / "final.mp4"
    run(
        [
            "-loop", "1", "-i", str(still), "-t", "3", "-r", "24",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video),
        ]
    )  # fmt: skip

    pick = choose(video, title="Лицо", ass_text="", limit=2)

    assert pick is not None and pick.face and pick.path.exists()
