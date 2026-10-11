"""Задачи человека (todo.md): связь задачи с записью в features/.

Строка задачи, её код и кнопка в пульте проверены в test_api.py. Здесь — то,
что появилось после 12.09.2026, когда три записи «в работе» оказались давно
сделанными: часть строки `features/<id>` как связь, подсказка при закрытии и
её показ в пульте. Файл задач репозитория тесты не трогают — только копию."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from videoforge.api.app import create_app
from videoforge.cli import app
from videoforge.core.features import create_feature, load_feature, set_status
from videoforge.core.todo import _parse_line, add_task, close_task, feature_hint, load_tasks


@pytest.fixture
def todo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """todo.md во временной папке: CLI и пульт читают его через ROOT модуля."""
    monkeypatch.setattr("videoforge.core.todo.ROOT", tmp_path)
    return tmp_path


def test_link_is_a_whole_part_of_the_line_not_a_mention() -> None:
    """Связь — отдельная часть строки; `features/x` внутри текста — «см. там»."""
    linked = _parse_line("- [ ] T-07 · войти в TikTok · podcast_clips · features/tiktok_upload")
    assert linked is not None
    assert linked.feature == "tiktok_upload" and linked.tag == "podcast_clips"
    assert linked.line == "- [ ] T-07 · войти в TikTok · podcast_clips · features/tiktok_upload"

    # без метки: связь не занимает место метки
    bare = _parse_line("- [ ] T-08 · текст · feature:tiktok_upload")
    assert bare is not None and bare.feature == "tiktok_upload" and bare.tag == ""
    assert bare.line == "- [ ] T-08 · текст · features/tiktok_upload"

    # упоминание в тексте — не связь: так пишут «см. запись», а не «закрываю её»
    mention = _parse_line("- [ ] T-09 · да → делаем features/multi_channel_probe · podcast_clips")
    assert mention is not None and mention.feature == "" and mention.tag == "podcast_clips"

    # закрытая строка с датой переживает разбор и запись без перестановок
    done = _parse_line("- [x] T-05 · шаг · gpu · features/lora_in_fingerprint · сделано 2026-09-12")
    assert done is not None and done.done and done.feature == "lora_in_fingerprint"
    assert done.done_at == "2026-09-12"
    assert done.line == "- [x] T-05 · шаг · gpu · features/lora_in_fingerprint · сделано 2026-09-12"


def test_add_checks_that_the_record_exists(features_root: Path, todo_root: Path) -> None:
    """Опечатка в id ловится при записи, а не на done, когда подсказка указала бы в никуда."""
    with pytest.raises(ValueError, match="feature list"):
        add_task("нажать кнопку", feature="no_such_record", root=todo_root)
    assert load_tasks(todo_root) == []

    create_feature("tiktok_upload", title="Загрузка в TikTok", area="publish", summary="…")
    task = add_task(
        "войти в TikTok", tag="podcast_clips", feature="features/tiktok_upload", root=todo_root
    )
    assert task.feature == "tiktok_upload"  # префикс из строки принимается, хранится id
    text = (todo_root / "todo.md").read_text(encoding="utf-8")
    assert "- [ ] T-01 · войти в TikTok · podcast_clips · features/tiktok_upload" in text
    assert load_tasks(todo_root)[0].feature == "tiktok_upload"


def test_done_names_the_record_that_waited(features_root: Path, todo_root: Path) -> None:
    """`todo done` называет запись и предлагает закрыть её — сам не трогает."""
    create_feature(
        "tiktok_upload",
        title="Загрузка в TikTok",
        area="publish",
        status="in_progress",
        summary="…",
    )
    runner = CliRunner()
    added = runner.invoke(app, ["todo", "add", "войти в TikTok", "--feature", "tiktok_upload"])
    assert added.exit_code == 0 and "→ features/tiktok_upload" in added.output
    bad = runner.invoke(app, ["todo", "add", "x", "--feature", "nope"])
    assert bad.exit_code == 1 and "feature list" in bad.output
    add_task("посмотреть ролик", root=todo_root)

    listed = runner.invoke(app, ["todo"])
    assert "T-01" in listed.output and "→ features/tiktok_upload" in listed.output

    result = runner.invoke(app, ["todo", "done", "1"])
    assert result.exit_code == 0, result.output
    assert "features/tiktok_upload (Загрузка в TikTok, статус «в работе»)" in result.output
    assert "videoforge feature status tiktok_upload done" in result.output
    assert load_feature("tiktok_upload").status == "in_progress"  # решает человек

    plain = runner.invoke(app, ["todo", "done", "2"])
    assert plain.exit_code == 0 and "features/" not in plain.output  # без связи — молчит

    # снова открыли — запись опять ждёт; запись уже done — другой совет
    assert "опять ждёт" in feature_hint(close_task("1", root=todo_root, done=False))  # type: ignore[arg-type]
    set_status("tiktok_upload", "done")
    assert "уже закрыта" in feature_hint(close_task("1", root=todo_root))  # type: ignore[arg-type]
    # запись удалили — связь висит в воздухе, и об этом говорится прямо
    (features_root / "tiktok_upload.yaml").unlink()
    assert "такой записи нет" in feature_hint(close_task("1", root=todo_root))  # type: ignore[arg-type]


def test_pult_shows_the_link_and_returns_the_hint(features_root: Path, todo_root: Path) -> None:
    """Пульт отдаёт связь словами (заголовок записи) и ту же подсказку, что CLI."""
    create_feature("tiktok_upload", title="Загрузка в TikTok", area="publish", summary="…")
    add_task("войти в TikTok", feature="tiktok_upload", root=todo_root)
    add_task("посмотреть ролик", root=todo_root)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))

    rows = {r["code"]: r for r in client.get("/api/todo").json()["open"]}
    assert rows["T-01"]["feature"] == "tiktok_upload"
    assert rows["T-01"]["feature_title"] == "Загрузка в TikTok"
    assert rows["T-02"]["feature"] == "" and rows["T-02"]["feature_title"] == ""

    closed = client.post("/api/todo/T-01/done").json()
    assert closed["done"] is True and closed["feature"] == "tiktok_upload"
    assert "videoforge feature status tiktok_upload done" in closed["hint"]
    assert client.post("/api/todo/T-02/done").json()["hint"] == ""

    # записи не стало — ссылка остаётся, заголовок пустой, страница не падает
    (features_root / "tiktok_upload.yaml").unlink()
    done = {r["code"]: r for r in client.get("/api/todo").json()["done"]}
    assert done["T-01"]["feature"] == "tiktok_upload" and done["T-01"]["feature_title"] == ""
