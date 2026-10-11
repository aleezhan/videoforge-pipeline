"""Выбор моментов: привязка границ к предложениям, блок по длине и повтор,
слияние пересечений, heat, сквозной прогон на заглушке (highlights → pick →
эпизод со `source`), отчёт, свежесть."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from videoforge.clips import highlights as hl
from videoforge.clips.highlights import (
    CHUNK_MIN_COUNT,
    CRITIQUE_ITEM_CHARS,
    MAX_SEC,
    OVERSAMPLE,
    Candidate,
    Chunk,
    candidate_from_span,
    chunk_plan,
    chunk_quota,
    critique_batches,
    ends_thought,
    episode_from_candidate,
    format_clock,
    heat_at,
    interleave,
    load_highlights,
    parse_clock,
    parse_span,
    resolve_candidates,
    select_highlights,
    snap_end,
    snap_start,
    starts_thought,
)
from videoforge.clips.prepare import prepare_source
from videoforge.clips.report import render_highlights_report
from videoforge.clips.source import Chapter, HeatPoint, Sentence, SourceInfo, load_transcript
from videoforge.core.project import ProjectSpec, create_project
from videoforge.core.storyboard import load_storyboard
from videoforge.paths import source_dir
from videoforge.providers.base import CheckResult, LlmProvider, LlmResult, Reference
from videoforge.providers.mock import MockLlm, MockTranscriber

# ---------------------------------------------------------------- python без модели


def _sentences() -> list[Sentence]:
    """Диалог: короткий вопрос ведущего, длинный ответ, снова вопрос."""
    return [
        Sentence(start_sec=0.0, end_sec=4.0, text="Вступление.", chapter="Начало"),
        Sentence(start_sec=4.0, end_sec=8.0, text="Вопрос?", chapter="Начало", speaker_turn=True),
        Sentence(start_sec=8.0, end_sec=20.0, text="Ответ раз.", chapter="Начало"),
        Sentence(start_sec=20.0, end_sec=32.0, text="Ответ два.", chapter="Суть"),
        Sentence(start_sec=32.0, end_sec=44.0, text="Ответ три.", chapter="Суть"),
        Sentence(
            start_sec=44.0, end_sec=48.0, text="Ещё вопрос?", chapter="Суть", speaker_turn=True
        ),
        Sentence(start_sec=48.0, end_sec=70.0, text="Длинный ответ.", chapter="Суть"),
    ]


_CHAPTERS = [
    Chapter(title="Начало", start_sec=0, end_sec=20),
    Chapter(title="Суть", start_sec=20, end_sec=80),
]


def test_clock_parsing_accepts_what_models_actually_write() -> None:
    assert parse_clock("0:12:43") == 763.0
    assert parse_clock("12:43") == 763.0
    assert parse_clock(763) == 763.0
    assert parse_clock("763.5") == 763.5
    assert parse_clock("") is None and parse_clock("завтра") is None
    assert format_clock(763) == "0:12:43"


def test_bounds_snap_to_sentences_and_short_lead_in() -> None:
    """Начало — начало предложения; если оно в середине недавно начавшейся
    реплики, момент берёт реплику с начала. Конец — конец предложения."""
    resolved = resolve_candidates(
        {"candidates": [{"start": "0:00:10", "end": "0:00:40", "title": "Ответ", "why": "мок"}]},
        sentences=_sentences(),
        chapters=_CHAPTERS,
        heatmap=[],
    )
    assert not resolved.problems
    (cand,) = resolved.candidates
    # 10 с приходится на «Ответ раз» (8–20), реплика началась в 4 с — вопрос вошёл
    assert (cand.start_sec, cand.end_sec) == (4.0, 44.0)
    assert cand.text.startswith("Вопрос?") and cand.text.endswith("Ответ три.")
    assert cand.chapter == "Начало" and cand.speaker_turns == 0
    assert any("по предложениям" in f for f in resolved.fixes)

    exact = resolve_candidates(
        {"candidates": [{"start": "0:00:04", "end": "0:00:44", "why": "мок"}]},
        sentences=_sentences(),
        chapters=_CHAPTERS,
        heatmap=[],
    )  # границы уже по предложениям — починки нет, и писать о ней нечего
    assert exact.candidates and not exact.fixes and not exact.problems


def test_end_reaches_the_end_of_the_thought() -> None:
    """Панчлайн не остаётся за границей. Первый живой шортс кончался на «…кто
    Реалу забивает, я», а следующие 0.9 секунды «сразу в блейклист» были шуткой,
    ради которой момент и выбрали: кусок автосубтитров кончается по паузе, длине
    и смене говорящего, а не по мысли."""
    s = [
        Sentence(start_sec=0.0, end_sec=4.0, text="Начало."),
        Sentence(start_sec=4.0, end_sec=7.0, text="Я вот когда смотрю, кто забивает, я"),
        Sentence(start_sec=7.0, end_sec=8.0, text="сразу в блэклист."),
        Sentence(start_sec=9.0, end_sec=12.0, text="Другая тема."),
    ]
    assert ends_thought(s, 0) and not ends_thought(s, 1) and ends_thought(s, 2)
    assert snap_end(s, 7.0, first=0) == 2  # мысль дотянута до точки
    assert snap_end(s, 7.4, first=0) == 2  # назад граница не двигается никогда

    pause = [
        Sentence(start_sec=0.0, end_sec=3.0, text="без знака"),
        Sentence(start_sec=4.0, end_sec=6.0, text="дальше"),
    ]
    assert ends_thought(pause, 0) and snap_end(pause, 3.0, first=0) == 0  # пауза — тоже конец

    # речь без пауз и знаков не съедает момент целиком: дотягивание ограничено
    endless = [
        Sentence(start_sec=float(i), end_sec=float(i + 1), text=f"кусок {i}") for i in range(20)
    ]
    assert snap_end(endless, 2.0, first=0) == 4  # +3 с потолка, не больше


def test_start_does_not_land_on_the_tail_of_someone_elses_phrase() -> None:
    """Шортс на 65 секунд начинался со слова «там.» — хвоста фразы «…за нас
    пусть поболеет там», которой зритель не слышал."""
    s = [
        Sentence(start_sec=0.0, end_sec=5.8, text="пусть приедет, за нас пусть поболеет"),
        Sentence(start_sec=5.8, end_sec=6.7, text="там.", speaker_turn=True),
        Sentence(start_sec=6.7, end_sec=7.8, text="Ты же помнишь."),
        Sentence(start_sec=8.1, end_sec=10.8, text="О, я сам первый раз его увидел."),
    ]
    assert not starts_thought(s, 1) and starts_thought(s, 2)
    assert snap_start(s, 5.8) == 2  # обрывок пропущен, момент начинается с фразы
    assert snap_start(s, 8.1) == 3  # нормальное начало не двигаем

    # хвост, за которым сразу новая мысль, не тянет за собой полминуты
    endless = [
        Sentence(start_sec=float(i), end_sec=float(i + 1), text=f"кусок {i}") for i in range(20)
    ]
    assert snap_start(endless, 1.0) <= 4  # +3 с потолка, дальше начинаем как есть


def test_far_turn_start_does_not_swallow_the_whole_monologue() -> None:
    """Реплика, начавшаяся давно, начало не тянет: иначе момент из монолога
    растянулся бы до его начала и вылетел за длину."""
    sentences = [
        Sentence(start_sec=0.0, end_sec=6.0, text="Вопрос?", speaker_turn=True),
        *[
            Sentence(start_sec=6.0 + i * 10, end_sec=16.0 + i * 10, text=f"Кусок {i}.")
            for i in range(12)
        ],
    ]
    resolved = resolve_candidates(
        {"candidates": [{"start": "0:01:06", "end": "0:01:36", "why": "мок"}]},
        sentences=sentences,
        chapters=[],
        heatmap=[],
    )
    (cand,) = resolved.candidates
    assert cand.start_sec == 66.0 and cand.duration_sec <= MAX_SEC


def test_length_outside_limits_is_a_reason_to_ask_again() -> None:
    resolved = resolve_candidates(
        {
            "candidates": [
                {"start": "0:00:04", "end": "0:00:06", "title": "Коротыш", "why": "мок"},
                {"start": "0:00:04", "end": "0:00:44", "title": "Норм", "why": "мок"},
            ]
        },
        sentences=_sentences(),
        chapters=_CHAPTERS,
        heatmap=[],
    )
    assert [c.title for c in resolved.candidates] == ["Норм"]
    assert len(resolved.problems) == 1
    assert "Коротыш" in resolved.problems[0] and "20–75" in resolved.problems[0].replace("-", "–")


def test_broken_answers_do_not_crash_and_explain_themselves() -> None:
    resolved = resolve_candidates(
        {
            "candidates": [
                {"start": "потом", "end": "0:00:44"},
                {"start": "0:00:44", "end": "0:00:04"},
            ]
        },
        sentences=_sentences(),
        chapters=_CHAPTERS,
        heatmap=[],
    )
    assert not resolved.candidates and len(resolved.problems) == 2
    assert "не разобрать время" in resolved.problems[0]
    assert resolve_candidates([], sentences=_sentences(), chapters=[], heatmap=[]).candidates == []
    assert resolve_candidates({}, sentences=[], chapters=[], heatmap=[]).problems


def _even_sentences(count: int = 10, length: float = 10.0) -> list[Sentence]:
    return [
        Sentence(start_sec=i * length, end_sec=(i + 1) * length, text=f"Кусок {i}.")
        for i in range(count)
    ]


def test_opening_window_is_not_evidence() -> None:
    """Стартовое окно карты у YouTube всегда 1.0: его смотрят все, кто открыл
    видео. В heat кандидата оно не идёт — иначе первый попавшийся момент из
    начала записи возглавляет список, не имея за собой ни одного пересмотра."""
    heatmap = [
        HeatPoint(start_sec=0, end_sec=95, value=1.0),
        HeatPoint(start_sec=95, end_sec=190, value=0.4),
    ]
    assert heat_at(heatmap, 28, 51) == 0.0
    assert heat_at(heatmap, 100, 150) == 0.4


def test_overlapping_candidates_collapse_to_the_hotter_one() -> None:
    heatmap = [
        HeatPoint(start_sec=30, end_sec=60, value=0.2),
        HeatPoint(start_sec=60, end_sec=100, value=0.9),
    ]
    resolved = resolve_candidates(
        {
            "candidates": [
                {"start": "0:00:30", "end": "0:01:00", "title": "Холодный", "why": "мок"},
                {"start": "0:00:40", "end": "0:01:10", "title": "Горячий", "why": "мок"},
                {"start": "0:01:10", "end": "0:01:40", "title": "Другой", "why": "мок"},
            ]
        },
        sentences=_even_sentences(),
        chapters=[],
        heatmap=heatmap,
    )
    assert [c.title for c in resolved.candidates] == ["Горячий", "Другой"]  # холодный слит
    assert [c.id for c in resolved.candidates] == ["h01", "h02"]
    assert resolved.candidates[0].heat == 0.9
    assert any("пересекается" in f for f in resolved.fixes)
    assert heat_at(heatmap, 30, 40) == 0.2 and heat_at([], 0, 10) == 0.0


def test_limit_keeps_the_hottest() -> None:
    sentences = [
        Sentence(start_sec=i * 30.0, end_sec=i * 30.0 + 30.0, text=f"Кусок {i}.") for i in range(6)
    ]
    heatmap = [HeatPoint(start_sec=i * 30, end_sec=i * 30 + 30, value=i / 10) for i in range(6)]
    resolved = resolve_candidates(
        {
            "candidates": [
                {"start": format_clock(i * 30), "end": format_clock(i * 30 + 30), "why": "мок"}
                for i in range(6)
            ]
        },
        sentences=sentences,
        chapters=[],
        heatmap=heatmap,
        limit=2,
    )
    assert [c.heat for c in resolved.candidates] == [0.5, 0.4]
    assert any("оставлено 2" in f for f in resolved.fixes)


# ---------------------------------------------------------------- на заглушке


@pytest.fixture
def source(projects_root: Path) -> Path:
    """Проект нарезок с источником на заглушке: 10 минут ровной речи."""
    create_project(ProjectSpec(id="c", name="Нарезки", workflow="clips_from_long"))
    sdir = source_dir("c", "vid1")
    info = SourceInfo(
        id="vid1",
        url="https://www.youtube.com/watch?v=vid1",
        title="Подкаст",
        channel="Канал",
        duration_sec=600.0,
        chapters=[
            Chapter(title="Начало", start_sec=0, end_sec=300),
            Chapter(title="Суть", start_sec=300, end_sec=600),
        ],
        heatmap=[HeatPoint(start_sec=i * 60, end_sec=i * 60 + 60, value=i / 10) for i in range(10)],
    )
    prepare_source(sdir, info, MockTranscriber())
    return sdir


def _select(sdir: Path, llm: MockLlm, **kwargs: object) -> object:
    from videoforge.clips.source import load_source

    info = load_source(sdir)
    transcript = load_transcript(sdir)
    assert info is not None and transcript is not None
    return select_highlights(sdir, info=info, transcript=transcript, llm=llm, **kwargs)  # type: ignore[arg-type]


def test_blocked_candidate_costs_one_more_call_and_then_passes(source: Path) -> None:
    """Заглушка первым ответом даёт двухсекундный момент — блок; на повторе его
    нет. Так проверяется весь путь «блок → причина → исправленный ответ»."""
    llm = MockLlm()
    report = _select(source, llm)
    data = report.highlights  # type: ignore[attr-defined]
    # два захода на поиск + один на критика
    assert llm.highlight_calls == 3 and data.calls == 3
    assert not data.warnings and len(data.candidates) == 2
    assert all(20 <= c.duration_sec <= MAX_SEC for c in data.candidates)
    assert all(c.why and c.hook and c.text for c in data.candidates)
    assert [c.id for c in data.candidates] == ["h01", "h02"]
    assert data.candidates[0].heat >= data.candidates[1].heat
    # критик оставил моменты и упаковал их: заголовок, описание, хэштеги
    assert all(c.verdict == "keep" and c.verdict_why for c in data.candidates)
    assert all(c.publish_title and c.description and c.hashtags for c in data.candidates)
    assert load_highlights(source) is not None


def test_highlights_are_not_rechosen_without_reason(source: Path) -> None:
    llm = MockLlm()
    _select(source, llm)
    calls = llm.highlight_calls
    again = _select(source, llm)
    assert again.skipped and llm.highlight_calls == calls  # type: ignore[attr-defined]

    forced = _select(source, llm, force=True)
    assert not forced.skipped and llm.highlight_calls > calls  # type: ignore[attr-defined]

    # транскрипт переделали — прежний выбор устарел
    path = source / "transcript.json"
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 100))
    assert not _select(source, llm).skipped  # type: ignore[attr-defined]

    # другая модель — тоже
    data = load_highlights(source)
    assert data is not None and data.llm == "mock"


def test_mock_does_not_overwrite_a_real_selection(source: Path) -> None:
    """Заглушка не заменяет живой разбор — даже когда формально «устарел».

    Свежесть считается по имени модели, поэтому прогон с `--llm mock` поверх
    выбора, сделанного claude_cli, выглядел как законное обновление и молча
    клал мок-моменты на место часа работы. Восстановить нечем: выбор моментов
    недетерминирован, три прогона дают три разных набора."""
    llm = MockLlm()
    _select(source, llm)
    path = source / "highlights.json"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace('"llm":"mock"', '"llm":"claude_cli"')
        .replace('"llm": "mock"', '"llm": "claude_cli"'),
        encoding="utf-8",
    )
    data = load_highlights(source)
    assert data is not None and data.llm == "claude_cli"

    with pytest.raises(RuntimeError, match="заглушка"):
        _select(source, llm)
    assert load_highlights(source).llm == "claude_cli"  # type: ignore[union-attr]

    # человек сказал прямо — переписываем
    _select(source, llm, force=True)
    assert load_highlights(source).llm == "mock"  # type: ignore[union-attr]


def test_pick_makes_an_episode_out_of_a_source_segment(source: Path) -> None:
    llm = MockLlm()
    report = _select(source, llm)
    cand = report.highlights.candidates[0]  # type: ignore[attr-defined]
    edir = episode_from_candidate("c", "vid1", cand)
    assert edir.name == f"vid1_{cand.id}"
    sb = load_storyboard(edir)
    assert sb.workflow == "clips_from_long" and sb.title == cand.publish_title
    (scene,) = sb.scenes
    assert scene.source is not None and scene.source.path == "sources/vid1"
    assert (scene.source.start_sec, scene.source.end_sec) == (cand.start_sec, cand.end_sec)
    assert scene.duration_sec == cand.duration_sec and scene.narration == cand.text
    assert cand.hook in sb.brief
    # упаковка едет с моментом: публикации не придётся выдумывать её заново
    assert sb.publish.title == cand.publish_title
    assert sb.publish.description == cand.description and sb.publish.hashtags == cand.hashtags


def test_critic_closes_the_weak_and_keeps_the_reason(source: Path) -> None:
    """Отбор лучших, а не первый попавшийся список: критик читает моменты целиком
    и закрывает слабые с причиной. Закрытые не исчезают — человек вправе взять их
    по id."""
    from videoforge.clips.highlights import apply_verdicts

    cands = [
        Candidate(id=f"h{i:02d}", start_sec=i * 100.0, end_sec=i * 100.0 + 40, title=f"м{i}")
        for i in range(1, 4)
    ]
    raw = {
        "verdicts": [
            {"id": "h01", "keep": True, "why": "держит", "title": "Заголовок",
             "description": "Описание.", "hashtags": ["#Тег", " второй "]},
            {"id": "h02", "keep": False, "why": "зритель не поймёт, о ком речь"},
        ]
    }  # fmt: skip
    kept, dropped = apply_verdicts(cands, raw, limit=10)
    assert [c.id for c in kept] == ["h01", "h03"]  # про h03 критик промолчал — оставлен
    assert [c.id for c in dropped] == ["h02"]
    assert dropped[0].verdict == "drop" and "не поймёт" in dropped[0].verdict_why
    assert kept[0].publish_title == "Заголовок" and kept[0].hashtags == ["Тег", "второй"]
    assert kept[1].verdict == "" and not kept[1].publish_title  # молчание — не одобрение

    # ответ не по форме не роняет выбор: остаются все кандидаты
    same, none = apply_verdicts(cands, {"nope": 1}, limit=10)
    assert len(same) == 3 and not none
    # лимит режет хвост, а не порядок: он задан измеренным heat
    short, extra = apply_verdicts(cands, raw, limit=1)
    assert [c.id for c in short] == ["h01"] and [c.id for c in extra] == ["h02", "h03"]
    # и «не влез в число» не выглядит как «закрыт критиком»: h03 критика прошёл,
    # у него отличие одно — heat ниже. Без причины человек читает молчание как
    # приговор и не берёт годный момент
    overflow = next(c for c in extra if c.id == "h03")
    assert overflow.verdict == "" or overflow.verdict == "keep"
    assert "за пределами запрошенных" in overflow.verdict_why


def test_report_shows_hooks_and_the_replay_graph(source: Path) -> None:
    _select(source, MockLlm())
    path = render_highlights_report(source)
    page = path.read_text(encoding="utf-8")
    data = load_highlights(source)
    assert data is not None
    for cand in data.candidates:
        assert cand.hook in page and cand.id in page and cand.text[:40] in page
    assert "<svg" in page and "source pick c vid1" in page


def test_report_refuses_to_render_without_a_choice(source: Path) -> None:
    """Отчёт — просмотрщик: он читает файлы и ничего не выбирает сам."""
    assert load_highlights(source) is None
    with pytest.raises(FileNotFoundError):
        render_highlights_report(source)


def test_candidate_duration_is_derived_not_stored() -> None:
    cand = Candidate(id="h01", start_sec=10.0, end_sec=52.5)
    assert cand.duration_sec == 42.5


# ---------------------------------------------------------------- момент по времени


def test_span_parsing_reads_clocks_and_refuses_the_rest() -> None:
    """`--at` понимает то, что человек видит в плеере и в отчёте: часы, не секунды."""
    assert parse_span("20:41-21:33") == (1241.0, 1293.0)
    assert parse_span("1:20:41-1:21:33") == (4841.0, 4893.0)
    assert parse_span("0:05.5-0:10") == (5.5, 10.0)
    assert parse_span("20:41 – 21:33") == (1241.0, 1293.0)  # тире, скопированное из отчёта
    for bad in ("20:41", "1241-1293", "abc-def", "20:41-", "-20:41-21:33"):
        with pytest.raises(ValueError, match="20:41"):
            parse_span(bad)
    with pytest.raises(ValueError, match="не позже начала"):
        parse_span("21:33-20:41")
    with pytest.raises(ValueError, match="не позже начала"):
        parse_span("21:33-21:33")


def test_span_from_a_human_snaps_exactly_like_a_model_candidate() -> None:
    """Отрезок руками проходит ту же привязку, что ответ модели: начало — к
    началу предложения и к началу недавней реплики, конец — к концу
    предложения. Иначе момент по времени начинался бы с обрывка чужой фразы,
    от которого кандидатов модели уже вылечили."""
    cand, notes = candidate_from_span(
        10.0, 40.0, title=" Ответ ", sentences=_sentences(), chapters=_CHAPTERS, heatmap=[]
    )
    (model,) = resolve_candidates(
        {"candidates": [{"start": "0:00:10", "end": "0:00:40", "why": "мок"}]},
        sentences=_sentences(),
        chapters=_CHAPTERS,
        heatmap=[],
    ).candidates
    assert (cand.start_sec, cand.end_sec) == (model.start_sec, model.end_sec) == (4.0, 44.0)
    assert cand.text == model.text and cand.chapter == model.chapter == "Начало"
    assert cand.id == "at0010" and cand.title == "Ответ"
    # критика не было — запись говорит это прямо, а не изображает hook
    assert "по времени" in cand.why and cand.hook == cand.why
    assert not cand.publish_title and not cand.verdict and not cand.description
    assert notes == ["границы 0:00:10–0:00:40 → 0:00:04–0:00:44 (по предложениям)"]

    exact, notes = candidate_from_span(
        4.0, 44.0, title="Ровно", sentences=_sentences(), chapters=_CHAPTERS, heatmap=[]
    )
    assert exact.duration_sec == 40.0 and notes == []  # ни сдвига, ни предупреждения


def test_span_length_outside_limits_warns_but_still_makes_the_moment() -> None:
    """У модели 84 секунды — блок и повтор; у человека — предупреждение с числом:
    он выбирал сам, и у него могут быть причины."""
    long_, notes = candidate_from_span(
        0.0, 90.0, title="Длинный", sentences=_even_sentences(), chapters=[], heatmap=[]
    )
    assert long_.duration_sec == 90.0
    assert notes == ["ПРОВЕРЬ: 90 с — момент должен длиться 20–75 с"]
    short, notes = candidate_from_span(
        0.0, 8.0, title="Короткий", sentences=_even_sentences(), chapters=[], heatmap=[]
    )
    assert short.duration_sec == 10.0
    assert any(n.startswith("ПРОВЕРЬ") and "10 с" in n for n in notes)


def test_span_needs_a_title_and_a_transcript_under_it() -> None:
    with pytest.raises(ValueError, match="title"):
        candidate_from_span(0.0, 30.0, title=" ", sentences=_sentences(), chapters=[], heatmap=[])
    with pytest.raises(ValueError, match="кончается"):
        candidate_from_span(
            100.0, 130.0, title="х", sentences=_sentences(), chapters=[], heatmap=[]
        )
    with pytest.raises(ValueError, match="пустой"):
        candidate_from_span(0.0, 30.0, title="х", sentences=[], chapters=[], heatmap=[])


def test_pick_by_time_makes_an_episode_without_a_highlights_run(source: Path) -> None:
    """Момент, которого нет в разборе — или разбора ещё нет, — берётся по времени
    с заголовком от человека. Упаковки у эпизода нет: её спросит `publish`, а
    заголовок автора увидит в промпте."""
    from typer.testing import CliRunner

    from videoforge.api.data import _sources_rows
    from videoforge.cli import app
    from videoforge.paths import episode_dir

    runner = CliRunner()
    assert load_highlights(source) is None  # разбор не нужен
    result = runner.invoke(
        app, ["source", "pick", "c", "vid1", "--at", "1:00-1:40", "--title", "Момент руками"]
    )
    assert result.exit_code == 0, result.output
    edir = episode_dir("c", "vid1_at0100")
    assert (
        edir.is_dir() and "c/vid1_at0100" in result.output and "run c/vid1_at0100" in result.output
    )
    sb = load_storyboard(edir)
    assert sb.title == "Момент руками" and sb.workflow == "clips_from_long"
    assert "по времени" in sb.brief
    (scene,) = sb.scenes
    assert scene.source is not None and scene.source.path == "sources/vid1"
    assert scene.source.start_sec <= 60.0 < 100.0 <= scene.source.end_sec  # к предложениям
    assert scene.duration_sec == round(scene.source.end_sec - scene.source.start_sec, 2)
    assert scene.narration.startswith("слово")
    assert not sb.publish.title and not sb.publish.description
    assert "ПРОВЕРЬ" not in result.output  # 40 с с запасом на привязку — в норме

    # то же начало второй раз — отказ с причиной, а не молчаливая перезапись
    again = runner.invoke(
        app, ["source", "pick", "c", "vid1", "--at", "1:00-1:40", "--title", "Ещё раз"]
    )
    assert again.exit_code == 1 and "уже" in again.output
    assert load_storyboard(edir).title == "Момент руками"

    # длина вне 20–75 — эпизод делается, число человек видит
    long_ = runner.invoke(
        app, ["source", "pick", "c", "vid1", "--at", "3:00-4:30", "--title", "Длинный"]
    )
    assert long_.exit_code == 0, long_.output
    assert "ПРОВЕРЬ" in long_.output and "20–75" in long_.output
    assert episode_dir("c", "vid1_at0300").is_dir()

    # пульт: строка источника собирается и без разбора, с эпизодами по времени рядом
    (row,) = _sources_rows("c")
    assert row["id"] == "vid1" and row["kind"] == "clips" and row["highlights"] is None


def test_pick_by_time_and_by_id_do_not_mix(source: Path) -> None:
    """Либо кандидат из разбора, либо отрезок; `--title` — только с отрезком."""
    from typer.testing import CliRunner

    from videoforge.cli import app
    from videoforge.paths import episode_dir

    runner = CliRunner()
    both = runner.invoke(
        app, ["source", "pick", "c", "vid1", "h01", "--at", "1:00-1:40", "--title", "x"]
    )
    assert both.exit_code == 1 and "либо" in both.output
    no_title = runner.invoke(app, ["source", "pick", "c", "vid1", "--at", "1:00-1:40"])
    assert no_title.exit_code == 1 and "--title" in no_title.output
    backwards = runner.invoke(
        app, ["source", "pick", "c", "vid1", "--at", "1:40-1:00", "--title", "x"]
    )
    assert backwards.exit_code == 1 and "не позже начала" in backwards.output
    stray = runner.invoke(app, ["source", "pick", "c", "vid1", "h01", "--title", "x"])
    assert stray.exit_code == 1 and "--at" in stray.output
    nothing = runner.invoke(app, ["source", "pick", "c", "vid1"])
    assert nothing.exit_code == 1 and "--at" in nothing.output
    assert not episode_dir("c", "vid1_at0100").exists()


# ---------------------------------------------------------------- куски транскрипта


def _chaptered(
    counts: tuple[int, ...] = (3, 3, 3), *, chars: int = 100
) -> tuple[list[Sentence], list[Chapter]]:
    """Главы A, B, C… по `counts` предложений: каждое 10 с, текст ровно `chars`
    знаков — строка в промпте выходит на `chars + len(главы) + 14`."""
    sentences: list[Sentence] = []
    chapters: list[Chapter] = []
    i = 0
    for title, n in zip("ABCDEFG", counts, strict=False):
        chapters.append(Chapter(title=title, start_sec=i * 10.0, end_sec=(i + n) * 10.0))
        for _ in range(n):
            sentences.append(
                Sentence(
                    start_sec=i * 10.0, end_sec=i * 10.0 + 9.0, text="x" * chars, chapter=title
                )
            )
            i += 1
    return sentences, chapters


def _coverage(chunks: list[Chunk]) -> list[tuple[int, int]]:
    return [(c.own_first, c.last) for c in chunks]


def test_short_transcript_is_one_chunk_without_overlap() -> None:
    """Влезает целиком — один кусок, и всё как до кусков: полная квота,
    окно — весь транскрипт, перекрытия нет."""
    (chunk,) = chunk_plan(_sentences(), _CHAPTERS)
    assert (chunk.index, chunk.first, chunk.own_first, chunk.last) == (1, 0, 0, 6)
    assert (chunk.start_sec, chunk.own_start_sec, chunk.end_sec) == (0.0, 0.0, 70.0)
    assert chunk_quota(chunk, total_chars=chunk.chars, count=12) == 12 * OVERSAMPLE
    assert chunk_plan([], []) == []


def test_chunks_follow_chapters_and_merge_while_they_fit() -> None:
    """Граница куска — начало главы; соседние главы сливаются, пока влезают
    (тридцать девять глав — не тридцать девять вызовов)."""
    sentences, chapters = _chaptered((3, 3, 3))  # строка 115 знаков, глава 345
    two = chunk_plan(sentences, chapters, max_chars=700, overlap_sec=15)  # A+B влезают, C — нет
    assert _coverage(two) == [(0, 5), (6, 8)] and [c.index for c in two] == [1, 2]
    assert sentences[two[1].own_first].chapter == "C" and sentences[two[0].last].chapter == "B"
    assert _coverage(chunk_plan(sentences, chapters, max_chars=400)) == [(0, 2), (3, 5), (6, 8)]
    # своих знаков в сумме ровно столько, сколько в транскрипте: перекрытие не считается дважды
    assert sum(c.chars for c in two) == sum(len(s.text) + len(s.chapter) + 14 for s in sentences)


def test_chapter_longer_than_the_limit_is_cut_by_sentences() -> None:
    """Глава B из шести предложений (690 знаков) в 500 не влезает — режется по
    предложениям, а её хвост склеивается со следующей главой."""
    sentences, chapters = _chaptered((2, 6, 1))
    chunks = chunk_plan(sentences, chapters, max_chars=500, overlap_sec=15)
    assert _coverage(chunks) == [(0, 1), (2, 5), (6, 8)]
    assert all(c.chars <= 500 for c in chunks)


def test_without_chapters_chunks_go_by_sentences_up_to_the_limit() -> None:
    sentences = [
        Sentence(start_sec=i * 10.0, end_sec=i * 10.0 + 9.0, text="x" * 100) for i in range(9)
    ]
    chunks = chunk_plan(sentences, [], max_chars=500)  # строка 114 знаков: по четыре
    assert _coverage(chunks) == [(0, 3), (4, 7), (8, 8)]
    assert all(c.chars <= 500 for c in chunks)
    # без единой дыры и без наложения территорий
    assert all(b.own_first == a.last + 1 for a, b in zip(chunks, chunks[1:], strict=False))


def test_neighbouring_chunks_overlap_by_the_tail_of_the_previous_one() -> None:
    """Окно куска начинается на `overlap_sec` раньше своей территории, по
    началу предложения; у первого куска перекрытия нет."""
    sentences, chapters = _chaptered((3, 3, 3))
    chunks = chunk_plan(sentences, chapters, max_chars=400, overlap_sec=15)
    first, second, third = chunks
    assert first.first == first.own_first == 0 and first.start_sec == first.own_start_sec
    assert second.first == second.own_first - 1  # предложение за 10 с назад — да, за 20 — нет
    assert second.start_sec == sentences[second.first].start_sec == 20.0
    assert second.own_start_sec == 30.0 and third.first == 5
    assert all(c.own_start_sec - c.start_sec <= 15 for c in chunks)
    # перекрытие по умолчанию (90 с) у коротких предложений уходит к самому началу
    wide = chunk_plan(sentences, chapters, max_chars=400)
    assert wide[1].first == 0 and wide[1].own_first == 3


def test_chunk_quota_is_proportional_with_a_floor() -> None:
    def chunk(chars: int) -> Chunk:
        return Chunk(
            index=1, first=0, own_first=0, last=0, chars=chars,
            start_sec=0, own_start_sec=0, end_sec=0,
        )  # fmt: skip

    assert chunk_quota(chunk(1000), total_chars=1000, count=12) == 24
    assert chunk_quota(chunk(100), total_chars=1000, count=12) == 3  # 2.4 → вверх
    assert chunk_quota(chunk(10), total_chars=1000, count=12) == CHUNK_MIN_COUNT


def test_interleave_takes_the_best_of_every_chunk_first() -> None:
    def cand(name: str) -> Candidate:
        return Candidate(id=name, start_sec=0, end_sec=1, title=name)

    a1, a2, a3, b1, c1, c2 = (cand(n) for n in ("a1", "a2", "a3", "b1", "c1", "c2"))
    mixed = interleave([[a1, a2, a3], [b1], [c1, c2]])
    assert [c.title for c in mixed] == ["a1", "b1", "c1", "a2", "c2", "a3"]
    assert interleave([[a1, a2]]) == [a1, a2] and interleave([]) == []


def test_critic_batches_split_by_text_and_keep_order() -> None:
    cands = [
        Candidate(id=f"h{i:02d}", start_sec=i * 100.0, end_sec=i * 100.0 + 40, text="т" * 300)
        for i in range(1, 6)
    ]
    limit = 2 * (300 + CRITIQUE_ITEM_CHARS)  # ровно два кандидата в пачку
    batches = critique_batches(cands, max_chars=limit)
    assert [[c.id for c in b] for b in batches] == [["h01", "h02"], ["h03", "h04"], ["h05"]]
    assert critique_batches(cands, max_chars=100_000) == [cands]  # влезли — одна пачка, как было
    cands[2].text = "т" * 5000  # длиннее лимита — своей пачкой, а не потерян
    assert [len(b) for b in critique_batches(cands, max_chars=limit)] == [2, 1, 2]


def test_prompt_names_the_part_and_its_overlap() -> None:
    from videoforge.clips.highlight_prompts import highlights_prompt

    sentences, chapters = _chaptered((3, 3, 3))
    info = SourceInfo(id="x", title="Стрим", duration_sec=90.0, chapters=chapters)
    chunks = chunk_plan(sentences, chapters, max_chars=400, overlap_sec=15)

    def prompt_for(window: list[Sentence], part: tuple[Chunk, int] | None) -> str:
        return highlights_prompt(
            info=info, sentences=window, peaks=[], rules="", blueprint="", platforms=[],
            target_duration_sec=60, language="ru", count=4, part=part,
        )  # fmt: skip

    prompt = prompt_for(sentences[chunks[1].first : chunks[1].last + 1], (chunks[1], 3))
    assert "PART 2 OF 3: 0:00:20–0:00:59 of a 0:01:30 recording" in prompt
    assert "before 0:00:30\nrepeat the tail of part 1" in prompt
    assert "over the whole\n  part" in prompt and prompt.count("[0:00:") == 4  # 1 + 3 строки
    whole = prompt_for(sentences, None)
    assert "PART" not in whole and "over the whole\n  recording" in whole


class _WindowLlm(LlmProvider):
    """Модель-заглушка для кусков: на широкий поиск отдаёт из ЗАДАННОГО списка
    моментов те, что целиком внутри окна из блока PART, критику — оставляет
    всех. Так проверяется и что окно в промпте есть, и что момент на шве не
    теряется и не удваивается. Время — числом: часы промпта режут доли секунды,
    а сдвиг на предложение спутал бы, кто с кем пересёкся."""

    name = "mock-windows"

    def __init__(self, moments: list[tuple[float, float]]) -> None:
        self.moments = moments
        self.windows: list[tuple[float, float]] = []  # окна, которые получил поиск
        self.critiques = 0

    def check_images(
        self,
        images: list[Path],
        *,
        references: list[Reference],
        checklist: list[str],
        context: str,
    ) -> CheckResult:
        raise NotImplementedError("выбор моментов кадры не проверяет")

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        images: list[Path] | None = None,
    ) -> LlmResult:
        if prompt.startswith("VIDEOFORGE-STEP: highlights_critique"):
            self.critiques += 1
            ids = re.findall(r"^### (\S+) ", prompt, re.M)
            verdicts = [
                {"id": i, "keep": True, "why": "мок", "title": f"T {i}", "description": "d",
                 "hashtags": ["мок"]}
                for i in ids
            ]  # fmt: skip
            return LlmResult(text=json.dumps({"verdicts": verdicts}))
        m = re.search(r"^PART (\d+) OF (\d+): (\d+:\d+:\d+)–(\d+:\d+:\d+) ", prompt, re.M)
        assert m, "у куска обязан быть блок PART"
        lo, hi = parse_clock(m.group(3)), parse_clock(m.group(4))
        assert lo is not None and hi is not None
        self.windows.append((lo, hi))
        inside = [(s, e) for s, e in self.moments if lo <= s and e <= hi + 1]
        cands = [
            {"start": s, "end": e, "title": f"м{format_clock(s)}", "hook": "х", "why": "мок",
             "kind": "story"}
            for s, e in inside
        ]  # fmt: skip
        return LlmResult(text=json.dumps({"candidates": cands}))


def test_wide_search_runs_once_per_chunk_and_merges_the_seams(
    source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Десять минут заглушки (97 предложений по 6.2 с) при лимите 3000 знаков —
    четыре куска по 20–28 предложений с перекрытием в 14. У каждого свой
    вызов; момент, севший на шов, виден следующему куску целиком и в списке
    один; момент в перекрытии назван обоими кусками и слит в один. Критик при
    том же лимите читает пачками, и вердикт есть у всех."""
    from videoforge.clips.source import load_source

    default = hl.CHUNK_MAX_CHARS
    monkeypatch.setattr(hl, "CHUNK_MAX_CHARS", 3000)
    info, transcript = load_source(source), load_transcript(source)
    assert info is not None and transcript is not None
    s = transcript.sentences
    chunks = chunk_plan(s, info.chapters, max_chars=3000)
    assert len(chunks) == 4 and all(c.own_first - c.first == 14 for c in chunks[1:])
    assert all(c.last - c.own_first >= 19 for c in chunks)

    def span(a: int, b: int) -> tuple[float, float]:
        return s[a].start_sec, s[b].end_sec  # шесть предложений ≈ 36 с, семь ≈ 42 с

    own = [span(c.own_first + 4, c.own_first + 9) for c in chunks]
    seams = [span(c.own_first - 3, c.own_first + 3) for c in chunks[1:]]  # через шов
    dups = [span(c.own_first - 11, c.own_first - 5) for c in chunks[1:]]  # в перекрытии
    llm = _WindowLlm(own + seams + dups)
    report = select_highlights(source, info=info, transcript=transcript, llm=llm, count=12)
    data = report.highlights

    assert llm.windows == [(float(int(c.start_sec)), float(int(c.end_sec))) for c in chunks]
    assert data.chunks == 4 and llm.critiques > 1
    assert data.calls == len(chunks) + llm.critiques and not data.warnings
    got = {(c.start_sec, c.end_sec) for c in data.candidates}
    assert got == set(own + seams + dups)  # ничего не потеряно…
    assert len(data.candidates) == len(got) == 10  # …и ничего не удвоено
    assert sum("пересекается" in f for f in data.fixes) == len(dups)
    assert all(c.verdict == "keep" and c.publish_title for c in data.candidates)
    assert [c.id for c in data.candidates] == [f"h{i:02d}" for i in range(1, 11)]
    saved = load_highlights(source)
    assert saved is not None and saved.chunks == 4

    # штатная заглушка по кускам: страж окна PART не срабатывает на настоящем
    # промпте, короткий мок-момент каждого куска чинится повтором своего куска
    mock = MockLlm()
    chunked = select_highlights(source, info=info, transcript=transcript, llm=mock, force=True)
    assert chunked.highlights.chunks == 4 and not chunked.highlights.warnings
    assert mock.highlight_calls >= 2 * len(chunks) + 1

    # тот же источник при обычном лимите — один кусок, поведение прежнее
    monkeypatch.setattr(hl, "CHUNK_MAX_CHARS", default)
    plain = select_highlights(source, info=info, transcript=transcript, llm=MockLlm(), force=True)
    assert plain.highlights.chunks == 1 and plain.highlights.calls == 3


