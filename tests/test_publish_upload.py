"""Загрузка на площадки: `videoforge publish` на заглушке.

Живой аккаунт здесь не трогается никогда — провайдер только `mock`. Проверяем
то, из-за чего публикация бывает больнее, чем польза от неё: что сухой прогон
действительно ничего не отправляет, что повтор команды не кладёт на канал
второй такой же ролик, и что чужая страница не может нажать кнопку за человека.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import pack, unpack
from fastapi.testclient import TestClient

from videoforge.api.app import create_app
from videoforge.core.accounts import create_account
from videoforge.core.manifest import PublishRecord, load_manifest, now, save_manifest
from videoforge.providers.base import ProviderError, ProviderSet, PublishMeta, PublishResult
from videoforge.providers.mock import MockPublish
from videoforge.providers.registry import build_publisher
from videoforge.stages.publish_upload import (
    NotReady,
    check_episode,
    load_packaging,
    publish_episode,
    sent_episodes,
    target_networks,
    update_episode,
)

# Эпизод с готовым файлом и упаковкой здесь — `unpack(packed, episode)`: копия
# следа стадии publish из шаблона conftest. До 14.09.2026 каждый тест кодировал
# 30 с 1080×1920 и гонял стадию сам — ~6 с на тест, 28 тестов, три минуты на
# файл; теперь рендер и стадия идут один раз на прогон, копия — миллисекунды.


def test_dry_run_sends_nothing_and_leaves_manifest_alone(episode: Path, packed: Path) -> None:
    unpack(packed, episode)
    pub = MockPublish()
    report = publish_episode(episode, to=["youtube"], provider=pub, log_fn=lambda _: None)

    assert report.dry_run and report.state == "dry_run"
    assert [p.state for p in report.platforms] == ["dry_run"]
    assert pub.uploads == []  # ничего не ушло
    assert load_manifest(episode).publish is None  # и в манифесте следа нет


def test_upload_writes_where_the_video_landed(episode: Path, packed: Path) -> None:
    unpack(packed, episode)
    pub = MockPublish()
    report = publish_episode(
        episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None
    )

    assert report.ok and report.state == "done"
    assert len(pub.uploads) == 1
    sent_video, sent_meta = pub.uploads[0]
    assert sent_video.name == "final.mp4"
    # пометка ИИ и приватность едут с файлом, а не «поставим потом руками»
    assert sent_meta.synthetic is True and sent_meta.privacy == "private"
    assert "#тег" in sent_meta.tagged_description()

    record = load_manifest(episode).publish
    assert record is not None and record.state == "done"
    assert record.platforms["youtube"].video_id == "mock001"
    assert record.platforms["youtube"].url.endswith("mock001")


def test_published_once_is_not_published_twice(episode: Path, packed: Path) -> None:
    """Повтор команды — самая дешёвая ошибка и самая заметная на канале."""
    unpack(packed, episode)
    pub = MockPublish()
    kw = {"to": ["youtube"], "dry_run": False, "provider": pub, "log_fn": lambda _: None}
    publish_episode(episode, **kw)  # type: ignore[arg-type]
    again = publish_episode(episode, **kw)  # type: ignore[arg-type]

    assert [p.state for p in again.platforms] == ["skipped"]
    assert len(pub.uploads) == 1  # второй раз не грузили
    # повтор не понижает состояние эпизода: ролик как лежал, так и лежит
    record = load_manifest(episode).publish
    assert record is not None and record.state == "done"
    forced = publish_episode(episode, force=True, **kw)  # type: ignore[arg-type]
    assert [p.state for p in forced.platforms] == ["done"] and len(pub.uploads) == 2


def test_a_platform_without_an_id_still_counts_as_published(episode: Path, packed: Path) -> None:
    """Форма TikTok id ролика не отдаёт. Если считать опубликованным только то,
    у чего есть id, следующий запуск выложит шортс второй раз."""
    from videoforge.providers.base import PublishResult

    unpack(packed, episode)

    class Silent(MockPublish):
        def upload(self, video_path: Path, meta: PublishMeta) -> PublishResult:
            super().upload(video_path, meta)
            return PublishResult(platform=meta.platform, state="done", video_id="", url="")

    pub = Silent()
    kw = {"to": ["tiktok"], "dry_run": False, "provider": pub, "log_fn": lambda _: None}
    publish_episode(episode, **kw)  # type: ignore[arg-type]
    again = publish_episode(episode, **kw)  # type: ignore[arg-type]
    assert [p.state for p in again.platforms] == ["skipped"] and len(pub.uploads) == 1


def test_gate_holds_publishing(episode: Path, packed: Path) -> None:
    """Эпизод, который ждёт одобрения, не публикуется: QA стоит перед публикацией."""
    unpack(packed, episode)
    m = load_manifest(episode)
    m.awaiting_approval = "qa"
    save_manifest(episode, m)
    with pytest.raises(NotReady, match="qa"):
        publish_episode(episode, to=["youtube"], dry_run=False, provider=MockPublish())


def test_a_failed_blocking_check_stops_the_upload_unless_forced(
    episode: Path, packed: Path
) -> None:
    """A blocking check that failed in the publish package stops the upload: the
    schedule slot is not wasted. `--force` is the human's override."""
    import json

    from videoforge.stages.publish import PublishCheck

    check = PublishCheck(
        label="лимит длины", value="112 с", ok=False, target="≤ 60 с", blocking=True
    )
    unpack(packed, episode)
    path = episode / "06_publish" / "publish.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"].append(check.model_dump())
    path.write_text(json.dumps(data), encoding="utf-8")
    pub = MockPublish()
    kw = {"to": ["youtube"], "dry_run": False, "provider": pub, "log_fn": lambda _: None}
    with pytest.raises(NotReady, match="лимит длины"):
        publish_episode(episode, **kw)  # type: ignore[arg-type]
    assert pub.uploads == []
    publish_episode(episode, force=True, **kw)  # type: ignore[arg-type]
    assert len(pub.uploads) == 1  # решение человека сильнее замера


def test_without_packaging_it_refuses(episode: Path) -> None:
    with pytest.raises(NotReady, match="publish"):
        publish_episode(episode, to=["youtube"], provider=MockPublish())


def test_platform_names_map_to_networks(episode: Path, mock_providers: ProviderSet) -> None:
    from videoforge.stages.publish import PublishPlan

    packaging = PublishPlan(platforms=["youtube_shorts", "tiktok"])
    assert target_networks([], packaging) == ["youtube", "tiktok"]
    assert target_networks(["youtube_shorts", "youtube"], packaging) == ["youtube"]
    with pytest.raises(NotReady, match="неизвестная площадка"):
        target_networks(["vk"], packaging)


