"""Загрузка готового ролика на площадки — по тем же файлам, что и всё остальное.

Это не стадия конвейера: стадия `publish` пакует ролик (`06_publish/publish.json`),
а здесь берётся готовая упаковка и отдаётся провайдеру. Вне workflow — как
`portfolio` и `prompt_translate`: публикация не должна случаться «заодно» с
прогоном, деньги и репутация текут только по явной команде человека.

Правила, которые здесь зашиты:

- **По умолчанию ничего не уходит.** `dry_run=True` — считаем и показываем, что
  ушло бы; настоящая загрузка только когда человек сказал `--yes`.
- **Дважды не публикуем.** Площадка, записанная в `manifest.publish.platforms`,
  пропускается; повтор — только с `force=True`. Иначе один случайный повтор
  команды кладёт на канал второй такой же ролик.
- **Ворота уважаем.** Эпизод, который ждёт одобрения (`awaiting_approval`), не
  публикуется: QA стоит перед публикацией именно для этого.
- **Отказ и молчание — разные исходы.** Отметку «загрузка начата»
  (`manifest.publish.in_flight`) снимает только `providers.base.Rejected` —
  отказ, который площадка ПРОИЗНЕСЛА. Любое другое исключение означает, что
  ответа не было: файл мог быть принят, отметка остаётся, площадка получает
  состояние `unknown`, и решает человек. Судить по типу исключения нельзя:
  `ConnectionResetError` приходит и после того, как ролик взяли.
- **Пометка ИИ едет с файлом, но не на всяком файле.** `PublishMeta.synthetic`
  берётся из workflow эпизода (`Workflow.synthetic`): `ai_video` рисует кадры и
  синтезирует голос — пометка нужна; `clips_from_long` режет живую съёмку —
  пометка на ней врёт зрителю о том, что он смотрит. Провайдер, который поле не
  умеет, обязан вернуть предупреждение, и оно попадает и в манифест, и в вывод
  команды.
- **Видимость — решение проекта, а не команды.** `channel.privacy` в
  `project.yaml`: `private`, пока канал смотрят глазами, `public`, когда он
  вышел на каденс. `--privacy` перебивает на один запуск.
- **Уехавшее правится, а не перезагружается** (`update_episode`, `publish
  --update`). Открыть видимость, поправить язык, поставить обложку заново — это
  тот же принцип «площадка получает то, что в файлах», только ролик на канале
  уже есть: у него та же ссылка и те же просмотры, и второй такой же рядом не
  появляется. Правка присылает только поля из упаковки; лицензию и встраивание,
  которые выбирал человек в Studio, она переносит как есть. Раздел канала —
  посередине: объявил его проект (`channel.category`) — правка его ставит, это
  решение в файле; молчит проект — раздел остаётся тем, что выбрали в Studio.
- **Что с роликом — спрашивается у площадки, а не угадывается**
  (`check_episode`, `publish --status`). Обработан, ещё думает, отклонён и
  почему — ответ `videos.list`, 1 единица квоты, только чтение. Манифест при
  этом чинится ровно в одном случае, и он однозначный: загрузка оборвалась без
  ответа (`in_flight`), а площадка говорит, что ролик с нашим названием у неё
  есть и обработан — тогда он переезжает в `platforms`, и повтор команды его
  не задублирует. Любое другое расхождение — «ПРОВЕРЬ» человеку, файлы не
  трогаются: запись «уехал», а площадка отвечает «rejected», — это не
  опечатка в манифесте, а событие, и решает по нему человек.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from videoforge.core.accounts import NETWORKS, Account, accounts_for_project
from videoforge.core.manifest import (
    Manifest,
    PlatformPublish,
    PublishRecord,
    load_manifest,
    now,
    save_manifest,
)
from videoforge.core.storyboard import load_storyboard
from videoforge.core.thumbnail import THUMB_FILE, fingerprint
from videoforge.providers.base import (
    ProviderError,
    PublishMeta,
    PublishProvider,
    PublishResult,
    Rejected,
)
from videoforge.stages.publish import DATA_FILE, OUT_DIR
from videoforge.stages.publish import PublishPlan as Packaging

log = logging.getLogger("videoforge.publish")

# формат ролика (PLATFORMS) → сеть, куда он уходит (accounts.NETWORKS)
PLATFORM_NETWORK: dict[str, str] = {
    platform: network for network, info in NETWORKS.items() for platform in info.platforms
}


class NotReady(RuntimeError):
    """Публиковать нечего или нельзя: нет файла, нет упаковки, ждут ворота."""


@dataclass
class PlatformOutcome:
    platform: str
    state: str  # done | dry_run | skipped | failed | unknown
    # unknown — файл ушёл, ответа не было: решает человек, а не повтор
    video_id: str = ""
    url: str = ""
    provider: str = ""
    account: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    # «было → станет» по полям: заполняет правка уже опубликованного
    changes: list[str] = field(default_factory=list)

    @property
    def line(self) -> str:
        head = f"{self.platform}: {self.state}"
        if self.url:
            head += f" · {self.url}"
        if self.error:
            head += f" · {self.error}"
        return head


@dataclass
class PublishReport:
    dry_run: bool
    video: Path
    title: str
    platforms: list[PlatformOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(p.state not in ("failed", "unknown") for p in self.platforms)

    @property
    def state(self) -> Literal["pending", "dry_run", "done", "failed"]:
        if not self.platforms:
            return "pending"
        if any(p.state in ("failed", "unknown") for p in self.platforms):
            return "failed"
        if self.dry_run:
            return "dry_run"
        # `skipped` — «уже лежит там»: повтор команды не должен понижать состояние
        # эпизода с «опубликовано» до «ждём»
        landed = ("done", "skipped")
        return "done" if any(p.state in landed for p in self.platforms) else "pending"


def load_packaging(edir: Path) -> Packaging:
    """`06_publish/publish.json` — то, что стадия publish собрала и проверила."""
    path = edir / OUT_DIR / DATA_FILE
    if not path.exists():
        raise NotReady(
            f"нет упаковки {path}: сначала прогони стадию publish "
            f"(videoforge run <проект>/<эпизод> --only publish)"
        )
    try:
        return Packaging.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise NotReady(f"{path} не разобран: {exc}") from exc


def networks_of(names: list[str]) -> list[str]:
    """Имена площадок как их пишет человек («youtube,tiktok» или форматы вроде
    `youtube_shorts`) → сети, без повторов; незнакомое имя — отказ."""
    out: list[str] = []
    for raw in names:
        name = raw.strip().lower()
        network = name if name in NETWORKS else PLATFORM_NETWORK.get(name, "")
        if not network:
            raise NotReady(
                f"неизвестная площадка {raw!r}; сети: {', '.join(NETWORKS)}; "
                f"форматы: {', '.join(sorted(PLATFORM_NETWORK))}"
            )
        if network not in out:
            out.append(network)
    return out


def target_networks(requested: list[str], packaging: Packaging) -> list[str]:
    """Что просили → сети. Пусто — площадки проекта из упаковки: публикуем
    туда, подо что делали."""
    out = networks_of(requested or packaging.platforms)
    if not out:
        raise NotReady("не сказано, куда публиковать: --to youtube,tiktok")
    return out


def _account(project_id: str, network: str) -> Account | None:
    for acc in accounts_for_project(project_id):
        if acc.network == network:
            return acc
    return None


SHORTS_FORMAT = "youtube_shorts"


def cover_for(
    packaging: Packaging, edir: Path, *, network: str, account: Account | None
) -> tuple[Path | None, str]:
    """Какая обложка уйдёт с роликом — и почему не уйдёт, если нет.

    Шортсу на YouTube своя картинка через API не ставится. `thumbnails.set` её
    принимает, `videos.list` потом показывает `hasCustomThumbnail: true` и
    отдаёт кадр во всех размерах 16:9 — а страница шортса в Studio показывает
    вместо него заглушку «три точки», и кадр, который человек выбрал руками,
    при этом затирается. Свою обложку шортсу YouTube принимает только у каналов
    Партнёрской программы (с 25.07.2026, на десктопе); остальным оставлен выбор
    кадра из ролика, и такого вызова в API нет. Замерено 12.09.2026 на первом
    шортсе канала: после каждой отправки — заглушка, после выбора кадра руками
    — картинка; обновление страницы не меняло ничего, так что это не кэш.
    Поэтому без `partner: true` у аккаунта обложка шортса не отправляется, а
    человеку называется секунда, которую выбрал конвейер. Что на партнёрском
    канале картинка через API встаёт, не проверено — своего такого канала нет.

    Обложка берётся только если файл на месте: путь из упаковки мог пережить
    уборку папки эпизода."""
    cover = edir / packaging.thumbnail if packaging.thumbnail else None
    if cover is None or not cover.exists():
        return None, ""
    partner = bool(account and account.partner)
    if network == "youtube" and SHORTS_FORMAT in packaging.platforms and not partner:
        where = f"accounts/{account.id}.yaml" if account else "accounts/<id>.yaml"
        return None, (
            "обложка шортса через API не уходит: свою картинку шортсу YouTube принимает "
            "только у каналов Партнёрской программы, у остальных она встаёт в Studio "
            "заглушкой и затирает выбранный руками кадр. Выбери кадр в приложении "
            f"YouTube на телефоне — наш: {packaging.thumbnail_why or cover.name}. "
            f"Канал в партнёрке — partner: true в {where}"
        )
    return cover, ""


def _meta(
    packaging: Packaging,
    *,
    network: str,
    language: str,
    text_language: str,
    account: str,
    privacy: str,
    synthetic: bool,
    cover: Path | None,
    category: str = "",
) -> PublishMeta:
    # главы (`packaging.chapters`) уже вписаны в описание стадией publish —
    # блоком «MM:SS Название» в конце; здесь их не склеивать, иначе площадка
    # получит два списка. Хэштеги допишет `tagged_description` провайдера
    description = packaging.description
    if packaging.attribution:  # лицензия, требующая упоминания, едет в описание
        description = f"{description}\n\n{packaging.attribution}".strip()
    return PublishMeta(
        platform=network,
        title=packaging.title,
        description=description,
        hashtags=list(packaging.hashtags),
        privacy=privacy,
        made_for_kids=False,
        synthetic=synthetic,
        account_id=account,
        language=language,
        text_language=text_language,
        thumbnail=str(cover) if cover else "",
        category=category,
    )


def _category(project_id: str, network: str) -> str:
    """Раздел площадки по слову проекта. Пусто — проект молчит, и раздел
    остаётся тем, что стоит: нейтральным у новой загрузки, выбранным человеком
    у уже уехавшего ролика."""
    from videoforge.core.project import category_id, load_project

    try:
        return category_id(load_project(project_id).channel.category, network)
    except (FileNotFoundError, ValueError):
        return ""


def _privacy(project_id: str, asked: str | None) -> str:
    """`--privacy` человека, иначе решение проекта, иначе скрытно."""
    if asked:
        return asked
    from videoforge.core.project import load_project

    try:
        return load_project(project_id).channel.privacy
    except (FileNotFoundError, ValueError):
        # проект не читается — грузим скрытно: ошибка в настройках не должна
        # оборачиваться роликом, который увидели все
        return "private"


def _synthetic(workflow: str) -> bool:
    """Пометка «сделано ИИ» — свойство workflow, которым сделан ролик."""
    from videoforge.core.workflow import load_workflow

    try:
        return load_workflow(workflow).synthetic
    except (FileNotFoundError, ValueError):
        return True  # неизвестный workflow: молча снять пометку хуже, чем поставить лишнюю


# ---------------------------------------------------------------- правка уехавшего

# поля, которыми распоряжаются наши файлы: их правка и присылает. Остальное
# (категория, лицензия, встраивание) остаётся тем, что выбрал человек в Studio
FIELDS: tuple[tuple[str, str], ...] = (
    ("заголовок", "title"),
    ("описание", "description"),
    ("теги", "tags"),
    ("язык речи", "language"),
    ("язык текста", "text_language"),
    ("видимость", "privacy"),
)


def _wanted(meta: PublishMeta) -> dict[str, str]:
    """Чем поля должны стать — из упаковки, теми же значениями, что у загрузки."""
    return {
        "title": meta.title.strip(),
        "description": meta.tagged_description().strip(),
        "tags": ", ".join(t.lstrip("#").strip() for t in meta.hashtags if t.strip()),
        "language": meta.language,
        "text_language": meta.text_language or meta.language,
        "privacy": meta.privacy,
    }


def _thumb_fingerprint(meta: PublishMeta) -> str:
    """Отпечаток обложки, которую отправим. Пусто — обложки в упаковке нет."""
    if not meta.thumbnail:
        return ""
    try:
        return fingerprint(Path(meta.thumbnail))
    except OSError:
        return ""


def _short(value: str) -> str:
    """Длинное поле в строке отчёта — началом и длиной, а не целиком: описание
    на триста знаков делает «было → станет» нечитаемым."""
    one = " ".join(value.split())
    if not one:
        return "пусто"
    return f"«{one}»" if len(one) <= 60 else f"«{one[:57]}…» ({len(one)} знаков)"


def _platform_state(posted: dict[str, str]) -> list[str]:
    """Что площадка говорит о ролике, если это повод насторожиться.

    Обработка, которая не кончилась, — это и есть ответ на вопросы вида
    «почему у ролика нет значка»: без него на них отвечали скриншотами."""
    out: list[str] = []
    processing = posted.get("processing", "")
    if processing and processing != "succeeded":
        out.append(f"площадка ещё не закончила обработку: {processing}")
    upload = posted.get("upload_status", "")
    if upload and upload not in ("processed", "uploaded"):
        out.append(f"состояние ролика на площадке: {upload}")
    return out


def _same(key: str, here: str, there: str) -> bool:
    """Одно ли это значение. Теги площадка отдаёт по алфавиту, а мы присылаем в
    своём порядке, и сравнение строкой находило бы изменение на каждом запуске
    — то есть отправляло бы правку всегда и врало, что что-то поменялось."""
    if key == "tags":
        listed = [
            sorted(t.strip().lower() for t in value.split(",") if t.strip())
            for value in (here, there)
        ]
        return listed[0] == listed[1]
    return here.strip() == there.strip()


def _category_change(posted: dict[str, str], meta: PublishMeta) -> list[str]:
    """Строка про раздел канала — только если проект его объявил.

    Не объявил — сравнивать не с чем: своего мнения о разделе у нас в этом
    случае нет, и «изменением» это быть не может. Показывается названием, а не
    числом: «22 → 17» человеку не говорит ничего."""
    from videoforge.core.project import category_title

    if not meta.category or _same("category", posted.get("category", ""), meta.category):
        return []
    was = posted.get("category", "")
    return [
        f"раздел: {_short(category_title(was, meta.platform)) if was else 'пусто'}"
        f" → {_short(category_title(meta.category, meta.platform))}"
    ]


def _changes(posted: dict[str, str], meta: PublishMeta) -> list[str]:
    """Что правка изменит. Пустой список — на площадке уже то же самое."""
    want = _wanted(meta)
    rows = [
        f"{label}: {_short(posted.get(key, ''))} → {_short(want[key])}"
        for label, key in FIELDS
        if not _same(key, posted.get(key, ""), want[key])
    ]
    return rows + _category_change(posted, meta)


def update_episode(
    edir: Path,
    *,
    to: list[str] | None = None,
    dry_run: bool = True,
    force: bool = False,
    privacy: str | None = None,
    provider: PublishProvider | None = None,
    build: Callable[[str, str], PublishProvider] | None = None,
    log_fn: Callable[[str], None] = log.info,
) -> PublishReport:
    """Привести уже уехавший ролик к упаковке: видимость, текст, языки, обложка.

    Файл не перезагружается — на канале остаётся тот же ролик, с той же ссылкой
    и просмотрами. Нужно это ровно потому, что иначе каждая такая правка
    остаётся ручным действием в Studio, а забытое ручное действие означает
    ролик, которого никто не увидел.

    Защиты от повтора, как у загрузки, здесь нет намеренно: правка идемпотентна
    — второй раз она не находит, что менять, и дубля на канале не бывает.
    Сухой прогон по-прежнему по умолчанию: `videos.update` меняет то, что уже
    видят люди. И ворота держат её так же, как загрузку: ролик, уехавший
    приватно, никто не видел, и `--privacy public` для него — это и есть
    публикация, а не мелкая правка."""
    from videoforge.core.runner import episode_lock

    with episode_lock(edir, what="правка публикации"):
        return _update_episode(
            edir,
            to=to,
            dry_run=dry_run,
            force=force,
            privacy=privacy,
            provider=provider,
            build=build,
            log_fn=log_fn,
        )


def _update_episode(
    edir: Path,
    *,
    to: list[str] | None,
    dry_run: bool,
    force: bool,
    privacy: str | None,
    provider: PublishProvider | None,
    build: Callable[[str, str], PublishProvider] | None,
    log_fn: Callable[[str], None],
) -> PublishReport:
    sb = load_storyboard(edir)
    manifest = load_manifest(edir)
    packaging = load_packaging(edir)
    record = manifest.publish
    if record is None or not record.platforms:
        raise NotReady(
            "ролик ещё никуда не уехал — править нечего "
            "(сначала videoforge publish <проект>/<эпизод> --to youtube --yes)"
        )
    if manifest.awaiting_approval and not force:
        raise NotReady(
            f"эпизод ждёт одобрения стадии {manifest.awaiting_approval}: "
            f"videoforge approve {manifest.project_id}/{manifest.episode_id} "
            f"{manifest.awaiting_approval}"
        )
    video = edir / packaging.video if packaging.video else edir
    report = PublishReport(dry_run=dry_run, video=video, title=packaging.title)
    # обложка лежит рядом с готовым файлом, а в упаковку попадает стадией
    # publish. Упаковка старше обложки — правка её не отправит, и человек будет
    # смотреть на неизменившийся значок и гадать, почему
    on_disk = video.parent / THUMB_FILE
    if on_disk.exists() and not packaging.thumbnail:
        report.warnings.append(
            f"обложка {on_disk.name} есть на диске, но в упаковке её нет — она не уйдёт. "
            f"Пересобрать упаковку: videoforge run "
            f"{manifest.project_id}/{manifest.episode_id} --only publish --force"
        )
    if build is None and provider is None:
        from videoforge.providers.registry import build_publisher

        build = build_publisher

    for network in target_networks(list(to or []), packaging):
        done = record.platforms.get(network)
        if done is None:
            report.platforms.append(
                PlatformOutcome(
                    platform=network, state="skipped", error="на эту площадку ролик не уезжал"
                )
            )
            continue
        if not done.video_id:
            # форма TikTok id не отдаёт: править нечего чем, и врать «готово»
            # здесь хуже, чем сказать про Studio
            report.platforms.append(
                PlatformOutcome(
                    platform=network,
                    state="skipped",
                    url=done.url,
                    error="площадка не вернула id ролика — правится только руками",
                )
            )
            continue
        acc = _account(manifest.project_id, network)
        account = acc.id if acc else ""
        cover, note = cover_for(packaging, edir, network=network, account=acc)
        meta = _meta(
            packaging,
            network=network,
            language=sb.language,
            text_language=sb.text_language,
            cover=cover,
            account=account,
            privacy=_privacy(manifest.project_id, privacy),
            synthetic=_synthetic(manifest.workflow),
            category=_category(manifest.project_id, network),
        )
        try:
            pub = provider if provider is not None else build(network, account) if build else None
            if pub is None:  # недостижимо: провайдер либо передан, либо собран выше
                raise NotReady(f"нечем править {network}")
        except Exception as exc:  # noqa: BLE001 — чем бы ни кончилась сборка провайдера
            report.platforms.append(
                PlatformOutcome(platform=network, state="failed", error=f"нечем править: {exc}")
            )
            log_fn(f"{network}: не поправлено — {exc}")
            continue
        # читаем, что там сейчас. Без этого правка слепа: `videos.update`
        # заменяет часть целиком, и сливать надо поверх прочитанного
        posted: dict[str, str] = {}
        read_error = ""
        try:
            posted = pub.describe(done.video_id)
        except (ProviderError, OSError) as exc:
            read_error = str(exc)
        if read_error and not dry_run:
            report.platforms.append(
                PlatformOutcome(
                    platform=network,
                    state="failed",
                    video_id=done.video_id,
                    url=done.url,
                    provider=pub.name,
                    error=f"что на площадке сейчас — не прочитано: {read_error}",
                )
            )
            log_fn(f"{network}: не поправлено — {read_error}")
            continue
        changes = _changes(posted, meta)
        if read_error:  # сухой прогон: сравнить не с чем, показываем хотя бы наше
            changes = [f"{label}: → {_short(_wanted(meta)[key])}" for label, key in FIELDS]
            changes += _category_change({}, meta)
        # обложку площадка обратно не отдаёт, поэтому сверяемся не с ней, а со
        # своей записью о том, что мы уже отправляли. Запись правдива, но не
        # полна: человек мог поменять обложку в Studio, и тогда «мы это уже
        # отправляли» перестаёт значить «оно там лежит». Вернуть свою — `--force`
        cover_print = _thumb_fingerprint(meta)
        if cover_print and (force or cover_print != done.thumbnail):
            changes.append(f"обложка: {Path(meta.thumbnail).name}")
        notes = _platform_state(posted)
        if note:
            notes.append(note)
        if read_error:
            notes.append(f"состояние на площадке не прочитано: {read_error}")
        if dry_run:
            outcome = PlatformOutcome(
                platform=network,
                state="dry_run",
                video_id=done.video_id,
                url=done.url,
                provider=pub.name,
                account=account,
                changes=changes,
                warnings=notes,
            )
            report.platforms.append(outcome)
            log_fn(f"{network}: сухой прогон правки · {len(changes)} изм.")
            continue
        if not changes:
            # площадка уже такая, как файлы, — но запись о видимости могла
            # отстать: человек открыл ролик руками в Studio, и файлы об этом
            # не узнали (12.09.2026 пульт показывал «приват» у ролика, который
            # уже смотрели все). Прочитанное — не действие, записать его можно
            seen = posted.get("privacy", "")
            if seen and seen != done.privacy:
                done.privacy = seen
                record.platforms[network] = done
                manifest.publish = record
                save_manifest(edir, manifest)
                notes.append(f"видимость на площадке {seen} — записано в манифест")
            report.platforms.append(
                PlatformOutcome(
                    platform=network,
                    state="skipped",
                    video_id=done.video_id,
                    url=done.url,
                    provider=pub.name,
                    error="на площадке уже то же самое",
                    warnings=notes,
                )
            )
            continue
        try:
            result: PublishResult = pub.update(done.video_id, meta)
        except (ProviderError, OSError) as exc:
            report.platforms.append(
                PlatformOutcome(
                    platform=network,
                    state="failed",
                    video_id=done.video_id,
                    url=done.url,
                    provider=pub.name,
                    error=str(exc),
                )
            )
            log_fn(f"{network}: не поправлено — {exc}")
            continue
        done.privacy = meta.privacy
        if result.meta.get("thumbnail") == "done":
            done.thumbnail = cover_print
        record.platforms[network] = done
        manifest.publish = record
        save_manifest(edir, manifest)
        report.platforms.append(
            PlatformOutcome(
                platform=network,
                state=result.state,
                video_id=done.video_id,
                url=done.url or result.url,
                provider=pub.name,
                account=account,
                changes=changes,
                warnings=notes + list(result.warnings),
            )
        )
        log_fn(f"{network}: поправлено · {'; '.join(changes)}")
    return report


def publish_episode(
    edir: Path,
    *,
    to: list[str] | None = None,
    dry_run: bool = True,
    force: bool = False,
    privacy: str | None = None,
    provider: PublishProvider | None = None,
    build: Callable[[str, str], PublishProvider] | None = None,
    log_fn: Callable[[str], None] = log.info,
) -> PublishReport:
    """Загрузить `05_assemble/final.mp4` эпизода туда, куда просят.

    `provider` — один провайдер на все площадки (так тесты подставляют mock);
    `build` — как получить провайдера по сети и аккаунту (по умолчанию реестр).

    Под тем же замком, что прогон: публикация читает и пишет манифест эпизода,
    и два процесса, одновременно решившие «эта площадка ещё не публиковалась»,
    кладут на канал дубль. Сухой прогон замок тоже берёт — он читает то же
    состояние, и врать про «уйдёт» одновременно с настоящей загрузкой незачем."""
    # локальным импортом: ядро знает о стадиях только через реестр, и тянуть
    # runner на уровень модуля отсюда — это цикл
    from videoforge.core.runner import episode_lock

    with episode_lock(edir, what="публикация"):
        return _publish_episode(
            edir,
            to=to,
            dry_run=dry_run,
            force=force,
            privacy=privacy,
            provider=provider,
            build=build,
            log_fn=log_fn,
        )


def _publish_episode(
    edir: Path,
    *,
    to: list[str] | None,
    dry_run: bool,
    force: bool,
    privacy: str | None,
    provider: PublishProvider | None,
    build: Callable[[str, str], PublishProvider] | None,
    log_fn: Callable[[str], None],
) -> PublishReport:
    sb = load_storyboard(edir)
    manifest = load_manifest(edir)
    packaging = load_packaging(edir)

    video = edir / packaging.video if packaging.video else None
    if video is None or not video.exists():
        raise NotReady(f"нет готового файла: {packaging.video or '—'} (сначала assemble)")
    if not packaging.title.strip():
        raise NotReady("в упаковке нет заголовка — публиковать нечего")
    if manifest.awaiting_approval and not force:
        raise NotReady(
            f"эпизод ждёт одобрения стадии {manifest.awaiting_approval}: "
            f"videoforge approve {manifest.project_id}/{manifest.episode_id} "
            f"{manifest.awaiting_approval}"
        )
    blocked = [f"{c.label} {c.value} (надо {c.target})" for c in packaging.failed if c.blocking]
    if blocked and not force:
        raise NotReady(
            "площадка скроет ролик: " + "; ".join(blocked) + " — --force грузит всё равно"
        )
    failed = [c.label for c in packaging.failed]
    report = PublishReport(dry_run=dry_run, video=video, title=packaging.title)
    if failed:  # не запрет: решает человек, но знать он обязан
        report.warnings.append("проверки площадки не сошлись: " + "; ".join(failed))

    record = manifest.publish or PublishRecord()
    if build is None and provider is None:
        from videoforge.providers.registry import build_publisher

        build = build_publisher

    def remember() -> None:
        """Записать состояние публикации на диск немедленно.

        Зовётся после КАЖДОЙ площадки, а не в конце цикла: между удачной
        загрузкой и падением на следующей площадке лежит запись о том, что
        ролик уже на канале, и потерять её нельзя — повтор команды загрузит
        его второй раз, а отменить это локальной правкой уже невозможно."""
        record.state = report.state
        record.at = now()
        record.error = "; ".join(p.error for p in report.platforms if p.state == "failed")
        manifest.publish = record
        save_manifest(edir, manifest)

    for network in target_networks(list(to or []), packaging):
        done = record.platforms.get(network)
        # запись появляется только после удачной загрузки, поэтому её наличия
        # достаточно: id есть не у всех площадок (форма TikTok его не отдаёт),
        # и проверка «есть ли id» пустила бы шортс на канал второй раз
        if done and not force:
            report.platforms.append(
                PlatformOutcome(
                    platform=network,
                    state="skipped",
                    video_id=done.video_id,
                    url=done.url,
                    provider=done.provider,
                    error="уже опубликовано (--force, чтобы повторить)",
                )
            )
            continue
        # прошлый заход отдал файл и не дождался ответа. Повторять вслепую
        # нельзя: если площадка тогда приняла ролик, повтор кладёт дубль
        began = record.in_flight.get(network)
        if began and not force:
            report.platforms.append(
                PlatformOutcome(
                    platform=network,
                    state="unknown",
                    error=(
                        f"прошлая загрузка начата {began:%d.%m %H:%M} и оборвалась без ответа — "
                        f"ролик мог уже уехать. Посмотри канал: если он там, отметь площадку "
                        f"в manifest.publish.platforms; если нет — publish --force"
                    ),
                )
            )
            log_fn(
                f"{network}: исход прошлой загрузки неизвестен — смотри канал, не повторяй вслепую"
            )
            continue
        acc = _account(manifest.project_id, network)
        account = acc.id if acc else ""
        cover, note = cover_for(packaging, edir, network=network, account=acc)
        told = [note] if note else []
        meta = _meta(
            packaging,
            network=network,
            language=sb.language,
            text_language=sb.text_language,
            cover=cover,
            account=account,
            privacy=_privacy(manifest.project_id, privacy),
            synthetic=_synthetic(manifest.workflow),
            category=_category(manifest.project_id, network),
        )
        if dry_run:
            notes = list(told)
            if not account:
                notes.insert(0, f"аккаунта {network} у проекта нет — videoforge account new")
            report.platforms.append(
                PlatformOutcome(
                    platform=network,
                    state="dry_run",
                    account=account,
                    provider=(provider.name if provider else "—"),
                    warnings=notes,
                )
            )
            # видимость и пометку ИИ показываем именно здесь: сухой прогон
            # затем и нужен, чтобы человек увидел решение ДО загрузки
            mark = "пометка ИИ: да" if meta.synthetic else "пометка ИИ: нет"
            log_fn(
                f"{network}: сухой прогон · «{meta.title}» · {video.name} · {meta.privacy} · {mark}"
            )
            continue
        # сборка провайдера — внутри try вместе с загрузкой: неустановленный
        # Playwright для второй площадки не должен уносить из функции успех первой
        try:
            pub = provider if provider is not None else build(network, account) if build else None
            if pub is None:  # недостижимо: провайдер либо передан, либо собран выше
                raise NotReady(f"нечем публиковать в {network}")
            log_fn(f"{network}: гружу {video.name} через {pub.name}…")
        except Exception as exc:  # noqa: BLE001 — чем бы ни кончилась сборка провайдера
            report.platforms.append(
                PlatformOutcome(platform=network, state="failed", error=f"нечем грузить: {exc}")
            )
            log_fn(f"{network}: не ушло — {exc}")
            remember()
            continue
        # отметка «файл отдан» ложится на диск ДО вызова: убитый процесс должен
        # оставить след того, что загрузка началась, иначе неизвестный исход
        # неотличим от «не пробовали»
        record.in_flight[network] = now()
        manifest.publish = record
        save_manifest(edir, manifest)
        try:
            result: PublishResult = pub.upload(video, meta)
        except Rejected as exc:
            # площадка ОТВЕТИЛА отказом: ролика там нет, и это известно —
            # отметку можно снять, повтор безопасен
            record.in_flight.pop(network, None)
            report.platforms.append(
                PlatformOutcome(platform=network, state="failed", provider=pub.name, error=str(exc))
            )
            log_fn(f"{network}: не ушло — {exc}")
            remember()
            continue
        except Exception as exc:  # noqa: BLE001 — см. ниже: тип исключения ничего не доказывает
            # Ответа не было. Обрыв связи, таймаут, чужая ошибка внутри
            # адаптера — по типу исключения нельзя судить, принят ли файл:
            # `ConnectionResetError` приходит и после того, как площадка ролик
            # взяла. Поэтому отметка ОСТАЁТСЯ, а исход называется неизвестным:
            # слепой повтор кладёт дубль, а дубль не отменяется локальной
            # правкой. До 12.09.2026 здесь стояло `except (ProviderError,
            # OSError)` со снятием отметки, и обрыв связи после приёма файла
            # давал вторую загрузку (нашёл внешний аудит, воспроизведено).
            report.platforms.append(
                PlatformOutcome(
                    platform=network,
                    state="unknown",
                    provider=pub.name,
                    error=(
                        f"ответа от площадки не было ({exc}) — ролик мог уже уехать. "
                        f"Посмотри канал: если он там, отметь площадку в "
                        f"manifest.publish.platforms; если нет — publish --force"
                    ),
                )
            )
            log_fn(f"{network}: исход неизвестен — смотри канал, не повторяй вслепую")
            remember()
            continue
        record.in_flight.pop(network, None)
        record.platforms[network] = PlatformPublish(
            video_id=result.video_id,
            url=result.url,
            at=now(),
            provider=pub.name,
            privacy=meta.privacy,
            # обложку запоминаем только если площадка её приняла: иначе правка
            # решит, что она уже там, и не отправит её никогда
            thumbnail=(_thumb_fingerprint(meta) if result.meta.get("thumbnail") == "done" else ""),
            warnings=[*result.warnings, *told],
        )
        manifest.add_cost(
            stage="publish", provider=pub.name, usd=result.cost_usd, note=f"upload {network}"
        )
        report.platforms.append(
            PlatformOutcome(
                platform=network,
                state=result.state,
                video_id=result.video_id,
                url=result.url,
                provider=pub.name,
                account=account,
                warnings=[*result.warnings, *told],
            )
        )
        remember()
        log_fn(f"{network}: {result.state} {result.url or result.video_id}".rstrip())

    if not dry_run:  # сухой прогон манифест не трогает: он ничего не сделал
        remember()
    return report


# ---------------------------------------------------------------- что с роликом на площадке


@dataclass
class PlatformStatus:
    """Что площадка сказала об одном ролике — и сходится ли это с манифестом."""

    platform: str
    where: str  # platforms | in_flight — где ролик записан в манифесте
    video_id: str = ""
    url: str = ""
    # словами площадки: processed | processing | uploaded | rejected | failed |
    # «нет на площадке»; пусто — не спросили (см. error)
    state: str = ""
    reason: str = ""  # что площадка назвала причиной: copyright, duplicate, codec…
    privacy: str = ""
    provider: str = ""
    account: str = ""
    fixed: str = ""  # что записано в манифест; пусто — не трогали
    # расхождения манифеста с ответом площадки: печатаются с «ПРОВЕРЬ», файлы
    # не трогаются — решает человек
    checks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # чего через API не видно и т.п.
    error: str = ""  # площадку не спросили: нечем, не о чем, отказ API

    @property
    def line(self) -> str:
        if self.error:
            head = f"{self.platform}: {self.video_id or '?'} · не спросили — {self.error}"
            return f"{head} · {self.url}" if self.url else head
        state = f"{self.state} ({self.reason})" if self.reason else self.state
        parts = [self.video_id or "?", state]
        if self.privacy:
            parts.append(self.privacy)
        if self.url:
            parts.append(self.url)
        return f"{self.platform}: " + " · ".join(parts)


@dataclass
class StatusReport:
    project_id: str
    episode_id: str
    platforms: list[PlatformStatus] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Каждую площадку удалось спросить. Расхождения в `ok` не входят: это
        ответ человеку, а не сбой команды."""
        return all(not p.error for p in self.platforms)

    @property
    def checks(self) -> list[str]:
        return [c for p in self.platforms for c in p.checks]