def test_report_says_there_is_no_replay_graph_for_a_local_file(projects_root: Path) -> None:
    """У локального файла карты пересмотров не бывает: отчёт говорит это словами,
    а не пустым графиком, и не падает. У ролика с YouTube без карты — своя причина."""
    create_project(ProjectSpec(id="c", name="Нарезки", workflow="clips_from_long"))
    local = source_dir("c", "stream")
    file = str(projects_root / "stream.mp4")
    prepare_source(
        local,
        SourceInfo(id="stream", file=file, title="Стрим", duration_sec=600.0),
        MockTranscriber(),
    )
    _select(local, MockLlm())
    page = render_highlights_report(local).read_text(encoding="utf-8")
    assert "карты пересмотров нет (локальный файл)" in page
    assert "как часто пересматривали" not in page and "<svg" in page  # кандидаты на шкале остались
    data = load_highlights(local)
    assert data is not None and data.candidates and all(c.heat == 0.0 for c in data.candidates)

    online = source_dir("c", "vid2")
    prepare_source(
        online,
        SourceInfo(id="vid2", url="https://www.youtube.com/watch?v=vid2", duration_sec=600.0),
        MockTranscriber(),
    )
    _select(online, MockLlm())
    page = render_highlights_report(online).read_text(encoding="utf-8")
    assert "карты пересмотров нет (YouTube её не отдал)" in page