@pytest.mark.render
def test_ai_mark_follows_the_workflow(
    clip_episode: Path, mock_providers: ProviderSet, final: Path
) -> None:
    """Нарезка живого интервью не помечается как сделанная ИИ.

    Пометка — свойство workflow: `ai_video` рисует кадры и синтезирует голос,
    `clips_from_long` режет настоящую съёмку с настоящим голосом. Пометка на
    ней говорит зрителю неправду о том, что он смотрит."""
    # нарезка — другой проект и workflow, шаблон t/e1 ей не подходит: стадия честно
    pack(clip_episode, mock_providers, final)
    pub = MockPublish()
    publish_episode(
        clip_episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None
    )

    _, meta = pub.uploads[0]
    assert meta.synthetic is False


def test_privacy_is_the_projects_decision(episode: Path, packed: Path) -> None:
    """Видимость берётся из project.yaml, а `--privacy` перебивает её на раз.

    Канал на каденсе ставит `public` один раз в файле: ручное «открыть ролик»
    каждый день — это действие, которое однажды забудут, и ролик не увидит никто."""
    from videoforge.paths import project_config_path

    unpack(packed, episode)
    # правим файл текстом, как это делает человек: project.yaml — шаблон с
    # комментариями, и записи через model_dump у него нет намеренно
    cfg_path = project_config_path("t")
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace("privacy: private", "privacy: public"),
        encoding="utf-8",
    )

    pub = MockPublish()
    kw = {"to": ["youtube"], "dry_run": False, "provider": pub, "log_fn": lambda _: None}
    publish_episode(episode, **kw)  # type: ignore[arg-type]
    assert pub.uploads[0][1].privacy == "public"

    publish_episode(episode, force=True, privacy="unlisted", **kw)  # type: ignore[arg-type]
    assert pub.uploads[1][1].privacy == "unlisted"


def _set_category(project_id: str, value: str) -> None:
    """Объявить раздел в project.yaml — текстом, как это делает человек."""
    from videoforge.paths import project_config_path

    path = project_config_path(project_id)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('category: ""', f"category: {value}"), encoding="utf-8")


def test_channel_section_is_the_projects_decision(episode: Path, packed: Path) -> None:
    """Раздел канала — полка, на которую площадка кладёт ролик. До 15.09.2026 он
    был зашит в провайдере числом «People & Blogs» для любого проекта; теперь его
    объявляет project.yaml, и загрузка везёт наше слово переведённым в id площадки."""
    from videoforge.providers.youtube_api import DEFAULT_CATEGORY

    unpack(packed, episode)
    kw = {"to": ["youtube"], "dry_run": False, "log_fn": lambda _: None}

    neutral = MockPublish()
    publish_episode(episode, provider=neutral, **kw)  # type: ignore[arg-type]
    assert neutral.uploads[0][1].category == ""  # проект молчит

    _set_category("t", "sports")
    pub = MockPublish()
    publish_episode(episode, force=True, provider=pub, **kw)  # type: ignore[arg-type]
    assert pub.uploads[-1][1].category == "17"

    # в теле запроса к YouTube объявленный раздел вытесняет нейтральный
    from videoforge.providers.youtube_api import YoutubeUpload

    body = YoutubeUpload("t_yt").request_body(pub.uploads[-1][1])
    assert body["snippet"]["categoryId"] == "17"
    silent = YoutubeUpload("t_yt").request_body(neutral.uploads[0][1])
    assert silent["snippet"]["categoryId"] == DEFAULT_CATEGORY


def test_an_unknown_section_is_refused_by_the_file(episode: Path) -> None:
    """Опечатка в разделе должна падать на чтении настроек, а не на площадке."""
    from videoforge.core.project import load_project

    _set_category("t", "спорт")
    with pytest.raises(ValueError, match="неизвестный раздел"):
        load_project("t")


# ---------------------------------------------------------------- правка уехавшего


def _published(episode: Path, packed: Path) -> MockPublish:
    """Эпизод, который уже уехал на заглушечный YouTube."""
    unpack(packed, episode)
    pub = MockPublish()
    publish_episode(episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None)
    return pub


def test_update_dry_run_shows_the_change_and_sends_nothing(episode: Path, packed: Path) -> None:
    """Правка меняет то, что уже видят люди, поэтому сухой прогон здесь такой
    же обязательный, как у загрузки: сначала «было → станет», потом --yes."""
    pub = _published(episode, packed)
    report = update_episode(
        episode, to=["youtube"], privacy="public", provider=pub, log_fn=lambda _: None
    )

    assert report.dry_run and [p.state for p in report.platforms] == ["dry_run"]
    assert any("видимость" in c for c in report.platforms[0].changes)
    assert pub.edits == []
    record = load_manifest(episode).publish
    assert record is not None and record.platforms["youtube"].privacy == "private"


def test_update_changes_the_video_without_uploading_it_again(episode: Path, packed: Path) -> None:
    """Ролик на канале остаётся тем же: та же ссылка, те же просмотры."""
    pub = _published(episode, packed)
    report = update_episode(
        episode,
        to=["youtube"],
        dry_run=False,
        privacy="public",
        provider=pub,
        log_fn=lambda _: None,
    )

    assert [p.state for p in report.platforms] == ["done"]
    video_id, meta = pub.edits[0]
    assert video_id == "mock001" and meta.privacy == "public"
    assert len(pub.uploads) == 1  # файл второй раз не уезжал
    record = load_manifest(episode).publish
    assert record is not None and record.platforms["youtube"].privacy == "public"


def test_update_the_second_time_finds_nothing_to_change(episode: Path, packed: Path) -> None:
    """Правка идемпотентна — поэтому у неё и нет защиты от повтора, как у
    загрузки: второй запуск не находит, что менять, и дубля на канале нет."""
    pub = _published(episode, packed)
    kw = {"to": ["youtube"], "dry_run": False, "privacy": "public", "provider": pub}
    update_episode(episode, log_fn=lambda _: None, **kw)  # type: ignore[arg-type]
    report = update_episode(episode, log_fn=lambda _: None, **kw)  # type: ignore[arg-type]

    assert [p.state for p in report.platforms] == ["skipped"]
    assert len(pub.edits) == 1


def test_update_records_the_visibility_the_platform_reports(episode: Path, packed: Path) -> None:
    """Человек открыл ролик руками в Studio — файлы об этом не узнали, и пульт
    показывал «приват» у ролика, который уже смотрели все (12.09.2026). Правка
    читает площадку в любом случае, и прочитанное — не действие: когда менять
    нечего, запись о видимости приводится к тому, что площадка сказала."""
    pub = _published(episode, packed)
    pub.posted["mock001"].privacy = "public"  # открыли в Studio, мимо файлов

    report = update_episode(
        episode,
        to=["youtube"],
        dry_run=False,
        privacy="public",
        provider=pub,
        log_fn=lambda _: None,
    )

    assert [p.state for p in report.platforms] == ["skipped"] and pub.edits == []
    record = load_manifest(episode).publish
    assert record is not None and record.platforms["youtube"].privacy == "public"
    assert any("public" in w for w in report.platforms[0].warnings)


