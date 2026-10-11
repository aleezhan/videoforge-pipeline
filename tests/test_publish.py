"""Стадия publish: упаковка ролика и проверки под площадку.

Загрузки нет и не проверяется — проверяется то, что человек иначе считает
руками в момент, когда уже нажимает «загрузить»: влезает ли хронометраж, тот ли
формат кадра, не длинный ли заголовок.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import FINAL_SEC, make_test_video

from videoforge.clips.source import SourceInfo, save_source
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import (
    PublishInfo,
    Scene,
    SceneOverlay,
    SourceSegment,
    load_storyboard,
    save_storyboard,
)
from videoforge.providers.base import ProviderSet, PublishMeta
from videoforge.stages.publish import (
    CHAPTERS_MIN_SEC,
    DATA_FILE,
    DESCRIPTION_MAX,
    OUT_DIR,
    PAGE_FILE,
    TITLE_MAX,
    Chapter,
    PublishPlan,
    chapter_lines,
    chapters,
    describe,
    platform_checks,
    render_page,
)

OPTS = RunOptions(only="publish", raise_errors=True)


def _with_final(
    episode: Path, final: Path | None = None, *, seconds: float = 30.0, size: str = "1080x1920"
) -> Path:
    """Эпизоду подкладывается готовый файл: publish собирается после сборки, но
    для проверки самой упаковки гонять всю сборку незачем.

    С `final` (общий ролик из conftest, 30 с 1080×1920) файл копируется —
    миллисекунды вместо ~6 с рендера; `seconds`/`size` — для честного рендера
    файла другой длины или формата, когда `final` не передан."""
    out = episode / "05_assemble"
    out.mkdir(exist_ok=True)
    if final is not None:
        shutil.copy2(final, out / "final.mp4")
        seconds = FINAL_SEC
    else:
        make_test_video(out / "final.mp4", seconds=seconds, size=size)
    sb = load_storyboard(episode)
    sb.final.video = "05_assemble/final.mp4"
    sb.final.duration_sec = seconds
    save_storyboard(episode, sb)
    return episode


def test_platform_checks_count_what_the_platform_requires() -> None:
    info = PublishInfo(title="Заголовок", description="Описание.", hashtags=["а", "б"])
    rows = {c.label: c for c in platform_checks(
        info, platforms=["youtube_shorts"], duration_sec=45.0, size=(1080, 1920)
    )}  # fmt: skip
    assert all(c.ok for c in rows.values()), [c.label for c in rows.values() if not c.ok]

    long = PublishInfo(title="x" * (TITLE_MAX + 1), description="", hashtags=[])
    bad = {c.label: c for c in platform_checks(
        long, platforms=["youtube_shorts"], duration_sec=400.0, size=(1920, 1080)
    )}  # fmt: skip
    assert not bad["заголовок"].ok and not bad["хэштеги"].ok
    assert bad["описание"].ok  # пустое описание площадка принимает
    assert not bad["YouTube Shorts: хронометраж"].ok  # 400 с против лимита 180
    assert not bad["YouTube Shorts: формат кадра"].ok  # горизонталь вместо вертикали

    # «сладкая» длина — ориентир, а не запрет: она отдельной строкой
    short = platform_checks(info, platforms=["youtube_shorts"], duration_sec=8.0, size=(1080, 1920))
    by = {c.label: c for c in short}
    assert by["YouTube Shorts: хронометраж"].ok and not by["YouTube Shorts: удачная длина"].ok


def test_page_is_ready_to_paste() -> None:
    info = PublishInfo(title="Заголовок", description="Описание.", hashtags=["хабиб", "мма"])
    plan_checks = platform_checks(
        info, platforms=["youtube_shorts"], duration_sec=40.0, size=(1080, 1920)
    )
    from videoforge.stages.publish import PublishPlan

    page = render_page(
        PublishPlan(
            video="05_assemble/final.mp4",
            duration_sec=40.0,
            platforms=["youtube_shorts"],
            title=info.title,
            description=info.description,
            hashtags=info.hashtags,
            checks=plan_checks,
        )
    )
    assert "Заголовок" in page and "#хабиб #мма" in page and "Описание." in page
    assert "05_assemble/final.mp4" in page and "Загрузку стадия не делает" in page


@pytest.mark.render
def test_publish_uses_the_packaging_that_came_with_the_moment(
    clip_episode: Path, mock_providers: ProviderSet, final: Path
) -> None:
    """У нарезки упаковку написал критик моментов — переписывать её незачем."""
    _with_final(clip_episode, final)
    sb = load_storyboard(clip_episode)
    sb.publish = PublishInfo(title="Свой заголовок", description="Своё.", hashtags=["тег"])
    save_storyboard(clip_episode, sb)

    llm = mock_providers.llm
    before = getattr(llm, "calls", 0)
    report = run_episode(clip_episode, mock_providers, OPTS)
    assert report.ok, report.reason

    page = (clip_episode / OUT_DIR / PAGE_FILE).read_text(encoding="utf-8")
    assert "Свой заголовок" in page and "#тег" in page
    assert (clip_episode / OUT_DIR / DATA_FILE).exists()
    assert getattr(llm, "calls", 0) == before  # модель не звали: упаковка своя
    assert "Свой заголовок" in report.executed[0][1].message


def test_publish_asks_the_model_when_there_is_no_packaging(
    episode: Path, mock_providers: ProviderSet, final: Path
) -> None:
    """У эпизода ai_video упаковки нет — стадия просит её один раз и запоминает
    в раскадровке: со второго прогона модель уже не нужна."""
    _with_final(episode, final)
    assert not load_storyboard(episode).publish.title

    assert run_episode(episode, mock_providers, OPTS).ok
    sb = load_storyboard(episode)
    assert sb.publish.title == "Мок-заголовок ролика"
    assert sb.publish.hashtags == ["Мок", "шортс"]  # решётку снимаем сами
    page = (episode / OUT_DIR / PAGE_FILE).read_text(encoding="utf-8")
    assert "Мок-заголовок ролика" in page


@pytest.mark.render
def test_publish_says_what_is_off_instead_of_failing(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Ролик длиннее лимита площадки — это замечание человеку, а не отказ:
    выкладывать или резать, решает он."""
    _with_final(episode, seconds=200.0, size="1920x1080")
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok
    message = report.executed[0][1].message
    assert "ПРОВЕРЬ" in message and "хронометраж" in message


