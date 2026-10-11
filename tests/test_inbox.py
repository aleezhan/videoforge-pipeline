"""Входящие файлы: загрузка, раскладка по дням, скачивание, уборка."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videoforge.api import inbox
from videoforge.api.app import create_app


@pytest.fixture
def inbox_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный inbox/: файлы репозитория в тесты не тянутся."""
    root = tmp_path / "inbox"
    monkeypatch.setenv("VF_INBOX_DIR", str(root))
    return root


@pytest.fixture
def client(inbox_root: Path) -> TestClient:
    return TestClient(create_app(ui_dist=Path("nonexistent")))


def test_upload_lands_in_todays_folder(client: TestClient, inbox_root: Path) -> None:
    body = client.post("/api/inbox?name=письмо.pdf", content=b"%PDF-1.4 ...").json()
    day = inbox.today()
    assert body["path"] == f"inbox/{day}/письмо.pdf" and body["kind"] == "other"
    assert (inbox_root / day / "письмо.pdf").read_bytes() == b"%PDF-1.4 ..."

    days = client.get("/api/inbox").json()["days"]
    assert [d["day"] for d in days] == [day] and days[0]["title"] == "сегодня"
    assert days[0]["files"][0]["name"] == "письмо.pdf" and days[0]["size"] == 12


def test_second_upload_does_not_overwrite(client: TestClient) -> None:
    """Человек грузит второй раз, потому что не был уверен, что долетело.
    Молча заменить первый файл — потерять его."""
    first = client.post("/api/inbox?name=clip.mp4", content=b"aaa").json()
    second = client.post("/api/inbox?name=clip.mp4", content=b"bb").json()
    assert first["name"] == "clip.mp4" and second["name"] == "clip-2.mp4"
    assert client.get("/api/inbox").json()["total"] == 2


def test_name_from_phone_cannot_escape_the_folder(client: TestClient, inbox_root: Path) -> None:
    body = client.post("/api/inbox?name=../../secret.env", content=b"x").json()
    assert body["name"] == "secret.env"
    assert (inbox_root / inbox.today() / "secret.env").exists()
    assert not (inbox_root.parent / "secret.env").exists()


def test_empty_upload_is_not_a_file(client: TestClient, inbox_root: Path) -> None:
    """Обрыв на первом байте выглядит как успех — а файла нет."""
    assert client.post("/api/inbox?name=nothing.bin", content=b"").status_code == 400
    assert client.post("/api/inbox", content=b"x").status_code == 400
    assert client.get("/api/inbox").json()["total"] == 0
    assert list((inbox_root / inbox.today()).glob("*")) == []


def test_download_link_gives_the_file_as_attachment(client: TestClient) -> None:
    body = client.post("/api/inbox?name=note.txt", content=b"hello").json()
    view = client.get(body["url"])
    assert view.status_code == 200 and view.content == b"hello"
    assert "attachment" not in view.headers.get("content-disposition", "")
    saved = client.get(body["download_url"])
    assert saved.status_code == 200 and "attachment" in saved.headers["content-disposition"]


def test_delete_takes_the_empty_day_with_it(client: TestClient, inbox_root: Path) -> None:
    body = client.post("/api/inbox?name=note.txt", content=b"hello").json()
    resp = client.delete(f"/api/inbox/{body['day']}/{body['name']}")
    assert resp.status_code == 200 and resp.json()["deleted"] == body["path"]
    assert not (inbox_root / body["day"]).exists()
    assert client.delete(f"/api/inbox/{body['day']}/{body['name']}").status_code == 404


def test_files_put_in_by_hand_are_shown(client: TestClient, inbox_root: Path) -> None:
    """Положили проводником в корень — страница обязана показать: невидимый файл
    в папке обмена хуже лишней группы."""
    inbox_root.mkdir(parents=True, exist_ok=True)
    (inbox_root / "README.md").write_text("объяснение папки", encoding="utf-8")
    (inbox_root / "по-быстрому.png").write_bytes(b"\x89PNG")
    body = client.get("/api/inbox").json()
    assert body["total"] == 1 and body["days"][0]["title"] == "без даты"
    loose = body["days"][0]["files"][0]
    assert loose["name"] == "по-быстрому.png" and loose["day"] == "" and loose["kind"] == "image"
    assert client.delete(f"/api/inbox/-/{loose['name']}").status_code == 200
    assert (inbox_root / "README.md").exists()  # объяснение папки не удаляем


def test_days_are_named_and_sorted(client: TestClient, inbox_root: Path) -> None:
    today = datetime.now().date()
    for delta in (0, 1, 5):
        day = (today - timedelta(days=delta)).strftime(inbox.DAY_FMT)
        (inbox_root / day).mkdir(parents=True)
        (inbox_root / day / f"f{delta}.txt").write_text("x", encoding="utf-8")
    days = client.get("/api/inbox").json()["days"]
    assert [d["title"] for d in days][:2] == ["сегодня", "вчера"]
    assert (
        days[2]["title"] == inbox.day_title(days[2]["day"]) and "сентября" not in days[0]["title"]
    )
    assert [d["day"] for d in days] == sorted((d["day"] for d in days), reverse=True)


def test_upload_from_another_site_is_refused(client: TestClient) -> None:
    """Пульт слушает без авторизации: чужая страница в браузере не должна ни
    класть файлы, ни удалять их."""
    alien = {"origin": "http://evil.example", "referer": "http://evil.example/"}
    assert client.post("/api/inbox?name=x.txt", content=b"x", headers=alien).status_code == 403
    body = client.post("/api/inbox?name=x.txt", content=b"x").json()
    assert (
        client.delete(f"/api/inbox/{body['day']}/{body['name']}", headers=alien).status_code == 403
    )
