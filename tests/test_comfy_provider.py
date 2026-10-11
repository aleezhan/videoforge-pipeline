"""ComfyUI-провайдер без сети: графы, ожидание (и отказ ждать мёртвый процесс),
скачивание, стоимость по секундам; промпт без паспорта при LoRA."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from videoforge.core.resources import Character, LoraRef, ResourceBundle, Style
from videoforge.core.storyboard import Scene
from videoforge.providers import comfy_http as ch
from videoforge.providers.base import ProviderError
from videoforge.stages.prompts import image_prompt


class FakeComfy:
    """Достаточно ComfyUI, чтобы провайдер прошёл путь целиком."""

    def __init__(self) -> None:
        self.graphs: list[dict] = []
        self.uploads: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/upload/image":
            self.uploads.append("A01.png")
            return httpx.Response(200, json={"name": "A01.png", "subfolder": "", "type": "input"})
        if path == "/prompt":
            body = json.loads(request.content)
            self.graphs.append(body["prompt"])
            return httpx.Response(200, json={"prompt_id": "p1", "number": 1, "node_errors": {}})
        if path.startswith("/history/"):
            g = self.graphs[-1]
            fname = "x.mp4" if "SaveVideo" in json.dumps(g) else "x.png"
            return httpx.Response(
                200,
                json={
                    "p1": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "save": {
                                "images": [{"filename": fname, "subfolder": "", "type": "output"}]
                            }
                        },
                    }
                },
            )
        if path == "/view":
            return httpx.Response(200, content=b"\x89PNG fake bytes")
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeComfy:
    f = FakeComfy()
    transport = httpx.MockTransport(f.handler)
    real_client = httpx.Client

    def patched(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(ch.httpx, "Client", patched)
    return f


def test_image_generate_builds_klein_graph_with_lora_and_bills_seconds(
    fake: FakeComfy, tmp_path: Path
) -> None:
    state = ch.PodState(
        base_url="http://comfy", rate_usd_hr=0.72, lora="amir_v2.safetensors", lora_strength=0.9
    )
    out = tmp_path / "s1_v01.png"
    res = ch.ComfyImage(state).generate(
        "amir, flat style. a man walks", references=[], aspect_ratio="16:9", out_path=out
    )
    assert out.read_bytes().startswith(b"\x89PNG")
    g = fake.graphs[-1]
    assert g["lora"]["inputs"]["lora_name"] == "amir_v2.safetensors"
    assert g["lora"]["inputs"]["strength_model"] == 0.9
    assert g["guider"]["inputs"]["model"] == ["lora", 0] and g["guider"]["inputs"]["cfg"] == 1.0
    assert g["latent"]["inputs"] == {"width": 1280, "height": 720, "batch_size": 1}
    assert g["sigmas"]["inputs"] == {"steps": 4, "width": 1280, "height": 720}
    assert g["neg"]["class_type"] == "ConditioningZeroOut"
    assert 0 <= res.cost_usd < 0.01  # секунды × ставка, а не прайс
    assert res.meta["seed"] == ch._seed("amir, flat style. a man walks" + "s1_v01")


def test_image_vertical_and_no_lora(fake: FakeComfy, tmp_path: Path) -> None:
    state = ch.PodState(base_url="http://comfy")
    ch.ComfyImage(state).generate(
        "p", references=[], aspect_ratio="9:16", out_path=tmp_path / "a.png"
    )
    g = fake.graphs[-1]
    assert "lora" not in g and g["guider"]["inputs"]["model"] == ["unet", 0]
    assert g["latent"]["inputs"]["width"] == 720 and g["latent"]["inputs"]["height"] == 1280


def test_video_ltx_graph_frames_and_upload(fake: FakeComfy, tmp_path: Path) -> None:
    img = tmp_path / "A01.png"
    img.write_bytes(b"\x89PNG")
    state = ch.PodState(base_url="http://comfy", video="ltx25")
    v = ch.ComfyVideo(state)
    # 2 с добавлены 12.09.2026 для замера cat_punch_speed (одно событие без слоумо)
    assert v.name == "comfy:ltx25" and v.supported_durations == (2, 3, 4, 5, 6)
    assert v.pick_duration(5.4) == 6
    res = v.animate(
        img, "walks", duration_sec=5, aspect_ratio="16:9", out_path=tmp_path / "s1_v01.mp4"
    )
    g = fake.graphs[-1]
    assert fake.uploads == ["A01.png"]
    assert g["lat1"]["inputs"] == {"width": 640, "height": 352, "length": 121, "batch_size": 1}
    assert g["aud"]["inputs"]["frames_number"] == 121
    assert g["sig1"]["inputs"]["sigmas"] == ch.LTX_SIGMAS_PASS1
    assert res.meta["frames"] == 121 and res.meta["fps"] == 24
    assert (tmp_path / "s1_v01.mp4").exists()


def test_video_wan_preset_graph(fake: FakeComfy, tmp_path: Path) -> None:
    img = tmp_path / "A01.png"
    img.write_bytes(b"\x89PNG")
    v = ch.ComfyVideo(ch.PodState(base_url="http://comfy", video="wan480"))
    assert v.name == "comfy:wan480"
    v.animate(img, "walks", duration_sec=4, aspect_ratio="16:9", out_path=tmp_path / "s.mp4")
    g = fake.graphs[-1]
    assert g["i2v"]["inputs"]["length"] == 65 and g["i2v"]["inputs"]["width"] == 832
    assert g["s1"]["inputs"]["end_at_step"] == 2 and g["s2"]["inputs"]["start_at_step"] == 2


def test_frames_rule_and_prices() -> None:
    assert ch.frames_for(5, 24) == 121 and ch.frames_for(5, 16) == 81 and ch.frames_for(4, 24) == 97
    v = ch.ComfyVideo(ch.PodState(base_url="http://x", video="ltx25"))
    assert 0.005 < v.price_for(5) < 0.02  # ≈39 с × $0.72/ч
    assert ch.ComfyImage(ch.PodState(base_url="http://x")).price_usd < 0.001


def test_node_errors_become_provider_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(
                200, json={"prompt_id": "p", "node_errors": {"unet": {"errors": ["no such file"]}}}
            )
        return httpx.Response(404)

    real = httpx.Client
    monkeypatch.setattr(
        ch.httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k)
    )
    with pytest.raises(ProviderError, match="отверг граф"):
        ch.ComfyImage(ch.PodState(base_url="http://comfy")).generate(
            "p", references=[], aspect_ratio="16:9", out_path=tmp_path / "a.png"
        )


# --- ожидание результата: мёртвый ComfyUI против долгой задачи


class FakeClock:
    """Часы ожидания без настоящего сна: `sleep` двигает время, запрос — нет.
    Так тест проверяет умолчания `wait` (60 с, 1800 с) за миллисекунды."""

    def __init__(self) -> None:
        self.now = 0.0

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, sec: float) -> None:
        self.now += sec


Handler = Callable[[httpx.Request, float], httpx.Response]


def _client(
    monkeypatch: pytest.MonkeyPatch, handler: Handler
) -> tuple[ch.ComfyClient, FakeClock, list[str]]:
    """Клиент на подменённом HTTP: ответ зависит от запроса и «времени» часов."""
    clock = FakeClock()
    monkeypatch.setattr(ch, "time", clock)
    asked: list[str] = []

    def spy(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return handler(request, clock.now)

    client = ch.ComfyClient("http://comfy")
    client.http = httpx.Client(transport=httpx.MockTransport(spy))
    return client, clock, asked


DONE = {"p1": {"status": {"status_str": "success", "completed": True}, "outputs": {}}}
RUNNING = {"queue_running": [[1, "p1", {}, {}, []]], "queue_pending": []}


def test_wait_gives_up_on_a_dead_comfy_in_a_minute_not_half_an_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """16.09.2026: OOM убил ComfyUI, прокси RunPod отдавал 404 с пустым телом, а
    ожидание читало это как «ещё не готово» и крутилось к таймауту 1800 с."""
    client, clock, asked = _client(monkeypatch, lambda r, now: httpx.Response(404))
    with pytest.raises(ProviderError, match=r"не отвечает.*код 404, пустое тело.*мёртв") as err:
        client.wait("p1")
    assert ch.DEAD_AFTER_S <= clock.now <= ch.DEAD_AFTER_S + 10  # минута, а не полчаса
    assert "/interrupt" not in asked  # мёртвому процессу прерывание не шлётся

    from videoforge.bench.run import error_kind

    assert error_kind(err.value) == "network"  # замер по схеме не жжёт соседние сиды


def test_wait_treats_html_and_a_dead_queue_as_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTML «Waiting for service» с кодом 200 — тоже молчание. И пустая история
    при мёртвой очереди: раньше неразобранный ответ /queue глотался молча."""

    def html(request: httpx.Request, now: float) -> httpx.Response:
        return httpx.Response(200, text="<html>Waiting for service</html>")

    client, clock, _ = _client(monkeypatch, html)
    with pytest.raises(ProviderError, match="не JSON"):
        client.wait("p1")
    assert clock.now <= ch.DEAD_AFTER_S + 10

    def queue_dead(request: httpx.Request, now: float) -> httpx.Response:
        if request.url.path == "/queue":
            return httpx.Response(404)
        return httpx.Response(200, json={})

    client, clock, _ = _client(monkeypatch, queue_dead)
    with pytest.raises(ProviderError, match=r"/queue: код 404"):
        client.wait("p1")
    # очередь спрашивают после 30 с пустой истории; ещё минута молчания — приговор
    assert clock.now <= ch.LOST_AFTER_S + ch.DEAD_AFTER_S + 10


