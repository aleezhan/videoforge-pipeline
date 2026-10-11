"""Субтитры из таймингов озвучки: слова → реплики → ASS с караоке."""

from __future__ import annotations

import re

from videoforge.core.captions import (
    MIN_EVENT_SEC,
    SceneWords,
    Word,
    build_ass,
    build_events,
    group_words,
    parse_srt_words,
)
from videoforge.core.resources import CaptionStyle

# ровно то, что пишет edge-tts: по слову на реплику
SRT = """1
00:00:00,100 --> 00:00:00,337
Ты

2
00:00:00,337 --> 00:00:00,637
сто

3
00:00:00,637 --> 00:00:00,962
раз

4
00:00:00,962 --> 00:00:01,200
это

5
00:00:01,200 --> 00:00:01,762
говорил.

6
00:00:02,600 --> 00:00:02,900
Потом
"""


def test_parse_srt_words() -> None:
    words = parse_srt_words(SRT)
    assert [w.text for w in words] == ["Ты", "сто", "раз", "это", "говорил.", "Потом"]
    assert words[0].start == 0.1 and words[0].end == 0.337
    assert words[-1].start == 2.6


def test_multiword_cue_is_split_proportionally() -> None:
    words = parse_srt_words("1\n00:00:01,000 --> 00:00:02,000\nаб вгде\n")
    assert [w.text for w in words] == ["аб", "вгде"]
    assert words[0].end == words[1].start
    assert abs((words[0].end - words[0].start) - 1 / 3) < 0.01  # 2 буквы из 6


def test_groups_break_on_limits_pause_and_sentence_end() -> None:
    words = parse_srt_words(SRT)
    groups = group_words(words, max_words=3, max_chars=24, max_gap_sec=0.55)
    texts = [" ".join(w.text for w in g) for g in groups]
    # три слова — лимит; «говорил.» закрывает предложение; «Потом» после паузы 0.84 с
    assert texts == ["Ты сто раз", "это говорил.", "Потом"]


def test_orphan_preposition_moves_to_the_next_group() -> None:
    """«Почему сияние только у» / «полюсов» — так рвалась реплика на живом прогоне."""
    words = parse_srt_words(
        "1\n00:00:00,000 --> 00:00:00,400\nПочему\n\n2\n00:00:00,400 --> 00:00:01,200\nсияние\n\n"
        "3\n00:00:01,200 --> 00:00:01,600\nтолько\n\n4\n00:00:01,600 --> 00:00:01,700\nу\n\n"
        "5\n00:00:01,700 --> 00:00:02,300\nполюсов\n"
    )
    groups = group_words(words, max_words=5, max_chars=24, max_gap_sec=0.55)
    assert [" ".join(w.text for w in g) for g in groups] == ["Почему сияние только", "у полюсов"]


def test_ass_karaoke_spans_the_words_and_offsets_the_scene() -> None:
    words = parse_srt_words(SRT)[:5]
    style = CaptionStyle(id="t", max_words=5, max_chars=40)
    events = build_events([SceneWords("s1", 10.0, 4.0, words)], style)
    assert len(events) == 1
    ass = build_ass(events, style, size=(1920, 1080))

    assert "PlayResX: 1920" in ass and "Fontsize: " not in ass  # размер в строке Style
    assert "Dialogue: 0,0:00:10.10," in ass  # смещение сцены прибавлено
    karaoke = [int(k) for k in re.findall(r"\\k(\d+)", ass)]
    assert len(karaoke) == 5
    # подсветка тянется от первого слова до конца последнего: 0.1 → 1.762 = 166 сс
    assert sum(karaoke) == round((words[-1].end - words[0].start) * 100)
    assert "\\fad(60,60)" in ass


def test_short_event_is_held_but_not_past_the_scene_box() -> None:
    words = parse_srt_words(SRT)[:1]  # одно слово в 0.24 с
    style = CaptionStyle(id="t")
    ev = build_events([SceneWords("s1", 0.0, 6.0, words)], style)[0]
    assert ev.end - ev.start >= MIN_EVENT_SEC - 1e-6
    boxed = build_events([SceneWords("s1", 0.0, 0.5, words)], style)[0]
    assert boxed.end <= 0.5


def test_words_after_the_scene_box_are_dropped() -> None:
    words = parse_srt_words(SRT)
    style = CaptionStyle(id="t")
    events = build_events([SceneWords("s1", 0.0, 2.0, words)], style)
    shown = [w.text for e in events for w in e.words]
    assert "Потом" not in shown  # начинается на 2.6 с, коробка сцены — 2 с


