"""API интерфейса: те же папки, что у CLI, → JSON. Без сервера — TestClient."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videoforge.api import bench, data
from videoforge.api.app import create_app
from videoforge.core.accounts import StatsSnapshot, add_snapshot, create_account, load_account
from videoforge.core.manifest import now
from videoforge.core.project import ProjectSpec, create_project
from videoforge.core.resources import create_resource
from videoforge.core.storyboard import load_storyboard, save_storyboard


@pytest.fixture
def client(episode: Path, accounts_root: Path) -> TestClient:
    return TestClient(create_app(ui_dist=Path("nonexistent")))


def test_projects_status_follows_accounts(client: TestClient) -> None:
    items = client.get("/api/projects").json()["items"]
    assert [p["id"] for p in items] == ["t"]
    assert items[0]["status"] == "demo" and items[0]["episodes"] == 1
    assert items[0]["platform_titles"] == ["YouTube Shorts"]

    create_account("t_yt", network="youtube", project="t", handle="@t")
    items = client.get("/api/projects").json()["items"]
    assert items[0]["status"] == "live" and items[0]["accounts"][0]["id"] == "t_yt"


def test_project_detail(client: TestClient) -> None:
    body = client.get("/api/projects/t").json()
    assert body["name"] == "Тест" and body["workflow"] == "ai_video"
    slots = {r["slot"]: r for r in body["resources"]}
    assert slots["character"]["id"] == "timur" and slots["character"]["missing"] is False
    assert slots["character"]["kind"] == "characters"
    assert body["episodes"][0]["id"] == "e1" and body["episodes"][0]["scenes"] == 2
    stages = body["episodes"][0]["stages"]
    assert stages[0]["name"] == "director" and stages[0]["gate_after"] is True
    assert stages[0]["status"] == "pending"
    assert client.get("/api/projects/nope").status_code == 404


def test_episode_detail(client: TestClient) -> None:
    body = client.get("/api/projects/t/episodes/e1").json()
    assert body["title"] == "проба" and len(body["scenes"]) == 2
    assert body["scenes"][0]["id"] == "s1" and body["scenes"][0]["assets"]["image"] is None
    assert [c["id"] for c in body["cast"]] == ["timur"]
    assert body["duration_sec"] == 12.0  # две сцены по умолчанию, финала ещё нет
    assert body["final_checks"] == [] and body["qa"] is None
    assert body["report_url"].endswith("/report")
    assert client.get("/api/projects/t/episodes/zzz").status_code == 404


def test_episode_report_redirects_to_file(client: TestClient) -> None:
    resp = client.get("/api/projects/t/episodes/e1/report", follow_redirects=False)
    assert resp.status_code == 307
    # у медиа-ссылок в хвосте отпечаток файла (?v=…) — он и обновляет кэш браузера
    assert resp.headers["location"].split("?")[0] == "/files/projects/t/episodes/e1/report.html"
    page = client.get(resp.headers["location"])
    assert page.status_code == 200 and "<html" in page.text.lower()


def test_resources(client: TestClient) -> None:
    kinds = {k["kind"]: k for k in client.get("/api/resources").json()["kinds"]}
    assert kinds["characters"]["count_global"] >= 1 and kinds["characters"]["visual"] is True
    items = client.get("/api/resources/characters").json()["items"]
    assert any(i["id"] == "timur" and i["scope"] == "global" for i in items)
    detail = client.get("/api/resources/characters/timur").json()
    assert detail["fields"]["id"] == "timur" and isinstance(detail["portfolio"], list)
    assert {"project": "t", "where": "project.yaml: character", "episode": ""} in detail["used_by"]
    assert client.get("/api/resources/robots").status_code == 404
    assert client.get("/api/resources/characters/nobody").status_code == 404


def test_project_local_resource(client: TestClient, projects_root: Path) -> None:
    local = projects_root / "t" / "resources" / "props"
    local.mkdir(parents=True)
    (local / "compass.yaml").write_text("id: compass\nname: Компас\n", encoding="utf-8")
    body = client.get("/api/projects/t").json()
    assert [r["id"] for r in body["local_resources"]] == ["compass"]
    assert body["local_resources"][0]["scope"] == "project"
    items = client.get("/api/resources/props").json()["items"]
    assert any(i["id"] == "compass" and i["project"] == "t" for i in items)
    detail = client.get("/api/resources/props/compass", params={"project": "t"}).json()
    assert detail["name"] == "Компас"


def test_project_resources_say_own_or_shared_and_where_used(
    client: TestClient, episode: Path, projects_root: Path
) -> None:
    """Свой или общий — и где используется. Общий голос и свой персонаж в слоте
    project.yaml выглядели одинаково «ресурсами проекта», а правка общего
    меняет все проекты, где он стоит: у общего — другие проекты, у каста —
    эпизоды со счётом сцен, у своего — пустой список других."""
    # ещё два проекта на тот же общий персонаж: u берёт общего, w держит СВОЕГО с тем же id
    for pid in ("u", "w"):
        create_project(
            ProjectSpec(
                id=pid,
                name=pid,
                platforms=["youtube_shorts"],
                character="timur",
                style="pixar3d",
                voice="ru_male",
            )
        )
    own = projects_root / "w" / "resources" / "characters"
    own.mkdir(parents=True)
    (own / "timur.yaml").write_text("id: timur\nname: Свой Тимур\n", encoding="utf-8")
    # свой предмет проекта t в касте одной сцены; персонаж — в касте двух
    local = projects_root / "t" / "resources" / "props"
    local.mkdir(parents=True)
    (local / "compass.yaml").write_text("id: compass\nname: Компас\n", encoding="utf-8")
    sb = load_storyboard(episode)
    sb.scenes[0].cast = ["characters/timur", "props/compass"]
    sb.scenes[1].cast = ["characters/timur"]
    save_storyboard(episode, sb)

    slots = {r["slot"]: r for r in client.get("/api/projects/t").json()["resources"]}
    character = slots["character"]
    assert character["scope"] == "global"
    assert character["shared_with"] == ["u"]  # w со своим timur общим не пользуется
    assert character["used_in"] == [{"episode": "e1", "scenes": 2}]
    voice = slots["voice"]
    assert voice["scope"] == "global" and voice["shared_with"] == ["u", "w"]
    assert "used_in" not in voice  # голос в касте не бывает: его место — слот

    compass = client.get("/api/projects/t").json()["local_resources"][0]
    assert compass["scope"] == "project" and compass["shared_with"] == []
    assert compass["used_in"] == [{"episode": "e1", "scenes": 1}]

    w = {r["slot"]: r for r in client.get("/api/projects/w").json()["resources"]}
    assert w["character"]["scope"] == "project" and w["character"]["shared_with"] == []
    assert w["character"]["used_in"] == []  # заведён, в кадре не был


def test_resource_rename(
    projects_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кнопка «Переименовать» в пульте — та же функция, что у CLI, тот же файл."""
    monkeypatch.setenv("VF_RESOURCES_DIR", str(tmp_path / "res"))
    create_resource("voice", "ava", name="Ава")
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    body = client.patch("/api/resources/voices/ava", json={"name": "Рассказчик шортсов"})
    assert body.status_code == 200 and body.json()["name"] == "Рассказчик шортсов"
    assert client.get("/api/resources/voices/ava").json()["name"] == "Рассказчик шортсов"
    assert client.patch("/api/resources/voices/ava", json={"name": " "}).status_code == 422
    assert client.patch("/api/resources/voices/nobody", json={"name": "Икс"}).status_code == 404
    # чужая страница в браузере переименовать не может
    cross = client.patch(
        "/api/resources/voices/ava",
        json={"name": "Взломано"},
        headers={"Origin": "http://evil.example"},
    )
    assert cross.status_code == 403
    assert client.get("/api/resources/voices/ava").json()["name"] == "Рассказчик шортсов"


