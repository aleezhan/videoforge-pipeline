"""Аренда карты на RunPod под ComfyUI: `videoforge gpu up|down|status`.

Под — не резидентный агент, а ресурс с явным началом и концом: `up` создаёт
под (REST v2), ждёт SSH, заливает setup-скрипт и LoRA персонажа, запускает
подготовку (модели, ComfyUI 0.34) и, дождавшись `SETUP_DONE`, пишет
`.gpu/pod.json` — единственное, что читает провайдер comfy_http. `down`
терминирует под и закрывает строку в `.gpu/sessions.csv` с длительностью и
стоимостью. Стадии конвейера подов не поднимают: деньги начинают течь только
по явной команде человека (или расписания, которое её вызовет).

Только REST v2 (api.runpod.io/v2): v1 отключают 15.11.2026. Ключ не печатается.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from videoforge.config import runpod_key_var
from videoforge.paths import ROOT
from videoforge.providers.base import ProviderError

log = logging.getLogger("videoforge.runpod")

API = "https://api.runpod.io/v2"
STATE_DIR = ROOT / ".gpu"
STATE_FILE = STATE_DIR / "pod.json"
SESSIONS_FILE = STATE_DIR / "sessions.csv"
SETUP_SCRIPT = Path(__file__).resolve().parents[1] / "assets" / "gpu" / "comfy_setup.sh"

# Замер 07–08.09.2026: RTX PRO 4500 Blackwell 32 ГБ, Secure, EU-RO-1 — единственная
# из доступных карт, куда влезают Wan 2.2 fp8 и LTX-2.5 int8 (пик 30–31 ГБ).
DEFAULT_GPU = "NVIDIA RTX PRO 4500 Blackwell"
DEFAULT_DC = "EU-RO-1"
DEFAULT_IMAGE = "runpod/comfyui:1.2.0-rc.163-comfyuiv0.30.0-cuda13.0"
DEFAULT_DISK_GB = 150  # klein 17 + LTX-2.5 39 + Wan 37 ГБ + образ; 80 ГБ кончались (Test 1b)
DEFAULT_RATE_USD_HR = 0.72
# Минимум ОЗУ контейнера на карту, ГБ. Замер 16.09.2026 (t07_lora_motion_ref):
# две RTX PRO 4500 из одного каталога пришли с лимитом cgroup 30 ГБ (memory.max
# 29 999 996 928, oom_kill на первом клипе) и 62 ГБ. LTX-2.5 в памяти — энкодер
# 14.6 ГБ + трансформер 20.5 ГБ ≈ 35 ГБ, плюс VAE, апскейлер и сам процесс; при
# смене klein → LTX без `/free` не хватало и 62 (Test 3, 08.09). 60 — запас над
# LTX-набором вместе с klein (16 ГБ), но не выше 62, на которых конвейер прошёл
# целиком: выше — отрезали бы проверенные хосты, а карта и так в дефиците.
# Уходит в REST v2 полем `gpu.minRamPerGpu` (фильтр размещения, ГБ на карту),
# и то же число сверяется с cgroup пода после старта (`check_ram`).
MIN_RAM_GB = 60


@dataclass
class PodSpec:
    name: str = "videoforge-comfy"
    gpu: str = DEFAULT_GPU
    dc: str = DEFAULT_DC
    image: str = DEFAULT_IMAGE
    disk_gb: int = DEFAULT_DISK_GB
    min_ram_gb: int = MIN_RAM_GB
    video: str = "ltx25"  # ltx25 | wan480 | both | animate | anisora | h3, через запятую
    klein: bool = True
    image_models: str = ""  # модели картинок сверх klein: qwen_edit
    comfy: str = ""  # тег ComfyUI; пусто — решает comfy_setup.sh (0.34, с h3 — 0.36)
    ports: list[str] = field(default_factory=lambda: ["22/tcp", "8188/http"])


def _headers() -> dict[str, str]:
    key = os.environ.get(runpod_key_var(), "")
    if not key:
        raise ProviderError(f"{runpod_key_var()} не задан в .env")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _api(method: str, path: str, **kw: Any) -> Any:
    """REST v2 с повтором на 5xx и обрывах: Cloudflare перед api.runpod.io отдаёт
    502 на ровном месте (08.09.2026 — посреди ожидания SSH), и один такой ответ
    не повод терминировать под."""
    timeout = kw.pop("timeout", 60)
    last = ""
    for attempt in range(4):
        try:
            r = httpx.request(method, f"{API}{path}", headers=_headers(), timeout=timeout, **kw)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (200, 201, 204):
            return r.json() if r.content and r.status_code != 204 else {}
        last = f"{r.status_code} {r.text[:200]}"
        if r.status_code < 500:
            break
        time.sleep(5 * (attempt + 1))
    raise ProviderError(f"RunPod {method} {path}: {last}")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _public_key() -> str:
    """Наш публичный ключ для `authorized_keys` пода.

    RunPod кладёт в `~/.ssh/authorized_keys` содержимое переменной `PUBLIC_KEY`
    и больше ничего: ключи, сохранённые в настройках аккаунта, попадают туда
    только через ту же переменную, и если её не передать, под поднимается с
    пустым `authorized_keys`. Тогда `ssh` честно предлагает наш ключ и получает
    `Permission denied (publickey,password)` — ровно то, на чём 09.09.2026
    подряд умерли два `gpu up` (под сам себя терминировал, деньги не утекли, но
    и работы не было). Поэтому ключ передаём явно.
    """
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        path = Path.home() / ".ssh" / name
        if path.exists():
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
    raise ProviderError(
        "нет публичного ключа ~/.ssh/id_ed25519.pub — без него под не пустит по SSH; "
        "сделай `ssh-keygen -t ed25519`"
    )


def _ssh_target(pod: dict[str, Any]) -> tuple[str, int]:
    for port in (pod.get("runtime") or {}).get("ports") or []:
        if port.get("private") == 22 and port.get("type") == "tcp" and port.get("ip"):
            return str(port["ip"]), int(port["public"])
    raise ProviderError("у пода нет прямого SSH-порта (22/tcp)")


def _ssh(host: str, port: int, cmd: str, *, timeout: int = 600) -> str:
    r = subprocess.run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=20",
            "-p",
            str(port),
            f"root@{host}",
            cmd,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        raise ProviderError(f"ssh: {r.stderr.strip()[:300] or r.stdout.strip()[:300]}")
    return r.stdout


def _scp(host: str, port: int, src: Path, dst: str) -> None:
    r = subprocess.run(
        [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "BatchMode=yes",
            "-q",
            "-P",
            str(port),
            str(src),
            f"root@{host}:{dst}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        raise ProviderError(f"scp {src.name}: {r.stderr.strip()[:300]}")


def proxy_url(pod_id: str, port: int = 8188) -> str:
    return f"https://{pod_id}-{port}.proxy.runpod.net"


def _log_session(row: dict[str, Any]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    fields = [
        "started",
        "ended",
        "pod_id",
        "gpu",
        "dc",
        "rate_usd_hr",
        "minutes",
        "cost_usd",
        "note",
    ]
    new = not SESSIONS_FILE.exists()
    with SESSIONS_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def load_state() -> dict[str, Any] | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def gpu_rate(gpu_id: str) -> float:
    """Ставка Secure из каталога; каталог недоступен — прайс по умолчанию."""
    try:
        cat = _api("GET", "/catalog/gpus")
        for g in cat.get("gpus", []):
            if g.get("id") == gpu_id:
                return float((g.get("price") or {}).get("secure") or DEFAULT_RATE_USD_HR)
    except ProviderError:
        pass
    return DEFAULT_RATE_USD_HR


def create_pod(spec: PodSpec) -> dict[str, Any]:
    body = {
        "name": spec.name,
        "cloud": "SECURE",
        # minRamPerGpu — фильтр размещения REST v2 (CreateGpuConfig, ГБ на карту):
        # без него та же карта приходила с 30 ГБ памяти контейнера, и LTX-2.5
        # умирала на первом клипе
        "gpu": {"id": spec.gpu, "count": 1, "minRamPerGpu": spec.min_ram_gb},
        "dataCenterIds": [spec.dc],
        "image": spec.image,
        "disk": spec.disk_gb,
        "ports": spec.ports,
        "startSsh": True,
        "startJupyter": False,
        "env": {"PUBLIC_KEY": _public_key()},
    }
    try:
        return _api("POST", "/pods", json=body, timeout=180)
    except httpx.ReadTimeout:
        # POST может отвалиться по таймауту, а под создаться (Test 1b): ищем по имени
        for p in _api("GET", "/pods").get("pods", []):
            if p.get("name") == spec.name:
                return p
        raise ProviderError("POST /pods: таймаут, под не найден") from None


def wait_ready(pod_id: str, timeout_s: int = 900, echo: Any = print) -> dict[str, Any]:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        p = _api("GET", f"/pods/{pod_id}")
        rt = p.get("runtime") or {}
        if rt.get("uptime") and any(x.get("private") == 22 for x in rt.get("ports") or []):
            return p
        echo(f"  {int(time.time() - t0):4d} с · {p.get('desiredStatus')} · ждём SSH")
        time.sleep(15)
    raise ProviderError("под не поднялся за отведённое время")


def wait_ssh(host: str, port: int, timeout_s: int = 300) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            _ssh(host, port, "echo ok", timeout=40)
            return
        except (ProviderError, subprocess.TimeoutExpired):
            time.sleep(10)
    raise ProviderError("SSH не отвечает")


# Что видит процесс пода: лимит cgroup v2, v1 и память хоста — метками строк,
# чтобы пустой ответ одного файла не сдвигал разбор остальных
RAM_PROBE = (
    "echo cgroup2 $(cat /sys/fs/cgroup/memory.max 2>/dev/null); "
    "echo cgroup1 $(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null); "
    "grep MemTotal /proc/meminfo"
)


def container_ram_bytes(probe: str) -> int | None:
    """Сколько памяти у процессов пода на самом деле: меньшее из лимита cgroup и
    памяти хоста. `max` у v2 и ≈2^63 у v1 — «без лимита», не число. Не прочитано
    ничего — None."""
    found: list[int] = []
    for line in probe.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        if parts[0] in ("cgroup2", "cgroup1") and int(parts[1]) < 1 << 60:
            found.append(int(parts[1]))
        elif parts[0] == "MemTotal:":
            found.append(int(parts[1]) * 1024)  # /proc/meminfo — в кБ
    return min(found) if found else None


def check_ram(host: str, port: int, min_gb: int, echo: Any = print) -> int | None:
    """Память контейнера против минимума — ДО заливки и скачивания моделей.

    `minRamPerGpu` — фильтр размещения, и что за ним лимит cgroup не меньше,
    известно только из документации, а 16.09.2026 одна и та же карта приходила
    с 30 и 62 ГБ. Мало — ProviderError, и `up` терминирует под за минуты, а не
    после получаса моделей и первого клипа. Лимит не прочитан — не повод
    убивать под: слово человеку, дальше как раньше."""
    limit = container_ram_bytes(_ssh(host, port, RAM_PROBE, timeout=60))
    if limit is None:
        echo("  лимит памяти контейнера не прочитан — проверка пропущена")
        return None
    gb = limit / 1e9
    echo(f"  память контейнера: {gb:.1f} ГБ (нужно ≥ {min_gb})")
    # RunPod пишет лимит в десятичных ГБ, округлённых вниз до страницы
    # (29 999 996 928 при «30 ГБ»): полгигабайта допуска, чтобы не убить ровно 60
    if limit < (min_gb - 0.5) * 1e9:
        raise ProviderError(
            f"у пода {gb:.0f} ГБ памяти контейнера, нужно ≥ {min_gb}: LTX-2.5 (энкодер "
            "14.6 + трансформер 20.5 ГБ) на такой умирает на первом клипе — под "
            "терминирован до скачивания моделей, подними заново (`videoforge gpu up`)"
        )
    return limit


def wait_setup(host: str, port: int, timeout_s: int = 1800, echo: Any = print) -> str:
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout_s:
        try:
            tail = _ssh(
                host, port, "tail -n 3 /workspace/out/setup.log 2>/dev/null", timeout=60
            ).strip()
        except (ProviderError, subprocess.TimeoutExpired):
            tail = ""
        if tail and tail != last:
            echo("  " + tail.splitlines()[-1][:120])
            last = tail
        if "SETUP_DONE" in tail:
            return tail
        if "FAIL" in tail:
            raise ProviderError(f"подготовка пода упала: {tail[-300:]}")
        time.sleep(15)
    raise ProviderError("подготовка пода не завершилась за отведённое время")


def wait_comfy(base_url: str, timeout_s: int = 300) -> dict[str, Any]:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            r = httpx.get(f"{base_url}/system_stats", timeout=30)
            if r.status_code == 200:
                return r.json()
        except httpx.HTTPError:
            pass
        time.sleep(10)
    raise ProviderError(f"ComfyUI не отвечает по {base_url}")


def up(
    spec: PodSpec,
    *,
    lora_file: Path | None = None,
    lora_strength: float = 1.0,
    echo: Any = print,
) -> dict[str, Any]:
    """Создать под, подготовить, записать состояние. Уже поднят — вернуть его."""
    state = load_state()
    if state and state.get("pod_id"):
        try:
            p = _api("GET", f"/pods/{state['pod_id']}")
            if (p.get("runtime") or {}).get("uptime"):
                echo(f"под {state['pod_id']} уже поднят: {state['base_url']}")
                return state
        except ProviderError:
            pass
        STATE_FILE.unlink(missing_ok=True)
    if lora_file and not lora_file.exists():
        raise ProviderError(f"файла LoRA нет: {lora_file}")

    started = _now()
    pod = create_pod(spec)
    pod_id = str(pod["id"])
    echo(f"под создан: {pod_id} ({spec.gpu}, {spec.dc}, диск {spec.disk_gb} ГБ)")
    rate = gpu_rate(spec.gpu)
    try:
        pod = wait_ready(pod_id, echo=echo)
        gpu = pod.get("gpu")
        if isinstance(gpu, dict) and gpu.get("memory"):
            echo(f"ОЗУ по API: {gpu['memory']} ГБ")  # для журнала; решает замер cgroup ниже
        host, port = _ssh_target(pod)
        wait_ssh(host, port)
        echo(f"ssh готов: root@{host} -p {port}")
        check_ram(host, port, spec.min_ram_gb, echo=echo)
        _scp(host, port, SETUP_SCRIPT, "/workspace/comfy_setup.sh")
        lora_name = ""
        if lora_file:
            _ssh(host, port, "mkdir -p /workspace/runpod-slim/ComfyUI/models/loras")
            _scp(
                host,
                port,
                lora_file,
                f"/workspace/runpod-slim/ComfyUI/models/loras/{lora_file.name}",
            )
            lora_name = lora_file.name
            echo(f"LoRA залита: {lora_name}")
        args = f"--video {spec.video}" + ("" if spec.klein else " --no-klein")
        if spec.image_models:
            args += f" --image-models {spec.image_models}"
        if spec.comfy:
            args += f" --comfy {spec.comfy}"
        _ssh(
            host,
            port,
            "mkdir -p /workspace/out && nohup bash /workspace/comfy_setup.sh "
            f"{args} > /workspace/out/setup.log 2>&1 < /dev/null &",
        )
        echo(f"подготовка запущена ({args}) …")
        wait_setup(host, port, echo=echo)
        base_url = proxy_url(pod_id)
        stats = wait_comfy(base_url)
        echo(f"ComfyUI {stats.get('system', {}).get('comfyui_version')} отвечает: {base_url}")
    except Exception:
        echo("подготовка не удалась — терминирую под, чтобы не платить за пустое")
        try:
            _api("DELETE", f"/pods/{pod_id}")
        finally:
            _log_session(
                {
                    "started": started,
                    "ended": _now(),
                    "pod_id": pod_id,
                    "gpu": spec.gpu,
                    "dc": spec.dc,
                    "rate_usd_hr": rate,
                    "minutes": "",
                    "cost_usd": "",
                    "note": "setup failed",
                }
            )
        raise
    state = {
        "pod_id": pod_id,
        "base_url": base_url,
        "ssh": f"ssh -p {port} root@{host}",
        "host": host,
        "port": port,
        "gpu": spec.gpu,
        "dc": spec.dc,
        "rate_usd_hr": rate,
        "disk_gb": spec.disk_gb,
        "video": spec.video,
        "lora": lora_name,
        "lora_strength": lora_strength,
        "started": started,
        "spec": asdict(spec),
    }
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return state


def down(echo: Any = print) -> dict[str, Any] | None:
    state = load_state()
    if not state:
        echo("под не поднят (.gpu/pod.json нет)")
        return None
    pod_id = state["pod_id"]
    try:
        _api("DELETE", f"/pods/{pod_id}")
        echo(f"под {pod_id} терминирован")
    except ProviderError as exc:
        echo(f"терминация: {exc} (возможно, под уже удалён)")
    ended = _now()
    t0 = datetime.strptime(state["started"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    minutes = (datetime.now(UTC) - t0).total_seconds() / 60
    cost = round(minutes / 60 * float(state.get("rate_usd_hr") or DEFAULT_RATE_USD_HR), 3)
    _log_session(
        {
            "started": state["started"],
            "ended": ended,
            "pod_id": pod_id,
            "gpu": state.get("gpu"),
            "dc": state.get("dc"),
            "rate_usd_hr": state.get("rate_usd_hr"),
            "minutes": round(minutes, 1),
            "cost_usd": cost,
            "note": "",
        }
    )
    STATE_FILE.unlink(missing_ok=True)
    echo(f"сессия: {minutes:.0f} мин ≈ ${cost:.2f} (GPU по ставке, без диска) → .gpu/sessions.csv")
    return {"minutes": minutes, "cost_usd": cost}


def status() -> dict[str, Any]:
    state = load_state()
    if not state:
        return {"up": False}
    out: dict[str, Any] = {"up": True, **state}
    try:
        p = _api("GET", f"/pods/{state['pod_id']}")
        out["runpod_status"] = p.get("desiredStatus")
        out["uptime_sec"] = (p.get("runtime") or {}).get("uptime")
    except ProviderError as exc:
        out["runpod_status"] = f"? ({exc})"
    try:
        r = httpx.get(f"{state['base_url']}/queue", timeout=20)
        if r.status_code != 200:
            # прокси RunPod на мёртвый процесс: 404 с пустым телом (16.09.2026)
            out["comfy"] = f"не отвечает (код {r.status_code}) — процесс на поде мёртв?"
        else:
            q = r.json()
            out["queue_running"] = len(q.get("queue_running", []))
            out["queue_pending"] = len(q.get("queue_pending", []))
    except httpx.HTTPError as exc:
        out["comfy"] = f"не отвечает ({type(exc).__name__})"
    except ValueError:
        out["comfy"] = "не отвечает (код 200, не JSON — прокси без процесса?)"
    t0 = datetime.strptime(state["started"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    out["minutes"] = round((datetime.now(UTC) - t0).total_seconds() / 60, 1)
    out["cost_so_far_usd"] = round(
        out["minutes"] / 60 * float(state.get("rate_usd_hr") or DEFAULT_RATE_USD_HR), 3
    )
    return out
