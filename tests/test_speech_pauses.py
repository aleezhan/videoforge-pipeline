"""Пауза между фразами по смыслу, а не по стыку сцен (features/speech_pause_by_class).

Замер research/_probes/speech_pauses_2026-09-16: у конкурентов пауза на конце
предложения 0.14–0.58 с, у ep001 — 1.5–1.7 с после почти каждого предложения.
Её складывали вшитая тишина edge-tts, хвост кадра и добивка до плана. Здесь —
срез тишины по таймингам слов, класс паузы, коробка сцены из них, защита готовых
эпизодов от пересчёта и замер пауз готового ролика.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from videoforge.core.captions import Word, parse_srt_words
from videoforge.core.pauses import (
    PAUSE_BLOCK_SEC,
    PAUSE_CLAUSE_SEC,
    PAUSE_JITTER_SEC,
    PAUSE_POINT_SEC,
    PAUSE_QUESTION_SEC,
    PAUSE_SENTENCE_SEC,
    TRIM_PAD_SEC,
    ScriptMarks,
    TimedWord,
    ending,
    jitter,
    pause_after,
    pause_class,
    scene_lead_in,
    script_marks,
    sentence_ends,
    speech_pauses,
    trim_window,
)
from videoforge.core.runner import RunReport, run_episode
from videoforge.core.script import parse_script
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import (
    FinalOutput,
    Scene,
    SceneOverlay,
    SilenceGap,
    load_storyboard,
    save_storyboard,
)
from videoforge.providers.base import ProviderSet
from videoforge.providers.mock import MOCK_LEAD_SEC, MOCK_TRAIL_SEC, MockTts
from videoforge.stages import planner
from videoforge.stages.audio import (
    MAX_PAD_SEC,
    MIN_SCENE_SEC,
    MUTE_END_SCREEN_SEC,
    MUTE_MIDDLE_SEC,
    MUTE_OPENING_SEC,
    RAW_KEY,
)

AUDIO = RunOptions(only="audio", auto_approve=True, raise_errors=True)


def _message(report: RunReport, stage: str = "audio") -> str:
    return dict(report.executed)[stage].message


def _words(path: Path) -> list[Word]:
    return parse_srt_words(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- срез


def test_trim_window_keeps_a_pad_around_the_words() -> None:
    words = [Word("раз", 0.24, 0.6), Word("два", 0.7, 1.9)]
    assert trim_window(words, 2.87) == (round(0.24 - TRIM_PAD_SEC, 3), round(1.9 + TRIM_PAD_SEC, 3))
    # запас не выходит за файл: слово с нуля и слово до самого конца
    assert trim_window([Word("а", 0.01, 2.86)], 2.87) == (0.0, 2.87)
    # таймингов нет — файл целиком: резать не по чему
    assert trim_window([], 3.0) == (0.0, 3.0)


def test_audio_cuts_embedded_silence_and_shifts_the_words(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Файл сервиса с тишиной по краям → WAV от первого слова до последнего с
    запасом, и .srt сдвинут на ту же величину: субтитры и сборка читают одну
    шкалу. Файл сервиса рядом не остаётся."""
    run_episode(episode, mock_providers, AUDIO)
    s1 = load_storyboard(episode).ordered_scenes()[0]
    assert s1.assets.audio == "04_audio/s1.wav" and s1.assets.captions == "04_audio/s1.srt"
    speech = len(s1.narration) / 14  # темп заглушки
    # длительность из `ffmpeg -i` — с точностью до сотых
    assert abs(s1.measured_sec[RAW_KEY] - (MOCK_LEAD_SEC + speech + MOCK_TRAIL_SEC)) < 0.015
    assert abs(s1.measured_sec["audio"] - (speech + 2 * TRIM_PAD_SEC)) < 0.015
    with wave.open(str(episode / s1.assets.audio)) as w:
        assert abs(w.getnframes() / w.getframerate() - (speech + 2 * TRIM_PAD_SEC)) < 0.002
    words = _words(episode / s1.assets.captions)
    assert abs(words[0].start - TRIM_PAD_SEC) < 0.002
    assert abs(s1.measured_sec["audio"] - words[-1].end - TRIM_PAD_SEC) < 0.015
    left = sorted(p.name for p in (episode / "04_audio").iterdir())
    assert left == ["s1.srt", "s1.wav"], left


# ---------------------------------------------------------------- класс паузы