def test_update_refuses_when_the_video_never_left(episode: Path, packed: Path) -> None:
    unpack(packed, episode)
    with pytest.raises(NotReady, match="не уехал"):
        update_episode(episode, to=["youtube"], provider=MockPublish())


def test_gate_holds_the_update_too(episode: Path, packed: Path) -> None:
    """Ролик, уехавший приватно, никто не видел: `--privacy public` для него —
    это и есть публикация, и ворота QA держат её так же, как загрузку."""
    pub = _published(episode, packed)
    m = load_manifest(episode)
    m.awaiting_approval = "qa"
    save_manifest(episode, m)

    with pytest.raises(NotReady, match="qa"):
        update_episode(episode, to=["youtube"], privacy="public", provider=pub)

    report = update_episode(
        episode,
        to=["youtube"],
        dry_run=False,
        force=True,
        privacy="public",
        provider=pub,
        log_fn=lambda _: None,
    )
    assert [p.state for p in report.platforms] == ["done"]


def test_a_cover_the_packaging_does_not_know_about_is_named_out_loud(
    episode: Path, packed: Path
) -> None:
    """Обложка на диске, которой нет в упаковке, не уйдёт — и молчать об этом
    нельзя: человек будет смотреть на неизменившийся значок и гадать, почему.

    Так вышло с первым шортсом: упаковку собрали до того, как обложки вообще
    появились в проекте."""
    pub = _published(episode, packed)
    plan = load_packaging(episode)
    plan.thumbnail = ""  # упаковка старше обложки — ровно случай первого шортса
    (episode / "06_publish" / "publish.json").write_text(
        plan.model_dump_json(indent=2), encoding="utf-8"
    )
    (episode / "05_assemble" / "thumbnail.jpg").write_bytes(b"\xff\xd8\xff")

    report = update_episode(episode, to=["youtube"], provider=pub, log_fn=lambda _: None)

    assert any("thumbnail.jpg" in w and "--only publish" in w for w in report.warnings)


def test_the_same_cover_is_not_sent_twice_but_a_changed_one_is(episode: Path, packed: Path) -> None:
    """Площадка обложку обратно не отдаёт: «уже стоит» мы знаем только по своей
    записи. Без неё правка льёт одну и ту же картинку каждый запуск, а
    изменившуюся не отличает от неизменившейся."""
    unpack(packed, episode)
    cover = episode / "05_assemble" / "thumbnail.jpg"
    cover.write_bytes(b"\xff\xd8\xff\x01")
    plan = load_packaging(episode)
    plan.thumbnail = "05_assemble/thumbnail.jpg"
    plan.platforms = ["youtube"]  # длинный ролик: обложка шортса через API не уходит
    (episode / "06_publish" / "publish.json").write_text(
        plan.model_dump_json(indent=2), encoding="utf-8"
    )
    pub = MockPublish()
    publish_episode(episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None)
    record = load_manifest(episode).publish
    assert record is not None and record.platforms["youtube"].thumbnail  # отпечаток записан

    kw = {"to": ["youtube"], "dry_run": False, "provider": pub, "log_fn": lambda _: None}
    quiet = update_episode(episode, **kw)  # type: ignore[arg-type]
    assert [p.state for p in quiet.platforms] == ["skipped"]

    cover.write_bytes(b"\xff\xd8\xff\x02")  # человек выбрал другой кадр
    again = update_episode(episode, **kw)  # type: ignore[arg-type]
    assert [p.state for p in again.platforms] == ["done"]
    assert any("обложка" in c for c in again.platforms[0].changes)


def test_tag_order_is_not_a_change() -> None:
    """YouTube отдаёт теги по алфавиту, мы присылаем в своём порядке. Сравнение
    строкой находило бы изменение на каждом запуске — правка уходила бы всегда
    и врала, что что-то поменялось."""
    from videoforge.stages.publish_upload import _same

    assert _same("tags", "travel, football, coach", "coach, travel, football")
    assert not _same("tags", "coach, ufc", "coach, ufc, mma")
    assert not _same("title", "Было", "Стало")


def test_unfinished_processing_is_said_out_loud(episode: Path, mock_providers: ProviderSet) -> None:
    """«Почему у ролика нет значка» — это вопрос к обработке на площадке, и
    отвечать на него скриншотами не надо: правка спрашивает и говорит."""
    from videoforge.stages.publish_upload import _platform_state

    assert _platform_state({"processing": "succeeded", "upload_status": "processed"}) == []
    notes = _platform_state({"processing": "processing", "upload_status": "uploaded"})
    assert notes and "обработку" in notes[0]
    assert _platform_state({"upload_status": "rejected"}) == [
        "состояние ролика на площадке: rejected"
    ]


def test_force_sends_the_cover_again(episode: Path, packed: Path) -> None:
    """Запись «мы это уже отправляли» правдива, но не полна: человек мог
    поменять обложку в Studio, и тогда она не значит «оно там лежит».
    `--force` возвращает нашу, не требуя менять файл."""
    unpack(packed, episode)
    (episode / "05_assemble" / "thumbnail.jpg").write_bytes(b"\xff\xd8\xff\x07")
    plan = load_packaging(episode)
    plan.thumbnail = "05_assemble/thumbnail.jpg"
    plan.platforms = ["youtube"]  # длинный ролик: обложка шортса через API не уходит
    (episode / "06_publish" / "publish.json").write_text(
        plan.model_dump_json(indent=2), encoding="utf-8"
    )
    pub = MockPublish()
    publish_episode(episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None)

    kw = {"to": ["youtube"], "dry_run": False, "provider": pub, "log_fn": lambda _: None}
    assert [p.state for p in update_episode(episode, **kw).platforms] == ["skipped"]  # type: ignore[arg-type]
    again = update_episode(episode, force=True, **kw)  # type: ignore[arg-type]
    assert [p.state for p in again.platforms] == ["done"]
    assert any("обложка" in c for c in again.platforms[0].changes)


def _shorts_with_cover(episode: Path, packed: Path) -> Path:
    """Шортс с обложкой в упаковке — как первый ролик канала."""
    unpack(packed, episode)
    (episode / "05_assemble" / "thumbnail.jpg").write_bytes(b"\xff\xd8\xff\x09")
    plan = load_packaging(episode)
    assert plan.platforms == ["youtube_shorts"]
    plan.thumbnail = "05_assemble/thumbnail.jpg"
    plan.thumbnail_why = "22.1 с, на экране «yamal», лицо в кадре"
    (episode / "06_publish" / "publish.json").write_text(
        plan.model_dump_json(indent=2), encoding="utf-8"
    )
    return episode


