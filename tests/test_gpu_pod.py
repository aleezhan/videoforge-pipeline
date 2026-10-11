"""Аренда карты и подготовка пода без сети: минимум памяти в запросе REST v2,
проверка лимита cgroup до скачивания моделей, разбор object_info в
setup-скриптах пода (старый и новый формат combo)."""

from __future__ import annotations

import io
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from videoforge.providers import runpod_pod as rp
from videoforge.providers.base import ProviderError

GPU_ASSETS = Path(rp.__file__).resolve().parents[1] / "assets" / "gpu"

# ответ пода на RAM_PROBE: 16.09.2026 одна карта пришла с таким лимитом, другая с 62 ГБ
PROBE_30 = "cgroup2 29999996928\ncgroup1\nMemTotal:       263852340 kB\n"
PROBE_62 = "cgroup2 62000001024\ncgroup1\nMemTotal:       263852340 kB\n"


class FakeRunPod:
    """REST v2 и ssh/scp пода: пишет, что у него просили, в одном журнале —
    так виден порядок «проверка памяти → заливка setup-скрипта»."""

    def __init__(self, probe: str) -> None:
        self.probe = probe
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict[str, Any]] = []

    def api(self, method: str, path: str, **kw: Any) -> Any:
        self.calls.append((method, path))
        if method == "POST" and path == "/pods":
            self.bodies.append(kw["json"])
            return {"id": "pod1"}
        if method == "GET" and path == "/pods/pod1":
            ports = [{"private": 22, "type": "tcp", "ip": "10.0.0.1", "public": 40022}]
            return {"runtime": {"uptime": 12, "ports": ports}, "gpu": {"memory": 30}}
        return {}

    def ssh(self, host: str, port: int, cmd: str, *, timeout: int = 600) -> str:
        self.calls.append(("ssh", cmd))
        return self.probe if cmd == rp.RAM_PROBE else ""

    def scp(self, host: str, port: int, src: Path, dst: str) -> None:
        self.calls.append(("scp", dst))