def test_wait_sits_through_a_long_clip_and_a_proxy_blip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Живая очередь — ждём сколько надо (клип LTX идёт минуты); пара секунд 502
    от Cloudflare посреди — не приговор."""

    def slow(request: httpx.Request, now: float) -> httpx.Response:
        if 100 <= now < 115:
            return httpx.Response(502, text="Bad gateway")
        if request.url.path == "/queue":
            return httpx.Response(200, json=RUNNING)
        return httpx.Response(200, json=DONE if now >= 300 else {})

    client, clock, _ = _client(monkeypatch, slow)
    assert client.wait("p1")["status"]["completed"] is True
    assert clock.now >= 300


def test_wait_still_notices_a_restarted_comfy_that_forgot_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Перезапущенный ComfyUI отвечает внятно, но задачи не знает: пустая история
    и пустая очередь — «потерял задачу», как и раньше, без ожидания молчания."""

    def forgot(request: httpx.Request, now: float) -> httpx.Response:
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        return httpx.Response(200, json={})

    client, clock, _ = _client(monkeypatch, forgot)
    with pytest.raises(ProviderError, match="потерял задачу p1"):
        client.wait("p1")
    assert clock.now <= ch.LOST_AFTER_S + 5


def test_pod_state_requires_pod_or_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VF_COMFY_URL", raising=False)
    with pytest.raises(ProviderError, match="gpu up"):
        ch.PodState.load(tmp_path)
    monkeypatch.setenv("VF_COMFY_URL", "http://my-comfy:8188/")
    assert ch.PodState.load(tmp_path).base_url == "http://my-comfy:8188"


