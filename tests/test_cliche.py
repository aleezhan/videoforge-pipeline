"""Нейроязык в сценарии ловит python: строка, маркер, правило, порог плотности."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoforge.core.manifest import load_manifest
from videoforge.core.project import SCRIPT_DIR, ProjectSpec, create_episode, create_project
from videoforge.core.runner import run_episode
from videoforge.core.script import CRITIQUE_FILE, ROUNDS_DIR
from videoforge.core.stage import RunOptions
from videoforge.providers.base import ProviderSet
from videoforge.stages.cliche import (
    CLICHE_BLOCK_MIN_HITS,
    CLICHE_BLOCK_PER_1000_WORDS,
    FINDING_KIND,
    MARKERS,
    cliche_findings,
    find_cliches,
    lint_script,
)

# Два абзаца одобренного человеком сценария doc_channel/ep001 (14.09.2026):
# без единого маркера — чистый текст не должен давать ложных срабатываний.
APPROVED_CLEAN = (
    "## g2 · Тысячи лет поклонения · 55s\n\n"
    "**Текст:** Он не был ангелом. Об этом сказано прямо: «Он был из джиннов и ослушался "
    "повеления своего Господа» — сура восемнадцать, аят пятидесятый. Джинны созданы из огня, "
    "у них есть выбор, они могут подчиниться и могут отказаться. Ангелы созданы из света, "
    "у них нет пола и нет своеволия: они делают только то, что им велено. Тогда откуда он "
    "взялся среди ангелов? Его подняло туда поклонение. Ибн Каси́р и ат-Табари́ передают — "
    "и это предание учёные передают со слов ранних толкователей, а не как хадис от Пророка, "
    "салляллаху алейхи ва саллям, — что он поклонялся так долго и так усердно, что был "
    "приближён и оказался в собрании ангелов. Не по природе. По делам. Остановитесь на "
    "секунду и посчитайте: он поклонялся Алла́ху дольше, чем вы живёте. Дольше, чем живёт "
    "ваш род. И это ещё не самое неприятное в этой истории.\n\n"
    "**Кадр:** Огромный зал из света, уходящий вверх без потолка.\n\n"
    "## g5 · Пылинка · 55s\n\n"
    "**Текст:** Теперь самое неприятное. Если бы это работало на счёт дел, он был бы спасён: "
    "у него их было больше, чем у любого из нас. Но в Сахи́х Му́слим, хадис девяносто первый, "
    "достоверный, сказано: не войдёт в Рай тот, в чьём сердце есть надменность весом с "
    "пылинку. Послушайте, как она звучит на нашем языке. «Я лучше знаю». «Я не как они». "
    "«Мой намаз лучше, чем у него». «Слава Алла́ху, я не такой». Ни одна из этих фраз не "
    "звучит как богохульство. Ни за одну из них вас не поправят в мечети. В этом и "
    "опасность: за грех, которого вы за собой не замечаете, вы не станете просить прощения. "
    "А годы благих дел на другой чаше весят легче этой пылинки.\n"
)

# Ещё два абзаца того же одобренного сценария: по одному «не X, а Y» в каждом —
# уместное противопоставление, которое человек оставил. Находка есть, блока нет.
APPROVED_WITH_CONTRAST = (
    "## g4 · Три слова · 65s\n\n"
    "**Текст:** Сначала — что он НЕ сказал. Он не отрицал Алла́ха: он обращается к Нему "
    "напрямую, на «Ты». Он не отрицал приказ: он его прекрасно понял. Он не сказал «я не "
    "буду» и не назвал причины: устал, не могу, не готов. Он сказал два слова про себя и "
    "одно про другого — и вот они, дословно: «Я лучше него. Ты создал меня из огня, а его "
    "создал из глины» — сура семь, аят двенадцатый, и сура тридцать восемь, аят семьдесят "
    "шестой. Разбираем по частям. «Ты создал меня из огня» — это правда, это факт, здесь "
    "лжи нет. «Ты создал его из глины» — тоже правда. Ложь ровно в двух словах: «я лучше». "
    "Он взял два верных факта и сам вынес приговор, чья природа выше, — то есть поставил "
    "своё суждение выше приказа Того, Кто их обе и создал. Вот это и есть кибр. "
    "Надменность. Не отрицание Алла́ха, а сравнение себя с другим.\n\n"
    "## g6 · Одна разница · 40s\n\n"
    "**Текст:** Но разница между ним и Ада́мом, мир ему, была не в делах. Ада́м, мир ему, "
    "тоже ослушался. Только он сказал: «Господь наш, мы поступили несправедливо к себе» — "
    "сура семь, аят двадцать третий. И был принят. А тот стал оправдываться — и был изгнан. "
    "Не грех их разделил, а ответ на грех. Сегодня вечером сделайте одно: поймайте себя на "
    "словах «я лучше» — и попросите прощения прямо там. Вы ещё дышите — значит, дверь "
    "открыта. Напишите в комментариях, какую из четырёх фраз вы узнали за собой. И "
    "поделитесь роликом с тем, за кого переживаете. Да примет Алла́х наше покаяние и наши "
    "дела.\n"
)


def names(hits: list) -> list[str]:  # type: ignore[type-arg]
    return [h.name for h in hits]


def test_ru_markers_name_line_fragment_and_rule() -> None:
    text = (
        "Компас в горах врёт.\n"
        "Важно понимать: это не просто прибор, а привычка верить стрелке.\n"
        "В наше время так думают все. Во-первых, железо рядом.\n"
    )
    hits = find_cliches(text, "ru")
    assert [(h.line, h.rule) for h in hits] == [(2, 9), (2, 2), (3, 10), (3, 7)]
    assert hits[0].fragment == "Важно понимать"
    # «не просто X, а Y» — своя находка, а не частный случай «не X, а Y»
    assert hits[1].name == "«не просто X, а Y»"
    assert hits[1].fragment == "не просто прибор, а привычка"
    assert all(h.block == "" for h in hits)


def test_not_x_but_y_is_caught_only_with_the_comma_shape() -> None:
    assert names(find_cliches("Дело не в делах, а в ответе на грех.", "ru")) == ["«не X, а Y»"]
    # «а не» после запятой — обычный порядок слов, не шаблон
    assert not find_cliches("Карту читают по солнцу, а не по стрелке.", "ru")
    # «не просто так» — идиома, не противопоставление
    assert not find_cliches("Он пришёл сюда не просто так.", "ru")


def test_this_is_not_x_this_is_y_survives_a_sentence_break() -> None:
    text = "Это было не помрачение. Это был выбор в полном сознании."
    assert names(find_cliches(text, "ru")) == ["«это не X, это Y»"]
    dash = "И это не мелкая неточность — это ошибка в самом главном."
    assert names(find_cliches(dash, "ru")) == ["«это не X, это Y»"]


def test_counter_inside_one_line_is_one_hit_but_a_real_list_is_not() -> None:
    inline = "Первое: он не отрицал. Второе: он понял приказ. Третье, и вот здесь всё."
    hits = find_cliches(inline, "ru")
    assert [h.rule for h in hits] == [7]
    # пункт в абзаце — форма ролика: одно «Первое:» без пары штампом не считается
    assert not find_cliches("Первое: у ангелов нет своеволия, и это сказано прямо.", "ru")


def test_en_markers() -> None:
    text = (
        "In today's fast-paced world we delve into the rich and vibrant tapestry of maps.\n"
        "It's not just a tool, it's a game-changer. Firstly, iron. It's important to note this.\n"
    )
    hits = find_cliches(text, "en")
    assert [(h.line, h.rule) for h in hits] == [
        (1, 1),
        (1, 11),
        (1, 11),
        (1, 11),
        (2, 2),
        (2, 11),
        (2, 7),
        (2, 9),
    ]
    assert hits[1].fragment == "delve"
    assert hits[2].name == "paired epithets"
    assert hits[4].name == "it's not X, it's Y"
    assert hits[5].fragment == "game-changer"


def test_choppy_run_is_one_hit_and_long_anaphora_is_a_device() -> None:
    choppy = "Весь зал склонился. Он не забыл. Он не испугался. Он не перепутал."
    hits = find_cliches(choppy, "ru")
    # рубленый ряд и одно начало на том же месте — одна находка, не две
    assert names(hits) == ["рубленые предложения подряд"]
    assert hits[0].rule == 3

    anaphora = "We stored the food. We dumped the water. We lived on hope alone."
    assert names(find_cliches(anaphora, "en")) == ["одно начало у предложений подряд"]

    # два коротких подряд — ударная пара, не дробь
    assert not find_cliches("Не по природе. По делам. Остановитесь на секунду и посчитайте.", "ru")
    # длинная анафора — приём одобренного сценария, а не почерк модели
    long = (
        "Он не отрицал Алла́ха: он обращается к Нему напрямую, на «Ты». "
        "Он не отрицал приказ: он его прекрасно понял и не оспаривал. "
        "Он не сказал «я не буду» и не назвал причины: устал, не могу, не готов."
    )
    assert not find_cliches(long, "ru")


def test_unknown_language_checks_only_rhythm() -> None:
    assert "kk" not in MARKERS
    text = "Важно понимать: не просто X, а Y. Раз. Два. Три."
    hits = find_cliches(text, "kk")
    assert names(hits) == ["рубленые предложения подряд"]
    report = lint_script("## g1 · Блок · 5s\n\n**Текст:** " + text + "\n", "kk-KZ")
    assert "не заведён" in report.summary()


def test_density_threshold_and_minimum_hits() -> None:
    cliche = "Важно понимать: в наше время компас врёт."  # 7 слов, два попадания
    filler = " ".join(f"слово{n}" for n in range(60))
    dense = f"## g1 · Блок · 30s\n\n**Текст:** {cliche} {filler}\n"
    report = lint_script(dense, "ru")
    assert report.words == 67 and len(report.hits) == 2
    assert report.per_1000 >= CLICHE_BLOCK_PER_1000_WORDS
    assert report.blocking
    assert all(f.blocking for f in report.findings())

    diluted = f"## g1 · Блок · 300s\n\n**Текст:** {cliche} {' '.join([filler] * 5)}\n"
    report = lint_script(diluted, "ru")
    assert len(report.hits) == 2 and not report.blocking
    assert report.findings() and not any(f.blocking for f in report.findings())

    # одно попадание — фраза, а не почерк: даже в коротком тексте не блок
    single = "## g1 · Блок · 5s\n\n**Текст:** Важно понимать: стрелка верит железу.\n"
    report = lint_script(single, "ru")
    assert len(report.hits) == 1 < CLICHE_BLOCK_MIN_HITS
    assert report.per_1000 > CLICHE_BLOCK_PER_1000_WORDS and not report.blocking


def test_script_lint_reads_only_the_narration_layer() -> None:
    md = (
        "## g1 · Блок · 8s\n\n"
        "**Текст:** Стрелка верит железу.\n\n"
        "Важно понимать: в наше время так думают все.\n\n"
        "**Кадр:** в современном мире компас лежит на данной скале\n"
        "**Эмоция:** важно отметить холодок узнавания\n\n"
        "## g2 · Второй · 6s\n\n"
        "**Текст:** Это не магия, это физика.\n"
    )
    report = lint_script(md, "ru")
    assert [(h.block, h.line, h.name) for h in report.hits] == [
        ("g1", 5, "пустая экспертная фраза"),
        ("g1", 5, "гиперобобщение"),
        ("g2", 12, "«это не X, это Y»"),
    ]
    findings = cliche_findings(md, "ru")
    assert [f.where for f in findings] == ["g1, строка 5", "g1, строка 5", "g2, строка 12"]
    assert all(f.kind == FINDING_KIND and f.fix for f in findings)
    assert "Важно понимать" in findings[0].why_viewer_leaves
    assert "правило 9" in findings[0].why_viewer_leaves


def test_approved_script_stays_below_the_block() -> None:
    """Два абзаца, которые человек одобрил, — ноль ложных срабатываний."""
    clean = lint_script(APPROVED_CLEAN, "ru")
    assert clean.words > 200
    assert not clean.hits, names(clean.hits)
    assert clean.summary() == "нейроязык: 0 попаданий"

    # два уместных «не X, а Y» на 250 слов — предупреждение, не блок
    contrast = lint_script(APPROVED_WITH_CONTRAST, "ru")
    assert names(contrast.hits) == ["«не X, а Y»", "«не X, а Y»"]
    assert contrast.per_1000 < CLICHE_BLOCK_PER_1000_WORDS
    assert not contrast.blocking
    assert contrast.summary().startswith("нейроязык: 2 попадания (")
    assert "правила: 2" in contrast.summary()


# Черновик заглушки, набитый штампами: у каждого блока слов не меньше нормы
# речи, чтобы находки были только про нейроязык
CLICHE_DRAFT = (
    "## g1 · Компас · 4s\n\n"
    "**Текст:** Важно понимать: в наше время компас врёт не просто иногда, а постоянно.\n"
    "**Кадр:** compass\n**Эмоция:** тревога\n\n"
    "## g2 · Железо · 8s\n\n"
    "**Текст:** Это не магия, это физика. Во-первых, железо рядом. Во-вторых, руда под "
    "ногами. И вот что важно: стрелка верит железу, а не северу.\n"
    "**Кадр:** ore\n**Эмоция:** узнавание\n\n"
    "## g3 · Скала · 12s\n\n"
    "**Текст:** Таким образом, в горах компас показывает на ближайшую скалу с рудой. "
    "Данный подход к навигации подводит каждого туриста, поэтому карту читают по солнцу "
    "и по звёздам, и только потом сверяются со стрелкой.\n"
    "**Кадр:** rock\n**Эмоция:** облегчение\n"
)


@pytest.fixture
def topic_episode(projects_root: Path) -> Path:
    create_project(
        ProjectSpec(
            id="d",
            name="Сценарист",
            platforms=["youtube_shorts"],
            character="timur",
            style="pixar3d",
            voice="ru_male",
            target_duration_sec=30,
        )
    )
    return create_episode("d", "e1", title="проба", brief="почему компасы врут в горах")


def test_director_feeds_cliches_into_the_critique(
    topic_episode: Path, mock_providers: ProviderSet
) -> None:
    """Находки нейроязыка идут в цикл черновик ↔ критик вместе с находками
    критика: модель получает их в повтор с причинами, они лежат в critique.json,
    и стадия называет счёт в сообщении."""
    draft_prompts: list[str] = []

    class Clichéd(type(mock_providers.llm)):  # type: ignore[misc]
        def complete(self, prompt: str, **kw: object) -> object:
            result = super().complete(prompt, **kw)  # type: ignore[arg-type]
            if prompt.startswith("VIDEOFORGE-STEP: draft"):
                draft_prompts.append(prompt)
                result.text = CLICHE_DRAFT
            return result

    mock_providers.llm = Clichéd()
    run_episode(
        topic_episode,
        mock_providers,
        RunOptions(auto_approve=True, raise_errors=True, only="director", max_attempts=2),
    )
    out = topic_episode / SCRIPT_DIR
    critique = json.loads((out / CRITIQUE_FILE).read_text(encoding="utf-8"))
    ours = [f for f in critique["findings"] if f["kind"] == FINDING_KIND]
    assert len(ours) >= 6 and all(f["blocking"] for f in ours)
    assert {f["where"].split(",")[0] for f in ours} == {"g1", "g2", "g3"}
    assert not [f for f in critique["findings"] if "с речи при заявленных" in f["fix"]]

    # заглушка штампы не убирает — цикл дошёл до потолка, и второй черновик
    # просили с причинами первого
    assert len(sorted((out / ROUNDS_DIR).glob("round_*.md"))) == 2
    assert len(draft_prompts) == 2
    assert FINDING_KIND in draft_prompts[1] and "Важно понимать" in draft_prompts[1]
    assert FINDING_KIND not in draft_prompts[0]

    message = load_manifest(topic_episode).stages["director"].message
    assert "нейроязык:" in message and "/1000 слов" in message


def test_clean_mock_draft_adds_no_cliche_findings(
    topic_episode: Path, mock_providers: ProviderSet
) -> None:
    run_episode(
        topic_episode,
        mock_providers,
        RunOptions(auto_approve=True, raise_errors=True, only="director"),
    )
    critique = json.loads((topic_episode / SCRIPT_DIR / CRITIQUE_FILE).read_text("utf-8"))
    assert not [f for f in critique["findings"] if f["kind"] == FINDING_KIND]
    assert "нейроязык: 0 попаданий" in load_manifest(topic_episode).stages["director"].message