def sent_episodes(project_id: str) -> list[str]:
    """Эпизоды проекта, которые площадке отдавали: есть запись о принятом
    ролике или об оборванной загрузке. Сухой прогон и отказ до отправки — нет."""
    from videoforge.core.project import list_episodes
    from videoforge.paths import episode_dir

    out: list[str] = []
    for ep in list_episodes(project_id):
        try:
            rec = load_manifest(episode_dir(project_id, ep)).publish
        except (FileNotFoundError, ValueError):
            continue
        if rec is not None and (rec.platforms or rec.in_flight):
            out.append(ep)
    return out


def check_episode(
    edir: Path,
    *,
    to: list[str] | None = None,
    provider: PublishProvider | None = None,
    build: Callable[[str, str], PublishProvider] | None = None,
) -> StatusReport:
    """Спросить площадку, что с уже отданным роликом: обработан, ещё думает,
    отклонён и почему. Только чтение (у YouTube — `videos.list`, 1 единица
    квоты на ролик), сухого прогона нет — прогонять нечего.

    Манифест чинится ровно в одном случае: площадка записана в `in_flight`
    (файл отдали, ответа не было), а у неё есть ролик с нашим названием,
    загруженный после этого и обработанный — тогда он переезжает в
    `platforms`, и повтор `publish` его не задублирует. Это и есть вопрос, ради
    которого проверка нужна: уехал он или нет. Все остальные расхождения —
    `checks` («ПРОВЕРЬ»), файлы не трогаются.

    Под тем же замком, что публикация: перенос из `in_flight` пишет тот же
    манифест, который в этот момент может писать `publish --force`."""
    from videoforge.core.runner import episode_lock

    with episode_lock(edir, what="проверка публикации"):
        return _check_episode(edir, to=to, provider=provider, build=build)