def test_prompt_lora_mode_uses_trigger_instead_of_passport() -> None:
    amir = Character(
        id="amir",
        prompt_block="adult man around 35, mole under the left eye",
        lora=LoraRef(file="amir/lora/x.safetensors", trigger="amir"),
    )
    style = Style(
        id="s",
        prompt_prefix="soft 2D illustration",
        prompt_suffix="high quality",
        negative="3D render",
    )
    scene = Scene(id="s1", order=1, visual_prompt="Amir seen from behind on a dune")
    res = ResourceBundle(character=amir, style=style)
    plain = image_prompt(scene, res, [("characters", amir)], has_references=True)
    lora = image_prompt(scene, res, [("characters", amir)], has_references=True, lora_mode=True)
    assert "mole" in plain and "Avoid:" in plain and "reference images" in plain
    assert lora.startswith("amir. soft 2D illustration")
    assert "mole" not in lora and "Avoid:" not in lora and "reference images" not in lora
    assert "from behind" in lora and lora.endswith("high quality.")


def test_image_weights_are_read_from_the_pod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Веса художника — LoRA и её сила с ПОДА, а не из файла персонажа: рендерит
    он тем, что залито. Без LoRA — пусто (кадр определяют промпт и имя), без
    карты — None: чем рендерили бы, неизвестно, и в отпечаток это не идёт."""
    with_lora = ch.PodState(base_url="http://comfy", lora="amir_v2.safetensors", lora_strength=0.9)
    assert ch.ComfyImage(with_lora).weights() == "amir_v2.safetensors@0.9"
    assert ch.ComfyImage(ch.PodState(base_url="http://comfy")).weights() == ""

    import videoforge.paths

    monkeypatch.delenv("VF_COMFY_URL", raising=False)
    monkeypatch.setattr(videoforge.paths, "ROOT", tmp_path)  # .gpu/pod.json нет
    assert ch.ComfyImage().weights() is None