def test_uppercase_emphasis_and_phrase_mode() -> None:
    words = parse_srt_words(SRT)[:5]
    scene = SceneWords("s1", 0.0, 4.0, words)
    loud = CaptionStyle(id="k", uppercase=True)
    ass = build_ass(build_events([scene], loud), loud, size=(1080, 1920),
                    emphasis={"s1": frozenset({"сто"})})  # fmt: skip
    assert "СТО" in ass and "\\fscx112" in ass
    assert "PlayResY: 1920" in ass

    quiet = CaptionStyle(id="p", mode="phrase")
    ass = build_ass(build_events([scene], quiet), quiet, size=(1920, 1080))
    assert "\\k" not in ass and "Ты сто раз это говорил." in ass


def test_shadow_is_off_by_default() -> None:
    """Тень ASS — это вторая копия текста со смещением, а не размытие. Поверх
    обводки она читается как второй субтитр за первым: человек увидел это на
    готовом шортсе, рендер того же кадра с тенью и без подтвердил. Включать её
    можно, но осознанно и заметной, а не однопиксельной."""
    style = CaptionStyle(id="t")
    assert style.shadow == 0.0
    words = parse_srt_words(SRT)[:2]
    ev = build_events([SceneWords("s1", 0.0, 4.0, words)], style)
    line = next(
        x for x in build_ass(ev, style, size=(1080, 1920)).splitlines() if x[:6] == "Style:"
    )
    outline, shadow = line.split(",")[16:18]
    assert float(outline) > 0 and float(shadow) == 0  # от фона отделяет обводка, не тень

    loud = CaptionStyle(id="t", shadow=4.0)  # захотел тень — получил, и сразу видную
    line = next(x for x in build_ass(ev, loud, size=(1080, 1920)).splitlines() if x[:6] == "Style:")
    assert float(line.split(",")[17]) == 4.0


def test_font_size_follows_the_short_side() -> None:
    style = CaptionStyle(id="t", size_pct=5.0)
    words = parse_srt_words(SRT)[:2]
    ev = build_events([SceneWords("s1", 0.0, 4.0, words)], style)
    wide = build_ass(ev, style, size=(1920, 1080))
    tall = build_ass(ev, style, size=(1080, 1920))
    assert "Style: Default,PT Sans,54," in wide
    assert "Style: Default,PT Sans,54," in tall  # одинаково читается в обоих форматах


def test_pop_gives_every_word_its_own_line_with_a_spring() -> None:
    r"""`animation: pop` — реплика на каждое слово, и произносимое подпрыгивает.

    Пружину рисует `\t`, а он живёт внутри реплики ASS: анимировать второе
    слово фразы, не разбив её на реплики по слову, нечем."""
    words = parse_srt_words(SRT)[:5]
    scene = SceneWords("s1", 0.0, 4.0, words)
    style = CaptionStyle(id="b", animation="pop", pop_scale=124)
    lines = [
        x for x in build_ass(build_events([scene], style), style, size=(1080, 1920)).splitlines()
        if x.startswith("Dialogue:")
    ]  # fmt: skip
    assert len(lines) == len(words)  # по строке на слово, а не по строке на фразу
    assert all(r"\t(0," in x and r"\fscx124" in x for x in lines)
    assert r"\k" not in "\n".join(lines)  # цвет расставлен явно, караоке-теги не нужны
    # строки идут встык: у libass нет ни мигания, ни наложения двух копий текста
    starts = [x.split(",")[1] for x in lines]
    ends = [x.split(",")[2] for x in lines]
    assert ends[:-1] == starts[1:]


def _dialogues(style: CaptionStyle, words_count: int = 3) -> tuple[list[str], list[str]]:
    words = parse_srt_words(SRT)[:words_count]
    scene = SceneWords("s1", 0.0, 3.0, words)
    lines = [
        x for x in build_ass(build_events([scene], style), style, size=(1080, 1920)).splitlines()
        if x.startswith("Dialogue:")
    ]  # fmt: skip
    return (
        [x for x in lines if x.startswith("Dialogue: 0,")],
        [x for x in lines if x.startswith("Dialogue: 1,")],
    )


