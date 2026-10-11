"""Озвучка: narration → 04_audio/<scene>.wav + пословный .srt. Сцены без текста пропускаются.

Стадия идёт раньше картинок и видео намеренно: ритм задаёт голос. Сколько сцена
длится — не догадка планнера, а измеренная речь плюс пауза до следующей
реплики. Платные стадии ниже получают факт.

Пауза считается, а не достаётся от сервиса (16.09.2026,
`features/speech_pause_by_class`): тишину, которую TTS вшивает в начало и конец
файла, стадия срезает по таймингам слов, а паузу стыка ставит по смыслу
(`core/pauses.py`) — конец предложения, вопрос, новый пункт, конец блока
сценария, разрез внутри предложения. До этого после почти каждого предложения
звучало 1.5–1.7 с тишины, и ролик шёл 81 слово в минуту при 118–134 у
конкурентов.

Склейка — посреди этой паузы (17.09.2026, `features/cut_mid_pause`): новая
картинка появляется в тишине, до первого слова своей реплики. Сколько картинка
идёт до речи, стадия пишет в `measured_sec.lead_in`, а сборка сдвигает на это
звук.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from videoforge.core import pronunciation
from videoforge.core.captions import Word, parse_srt_words, words_to_srt
from videoforge.core.ffmpeg import probe_duration, trim_audio
from videoforge.core.pauses import (
    LEAD_IN_KEY,
    PauseClass,
    ScriptMarks,
    lead_in,
    pause_after,
    pause_class,
    script_marks,
    shift_words,
    trim_window,
)
from videoforge.core.project import SCRIPT_DIR
from videoforge.core.resources import Voice
from videoforge.core.script import SCRIPT_FILE, parse_script
from videoforge.core.stage import Stage, StageContext, StageResult, adopt_old_assets
from videoforge.core.storyboard import Scene, fingerprint

OUT_DIR = "04_audio"
AUDIO_EXT = ".wav"  # после среза: PCM, без задержки кодера на краях (`trim_audio`)
RAW_KEY = "audio_raw"  # measured_sec: длина файла TTS до среза — и метка «срезано»

DEFAULT_TAIL_SEC = 0.5  # если эпизод лежит вне проекта и настройки взять негде
MIN_SCENE_SEC = 2.0  # короче кадр не успевает прочитаться, даже если реплика в слово
# насколько планнер вправе растянуть паузу сцены сверх паузы по классу. Было
# 1.5 с (ep001, 08.09.2026: без предела 381 с речи стали 507 с). 0.3 с — столько,
# чтобы пауза на конце предложения (0.45 ± 0.1) с добивкой не вышла за p90
# конкурентов (0.76–0.91 с) и за порог медианы готового файла 0.8 с
MAX_PAD_SEC = 0.3
# сцена без слов живёт не весь план, а столько, сколько законно молчать: вспышка
# до первого слова и конечная заставка — те же числа, что
# `planner.SILENT_OPENING_SEC` и `SILENT_END_SCREEN_SEC` (планнер импортирует
# отсюда MIN_SCENE_SEC, поэтому обратного импорта нет; равенство держит тест).
# Немая сцена посреди ролика — наследие раскадровок до 16.09.2026: не дольше
# секунды (у конкурентов пауз дольше 2.1 с посреди ролика нет)
MUTE_OPENING_SEC = 1.0
MUTE_END_SCREEN_SEC = 5.0
MUTE_MIDDLE_SEC = 1.0

# Озвучка фразами (`Voice.phrase`, 28.09.2026). Сцена — полтора предложения, и
# голос, получивший её отдельно, начинал интонацию заново на каждой склейке и
# обрывал на ней мысль: «половину предложения говоришь, потом без остановки
# полтора продолжаешь» (владелец). Фраза — подряд идущие сцены до конца
# предложения, не короче PHRASE_CHARS знаков; блок сценария и новый пункт её
# закрывают всегда — там пауза длиннее той, что голос сделает сам
PHRASE_CHARS = 600
PHRASE_MAX_CHARS = 1500  # предложение без конца длиннее этого режется всё равно
CONTEXT_CHARS = 300  # сколько соседнего текста слышит голос, который это умеет
# measured_sec: тишина между последним словом сцены и первым словом следующей
# внутри фразы — та, что поставил сам голос; расчёт стыков берёт её вместо
# паузы по классу
JOINED_GAP_KEY = "joined_gap"


def tokens(text: str) -> list[str]:
    """Слова реплики так, как их считает озвучка: по пробелам, без токенов из
    одних знаков («—», «…»)."""
    return [t for t in text.split() if any(c.isalnum() for c in t)]


def phrases(scenes: list[Scene], marks: ScriptMarks) -> list[list[Scene]]:
    """Сцены с речью → фразы для одного вызова озвучки. Немая сцена фразу
    закрывает: голос через неё не идёт."""
    out: list[list[Scene]] = []
    cur: list[Scene] = []
    chars = 0
    for i, scene in enumerate(scenes):
        if not scene.narration.strip():
            if cur:
                out.append(cur)
            cur, chars = [], 0
            continue
        cur.append(scene)
        chars += len(scene.narration)
        nxt = scenes[i + 1] if i + 1 < len(scenes) else None
        cls = pause_class(scene, nxt, marks)
        closes = cls in ("block", "point") or (
            cls in ("sentence", "question") and chars >= PHRASE_CHARS
        )
        if closes or chars >= PHRASE_MAX_CHARS:
            out.append(cur)
            cur, chars = [], 0
    if cur:
        out.append(cur)
    return out


class _Voiced(NamedTuple):
    """Сцена с речью, как её видит расчёт стыков: файл, слова, пауза после."""

    measured: float  # длина срезанного файла озвучки
    speech_end: float  # конец последнего слова в файле
    head: float  # начало первого слова в файле (запас среза)
    pause: float  # тишина между последним словом и первым словом следующей
    cls: PauseClass


class Span(NamedTuple):
    """Из чего складывается коробка сцены с речью."""

    measured: float  # длина срезанного файла озвучки
    lead_in: float  # заход: картинка до начала файла озвучки
    rest: float  # от начала файла озвучки до склейки в следующую сцену


def tail_sec(ctx: StageContext) -> float:
    return ctx.project.video.scene_tail_sec if ctx.project else DEFAULT_TAIL_SEC


# Знаки ударения (U+0301 и U+0300) сценарий ставит для человека, а голосу они
# вредят: на знаке голос рвёт слово и читает оставшуюся согласную названием
# буквы — «Алла́х» звучит «Аллаха», «Ами́р» — «Амиэр», «Кари́м» — «Кориэм».
# Замер 18.09.2026 (research/_probes/pronunciation_2026-09-18): у всех шести
# голосов, william без знака узнаётся в 21 слове из 30 против 11 со знаком
STRESS_MARKS = ("́", "̀")


def spoken_text(narration: str) -> str:
    """Реплика в том виде, в каком её получает голос: без знаков ударения."""
    for mark in STRESS_MARKS:
        narration = narration.replace(mark, "")
    return narration


def prosody(voice: Voice, scene: Scene) -> tuple[str, str, str]:
    """Подача сцены — (rate, pitch, volume): ресурс голоса, переопределённый
    `Scene.voice` по полю. Поле сцены None — как у ресурса."""
    over = scene.voice
    return (
        over.rate if over and over.rate else voice.rate,
        over.pitch if over and over.pitch else voice.pitch,
        over.volume if over and over.volume else voice.volume,
    )


def is_default(value: str) -> bool:
    """Подача по умолчанию — "+0%", "-0Hz", "+0.0%": та, на которой замерен
    WORDS_PER_SEC."""
    try:
        return float(value.rstrip("%Hz")) == 0
    except ValueError:
        return False


def is_legacy(scene: Scene) -> bool:
    """Озвучка сделана до среза тишины (16.09.2026): файл с вшитой тишиной, и
    длительность сцены посчитана по прежним правилам. Её не пересчитывают —
    новая длина сделала бы устаревшими готовые клипы, и обновление кода
    перегенерировало бы их за деньги (приватные дизайн-заметки, принцип 4).
    Новые паузы
    получает озвучка, сделанная заново: правка реплики или `--force`."""
    return (
        bool(scene.narration.strip() and scene.assets.audio) and RAW_KEY not in scene.measured_sec
    )


def is_uncut(scene: Scene) -> bool:
    """Озвучка сделана до склейки посреди паузы (17.09.2026): заход не
    записан, пауза стыка целиком в хвосте прошлой сцены. Такой сцене заход не
    ставится — коробка сдвинулась бы на него, и клип мог бы устареть; соседняя
    сцена нового правила на стыке с ней заход тоже не получает. Метка —
    ключ `measured_sec.lead_in` (`LEAD_IN_KEY`): его ставит только новая озвучка."""
    return bool(scene.assets.audio) and LEAD_IN_KEY not in scene.measured_sec


def mute_duration(scene: Scene, *, first: bool, last: bool) -> float:
    """Длина сцены без слов: как в раскадровке (там план или правка руками), но
    не дольше законного молчания на её месте."""
    cap = MUTE_OPENING_SEC if first else MUTE_END_SCREEN_SEC if last else MUTE_MIDDLE_SEC
    return round(min(scene.duration_sec, cap), 2)


class AudioStage(Stage):
    name = "audio"
    needs = ("tts",)

    @staticmethod
    def _inputs(ctx: StageContext, scene: Scene) -> str:
        """Из чего сделана озвучка: текст реплики, голос и подача.

        Правка `narration` руками — главный способ вмешаться в этот конвейер,
        и раньше повтор её не замечал: файл на месте, провайдер тот же, значит
        «готово», — а звучал он старым текстом. Подача (`Scene.voice`) — такая
        же правка руками: шёпот, вписанный в сцену, обязан перегнать озвучку.

        Громкость дописывается в отпечаток только когда она не по умолчанию:
        поле появилось 14.09.2026, и седьмая часть в каждом отпечатке сменила
        бы отпечатки всех готовых озвучек разом — первый прогон после
        обновления перегнал бы озвучку живых эпизодов, которые никто не правил.
        По той же причине срез тишины в отпечаток не входит: озвучка до него
        остаётся готовой (`is_legacy`)."""
        voice = ctx.resources.voice
        rate, pitch, volume = prosody(voice or Voice(id="default"), scene)
        # фразой: звук сцены вырезан из общей записи, и сделан он из текста
        # всей фразы и места сцены в ней — правка соседа перегоняет фразу
        said_text = scene.narration
        if voice is not None and voice.phrase:
            index, phrase = _phrase_map(ctx).get(scene.id, (0, [scene]))
            said_text = f"phrase:{index}:" + "\n".join(s.narration for s in phrase)
            rate, pitch, volume = prosody(voice, phrase[0])
        parts: list[object] = [
            said_text,
            ctx.storyboard.language,
            getattr(voice, "id", ""),
            getattr(voice, "voice", ""),
            rate,
            pitch,
        ]
        if not is_default(volume):
            parts.append(volume)
        # словарь произношения — только записи, которые есть в этой реплике, и
        # только когда они есть: отпечатки сцен без таких слов не меняются
        said = pronunciation.used(
            spoken_text(scene.narration), pronunciation.load(ctx.project_root)
        )
        if said:
            parts.append("pron:" + ";".join(f"{k}={v}" for k, v in said.items()))
        return fingerprint(*parts)

    @staticmethod
    def _rate_note(ctx: StageContext, voice: Voice) -> str:
        """Одно предупреждение на прогон, если скорость речи не та, на которой
        замерен WORDS_PER_SEC ("+0%", `core/script.py`): планнер
        раздал сценам секунды по словам, и при другой скорости сцена длится не
        столько — ритм эпизода уже не тот, что он считал. Не запрет: длину
        сцены всё равно задаёт измеренная озвучка, но человек должен знать,
        что план и факт разошлись не случайно."""
        spoken = [s for s in ctx.storyboard.ordered_scenes() if s.narration.strip()]
        scenes = [
            f"{s.id} ({prosody(voice, s)[0]})"
            for s in spoken
            if s.voice and s.voice.rate and not is_default(s.voice.rate)
        ]
        where: list[str] = []
        if spoken and not is_default(voice.rate):
            where.append(f"ресурс {voice.id} {voice.rate}")
        if scenes:
            where.append("сцены " + ", ".join(scenes))
        if not where:
            return ""
        note = (
            "скорость голоса не по умолчанию ("
            + "; ".join(where)
            + ") — WORDS_PER_SEC замерен на +0%, планнер считал слова по нему"
        )
        ctx.log.warning(note)
        return f" · ПРОВЕРЬ: {note}"

    @classmethod
    def _ready(cls, ctx: StageContext, scene: Scene) -> bool:
        if not scene.narration.strip():
            return scene.checks.audio == "skipped"
        return (
            scene.asset_ready("audio", ctx.providers.tts.name, cls._inputs(ctx, scene))
            and ctx.abs(scene.assets.audio or "").exists()
        )

    def _todo(self, ctx: StageContext) -> list[Scene]:
        return [
            s
            for s in ctx.storyboard.ordered_scenes()
            if ctx.options.force or not self._ready(ctx, s)
        ]

    def estimate_cost(self, ctx: StageContext) -> float:
        chars = sum(len(s.narration) for s in self._todo(ctx))
        return chars * ctx.providers.tts.price_per_char

    @staticmethod
    def _words(ctx: StageContext, scene: Scene) -> list[Word]:
        rel = scene.assets.captions
        if not rel or not ctx.abs(rel).exists():
            return []
        return parse_srt_words(ctx.abs(rel).read_text(encoding="utf-8"))

    @staticmethod
    def _marks(ctx: StageContext, scenes: list[Scene]) -> ScriptMarks:
        """Где в сценарии кончаются блоки и начинаются абзацы — для класса
        паузы. Сценария нет (раскадровка написана руками) — классы по знакам."""
        path = ctx.project_dir / SCRIPT_DIR / SCRIPT_FILE
        if not path.exists():
            return ScriptMarks()
        return script_marks(scenes, parse_script(path.read_text(encoding="utf-8")))

    @staticmethod
    def _spans(
        scenes: list[Scene],
        words: dict[str, list[Word]],
        marks: ScriptMarks,
        *,
        tail: float,
        ceiling: float,
    ) -> dict[str, Span]:
        """Заход и остаток коробки каждой сцены с речью, озвученной после среза
        тишины (сцены прежних правил и без замера сюда не попадают).

        Сначала заходы на стыках двух сцен, озвученных по правилу склейки
        посреди паузы, потом проход с конца: остаток коробки зависит от захода
        следующей сцены. Заход, с которым сцена не влезла бы в самый длинный
        клип провайдера, возвращается хвосту прошлой: речь влезает, а кадр
        замёрз бы из-за тишины перед ней."""
        voiced: dict[str, _Voiced] = {}
        for i, scene in enumerate(scenes):
            measured = scene.measured_sec.get("audio")
            if not scene.narration.strip() or is_legacy(scene) or not measured:
                continue
            nxt = scenes[i + 1] if i + 1 < len(scenes) else None
            own = words.get(scene.id) or []
            # внутри фразы паузу уже поставил голос — её и держим
            joined = scene.measured_sec.get(JOINED_GAP_KEY)
            voiced[scene.id] = _Voiced(
                measured=measured,
                speech_end=own[-1].end if own else measured,
                head=own[0].start if own else 0.0,
                pause=joined
                if joined is not None and nxt is not None
                else pause_after(scene, nxt, marks, tail_sec=tail),
                cls=pause_class(scene, nxt, marks),
            )
        leads: dict[str, float] = {}
        for prev, scene in zip(scenes, scenes[1:], strict=False):
            p, s = voiced.get(prev.id), voiced.get(scene.id)
            if p is None or s is None or is_uncut(prev) or is_uncut(scene):
                continue  # стык со сценой прежних правил или немой: пауза целиком в хвосте
            leads[scene.id] = lead_in(
                p.cls, p.pause, head=s.head, tail_pad=max(0.0, p.measured - p.speech_end)
            )
        spans: dict[str, Span] = {}
        for i in reversed(range(len(scenes))):
            scene = scenes[i]
            v = voiced.get(scene.id)
            if v is None:
                continue
            nxt = scenes[i + 1] if i + 1 < len(scenes) else None
            following = words.get(nxt.id) if nxt is not None else None
            # склейка стоит за (заход следующей + начало её первого слова) до слова
            before_word = (following[0].start if following else 0.0) + (
                spans[nxt.id].lead_in if nxt is not None and nxt.id in spans else 0.0
            )
            rest = max(v.measured, v.speech_end + v.pause - before_word)
            lead = leads.get(scene.id, 0.0)
            if lead > 0 and lead + rest > ceiling:
                lead = round(max(0.0, ceiling - rest), 3)
            spans[scene.id] = Span(measured=v.measured, lead_in=lead, rest=rest)
        return spans

    @classmethod
    def _apply_timing(cls, ctx: StageContext) -> bool:
        """duration_sec сцен — из речи и паузы до следующей реплики, но не
        короче того, что просила история (`planned_sec`), в пределах
        `MAX_PAD_SEC`. Идёт по всем сценам, а не только по свежим: проход
        детерминирован и чинит раскадровку после ручных правок.

        Между последним словом сцены и первым словом следующей звучит ровно
        пауза класса, а не пауза плюс вшитая тишина плюс хвост кадра. Склейка
        стоит посреди этой паузы (замер 17.09.2026): часть паузы — хвост
        уходящей сцены, часть — заход новой (`measured_sec.lead_in`, картинка
        до её речи). Коробка сцены = заход + конец последнего слова + пауза −
        (заход следующей + начало её первого слова); сумма коробок та же, что
        без захода, — меняется место склейки, а не хронометраж.

        Заход ставится только между двумя сценами, озвученными по этому правилу
        (`is_uncut` — нет): у сцены, озвученной раньше, коробка остаётся
        прежней, и её клип не устаревает. Озвучка до среза тишины (`is_legacy`)
        и немые сцены эпизода, где она есть, не трогаются."""
        tail = tail_sec(ctx)
        # потолок — самый длинный клип ИЗ ТЕХ, кем ролик будет рендериться:
        # при доле клипов < 100% длинная сцена уйдёт картинкой с наездом, а у
        # неё длина любая. По потолку одного видеопровайдера коробка сцены
        # обрезалась короче речи, и хвост доигрывал замороженный кадр —
        # ep003, 18.09.2026: 20 сцен, 30 с заморозки
        ceiling = max(ctx.providers.video.supported_durations)
        stills = ctx.providers.still
        mixes = bool(ctx.project and ctx.project.video.clip_share < 1)
        if mixes and stills is not None and stills.supported_durations:
            ceiling = max(ceiling, max(stills.supported_durations))
        scenes = ctx.storyboard.ordered_scenes()
        legacy = any(is_legacy(s) for s in scenes)
        fresh = [s for s in scenes if s.narration.strip() and not is_legacy(s)]
        # немые сцены укорачиваются только там, где голос есть: ролик без речи
        # вовсе (под музыку, реалтайм-проба) живёт планом, а не «заставками»
        spoken = any(s.narration.strip() for s in scenes)
        marks = cls._marks(ctx, scenes) if fresh else ScriptMarks()
        words = {s.id: cls._words(ctx, s) for s in scenes if s.narration.strip()}
        spans = cls._spans(scenes, words, marks, tail=tail, ceiling=ceiling)
        changed = False
        for i, scene in enumerate(scenes):
            nxt = scenes[i + 1] if i + 1 < len(scenes) else None
            if not scene.narration.strip():
                if legacy or not spoken:
                    continue  # раскадровка прежних правил или ролик без речи: живёт как жила
                wanted = mute_duration(scene, first=i == 0, last=nxt is None)
            elif scene.id not in spans:
                continue  # озвучка прежних правил или без замера
            else:
                span = spans[scene.id]
                if not is_uncut(scene) and scene.measured_sec[LEAD_IN_KEY] != span.lead_in:
                    scene.measured_sec[LEAD_IN_KEY] = span.lead_in
                    changed = True
                natural = span.lead_in + span.rest
                floor = max(MIN_SCENE_SEC, min(scene.planned_sec or 0.0, natural + MAX_PAD_SEC))
                wanted = round(max(floor, natural), 2)
                if wanted > ceiling:
                    ctx.log.warning(
                        "сцена %s: озвучка %.1fs длиннее самого длинного клипа провайдера "
                        "(%ds) — в сборке кадр замрёт; сократи narration или разбей сцену",
                        scene.id,
                        span.measured,
                        ceiling,
                    )
                    wanted = float(ceiling)
            if abs(scene.duration_sec - wanted) > 0.005:
                ctx.log.info(
                    "сцена %s: длительность %.2fs → %.2fs (по озвучке)",
                    scene.id,
                    scene.duration_sec,
                    wanted,
                )
                scene.duration_sec = wanted
                changed = True
        if changed:
            ctx.save()
        return changed

    @staticmethod
    def _legacy_note(ctx: StageContext) -> str:
        scenes = ctx.storyboard.ordered_scenes()
        legacy = [s.id for s in scenes if is_legacy(s)]
        # срезано, но до склейки посреди паузы (16–17.09.2026): коробки прежние
        uncut = [s.id for s in scenes if s.narration.strip() and is_uncut(s) and not is_legacy(s)]
        note = ""
        if legacy:
            note += (
                f" · озвучено до среза тишины сцен: {len(legacy)} — длительности не пересчитаны "
                "(новые паузы: videoforge run <эпизод> --only audio --force; клипы, чья длина "
                "уйдёт дальше допуска, станут устаревшими)"
            )
        if uncut:
            note += (
                f" · озвучено до склейки посреди паузы сцен: {len(uncut)} — заход не поставлен, "
                "склейка вплотную к слову (перевести: --only audio --force)"
            )
        return note

    def _synthesize(self, ctx: StageContext, scene: Scene, voice: Voice) -> None:
        """Одна реплика: TTS → срез вшитой тишины по таймингам слов → WAV и .srt
        со сдвигом на ту же величину. Файл сервиса не хранится: субтитры и
        сборка обязаны читать одну и ту же шкалу, а два файла с разными нулями
        рано или поздно перепутают."""
        out_dir = ctx.stage_dir(OUT_DIR)
        tts = ctx.providers.tts
        raw = out_dir / f"{scene.id}.raw{tts.extension}"
        path = out_dir / f"{scene.id}{AUDIO_EXT}"
        rate, pitch, volume = prosody(voice, scene)
        ctx.log.info("сцена %s: озвучка (%s %s %s %s)", scene.id, voice.voice, rate, pitch, volume)
        # словарь произношения проекта: голосу — удобное написание, а слова
        # .srt ниже возвращаются к написанию сценария (`core/pronunciation.py`)
        said = pronunciation.load(ctx.project_root)
        text = pronunciation.spoken(spoken_text(scene.narration), said)
        ctx.afford(len(text) * tts.price_per_char, f"озвучка {scene.id}")
        gen = tts.synthesize(
            text,
            voice=voice.voice,
            rate=rate,
            pitch=pitch,
            volume=volume,
            out_path=raw,
        )
        ctx.add_cost(gen.cost_usd, tts.name, scene.id, "tts")
        scene.cost_usd["audio"] = round(scene.cost_usd.get("audio", 0.0) + gen.cost_usd, 6)
        raw_sec = probe_duration(raw)
        srt_name = gen.meta.get("srt")
        raw_srt = out_dir / srt_name if srt_name else None
        words = (
            parse_srt_words(raw_srt.read_text(encoding="utf-8"))
            if raw_srt is not None and raw_srt.exists()
            else []
        )
        if back := pronunciation.restore_map(said):
            words = [
                Word(pronunciation.restore(w.text, back), w.start, w.end, w.turn) for w in words
            ]
        start, end = trim_window(words, raw_sec)
        self._store(ctx, scene, raw, words, start=start, end=end, raw_sec=raw_sec)
        for leftover in (raw, raw_srt):
            if leftover is not None and leftover != path:
                leftover.unlink(missing_ok=True)
        scene.measured_sec.pop(JOINED_GAP_KEY, None)  # сцена озвучена сама по себе
        ctx.save()

    def _store(
        self,
        ctx: StageContext,
        scene: Scene,
        raw: Path,
        words: list[Word],
        *,
        start: float,
        end: float,
        raw_sec: float,
    ) -> None:
        """Окно [start, end] файла озвучки → WAV сцены и её .srt со сдвигом на
        start; записи сцены о том, чем и из чего озвучена."""
        out_dir = ctx.stage_dir(OUT_DIR)
        tts = ctx.providers.tts
        path = out_dir / f"{scene.id}{AUDIO_EXT}"
        seconds = trim_audio(raw, path, start=start, end=end)
        captions = out_dir / f"{scene.id}.srt"
        if words:
            captions.write_text(words_to_srt(shift_words(words, -start)), encoding="utf-8")
        else:
            captions.unlink(missing_ok=True)
        # файл прежней озвучки с другим расширением (mp3 до среза) — не оставлять
        # рядом: по нему не понять, какой из двух звучит в ролике
        old = scene.assets.audio
        if old and ctx.abs(old).resolve() != path.resolve():
            ctx.abs(old).unlink(missing_ok=True)
        scene.assets.audio = ctx.rel(path)
        scene.assets.captions = ctx.rel(captions) if words else None
        scene.provenance["audio"] = tts.name
        scene.inputs_fingerprint["audio"] = self._inputs(ctx, scene)
        scene.measured_sec["audio"] = round(seconds, 3)
        scene.measured_sec[RAW_KEY] = round(raw_sec, 3)
        # метка «озвучено по правилу склейки посреди паузы»: сам заход ставит
        # расчёт длительностей, когда известны соседи (`_apply_timing`)
        scene.measured_sec[LEAD_IN_KEY] = 0.0
        scene.checks.audio = "pass" if seconds > 0 else "fail"
        scene.attempts["audio"] = scene.attempts.get("audio", 0) + 1

    def _synthesize_phrase(
        self,
        ctx: StageContext,
        phrase: list[Scene],
        voice: Voice,
        context: tuple[str, str],
    ) -> None:
        """Фраза одним вызовом → звук каждой сцены вырезан из общей записи по
        словам. Склейка между сценами — посреди тишины, которую поставил сам
        голос; эта тишина (`JOINED_GAP_KEY`) и идёт в расчёт стыков.

        Слова раскладываются по сценам счётом: сколько слов в реплике, столько
        и забирает сцена. Не сошёлся счёт (провайдер разбил слово иначе) — фраза
        озвучивается по сценам, как раньше: неверно разрезанная речь хуже
        рваной интонации."""
        if len(phrase) == 1:
            self._synthesize(ctx, phrase[0], voice)
            return
        out_dir = ctx.stage_dir(OUT_DIR)
        tts = ctx.providers.tts
        said = pronunciation.load(ctx.project_root)
        parts = [pronunciation.spoken(spoken_text(s.narration), said) for s in phrase]
        text = " ".join(parts)
        rate, pitch, volume = prosody(voice, phrase[0])
        first, last = phrase[0].id, phrase[-1].id
        ctx.log.info(
            "фраза %s…%s (%d сцен, %d знаков): озвучка", first, last, len(phrase), len(text)
        )
        ctx.afford(len(text) * tts.price_per_char, f"озвучка {first}…{last}")
        raw = out_dir / f"{first}.phrase{tts.extension}"
        extra = {"context": context} if tts.takes_context else {}
        gen = tts.synthesize(
            text, voice=voice.voice, rate=rate, pitch=pitch, volume=volume, out_path=raw, **extra
        )
        raw_sec = probe_duration(raw)
        srt_name = gen.meta.get("srt")
        raw_srt = out_dir / srt_name if srt_name else None
        words = (
            parse_srt_words(raw_srt.read_text(encoding="utf-8"))
            if raw_srt is not None and raw_srt.exists()
            else []
        )
        counts = [len(tokens(p)) for p in parts]
        chars = sum(len(p) for p in parts) or 1
        for scene, part in zip(phrase, parts, strict=True):
            share = round(gen.cost_usd * len(part) / chars, 6)
            ctx.add_cost(share, tts.name, scene.id, "tts")
            scene.cost_usd["audio"] = round(scene.cost_usd.get("audio", 0.0) + share, 6)
        if sum(counts) != len(words) or 0 in counts:
            ctx.log.warning(
                "фраза %s…%s: слов в записи %d, в репликах %d — озвучиваю по сценам",
                first,
                last,
                len(words),
                sum(counts),
            )
            for leftover in (raw, raw_srt):
                if leftover is not None:
                    leftover.unlink(missing_ok=True)
            for scene in phrase:
                self._synthesize(ctx, scene, voice)
            return
        if back := pronunciation.restore_map(said):
            words = [
                Word(pronunciation.restore(w.text, back), w.start, w.end, w.turn) for w in words
            ]
        slices: list[list[Word]] = []
        at = 0
        for n in counts:
            slices.append(words[at : at + n])
            at += n
        for k, (scene, own) in enumerate(zip(phrase, slices, strict=True)):
            prev = slices[k - 1] if k > 0 else None
            nxt = slices[k + 1] if k + 1 < len(slices) else None
            # границы куска — середины тишины между соседними словами
            low = (prev[-1].end + own[0].start) / 2 if prev else 0.0
            high = (own[-1].end + nxt[0].start) / 2 if nxt else raw_sec
            start, end = trim_window(own, raw_sec)
            start, end = round(max(start, low), 3), round(min(end, high), 3)
            self._store(ctx, scene, raw, own, start=start, end=end, raw_sec=raw_sec)
            if nxt:
                scene.measured_sec[JOINED_GAP_KEY] = round(max(0.0, nxt[0].start - own[-1].end), 3)
            else:
                scene.measured_sec.pop(JOINED_GAP_KEY, None)
        for leftover in (raw, raw_srt):
            if leftover is not None:
                leftover.unlink(missing_ok=True)
        ctx.save()

    def run(self, ctx: StageContext) -> StageResult:
        todo = self._todo(ctx)
        # озвучка, которую оставляем, помечается принятой (см. adopt_old_assets)
        keep = [
            s
            for s in ctx.storyboard.ordered_scenes()
            if s not in todo and s.narration.strip() and s.assets.audio
        ]
        adopt_old_assets(ctx, "audio", [(s, self._inputs(ctx, s)) for s in keep])
        voice = ctx.resources.voice or Voice(id="default")
        rate_note = self._rate_note(ctx, voice)
        if not todo:
            retimed = self._apply_timing(ctx)
            return StageResult(
                status="done",
                message=f"вся озвучка уже на месте{rate_note}{self._legacy_note(ctx)}",
                changed=retimed,
            )

        if voice.provider not in (ctx.providers.tts.name, "") and ctx.providers.tts.name != "mock":
            return StageResult(
                status="failed",
                message=(
                    f"голос {voice.id} — для {voice.provider}, а озвучивает "
                    f"{ctx.providers.tts.name}: VF_TTS_PROVIDER={voice.provider}"
                ),
            )
        changed = False
        for scene in todo:
            if not scene.narration.strip():
                scene.assets.audio = None
                scene.measured_sec.pop(LEAD_IN_KEY, None)  # без речи нечего и сдвигать
                scene.checks.audio = "skipped"
                ctx.save()
        voiced = [s for s in todo if s.narration.strip()]
        if voice.phrase:
            all_phrases = [p for _, p in _unique_phrases(ctx)]
            texts = [" ".join(s.narration for s in p) for p in all_phrases]
            for i, phrase in enumerate(all_phrases):
                if not any(s in voiced for s in phrase):
                    continue
                context = (
                    texts[i - 1][-CONTEXT_CHARS:] if i > 0 else "",
                    texts[i + 1][:CONTEXT_CHARS] if i + 1 < len(texts) else "",
                )
                self._synthesize_phrase(ctx, phrase, voice, context)
                changed = True
        else:
            for scene in voiced:
                self._synthesize(ctx, scene, voice)
                changed = True
        changed = self._apply_timing(ctx) or changed
        return StageResult(
            status="done",
            message=f"готово сцен: {len(todo)}{rate_note}{self._legacy_note(ctx)}",
            changed=changed,
        )


# Фразы считаются из раскадровки и сценария при каждом обращении отпечатка —
# а оно идёт на каждую сцену. Кэш по содержимому: те же реплики в том же
# порядке — те же фразы
_PHRASES: dict[tuple[str, tuple[tuple[str, str], ...]], list[tuple[int, list[Scene]]]] = {}


def _unique_phrases(ctx: StageContext) -> list[tuple[int, list[Scene]]]:
    scenes = ctx.storyboard.ordered_scenes()
    key = (str(ctx.project_dir), tuple((s.id, s.narration) for s in scenes))
    cached = _PHRASES.get(key)
    if cached is not None:
        by_id = {s.id: s for s in scenes}
        return [(i, [by_id[s.id] for s in p]) for i, p in cached]
    found = list(enumerate(phrases(scenes, AudioStage._marks(ctx, scenes))))
    _PHRASES.clear()  # один эпизод за раз: держать старые незачем
    _PHRASES[key] = found
    return found


def _phrase_map(ctx: StageContext) -> dict[str, tuple[int, list[Scene]]]:
    """Сцена → (номер фразы, её сцены)."""
    return {s.id: (i, p) for i, p in _unique_phrases(ctx) for s in p}