def test_library(client: TestClient) -> None:
    body = client.get("/api/library").json()
    ids = [d["id"] for d in body["decisions"]]
    assert "captions" in ids and body["total"] >= 1
    group = client.get("/api/library/captions").json()
    assert any(t["id"] == "social_captions" for t in group["items"])
    t = client.get("/api/library/captions/social_captions").json()
    assert t["status"] == "implemented" and t["problems"] == []
    assert "example" in t
    assert client.get("/api/library/transitions").status_code == 404


def test_accounts(client: TestClient) -> None:
    assert client.get("/api/accounts").json()["items"] == []
    create_account("t_yt", network="youtube", project="t", handle="@t")
    acc = load_account("t_yt")
    add_snapshot(acc, StatsSnapshot(at=now(), source="mock", subscribers=40, video_count=3))
    add_snapshot(acc, StatsSnapshot(at=now(), source="manual", subscribers=42))
    items = client.get("/api/accounts").json()["items"]
    assert items[0]["latest"]["subscribers"] == 42 and items[0]["project_name"] == "Тест"
    # ручной замер без числа роликов не стирает число, снятое раньше
    assert items[0]["latest"]["video_count"] is None
    assert items[0]["known"]["video_count"]["value"] == 3
    assert items[0]["known"]["subscribers"]["value"] == 42
    detail = client.get("/api/accounts/t_yt").json()
    assert len(detail["history"]) == 2 and detail["published"] == [] and detail["problems"] == []
    assert detail["avatar_url"] is None and detail["days_since_publish"] is None
    assert detail["known"]["likes_total"] is None
    assert client.get("/api/accounts/none").status_code == 404