def test_a_shorts_cover_is_not_sent_through_the_api(
    episode: Path, accounts_root: Path, packed: Path
) -> None:
    """Свою картинку шортсу YouTube принимает только у партнёрских каналов. У
    остальных thumbnails.set отвечает 200, videos.list показывает
    hasCustomThumbnail: true и отдаёт кадр во всех размерах 16:9 — а Studio
    показывает заглушку «три точки» и затирает кадр, выбранный руками
    (замерено 12.09.2026 на первом шортсе; обновление страницы не помогало).
    Поэтому обложка шортса не отправляется, а человеку называется секунда."""
    _shorts_with_cover(episode, packed)
    pub = MockPublish()

    dry = publish_episode(episode, to=["youtube"], provider=pub, log_fn=lambda _: None)
    assert any("22.1 с" in w and "partner: true" in w for w in dry.platforms[0].warnings)

    publish_episode(episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None)
    assert pub.uploads[0][1].thumbnail == ""  # ролик ушёл, картинка — нет
    record = load_manifest(episode).publish
    assert record is not None
    landed = record.platforms["youtube"]
    assert landed.thumbnail == "" and any("22.1 с" in w for w in landed.warnings)

    # и правка её не «возвращает» даже по --force: возвращать нечего
    kw = {"to": ["youtube"], "dry_run": False, "provider": pub, "log_fn": lambda _: None}
    again = update_episode(episode, force=True, **kw)  # type: ignore[arg-type]
    assert [p.state for p in again.platforms] == ["skipped"]
    assert not any("обложка:" in c for c in again.platforms[0].changes)
    assert any("22.1 с" in w for w in again.platforms[0].warnings)


def test_a_partner_channel_sends_the_shorts_cover(
    episode: Path, accounts_root: Path, packed: Path
) -> None:
    """Партнёрскому каналу YouTube свою обложку шортса принимает — ей и
    отправляем. Проверено не на своём канале (партнёрки у него нет), а по
    объявлению YouTube от 24.07.2026: переключатель лежит в аккаунте, чтобы
    включить его, когда канал дорастёт, а не править код."""
    accounts_root.mkdir(parents=True)
    (accounts_root / "t_yt.yaml").write_text(
        "id: t_yt\nnetwork: youtube\nproject: t\npartner: true\n", encoding="utf-8"
    )
    _shorts_with_cover(episode, packed)
    pub = MockPublish()

    report = publish_episode(
        episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None
    )

    assert report.platforms[0].account == "t_yt" and report.platforms[0].warnings == []
    assert pub.uploads[0][1].thumbnail.endswith("thumbnail.jpg")
    record = load_manifest(episode).publish
    assert record is not None and record.platforms["youtube"].thumbnail


def test_a_platform_without_an_id_is_not_pretended_to_be_fixed(episode: Path, packed: Path) -> None:
    """Форма TikTok id ролика не отдаёт: править нечего чем, и это говорится."""
    unpack(packed, episode)

    class Silent(MockPublish):
        def upload(self, video_path: Path, meta: PublishMeta) -> PublishResult:
            return PublishResult(platform=meta.platform, state="done", url="")

    pub = Silent()
    publish_episode(episode, to=["tiktok"], dry_run=False, provider=pub, log_fn=lambda _: None)
    report = update_episode(episode, to=["tiktok"], provider=pub, log_fn=lambda _: None)

    row = report.platforms[0]
    assert row.state == "skipped" and "руками" in row.error


def test_youtube_update_writes_over_what_it_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """`videos.update` заменяет часть целиком, а не сливает поля.

    Поэтому правка идёт поверх прочитанного: иначе смена видимости снимает
    лицензию, встраивание и категорию, которую человек выбрал в Studio. Сеть
    здесь не трогается — проверяем, что именно уходит."""
    from videoforge.providers.youtube_api import YoutubeUpload

    pub = YoutubeUpload("t_yt")
    monkeypatch.setattr(pub, "access_token", lambda: "токен")
    monkeypatch.setattr(
        pub,
        "_video",
        lambda video_id: {
            "snippet": {
                "title": "старое",
                "categoryId": "17",  # человек поменял в Studio на Sports
                "channelTitle": "канал",  # read-only: в запрос лезть не должно
            },
            "status": {
                "privacyStatus": "private",
                "license": "creativeCommon",
                "embeddable": False,
                "uploadStatus": "processed",  # тоже read-only
            },
        },
    )
    sent: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(
        pub,
        "_write",
        lambda token, video_id, snippet, status: sent.update(snippet=snippet, status=status),
    )

    result = pub.update(
        "abc",
        PublishMeta(
            platform="youtube",
            title="новое",
            description="Описание",
            privacy="public",
            language="ru",
            text_language="en",
        ),
    )

    assert result.state == "done" and result.video_id == "abc"
    assert sent["snippet"]["title"] == "новое"
    assert sent["snippet"]["categoryId"] == "17"
    assert sent["snippet"]["defaultLanguage"] == "en"
    assert sent["snippet"]["defaultAudioLanguage"] == "ru"
    assert "channelTitle" not in sent["snippet"]
    assert sent["status"]["privacyStatus"] == "public"
    assert sent["status"]["license"] == "creativeCommon"
    assert sent["status"]["embeddable"] is False
    assert "uploadStatus" not in sent["status"]
    # пометка ИИ — write-only: прочитать её нельзя, а промолчать значит снять
    assert sent["status"]["containsSyntheticMedia"] is True


def test_a_declared_section_wins_over_the_one_set_in_studio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Раздел канала — посередине между «наше» и «человеческое».

    Проект молчит — на площадке остаётся то, что выбрали в Studio (проверено
    тестом выше). Проект объявил — это решение, записанное в файле, и файл
    побеждает: иначе настройка была бы бутафорией, которая работает только на
    новых роликах."""
    from videoforge.providers.youtube_api import YoutubeUpload

    pub = YoutubeUpload("t_yt")
    monkeypatch.setattr(pub, "access_token", lambda: "токен")
    monkeypatch.setattr(
        pub,
        "_video",
        lambda video_id: {"snippet": {"categoryId": "22"}, "status": {"privacyStatus": "public"}},
    )
    sent: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(
        pub,
        "_write",
        lambda token, video_id, snippet, status: sent.update(snippet=snippet, status=status),
    )
    pub.update(
        "abc",
        PublishMeta(platform="youtube", title="т", privacy="public", category="17"),
    )
    assert sent["snippet"]["categoryId"] == "17"


def test_a_refusal_about_rights_says_what_to_do() -> None:
    """Голое «403: insufficient authentication scopes» заставляет человека
    гадать, что он сделал не так, хотя ответ один: повторный вход."""
    import httpx

    from videoforge.providers.youtube_api import _denied

    resp = httpx.Response(
        403,
        json={
            "error": {
                "message": "Request had insufficient authentication scopes.",
                "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
            }
        },
    )
    assert "повторный вход" in _denied(resp)


def test_unknown_rights_are_not_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой список прав — «не знаем», а не «нельзя»: запрет по незнанию
    выдумал бы отказ там, где площадка разрешает."""
    from videoforge.providers.youtube_api import SCOPE_MANAGE, SCOPE_UPLOAD, YoutubeUpload

    pub = YoutubeUpload("t_yt")
    monkeypatch.setattr(pub, "access_token", lambda: "токен")
    assert pub.manages() is True

    pub.granted = frozenset({SCOPE_UPLOAD})
    assert pub.manages() is False

    pub.granted = frozenset({SCOPE_UPLOAD, SCOPE_MANAGE})
    assert pub.manages() is True