@pytest.mark.render
def test_music_credit_reaches_the_upload_page(
    episode: Path, projects_root: Path, mock_providers: ProviderSet
) -> None:
    """Трек под лицензией CC BY обязан быть упомянут в описании ролика.

    Лицензия, о которой знает только YAML, — это нарушение, о котором человек
    узнаёт от площадки. Кредит берётся у того трека, который реально попал в
    файл (`final.music_track`), а не у того, что записан в проекте: план
    монтажа мог его сменить."""
    from conftest import add_music

    add_music(episode, projects_root)
    mdir = projects_root / "t" / "resources" / "music"
    (mdir / "tone.yaml").write_text(
        'id: tone\nfile: tone.wav\nlicense: "CC BY 4.0"\nsource: "тест"\n'
        'attribution: "Музыка: «Tone» — Тестовый Автор, CC BY 4.0"\n',
        encoding="utf-8",
    )
    run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    page = (episode / "06_publish" / "publish.md").read_text(encoding="utf-8")
    assert "Тестовый Автор" in page and "лицензия музыки" in page


# ------------------------------------------------------------------ главы


def _scene(i: int, plate: tuple[str, str] | None = None) -> Scene:
    """Сцена с плашкой (`("label", "ПУНКТ 1")`) или без."""
    overlay = SceneOverlay(text=plate[1], kind=plate[0]) if plate else None  # type: ignore[arg-type]
    return Scene(id=f"s{i:02d}", order=i, overlay=overlay)


def _timeline(scenes: list[Scene], seconds: list[float] | float = 20.0) -> list[dict]:
    """Строки timeline.json: сцены подряд, каждая своей длины."""
    lengths = seconds if isinstance(seconds, list) else [seconds] * len(scenes)
    rows, at = [], 0.0
    for scene, sec in zip(scenes, lengths, strict=True):
        rows.append({"scene_id": scene.id, "start": round(at, 3), "end": round(at + sec, 3)})
        at += sec
    return rows


