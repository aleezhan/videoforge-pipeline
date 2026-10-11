"""Очередь публикаций: порядок из файла, состояние — из файлов эпизода.

Смысл проверок один: очередь не должна заводить собственную правду. Ролик
считается уехавшим, потому что так сказал манифест, а не потому что кто-то
вычеркнул строку.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import unpack

from videoforge.core.manifest import PlatformPublish, PublishRecord, load_manifest, save_manifest
from videoforge.core.project import ProjectSpec, create_episode, create_project
from videoforge.core.queue import (
    SPREAD_DEFAULT_MIN,
    TICK_MAX_ATTEMPTS,
    Schedule,
    due,
    load_order,
    load_schedule,
    load_tick,
    next_ready,
    notices,
    parse_times,
    plan,
    publish_at,
    queue,
    queue_path,
    runway,
    save_schedule,
    scheduled_projects,
    tick,
    tick_result,
)
from videoforge.paths import episode_dir, project_dir

# местный пояс тестов — не UTC намеренно: слоты в файле местные, отметки
# манифеста в UTC, и путаница между ними должна быть видна
TZ = timezone(timedelta(hours=5))
DAY = datetime(2026, 9, 13, tzinfo=TZ)


def _at(hour: int, minute: int = 0, *, days: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute) + timedelta(days=days)


def _ready_files(edir: Path) -> None:
    """Готовый к публикации эпизод по файлам, без рендера: очередь смотрит
    только на их наличие."""
    (edir / "05_assemble").mkdir(exist_ok=True)
    (edir / "05_assemble" / "final.mp4").write_bytes(b"")
    (edir / "06_publish").mkdir(exist_ok=True)
    (edir / "06_publish" / "publish.json").write_text("{}", encoding="utf-8")


def _ready(edir: Path, packed: Path) -> Path:
    """Эпизод с собранным роликом, упаковкой и снятыми воротами.

    Ролик, обложка и упаковка — копия шаблона `packed` из conftest: очереди
    нужен след стадии publish, а не её ход, и до 14.09.2026 два теста здесь
    кодировали 30 с 1080×1920 и гоняли стадию сами — по ~10 с каждый."""
    unpack(packed, edir)
    m = load_manifest(edir)
    m.awaiting_approval = ""
    save_manifest(edir, m)
    return edir


def test_unfinished_and_gated_episodes_are_named(episode: Path, packed: Path) -> None:
    """Не готов — не молча: у каждого состояния своя причина словами."""
    create_episode("t", "e2", title="второй")

    rows = {r.episode: r for r in queue("t")}
    assert rows["e1"].state == "unfinished" and "собранного" in rows["e1"].note
    assert rows["e2"].state == "unfinished"
    assert next_ready("t") is None

    # ворота стоят перед упаковкой: собранный ролик на воротах — «ждёт ворот»,
    # а не «нет упаковки»
    (episode / "05_assemble").mkdir(exist_ok=True)
    (episode / "05_assemble" / "final.mp4").write_bytes(b"")
    m = load_manifest(episode)
    m.awaiting_approval = "qa"
    save_manifest(episode, m)
    assert queue("t")[0].state == "waiting"
    m.awaiting_approval = ""
    save_manifest(episode, m)
    assert queue("t")[0].state == "unfinished" and "упаковки" in queue("t")[0].note

    _ready(episode, packed)
    m = load_manifest(episode)
    m.awaiting_approval = "qa"
    save_manifest(episode, m)
    assert queue("t")[0].state == "waiting"
    assert next_ready("t") is None  # ворота держат и очередь тоже


def test_next_is_the_first_ready_one(episode: Path, packed: Path) -> None:
    _ready(episode, packed)
    item = next_ready("t")
    assert item is not None and item.episode == "e1" and item.state == "ready"

    # уехало — знает манифест, и очередь читает это оттуда, а не из своей отметки
    m = load_manifest(episode)
    m.publish = PublishRecord(state="done", platforms={"youtube": PlatformPublish(video_id="x")})
    save_manifest(episode, m)
    assert queue("t")[0].state == "published"
    assert next_ready("t") is None


def test_written_order_wins_and_the_rest_goes_last(episode: Path) -> None:
    """Порядок задаёт файл; забытый в нём эпизод не пропадает, а встаёт в конец."""
    create_episode("t", "e2", title="второй")
    create_episode("t", "e3", title="третий")
    queue_path("t").write_text("episodes:\n  - e3\n  - e1\n", encoding="utf-8")

    assert load_order("t") == ["e3", "e1", "e2"]

    # битый файл — не отказ работать: порядок как на диске
    queue_path("t").write_text("episodes: [не список\n", encoding="utf-8")
    assert load_order("t") == ["e1", "e2", "e3"]


def test_schedule_lives_in_the_queue_file_next_to_the_order(episode: Path) -> None:
    """Слоты пишутся блоком в тот же queue.yaml: порядок и комментарии человека
    остаются, блок при второй записи заменяется, а не дописывается."""
    queue_path("t").write_text("# порядок выпуска\nepisodes:\n  - e1  # первый\n", encoding="utf-8")
    save_schedule("t", Schedule(times=parse_times(["18:00", "12:00"])))
    text = queue_path("t").read_text(encoding="utf-8")
    assert "# порядок выпуска" in text and "- e1  # первый" in text
    assert load_schedule("t") == Schedule(times=("12:00", "18:00"))
    assert load_order("t") == ["e1"]

    save_schedule("t", Schedule(times=("09:30",), paused=True, spread_min=15))
    text = queue_path("t").read_text(encoding="utf-8")
    assert text.count("schedule:") == 1
    assert load_schedule("t") == Schedule(times=("09:30",), paused=True, spread_min=15)

    # YAML 1.1 читает незакавыченное 12:00 как число 720 — человек кавычки забудет;
    # разброс не задан — по умолчанию, мусор в нём — тоже по умолчанию
    queue_path("t").write_text("schedule:\n  times: [12:00, 18:00]\n", encoding="utf-8")
    assert load_schedule("t").times == ("12:00", "18:00")
    assert load_schedule("t").spread_min == SPREAD_DEFAULT_MIN
    queue_path("t").write_text("schedule:\n  times: [12:00]\n  spread_min: -5\n", encoding="utf-8")
    assert load_schedule("t").spread_min == SPREAD_DEFAULT_MIN
    # мусор — «расписания нет», а не падение; а вот явная правка мусором — ошибка
    queue_path("t").write_text("schedule: [что-то]\n", encoding="utf-8")
    assert load_schedule("t") == Schedule()
    with pytest.raises(ValueError, match="HH:MM"):
        parse_times(["25:00"])


def test_scheduler_walks_every_live_schedule_and_never_a_bench(episode: Path) -> None:
    """Тик без имени проекта обходит проекты с расписанием: второй канал
    попадает в обход правкой своего queue.yaml, а не задачи планировщика.
    Полигон (kind: test) и скрытый образец не выходят никогда, пауза выводит
    из обхода, а битый project.yaml соседа список не роняет."""
    assert scheduled_projects() == []  # у t расписания пока нет
    save_schedule("t", Schedule(times=("12:00",)))
    create_project(ProjectSpec(id="u", name="Второй канал"))
    save_schedule("u", Schedule(times=("18:00",)))
    create_project(ProjectSpec(id="lab", name="Полигон", kind="test"))
    save_schedule("lab", Schedule(times=("12:00",)))
    create_project(ProjectSpec(id="sample", name="Образец"))
    save_schedule("sample", Schedule(times=("12:00",)))
    pfile = project_dir("sample") / "project.yaml"
    text = pfile.read_text(encoding="utf-8")
    assert "hidden: false" in text  # строка шаблона; правим её, а не дописываем второй ключ
    pfile.write_text(text.replace("hidden: false", "hidden: true"), encoding="utf-8")
    assert scheduled_projects() == ["t", "u"]

    save_schedule("u", Schedule(times=("18:00",), paused=True))
    assert scheduled_projects() == ["t"]

    create_project(ProjectSpec(id="typo", name="Опечатка"))
    save_schedule("typo", Schedule(times=("12:00",)))
    (project_dir("typo") / "project.yaml").write_text("kind: канал\n", encoding="utf-8")
    create_project(ProjectSpec(id="torn", name="Рваный YAML"))
    save_schedule("torn", Schedule(times=("12:00",)))
    (project_dir("torn") / "project.yaml").write_text("kind: [\n", encoding="utf-8")
    assert scheduled_projects() == ["t"]


def test_due_follows_the_slot_and_what_the_platform_answered(episode: Path) -> None:
    """Пора — когда слот прошёл и после него площадка ничего не принимала.
    Сухой прогон отметкой не считается, принятая загрузка и отказ — считаются."""
    _ready_files(episode)
    assert due("t", _at(13)) == (None, "расписания нет")

    save_schedule("t", Schedule(times=("12:00", "18:00"), spread_min=0))
    verdict, why = due("t", _at(11, 59))
    assert verdict is None and "ещё не наступил" in why
    verdict, _ = due("t", _at(12))
    assert verdict is not None and verdict.slot == _at(12)
    assert verdict.item is not None and verdict.item.episode == "e1"

    m = load_manifest(episode)
    m.publish = PublishRecord(state="dry_run", at=_at(12, 5).astimezone(UTC))
    save_manifest(episode, m)
    assert due("t", _at(12, 15))[0] is not None  # считали, что уйдёт — не ушло

    m.publish = PublishRecord(
        state="done",
        at=_at(12, 6).astimezone(UTC),
        platforms={"youtube": PlatformPublish(video_id="x", at=_at(12, 6).astimezone(UTC))},
    )
    save_manifest(episode, m)
    verdict, why = due("t", _at(12, 30))
    assert verdict is None and "e1" in why
    # следующий слот — снова пора, даже если готовых уже нет: отчёт «нечего» нужен
    verdict, _ = due("t", _at(18))
    assert verdict is not None and verdict.item is None

    m.publish = PublishRecord(state="failed", at=_at(18, 2).astimezone(UTC), error="нет сети")
    save_manifest(episode, m)
    assert due("t", _at(18, 15))[0] is None  # отказ не повторяется вслепую

    save_schedule("t", Schedule(times=("12:00", "18:00"), paused=True, spread_min=0))
    assert due("t", _at(18, 15, days=1)) == (None, "расписание на паузе")


def test_publish_second_is_spread_inside_the_slot_window(episode: Path) -> None:
    """Слот — начало окна: ролик уходит в случайную, но для всех одну и ту же
    секунду внутри `spread_min`; ровно 12:00:00 каждый день — почерк скрипта."""
    _ready_files(episode)
    schedule = Schedule(times=("12:00", "18:00"), spread_min=40)
    save_schedule("t", schedule)
    at = publish_at("t", schedule, _at(12))
    assert _at(12) <= at < _at(12, 40) and at != _at(12)
    assert publish_at("t", schedule, _at(12)) == at  # тик в 12:00 и тик в 12:15 зовут одну секунду
    assert publish_at("t", schedule, _at(12, days=1)) != at  # а день на день не похож
    assert publish_at("other", schedule, _at(12)) != at

    verdict, why = due("t", _at(12))
    assert verdict is None and f"выход в {at:%H:%M:%S}" in why
    assert due("t", at - timedelta(seconds=1))[0] is None
    assert due("t", at)[0] is not None

    # план и запас называют секунду выхода, а не слот
    rows = plan("t", _at(11))
    assert rows[0].at == at and rows[0].item is not None and rows[0].item.episode == "e1"
    assert rows[1].at == publish_at("t", schedule, _at(18))
    assert plan("t", _at(12, 1))[0].at == at  # слот наступил, секунда впереди — он первый

    # тик ждёт цель только когда она ближе своего интервала (15 минут)
    t = tick("t", _at(12))
    assert not t.due and t.target == at
    assert t.wait_sec(_at(12)) == int((at - _at(12)).total_seconds())
    assert replace(t, target=_at(12, 30)).wait_sec(_at(12)) == 0  # дальше интервала — не ждёт
    assert replace(t, target=_at(11, 30)).wait_sec(_at(12)) == 0  # цель прошла — не ждёт
    assert tick("t", at).due

    # шаг задачи приносит сам тик: на редком шаге он ждёт дольше и протухает позже
    rare = tick("t", _at(12), every_min=120)
    assert rare.wait_sec(_at(12)) == int((at - _at(12)).total_seconds())
    assert replace(rare, target=_at(12, 30)).wait_sec(_at(12)) > 0  # 30 минут — внутри шага
    assert not rare.stale(_at(17, 30)) and rare.stale(_at(18, 30))  # 3 × 120 минут
    assert load_tick("t").every_min == 120  # шаг лёг в след: пульт читает его, а не константу
    assert tick("t", _at(12)).stale(_at(13))  # шаг вернулся к умолчанию — и свежесть с ним

    # окна двух слотов не накладываются: разброс режется до зазора между ними
    tight = Schedule(times=("12:00", "12:10"), spread_min=40)
    assert _at(12) <= publish_at("t", tight, _at(12)) < _at(12, 9)
    assert publish_at("t", Schedule(times=("12:00",), spread_min=0), _at(12)) == _at(12)


def test_tick_handles_a_slot_once(episode: Path) -> None:
    """Тик, запустивший публикацию в 12:00, не запускает её в 12:15: ролик ещё
    грузится или загрузка не удалась, и повтор кладёт дубль, а не чинит."""
    _ready_files(episode)
    save_schedule("t", Schedule(times=("12:00", "18:00"), spread_min=0))
    first = tick("t", _at(12, 1))
    assert first.due and first.handled == _at(12) and first.attempts == 1 and first.item == "e1"
    second = tick("t", _at(12, 16))
    assert not second.due and "отработан тиком в 12:01" in second.why
    saved = load_tick("t")
    assert saved is not None and saved.handled == _at(12) and saved.at == _at(12, 16)
    assert tick("t", _at(18, 1)).due  # новый слот — новая работа


def test_failed_attempt_gives_the_slot_back(episode: Path) -> None:
    """Слот отработан по результату, а не по запуску: Claude не достучался до
    API — следующий тик повторяет, до трёх раз; ушло — слот закрыт до следующего.
    13.09.2026 без этого слот 18:00 пропал из-за минутного обрыва сети."""
    _ready_files(episode)
    save_schedule("t", Schedule(times=("12:00", "18:00"), spread_min=0))
    assert tick("t", _at(12, 1)).due
    done = tick_result("t", 1, _at(12, 4))
    assert done is not None and done.failed and "повтор" in done.why
    again = tick("t", _at(12, 16))
    assert again.due and again.attempts == 2 and "попытка 1 в 12:01 упала" in again.why
    # код 0, но ролик был, а площадка не отвечала — тоже провал: запуск прошёл,
    # публикации не случилось (разрешение не дали, скилл остановился)
    done = tick_result("t", 0, _at(12, 18))
    assert done is not None and done.failed
    third = tick("t", _at(12, 31))
    assert third.due and third.attempts == TICK_MAX_ATTEMPTS
    tick_result("t", 1, _at(12, 33))
    gave_up = tick("t", _at(12, 46))
    assert not gave_up.due and "3 попытки упали" in gave_up.why
    assert tick("t", _at(18, 1)).attempts == 1  # новый слот — счёт заново

    m = load_manifest(episode)
    m.publish = PublishRecord(
        state="done",
        at=_at(18, 3).astimezone(UTC),
        platforms={"youtube": PlatformPublish(video_id="x", at=_at(18, 3).astimezone(UTC))},
    )
    save_manifest(episode, m)
    done = tick_result("t", 0, _at(18, 4))
    assert done is not None and not done.failed and "ушёл e1" in done.why
    assert not tick("t", _at(18, 16)).due
    assert tick_result("t", 1, _at(19)) is not None  # след есть — есть что записать

    # площадка отказала сама (state: failed) — повтора нет: она могла принять и не ответить
    m.publish = PublishRecord(state="failed", at=_at(18, 3).astimezone(UTC), error="quota")
    save_manifest(episode, m)
    assert not tick("t", _at(18, 20)).due


def test_offline_tick_keeps_the_slot_for_later(episode: Path) -> None:
    """Сети нет — слот не тронут: ни попытки, ни отметки, только «жду»."""
    _ready_files(episode)
    save_schedule("t", Schedule(times=("12:00",), spread_min=0))
    waiting = tick("t", _at(12, 1), online=False)
    assert not waiting.due and waiting.handled is None and "сети нет" in waiting.why
    assert tick_result("t", 1, _at(12, 2)) is None  # запуска не было — записывать нечего
    back = tick("t", _at(12, 16), online=True)
    assert back.due and back.attempts == 1


def test_plan_places_ready_in_order_and_names_where_content_ends(episode: Path) -> None:
    """План — готовые по очереди на ближайшие слоты плюс первый пустой слот:
    он и есть день, когда канал замолчит. Запас меньше трёх дней — предупреждение."""
    for ep in ("e2", "e3"):
        create_episode("t", ep, title=ep)
    for ep in ("e1", "e2", "e3"):
        _ready_files(episode_dir("t", ep))
    queue_path("t").write_text("episodes:\n  - e3\n  - e1\n", encoding="utf-8")
    save_schedule("t", Schedule(times=("12:00", "18:00"), spread_min=0))
    now = _at(13)  # 12:00 прошёл и ждёт тика

    rows = [(r.at, r.item.episode if r.item else None, r.pending) for r in plan("t", now)]
    assert rows == [
        (_at(12), "e3", True),
        (_at(18), "e1", False),
        (_at(12, days=1), "e2", False),
        (_at(18, days=1), None, False),
    ]
    rw = runway("t", now)
    assert (rw.ready, rw.per_day, rw.ends, rw.days, rw.level) == (
        3,
        2,
        _at(18, days=1),
        1.2,
        "warn",
    )

    notes = notices("t", now, live=True)
    assert any("контента на 1.2 дн." in n.text and "до 14.09 18:00" in n.text for n in notes)
    assert any("не тикал ни разу" in n.text for n in notes)  # план без планировщика — не обещание
    tick("t", now)
    assert not any("тикал" in n.text for n in notices("t", now, live=True))
    assert any("молчит" in n.text for n in notices("t", now + timedelta(hours=1), live=True))

    for ep in ("e1", "e2", "e3"):
        m = load_manifest(episode_dir("t", ep))
        m.awaiting_approval = "qa"
        save_manifest(episode_dir("t", ep), m)
    assert plan("t", now)[0].item is None and runway("t", now).level == "bad"
    assert any(
        "выкладывать нечего" in n.text and "ждут ворот: 3" in n.text for n in notices("t", now)
    )
    # у полигона без расписания предупреждать нечего, у живого канала — есть о чём
    save_schedule("t", Schedule(spread_min=0))
    assert notices("t", now) == [] and "расписания нет" in notices("t", now, live=True)[0].text