def _check_episode(
    edir: Path,
    *,
    to: list[str] | None,
    provider: PublishProvider | None,
    build: Callable[[str, str], PublishProvider] | None,
) -> StatusReport:
    manifest = load_manifest(edir)
    record = manifest.publish
    if record is None or not (record.platforms or record.in_flight):
        raise NotReady("ролик никуда не уезжал — спрашивать площадку не о чем")
    report = StatusReport(project_id=manifest.project_id, episode_id=manifest.episode_id)
    # сначала принятые, потом оборванные; одна площадка в обоих списках
    # спрашивается по принятому id, а про второй список говорится отдельно
    networks = [*record.platforms, *(n for n in record.in_flight if n not in record.platforms)]
    asked = networks_of(list(to or []))
    if asked:
        networks = [n for n in asked if n in networks]
        for n in asked:
            if n not in networks:
                report.platforms.append(
                    PlatformStatus(platform=n, where="", error="на эту площадку ролик не уезжал")
                )
    if build is None and provider is None:
        from videoforge.providers.registry import build_publisher

        build = build_publisher

    for network in networks:
        where = "platforms" if network in record.platforms else "in_flight"
        acc = _account(manifest.project_id, network)
        row = PlatformStatus(platform=network, where=where, account=acc.id if acc else "")
        report.platforms.append(row)
        try:
            pub = (
                provider if provider is not None else build(network, row.account) if build else None
            )
            if pub is None:  # недостижимо: провайдер либо передан, либо собран выше
                raise NotReady(f"нечем спросить {network}")
        except Exception as exc:  # noqa: BLE001 — чем бы ни кончилась сборка провайдера
            row.error = f"нечем спросить: {exc}"
            continue
        row.provider = pub.name
        if where == "platforms":
            _check_landed(row, record, pub)
        else:
            _check_lost(row, edir, manifest, pub)
    return report