def _scene(sid: str, text: str, **kw: object) -> Scene:
    return Scene(id=sid, order=1, narration=text, **kw)  # type: ignore[arg-type]


def test_pause_class_follows_what_ended_and_what_begins() -> None:
    none = ScriptMarks()
    a = _scene("s1", "Он был одним из них.")
    q = _scene("s2", "Тогда откуда он взялся среди ангелов?")
    comma = _scene("s3", "Джинны созданы из огня,")
    cont = _scene("s3_2", "у них есть выбор.", continues="s3")
    other = _scene("s4", "Весь зал склонился.")
    label = _scene("s5", "Стадия вторая.", overlay=SceneOverlay(kind="label", text="СТАДИЯ 2"))
    opener = _scene("s6", "Во-вторых, это правда.")

    assert ending("«…из глины».") == "sentence" and ending("Что?!") == "question"
    assert ending("сказано:") == "clause" and ending("") == "clause"
    assert pause_class(a, q, none) == "sentence"
    assert pause_class(q, a, none) == "question"
    assert pause_class(comma, cont, none) == "clause"  # тот же план, голос идёт дальше
    assert pause_class(comma, other, none) == "clause"  # запятая, но кадр сменился
    assert pause_class(a, label, none) == "point"  # ярлык пункта на первой сцене
    assert pause_class(a, opener, none) == "point"
    assert pause_class(a, other, ScriptMarks(block_end={"s1"})) == "block"
    assert pause_class(a, other, ScriptMarks(point_start={"s4"})) == "point"
    assert pause_class(a, None, none) == "block"  # конец ролика — конец последнего блока

    def base(
        scene: Scene, nxt: Scene | None, marks: ScriptMarks = none, tail: float = 0.0
    ) -> float:
        return round(pause_after(scene, nxt, marks, tail_sec=tail) - jitter(scene.id), 3)

    assert base(a, q) == PAUSE_SENTENCE_SEC
    assert base(q, a) == PAUSE_QUESTION_SEC
    assert base(a, label) == PAUSE_POINT_SEC
    assert base(a, None) == PAUSE_BLOCK_SEC
    assert base(comma, cont) == PAUSE_CLAUSE_SEC
    # хвост кадра покрывается паузой (max), а не прибавляется к ней; продолжению
    # того же плана хвост не нужен — кадр не меняется
    assert base(a, q, tail=0.5) == 0.5
    assert base(a, None, tail=0.5) == PAUSE_BLOCK_SEC
    assert base(comma, cont, tail=0.5) == PAUSE_CLAUSE_SEC


