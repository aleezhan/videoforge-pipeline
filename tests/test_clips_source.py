"""Источник для нарезок: json3 → слова, выбор дорожки, предложения, подготовка
источника на заглушке, workflow проекта."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_test_video
from typer.testing import CliRunner

import videoforge.providers.whisper_local as whisper_local
from videoforge.cli import app
from videoforge.clips.prepare import prepare_source
from videoforge.clips.source import (
    TRANSCRIPT_FILE,
    WORDS_FILE,
    Chapter,
    HeatPoint,
    SourceInfo,
    list_sources,
    load_source,
    load_transcript,
    sentences_from_words,
    top_peaks,
)
from videoforge.core.captions import Word, parse_srt_words, words_to_srt
from videoforge.core.project import ProjectSpec, create_episode, create_project, load_project
from videoforge.paths import source_dir, sources_dir
from videoforge.providers.base import ProviderError
from videoforge.providers.mock import MockTranscriber
from videoforge.providers.registry import transcriber_for
from videoforge.providers.whisper_local import WhisperLocal
from videoforge.providers.youtube import json3_to_words, pick_caption_track, source_info_from


def test_source_info_keeps_chapters_and_replay_heatmap() -> None:
    raw = {
        "id": "abc",
        "webpage_url": "https://www.youtube.com/watch?v=abc",
        "title": "Подкаст",
        "duration": 120,
        "language": "ru-orig",
        "chapters": [{"title": "Старт", "start_time": 0, "end_time": 60}],
        "heatmap": [
            {"start_time": 0, "end_time": 1.2, "value": 1.0},  # стартовый пик — не момент
            {"start_time": 40, "end_time": 41.2, "value": 0.3},
            {"start_time": 90, "end_time": 91.2, "value": 0.7},
        ],
    }
    info = source_info_from(raw)
    assert info.language == "ru" and [c.title for c in info.chapters] == ["Старт"]
    assert len(info.heatmap) == 3
    peaks = top_peaks(info.heatmap, 2)
    assert [(p.start_sec, p.value) for p in peaks] == [(90.0, 0.7), (40.0, 0.3)]
    assert top_peaks([HeatPoint(start_sec=0, end_sec=1, value=1.0)]) == []


def test_json3_words_and_srt_roundtrip() -> None:
    payload = {
        "events": [
            {"tStartMs": 0, "dDurationMs": 5000, "segs": [{"utf8": "\n"}]},  # служебный
            {
                "tStartMs": 1000,
                "dDurationMs": 2000,
                "segs": [
                    {"utf8": "Почему", "tOffsetMs": 0},
                    {"utf8": " сияние", "tOffsetMs": 600},
                    {"utf8": " только", "tOffsetMs": 1100},
                ],
            },
            {
                "tStartMs": 9000,
                "dDurationMs": 3000,
                "segs": [{"utf8": ">> у полюсов", "tOffsetMs": 0}],  # смена говорящего
            },
            {  # звук, а не речь: в караоке ему делать нечего
                "tStartMs": 13000,
                "dDurationMs": 1000,
                "segs": [{"utf8": "[смех]", "tOffsetMs": 0}],
            },
            {  # пометка из двух слов приходит двумя сегментами, ещё и слипшейся
                "tStartMs": 15000,
                "dDurationMs": 2000,
                "segs": [
                    {"utf8": "[вздыхает][тяжело", "tOffsetMs": 0},
                    {"utf8": " вздыхает]", "tOffsetMs": 500},
                    {"utf8": " дальше", "tOffsetMs": 1000},
                ],
            },
        ]
    }
    words = json3_to_words(payload)
    # звуковые пометки выброшены целиком — даже разорванные по сегментам
    assert [w.text for w in words] == ["Почему", "сияние", "только", "у", "полюсов", "дальше"]
    words = words[:-1]
    assert [w.turn for w in words] == [False, False, False, True, False]  # маркер стал признаком
    assert (words[0].start, words[0].end) == (1.0, 1.6)  # конец — начало следующего слова
    assert words[2].end == 3.0  # последнее слово реплики — до конца реплики
    # реплика ручных субтитров: 1.5 с потолка делятся между двумя словами поровну
    assert (words[3].end, words[4].start, words[4].end) == (9.75, 9.75, 10.5)
    srt = words_to_srt(words)
    back = parse_srt_words(srt)
    assert [(w.text, round(w.start, 3), round(w.end, 3), w.turn) for w in back] == [
        (w.text, round(w.start, 3), round(w.end, 3), w.turn) for w in words
    ]  # смена говорящего переживает запись в .srt и чтение обратно
    assert "\n>>\n" in srt and ">>" not in [w.text for w in back]


def test_caption_track_prefers_auto_in_the_spoken_language() -> None:
    subs = {"en": [], "ru": []}
    autos = {"ru": [], "ru-orig": [], "en": [], "de-abc": []}
    assert pick_caption_track("ru", subs, autos) == ("ru-orig", "auto")  # оригинал, не перевод
    assert pick_caption_track("de", subs, autos) == ("de-abc", "auto")
    assert pick_caption_track("ru", subs, {}) == ("ru", "manual")  # ручные — запасной путь
    assert pick_caption_track("fr", subs, autos) is None  # чужой язык не берём


def test_sentences_split_on_pause_length_and_punctuation() -> None:
    words = [Word(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(10)]  # ровная речь
    words[4] = Word("конец.", 2.0, 2.4)  # точка
    words[7] = Word("другой", 3.5, 3.9, turn=True)  # смена говорящего
    words += [Word("после", 8.0, 8.4), Word("паузы", 8.5, 8.9)]  # пауза > 0.8 с
    chapters = [
        Chapter(title="Вступление", start_sec=0, end_sec=5),
        Chapter(title="Тема", start_sec=5, end_sec=20),
    ]
    sentences = sentences_from_words(words, chapters, max_words=4)
    texts = [s.text for s in sentences]
    assert texts[0] == "w0 w1 w2 w3"  # по длине
    assert texts[1] == "конец."  # точка закрывает предложение
    assert texts[2] == "w5 w6" and texts[3] == "другой w8 w9"  # смена говорящего
    assert sentences[3].speaker_turn and not sentences[2].speaker_turn
    assert texts[-1] == "после паузы" and sentences[-1].chapter == "Тема"
    assert sentences[0].chapter == "Вступление"


def _clips_project(projects_root: Path) -> Path:
    create_project(ProjectSpec(id="c", name="Нарезки", workflow="clips_from_long"))
    return projects_root / "c"


def test_prepare_source_is_idempotent(projects_root: Path) -> None:
    _clips_project(projects_root)
    info = SourceInfo(
        id="vid1",
        url="https://www.youtube.com/watch?v=vid1",
        title="Подкаст",
        duration_sec=30.0,
        chapters=[
            Chapter(title="Начало", start_sec=0, end_sec=15),
            Chapter(title="Суть", start_sec=15, end_sec=30),
        ],
    )
    mock = MockTranscriber()
    sdir = source_dir("c", "vid1")
    report = prepare_source(sdir, info, mock)
    assert report.steps == ["transcript", "sentences"] and report.cost_usd == 0
    assert (sdir / WORDS_FILE).exists() and (sdir / TRANSCRIPT_FILE).exists()
    saved = load_source(sdir)
    assert saved is not None and saved.transcriber == "mock" and saved.word_count > 40
    transcript = load_transcript(sdir)
    assert transcript is not None and len(transcript.sentences) >= 4
    assert {s.chapter for s in transcript.sentences} == {"Начало", "Суть"}
    assert [s.id for s in list_sources(sources_dir("c"))] == ["vid1"]

    again = prepare_source(sdir, info.model_copy(update={"views": 5}), mock)
    assert again.steps == [] and mock.calls == 1  # ничего не переделано
    forced = prepare_source(sdir, info, mock, force=True)
    assert forced.steps == ["transcript", "sentences"] and mock.calls == 2


def test_prepare_source_fails_on_empty_transcript(projects_root: Path, tmp_path: Path) -> None:
    _clips_project(projects_root)
    info = SourceInfo(id="vid2", url="https://www.youtube.com/watch?v=vid2", duration_sec=0.1)
    with pytest.raises(ProviderError, match="пустой"):
        prepare_source(source_dir("c", "vid2"), info, MockTranscriber())


def test_transcriber_follows_the_target() -> None:
    """Ready word-level captions exist only on YouTube; a file on disk is
    transcribed by Whisper. Nothing for the user to choose."""
    for url in (
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://m.youtube.com/shorts/abc",
    ):
        assert transcriber_for(url) == "youtube"
    for target in (
        r"D:\video\episode.mp4",
        "/home/u/episode.mp4",
        "https://notyoutube.com/watch?v=abc",
    ):
        assert transcriber_for(target) == "whisper"


def test_source_add_takes_a_file_from_disk(projects_root: Path, tmp_path: Path) -> None:
    """Файл на диске — источник без сети: размер и длительность читает ffmpeg,
    транскрипт — по звуку (здесь заглушка), cut режет файл на месте. Второй
    запуск с auto не переделывает транскрипт другим транскрибером."""
    _clips_project(projects_root)
    video = make_test_video(tmp_path / "episode.mp4", seconds=12, size="320x180")
    result = CliRunner().invoke(app, ["source", "add", "c", str(video), "--transcriber", "mock"])
    assert result.exit_code == 0, result.output
    info = load_source(source_dir("c", "episode"))
    assert info is not None and info.file == str(video.resolve()) and info.url == ""
    assert (info.width, info.height) == (320, 180) and 11 < info.duration_sec < 13
    assert info.transcriber == "mock" and info.word_count > 0

    again = CliRunner().invoke(app, ["source", "add", "c", str(video)])
    assert again.exit_code == 0, again.output
    assert "транскрибер: mock" in again.output and "всё уже было" in again.output


def test_whisper_listens_to_a_file_or_fetches_the_sound_of_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ссылке без субтитров whisper качает звук сам, рядом с транскриптом, а
    время счёта на процессоре называет до запуска — по замеру, не на глаз."""
    fetched: list[str] = []

    def fake_download(url: str, out_path: Path) -> Path:
        fetched.append(url)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"")
        return out_path

    monkeypatch.setattr(whisper_local, "download_audio", fake_download)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    assert WhisperLocal.audio_source("https://x/1", video, tmp_path) == video
    link = "https://www.youtube.com/watch?v=own1"
    got = WhisperLocal.audio_source(link, None, tmp_path / "src")
    assert got == tmp_path / "src" / whisper_local.AUDIO_FILE and fetched == [link]
    with pytest.raises(ValueError, match="ни то ни другое"):
        WhisperLocal.audio_source("", None, tmp_path)
    assert WhisperLocal(device="cpu").estimate_sec(100) == pytest.approx(160)  # 40 с модель
    assert WhisperLocal(device="cuda").estimate_sec(100) is None  # не мерили — не выдумываем


def test_project_workflow_is_configurable(projects_root: Path) -> None:
    _clips_project(projects_root)
    assert load_project("c").workflow == "clips_from_long"
    text = (projects_root / "c" / "project.yaml").read_text(encoding="utf-8")
    assert "workflow: clips_from_long" in text
    edir = create_episode("c", "short01", title="проба")
    assert (edir / "storyboard.json").exists()
