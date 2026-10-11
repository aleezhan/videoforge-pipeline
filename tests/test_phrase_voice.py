"""Озвучка фразами: несколько сцен одним вызовом, звук сцены — кусок общей
записи по словам (28.09.2026). И разбор ответа ElevenLabs — без сети."""

from __future__ import annotations

from pathlib import Path

from videoforge.core.captions import parse_srt_words
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard, save_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.providers.elevenlabs import speed_from_rate, words_from_alignment
from videoforge.providers.mock import MockTts
from videoforge.stages.audio import JOINED_GAP_KEY, tokens

AUDIO = RunOptions(only="audio")


def _phrase_voice(episode: Path) -> None:
    """Голос проекта с phrase: true, подключённый эпизоду."""
    voices = episode.parent.parent / "resources" / "voices"
    voices.mkdir(parents=True, exist_ok=True)
    (voices / "phr.yaml").write_text(
        "id: phr\nname: фразами\nprovider: mock\nvoice: x\nphrase: true\n", encoding="utf-8"
    )
    sb = load_storyboard(episode)
    sb.resources.voice = "phr"
    first, second = sb.ordered_scenes()[:2]
    first.narration = "Он был из джиннов, и он"
    second.narration = "ослушался повеления своего Господа."
    save_storyboard(episode, sb)


def test_phrase_is_one_call_and_each_scene_gets_its_own_words(
    episode: Path, mock_providers: ProviderSet
) -> None:
    heard: list[str] = []

    class Ear(MockTts):
        def synthesize(self, text, **kwargs):  # type: ignore[no-untyped-def, override]
            heard.append(text)
            return super().synthesize(text, **kwargs)

    mock_providers.tts = Ear()
    _phrase_voice(episode)
    run_episode(episode, mock_providers, AUDIO)

    assert heard == ["Он был из джиннов, и он ослушался повеления своего Господа."]
    first, second = load_storyboard(episode).ordered_scenes()[:2]
    said = [
        [w.text for w in parse_srt_words((episode / (s.assets.captions or "")).read_text("utf-8"))]
        for s in (first, second)
    ]
    assert said == [
        ["Он", "был", "из", "джиннов,", "и", "он"],
        ["ослушался", "повеления", "своего", "Господа."],
    ]
    # пауза внутри фразы — та, что в записи, а не по классу стыка
    assert JOINED_GAP_KEY in first.measured_sec and JOINED_GAP_KEY not in second.measured_sec
    assert first.assets.audio and (episode / first.assets.audio).exists()

    # правка соседа перегоняет всю фразу, повтор без правок — ничего
    heard.clear()
    run_episode(episode, mock_providers, AUDIO)
    assert heard == []
    sb = load_storyboard(episode)
    sb.ordered_scenes()[1].narration = "ослушался повеления Господа своего."
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, AUDIO)
    assert heard == ["Он был из джиннов, и он ослушался повеления Господа своего."]


def test_word_count_mismatch_falls_back_to_scene_by_scene(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Провайдер разбил слова иначе — резать по счёту нельзя: фраза уходит
    по сценам, как раньше."""
    heard: list[str] = []

    class Glued(MockTts):
        def synthesize(self, text, **kwargs):  # type: ignore[no-untyped-def, override]
            heard.append(text)
            glued = text.replace("из джиннов", "из_джиннов")
            return super().synthesize(glued, **kwargs)

    mock_providers.tts = Glued()
    _phrase_voice(episode)
    run_episode(episode, mock_providers, AUDIO)
    assert heard[1:] == ["Он был из джиннов, и он", "ослушался повеления своего Господа."]
    first = load_storyboard(episode).ordered_scenes()[0]
    assert JOINED_GAP_KEY not in first.measured_sec


def test_tokens_skip_punctuation_only() -> None:
    assert tokens("Он — из джиннов …") == ["Он", "из", "джиннов"]


def test_elevenlabs_alignment_becomes_words() -> None:
    text = "Он — был."
    alignment = {
        "characters": list(text),
        "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
    }
    words = words_from_alignment(alignment)
    assert [(w.text, w.start, w.end) for w in words] == [("Он", 0.0, 0.2), ("был.", 0.5, 0.9)]


def test_rate_maps_to_speed_in_range() -> None:
    assert speed_from_rate("+0%") == 1.0
    assert speed_from_rate("+10%") == 1.1
    assert speed_from_rate("+50%") == 1.2
    assert speed_from_rate("-40%") == 0.7