def test_youtube_body_carries_the_ai_disclosure() -> None:
    """Тело `videos.insert` — единственное место, где пометка «сделано ИИ»
    попадает на площадку. Сеть здесь не трогается: проверяем, что уходит."""
    from videoforge.providers.youtube_api import YoutubeUpload

    body = YoutubeUpload("t_yt").request_body(
        PublishMeta(platform="youtube", title="Т", description="О", hashtags=["#а", "б"])
    )
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["status"]["privacyStatus"] == "private"
    assert body["snippet"]["tags"] == ["а", "б"]
    assert "#а #б" in body["snippet"]["description"]


# ---------------------------------------------------------------- что с роликом на площадке


def test_status_asks_the_platform_and_repeats_its_words(episode: Path, packed: Path) -> None:
    """«Что с роликом» — ответ площадки, а не догадка по манифесту. Прочитанное
    — не действие: манифест после вопроса такой же, как до."""
    pub = _published(episode, packed)
    before = (episode / "manifest.json").read_bytes()

    report = check_episode(episode, to=["youtube"], provider=pub)

    (row,) = report.platforms
    assert row.where == "platforms" and row.video_id == "mock001"
    assert row.state == "processed" and row.privacy == "private"
    assert row.checks == [] and row.fixed == "" and report.ok
    assert "mock001" in row.line and "processed" in row.line
    assert (episode / "manifest.json").read_bytes() == before


def _lost(episode: Path, packed: Path, *, title: str | None = None) -> MockPublish:
    """Файл отдали, ответа не было: на площадке ролик лежит, в манифесте —
    только отметка in_flight без id (id и приходит в потерянном ответе)."""
    unpack(packed, episode)
    pub = MockPublish()
    pub.posted["mock777"] = PublishMeta(
        platform="youtube", title=title or load_packaging(episode).title, privacy="private"
    )
    m = load_manifest(episode)
    m.publish = PublishRecord(state="failed", at=now(), in_flight={"youtube": now()})
    save_manifest(episode, m)
    return pub


def test_a_lost_upload_the_platform_did_take_is_written_down(episode: Path, packed: Path) -> None:
    """Единственный случай, когда проверка пишет в манифест: загрузка оборвалась,
    а площадка говорит «ролик с таким названием есть и обработан». Это и есть
    вопрос «уехал он или нет», ради которого проверка нужна — и после ответа
    повтор publish не кладёт дубль."""
    pub = _lost(episode, packed)

    report = check_episode(episode, provider=pub)

    (row,) = report.platforms
    assert row.where == "in_flight" and row.video_id == "mock777" and row.state == "processed"
    assert row.fixed and "in_flight" in row.fixed and row.checks == []
    record = load_manifest(episode).publish
    assert record is not None and not record.in_flight and record.state == "done"
    landed = record.platforms["youtube"]
    assert landed.video_id == "mock777" and landed.privacy == "private" and landed.at is not None
    again = publish_episode(
        episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None
    )
    assert [p.state for p in again.platforms] == ["skipped"] and pub.uploads == []


def test_a_lost_upload_the_platform_never_got_stays_in_flight(episode: Path, packed: Path) -> None:
    """Не нашли — не значит «не ушло»: ролик мог уехать под другим названием.
    Манифест не трогается, человеку — ПРОВЕРЬ с обоими выходами."""
    pub = _lost(episode, packed, title="совсем другой ролик")

    report = check_episode(episode, provider=pub)

    (row,) = report.platforms
    assert row.state == "нет на площадке" and row.fixed == ""
    assert row.checks and "--force" in row.checks[0] and "руками" in row.checks[0]
    record = load_manifest(episode).publish
    assert record is not None and "youtube" in record.in_flight and not record.platforms


def test_a_found_upload_still_processing_is_not_written_yet(episode: Path, packed: Path) -> None:
    """«Обработан» — условие записи, а не «нашёлся»: ещё думающий ролик
    остаётся in_flight, и следующий запуск спросит снова."""
    pub = _lost(episode, packed)
    pub.states["mock777"] = {"upload_status": "uploaded", "processing": "processing"}

    report = check_episode(episode, provider=pub)

    (row,) = report.platforms
    assert row.video_id == "mock777" and row.state == "processing" and row.fixed == ""
    assert row.checks and "спроси позже" in row.checks[0]
    record = load_manifest(episode).publish
    assert record is not None and "youtube" in record.in_flight


def test_two_videos_with_our_title_are_left_to_the_human(episode: Path, packed: Path) -> None:
    """Два ролика с нашим названием — один из них дубль, и который наш, машина
    не знает. Записать любой значило бы спрятать дубль."""
    pub = _lost(episode, packed)
    pub.posted["mock778"] = pub.posted["mock777"]

    report = check_episode(episode, provider=pub)

    (row,) = report.platforms
    assert row.fixed == "" and row.checks and "mock777" in row.checks[0]
    record = load_manifest(episode).publish
    assert record is not None and "youtube" in record.in_flight


def test_a_platform_that_disagrees_with_the_manifest_is_named_not_fixed(
    episode: Path, packed: Path
) -> None:
    """Манифест говорит «уехал», площадка — «rejected: copyright». Это не
    опечатка в файле, а событие: файлы не трогаются, человеку — ПРОВЕРЬ."""
    pub = _published(episode, packed)
    pub.states["mock001"] = {"upload_status": "rejected", "reason": "copyright"}
    before = (episode / "manifest.json").read_bytes()

    report = check_episode(episode, provider=pub)

    (row,) = report.platforms
    assert row.state == "rejected" and row.reason == "copyright" and row.fixed == ""
    assert row.checks and "rejected" in row.checks[0] and "copyright" in row.checks[0]
    assert "rejected (copyright)" in row.line
    assert (episode / "manifest.json").read_bytes() == before
    assert report.ok  # спросить удалось; расхождение — ответ человеку, не сбой

    # ролика с таким id на площадке нет — тоже расхождение, и тоже не починка
    pub.states.clear()
    del pub.posted["mock001"]
    (row,) = check_episode(episode, provider=pub).platforms
    assert row.state == "нет на площадке" and row.checks and row.fixed == ""
    assert (episode / "manifest.json").read_bytes() == before