def test_jitter_is_deterministic_and_bounded() -> None:
    ids = [f"s{i:02d}" for i in range(1, 120)]
    values = [jitter(i) for i in ids]
    assert values == [jitter(i) for i in ids]  # повторный прогон — те же секунды
    assert all(-PAUSE_JITTER_SEC <= v <= PAUSE_JITTER_SEC for v in values)
    # соседние паузы не одинаковы: у конкурентов отличаются на 0.14–0.17 с
    neighbours = sorted(abs(a - b) for a, b in zip(values, values[1:], strict=False))
    assert neighbours[len(neighbours) // 2] > 0.03


SCRIPT = """## g1 · Не ангел · 20s

**Текст:** Он не был ангелом. Об этом сказано прямо.

Джинны созданы из огня, у них есть выбор.

**Кадр:** зал

## g2 · Приказ · 20s

**Текст:**
А потом был приказ. Весь зал склонился.

**Эмоция:** замирание
"""


def test_script_marks_find_block_ends_and_paragraphs_despite_paraphrase() -> None:
    """Связи «сцена ← блок» в раскадровке нет, а планнер перефразирует: сверка
    идёт по словам целиком, и блоки находятся и так."""
    blocks = parse_script(SCRIPT)
    assert [b.paragraphs for b in blocks] == [
        ["Он не был ангелом. Об этом сказано прямо.", "Джинны созданы из огня, у них есть выбор."],
        ["А потом был приказ. Весь зал склонился."],
    ]
    assert (
        blocks[0].text
        == "Он не был ангелом. Об этом сказано прямо. Джинны созданы из огня, у них есть выбор."
    )
    scenes = [
        _scene("s1", "Он не был ангелом."),
        _scene("s2", "Об этом сказано совершенно прямо."),  # перефраз
        _scene("s3", "Джинны созданы из огня,"),
        _scene("s3_2", "у них есть выбор.", continues="s3"),
        _scene("s4", "А потом был приказ."),
        _scene("s5", "Весь зал склонился."),
    ]
    for n, scene in enumerate(scenes, start=1):
        scene.order = n
    marks = script_marks(scenes, blocks)
    assert marks.block_end == {"s3_2", "s5"}
    assert marks.point_start == {"s3"}
    assert script_marks(scenes, []) == ScriptMarks()


def test_scene_box_is_speech_plus_pause_not_tail_plus_silence(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Коробка сцены = конец последнего слова + пауза класса − начало первого
    слова следующей: между словами звучит пауза класса. Немая конечная
    заставка — не дольше 5 с, а не весь план."""
    sb = load_storyboard(episode)
    s1, s2 = sb.ordered_scenes()
    s2.narration = "А это вопрос?"
    s3 = Scene(id="s3", order=3, narration="", visual_prompt="end", motion_prompt="hold")
    s3.planned_sec = 12.0
    sb.scenes.append(s3)
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, AUDIO)

    s1, s2, s3 = load_storyboard(episode).ordered_scenes()
    w1, w2 = _words(episode / s1.assets.captions), _words(episode / s2.assets.captions)
    # тишина между словами s1 и s2: хвост s1 после слова + заход s2 + запас среза
    # (склейка посреди паузы — tests/test_cut_mid_pause.py)
    lead2 = scene_lead_in(s2)
    gap = s1.duration_sec - scene_lead_in(s1) - w1[-1].end + lead2 + w2[0].start
    assert abs(gap - max(0.5, PAUSE_SENTENCE_SEC) - jitter("s1")) < 0.011
    assert gap < 0.5 + PAUSE_JITTER_SEC + 0.011  # не 1.5 с, как складывалось раньше
    # s2 → немая заставка: пауза после вопроса, заставка — отдельно и не дольше 5 с
    wanted2 = max(MIN_SCENE_SEC, lead2 + w2[-1].end + 0.55 + jitter("s2"))
    assert abs(s2.duration_sec - wanted2) < 0.011
    assert s3.duration_sec == MUTE_END_SCREEN_SEC

    # полы планнера растягивают паузу не больше чем на MAX_PAD_SEC
    sb = load_storyboard(episode)
    natural = sb.scenes[0].duration_sec
    sb.scenes[0].planned_sec = 9.0
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, AUDIO)
    assert abs(load_storyboard(episode).scenes[0].duration_sec - (natural + MAX_PAD_SEC)) < 0.011


def test_video_without_speech_lives_by_its_plan(episode: Path, mock_providers: ProviderSet) -> None:
    """Ролик без речи вовсе (под музыку, реалтайм-проба) — не «заставки»:
    сцены живут планом, иначе каждая стала бы секундной."""
    sb = load_storyboard(episode)
    for scene in sb.scenes:
        scene.narration = ""
        scene.duration_sec = 8.0
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, AUDIO)
    assert [s.duration_sec for s in load_storyboard(episode).ordered_scenes()] == [8.0, 8.0]


def test_long_speech_is_capped_by_the_still_provider_when_the_episode_mixes(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    """Коробка сцены обрезается по самому длинному клипу ИЗ ТЕХ, кем ролик
    рендерится. При доле клипов < 100% длинная сцена уйдёт картинкой с наездом,
    а у неё длина любая: ep003 (18.09.2026) по потолку одного видеопровайдера
    обрезал 20 сцен, и хвост речи доигрывал замороженный кадр 30 с."""
    long_text = " ".join(["слово"] * 60)  # заведомо длиннее клипа заглушки
    sb = load_storyboard(episode)
    sb.ordered_scenes()[0].narration = long_text
    save_storyboard(episode, sb)
    cap = max(mock_providers.video.supported_durations)
    assert mock_providers.still is not None
    assert max(mock_providers.still.supported_durations) > cap

    run_episode(episode, mock_providers, AUDIO)
    capped = load_storyboard(episode).ordered_scenes()[0].duration_sec
    assert capped <= cap + 0.01, "у проекта доля клипов 100% — потолок клипа"

    cfg = projects_root / "t" / "project.yaml"
    was = cfg.read_text(encoding="utf-8")
    try:
        cfg.write_text(was.replace("clip_share: 1.0", "clip_share: 0.2"), encoding="utf-8")
        sb = load_storyboard(episode)
        sb.ordered_scenes()[0].narration = long_text + " ещё"  # правка текста → пересчёт
        save_storyboard(episode, sb)
        run_episode(episode, mock_providers, AUDIO)
        mixed = load_storyboard(episode).ordered_scenes()[0].duration_sec
        assert mixed > cap + 0.5, "смешанный ролик: длинная сцена живёт полной речью"
    finally:
        cfg.write_text(was, encoding="utf-8")  # проект общий на тесты файла


def test_stress_marks_are_for_the_reader_not_for_the_voice(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Знак ударения голос читает как разрыв слова: «Алла́х» → «Аллаха»
    (замер 18.09.2026, research/_probes/pronunciation_2026-09-18). В голос
    уходит реплика без знаков, а в раскадровке он остаётся для человека."""
    heard: list[str] = []

    class Ear(MockTts):
        def synthesize(self, text, **kwargs):  # type: ignore[no-untyped-def, override]
            heard.append(text)
            return super().synthesize(text, **kwargs)

    mock_providers.tts = Ear()
    sb = load_storyboard(episode)
    sb.ordered_scenes()[0].narration = "Так сказал Алла́х, и Ами́р ответил."
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, AUDIO)
    assert "Так сказал Аллах, и Амир ответил." in heard
    assert all("́" not in t for t in heard)
    assert "́" in load_storyboard(episode).ordered_scenes()[0].narration


def test_pronunciation_dictionary_is_for_the_voice_not_for_the_captions(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """«Карим» голос читает «Карем», а «Ка-рим» — верно (замер 18.09.2026), но
    субтитры строятся из слов сервиса. Словарь проекта меняет написание только
    на пути к голосу: в .srt слово возвращается, форма вне словаря («Амира»)
    не трогается, а правка словаря перегоняет только сцены с этим словом."""
    heard: list[str] = []

    class Ear(MockTts):
        def synthesize(self, text, **kwargs):  # type: ignore[no-untyped-def, override]
            heard.append(text)
            return super().synthesize(text, **kwargs)

    mock_providers.tts = Ear()
    book = episode.parent.parent / "pronunciation.yaml"
    book.write_text('words:\n  Карим: Ка-рим\n  Амир: "А-мир"\n', encoding="utf-8")
    try:
        sb = load_storyboard(episode)
        first, second = sb.ordered_scenes()[:2]
        first.narration = "Это Ами́р, а Кари́м нашёл работу у Амира."
        second.narration = "Оба просили об одном."
        save_storyboard(episode, sb)
        run_episode(episode, mock_providers, AUDIO)
        assert "Это А-мир, а Ка-рим нашёл работу у Амира." in heard
        scenes = load_storyboard(episode).ordered_scenes()
        shown = [
            w.text.strip(".,")
            for w in parse_srt_words(
                (episode / (scenes[0].assets.captions or "")).read_text(encoding="utf-8")
            )
        ]
        assert "Карим" in shown and "Амир" in shown, shown
        assert not any("-" in w for w in shown), shown

        heard.clear()
        book.write_text("words:\n  Карим: Каа-рим\n", encoding="utf-8")
        run_episode(episode, mock_providers, AUDIO)
        assert heard == ["Это Амир, а Каа-рим нашёл работу у Амира."], heard
    finally:
        book.unlink(missing_ok=True)


def test_mute_caps_are_the_planner_limits() -> None:
    """Озвучка не импортирует планнер (планнер импортирует её) — числа
    продублированы, и расходиться им нельзя."""
    assert MUTE_OPENING_SEC == planner.SILENT_OPENING_SEC
    assert MUTE_END_SCREEN_SEC == planner.SILENT_END_SCREEN_SEC
    assert MUTE_MIDDLE_SEC <= planner.SILENT_RUN_LIMIT_SEC


# ---------------------------------------------------------------- готовые эпизоды


def _legacy_episode(episode: Path) -> dict[str, float]:
    """Эпизод, озвученный и собранный до среза тишины: mp3 с вшитой тишиной
    (здесь — wav заглушки), длительности по прежним правилам, немая сцена по
    плану на 15 с и готовые клипы. Возвращает длительности сцен."""
    sb = load_storyboard(episode)
    s1, s2 = sb.ordered_scenes()
    s3 = Scene(
        id="s3",
        order=3,
        narration="Третья сцена после паузы.",
        visual_prompt="v",
        motion_prompt="m",
    )
    sb.scenes.append(s3)
    s2.duration_sec = s2.planned_sec = 15.0  # «длинная сцена-выдох» старого планнера
    out = episode / "04_audio"
    out.mkdir(exist_ok=True)
    tts = MockTts()
    for scene in (s1, s3):
        gen = tts.synthesize(
            scene.narration, voice="v", rate="+0%", pitch="+0Hz", out_path=out / f"{scene.id}.wav"
        )
        seconds = gen.meta["seconds"]
        scene.assets.audio = f"04_audio/{scene.id}.wav"
        scene.assets.captions = f"04_audio/{scene.id}.srt"
        scene.provenance["audio"] = "mock"
        scene.checks.audio = "pass"
        scene.measured_sec["audio"] = round(seconds, 3)
        scene.duration_sec = round(seconds + 0.5 + 0.3, 1)  # старая формула с добивкой
    s2.checks.audio = "skipped"
    vdir = episode / "03_video"
    vdir.mkdir(exist_ok=True)
    for scene in sb.scenes:
        clip = vdir / f"{scene.id}_v01.mp4"
        clip.write_bytes(b"clip")
        scene.assets.image = f"02_images/{scene.id}.png"
        scene.assets.video = f"03_video/{clip.name}"
        scene.provenance["video"] = "mock"
        scene.checks.video = "pass"
        scene.attempts["video"] = 1
        scene.measured_sec["video"] = float(mock_providers_pick(scene.duration_sec))
    save_storyboard(episode, sb)
    return {s.id: s.duration_sec for s in sb.scenes}


def mock_providers_pick(seconds: float) -> int:
    return min((d for d in (6, 10) if d >= seconds), default=10)


def test_ready_episode_keeps_its_durations_and_clips(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Обновление кода не пересчитывает длительности готового эпизода: новая
    длина сделала бы клипы устаревшими, и `run` перегенерировал бы их за
    деньги. Правка реплики — новая озвучка, и новые паузы получает она."""
    before = _legacy_episode(episode)
    report = run_episode(episode, mock_providers, AUDIO)
    assert "до среза тишины сцен: 2" in _message(report)
    sb = load_storyboard(episode)
    assert {s.id: s.duration_sec for s in sb.scenes} == before
    assert all(RAW_KEY not in s.measured_sec for s in sb.scenes)  # озвучку не перегоняли
    assert (episode / "04_audio" / "s1.wav").exists()

    report = run_episode(
        episode, mock_providers, RunOptions(only="video", auto_approve=True, raise_errors=True)
    )
    assert "все клипы уже на месте" in _message(report, "video")
    assert all(s.attempts["video"] == 1 for s in load_storyboard(episode).scenes)

    # правка реплики: сцена озвучена заново, срезана и получила паузу класса;
    # соседние готовые сцены и немая сцена старого эпизода не тронуты
    sb = load_storyboard(episode)
    sb.scene("s1").narration = "Первая сцена, другая реплика."
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, AUDIO)
    sb = load_storyboard(episode)
    s1 = sb.scene("s1")
    assert RAW_KEY in s1.measured_sec and s1.assets.audio == "04_audio/s1.wav"
    assert s1.duration_sec < before["s1"]
    assert sb.scene("s2").duration_sec == before["s2"] == 15.0
    assert sb.scene("s3").duration_sec == before["s3"]


def test_paid_clip_longer_than_the_scene_is_not_redone(
    episode: Path, mock_providers: ProviderSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Голос сменили на более быстрый — сцены стали короче, готовые клипы
    длиннее нужного. Лишнее сборка срежет: платный клип из-за этого не
    переделывается (ep003, 19.09.2026: десять клипов LTX ушли бы на карту),
    бесплатный наезд перерисовывается, а клип КОРОЧЕ речи устарел у обоих."""
    _legacy_episode(episode)
    video = RunOptions(only="video", auto_approve=True, raise_errors=True)
    run_episode(episode, mock_providers, video)

    def stretch(by: float) -> None:
        sb = load_storyboard(episode)
        s1 = sb.scene("s1")
        s1.measured_sec["video"] = mock_providers.video.pick_duration(s1.duration_sec) + by
        save_storyboard(episode, sb)

    def made() -> float:
        return load_storyboard(episode).scene("s1").measured_sec["video"]

    fresh = made()
    monkeypatch.setattr(type(mock_providers.video), "price_for", lambda *a, **k: 0.5)
    stretch(+4.0)
    report = run_episode(episode, mock_providers, video)
    assert "все клипы уже на месте" in _message(report, "video")
    assert made() == fresh + 4.0

    # кадров у этого эпизода нет, переделать клип нечем — смотрим, берёт ли
    # стадия сцену в работу
    stretch(-4.0)  # короче речи — устарел и платный
    report = run_episode(episode, mock_providers, RunOptions(only="video", auto_approve=True))
    assert "все клипы уже на месте" not in _message(report, "video")

    monkeypatch.setattr(type(mock_providers.video), "price_for", lambda *a, **k: 0.0)
    stretch(+4.0)  # бесплатный длиннее нужного — перерисовать
    report = run_episode(episode, mock_providers, RunOptions(only="video", auto_approve=True))
    assert "все клипы уже на месте" not in _message(report, "video")


# ---------------------------------------------------------------- готовый файл


def test_speech_pauses_are_measured_between_words() -> None:
    narration = "Сура пятьдесят пять, аят 15. Он был одним из них?"
    words = [
        Word(t, i * 0.5, i * 0.5 + 0.4)
        for i, t in enumerate("Сура пятьдесят пять аят пятнадцатый Он был одним из них".split())
    ]
    flags = sentence_ends(narration, words)
    # «15» в тексте против «пятнадцатый» в таймингах не совпало: конец
    # предложения за числом теряется (медиана считается по остальным), а
    # концовка реплики стоит всегда — у последнего слова
    assert flags == [False] * 9 + [True]

    timed = [
        TimedWord("s1", 0.0, 1.0, True),
        TimedWord("s2", 1.5, 2.0, False),
        TimedWord("s2", 3.2, 3.5, True),  # пауза 1.2 с посреди реплики
        TimedWord("s3", 4.2, 5.0, True),
        TimedWord("s3", 5.2, 5.5, False),
    ]
    p = speech_pauses(timed)
    assert p.words == 5
    assert p.sentence_median_sec == 0.5  # 0.5, 0.7, 0.2 → медиана 0.5
    assert [(x.scene_id, x.sec) for x in p.long] == [("s2", 1.2)]
    assert speech_pauses([]).sentence_median_sec is None


def _rows(final: FinalOutput, *, filmed: bool = False) -> dict[str, bool]:
    return {
        c.label: c.ok
        for c in final.checks(captions_expected=False, music_expected=False, filmed=filmed)
    }


def test_final_pause_rows_follow_the_competitors() -> None:
    ep001 = FinalOutput(duration_sec=507.0, sentence_pause_sec=1.6, long_pauses=77)
    rows = _rows(ep001)
    assert rows["пауза на конце предложения"] is False
    assert rows["паузы речи дольше 1s"] is False
    # william через новую озвучку на тексте ep001: 336 с, медиана 0.51, 4 паузы > 1 с
    pace = FinalOutput(duration_sec=336.0, sentence_pause_sec=0.51, long_pauses=4)
    rows = _rows(pace)
    assert rows["пауза на конце предложения"] and rows["паузы речи дольше 1s"]
    # одна пауза в шортсе на 36 с — не «1.7 в минуту», а один поворот мысли
    assert _rows(FinalOutput(duration_sec=36.0, sentence_pause_sec=0.3, long_pauses=1))[
        "паузы речи дольше 1s"
    ]
    # старый финал строк не получает; нарезка тоже — темп живой речи не наш рендер
    assert "пауза на конце предложения" not in _rows(FinalOutput(duration_sec=60.0))
    assert "паузы речи дольше 1s" not in _rows(ep001, filmed=True)


def test_end_screen_tail_is_not_the_longest_pause_in_the_middle() -> None:
    """Заставка в конце живёт по своему порогу (до 6 с): посреди ролика её
    считать нельзя, иначе законный хвост горел бы «самой длинной паузой»."""
    final = FinalOutput(
        duration_sec=300.0,
        silence_sec=4.5,
        silence_tail_sec=4.5,
        silence_gaps=[SilenceGap(scene_id="s40", at_sec=290.0, sec=4.5)],
    )
    rows = _rows(final)
    assert rows["самая длинная пауза"] and rows["тишина в конце"] and rows["доля тишины"]
    assert final.silence_longest_sec == 0.0
    final.silence_gaps.insert(0, SilenceGap(scene_id="s12", at_sec=60.0, sec=2.6))
    assert final.silence_longest_sec == 2.6 and not _rows(final)["самая длинная пауза"]
