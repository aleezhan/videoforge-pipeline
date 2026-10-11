"""Выход продакшена: сколько попыток стоит принятый кадр (`videoforge yield`,
`core/production.py`, `features/production_yield`).

Считается по файлам, без вызовов: попытки на принятый актив, доля с первой,
деньги за отвергнутые и причины. Причины есть только у прогонов с журналом
попыток; у старых — «не сохранены», и это число, а не молчание. Журнал
расходов помнит генерации, которые счётчик сцены забыл (сброшен руками), —
на ep002 это 11 против 2 у одной сцены."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from videoforge.cli import app
from videoforge.core.manifest import CostEntry, Manifest, StageRecord, load_manifest, save_manifest
from videoforge.core.production import (
    REPLACED,
    EpisodeFiles,
    load_episodes,
    measure,
    scene_yield,
)
from videoforge.core.storyboard import (
    Attempt,
    Scene,
    SourceSegment,
    Storyboard,
    load_storyboard,
    save_storyboard,
)

PROVIDER = "comfy:klein4b"


def _scene(
    sid: str,
    attempts: int,
    *,
    cost: float = 0.0,
    provider: str = PROVIDER,
    status: str = "pass",
    log: list[Attempt] | None = None,
    kept: int | None = None,
) -> Scene:
    """Сцена с активом-картинкой: `kept` — номер принятой попытки в имени файла."""
    kept = attempts if kept is None else kept
    s = Scene(id=sid, order=int(sid[1:]), visual_prompt=f"кадр {sid}")
    s.attempts["image"] = attempts
    s.cost_usd["image"] = cost
    s.provenance["image"] = provider
    s.assets.image = f"02_images/{sid}_v{kept:02d}.png"
    s.checks.image = status  # type: ignore[assignment]
    if log is not None:
        s.attempt_log["image"] = log
    return s


def _files(scenes: list[Scene], manifest: Manifest | None = None) -> EpisodeFiles:
    sb = Storyboard(project_id="p", episode_id="e1", aspect_ratio="9:16", scenes=scenes)
    return EpisodeFiles("p", "e1", sb, manifest)


def _manifest(ledger: list[CostEntry] | None = None, **stages: StageRecord) -> Manifest:
    return Manifest(
        project_id="p",
        episode_id="e1",
        workflow="ai_video",
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
        ledger=ledger or [],
        stages=stages,
    )


def _entry(scene: str, n: int, usd: float) -> CostEntry:
    return CostEntry(
        at=datetime(2026, 9, 8, tzinfo=UTC),
        stage="images",
        provider=PROVIDER,
        usd=usd,
        scene_id=scene,
        note=f"image v{n}",
    )


def test_row_counts_attempts_money_and_reasons_and_skips_mock() -> None:
    """Две старые сцены (1 и 3 попытки, без журнала) и одна с журналом причин:
    медиана 3, с первой — треть, деньги старой сцены — оценка (≈), новой —
    запись; причины только у новой, у старой — «не сохранены»; заглушка не
    в счёте, а в пропущенных."""
    with_log = [
        Attempt(
            n=1,
            path="02_images/s3_v01.png",
            passed=False,
            issues=["поток вверх ", "лишний объект"],
            cost_usd=0.012,
        ),
        Attempt(
            n=2, path="02_images/s3_v02.png", passed=False, issues=["поток вверх."], cost_usd=0.011
        ),
        Attempt(n=3, path="02_images/s3_v03.png", passed=True, cost_usd=0.010),
    ]
    files = _files(
        [
            _scene("s1", 1, cost=0.01, status="skipped"),  # принят, но никто не смотрел
            _scene("s2", 3, cost=0.03),  # старый прогон: сумма есть, попыток по одной нет
            _scene("s3", 3, cost=0.033, log=with_log),
            _scene("s4", 2, cost=0.0, provider="mock"),
        ]
    )
    result = measure([files])

    assert result.mock_scenes == 1 and result.cut_scenes == 0
    assert len(result.rows) == 1
    row = result.rows[0]
    assert (row.kind, row.provider, row.aspect_ratio, row.project) == (
        "image",
        PROVIDER,
        "9:16",
        "p",
    )
    assert row.scenes == 3 and row.accepted == 3 and row.attempts == 7
    assert row.unchecked == 1  # «с первой» у s1 — потому что проверки не было
    assert row.rejected == 4
    assert row.mean == round(7 / 3, 2) and row.median == 3.0
    assert row.first_try_share == round(1 / 3, 3)
    # s2: 0.03 × 2/3 по средней (оценка); s3: 0.012 + 0.011 по журналу (запись)
    assert row.rejected_usd == round(0.02 + 0.023, 6) and row.estimated == 1
    assert row.top_reasons() == [("поток вверх", 2), ("лишний объект", 1)]  # пробелы и точка слиты
    assert row.unexplained == 2  # две отвергнутые попытки s2 — без причин

    data = row.as_dict()
    assert data["accepted_unchecked"] == 1
    assert data["rejected_usd_estimated"] is True
    assert data["reasons"][0] == {"issue": "поток вверх", "count": 2}
    assert data["reasons_unknown"] == 2 and data["episodes"] == ["e1"]


def test_ledger_remembers_generations_the_counter_forgot() -> None:
    """Счётчик сцены говорит 2, журнал расходов — 5 генераций (сброс руками,
    как у s03 в ep002): попыток 5, принятая — последняя `v2` по имени файла,
    деньги за остальные четыре — из журнала, не оценка. Упавшая попытка без
    строки в журнале (ep001 s05) стоит ноль, и оценка ей не приписывается."""
    ledger = [
        _entry("s1", 1, 0.001),
        _entry("s1", 2, 0.001),
        _entry("s1", 3, 0.001),
        _entry("s1", 1, 0.002),  # второй заход с нуля
        _entry("s1", 2, 0.0005),  # принятая: s1_v02.png
        _entry("s2", 2, 0.0147),  # первая попытка s2 упала — строки нет
    ]
    files = _files(
        [_scene("s1", 2, cost=0.0055, kept=2), _scene("s2", 2, cost=0.0147, kept=2)],
        _manifest(ledger),
    )
    row = measure([files]).rows[0]

    assert row.attempts == 7 and row.accepted == 2 and row.rejected == 5
    assert row.rejected_usd == 0.005 and row.estimated == 0
    assert row.per_accepted == [5, 2] and row.first_try_share == 0.0
    assert row.unexplained == 5  # журнала попыток нет — причин нет


def test_unaccepted_scene_counts_all_attempts_as_rejected_and_replaced_is_named() -> None:
    """Сцена со статусом fail: принятого нет, все попытки отвергнуты вместе с
    деньгами; оставленная лучшая — тоже. Прошедшая, но заменённая повтором
    попытка называется своей причиной, а не молчит."""
    log = [
        Attempt(n=1, path="02_images/s1_v01.png", passed=False, issues=["ракурс"], cost_usd=0.01),
        Attempt(n=2, path="02_images/s1_v02.png", passed=False, issues=["ракурс"], cost_usd=0.01),
    ]
    replaced = [
        Attempt(n=1, path="02_images/s2_v01.png", passed=True, cost_usd=0.01),
        Attempt(n=2, path="02_images/s2_v02.png", passed=True, cost_usd=0.01),
    ]
    files = _files(
        [
            _scene("s1", 2, cost=0.02, status="fail", log=log, kept=2),
            _scene("s2", 2, cost=0.02, log=replaced, kept=2),
        ]
    )
    row = measure([files]).rows[0]

    assert row.accepted == 1 and row.attempts == 4 and row.rejected == 3
    assert row.rejected_usd == 0.03 and row.mean == 4.0
    assert row.reasons == {"ракурс": 2, REPLACED: 1} and row.unexplained == 0

    one = scene_yield(files.storyboard.scenes[0], "image", [])
    assert one is not None and not one.accepted and one.rejected_usd == 0.02
    assert scene_yield(files.storyboard.scenes[0], "video", []) is None  # клипа не делали


def test_cut_scenes_since_and_kind_filter() -> None:
    """Сцена-отрезок нарезки не считается (у cut нет проверки); `since`
    сравнивается с последним запуском стадии, без записи — с созданием;
    неизвестный вид — ошибка, а не пустая таблица."""
    cut = Scene(id="c1", order=1, source=SourceSegment(path="src.mp4", start_sec=0, end_sec=5))
    cut.attempts["video"] = 2
    cut.provenance["video"] = "cut"
    old = _files(
        [_scene("s1", 1, cost=0.01), cut],
        _manifest(images=StageRecord(finished_at=datetime(2026, 9, 5, tzinfo=UTC))),
    )
    fresh = _files([_scene("s1", 2, cost=0.02)], _manifest())  # без записи стадии: 2026-09-08
    assert old.ref == "p/e1"

    result = measure([old, fresh], since=datetime(2026, 9, 6, tzinfo=UTC).date())
    assert result.skipped_episodes == 1 and result.cut_scenes == 0  # отброшен целиком
    assert [r.attempts for r in result.rows] == [2]

    both = measure([old, fresh])
    assert both.cut_scenes == 1 and both.rows[0].attempts == 3
    assert measure([old, fresh], kinds=("video",)).rows == []
    with pytest.raises(ValueError, match="audio"):
        measure([old], kinds=("audio",))


def test_cli_table_and_json_read_the_episode_files(episode: Path) -> None:
    """`videoforge yield <проект>` читает раскадровку и манифест с диска: таблица
    называет провайдера и пропущенное, `--json` отдаёт тот же словарь;
    заглушки из демо-проекта не попадают в строки. Плохая дата и вид — ошибка."""
    sb = load_storyboard(episode)
    sb.scenes[0].attempts["image"] = 3
    sb.scenes[0].cost_usd["image"] = 0.03
    sb.scenes[0].provenance["image"] = PROVIDER
    sb.scenes[0].assets.image = "02_images/s1_v03.png"
    sb.scenes[0].checks.image = "pass"
    sb.scenes[1].attempts["image"] = 1
    sb.scenes[1].provenance["image"] = "mock"
    sb.scenes[1].assets.image = "02_images/s2_v01.png"
    sb.scenes[1].checks.image = "pass"
    # клип s1 принят без проверки: в таблице это называется отдельной строкой
    sb.scenes[0].attempts["video"] = 1
    sb.scenes[0].provenance["video"] = "comfy:ltx25"
    sb.scenes[0].assets.video = "03_video/s1_v01.mp4"
    sb.scenes[0].checks.video = "skipped"
    save_storyboard(episode, sb)
    m = load_manifest(episode)
    m.ledger = [_entry("s1", n, usd) for n, usd in ((1, 0.012), (2, 0.011), (3, 0.007))]
    save_manifest(episode, m)

    loaded = load_episodes(["t"])
    assert [(f.ref, f.manifest is not None) for f in loaded] == [("t/e1", True)]

    runner = CliRunner()
    table = runner.invoke(app, ["yield", "t"])
    assert table.exit_code == 0, table.output
    assert PROVIDER in table.output and "$0.0230" in table.output
    assert "причины не сохранены: 2" in table.output
    assert "без проверки: 1 из 1 принятых" in table.output  # строка клипа
    assert "сцен на заглушке (mock) — 1" in table.output

    as_json = runner.invoke(app, ["yield", "--json", "--kind", "image"])  # все проекты
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.output)
    assert [r["project"] for r in data["rows"]] == ["t"]
    assert data["rows"][0]["attempts"] == 3 and data["rows"][0]["rejected_usd"] == 0.023
    assert data["skipped"]["mock_scenes"] == 1

    empty = runner.invoke(app, ["yield", "t", "--since", "2030-01-01"])
    assert empty.exit_code == 0 and "строк нет" in empty.output
    assert "эпизодов раньше --since — 1" in empty.output

    assert runner.invoke(app, ["yield", "t", "--since", "вчера"]).exit_code == 1
    assert runner.invoke(app, ["yield", "t", "--kind", "audio"]).exit_code == 1
    assert runner.invoke(app, ["yield", "no_such_project"]).exit_code == 1
