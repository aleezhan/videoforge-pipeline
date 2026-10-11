"""Подача голоса: скорость, высота и громкость — настройка, а не то, что дал
edge-tts (`features/voice_prosody`). Ресурс задаёт подачу по умолчанию, сцена
переопределяет, витрина даёт пробы на выбор ушами."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from videoforge.api.app import create_app
from videoforge.core.resource_examples import (
    VOICE_VARIANTS,
    NeedsSynth,
    NoRecipe,
    find_example,
    find_variants,
    render_variants,
)
from videoforge.core.resources import CaptionStyle, Voice, load_resource
from videoforge.core.runner import RunReport, run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import Scene, SceneVoice, load_storyboard, save_storyboard
from videoforge.providers.base import GenResult, ProviderSet
from videoforge.providers.mock import MockTts
from videoforge.stages.audio import is_default, prosody


def test_scene_voice_keeps_the_edge_tts_format() -> None:
    """Знак, число, единица — и единица своя у каждого поля: без знака сервис
    отвечает ошибкой уже после того, как за реплику подождали."""
    v = SceneVoice(rate="+10%", pitch="-4Hz", volume="-6%")
    assert (v.rate, v.pitch, v.volume) == ("+10%", "-4Hz", "-6%")
    assert SceneVoice(rate="-12.5%").rate == "-12.5%"
    assert SceneVoice().rate is None
    for bad in (
        {"rate": "10%"},  # без знака
        {"rate": "+10"},  # без единицы
        {"rate": "+2Hz"},  # единица не та
        {"pitch": "+2%"},
        {"volume": "тише"},
        {"pitch": "+4 Hz"},
    ):
        with pytest.raises(ValidationError):
            SceneVoice(**bad)
    # поле сцены разбирается той же схемой, и JSON от руки его ловит
    with pytest.raises(ValidationError):
        Scene.model_validate({"id": "s", "order": 1, "voice": {"rate": "fast"}})


def test_scene_overrides_the_resource_by_field() -> None:
    voice = Voice(id="v", rate="+5%", pitch="+0Hz", volume="+0%")
    plain = Scene(id="a", order=1)
    assert prosody(voice, plain) == ("+5%", "+0Hz", "+0%")
    whisper = Scene(id="b", order=2, voice=SceneVoice(pitch="-4Hz", volume="-6%"))
    assert prosody(voice, whisper) == ("+5%", "-4Hz", "-6%")  # rate остался ресурсный
    assert is_default("+0%") and is_default("-0Hz") and is_default("+0.0%")
    assert not is_default("+10%") and not is_default("-4Hz") and not is_default("")


class SpyTts(MockTts):
    """Заглушка, которая запоминает, с какой подачей её позвали."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def synthesize(
        self,
        text: str,
        *,
        voice: str,
        rate: str,
        pitch: str,
        out_path: Path,
        volume: str = "+0%",
    ) -> GenResult:
        self.calls.append((text, rate, pitch, volume))
        return super().synthesize(
            text, voice=voice, rate=rate, pitch=pitch, out_path=out_path, volume=volume
        )


def _audio_message(report: RunReport) -> str:
    return dict(report.executed)["audio"].message


