"""Распознавание по локальному файлу: тайминги слов ИЗ САМОГО ЗВУКА.

Зачем понадобилось. Слова песни к эдиту брали готовым файлом с YouTube, и
проверить их было нечем: текст на экране рисовали из того же файла, поэтому
дорожки в пульте совпадали всегда — в том числе когда обе врали. Ошибка нашлась
ушами и оказалась двойной: расшифровка была от ДРУГОЙ записи (файл длиннее
трека на 5.9 с), а внутри неё распознавалка глотала слова («need someone a
home» пропало целиком). Пока времена приходят из чужого файла, совпадение с
вокалом — везение, а не проверка.

Здесь оно перестаёт быть везением: слова и времена считаются по тому самому
файлу, который звучит в ролике. Тогда дорожка «Слова песни» становится вторым
мнением, а не копией первого, и расхождение с экраном видно глазом.

Модель качается при первом запуске (сотни мегабайт) и живёт в кэше
huggingface — поэтому зависимость необязательная: `uv sync --extra transcribe`.
Денег не стоит, стоит времени; сколько именно — в `meta` результата.

Вокал под музыку распознаётся хуже чистой речи, и это не лечится флагом: если
слов не хватает, следующий шаг — отделение вокала, отдельная тяжёлая работа.
Поэтому результат смотрят глазами рядом с волной, а не принимают на веру.
"""

from __future__ import annotations

import logging
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from videoforge.core.captions import Word, words_to_srt
from videoforge.providers.base import TranscriptProvider, TranscriptResult
from videoforge.providers.youtube import download_audio

log = logging.getLogger("videoforge.providers")

# medium — компромисс: small на пении путает слова заметно чаще, large-v3 тянет
# ~3 ГБ и на процессоре считает минутами. Переопределяется VF_WHISPER_MODEL
DEFAULT_MODEL = "medium"
# Процессор по умолчанию, хотя карта в машине есть. `device="auto"` её находит и
# падает на `cublas64_12.dll`: CTranslate2 требует своих CUDA-библиотек, которых
# с пакетом не приезжает, и падает НЕ исключением — процесс просто умирает с
# кодом 0, не оставив файла. Молчаливая смерть хуже медленного счёта, поэтому
# карта включается явным `--device cuda`, когда библиотеки поставлены
DEFAULT_DEVICE = "cpu"
COMPUTE = {"cpu": "int8", "cuda": "float16"}

# Длинный файл модель разбирает по кускам сама — и на музыке теряет целые куски:
# на трёхминутном концертнике она пропустила 35 секунд вокала подряд, а тот же
# отрезок, поданный отдельным файлом, дал 62 слова. Поэтому окна режем сами.
# Перекрытие — чтобы слово на стыке не разорвало; берутся только слова из ядра
# окна, перекрытие служит моделью контекстом и в результат не попадает.
CHUNK_SEC = 60.0
OVERLAP_SEC = 5.0
SAMPLE_RATE = 16000

# Звук ссылки, у которой нет готовых субтитров, кладётся рядом с транскриптом:
# whisper слушает файл, а не сеть, и второй прогон его не качает заново
AUDIO_FILE = "audio.m4a"
# Скорость на процессоре: medium int8 разбирает 100 с звука примерно за 2 мин
# (замер 14.09.2026 на записи стрима), плюс загрузка
# модели — 33 с речи заняли 82.7 с (16.09.2026). Двухчасовой подкаст — два с
# половиной часа счёта, и человеку это говорят ДО запуска
CPU_SEC_PER_SEC = 1.2
MODEL_LOAD_SEC = 40.0


MAX_PROMPT_WORDS = 60  # подсказка длиннее модель обрезает сама, и обрежет не там
MIN_ANCHORS = 3  # меньше совпавших слов — не опознали место в тексте, подсказку не даём
FLAT_SHARE = 0.2  # доля слов нулевой длины, после которой подсказке верить нельзя
FLAT_SEC = 0.02


def _tokens(words: list[str]) -> list[str]:
    return [t for w in words for t in re.findall(r"[^\W\d_]+", w.lower())]