def _labeled() -> list[Scene]:
    """Десять сцен по 20 с: три ярлыка, у двух первых есть вывод, у третьего —
    только ссылка на источник."""
    return [
        _scene(1, ("label", "ПУНКТ 1")),
        _scene(2, ("caption", "Вода была раньше земли.")),
        _scene(3),
        _scene(4, ("label", "ПУНКТ 2")),
        _scene(5, ("caption", "Джинны жили первыми")),
        _scene(6),
        _scene(7, ("label", "ПУНКТ 3")),
        _scene(8),
        _scene(9, ("source", "Коран 55:15")),
        _scene(10),
    ]


def test_chapters_open_on_labels_and_take_their_names_from_captions() -> None:
    """Ярлык говорит «здесь начинается», вывод — «о чём»; без вывода остаётся
    текст ярлыка; точка реплики в заголовке главы не нужна; ссылка на источник
    названием не становится."""
    scenes = _labeled()
    found = chapters(scenes, _timeline(scenes), aspect_ratio="16:9")
    assert [(c.start_sec, c.title) for c in found] == [
        (0.0, "Вода была раньше земли"),
        (60.0, "Джинны жили первыми"),
        (120.0, "ПУНКТ 3"),
    ]


def test_intro_before_the_first_label_is_named_by_its_caption_or_there_are_no_chapters() -> None:
    """Первая отметка — всегда 00:00: сцены до первого ярлыка образуют
    вступление. Вывод внутри него даёт название; нет вывода — глав нет вовсе:
    «Вступление» или «Сцена 1» хуже, чем главы от площадки."""
    scenes = [
        _scene(1, ("caption", "Кто жил на Земле до Адама")),
        _scene(2),
        *_labeled()[3:],  # ярлыки 2 и 3 плюс хвост — итого три главы
    ]
    found = chapters(scenes, _timeline(scenes), aspect_ratio="16:9")
    assert found[0] == Chapter(start_sec=0.0, title="Кто жил на Земле до Адама")
    assert len(found) == 3

    mute = [_scene(1), _scene(2), *_labeled()[3:]]
    assert chapters(mute, _timeline(mute), aspect_ratio="16:9") == []


def test_short_chapter_merges_into_the_next_and_a_short_tail_into_the_previous() -> None:
    """Площадка не принимает главу короче 10 с: слитая глава начинается там,
    где начиналась короткая, и берёт название по общему правилу — вывод, если он
    есть где-то внутри, иначе первый ярлык."""
    scenes = [
        _scene(1, ("label", "ПУНКТ 1")),
        _scene(2, ("label", "ПУНКТ 2")),
        _scene(3, ("label", "ПУНКТ 3")),
        _scene(4, ("label", "ПУНКТ 4")),
        _scene(5, ("label", "ПУНКТ 5")),
    ]
    rows = _timeline(scenes, [5.0, 100.0, 50.0, 50.0, 4.0])
    found = chapters(scenes, rows, aspect_ratio="16:9")
    assert [(c.start_sec, c.title) for c in found] == [
        (0.0, "ПУНКТ 1"),
        (105.0, "ПУНКТ 3"),
        (155.0, "ПУНКТ 4"),
    ]


def test_no_chapters_when_fewer_than_three_or_the_video_is_a_short() -> None:
    scenes = _labeled()
    rows = _timeline(scenes)
    two = [s if s.id != "s07" else _scene(7) for s in scenes]
    assert chapters(two, _timeline(two), aspect_ratio="16:9") == []  # две главы
    assert chapters(scenes, rows, aspect_ratio="9:16") == []  # вертикаль — шортс
    brief = _timeline(scenes, CHAPTERS_MIN_SEC / len(scenes) - 1)  # короче трёх минут
    assert chapters(scenes, brief, aspect_ratio="16:9") == []
    assert chapters(scenes, [], aspect_ratio="16:9") == []  # границы не измерены
    assert chapters([_scene(i) for i in range(1, 11)], rows, aspect_ratio="16:9") == []