def test_visibility_opened_in_studio_is_a_check_not_a_fix(episode: Path, packed: Path) -> None:
    """Видимость расходится — говорим, но не пишем: записывать прочитанное умеет
    `publish --update`, и это его решение, а не побочный эффект вопроса."""
    pub = _published(episode, packed)
    pub.posted["mock001"].privacy = "public"

    (row,) = check_episode(episode, provider=pub).platforms

    assert row.privacy == "public" and any("видимость" in c for c in row.checks)
    record = load_manifest(episode).publish
    assert record is not None and record.platforms["youtube"].privacy == "private"


def test_status_without_an_id_and_without_a_record_says_so(episode: Path, packed: Path) -> None:
    unpack(packed, episode)
    with pytest.raises(NotReady, match="не уезжал"):
        check_episode(episode, provider=MockPublish())

    class Silent(MockPublish):
        def upload(self, video_path: Path, meta: PublishMeta) -> PublishResult:
            return PublishResult(platform=meta.platform, state="done", url="")

    pub = Silent()
    publish_episode(episode, to=["tiktok"], dry_run=False, provider=pub, log_fn=lambda _: None)
    report = check_episode(episode, provider=pub)
    (row,) = report.platforms
    assert row.error and "id" in row.error and not report.ok
    # площадка, куда ролик не уезжал, — отдельной строкой, а не молчанием
    asked = check_episode(episode, to=["tiktok", "youtube"], provider=pub)
    assert {p.platform: bool(p.error) for p in asked.platforms} == {"tiktok": True, "youtube": True}


def test_sent_episodes_are_those_the_platform_was_given(episode: Path, packed: Path) -> None:
    """Проектный `--status` идёт по эпизодам, которые площадке отдавали:
    принятые и оборванные; сухой прогон — нет."""
    unpack(packed, episode)
    pub = MockPublish()
    assert sent_episodes("t") == []
    publish_episode(episode, to=["youtube"], provider=pub, log_fn=lambda _: None)  # сухой
    assert sent_episodes("t") == []
    publish_episode(episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None)
    assert sent_episodes("t") == ["e1"]


def test_the_cli_prints_the_platforms_answer_and_marks_checks(
    episode: Path, packed: Path, accounts_root: Path
) -> None:
    """Команда печатает то, что сказала площадка, а расхождение — со словом
    ПРОВЕРЬ, чтобы его было видно в отчёте планировщика."""
    from typer.testing import CliRunner

    from videoforge.cli import app

    _published(episode, packed)
    # провайдер из реестра — свежая заглушка: ролика mock001 она не знает, и
    # это как раз расхождение, которое команда обязана назвать
    result = CliRunner().invoke(app, ["publish", "t/e1", "--status", "--provider", "mock"])

    assert result.exit_code == 0, result.output
    assert "t/e1:" in result.output and "youtube: mock001" in result.output
    assert "ПРОВЕРЬ:" in result.output and "нет на площадке" in result.output
    record = load_manifest(episode).publish
    assert record is not None and record.platforms["youtube"].video_id == "mock001"

    whole = CliRunner().invoke(app, ["publish", "t", "--status", "--provider", "mock"])
    assert whole.exit_code == 0 and "t/e1:" in whole.output
    both = CliRunner().invoke(app, ["publish", "t/e1", "--status", "--update"])
    assert both.exit_code == 1 and "вместе не бывает" in both.output


