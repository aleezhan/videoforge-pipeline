"""Правка плана монтажа из пульта: три запрета, на которых всё держится.

Первая запись из браузера — это три новых способа испортить работу: поменять
не то, затереть чужую правку, записать то, что сборка не переварит. Проверяется
каждый, потому что цена ошибки здесь — готовый ролик, а не строка в интерфейсе.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_test_video
from fastapi.testclient import TestClient

from videoforge.api.app import create_app
from videoforge.api.planedit import fingerprint
from videoforge.core.editplan import EditPlan, MusicPlan, SceneEdit, load_plan, save_plan
from videoforge.core.storyboard import load_storyboard, save_storyboard

URL = "/api/projects/t/episodes/e1/plan"


def _plan(episode: Path) -> EditPlan:
    """Эпизод с материалом и план монтажа, какой пишет стадия edit.

    Клипы настоящие: линт меряет их длину, и на пустых путях он честно
    блокирует правку — как и должен."""
    clips = episode / "03_video"
    clips.mkdir(exist_ok=True)
    sb = load_storyboard(episode)
    for scene in sb.ordered_scenes():
        make_test_video(clips / f"{scene.id}.mp4", seconds=scene.duration_sec, size="256x256")
        scene.assets.video = f"03_video/{scene.id}.mp4"
    save_storyboard(episode, sb)
    plan = EditPlan(
        author="llm",
        llm="mock",
        scenes=[SceneEdit(scene_id=s.id) for s in sb.ordered_scenes()],
        music=MusicPlan(enabled=True, gain_db=-18.0),
    )
    save_plan(episode, plan)
    return plan


def _client() -> TestClient:
    return TestClient(create_app(ui_dist=Path("nonexistent")))


def test_pult_reads_the_plan_with_its_fingerprint(episode: Path) -> None:
    _plan(episode)
    body = _client().get(URL).json()
    assert body["exists"] and body["plan"]["author"] == "llm"
    assert body["fingerprint"] == fingerprint(load_plan(episode))
    assert "music" in body["editable"] and "scenes[].transition_in" in body["editable"]


def test_allowed_edit_lands_in_the_file_and_becomes_manual(episode: Path) -> None:
    """Правка человека побеждает модель: план становится ручным, и стадия edit
    его больше не перепишет. Прежний план ложится рядом копией."""
    _plan(episode)
    client = _client()
    body = client.get(URL).json()
    plan = body["plan"]
    plan["music"]["gain_db"] = -14.0
    # переход ставим во ВТОРУЮ сцену: в первую переходить неоткуда, и линт
    # такой переход снимает — это его работа, а не ошибка правки
    plan["scenes"][1]["transition_in"] = {"type": "crossfade", "duration_sec": 0.4}

    resp = client.patch(URL, json={"plan": plan, "base_fingerprint": body["fingerprint"]})
    assert resp.status_code == 200, resp.text
    saved = load_plan(episode)
    assert saved is not None
    assert saved.music is not None and saved.music.gain_db == -14.0
    assert saved.scenes[1].transition_in.type == "crossfade"
    assert saved.author == "manual"
    assert resp.json()["backup"] and (episode / "05_edit" / resp.json()["backup"]).exists()


def test_the_lock_is_a_field_of_its_own_and_can_be_taken_off(episode: Path) -> None:
    """«Кто написал план» и «можно ли его трогать» — разные вопросы.

    Пока на оба отвечало одно поле `author`, первая же правка мышкой навсегда
    выключала монтажёра-модель, и снять это можно было только правкой файла
    руками. Теперь замок — отдельное поле, он виден в ответе и снимается той же
    правкой."""
    _plan(episode)
    client = _client()
    body = client.get(URL).json()
    plan = body["plan"]
    plan["music"]["gain_db"] = -13.0
    resp = client.patch(URL, json={"plan": plan, "base_fingerprint": body["fingerprint"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pinned"] is True  # правка без явного решения закрепляет
    saved = load_plan(episode)
    assert saved is not None and saved.pinned and saved.author == "manual"

    body = client.get(URL).json()
    plan = body["plan"]
    plan["pinned"] = False  # передумали: пусть модель снова считает монтаж
    resp = client.patch(URL, json={"plan": plan, "base_fingerprint": body["fingerprint"]})
    assert resp.status_code == 200, resp.text
    reopened = load_plan(episode)
    assert reopened is not None and not reopened.pinned


def test_timeline_edits_land_in_the_file(episode: Path) -> None:
    """Эффекты, звуки и замедленный хвост правятся из пульта — они живут на
    таймлайне ролика, и попасть эффектом в склейку можно только глядя в плеер."""
    _plan(episode)
    client = _client()
    body = client.get(URL).json()
    sounds = body["sfx"]
    assert sounds and body["effect_kinds"][0] == "zoom_punch"
    plan = body["plan"]
    plan["effects"] = [{"at_sec": 1.2, "duration_sec": 0.4, "kind": "flash", "why": "склейка"}]
    plan["sfx"] = [{"at_sec": 1.2, "id": sounds[0]["id"], "why": "удар"}]
    plan["outro"] = {"take_sec": 2.0, "speed": 0.5, "pitch_drop": True}

    resp = client.patch(URL, json={"plan": plan, "base_fingerprint": body["fingerprint"]})
    assert resp.status_code == 200, resp.text
    saved = load_plan(episode)
    assert saved is not None
    assert [e.kind for e in saved.effects] == ["flash"]
    assert [s.id for s in saved.sfx] == [sounds[0]["id"]]
    assert saved.outro is not None and saved.outro.take_sec == 2.0


def test_the_form_gets_the_sounds_the_lint_will_accept(episode: Path) -> None:
    """Звук выбирают из списка, а не набирают: неизвестный id линт БЛОКИРУЕТ, то
    есть опечатка отвергает всю правку. Список приходит оттуда же, откуда судит
    линт, и с ссылкой на файл — «choir_hit» на слух не звучит."""
    _plan(episode)
    client = _client()
    sounds = client.get(URL).json()["sfx"]
    assert all(s["id"] and s["url"] for s in sounds)

    body = client.get(URL).json()
    plan = body["plan"]
    plan["sfx"] = [{"at_sec": 1.0, "id": "нет-такого-звука"}]
    resp = client.patch(URL, json={"plan": plan, "base_fingerprint": body["fingerprint"]})
    assert resp.status_code == 422 and "не найден" in resp.json()["detail"]
    assert load_plan(episode).sfx == []  # type: ignore[union-attr]


def test_forbidden_field_is_refused_whole(episode: Path) -> None:
    """Белый список — не совет: правка с чужим полем не принимается целиком, а
    не «частично применяется». Автор плана в белый список не входит: подделать
    `author: manual` из браузера значило бы навсегда выключить монтажёра-модель."""
    _plan(episode)
    client = _client()
    body = client.get(URL).json()
    plan = body["plan"]
    plan["author"] = "manual"
    plan["version"] = 99

    resp = client.patch(URL, json={"plan": plan, "base_fingerprint": body["fingerprint"]})
    assert resp.status_code == 422
    assert "version" in resp.json()["detail"]
    assert load_plan(episode).author == "llm"  # type: ignore[union-attr]


def test_stale_fingerprint_does_not_overwrite_someone_elses_edit(episode: Path) -> None:
    """Пока человек смотрел на экран, план поменяли (стадия, CLI, другая
    вкладка). Записать поверх — потерять ту правку молча."""
    _plan(episode)
    client = _client()
    body = client.get(URL).json()

    other = load_plan(episode)
    assert other is not None
    other.notes = "правка мимо пульта"
    save_plan(episode, other)

    resp = client.patch(URL, json={"plan": body["plan"], "base_fingerprint": body["fingerprint"]})
    assert resp.status_code == 409
    assert load_plan(episode).notes == "правка мимо пульта"  # type: ignore[union-attr]


def test_lint_is_the_same_judge_as_the_stage(episode: Path) -> None:
    """Браузер не может быть более добрым судьёй: несуществующий стиль субтитров
    линт блокирует и здесь, ровно как на стадии."""
    _plan(episode)
    client = _client()
    body = client.get(URL).json()
    plan = body["plan"]
    plan["caption_style"] = "нет-такого-стиля"

    resp = client.patch(URL, json={"plan": plan, "base_fingerprint": body["fingerprint"]})
    assert resp.status_code == 422 and "не найден" in resp.json()["detail"]
    assert load_plan(episode).caption_style is None  # type: ignore[union-attr]


def test_lint_fixes_come_back_to_the_human(episode: Path) -> None:
    """Починки линта не молчат: человек двинул громкость музыки за предел, её
    подтянули к границе — и об этом сказали, иначе он решит, что не сработало."""
    _plan(episode)
    client = _client()
    body = client.get(URL).json()
    plan = body["plan"]
    plan["music"]["gain_db"] = 30.0  # музыка громче голоса — так нельзя

    resp = client.patch(URL, json={"plan": plan, "base_fingerprint": body["fingerprint"]})
    assert resp.status_code == 200, resp.text
    fixes = resp.json()["fixes"]
    assert any("gain_db" in f for f in fixes), fixes
    saved = load_plan(episode)
    # музыка не может быть громче голоса: 30 дБ подтянуты к верхней границе
    assert saved is not None and saved.music is not None and saved.music.gain_db <= 0


def test_patch_from_another_site_is_refused(episode: Path) -> None:
    """Пульт слушает localhost без авторизации: чужая страница в браузере умеет
    послать на него запрос, и куки для этого не нужны."""
    _plan(episode)
    client = _client()
    body = client.get(URL).json()
    resp = client.patch(
        URL,
        json={"plan": body["plan"], "base_fingerprint": body["fingerprint"]},
        headers={"origin": "https://evil.example", "sec-fetch-site": "cross-site"},
    )
    assert resp.status_code == 403


def test_plan_edit_does_not_touch_the_video(episode: Path) -> None:
    """Правка плана — это файл, а не рендер: готовый ролик пересобирают отдельно,
    осознанным нажатием."""
    _plan(episode)
    out = episode / "05_assemble"
    out.mkdir(exist_ok=True)
    final = out / "final.mp4"
    final.write_bytes(b"not really a video")
    made = final.stat().st_mtime_ns

    client = _client()
    body = client.get(URL).json()
    plan = body["plan"]
    plan["fade_out_sec"] = 1.5
    resp = client.patch(URL, json={"plan": plan, "base_fingerprint": body["fingerprint"]})
    assert resp.status_code == 200, resp.text
    assert final.stat().st_mtime_ns == made
    assert (
        json.loads((episode / "05_edit" / "edit_plan.json").read_text("utf-8"))["fade_out_sec"]
        == 1.5
    )


@pytest.mark.render
def test_rebuild_runs_the_same_stage_as_the_cli(episode: Path, mock_providers: object) -> None:
    """Кнопка «Пересобрать» — это `videoforge run <ref> --only assemble`, а не
    своя логика сборки на сервере. И она не трогает запись о том, ЧЕМ сделан
    эпизод: сборке провайдеры не нужны, а имена моделей в манифесте — это
    история, а не настройка."""
    from videoforge.core.manifest import load_manifest, save_manifest

    _plan(episode)
    manifest = load_manifest(episode)
    manifest.providers = {"image": "comfy", "video": "comfy", "tts": "edge", "llm": "claude_cli"}
    save_manifest(episode, manifest)

    client = _client()
    resp = client.post("/api/projects/t/episodes/e1/rebuild", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # клипы есть, озвучки нет — сборка отработала и сказала, что сделала
    assert body["status"] in ("done", "failed") and body["message"]
    assert load_manifest(episode).providers["image"] == "comfy"


def test_second_run_over_the_same_episode_is_refused(episode: Path) -> None:
    """Два прогона над одним эпизодом — это два ffmpeg, пишущих один final.mp4.
    Замок общий для пульта и терминала: у них нет общей памяти, только файлы."""
    from videoforge.core.runner import Busy, episode_lock, running

    _plan(episode)
    assert running(episode) is None
    with episode_lock(episode, what="сборка"):
        assert (running(episode) or {}).get("what") == "сборка"
        resp = _client().post("/api/projects/t/episodes/e1/rebuild", json={})
        assert resp.status_code == 409 and "уже идёт" in resp.json()["detail"]
        try:
            with episode_lock(episode, what="вторая"):
                raise AssertionError("второй замок не должен был взяться")
        except Busy:
            pass
    assert running(episode) is None  # замок снят вместе с выходом из блока