def test_chapter_block_goes_to_the_end_of_the_description_and_to_the_page() -> None:
    found = [
        Chapter(start_sec=0.0, title="Начало"),
        Chapter(start_sec=65.4, title="Середина"),
        Chapter(start_sec=3599.9, title="Конец"),
    ]
    text = describe("Что происходит.", found, duration_sec=3700.0)
    assert text == "Что происходит.\n\n0:00:00 Начало\n0:01:05 Середина\n0:59:59 Конец"
    assert chapter_lines(found[:2], duration_sec=500.0) == "00:00 Начало\n01:05 Середина"
    # таймкоды, вписанные человеком, побеждают: второй список площадка прочла
    # бы как ещё одни главы
    mine = "Своё.\n\n00:00 Моя первая\n01:00 Моя вторая\n02:00 Моя третья"
    assert describe(mine, found, duration_sec=500.0) == mine
    assert describe("Пусто.", [], duration_sec=500.0) == "Пусто."

    page = render_page(
        PublishPlan(video="f.mp4", duration_sec=500.0, title="Т", description=text, chapters=found)
    )
    assert "## Главы" in page and "| 01:05 | Середина |" in page


def test_clip_description_has_no_link_to_the_full_episode(
    episode: Path, mock_providers: ProviderSet, final: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ссылки «Full episode: …» в описании нарезки нет: с 15.09.2026 стадия её
    дописывала, 18.09.2026 владелец убрал — так никто из нарезчиков не делает.
    Описание уходит тем текстом, что написал критик моментов."""
    monkeypatch.setattr("videoforge.stages.publish._thumbnail", lambda *a, **k: None)
    url = "https://www.youtube.com/watch?v=ABC123"
    save_source(
        episode.parent.parent / "sources" / "ABC123",
        SourceInfo(id="ABC123", url=url, title="Выпуск", channel="Smol Talk"),
    )
    _with_final(episode, final)
    sb = load_storyboard(episode)
    sb.scenes[0].source = SourceSegment(path="sources/ABC123", start_sec=2114.24, end_sec=2174.2)
    sb.publish = PublishInfo(title="Т", description="Что происходит.", hashtags=["тег"])
    save_storyboard(episode, sb)

    assert run_episode(episode, mock_providers, OPTS).ok
    plan = PublishPlan.model_validate_json((episode / OUT_DIR / DATA_FILE).read_text("utf-8"))
    assert plan.description == "Что происходит."


def test_hashtag_already_in_the_description_is_not_added_twice() -> None:
    """Модель вписывает хэштеги и в текст описания; загрузка дописывает список
    в конец — и один из первых роликов уехал с #cartoon дважды."""
    meta = PublishMeta(
        platform="youtube",
        title="Т",
        description="Hero panics. #Cartoon #shorts",
        hashtags=["cartoon", "#hero", "shorts"],
    )
    assert meta.tagged_description() == "Hero panics. #Cartoon #shorts\n\n#hero"
    only_inline = PublishMeta(
        platform="youtube", title="Т", description="Текст #тег", hashtags=["тег"]
    )
    assert only_inline.tagged_description() == "Текст #тег"
    # заголовок из хэштегов (ролик без подписи автора): описание их не повторяет
    in_title = PublishMeta(
        platform="youtube", title="#cookies #shorts", hashtags=["cookies", "shorts", "cat"]
    )
    assert in_title.tagged_description() == "#cat"
    assert PublishMeta(platform="youtube", title="#a", hashtags=["a"]).tagged_description() == ""


def test_description_limit_counts_the_chapters() -> None:
    """Главы — часть описания, и лимит площадки считается с ними: площадка
    режет по 5000 молча, и обрезанный список она прочтёт как список покороче."""
    found = [Chapter(start_sec=float(i * 60), title=f"Глава {i}") for i in range(12)]
    body = "x" * (DESCRIPTION_MAX - 20)
    packed = PublishInfo(title="Т", description=describe(body, found, duration_sec=800.0))
    by = {c.label: c for c in platform_checks(packed, platforms=[], duration_sec=800.0, size=None)}
    assert not by["описание"].ok
    clean = PublishInfo(title="Т", description=body)
    ok = {c.label: c for c in platform_checks(clean, platforms=[], duration_sec=800.0, size=None)}
    assert ok["описание"].ok


def test_upload_takes_the_description_with_chapters_as_is() -> None:
    """Загрузка описание не собирает заново: блок глав уезжает в videos.insert
    ровно тем текстом, что в publish.json, хэштеги — после него."""
    from videoforge.stages.publish_upload import _meta

    packaging = PublishPlan(
        title="Т",
        description="Описание.\n\n00:00 Начало\n01:00 Середина\n02:00 Конец",
        hashtags=["тег"],
        chapters=[Chapter(start_sec=0.0, title="Начало")],
    )
    meta = _meta(
        packaging,
        network="youtube",
        language="ru",
        text_language="ru",
        account="",
        privacy="private",
        synthetic=True,
        cover=None,
    )
    assert meta.description == packaging.description
    assert meta.tagged_description().endswith("02:00 Конец\n\n#тег")


def test_publish_stage_writes_chapters_and_keeps_the_storyboard_clean(
    episode: Path, mock_providers: ProviderSet, final: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стадия кладёт главы в publish.json, в описание и в publish.md — а в
    раскадровку упаковка от модели ложится БЕЗ них: главы выводятся из таймлайна
    на каждом прогоне, записанные пережили бы пересборку и врали бы."""
    import json

    # обложка к главам отношения не имеет, а её выбор — секунды ffmpeg по
    # кадрам: без него тест остаётся в быстром наборе
    monkeypatch.setattr("videoforge.stages.publish._thumbnail", lambda *a, **k: None)
    _with_final(episode, final)
    sb = load_storyboard(episode)
    sb.aspect_ratio = "16:9"
    sb.scenes = _labeled()
    sb.final.duration_sec = 200.0
    save_storyboard(episode, sb)
    (episode / "05_assemble" / "timeline.json").write_text(
        json.dumps(_timeline(sb.scenes)), encoding="utf-8"
    )

    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    assert "глав в описании: 3" in report.executed[0][1].message
    plan = PublishPlan.model_validate_json(
        (episode / OUT_DIR / DATA_FILE).read_text(encoding="utf-8")
    )
    assert [c.title for c in plan.chapters] == [
        "Вода была раньше земли",
        "Джинны жили первыми",
        "ПУНКТ 3",
    ]
    assert plan.description.endswith(
        "\n\n00:00 Вода была раньше земли\n01:00 Джинны жили первыми\n02:00 ПУНКТ 3"
    )
    assert "## Главы" in (episode / OUT_DIR / PAGE_FILE).read_text(encoding="utf-8")
    assert "00:00" not in load_storyboard(episode).publish.description


def test_a_long_video_without_chapters_is_told_why() -> None:
    """Длинный горизонтальный ролик без глав получает причину для сообщения
    стадии — иначе отсутствие глав неотличимо от поломки. Шортсу причина не
    нужна: главы ему не положены, и говорить об этом на каждом шортсе незачем."""
    from videoforge.stages.publish import _chapters

    scenes = _labeled()
    rows = _timeline(scenes)
    assert _chapters(scenes, rows, aspect_ratio="16:9", duration_sec=200.0)[1] == ""
    assert _chapters(scenes, rows, aspect_ratio="9:16", duration_sec=200.0) == ([], "")
    assert _chapters(scenes, rows, aspect_ratio="16:9", duration_sec=100.0) == ([], "")
    why = _chapters(scenes, [], aspect_ratio="16:9", duration_sec=200.0)[1]
    assert "не измерены" in why
    plain = [_scene(i) for i in range(1, 11)]
    assert "label" in _chapters(plain, rows, aspect_ratio="16:9", duration_sec=200.0)[1]
    two = [s if s.id != "s07" else _scene(7) for s in scenes]
    assert "глав 2" in _chapters(two, rows, aspect_ratio="16:9", duration_sec=200.0)[1]
    mute = [_scene(1), _scene(2), *scenes[3:]]
    assert "00:00" in _chapters(mute, rows, aspect_ratio="16:9", duration_sec=200.0)[1]