def _prompt_for(lines: list[str], heard: list[str]) -> str:
    """Кусок текста песни, который поётся в этом окне — по услышанному.

    Подсказать модели ВЕСЬ текст нельзя: она обрежет его сама и не там, а лишние
    строки начнёт втискивать туда, где их не поют — слова слипаются в одну
    секунду. Поэтому место в тексте сначала опознаётся по тому, что уже
    услышано без подсказки, и подсказываются только соседние строки."""
    flat: list[str] = []
    line_of: list[int] = []
    for i, line in enumerate(lines):
        for token in _tokens([line]):
            flat.append(token)
            line_of.append(i)
    spoken = _tokens(heard)
    if not flat or not spoken:
        return ""
    blocks = [b for b in SequenceMatcher(None, flat, spoken).get_matching_blocks() if b.size]
    if sum(b.size for b in blocks) < MIN_ANCHORS:
        return ""
    first = line_of[blocks[0].a]
    last = line_of[min(blocks[-1].a + blocks[-1].size - 1, len(line_of) - 1)]
    picked = lines[max(0, first - 1) : last + 2]
    out: list[str] = []
    for line in picked:
        out.append(line)
        if len(_tokens(out)) >= MAX_PROMPT_WORDS:
            break
    return " ".join(out)


def decode_file(path: Path) -> Any:
    """Звук файла → массив 16 кГц моно, которым питается модель."""
    from faster_whisper.audio import decode_audio

    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)


def _window_starts(total_sec: float) -> list[float]:
    """Начала окон разбора. Последнее окно короче — доедает хвост."""
    starts: list[float] = []
    at = 0.0
    while at < total_sec:
        starts.append(at)
        at += CHUNK_SEC
    return starts or [0.0]