def test_youtube_status_is_the_platforms_words() -> None:
    """Ответ `videos.list` → состояние словами площадки. Сеть не трогается:
    разбор проверяется на ответах, какие площадка отдаёт на самом деле."""
    from videoforge.providers.youtube_api import CONTENT_ID_NOTE, video_status

    done = video_status(
        "abc",
        {
            "status": {"uploadStatus": "processed", "privacyStatus": "public"},
            "processingDetails": {"processingStatus": "succeeded"},
            "contentDetails": {"duration": "PT41S"},
        },
    )
    assert done.state == "done" and done.meta["privacy"] == "public"
    assert done.meta["reason"] == "" and done.url.endswith("abc")
    assert CONTENT_ID_NOTE in done.meta["notes"]  # «обработан» ≠ «без претензий»

    rejected = video_status(
        "abc",
        {"status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}},
    )
    assert rejected.state == "failed" and rejected.meta["reason"] == "copyright"

    thinking = video_status(
        "abc",
        {
            "status": {"uploadStatus": "uploaded"},
            "processingDetails": {
                "processingStatus": "processing",
                "processingProgress": {"partsProcessed": "3", "partsTotal": "10"},
            },
        },
    )
    assert thinking.state == "pending" and any("3/10" in n for n in thinking.meta["notes"])

    blocked = video_status(
        "abc",
        {
            "status": {"uploadStatus": "processed"},
            "contentDetails": {"regionRestriction": {"blocked": ["DE", "FR"]}},
        },
    )
    assert any("DE, FR" in n for n in blocked.meta["notes"])

    broken = video_status(
        "abc",
        {
            "status": {"uploadStatus": "processed"},
            "processingDetails": {
                "processingStatus": "terminated",
                "processingFailureReason": "transcodeFailed",
            },
        },
    )
    assert broken.state == "failed" and broken.meta["reason"] == "transcodeFailed"


def test_youtube_finds_a_lost_upload_by_title_and_time() -> None:
    """Оборванная загрузка ищется в плейлисте загрузок канала по названию и
    времени: наше название режется до 100 знаков ещё при отправке, площадка
    схлопывает пробелы, а старый ролик с тем же названием — не наш."""
    from datetime import UTC, datetime

    from videoforge.providers.youtube_api import TITLE_MAX, match_uploads

    long_title = "Заголовок " * 20  # длиннее TITLE_MAX
    since = datetime(2026, 9, 13, 7, 0, tzinfo=UTC)

    def item(vid: str, title: str, at: str) -> dict[str, object]:
        return {
            "snippet": {"title": title, "publishedAt": at, "resourceId": {"videoId": vid}},
            "contentDetails": {"videoId": vid},
        }

    items = [
        item("new1", " ".join(long_title[:TITLE_MAX].split()), "2026-09-13T07:00:45Z"),
        item("old1", long_title[:TITLE_MAX], "2026-09-01T12:00:00Z"),  # старый — не наш
        item("other", "Другой ролик", "2026-09-13T08:00:00Z"),
        item("undated", long_title[:TITLE_MAX], ""),  # без даты — не отбрасывается
    ]
    found = match_uploads(items, long_title, since=since)
    assert [f.video_id for f in found] == ["new1", "undated"]
    assert found[0].meta["at"] == datetime(2026, 9, 13, 7, 0, 45, tzinfo=UTC)
    # наивное «с какого момента» — тоже UTC, и не роняет сравнение
    assert [f.video_id for f in match_uploads(items, long_title, since=since.replace(tzinfo=None))]


# ---------------------------------------------------------------- пульт


@pytest.fixture
def client(episode: Path, accounts_root: Path) -> TestClient:
    return TestClient(create_app(ui_dist=Path("nonexistent")))


def test_ui_button_asks_for_a_dry_run(
    client: TestClient, episode: Path, packed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unpack(packed, episode)
    monkeypatch.setenv("VF_PUBLISH_PROVIDER", "mock")
    resp = client.post("/api/projects/t/episodes/e1/publish", json={"to": ["youtube"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True and body["platforms"][0]["state"] == "dry_run"
    assert load_manifest(episode).publish is None
    # нечего публиковать — понятная 409, а не пятисотка
    assert client.post("/api/projects/t/episodes/zzz/publish").status_code == 404


def test_ui_update_button_shows_the_diff_and_sends_nothing(
    client: TestClient, episode: Path, packed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кнопка правки в пульте — та же функция, что у CLI, и по умолчанию тоже
    сухой прогон: список «было → станет» и есть экран подтверждения."""
    monkeypatch.setenv("VF_PUBLISH_PROVIDER", "mock")
    _published(episode, packed)
    from videoforge.paths import project_config_path

    cfg = project_config_path("t")
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace("privacy: private", "privacy: public"),
        encoding="utf-8",
    )

    resp = client.post("/api/projects/t/episodes/e1/update", json={"to": ["youtube"]})

    assert resp.status_code == 200
    row = resp.json()["platforms"][0]
    assert row["state"] == "dry_run"
    assert any("видимость" in c for c in row["changes"])
    record = load_manifest(episode).publish
    assert record is not None and record.platforms["youtube"].privacy == "private"
    # править нечего — 409 с объяснением, а не пятисотка
    assert client.post("/api/projects/t/episodes/zzz/update").status_code == 404


def test_cross_site_requests_are_refused(client: TestClient) -> None:
    """Открытая в браузере чужая страница не должна уметь нажать кнопку.

    Пульт слушает localhost без авторизации, и пока он только читал, это ничего
    не стоило; POST, который выкладывает ролик, — стоит."""
    url = "/api/projects/t/episodes/e1/publish"
    assert client.post(url, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(url, headers={"Referer": "http://evil.example/x"}).status_code == 403
    # правка меняет то, что уже видят люди, — её чужая страница тоже не нажмёт
    fix = "/api/projects/t/episodes/e1/update"
    assert client.post(fix, headers={"Origin": "http://evil.example"}).status_code == 403
    # вопрос «что с роликом» умеет записать в манифест — тот же POST, та же проверка
    ask = "/api/projects/t/episodes/e1/publish/status"
    assert client.post(ask, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post(ask, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    # свои запросы проходят (409 — «нечего публиковать», но не 403)
    own = client.post(url, headers={"Origin": "http://testserver"})
    assert own.status_code != 403
    assert client.get("/api/projects", headers={"Origin": "http://evil.example"}).status_code == 200


def test_ui_status_button_asks_the_platform_the_same_way(
    client: TestClient, episode: Path, packed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кнопка «что с роликом» — та же функция, что у CLI: ответ площадки,
    расхождения списком, манифест без изменений."""
    monkeypatch.setenv("VF_PUBLISH_PROVIDER", "mock")
    _published(episode, packed)
    before = (episode / "manifest.json").read_bytes()

    resp = client.post("/api/projects/t/episodes/e1/publish/status", json={"to": ["youtube"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["episode"] == "e1"
    (row,) = body["platforms"]
    assert row["platform"] == "youtube" and row["video_id"] == "mock001"
    # заглушка из реестра свежая и mock001 не знает — расхождение, и оно названо
    assert row["state"] == "нет на площадке" and row["checks"] and row["fixed"] == ""
    assert (episode / "manifest.json").read_bytes() == before
    # спрашивать не о чем — 409 с объяснением; эпизода нет — 404
    assert client.post("/api/projects/t/episodes/zzz/publish/status").status_code == 404
    unpack(packed, episode)  # свежий манифест без записи о публикации
    assert client.post("/api/projects/t/episodes/e1/publish/status").status_code == 409


def test_a_lost_answer_is_not_a_refusal(episode: Path, packed: Path) -> None:
    """Площадка приняла файл, ответ потерялся — повтор НЕ грузит второй раз.

    Судить по типу исключения нельзя: `ConnectionResetError` приходит и после
    того, как ролик взяли. До 12.09.2026 стадия снимала отметку на любом
    `OSError`, и обрыв связи давал дубль на канале (нашёл внешний аудит)."""
    unpack(packed, episode)

    class Dropped(MockPublish):
        """Приняла файл и умерла до ответа."""

        def __init__(self) -> None:
            super().__init__()
            self.accepted = 0

        def upload(self, video_path: Path, meta: PublishMeta) -> PublishResult:
            self.accepted += 1
            raise ConnectionResetError("соединение оборвалось до ответа")

    pub = Dropped()
    first = publish_episode(
        episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None
    )
    assert [p.state for p in first.platforms] == ["unknown"]
    record = load_manifest(episode).publish
    assert record is not None and "youtube" in record.in_flight  # отметка осталась

    second = publish_episode(
        episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None
    )
    assert [p.state for p in second.platforms] == ["unknown"]
    assert pub.accepted == 1  # второй раз файл не отдавали


def test_a_spoken_refusal_clears_the_mark(episode: Path, packed: Path) -> None:
    """Отказ, который площадка произнесла, — исход известный: ролика там нет,
    отметка снимается, и следующий запуск пробует снова."""
    from videoforge.providers.base import Rejected

    unpack(packed, episode)

    class Refusing(MockPublish):
        def __init__(self) -> None:
            super().__init__()
            self.tries = 0

        def upload(self, video_path: Path, meta: PublishMeta) -> PublishResult:
            self.tries += 1
            raise Rejected("YouTube отказал в загрузке (403): quotaExceeded")

    pub = Refusing()
    kw = {"to": ["youtube"], "dry_run": False, "provider": pub, "log_fn": lambda _: None}
    report = publish_episode(episode, **kw)  # type: ignore[arg-type]
    assert [p.state for p in report.platforms] == ["failed"]
    record = load_manifest(episode).publish
    assert record is not None and not record.in_flight

    publish_episode(episode, **kw)  # type: ignore[arg-type]
    assert pub.tries == 2  # известный отказ повтору не мешает


def test_a_failing_second_platform_does_not_lose_the_first(episode: Path, packed: Path) -> None:
    """Успех первой площадки ложится на диск сразу, а не в конце цикла.

    Между принятым роликом и падением на следующей площадке лежит запись о том,
    что он уже на канале. Потерять её — значит при повторе команды загрузить
    его второй раз, а дубль на канале локальной правкой не отменяется."""
    unpack(packed, episode)
    pub = MockPublish()

    def build(network: str, account: str) -> MockPublish:
        if network == "youtube":
            return pub
        raise RuntimeError("Playwright не установлен")  # вторая площадка не собралась

    report = publish_episode(
        episode,
        to=["youtube", "tiktok"],
        dry_run=False,
        build=build,
        log_fn=lambda _: None,
    )
    states = {p.platform: p.state for p in report.platforms}
    assert states["youtube"] == "done" and states["tiktok"] == "failed"

    record = load_manifest(episode).publish
    assert record is not None and "youtube" in record.platforms

    # и повтор не грузит принятое второй раз
    again = publish_episode(
        episode,
        to=["youtube", "tiktok"],
        dry_run=False,
        build=build,
        log_fn=lambda _: None,
    )
    assert {p.platform: p.state for p in again.platforms}["youtube"] == "skipped"
    assert len(pub.uploads) == 1


def test_an_upload_with_no_answer_is_not_retried_blindly(episode: Path, packed: Path) -> None:
    """Файл отдали, ответа не было — это НЕ «не ушло».

    Процесс убили посреди загрузки: ролик мог уже лечь на канал. Слепой повтор
    кладёт дубль, молчание прячет удачную загрузку. Правильный ответ один —
    сказать человеку, что исход неизвестен, и ждать его решения."""
    unpack(packed, episode)

    class Killed(MockPublish):
        def upload(self, video_path: Path, meta: PublishMeta) -> PublishResult:
            raise KeyboardInterrupt("процесс убили посреди загрузки")

    kw = {"to": ["youtube"], "dry_run": False, "log_fn": lambda _: None}
    with pytest.raises(KeyboardInterrupt):
        publish_episode(episode, provider=Killed(), **kw)  # type: ignore[arg-type]

    # след остался: загрузка была начата
    record = load_manifest(episode).publish
    assert record is not None and "youtube" in record.in_flight

    pub = MockPublish()
    again = publish_episode(episode, provider=pub, **kw)  # type: ignore[arg-type]
    assert [p.state for p in again.platforms] == ["unknown"]
    assert not again.ok and len(pub.uploads) == 0  # вслепую не повторили

    # решение человека: --force грузит, и отметка снимается
    forced = publish_episode(episode, provider=pub, force=True, **kw)  # type: ignore[arg-type]
    assert [p.state for p in forced.platforms] == ["done"] and len(pub.uploads) == 1
    record = load_manifest(episode).publish
    assert record is not None and not record.in_flight


def test_publishing_takes_the_episode_lock(episode: Path, packed: Path) -> None:
    """Две одновременные публикации одного ролика — это дубль на канале.

    Замок у публикации тот же, что у прогона: у пульта и терминала нет общей
    памяти, а `videoforge publish` живёт вне workflow и раньше его не брал."""
    from videoforge.core.runner import Busy, episode_lock

    unpack(packed, episode)
    pub = MockPublish()
    with episode_lock(episode, what="прогон"):
        with pytest.raises(Busy):
            publish_episode(
                episode, to=["youtube"], dry_run=False, provider=pub, log_fn=lambda _: None
            )
    assert len(pub.uploads) == 0


def test_the_ui_button_says_busy_instead_of_crashing(
    client: TestClient, episode: Path, packed: Path
) -> None:
    """Занятый эпизод — это не поломка сервера, а 409, как у пересборки."""
    from videoforge.core.runner import episode_lock

    unpack(packed, episode)
    with episode_lock(episode, what="прогон"):
        resp = client.post("/api/projects/t/episodes/e1/publish", json={"dry_run": True})
    assert resp.status_code == 409 and "уже идёт" in resp.json()["detail"]


# ------------------------------------------------------------ токен — у аккаунта


def test_the_token_lives_with_its_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Токен — на аккаунт, а не на сеть: общий файл второй вход перезаписал бы,
    и ролики обоих проектов уехали бы на тот канал, который логинился последним."""
    from videoforge.providers.youtube_api import known_client, load_token, save_token, token_path

    monkeypatch.setenv("VF_SECRETS_DIR", str(tmp_path / ".secrets"))
    first = save_token("clips_yt", "app", "secret", "refresh-clips")
    second = save_token("doc_yt", "app", "secret", "refresh-doc")
    assert first != second and first.parent.name == "clips_yt"
    assert load_token("clips_yt")["refresh_token"] == "refresh-clips"
    assert load_token("doc_yt")["refresh_token"] == "refresh-doc"
    with pytest.raises(ProviderError, match="third_yt"):
        load_token("third_yt")
    with pytest.raises(ProviderError):
        token_path("")

    # клиент OAuth один на приложение: новому аккаунту его искать заново не надо,
    # а refresh-токен чужого канала отсюда не берётся
    found = known_client(prefer="clips_yt")
    assert found is not None and found[:2] == ("app", "secret") and found[2] == first
    found = known_client(prefer="third_yt")
    assert found is not None and found[2] in (first, second)

    # общий файл прежней раскладки не читается как чей-то: только называется
    (tmp_path / ".secrets" / "youtube_token.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ProviderError, match="прежней раскладки"):
        load_token("third_yt")


def test_without_a_youtube_account_there_is_nothing_to_upload_with(
    episode: Path, accounts_root: Path, packed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проект без аккаунта youtube не грузит «куда-нибудь»: токен лежит у
    аккаунта, и провайдер без него не строится — до отметки in_flight и до сети."""
    monkeypatch.setenv("VF_SECRETS_DIR", str(accounts_root.parent / ".secrets"))
    unpack(packed, episode)
    report = publish_episode(
        episode,
        to=["youtube"],
        dry_run=False,
        build=lambda network, account: build_publisher(network, account, name="youtube_api"),
        log_fn=lambda _: None,
    )
    (outcome,) = report.platforms
    assert outcome.state == "failed" and "аккаунт" in outcome.error
    record = load_manifest(episode).publish
    assert record is not None
    assert "youtube" not in record.in_flight and "youtube" not in record.platforms


def test_the_publisher_is_built_for_the_projects_account(
    episode: Path, accounts_root: Path, packed: Path
) -> None:
    """Провайдер строится под аккаунт проекта: именно его id уходит в реестр,
    и по нему YouTube берёт свой токен, а не общий."""
    create_account("t_yt", network="youtube", project="t", handle="@t")
    unpack(packed, episode)
    asked: list[tuple[str, str]] = []

    def build(network: str, account: str) -> MockPublish:
        asked.append((network, account))
        return MockPublish()

    report = publish_episode(
        episode, to=["youtube"], dry_run=False, build=build, log_fn=lambda _: None
    )
    assert asked == [("youtube", "t_yt")]
    assert report.state == "done"