def _said(row: PlatformStatus, result: PublishResult) -> None:
    """Ответ площадки → строка человеку. Слова площадки, не наши: «rejected
    (copyright)» говорит больше, чем «failed»."""
    meta = result.meta
    upload = str(meta.get("upload_status") or "")
    processing = str(meta.get("processing") or "")
    if not meta.get("found", True):
        row.state = "нет на площадке"
    elif upload in ("rejected", "failed", "deleted"):
        row.state = upload
    elif processing in ("failed", "terminated"):
        row.state = f"обработка {processing}"
    elif upload == "processed" or processing == "succeeded":
        row.state = "processed"
    elif processing == "processing":
        row.state = "processing"
    else:
        row.state = upload or {"done": "processed", "pending": "processing"}.get(
            result.state, result.state
        )
    row.reason = str(meta.get("reason") or "")
    row.privacy = str(meta.get("privacy") or "")
    row.url = row.url or result.url
    row.notes.extend(str(n) for n in meta.get("notes") or [])


def _check_landed(row: PlatformStatus, record: PublishRecord, pub: PublishProvider) -> None:
    """Площадка, которая ролик приняла: спросить по записанному id и сверить."""
    done = record.platforms[row.platform]
    row.video_id, row.url = done.video_id, done.url
    if not done.video_id:
        # форма TikTok id не отдаёт: спросить не о чем, и это говорится
        row.error = "площадка не вернула id ролика — что с ним, смотрят руками"
        return
    try:
        result = pub.check_status(done.video_id)
    except (ProviderError, OSError) as exc:
        row.error = str(exc)
        return
    _said(row, result)
    if not result.meta.get("found", True):
        row.checks.append(
            "в манифесте ролик записан как уехавший, а на площадке ролика с таким id нет "
            "— удалён, или это ролик другого аккаунта"
        )
    elif result.state == "failed":
        why = f": {row.reason}" if row.reason else ""
        row.checks.append(
            f"в манифесте ролик записан как уехавший, площадка говорит {row.state}{why}"
        )
    if row.privacy and done.privacy and row.privacy != done.privacy:
        row.checks.append(
            f"видимость в манифесте {done.privacy}, на площадке {row.privacy} — "
            f"открывали в Studio? publish --update запишет её в манифест"
        )
    if row.platform in record.in_flight:
        began = record.in_flight[row.platform]
        row.checks.append(
            f"площадка записана и как принявшая, и как оборванная ({began:%d.%m %H:%M}): "
            f"повтор с --force не дождался ответа — на канале может лежать второй такой же"
        )


