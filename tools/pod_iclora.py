"""Докатить IC-LoRA на поднятый под: узлы Lightricks, аннотаторы и адаптеры.

`videoforge gpu up` ставит только базовый набор; управление движением
(`comfy_control`) и пиксельный апскейлер (`comfy_upscale`) требуют ещё
`assets/gpu/iclora_setup.sh`. Апскейлеры лежат на HF за воротами: токен из
`.env` уходит на под файлом `/workspace/.hf_env` (права 600) — не печатается и
не попадает в командную строку ssh. Без HF_TOKEN скрипт ставит остальное, а
апскейлеры пропускает (SKIP в журнале). Ждёт `ICLORA_DONE`, потом — пока
ComfyUI снова ответит (скрипт на поде его перезапускает).

    uv run python tools/pod_iclora.py

Ключ RunPod берётся тот же, что у `gpu up` (`VF_RUNPOD_KEY_VAR`). До 21.09.2026
этот шаг жил разовыми скриптами сессий и пропадал вместе с ними.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from videoforge.config import load_env
from videoforge.paths import ROOT
from videoforge.providers import runpod_pod as rp

SCRIPT = ROOT / "videoforge" / "assets" / "gpu" / "iclora_setup.sh"
TIMEOUT_S = 1500


def main() -> int:
    load_env(ROOT)
    state = rp.load_state()
    if not state:
        print("под не поднят (.gpu/pod.json нет) — сначала videoforge gpu up")
        return 1
    host, port = state["host"], int(state["port"])
    token = os.environ.get("HF_TOKEN", "")
    if token:
        fd, tmp = tempfile.mkstemp(prefix="hf_env_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(f"HF_TOKEN={token}\n")
            rp._scp(host, port, Path(tmp), "/workspace/.hf_env")
        finally:
            os.unlink(tmp)
        rp._ssh(host, port, "chmod 600 /workspace/.hf_env")
    else:
        print("HF_TOKEN в .env нет — апскейлеры будут пропущены")
    rp._scp(host, port, SCRIPT, "/workspace/iclora_setup.sh")
    rp._ssh(host, port, "mkdir -p /workspace/out")
    # одна команда под nohup: `A && nohup B &` уводит в фон весь список и держит сессию
    rp._ssh(
        host,
        port,
        "nohup bash /workspace/iclora_setup.sh > /workspace/out/iclora.log 2>&1 < /dev/null &",
        timeout=60,
    )
    print("iclora_setup запущен", flush=True)
    t0 = time.time()
    seen = ""
    while time.time() - t0 < TIMEOUT_S:
        time.sleep(15)
        try:
            log = rp._ssh(host, port, "cat /workspace/out/iclora.log 2>/dev/null", timeout=60)
        except Exception as exc:  # noqa: BLE001 — сеть моргнула, следующий заход
            print(f"  ssh: {type(exc).__name__}", flush=True)
            continue
        for line in log[len(seen) :].splitlines() if log.startswith(seen) else log.splitlines():
            print("  " + line[:160], flush=True)
        seen = log
        if "ICLORA_DONE" in log:
            break
        # bash, упавший на разборе, слова FAIL не пишет — ловим и его
        if "FAIL" in log or "syntax error" in log or "invalid option" in log:
            print("подготовка IC-LoRA упала — журнал выше (/workspace/out/iclora.log)")
            return 1
    else:
        print(f"iclora_setup не закончился за {TIMEOUT_S // 60} мин")
        return 1
    version = rp.wait_comfy(state["base_url"]).get("system", {}).get("comfyui_version")
    print(f"ComfyUI {version} отвечает; IC-LoRA за {time.time() - t0:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