def test_scene_voice_reaches_the_provider_and_changes_the_fingerprint(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Шёпот, вписанный в сцену руками, — такая же правка, как правка текста:
    озвучка перегоняется, провайдер получает подачу, соседняя сцена не тронута."""
    spy = SpyTts()
    mock_providers.tts = spy
    opts = RunOptions(only="audio", auto_approve=True, raise_errors=True)

    sb = load_storyboard(episode)
    sb.scenes[1].narration = "У соседней сцены своя реплика."
    save_storyboard(episode, sb)
    report = run_episode(episode, mock_providers, opts)
    assert "ПРОВЕРЬ" not in _audio_message(report)  # всё по умолчанию — молчим
    sb = load_storyboard(episode)
    before = sb.scenes[0].inputs_fingerprint["audio"]
    neighbour = sb.scenes[1].inputs_fingerprint["audio"]
    assert len(spy.calls) == 2 and all(c[1:] == ("+0%", "+0Hz", "+0%") for c in spy.calls)

    run_episode(episode, mock_providers, opts)
    assert len(spy.calls) == 2  # без правок ничего не перегоняем

    sb.scenes[0].voice = SceneVoice(rate="+10%", pitch="-4Hz", volume="-6%")
    save_storyboard(episode, sb)
    report = run_episode(episode, mock_providers, opts)

    sb = load_storyboard(episode)
    assert sb.scenes[0].inputs_fingerprint["audio"] != before, "подачу сцены сочли той же"
    assert sb.scenes[1].inputs_fingerprint["audio"] == neighbour
    assert spy.calls[2:] == [("Первая сцена, короткая реплика.", "+10%", "-4Hz", "-6%")]
    # скорость не та, на которой замерен WORDS_PER_SEC, — одно предупреждение на
    # прогон, с именем сцены
    message = _audio_message(report)
    assert "ПРОВЕРЬ" in message and "s1 (+10%)" in message and "WORDS_PER_SEC" in message
    assert message.count("WORDS_PER_SEC") == 1

    # громкость одна меняет отпечаток, а высота без скорости предупреждения не даёт
    sb.scenes[0].voice = SceneVoice(volume="-3%")
    save_storyboard(episode, sb)
    report = run_episode(episode, mock_providers, opts)
    sb = load_storyboard(episode)
    assert sb.scenes[0].inputs_fingerprint["audio"] not in (before, neighbour)
    assert spy.calls[-1][1:] == ("+0%", "+0Hz", "-3%")
    assert "ПРОВЕРЬ" not in _audio_message(report)


def test_resource_rate_off_default_is_named_once_per_run(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    """Скорость в ресурсе голоса тоже не по умолчанию — предупреждение называет
    ресурс, а не каждую сцену, и стоит и на повторе без работы."""
    vdir = projects_root / "t" / "resources" / "voices"
    vdir.mkdir(parents=True)
    (vdir / "fast.yaml").write_text('id: fast\nvoice: ru-RU-DmitryNeural\nrate: "+15%"\n', "utf-8")
    sb = load_storyboard(episode)
    sb.resources.voice = "fast"
    save_storyboard(episode, sb)
    spy = SpyTts()
    mock_providers.tts = spy
    opts = RunOptions(only="audio", auto_approve=True, raise_errors=True)

    message = _audio_message(run_episode(episode, mock_providers, opts))
    assert "ресурс fast +15%" in message and message.count("WORDS_PER_SEC") == 1
    assert spy.calls and spy.calls[0][1] == "+15%"
    # повтор: озвучка на месте, а оговорка про ритм никуда не делась
    message = _audio_message(run_episode(episode, mock_providers, opts))
    assert "уже на месте" in message and "ресурс fast +15%" in message


# ---------------------------------------------------------------- витрина


@pytest.fixture
def resources_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "resources"
    (root / "voices").mkdir(parents=True)
    (root / "voices" / "ru.yaml").write_text("id: ru\nvoice: ru-RU-DmitryNeural\n", "utf-8")
    (root / "caption_styles").mkdir()
    (root / "caption_styles" / "plain.yaml").write_text("id: plain\n", "utf-8")
    monkeypatch.setenv("VF_RESOURCES_DIR", str(root))
    return root


def test_variants_are_the_same_phrase_with_one_setting_changed(resources_root: Path) -> None:
    """Четыре пробы рядом с витриной: имя файла говорит, что изменено, а
    синтез получает голос с ровно одной другой настройкой."""
    voice = load_resource("voices", "ru", Voice, [resources_root])
    heard: list[tuple[str, str, str]] = []

    def synth(text: str, v: Voice, out: Path) -> None:
        heard.append((text, v.rate, v.pitch))
        out.write_bytes(b"mp3")

    with pytest.raises(NeedsSynth):
        render_variants("voices", voice)
    style = load_resource("caption_styles", "plain", CaptionStyle, [resources_root])
    with pytest.raises(NoRecipe):
        render_variants("caption_styles", style, synth=synth)

    out = render_variants("voices", voice, synth=synth)
    folder = resources_root / "voices" / "ru"
    assert [p.name for p in out] == [
        "example.rate-10.mp3",
        "example.rate+10.mp3",
        "example.pitch-4Hz.mp3",
        "example.pitch+4Hz.mp3",
    ]
    assert len({t for t, _, _ in heard}) == 1  # одна и та же фраза
    assert [(r, p) for _, r, p in heard] == [
        ("-10%", "+0Hz"),
        ("+10%", "+0Hz"),
        ("+0%", "-4Hz"),
        ("+0%", "+4Hz"),
    ]
    assert voice.rate == "+0%" and voice.pitch == "+0Hz"  # сам ресурс не тронут
    # пробы — не витрина: главный example.mp3 по-прежнему отдельный файл
    assert find_example(voice) is None
    found = find_variants(voice)
    assert [v.label for v in found] == [v.label for v in VOICE_VARIANTS]
    assert all(v.path.parent == folder for v in found)


def test_api_lists_the_variants_with_labels(resources_root: Path) -> None:
    voice = load_resource("voices", "ru", Voice, [resources_root])
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    assert client.get("/api/resources/voices/ru").json()["examples"] == []

    render_variants("voices", voice, synth=lambda _t, _v, out: out.write_bytes(b"mp3"))
    detail = client.get("/api/resources/voices/ru").json()
    assert [e["label"] for e in detail["examples"]] == [v.label for v in VOICE_VARIANTS]
    urls = [e["url"].split("?")[0] for e in detail["examples"]]
    assert urls[0] == "/files/resources/voices/ru/example.rate-10.mp3"
    assert urls[3] == "/files/resources/voices/ru/example.pitch+4Hz.mp3"
    # без главной витрины пульт всё равно называет команду, которой её собрать
    assert detail["example_command"] == "videoforge resource gen-example voice ru"