def test_bench_shows_folder_as_is(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bench"
    monkeypatch.setenv("VF_BENCH_DIR", str(root))
    assert client.get("/api/bench").json()["exists"] is False

    (root / "out" / "test1" / "sub").mkdir(parents=True)
    (root / "README.md").write_text("# полигон\n", encoding="utf-8")
    (root / "LOG.md").write_text("# журнал\n", encoding="utf-8")
    (root / "runs.csv").write_text(
        "ts,test,endpoint,status,wall_s,cost_usd\n"
        "2026-09-07T10:00:00Z,test1,z-image-turbo,COMPLETED,4.0,0.01\n"
        "2026-09-07T11:00:00Z,test1,z-image-turbo,FAILED,,0\n"
        "2026-09-08T10:00:00Z,test2,seedance,COMPLETED,10.0,0.5\n",
        encoding="utf-8",
    )
    # карточка теста: вопрос, ответ и карта — то, чего в runs.csv нет
    (root / "tests.yaml").write_text(
        "test1:\n"
        "  question: LoRA против hosted\n"
        "  answer: klein держит лицо\n"
        "  models: [klein 4B + LoRA]\n"
        "  gpu: {name: RTX PRO 4500, pod: vf-bench-lora, minutes: 10, cost_usd: 0.12}\n"
        "test9:\n"
        "  question: папки нет — карточка без папки в списке не появляется\n"
        "  gpu: {minutes: 5, cost_usd: 0.06}\n",
        encoding="utf-8",
    )
    (root / "out" / "test1" / "summary.md").write_text("итог", encoding="utf-8")
    (root / "out" / "test1" / "sheet.png").write_bytes(b"\x89PNG")
    (root / "out" / "test1" / "sub" / "clip.mp4").write_bytes(b"\x00")
    (root / "out" / "test1" / "measures.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "out" / "report.html").write_text("<html></html>", encoding="utf-8")

    body = client.get("/api/bench").json()
    assert body["exists"] is True
    assert [d["name"] for d in body["docs"]] == ["README.md", "LOG.md"]  # карта, потом журнал
    assert body["tables"][0]["name"] == "runs.csv" and body["tables"][0]["rows"] == 3
    assert [t["id"] for t in body["tests"]] == ["test1"]
    assert body["tests"][0]["counts"] == {"doc": 1, "image": 1, "video": 1, "table": 1}
    runs = {r["test"]: r for r in body["runs"]}
    assert runs["test1"]["calls"] == 2 and runs["test1"]["completed"] == 1
    assert runs["test1"]["cost_usd"] == 0.01 and runs["test1"]["wall_mean_s"] == 4.0
    assert runs["test1"]["endpoints"] == [{"id": "z-image-turbo", "calls": 2, "cost_usd": 0.01}]
    card = body["tests"][0]
    assert card["meta"]["question"] == "LoRA против hosted" and card["meta"]["gpu"]["minutes"] == 10
    assert card["hosted"]["calls"] == 2 and card["hosted"]["wall_s"] == 4.0
    # деньги полигона = hosted из runs.csv + карта из всех карточек (test9 тоже
    # считается: под поднимался, даже если папки с выходами нет)
    assert body["totals"] == {
        "calls": 3,
        "hosted_usd": 0.51,
        "gpu_minutes": 15.0,
        "gpu_usd": 0.18,
        "bench_usd": 0.0,  # замеров по схеме (bench.yaml) в этой папке нет
        "cost_usd": 0.69,
        "tests": 1,
        "benches": 0,
    }
    # во временной папке полигон лежит внутри projects/ — ссылка идёт через тот
    # корень, что нашёлся первым; важно, что она есть и ведёт на файл
    report_url = body["reports"][0]["url"]
    assert report_url == data.file_url(root / "out" / "report.html")
    assert report_url.split("?")[0].endswith("/out/report.html")
    assert client.get(report_url).status_code == 200

    test = client.get("/api/bench/tests/test1").json()
    dirs = [g["dir"] for g in test["groups"]]
    assert dirs == ["", "sub"]
    assert test["groups"][0]["docs"][0]["text"] == "итог"
    assert test["groups"][0]["tables"][0]["table"]["rows"] == [["1", "2"]]
    clip_url = test["groups"][1]["videos"][0]["url"]
    assert clip_url.split("?")[0].endswith("/out/test1/sub/clip.mp4")
    assert client.get(clip_url).status_code == 200
    assert client.get("/api/bench/tests/nope").status_code == 404
    # «..» в URL клиент нормализует сам, до сервера не доходит — проверяем функцию
    with pytest.raises(data.NotFound):
        bench.bench_test("..")
    with pytest.raises(data.NotFound):
        bench.bench_test("../test1")

    text = client.get("/api/bench/text/LOG.md").json()
    assert text["kind"] == "doc" and text["text"] == "# журнал\n"
    table = client.get("/api/bench/text/runs.csv").json()
    assert table["table"]["columns"][:2] == ["ts", "test"] and table["table"]["total"] == 3
    assert client.get("/api/bench/text/out/test1/sheet.png").status_code == 404
    assert client.get("/api/bench/text/../pyproject.toml").status_code == 404
    # явный корень bench нужен, когда полигон лежит вне projects/
    assert client.get("/files/bench/out/test1/sheet.png").status_code == 200


def test_project_sources_folder_and_clips(client: TestClient, projects_root: Path) -> None:
    from videoforge.clips.source import SourceInfo, save_source

    material = projects_root / "t" / "sources" / "rezero" / "edits"
    material.mkdir(parents=True)
    (material.parent / "README.md").write_text("# материал\n", encoding="utf-8")
    (material / "e14.mp4").write_bytes(b"\x00")
    clips = projects_root / "t" / "sources" / "vid1"
    clips.mkdir()
    save_source(
        clips, SourceInfo(id="vid1", url="https://youtu.be/x", title="Подкаст", duration_sec=90)
    )

    body = client.get("/api/projects/t").json()
    assert body["workflow_exists"] is True
    rows = {s["id"]: s for s in body["sources"]}
    folder = rows["rezero"]
    assert folder["kind"] == "folder" and folder["readme_md"].startswith("# материал")
    file_row = folder["groups"][0]["files"][0]
    assert folder["groups"] == [
        {
            "dir": "edits",
            "files": [
                {
                    "name": "e14.mp4",
                    # хвост ?v=<отпечаток> у ссылки не проверяем — он меняется с файлом
                    "url": file_row["url"],
                    "kind": "video",
                    "size": 1,
                    "mtime": file_row["mtime"],
                }
            ],
        }
    ]
    assert file_row["url"].split("?")[0] == "/files/projects/t/sources/rezero/edits/e14.mp4"
    vid = rows["vid1"]
    assert vid["kind"] == "clips" and vid["title"] == "Подкаст" and vid["duration_sec"] == 90
    assert vid["highlights"] is None and vid["word_count"] == 0


def test_projects_list_other_folders(client: TestClient, projects_root: Path) -> None:
    (projects_root / "_bench").mkdir()
    (projects_root / "loose_material").mkdir()
    body = client.get("/api/projects").json()
    assert body["other_folders"] == ["loose_material"] and body["bench_folder"] == "_bench"


def test_projects_hidden_sample_is_not_listed(client: TestClient, projects_root: Path) -> None:
    """Образец (hidden: true) не попадает в список, но остаётся открываемым:
    demo нужен тестам и мок-прогону, а в пульте выглядел как ещё один канал."""
    from videoforge.core.project import ProjectSpec, create_project

    create_project(ProjectSpec(id="sample", name="Образец"))
    cfg = projects_root / "sample" / "project.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\nhidden: true\n", encoding="utf-8")

    body = client.get("/api/projects").json()
    assert [p["id"] for p in body["items"]] == ["t"]
    assert body["hidden"] == ["sample"]
    assert client.get("/api/projects/sample").status_code == 200


def test_files_are_confined_to_roots(client: TestClient, projects_root: Path) -> None:
    ok = client.get("/files/projects/t/episodes/e1/storyboard.json")
    assert ok.status_code == 200 and ok.json()["episode_id"] == "e1"
    assert client.get("/files/projects/../pyproject.toml").status_code == 404
    assert client.get("/files/etc/passwd").status_code == 404
    assert client.get("/files/projects/t/nothing.json").status_code == 404
    url = data.file_url(projects_root / "t" / "project.yaml")
    assert url is not None and url.split("?")[0] == "/files/projects/t/project.yaml"
    assert data.file_url(Path("C:/") / "elsewhere.txt") is None


def test_upload_state_separates_packaged_from_posted(episode: Path) -> None:
    """«Упаковано» и «выложено» — разные вещи, и пульт обязан их различать.

    Кружок стадии publish горел зелёным у ролика, которого на площадке нет:
    стадия пишет заголовок и описание, а не грузит. И отдельно — видимость:
    загруженный приватно лежит на канале, и его не видит никто."""
    from videoforge.api.data import episode_summary
    from videoforge.core.manifest import (
        PlatformPublish,
        PublishRecord,
        load_manifest,
        save_manifest,
    )

    assert episode_summary("t", "e1")["upload"] is None  # не грузили — и говорить нечего

    m = load_manifest(episode)
    m.publish = PublishRecord(
        state="done",
        platforms={"youtube": PlatformPublish(video_id="x", url="u", privacy="private")},
    )
    save_manifest(episode, m)

    up = episode_summary("t", "e1")["upload"]
    assert up["state"] == "done" and up["privacy"] == "private"
    assert [p["network"] for p in up["platforms"]] == ["youtube"]


def test_schedule_is_edited_from_the_pult_and_the_plan_follows(client: TestClient) -> None:
    """Слоты правятся кнопками — тем же файлом, что `videoforge queue --times`;
    план и запас пересчитываются на каждый запрос; аккаунт видит запас проекта."""
    from videoforge.core.queue import queue_path
    from videoforge.paths import episode_dir

    edir = episode_dir("t", "e1")
    (edir / "05_assemble").mkdir(exist_ok=True)
    (edir / "05_assemble" / "final.mp4").write_bytes(b"")
    (edir / "06_publish").mkdir(exist_ok=True)
    (edir / "06_publish" / "publish.json").write_text("{}", encoding="utf-8")

    view = client.get("/api/projects/t").json()["queue"]
    assert view["schedule"]["times"] == [] and view["plan"] == [] and view["live"] is False
    assert view["items"][0]["state"] == "ready" and view["notices"] == []  # демо без слотов молчит

    resp = client.patch(
        "/api/projects/t/schedule", json={"times": ["18:00", "12:00"], "paused": False}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["schedule"]["times"] == ["12:00", "18:00"] and body["runway"]["per_day"] == 2
    assert body["plan"][0]["episode"] == "e1" and body["plan"][1]["episode"] is None
    assert body["runway"]["ready"] == 1 and body["runway"]["level"] == "warn"
    assert "schedule:" in queue_path("t").read_text(encoding="utf-8")
    assert client.get("/api/projects/t/queue").json()["schedule"] == body["schedule"]

    assert client.patch("/api/projects/t/schedule", json={"times": ["25:00"]}).status_code == 400
    assert client.patch("/api/projects/nope/schedule", json={"times": []}).status_code == 404
    assert (
        client.patch(
            "/api/projects/t/schedule",
            json={"times": []},
            headers={"Origin": "http://evil.example"},
        ).status_code
        == 403
    )

    create_account("t_yt", network="youtube", project="t", handle="@t")
    item = client.get("/api/accounts").json()["items"][0]
    assert item["runway"]["ready"] == 1 and item["runway"]["per_day"] == 2
    # у аккаунта — то же расписание проекта: «когда следующий» спрашивают у канала
    detail = client.get("/api/accounts/t_yt").json()
    assert detail["queue"]["schedule"]["times"] == ["12:00", "18:00"]
    assert detail["queue"]["plan"][0]["episode"] == "e1" and detail["queue"]["live"] is True
    # живой проект без тика планировщика — предупреждение, что план никто не выполнит
    notes = [n["text"] for n in client.get("/api/projects/t/queue").json()["notices"]]
    assert any("не тикал" in n for n in notes)


def test_todo_tasks_keep_their_code_after_closing(tmp_path: Path) -> None:
    """Код задачи живёт с ней до конца и не переиспользуется.

    По коду задачу называют вслух («делаем T-07»), поэтому номер закрытой не
    должен достаться новой: через неделю это была бы уже другая работа."""
    from videoforge.core.todo import add_task, close_task, find_task, load_tasks, next_code

    first = add_task("войти в кабинет", tag="podcast_clips", root=tmp_path)
    second = add_task("нажать кнопку", root=tmp_path)
    assert (first.code, second.code) == ("T-01", "T-02")

    assert close_task("1", root=tmp_path) is not None  # «1» вместо «T-01» — тоже код
    assert find_task("T-01", root=tmp_path).done  # type: ignore[union-attr]
    assert next_code(tmp_path) == "T-03"  # номер закрытой не переиспользуется

    # файл остаётся markdown'ом, который человек правит руками
    text = (tmp_path / "todo.md").read_text(encoding="utf-8")
    assert "- [x] T-01 · войти в кабинет · podcast_clips · сделано" in text
    assert "- [ ] T-02 · нажать кнопку" in text
    assert [t.code for t in load_tasks(tmp_path)] == ["T-02", "T-01"]  # ждущие сверху


def test_todo_button_closes_the_task(client: TestClient, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Кнопка в пульте зовёт ту же функцию, что CLI, и не пускает чужую страницу."""
    import videoforge.paths as paths
    from videoforge.core.todo import add_task

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr("videoforge.core.todo.ROOT", tmp_path)
    task = add_task("посмотреть ролик", root=tmp_path)

    assert client.get("/api/todo").json()["open"][0]["code"] == task.code
    assert client.post(f"/api/todo/{task.code}/done").json()["done"] is True
    assert client.get("/api/todo").json()["open"] == []
    # чужая страница нажать не может
    assert (
        client.post(
            f"/api/todo/{task.code}/done", headers={"Origin": "http://evil.example"}
        ).status_code
        == 403
    )
    assert client.post("/api/todo/T-99/done").status_code == 404


def test_index_is_revalidated_but_assets_are_not(tmp_path: Path) -> None:
    """Страница пульта сверяется с сервером, файлы сборки — нет.

    Имя файла сборки содержит отпечаток, index.html — нет. Браузер, оставивший
    у себя старый index.html, грузит прошлую сборку: правка есть на диске, а на
    телефоне её нет. Поэтому index.html уходит с no-cache («спроси»), а не
    молча живёт в памяти телефона."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>пульт</body></html>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("// сборка", encoding="utf-8")
    client = TestClient(create_app(ui_dist=dist))

    for url in ("/", "/projects/podcast_clips"):  # корень и маршрут SPA
        page = client.get(url)
        assert page.status_code == 200 and "пульт" in page.text
        assert page.headers["cache-control"] == "no-cache"

    asset = client.get("/assets/index-abc123.js")
    assert asset.status_code == 200 and "cache-control" not in asset.headers
    assert client.get("/api/nothing").status_code == 404  # API остаётся честным