def test_words_appear_one_by_one_and_ride_up_from_below() -> None:
    r"""`animation: reveal` — слова проявляются по очереди на своих местах.

    Строка разложена целиком с первого кадра: непроизнесённое слово спрятано
    прозрачностью, а не выброшено, поэтому текст не двигается. Подъём рисует
    `\move`, а он живёт на реплике целиком — отсюда вторая, верхняя реплика,
    где видно только появляющееся слово."""
    words = parse_srt_words(SRT)[:3]  # «Ты сто раз»
    style = CaptionStyle(id="r", animation="reveal", reveal_ms=200, reveal_rise_pct=40)
    settled, rising = _dialogues(style)
    assert len(settled) == len(words)  # нижняя реплика на каждое слово
    assert len(rising) == 2 * len(words)  # подъём двумя отрезками: быстро, потом медленно

    # вся реплика на месте с первого кадра, будущие слова спрятаны прозрачностью
    for line in settled:
        assert all(w.text.lower() in line.lower() for w in words)
    assert settled[0].count(r"\alpha&HFF&") == len(words)  # видно только первое слово

    # слово едет снизу вверх и проявляется из прозрачного
    moves = [re.search(r"\\move\((\d+),(\d+),(\d+),(\d+),", x) for x in rising]
    assert all(m and int(m.group(2)) > int(m.group(4)) for m in moves)
    assert r"\k" not in "\n".join(settled + rising)  # строка и так перерисовывается


def test_grow_builds_the_line_word_by_word() -> None:
    """`animation: grow` — на экране только произнесённые слова: строка растёт,
    и каждое новое слово сдвигает прежние, потому что встаёт по центру заново."""
    words = parse_srt_words(SRT)[:3]
    style = CaptionStyle(id="g", animation="grow", reveal_ms=200, reveal_rise_pct=40)
    settled, _ = _dialogues(style)
    for i, line in enumerate(settled):
        for j, word in enumerate(words):
            assert (word.text.lower() in line.lower()) is (j <= i)


def test_appearing_word_is_never_handed_over_mid_flight() -> None:
    """Слово от появления до конца своего окна рисует одна реплика.

    Пока подъём отдавал слово осевшей строке, на стыке оно пропадало на кадр:
    время реплики ASS округлено до сотых секунды, и граница не совпадает с
    кадром. Замер на рендере показал 0 пикселей текста на 0.52 с; проверка
    держит форму, из-за которой стыка больше нет."""
    for mode in ("reveal", "grow"):
        style = CaptionStyle(id=mode, animation=mode, reveal_ms=200)  # type: ignore[arg-type]
        settled, rising = _dialogues(style)
        # у каждой нижней реплики есть верхняя, которая кончается ровно с ней
        for i, line in enumerate(settled):
            end = line.split(",")[2]
            assert rising[2 * i + 1].split(",")[2] == end
        # и появляющееся слово в нижней реплике не рисуется вовсе
        assert all(r"\t(" not in x for x in settled)


def test_reveal_line_is_measured_from_where_the_word_starts() -> None:
    """Полоса субтитров считается по нижней точке подъёма: пока слово едет, оно
    ниже своего места, и в зону интерфейса площадки попадает именно там."""
    from videoforge.core.captions import caption_band

    calm = CaptionStyle(id="c", position="bottom")
    rising = CaptionStyle(id="r", position="bottom", animation="reveal", reveal_rise_pct=40)
    assert caption_band(rising, (1080, 1920))[1] > caption_band(calm, (1080, 1920))[1]


def test_pop_line_is_measured_by_the_top_of_the_spring() -> None:
    """Прыжок реально увеличивает строку — значит, и полоса субтитров выше.

    По этой полосе QA сверяет субтитры с зоной интерфейса площадки; посчитать
    её по кеглю в покое значило бы разрешить строке залезать под кнопки."""
    from videoforge.core.captions import caption_band

    calm = CaptionStyle(id="c", position="center")
    jumpy = CaptionStyle(id="j", position="center", animation="pop", pop_scale=130)
    top_calm, bottom_calm = caption_band(calm, (1080, 1920))
    top_pop, bottom_pop = caption_band(jumpy, (1080, 1920))
    assert bottom_pop - top_pop > bottom_calm - top_calm


def test_stretch_events_slows_the_tail_and_leaves_the_head_alone() -> None:
    """Замедленный хвост: реплики после точки замедления растягиваются вместе
    с речью, всё до неё не двигается — иначе субтитры уехали бы от голоса."""
    from videoforge.core.captions import stretch_events

    words = parse_srt_words(SRT)
    events = build_events([SceneWords("s1", 0.0, 4.0, words)], CaptionStyle(id="t"))
    slowed = stretch_events(events, from_sec=1.0, speed=0.5)
    assert slowed[0].start == events[0].start  # реплика до точки — как была
    last, was = slowed[-1], events[-1]
    assert last.start == round(1.0 + (was.start - 1.0) / 0.5, 3)
    # внутри реплики слова тоже растянуты: `ev.start` совпадает с первым словом
    assert last.words[0].start == last.start