@pytest.fixture
def pod_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / ".gpu"
    monkeypatch.setattr(rp, "STATE_DIR", state)
    monkeypatch.setattr(rp, "STATE_FILE", state / "pod.json")
    monkeypatch.setattr(rp, "SESSIONS_FILE", state / "sessions.csv")
    monkeypatch.setattr(rp, "_public_key", lambda: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(rp, "wait_ssh", lambda host, port, timeout_s=300: None)
    return state


def _fake(monkeypatch: pytest.MonkeyPatch, probe: str) -> FakeRunPod:
    fake = FakeRunPod(probe)
    monkeypatch.setattr(rp, "_api", fake.api)
    monkeypatch.setattr(rp, "_ssh", fake.ssh)
    monkeypatch.setattr(rp, "_scp", fake.scp)
    return fake


def test_create_pod_asks_for_minimum_ram(pod_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Поле REST v2 — `gpu.minRamPerGpu` (CreateGpuConfig, ГБ на карту). Схема
    запрещает лишние поля, так что опечатка в имени — 422, а не тихий пропуск."""
    fake = _fake(monkeypatch, PROBE_62)
    rp.create_pod(rp.PodSpec())
    gpu = fake.bodies[-1]["gpu"]
    assert gpu == {"id": rp.DEFAULT_GPU, "count": 1, "minRamPerGpu": rp.MIN_RAM_GB}
    assert 36 <= rp.MIN_RAM_GB <= 62  # выше LTX-набора (≈35 ГБ), не выше проверенного хоста


def test_up_terminates_a_pod_with_too_little_ram_before_any_setup(
    pod_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(monkeypatch, PROBE_30)
    said: list[str] = []
    with pytest.raises(ProviderError, match=r"30 ГБ памяти контейнера, нужно ≥ 60"):
        rp.up(rp.PodSpec(), echo=said.append)
    assert ("DELETE", "/pods/pod1") in fake.calls  # не платим за пустое
    assert not [c for c in fake.calls if c[0] == "scp"]  # ни setup-скрипта, ни LoRA
    assert not any("comfy_setup.sh" in c[1] for c in fake.calls)  # модели не качались
    assert "setup failed" in (pod_env / "sessions.csv").read_text(encoding="utf-8")
    assert not (pod_env / "pod.json").exists()
    assert "ОЗУ по API: 30 ГБ" in said


def test_up_checks_ram_first_then_prepares_a_big_enough_pod(
    pod_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(monkeypatch, PROBE_62)
    monkeypatch.setattr(rp, "wait_setup", lambda *a, **k: "SETUP_DONE")
    monkeypatch.setattr(rp, "wait_comfy", lambda *a, **k: {"system": {}})
    state = rp.up(rp.PodSpec(), echo=lambda _: None)
    assert state["pod_id"] == "pod1" and (pod_env / "pod.json").exists()
    order = [c for c in fake.calls if c[0] in ("ssh", "scp")]
    assert order[0] == ("ssh", rp.RAM_PROBE)
    assert order[1] == ("scp", "/workspace/comfy_setup.sh")
    assert ("DELETE", "/pods/pod1") not in fake.calls


def test_container_ram_is_the_smaller_of_cgroup_and_host() -> None:
    assert rp.container_ram_bytes(PROBE_30) == 29999996928
    assert rp.container_ram_bytes(PROBE_62) == 62000001024
    # без лимита: v2 «max», v1 ≈2^63 — решает память хоста
    unlimited = "cgroup2 max\ncgroup1 9223372036854771712\nMemTotal: 263852340 kB\n"
    assert rp.container_ram_bytes(unlimited) == 263852340 * 1024
    assert rp.container_ram_bytes("cgroup2\ncgroup1\n") is None


def test_check_ram_tolerates_page_rounding(monkeypatch: pytest.MonkeyPatch) -> None:
    """RunPod округляет лимит вниз до страницы: ровно 60 ГБ — это 59 999 997 952
    байт, и такой под убивать нельзя. Не прочитали лимит — тоже не убиваем."""
    probes = iter(["cgroup2 59999997952\n", "garbage\n"])
    monkeypatch.setattr(rp, "_ssh", lambda *a, **k: next(probes))
    said: list[str] = []
    assert rp.check_ram("h", 1, 60, echo=said.append) == 59999997952
    assert rp.check_ram("h", 1, 60, echo=said.append) is None
    assert "проверка пропущена" in said[-1]


# --- setup-скрипты пода: разбор object_info


def _heredoc_python(script: Path) -> str:
    m = re.search(r"python3 - <<'EOF'\n(.*?)\nEOF\n", script.read_text(encoding="utf-8"), re.S)
    assert m, f"в {script.name} нет проверки узлов на python"
    return m.group(1)


def _run_check(
    script: Path, info: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> str:
    """Выполнить проверку узлов из скрипта против подменённого /object_info."""
    body = json.dumps(info).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda url: io.BytesIO(body))
    exec(compile(_heredoc_python(script), script.name, "exec"), {"__name__": "__main__"})
    return str(capsys.readouterr().out)


LORAS = ["ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors", "amir_v2.safetensors"]
UNETS = ["wan2.2_animate_14B_fp8.safetensors", "ltx-2.5.safetensors"]


@pytest.mark.parametrize("fmt", ["old", "v3"])
def test_setup_scripts_read_combo_lists_in_both_formats(
    fmt: str, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """16.09.2026 iclora_setup.sh печатал `loras: []` при адаптере на месте: узлы
    V3 отдают combo как ["COMBO", {"options": [...]}], а разбор ждал [[...], {...}]."""

    def combo(values: list[str]) -> list[Any]:
        return [values, {}] if fmt == "old" else ["COMBO", {"options": values}]

    info = {
        "LTXICLoRALoaderModelOnly": {"input": {"required": {"lora_name": combo(LORAS)}}},
        "UNETLoader": {"input": {"required": {"unet_name": combo(UNETS)}}},
    }
    out = _run_check(GPU_ASSETS / "iclora_setup.sh", info, monkeypatch, capsys)
    assert f"loras: {LORAS[:1]}" in out
    out = _run_check(GPU_ASSETS / "animate_setup.sh", info, monkeypatch, capsys)
    assert f"animate unet: {UNETS[:1]}" in out