def _check_lost(row: PlatformStatus, edir: Path, manifest: Manifest, pub: PublishProvider) -> None:
    """Площадка, которой файл отдали без ответа: id нет, ищем по названию.

    Единственный случай, когда проверка пишет в манифест — и только когда
    ответ однозначен: ролик с нашим названием один, загружен после начала
    той загрузки и обработан. Ещё обрабатывается — не пишем: «обработан» и
    есть условие, а следующий запуск найдёт его снова."""
    record = manifest.publish
    assert record is not None  # сюда приходят только с записью
    began = record.in_flight[row.platform]
    try:
        title = load_packaging(edir).title
    except NotReady as exc:
        row.error = f"искать по названию нечего: {exc}"
        return
    try:
        found = pub.find_video(title, since=began)
    except (ProviderError, OSError) as exc:
        row.error = str(exc)
        return
    if not found:
        row.state = "нет на площадке"
        row.checks.append(
            f"загрузка начата {began:%d.%m %H:%M} и оборвалась; ролика «{title}» на площадке "
            f"после этого нет. Если он там под другим названием — отметь площадку в "
            f"manifest.publish.platforms руками; если нет — publish --force"
        )
        return
    if len(found) > 1:
        ids = ", ".join(f.video_id for f in found)
        row.checks.append(
            f"на площадке {len(found)} ролика с названием «{title}» ({ids}) — который наш "
            f"и не дубль ли второй, решает человек; манифест не тронут"
        )
        return
    hit = found[0]
    row.video_id, row.url = hit.video_id, hit.url
    try:
        result = pub.check_status(hit.video_id)
    except (ProviderError, OSError) as exc:
        row.error = str(exc)
        return
    _said(row, result)
    if result.state != "done":
        why = f": {row.reason}" if row.reason else ""
        row.checks.append(
            f"ролик найден ({hit.video_id}), но площадка говорит {row.state}{why} — "
            f"в манифесте остаётся in_flight, спроси позже"
        )
        return
    at = hit.meta.get("at")
    record.in_flight.pop(row.platform, None)
    record.platforms[row.platform] = PlatformPublish(
        video_id=hit.video_id,
        url=hit.url,
        at=at if isinstance(at, datetime) else began,
        provider=pub.name,
        privacy=row.privacy,
        warnings=["найден по названию после оборванной загрузки (publish --status)"],
    )
    if not record.in_flight and not record.error:
        record.state = "done"
    record.at = now()
    manifest.publish = record
    save_manifest(edir, manifest)
    row.fixed = "ролик найден и обработан — перенесён из in_flight в platforms"