def test_box_style_draws_a_backdrop_instead_of_outline() -> None:
    """Подложка — BorderStyle 3 и BackColour с альфой по непрозрачности; без неё
    стиль остаётся обводкой (BorderStyle 1) с прежним полупрозрачным BackColour."""
    scene = SceneWords("s01", 0.0, 3.0, parse_srt_words(SRT))
    plain = CaptionStyle(id="plain")
    boxed = CaptionStyle(id="boxed", box_opacity=0.6, box_color="#000000")
    ass_plain = build_ass(build_events([scene], plain), plain, size=(1080, 1920))
    ass_boxed = build_ass(build_events([scene], boxed), boxed, size=(1080, 1920))
    style_plain = next(ln for ln in ass_plain.splitlines() if ln.startswith("Style:"))
    style_boxed = next(ln for ln in ass_boxed.splitlines() if ln.startswith("Style:"))
    # BackColour — седьмое поле после имени, BorderStyle — шестнадцатое
    assert style_plain.split(",")[6] == "&H80000000" and style_plain.split(",")[15] == "1"
    assert style_boxed.split(",")[6] == "&H66000000" and style_boxed.split(",")[15] == "3"


ASS_ANIMATED = r"""[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.30,0:00:00.77,Default,,0,0,0,,{\pos(540,1498)}HE ASKS ME
Dialogue: 1,0:00:00.30,0:00:00.40,Default,,0,0,0,,{\move(540,1522,540,1504)}HE ASKS ME
Dialogue: 1,0:00:00.40,0:00:00.77,Default,,0,0,0,,{\move(540,1504,540,1498)}HE ASKS ME
Dialogue: 0,0:00:00.77,0:00:02.20,Default,,0,0,0,,{\pos(540,1498)}HE ASKS ME
Dialogue: 0,0:00:02.50,0:00:03.40,Default,,0,0,0,,{\k44}ЕСТЬ {\k46}СТЕНА
"""


def test_ass_is_read_back_as_lines_not_as_rows() -> None:
    """Готовый ASS → то, что зритель видит. Анимированный стиль перерисовывает
    одну реплику на каждом слове (на живом шортсе 234 строки на 28 реплик), и
    считать строки — значит соврать в разы. Соседние строки с тем же текстом —
    одна реплика от первого появления до последнего исчезновения."""
    from videoforge.core.captions import parse_ass_events

    events = parse_ass_events(ASS_ANIMATED)
    assert [e.text for e in events] == ["HE ASKS ME", "ЕСТЬ СТЕНА"]
    assert (events[0].start, events[0].end) == (0.3, 2.2)
    # караоке-теги — единственные пословные тайминги, которые в файле есть
    assert [(w.text, w.start) for w in events[1].words] == [("ЕСТЬ", 2.5), ("СТЕНА", 2.94)]


VTT_ROLLUP = """WEBVTT

00:00:10.000 --> 00:00:12.000
GO ON

00:00:12.000 --> 00:00:14.000
GO ON<00:00:12.500><c> DEEPER</c><00:00:13.200><c> THAN</c>
"""


def test_vtt_rollup_words_are_taken_by_their_own_timecode() -> None:
    """Пословные тайминги YouTube: реплики идут внахлёст и повторяют друг друга,
    поэтому слово считается новым по своему тайм-коду, а повтор отбрасывается."""
    from videoforge.core.captions import parse_vtt_words

    words = parse_vtt_words(VTT_ROLLUP)
    assert [w.text for w in words] == ["GO", "ON", "DEEPER", "THAN"]
    assert words[2].start == 12.5 and words[3].start == 13.2


VTT_FOREIGN = """WEBVTT
Kind: captions

00:00:10.000 --> 00:00:12.000
GO ON

00:03:36.000 --> 00:03:39.610
[Applause]
"""