class WhisperLocal(TranscriptProvider):
    """faster-whisper по локальному файлу. Стоимость — 0: считает своя машина."""

    name = "whisper"

    def __init__(
        self, model: str = "", device: str = "", lyrics: str = "", *, speech: bool = False
    ) -> None:
        self.model_name = model or os.getenv("VF_WHISPER_MODEL", DEFAULT_MODEL)
        self.device = device or os.getenv("VF_WHISPER_DEVICE", DEFAULT_DEVICE)
        # текст песни: слова берутся из него, времена — по-прежнему из звука
        self.lyrics = [line.strip() for line in lyrics.splitlines() if line.strip()]
        # чистая речь (подкаст, стрим): детектор голоса включён и режет тишину,
        # на которой модель без него додумывает слова; для пения — выключен
        self.speech = speech

    def estimate_sec(self, duration_sec: float) -> float | None:
        """Сколько будет считать. На процессоре — дольше самой записи; для
        карты замера нет, и число не выдумывается."""
        if self.device != "cpu":
            return None
        return MODEL_LOAD_SEC + duration_sec * CPU_SEC_PER_SEC

    @staticmethod
    def audio_source(url: str, video: Path | None, out_dir: Path) -> Path:
        """Что слушать: файл, если он есть, иначе звук ссылки, скачанный один
        раз в `out_dir` (yt-dlp открывает и то, у чего субтитров нет: TikTok,
        чужие сайты, своя запись по ссылке). Ни того ни другого — ошибка словами."""
        if video is not None and video.exists():
            return video
        if url:
            return download_audio(url, out_dir / AUDIO_FILE)
        raise ValueError("whisper слушает файл или ссылку — не передано ни то ни другое")

    def _decode(self, model: object, piece: object, language: str, prompt: str = "") -> tuple:
        segments, info = model.transcribe(  # type: ignore[attr-defined]
            piece,
            language=language or None,
            word_timestamps=True,
            # без этого модель на музыке уходит в повтор: додумывает строку по
            # предыдущей вместо того, чтобы слушать
            condition_on_previous_text=False,
            # детектор голоса режет вокал под громкой музыкой: с ним на том же
            # треке осталось 17 слов вместо 327. Для чистой речи он включён
            vad_filter=self.speech,
            beam_size=5,
            initial_prompt=prompt or None,
        )
        out: list[Word] = []
        for segment in segments:  # генератор: работа идёт здесь, а не в transcribe()
            for word in segment.words or []:
                text = word.word.strip()
                if not text:
                    continue
                if text.startswith("-") and out:  # «straight» + «-talking» — одно слово
                    out[-1] = Word(out[-1].text + text, out[-1].start, round(word.end, 3))
                    continue
                out.append(Word(text, round(word.start, 3), round(word.end, 3)))
        return out, info

    @staticmethod
    def _collapsed(words: list[Word]) -> bool:
        """Подсказка заставила модель втиснуть строки, которых тут не поют.

        Видно по словам нулевой длины: они слипаются в одну секунду. Так вышло,
        когда в подсказку попала строка, спетая ДО начала отрезка — восемь слов
        встали на 1.26 с. Такой результат хуже честного распознавания."""
        if not words:
            return True
        flat = sum(1 for w in words if w.end - w.start < FLAT_SEC)
        return flat / len(words) > FLAT_SHARE

    def load_model(self) -> object:
        """Модель faster-whisper: грузится один раз на весь разбор."""
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover — зависимость необязательная
            raise RuntimeError(
                "нет faster-whisper: поставь его командой `uv sync --extra transcribe`"
            ) from exc
        return WhisperModel(
            self.model_name, device=self.device, compute_type=COMPUTE.get(self.device, "auto")
        )

    def listen(
        self, model: object, audio: object, language: str, *, offset: float = 0.0
    ) -> tuple[list[Word], str, int]:
        """Звук (массив 16 кГц) → слова со временем от `offset`, язык и сколько
        окон поправлено текстом песни. Окна режутся здесь, а не моделью."""
        total = len(audio) / SAMPLE_RATE  # type: ignore[arg-type]
        words: list[Word] = []
        language_found = ""
        prompted = 0
        for start in _window_starts(total):
            core_end = min(start + CHUNK_SEC, total)
            read_end = min(core_end + OVERLAP_SEC, total)
            piece = audio[int(start * SAMPLE_RATE) : int(read_end * SAMPLE_RATE)]  # type: ignore[index]
            heard, info = self._decode(model, piece, language or language_found)
            language_found = language_found or getattr(info, "language", "") or ""
            picked = heard
            # второй проход: место в тексте песни опознано по услышанному, и
            # соседние строки уходят подсказкой. Слова становятся верными,
            # времена остаются измеренными — проверено на опорных словах:
            # rodeo 14.80 → 14.82, control 17.02 → 17.00, vulnerable без сдвига
            if self.lyrics and (prompt := _prompt_for(self.lyrics, [w.text for w in heard])):
                better, _ = self._decode(model, piece, language or language_found, prompt)
                if not self._collapsed(better):
                    picked = better
                    prompted += 1
            for word in picked:
                at = start + word.start
                if start <= at < core_end:  # перекрытие — только контекст
                    words.append(
                        Word(
                            word.text,
                            round(offset + at, 3),
                            round(offset + start + word.end, 3),
                        )
                    )
        return words, language_found, prompted

    def transcribe(
        self,
        *,
        url: str,
        video: Path | None,
        language: str,
        duration_sec: float,
        out_path: Path,
    ) -> TranscriptResult:
        source = self.audio_source(url, video, out_path.parent)
        started = time.monotonic()
        model = self.load_model()
        audio = decode_file(source)
        total = len(audio) / SAMPLE_RATE
        words, language_found, prompted = self.listen(model, audio, language)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(words_to_srt(words), encoding="utf-8")
        spent = round(time.monotonic() - started, 1)
        log.info("whisper %s: %d слов за %s с", self.model_name, len(words), spent)
        return TranscriptResult(
            words_srt=out_path,
            language=language or language_found,
            cost_usd=0.0,
            meta={
                "model": self.model_name,
                "device": self.device,
                "audio": str(source),
                "words": len(words),
                "seconds": spent,
                "windows": len(_window_starts(total)),
                "prompted_windows": prompted,  # сколько окон удалось поправить текстом
            },
        )
