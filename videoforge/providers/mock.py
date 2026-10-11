"""Заглушки: весь конвейер прогоняется без единого платного вызова.

Картинка — цветной холст с текстом промпта, видео — Ken Burns через ffmpeg,
озвучка — тишина нужной длины (рядом .srt, как у edge-tts), проверка — всегда pass.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import math
import re
import textwrap
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from videoforge.core.captions import Word, words_to_srt
from videoforge.core.ffmpeg import aspect_size, image_to_video, probe_duration
from videoforge.core.storyboard import ChainLink
from videoforge.providers.base import (
    CheckResult,
    FrameToReview,
    FrameVerdict,
    GenResult,
    ImageCandidate,
    ImageProvider,
    ImageSearchProvider,
    LlmProvider,
    LlmResult,
    MotionGuide,
    ProviderError,
    PublishMeta,
    PublishProvider,
    PublishResult,
    Reference,
    Rejected,
    ReviewResult,
    TranscriptProvider,
    TranscriptResult,
    TtsProvider,
    VideoProvider,
    landed_state,
)

CHARS_PER_SECOND = 14  # средний темп русской речи — для длины тишины


def _color(seed: str) -> tuple[int, int, int]:
    h = int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:6], 16) / 0xFFFFFF
    r, g, b = colorsys.hsv_to_rgb(h, 0.45, 0.75)
    return int(r * 255), int(g * 255), int(b * 255)


class MockImage(ImageProvider):
    name = "mock"
    price_usd = 0.0

    def generate(
        self, prompt: str, *, references: list[Reference], aspect_ratio: str, out_path: Path
    ) -> GenResult:
        w, h = aspect_size(aspect_ratio)
        img = Image.new("RGB", (w, h), _color(prompt))
        draw = ImageDraw.Draw(img)
        font_size = max(24, w // 30)
        font = ImageFont.load_default(size=font_size)
        ref_lines = [f"[ref] {r.label}" for r in references]
        lines = ["[mock image]", *ref_lines, ""] + textwrap.wrap(prompt, width=36)[:20]
        y = h // 8
        for line in lines:
            draw.text((w // 12, y), line, fill=(20, 20, 20), font=font)
            y += int(font_size * 1.4)
        img.save(out_path)
        # списываем ровно объявленную цену (по умолчанию $0). Заглушка, которая
        # объявляет прайс и берёт ноль, делает бюджет непроверяемым: тест ставит
        # цену, ждёт перерасхода и не получает его — как и было с F1
        return GenResult(out_path, self.price_usd, {"references": len(references)})


class MockVideo(VideoProvider):
    name = "mock"
    supported_durations = (6, 10)
    # референс движения принимает, но не читает: наезд тот же. Что получила —
    # пишет в `references` и в meta, чтобы тест видел, что стадия его передала
    follows_reference = True

    def __init__(self) -> None:
        self.references: list[MotionGuide | None] = []  # по вызову animate, в порядке сцен

    def animate(
        self,
        image: Path,
        prompt: str,
        *,
        duration_sec: int,
        aspect_ratio: str,
        out_path: Path,
        chain: ChainLink | None = None,
        motion_reference: MotionGuide | None = None,
    ) -> GenResult:
        # Заглушка ведёт себя как i2v: каждый клип начинает наезд с исходного
        # масштаба (`image_to_video` — 12% за клип), места в цепочке не знает.
        # Так на заглушках проходится путь сборки «стык сшивает кроп»
        self.references.append(motion_reference)
        image_to_video(image, out_path, duration=duration_sec, size=aspect_size(aspect_ratio))
        meta: dict[str, Any] = {"prompt": prompt}
        if motion_reference is not None:
            meta["motion_reference"] = str(motion_reference.path)
            meta["signal"] = motion_reference.signal
        return GenResult(out_path, self.price_for(duration_sec), meta)


class MockStill(VideoProvider):
    """Картинка-сцена на заглушке: тот же наезд ffmpeg-ом, что у MockVideo, но
    ведёт себя как программная камера (remotion): любая длительность, цена 0,
    место в цепочке продолжений принимает. Чем клип отличался бы у remotion,
    заглушка не рисует — пишет, что получила (`chains`), чтобы тест видел, что
    стадия отдала звено именно ей."""

    name = "mock"
    chains_camera = True
    supported_durations = (60,)

    def __init__(self) -> None:
        self.chains: list[ChainLink | None] = []  # по вызову animate, в порядке сцен
        self.problem = ""  # что вернёт not_ready: тест ставит «node не найден»

    def not_ready(self) -> str:
        return self.problem

    def pick_duration(self, wanted: float) -> int:
        return max(1, math.ceil(wanted - 1e-6))

    def animate(
        self,
        image: Path,
        prompt: str,
        *,
        duration_sec: int,
        aspect_ratio: str,
        out_path: Path,
        chain: ChainLink | None = None,
        motion_reference: MotionGuide | None = None,
    ) -> GenResult:
        if motion_reference is not None:
            raise ProviderError("mock still: референс движения программной камере не нужен")
        self.chains.append(chain)
        image_to_video(image, out_path, duration=duration_sec, size=aspect_size(aspect_ratio))
        return GenResult(out_path, 0.0, {"prompt": prompt, "still": True})


def _srt_timestamp(seconds: float) -> str:
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")


def _mock_srt(text: str, seconds: float, *, lead: float = 0.0) -> str:
    """Пословные тайминги, равномерно размазанные по `seconds` речи с `lead`.

    edge-tts отдаёт настоящие WordBoundary; заглушке важно, чтобы файл
    существовал, был разборчивым и лежал внутри вшитой тишины так же, как у
    сервиса, — иначе срез тишины на mock нечего проверять."""
    words = text.split()
    if not words:
        return ""
    step = seconds / len(words)
    blocks = [
        f"{i}\n{_srt_timestamp(lead + (i - 1) * step)} --> {_srt_timestamp(lead + i * step)}"
        f"\n{word}\n"
        for i, word in enumerate(words, start=1)
    ]
    return "\n".join(blocks)


# вшитая тишина заглушки — как у edge-tts william (замер 16.09.2026,
# research/_probes/speech_pauses_2026-09-16): до первого слова и после последнего
MOCK_LEAD_SEC = 0.2
MOCK_TRAIL_SEC = 0.4


class MockTts(TtsProvider):
    name = "mock"
    extension = ".wav"

    def synthesize(
        self,
        text: str,
        *,
        voice: str,
        rate: str,
        pitch: str,
        out_path: Path,
        volume: str = "+0%",
        context: tuple[str, str] | None = None,
    ) -> GenResult:
        # подача (rate/pitch/volume) на тишину не влияет: заглушка принимает и не
        # слушает, как настоящий провайдер без такой ручки
        speech = max(1.0, len(text) / CHARS_PER_SECOND)
        seconds = MOCK_LEAD_SEC + speech + MOCK_TRAIL_SEC
        sample_rate = 24000
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(b"\x00\x00" * int(seconds * sample_rate))
        srt_path = out_path.with_suffix(".srt")
        srt_path.write_text(_mock_srt(text, speech, lead=MOCK_LEAD_SEC), encoding="utf-8")
        return GenResult(out_path, 0.0, {"seconds": seconds, "voice": voice, "srt": srt_path.name})


def _mock_scenes(prompt: str, *, offstyle: bool = False) -> list[dict[str, object]]:
    """Раскадровка-заглушка: столько сцен, сколько попросил планнер.

    Нужна ровно затем, чтобы сквозной тест доходил до сборки без платных
    вызовов; содержание сцен бессмысленное и таким и должно быть. Но форму
    заглушка обязана держать ту же, что требуется от живой модели: длительности
    разные (иначе не проверить, что ритм перестал быть ровным) и движение
    разное — раскадровка с одним движением на всех сценах не проходит проверку
    на однообразие, и правильно делает. `offstyle` — первая сцена описана
    «photorealistic»: промпт против стиля, как на ep002 (09.09.2026), чтобы на
    заглушках проходился путь «конфликт по словам → повтор планнера → блок»."""
    # числа сцен планнер модели не называет (длину решает содержание), и
    # заглушка берёт его так, как взяла бы модель без счёта: хронометраж на
    # медианную длину сцены, названную в промпте ориентиром
    target = float(m.group(1)) if (m := re.search(r"(\d+) seconds in total", prompt)) else 0
    median_re = r"median\s+picture\s+on\s+the\s+analysed\s+channels\s+runs\s+([\d.]+)\s+s"
    median = float(m.group(1)) if (m := re.search(median_re, prompt)) else 0
    count = max(1, round(target / median)) if target and median else 3
    # длины разные, и одна заведомо длиннее любого клипа — так на заглушках
    # проверяются и неровный ритм, и разбиение длинного бита
    lengths = [(4.0, 7.0, 16.0)[(i - 1) % 3] for i in range(1, count + 1)]
    if target:  # раскадровка обязана уложиться в хронометраж эпизода
        scale = target / sum(lengths)
        lengths = [round(x * scale, 1) for x in lengths]
        lengths[-1] = round(lengths[-1] + target - sum(lengths), 1)
    moves = ("slow push in", "pan left", "tilt up", "slow pull back", "orbit right")
    # плашки трёх видов на первых сценах: так на заглушках рисуется и ярлык,
    # и вывод с подсветкой слова, и источник — путь «планнер → ASS → финал»
    # проходится целиком, а не только у субтитров
    plates: list[dict[str, object] | None] = [
        {"kind": "label", "text": "ПУНКТ 1"},
        {"kind": "caption", "text": "Мок-вывод сцены два", "emphasis": ["вывод"]},
        {"kind": "source", "text": "Мок-источник 1:1"},
    ]
    # картинка несёт одно-два предложения по ритму (замер 17.09.2026,
    # features/scene_sentences_by_format): короче ориентира — одно, длиннее —
    # два, длинный план от 15 с — три. Так на заглушках счёт «предложений на
    # картинку» в сообщении планнера не вырождается в «ровно одно везде»
    orientation = median or 6.0
    scenes: list[dict[str, object]] = []
    for i, seconds in enumerate(lengths, start=1):
        move = moves[(i - 1) % len(moves)]
        sentences = 1 if seconds < orientation else (2 if seconds < 15 else 3)
        extra = ("Продолжение", "Итог")[: sentences - 1]
        scenes.append(
            {
                "narration": " ".join(
                    [f"Мок-озвучка сцены {i}."] + [f"{word} сцены {i}." for word in extra]
                ),
                "visual_prompt": f"mock scene {i}, wide shot"
                + (", photorealistic" if offstyle and i == 1 else ""),
                "motion_prompt": move,
                # темп пишет планнер, как живая модель: медленный ход камеры —
                # выдох, остальное — действие; так на заглушках проходится путь
                # «планнер → storyboard.json → блок темпа в промпте клипа»
                "tempo": "calm" if move.startswith("slow") else "action",
                "cast": [],
                "overlay": plates[i - 1] if i <= len(plates) else None,
                "duration_sec": seconds,
            }
        )
    return scenes


def _mock_readability(prompt: str, *, riddle: int) -> str:
    """Что зритель увидит без звука — заглушка. Обычно «сцена» (корень совпадает
    с мок-репликой «Мок-озвучка сцены N»). `riddle` = 1 — загадка только в
    первом кадре, `riddle` ≥ 2 — во всех: так на заглушках проходится и путь
    «загадка → повтор планнера → ворота», и правило «повтор хуже первой попытки
    — остаётся первая» (на ep001 модель починила 6 сцен и написала 12 новых)."""
    ids = re.findall(r"^(\S+) \| ", prompt, re.M)
    frames = []
    for n, sid in enumerate(ids):
        fog = riddle >= 2 or (riddle == 1 and n == 0)
        seen = ["дым", "туман", "пустота"] if fog else ["мок", "сцена", "кадр"]
        frames.append({"id": sid, "seen": seen})
    return json.dumps({"frames": frames}, ensure_ascii=False)


def _mock_readability_verdict(prompt: str) -> str:
    """Вердикт «связано / не связано» — заглушка: всё, что дошло до вердикта,
    не связано, с причиной. До вердикта доходят только сцены без общего корня,
    и на мок-раскадровке это ровно режим riddle."""
    ids = re.findall(r"^(\S+) \| hears:", prompt, re.M)
    return json.dumps(
        {
            "scenes": [
                {"id": i, "related": False, "reason": "мок: покажи предмет реплики"} for i in ids
            ]
        },
        ensure_ascii=False,
    )


def _mock_style_conflicts(prompt: str, *, flag: bool) -> str:
    """Мнение модели о промптах против стиля — заглушка. Обычно пусто: сюда
    доходит раскадровка, чистая по словам, и заглушке придраться не к чему.
    `flag` — первая сцена спорная, с причиной: так на заглушках проходится
    путь «совет → заметка на сцене → ПРОВЕРЬ на воротах», а не только «пусто»."""
    ids = re.findall(r"^(\S+) \| ", prompt, re.M)
    conflicts = (
        [{"scene_id": ids[0], "why": "мок: кадр описан не в технике стиля"}] if flag and ids else []
    )
    return json.dumps({"conflicts": conflicts}, ensure_ascii=False)


def _mock_links(prompt: str) -> list[dict[str, Any]]:
    """Строка `LINKS: [...]` промпта сценариста — звенья с посчитанными секундами."""
    m = re.search(r"^LINKS: (.*)$", prompt, re.M)
    return json.loads(m.group(1)) if m else []


def _mock_long_story(target: float, count: int) -> str:
    """Цепочка длинного ролика: столько звеньев, сколько просит подсказка длины,
    и разной длины — ровная цепочка на заглушке не проверила бы раскладку секунд."""
    unit = target / count
    return json.dumps(
        {
            "gaps": [
                {
                    "id": f"g{i}",
                    "knows": f"знает {i}",
                    "wants": f"хочет {i}",
                    "gets": f"получает {i}",
                    "opens": "" if i == count else f"новый вопрос {i}",
                    "planned_sec": round(unit * (0.8, 1.0, 1.2)[(i - 1) % 3], 1),
                    "emotion": "мок-эмоция",
                    "escalation": "ставка выше",
                }
                for i in range(1, count + 1)
            ],
            "ending": "главный разрыв закрыт внутри ролика",
            "target_duration_sec": target,
        },
        ensure_ascii=False,
    )


def _mock_chapters(prompt: str) -> str:
    """План глав: звенья подряд, пока глава не упрётся в потолок из промпта —
    так поступила бы модель, которой назвали границы."""
    links = _mock_links(prompt)
    ceiling = float(m.group(1)) if (m := re.search(r"each at most (\d+) seconds", prompt)) else 300
    groups: list[list[dict[str, Any]]] = [[]]
    for link in links:
        if groups[-1] and sum(x["planned_sec"] for x in groups[-1]) + link["planned_sec"] > ceiling:
            groups.append([])
        groups[-1].append(link)
    return json.dumps(
        {
            "promise": "мок-обещание начала",
            "throughline": "мок-сквозной вопрос",
            "chapters": [
                {
                    "id": f"ch{i:02d}",
                    "title": f"Мок-глава {i}",
                    "about": f"о чём мок-глава {i}",
                    "links": [x["id"] for x in group],
                    "ends_with": f"мок-крючок {i}",
                }
                for i, group in enumerate(groups, start=1)
            ],
        },
        ensure_ascii=False,
    )


def _mock_chapter_draft(prompt: str) -> str:
    """Глава: блок на звено, слов ровно столько, сколько просит стадия."""
    blocks = []
    for link in _mock_links(prompt):
        words = " ".join(f"слово{n}" for n in range(int(link["words"])))
        blocks.append(
            f"## {link['id']} · Мок-блок {link['id']} · {link['planned_sec']:g}s\n\n"
            f"**Текст:** {words}\n"
            f"**Кадр:** mock frame {link['id']}\n"
            f"**Эмоция:** мок-эмоция {link['id']}"
        )
    return "\n\n".join(blocks)


def _mock_director(step: str, prompt: str = "") -> str:
    """Ответы сценариста по шагам. Заглушка обязана держать форму, а не смысл:
    сквозной тест должен доходить до сборки без единого платного вызова."""
    if step == "story" and (m := re.search(r"LENGTH: (\d+) seconds, about (\d+) links", prompt)):
        return _mock_long_story(float(m.group(1)), int(m.group(2)))
    if step == "chapters":
        return _mock_chapters(prompt)
    if step == "draft_chapter":
        return _mock_chapter_draft(prompt)
    if step == "critique_chapter":
        first = next(iter(_mock_links(prompt)), {"id": "g1"})
        return json.dumps(
            {
                "findings": [
                    {
                        "kind": "ровный ритм",
                        "where": first["id"],
                        "why_viewer_leaves": "мок-причина главы",
                        "fix": "мок-правка главы",
                        "blocking": False,
                    }
                ],
                "verdict": "мок-вердикт главы",
            },
            ensure_ascii=False,
        )
    if step == "seams":
        return json.dumps({"findings": [], "verdict": "мок: стыки держатся"}, ensure_ascii=False)
    if step == "angles":
        return json.dumps(
            {
                "options": [
                    {
                        "id": f"a{i}",
                        "title": f"Мок-подача {i}",
                        "premise": f"Ролик про мок-подачу {i}.",
                        "hook": f"Первая фраза подачи {i}.",
                        "curiosity": i,
                        "novelty": 5 - i,
                        "visual": 3,
                        "payoff": 2,
                        "why": "мок-обоснование",
                    }
                    for i in range(1, 6)
                ],
                "rationale": "мок-сравнение подач",
            },
            ensure_ascii=False,
        )
    if step == "story":
        return json.dumps(
            {
                "gaps": [
                    {
                        "id": f"g{i}",
                        "knows": f"знает {i}",
                        "wants": f"хочет {i}",
                        "gets": f"получает {i}",
                        "opens": "" if i == 3 else f"новый вопрос {i}",
                        "planned_sec": 4.0 * i,
                        "emotion": "мок-эмоция",
                        "escalation": "ставка выше",
                    }
                    for i in range(1, 4)
                ],
                "ending": "главный разрыв закрыт внутри ролика",
                "target_duration_sec": 24.0,
            },
            ensure_ascii=False,
        )
    if step == "draft":
        # слов ровно столько, сколько просит стадия (0.85 × 2.2 × длительность):
        # иначе заглушка бесконечно спорит с проверкой недобора речи
        blocks = []
        for i in range(1, 4):
            seconds = 4 * i
            words = " ".join(f"слово{n}" for n in range(max(4, round(seconds * 0.85 * 2.2))))
            blocks.append(
                f"## g{i} · Мок-блок {i} · {seconds}s\n\n"
                f"**Текст:** {words}\n"
                f"**Кадр:** mock frame {i}\n"
                f"**Эмоция:** мок-эмоция {i}"
            )
        return "\n\n".join(blocks)
    if step == "critique":
        return json.dumps(
            {
                "findings": [
                    {
                        "kind": "ровный ритм",
                        "where": "g2",
                        "why_viewer_leaves": "мок-причина",
                        "fix": "мок-правка",
                        "blocking": False,
                    }
                ],
                "verdict": "мок-вердикт",
            },
            ensure_ascii=False,
        )
    if step == "publish":
        return json.dumps(
            {
                "title": "Мок-заголовок ролика",
                "description": "Мок-описание в два предложения. Смотри целиком.",
                "hashtags": ["#Мок", "шортс"],
            },
            ensure_ascii=False,
        )
    if step == "score":
        return json.dumps(
            {
                "score": 70,
                "score_reasons": {"открытие": "мок", "плотность смен": "мок"},
                "weakest": "мок-провал в середине",
            },
            ensure_ascii=False,
        )
    return json.dumps(  # retention
        {
            "beats": [
                {"t_sec": 4.0 * i, "changes": ["информация"], "note": f"мок-смена {i}"}
                for i in range(0, 7)
            ],
            "blocks": [
                {
                    "id": f"g{i}",
                    "removable": i == 2,
                    "if_removed": "мок-потеря" if i != 2 else "ничего, блок повторяет предыдущий",
                }
                for i in range(1, 4)
            ],
        },
        ensure_ascii=False,
    )


_PUNCT = ".,!?…:;—«»\"'()"


_SFX_KINDS = "impact|whoosh|riser|sub_drop|click|glitch|other"  # Sfx.kind, closed list


def _mock_edit(prompt: str, *, reject: bool) -> str:
    """План монтажа-заглушка: отклонения РАЗНОЙ формы на настоящих сценах.

    Урок планнера: заглушка «всё cut» проходит линт и тесты, ничего не проверяя.
    Поэтому здесь один dip_black (третья сцена, или вторая, если сцен две), один
    акцент (первое слово narration из строки MATERIAL), одна ступень музыки — и
    у каждого `why`. `reject` — добавить сцену, которой нет: так проверяется
    повтор по блоку линта.

    Таймлайн (с 14.09.2026) — той же формы, что просит промпт: один удар
    `zoom_punch` на склейке во вторую сцену (её `at` из MATERIAL), один звук в
    ту же секунду — id из блока AVAILABLE, потому что выдумать id заглушка не
    вправе так же, как модель (линт блокирует), — и одна ступень музыки внутри
    последней сцены, но только когда промпт не говорит, что музыка выключена.
    Хвоста нет: промпт просит его только там, где конец того требует."""
    ids_line = m.group(1) if (m := re.search(r"^SCENES:\s*(.*)$", prompt, re.M)) else ""
    ids = [s.strip() for s in ids_line.split(",") if s.strip()]
    narration = dict(re.findall(r'^(\S+) \| .*\| "(.*)"$', prompt, re.M))
    boxes = {
        sid: (float(at), float(box))
        for sid, at, box in re.findall(r"^(\S+) \| at ([\d.]+)s \| box ([\d.]+)s", prompt, re.M)
    }
    sounds = re.findall(rf"^(\S+) \| ({_SFX_KINDS}) \| [\d.]+s \|", prompt, re.M)
    music_off = "(music is off" in prompt
    scenes: dict[str, dict[str, object]] = {}

    def add(sid: str, why: str, **fields: object) -> None:
        entry = scenes.setdefault(sid, {"scene_id": sid})
        entry.update(fields)
        entry["why"] = f"{entry['why']}; {why}" if entry.get("why") else why

    if ids:
        target = ids[2] if len(ids) >= 3 else ids[-1]
        add(
            target,
            "мок: смена масштаба — затемнение вместо резкого стыка",
            transition_in={"type": "dip_black", "duration_sec": 0.4},
        )
        for sid in ids:
            words = [w.strip(_PUNCT) for w in narration.get(sid, "").split()]
            if words := [w for w in words if w]:
                add(sid, "мок: слово, которое зритель не должен пропустить", emphasis=[words[0]])
                break
        add(ids[-1], "мок: музыка ближе к финалу", music_gain_db=-9)
    if reject:
        add(
            "ghost",
            "мок: сцена, которой нет в раскадровке",
            transition_in={"type": "fade_in", "duration_sec": 0.5},
        )

    effects: list[dict[str, object]] = []
    sfx: list[dict[str, object]] = []
    music: dict[str, object] | None = None
    if len(ids) >= 2 and ids[1] in boxes:
        cut = boxes[ids[1]][0]
        effects.append(
            {
                "at_sec": cut,
                "duration_sec": 0.3,
                "kind": "zoom_punch",
                "why": "мок: удар на склейке",
            }
        )
        if sounds:
            # удар — кадр в кадр с наездом; предпочитаем impact, иначе первый в списке
            hit = next((sid for sid, kind in sounds if kind == "impact"), sounds[0][0])
            sfx.append({"at_sec": cut, "id": hit, "why": "мок: удар кадр в кадр с наездом"})
        if not music_off and ids[-1] in boxes:
            at, box = boxes[ids[-1]]
            music = {
                "cues": [
                    {
                        "at_sec": round(at + box / 2, 2),
                        "gain_db": -12,
                        "why": "мок: ступень внутри реплики, а не на границе сцены",
                    }
                ]
            }
    return json.dumps(
        {
            "caption_style": None,
            "music": music,
            "scenes": list(scenes.values()),
            "effects": effects,
            "sfx": sfx,
            "outro": None,
        },
        ensure_ascii=False,
    )


def _mock_highlights(prompt: str, *, retry: bool) -> str:
    """Кандидаты в шортсы-заглушка: моменты РАЗНОЙ длины на настоящих строках
    транскрипта из промпта.

    Один кандидат заведомо короткий (2 с): без него на заглушках не проверить ни
    починку границ, ни повтор по блоку. На повторе короткого нет — так путь
    «блок → причина → исправленный ответ» проходится целиком, а не упирается в
    заглушку, которая упрямо возвращает одно и то же."""

    def sec(text: str) -> float:
        h, m, s = (float(x) for x in text.split(":"))
        return h * 3600 + m * 60 + s

    def clock(value: float) -> str:
        h, rest = divmod(int(value), 3600)
        m, s = divmod(rest, 60)
        return f"{h}:{m:02d}:{s:02d}"

    times = sorted({sec(t) for t in re.findall(r"^\[(\d+:\d+:\d+)\]", prompt, re.M)})
    # Кусок транскрипта: блок PART называет окно, и кандидаты обязаны лежать в
    # нём. Строки промпта — это и есть окно, поэтому заглушка не фильтрует, а
    # СВЕРЯЕТ: разошлись — ошибка промпта, и её надо увидеть в тесте, а не
    # получить мок-момент из минут, которых модели не показывали
    if m := re.search(r"^PART \d+ OF \d+: (\d+:\d+:\d+)[–-](\d+:\d+:\d+) ", prompt, re.M):
        win_start, win_end = sec(m.group(1)), sec(m.group(2))
        if times and (times[0] < win_start - 1 or times[-1] > win_end):
            raise ValueError(
                f"мок: строки транскрипта {clock(times[0])}–{clock(times[-1])} "
                f"вне окна куска {m.group(1)}–{m.group(2)}"
            )
    wanted = [(0.2, 45.0, "story"), (0.5, 30.0, "claim")] + ([] if retry else [(0.8, 2.0, "humor")])
    candidates = []
    for i, (where, length, kind) in enumerate(wanted, start=1):
        if not times:
            break
        start = times[min(int(len(times) * where), len(times) - 1)]
        later = [t for t in times if t >= start + length]
        if not later:
            continue
        candidates.append(
            {
                "start": clock(start),
                "end": clock(later[0]),
                "title": f"Мок-момент {i}",
                "hook": f"мок-крючок {i}",
                "why": f"мок-причина {i}: заглушка обязана держать форму, а не смысл",
                "kind": kind,
            }
        )
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def _mock_critique(prompt: str) -> str:
    """Критик моментов-заглушка: закрывает каждый третий, остальным пишет
    упаковку. Один закрытый нужен, чтобы на заглушках проходился путь «критик
    закрыл → момент в rejected с причиной»; сплошное «keep» ничего бы не
    проверило, а сплошное «drop» не оставило бы что паковать."""
    ids = re.findall(r"^### (\S+) ", prompt, re.M)
    out = []
    for i, cid in enumerate(ids):
        keep = i % 3 != 2
        item: dict[str, object] = {
            "id": cid,
            "keep": keep,
            "why": (
                f"мок: держит на конкретике {cid}" if keep else "мок: зритель не поймёт, о ком речь"
            ),
        }
        if keep:
            item |= {
                "title": f"Мок-заголовок {cid}",
                "description": f"Мок-описание момента {cid}. Смотри выпуск целиком.",
                "hashtags": ["мок", "подкаст", "шортс"],
            }
        out.append(item)
    return json.dumps({"verdicts": out}, ensure_ascii=False)


def _mock_qa(prompt: str) -> str:
    """QA-заглушка: первый стык — «шов виден», первый кадр — «субтитры плохо
    читаются», остальное чисто.

    Один осмысленный вердикт на каждый вид разбора нужен, чтобы путь «замечание
    → qa.json → сообщение ворот → отчёт» проходился на заглушках; сплошное «ok»
    ничего бы не проверило."""
    if "FRAMES (" in prompt:  # разбор отдельных кадров, а не стыков
        names = re.findall(r"^(\S+\.jpg):", prompt, re.M)
        frames = [
            {
                "frame": name,
                "captions": "hard_to_read" if i == 0 else "ok",
                "framing": "ok",
                "note": "мок: субтитры сливаются со светлым фоном" if i == 0 else "",
            }
            for i, name in enumerate(names)
        ]
        return json.dumps({"frames": frames}, ensure_ascii=False)
    ids = re.findall(r"^(\S+>\S+)(?: \([^)]*\))?:", prompt, re.M)  # «s1>s2 (dip_black): …»
    cuts = [
        {
            "cut": cid,
            "seam": "visible" if i == 0 else "ok",
            "captions": "ok",
            "note": "мок: композиция до и после стыка повторяется" if i == 0 else "",
        }
        for i, cid in enumerate(ids)
    ]
    return json.dumps({"cuts": cuts}, ensure_ascii=False)


def _mock_translate(prompt: str) -> str:
    """Перевод-заглушка: каждой реплике — «EN n: <текст>», по одной на строку
    входа. Реплики читаются из строки `LINES:` промпта, поэтому заглушка держит
    ту же форму, что настоящая модель: столько же строк, те же номера."""
    m = re.search(r"^LINES:\s*(\[.*\])\s*$", prompt, re.MULTILINE)
    lines = json.loads(m.group(1)) if m else []
    return json.dumps(
        {"lines": [{"n": ln["n"], "text": f"EN {ln['n']}: {ln['t']}"} for ln in lines]},
        ensure_ascii=False,
    )


class MockLlm(LlmProvider):
    name = "mock"

    def __init__(self) -> None:
        self.edit_calls = 0  # сколько раз звали монтажёра: тесты свежести плана
        # первые N ответов монтажёра — с несуществующей сценой (тест повтора по блоку)
        self.reject_edit_calls = 0
        self.qa_calls = 0
        self.highlight_calls = 0  # тесты свежести highlights.json
        self.translate_calls = 0  # тесты свежести перевода субтитров
        self.planner_calls = 0  # сколько раз планнер просил раскадровку (повтор по блоку)
        # первые N раскадровок — с «photorealistic» в первой сцене (промпт
        # против стиля): один такой ответ — повтор планнера, два — блок
        self.planner_offstyle_calls = 0
        # первые N раскадровок — со второй сценой без реплики (немая сцена
        # посреди ролика, как «выдохи» ep001): один такой ответ — повтор, два — блок
        self.planner_mute_calls = 0
        self.planner_prompts: list[str] = []  # что планнер просил: тесты текста повтора
        self.style_calls = 0  # сколько раз спрашивали мнение модели о стиле
        self.style_flag_calls = 0  # первые N мнений называют первую сцену спорной
        self.readability_calls = 0
        # первые N описаний кадров — «загадки» (слова не из реплики): первый
        # такой вызов портит один кадр, следующие — все (см. _mock_readability)
        self.readability_riddle_calls = 0
        self.check_calls = 0  # сколько раз проверяли кадр или клип
        # вердикты первых проверок по порядку: список замечаний на каждую,
        # пустой список — пройдена; дальше все проходят. Так на заглушках
        # проходится путь «попытки с разным числом замечаний → оставлена лучшая,
        # а не последняя» (ep002: третья «прошла» тот же поток вверх)
        self.check_issues: list[list[str]] = []
        # проверка по описанию пачкой: вердикты на сцену — очередь по id, по
        # одному на каждую проверку этой сцены; кончилась — кадр про то
        self.review_verdicts: dict[str, list[FrameVerdict]] = {}
        self.review_calls = 0
        self.reviewed: list[list[str]] = []  # id сцен каждой пачки — тесты размера пачки
        self.pick = 1  # какого кандидата выбирать как референс; 0 — «ни один»
        self.pick_calls = 0

    reviews_frames = True

    def review_frames(self, frames: list[FrameToReview]) -> ReviewResult:
        self.review_calls += 1
        self.reviewed.append([f.id for f in frames])
        verdicts = []
        for f in frames:
            queue = self.review_verdicts.get(f.id)
            if queue:
                verdicts.append(queue.pop(0))
            else:
                verdicts.append(FrameVerdict(id=f.id, match=True, see="mock frame"))
        return ReviewResult(verdicts=verdicts, raw="mock")

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        images: list[Path] | None = None,
    ) -> LlmResult:
        # шаг стадии подписан маркером в первой строке — надёжнее, чем
        # угадывать по формулировкам, которые меняются вместе с промптом
        if m := re.match(r"VIDEOFORGE-STEP:\s*(\w+)", prompt):
            if m.group(1) == "reference_pick":
                self.pick_calls += 1
                return LlmResult(text=json.dumps({"pick": self.pick, "why": "mock: по листу"}))
            if m.group(1) == "edit":
                self.edit_calls += 1
                reject = self.edit_calls <= self.reject_edit_calls
                return LlmResult(text=_mock_edit(prompt, reject=reject))
            if m.group(1) == "qa":
                self.qa_calls += 1
                return LlmResult(text=_mock_qa(prompt))
            if m.group(1) == "highlights_critique":
                self.highlight_calls += 1
                return LlmResult(text=_mock_critique(prompt))
            if m.group(1) == "highlights":
                self.highlight_calls += 1
                return LlmResult(text=_mock_highlights(prompt, retry="REJECTED" in prompt))
            if m.group(1) == "translate":
                self.translate_calls += 1
                return LlmResult(text=_mock_translate(prompt))
            if m.group(1) == "readability":
                self.readability_calls += 1
                # первый «загадочный» вызов портит один кадр, следующие — все
                riddle = (
                    self.readability_calls
                    if self.readability_calls <= self.readability_riddle_calls
                    else 0
                )
                return LlmResult(text=_mock_readability(prompt, riddle=riddle))
            if m.group(1) == "readability_verdict":
                return LlmResult(text=_mock_readability_verdict(prompt))
            if m.group(1) == "style_conflict":
                self.style_calls += 1
                flag = self.style_calls <= self.style_flag_calls
                return LlmResult(text=_mock_style_conflicts(prompt, flag=flag))
            return LlmResult(text=_mock_director(m.group(1), prompt))
        if "storyboard" in prompt and "visual_prompt" in prompt:
            self.planner_calls += 1
            self.planner_prompts.append(prompt)
            offstyle = self.planner_calls <= self.planner_offstyle_calls
            scenes = _mock_scenes(prompt, offstyle=offstyle)
            if self.planner_calls <= self.planner_mute_calls and len(scenes) > 2:
                scenes[1]["narration"] = ""
            return LlmResult(text=json.dumps(scenes, ensure_ascii=False))
        return LlmResult(text='{"mock": true}' if json_mode else "mock")

    def check_images(
        self, images: list[Path], *, references: list[Reference], checklist: list[str], context: str
    ) -> CheckResult:
        self.check_calls += 1
        if self.check_calls <= len(self.check_issues):
            issues = list(self.check_issues[self.check_calls - 1])
            return CheckResult(passed=not issues, issues=issues, raw="mock")
        return CheckResult(passed=True, raw="mock")


class MockSearch(ImageSearchProvider):
    """Поиск-заглушка: рисует `candidates` серых картинок с текстом запроса во
    временной папке и отдаёт их как найденные (ссылка — file://). `files` —
    свои кандидаты вместо нарисованных; `candidates = 0` — «ничего не нашлось»."""

    name = "mock"

    def __init__(self, files: list[Path] | None = None, *, candidates: int = 2) -> None:
        self.files = files
        self.candidates = candidates
        self.calls: list[str] = []
        self._dir: Path | None = None

    def search(self, query: str, *, limit: int = 8) -> list[ImageCandidate]:
        import tempfile

        self.calls.append(query)
        if self.files is not None:
            paths = list(self.files)[:limit]
        else:
            if self._dir is None:
                self._dir = Path(tempfile.mkdtemp(prefix="vf-mock-search-"))
            paths = []
            for i in range(min(self.candidates, limit)):
                p = self._dir / f"{len(self.calls)}_{i + 1}.png"
                img = Image.new("RGB", (640, 360), (90 + 40 * i, 90 + 40 * i, 100))
                ImageDraw.Draw(img).text(
                    (24, 24), f"[mock photo {i + 1}] {query}", fill=(240, 240, 240)
                )
                img.save(p)
                paths.append(p)
        return [
            ImageCandidate(
                url=p.resolve().as_uri(),
                title=f"mock {i + 1}: {query}",
                page=f"mock://search/{i + 1}",
                license="CC0",
                width=640,
                height=360,
            )
            for i, p in enumerate(paths)
        ]

    def fetch(self, candidate: ImageCandidate, out_path: Path) -> Path:
        import shutil
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        src = Path(url2pathname(urlparse(candidate.url).path))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out_path)
        return out_path


class MockTranscriber(TranscriptProvider):
    """Транскрипт-заглушка: два слова в секунду ровной речи с паузой каждые
    десять слов — так на заглушке есть и слова, и границы предложений."""

    name = "mock"

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(
        self,
        *,
        url: str,
        video: Path | None,
        language: str,
        duration_sec: float,
        out_path: Path,
    ) -> TranscriptResult:
        self.calls += 1
        seconds = duration_sec or (probe_duration(video) if video and video.exists() else 60.0)
        words: list[Word] = []
        t, n = 0.0, 0
        while t < seconds - 0.3:
            n += 1
            words.append(Word(f"слово{n}", round(t, 3), round(t + 0.4, 3)))
            t += 0.5 + (1.2 if n % 10 == 0 else 0.0)  # пауза — граница предложения
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(words_to_srt(words), encoding="utf-8")
        return TranscriptResult(words_srt=out_path, language=language, meta={"words": len(words)})


class MockPublish(PublishProvider):
    """Публикация-заглушка: файл никуда не уходит, id выдуманный.

    Нужна, чтобы конвейер публикации проверялся тестами целиком — от чтения
    `publish.json` до записи результата в манифест — и ни один тест при этом не
    трогал живой аккаунт."""

    name = "mock"
    platforms = ("youtube", "tiktok", "instagram")

    def __init__(self) -> None:
        self.uploads: list[tuple[Path, PublishMeta]] = []
        # что «лежит на площадке»: заглушка помнит это, чтобы правка уже
        # опубликованного проверялась целиком — от чтения до «было → станет»
        self.posted: dict[str, PublishMeta] = {}
        self.edits: list[tuple[str, PublishMeta]] = []
        # что площадка «говорит» о ролике сверх «обработан»: тест кладёт сюда
        # {"upload_status": "rejected", "reason": "copyright"} и смотрит, что
        # проверка состояния с этим делает
        self.states: dict[str, dict[str, str]] = {}

    def upload(self, video_path: Path, meta: PublishMeta) -> PublishResult:
        if not video_path.exists():
            # файл не ушёл, и это известно — тот же контракт, что у живых
            # адаптеров: `Rejected` значит «ролика там нет»
            raise Rejected(f"файла нет: {video_path}")
        self.uploads.append((video_path, meta))
        vid = f"mock{len(self.uploads):03d}"
        self.posted[vid] = meta
        return PublishResult(
            platform=meta.platform,
            state="done",
            video_id=vid,
            url=f"https://example.invalid/{meta.platform}/{vid}",
            request_id=vid,
            meta={
                "title": meta.title,
                "privacy": meta.privacy,
                "synthetic": meta.synthetic,
                "thumbnail": "done" if meta.thumbnail else "",
            },
        )

    def check_status(self, request_id: str) -> PublishResult:
        """Известный ролик — обработан и лежит с той видимостью, с какой уехал;
        `states[id]` перекрывает любое из слов площадки; чужой id — ролика нет.
        Тот же словарь, что у YouTube: стадия читает одни и те же ключи."""
        meta = self.posted.get(request_id)
        said = self.states.get(request_id)
        if meta is None and said is None:
            return PublishResult(
                platform="mock", state="failed", video_id=request_id, meta={"found": False}
            )
        words: dict[str, Any] = {
            "found": True,
            "upload_status": "processed",
            "processing": "succeeded",
            "reason": "",
            "privacy": meta.privacy if meta else "",
            "notes": [],
        }
        words.update(said or {})
        return PublishResult(
            platform="mock",
            state=landed_state(str(words["upload_status"]), str(words["processing"])),
            video_id=request_id,
            url=f"https://example.invalid/mock/{request_id}",
            request_id=request_id,
            meta=words,
        )

    def find_video(self, title: str, *, since: datetime) -> list[PublishResult]:
        """Свои ролики по названию. Времени загрузки заглушка не помнит, поэтому
        `since` не смотрит: тест, которому нужен «старый» ролик, пишет другое
        название."""
        wanted = " ".join(title.split())
        return [
            PublishResult(
                platform="mock",
                state="done",
                video_id=vid,
                url=f"https://example.invalid/mock/{vid}",
                request_id=vid,
                meta={"title": meta.title, "at": None},
            )
            for vid, meta in self.posted.items()
            if " ".join(meta.title.split()) == wanted
        ]

    def describe(self, video_id: str) -> dict[str, str]:
        meta = self.posted.get(video_id)
        if meta is None:
            raise ProviderError(f"ролика {video_id} на площадке-заглушке нет")
        return {
            "title": meta.title,
            "description": meta.tagged_description(),
            "tags": ", ".join(meta.hashtags),
            "language": meta.language,
            "text_language": meta.text_language or meta.language,
            "privacy": meta.privacy,
        }

    def update(self, video_id: str, meta: PublishMeta) -> PublishResult:
        if video_id not in self.posted:
            raise ProviderError(f"ролика {video_id} на площадке-заглушке нет")
        self.posted[video_id] = meta
        self.edits.append((video_id, meta))
        return PublishResult(
            platform=meta.platform,
            state="done",
            video_id=video_id,
            url=f"https://example.invalid/{meta.platform}/{video_id}",
            meta={"privacy": meta.privacy, "thumbnail": "done" if meta.thumbnail else ""},
        )