def test_lyrics_file_from_another_recording_is_caught_by_its_length() -> None:
    """Файл слов длиннее трека — значит он от ДРУГОГО ролика.

    Стоило это собранного эдита: к треку 213.7 с лежал `.vtt` до 219.6 с,
    слова расходились с вокалом на секунды, а `check.md` объявлял приём
    проверенным. Субтитры не бывают длиннее своего видео — это и есть проверка,
    и она бесплатная. Считать надо по последней РЕПЛИКЕ: у живой записи хвост
    это аплодисменты без слов, и по словам файл выглядит короче, чем он есть."""
    from videoforge.core.captions import lyrics_problems, parse_vtt_words

    words = parse_vtt_words(VTT_FOREIGN)
    problems = lyrics_problems(VTT_FOREIGN, words, track_sec=213.7)
    assert any("от ДРУГОЙ записи" in p for p in problems)
    # тот же файл к своему треку длиной претензий не вызывает
    assert not any("ДРУГОЙ" in p for p in lyrics_problems(VTT_FOREIGN, words, track_sec=220.0))


def test_machine_transcription_is_named_machine() -> None:
    """Пометки распознавалки в файле — не текст песни, а догадка машины.
    Живой вокал она разбирает с пропусками, и выдавать её за замер нельзя."""
    from videoforge.core.captions import lyrics_problems, parse_vtt_words

    problems = lyrics_problems(VTT_FOREIGN, parse_vtt_words(VTT_FOREIGN), track_sec=220.0)
    assert any("машинная расшифровка" in p for p in problems)
    clean = VTT_FOREIGN.replace("Kind: captions", "").replace("[Applause]", "hey")
    assert not lyrics_problems(clean, parse_vtt_words(clean), track_sec=220.0)


def test_gradient_paints_the_line_in_stripes_and_is_read_back_as_one() -> None:
    """Градиента в ASS нет: реплика рисуется полосами `\\clip`, цвет заливки в
    каждой сдвинут к `gradient_to`. Верхняя полоса — цвет стиля, нижняя — цвет
    градиента, полосы закрывают кадр без дыр, а читается файл обратно всё той же
    одной репликой — иначе монтажный лист показал бы десять субтитров."""
    from videoforge.core.captions import GRADIENT_STRIPES, parse_ass_events

    style = CaptionStyle(
        id="g", color="#FFE600", highlight="#FFE600", gradient_to="#FF6A00",
        mode="phrase", max_words=1, animation="pop", position="center", margin_pct=0,
        uppercase=True,
    )  # fmt: skip
    words = parse_srt_words(SRT)[:2]
    events = build_events([SceneWords("s1", 0.0, 3.0, words)], style)
    ass = build_ass(events, style, size=(1080, 1920))
    first = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:") and "ТЫ" in ln]
    found = [re.search(r"\\clip\(0,(\d+),1080,(\d+)\)", ln) for ln in first]
    clips = [(int(m.group(1)), int(m.group(2))) for m in found if m]
    stripes = sorted(set(clips))
    assert len(stripes) == GRADIENT_STRIPES
    assert stripes[0][0] == 0 and stripes[-1][1] == 1920
    assert all(a[1] == b[0] for a, b in zip(stripes, stripes[1:], strict=False))  # без дыр
    top = next(ln for ln in first if "\\clip(0,0," in ln)
    bottom = next(ln for ln in first if ",1920)" in ln)
    assert "\\1c&H0000E6FF" in top and "\\1c&H00006AFF" in bottom  # BBGGRR
    assert [e.text for e in parse_ass_events(ass)] == ["ТЫ", "СТО"]

    plain = build_ass(events, style.model_copy(update={"gradient_to": ""}), size=(1080, 1920))
    assert "\\clip" not in plain


def test_one_word_style_drops_punctuation_at_the_word_edges() -> None:
    """Стилю «одно слово на экране» знаки на краях не нужны: «говорил.» →
    «ГОВОРИЛ». Внутри слова (апостроф, дефис) ничего не трогается, а без поля
    текст идёт как есть."""
    words = [Word("don't,", 0.0, 0.4), Word("«stop!»", 0.5, 0.9), Word("well-known.", 1.0, 1.5)]
    style = CaptionStyle(id="p", max_words=1, mode="phrase", strip_punctuation=True)
    events = build_events([SceneWords("s1", 0.0, 3.0, words)], style)
    ass = build_ass(events, style, size=(1080, 1920))
    assert "don't" in ass and "don't," not in ass
    assert "stop" in ass and "stop!" not in ass
    assert "well-known" in ass and "well-known." not in ass

    plain = build_ass(
        events, style.model_copy(update={"strip_punctuation": False}), size=(1080, 1920)
    )
    assert "don't," in plain
