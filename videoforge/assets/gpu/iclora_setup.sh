#!/bin/bash
# Дополнение к comfy_setup.sh для IC-LoRA: пакет узлов Lightricks ComfyUI-LTXVideo
# (LTXICLoRALoaderModelOnly, LTXAddVideoICLoRAGuide), аннотаторы
# comfyui_controlnet_aux (DWPose, Canny), веса адаптеров — управление движением
# ltx-2.3-22b-ic-lora-union-control-ref0.5 (655 МБ) и пиксельный апскейлер
# (×2 для LTX-2.5, 327 МБ; ×4 от LTX-2.3, 654 МБ), перезапуск ComfyUI.
# Идемпотентно. Последняя строка успешного прогона — ICLORA_DONE.
# Апскейлеры лежат на HF за воротами (лицензию принимает человек своим аккаунтом):
# качаются только если рядом есть /workspace/.hf_env с HF_TOKEN=… (кладёт scp,
# см. run_iclora_setup.py); без токена — SKIP, остальное ставится как прежде.
# Токен не печатается: нет set -x, нет echo $HF_TOKEN.
# Модели DWPose (yolox_l.onnx, dw-ll_ucoco_384) аннотатор качает сам при первом
# вызове с Hugging Face (yzd-v/DWPose, ~300 МБ) — это входит в время первого дубля.
set -u
NEW=/workspace/videoforge-comfy
OLD=$(ls -d /workspace/runpod-slim/ComfyUI /workspace/ComfyUI /ComfyUI 2>/dev/null | head -1)
M="$OLD/models"
CN="$NEW/custom_nodes"
mkdir -p "$CN" "$M/loras"
HF_TOKEN=""
if [ -f /workspace/.hf_env ]; then . /workspace/.hf_env; fi

clone() {  # clone <repo> <dir>
  if [ -d "$CN/$2" ]; then echo "have $2"; return 0; fi
  git clone -q --depth 1 "https://github.com/$1" "$CN/$2" || { echo "FAIL clone $1"; exit 1; }
  if [ -f "$CN/$2/requirements.txt" ]; then
    (cd "$CN/$2" && python3 -m pip install -q -r requirements.txt 2>&1 | grep -v "notice" | tail -n 2)
  fi
  echo "cloned $2"
}
clone Lightricks/ComfyUI-LTXVideo ComfyUI-LTXVideo
clone Fannovel16/comfyui_controlnet_aux comfyui_controlnet_aux
# kornia 0.8.3 (и в образе RunPod, и последняя на PyPI) убрала `pad` из
# geometry.transform.pyramid — pyramid_blending пакета Lightricks падает на
# «cannot import name 'pad'», и ВЕСЬ пакет не грузится. 0.8.1 его ещё содержит
# (проверено 13.09.2026)
python3 -m pip install -q "kornia==0.8.1" 2>&1 | grep -v "notice" | tail -n 1

fetch() {  # fetch <url> <dest> [gated] — aria2c с докачкой, иначе curl; токен только в заголовке
  local url="$1" dest="$2" gated="${3:-}"
  if [ -s "$dest" ]; then echo "have $(basename "$dest")"; return 0; fi
  if [ -n "$gated" ] && [ -z "$HF_TOKEN" ]; then echo "SKIP $(basename "$dest"): нет HF_TOKEN (лицензия на HF не принята)"; return 0; fi
  local auth=()
  if [ -n "$HF_TOKEN" ]; then auth=(--header="Authorization: Bearer $HF_TOKEN"); fi
  local t0=$(date +%s)
  if command -v aria2c >/dev/null 2>&1; then
    aria2c -x8 -s8 -c -q --file-allocation=none "${auth[@]}" -d "$(dirname "$dest")" -o "$(basename "$dest")" "$url" \
      || { echo "FAIL $(basename "$dest") ($(basename "$(dirname "$url")"))"; rm -f "$dest"; return 1; }
  else
    local h=()
    if [ -n "$HF_TOKEN" ]; then h=(-H "Authorization: Bearer $HF_TOKEN"); fi
    curl -L -sS --retry 3 "${h[@]}" -o "$dest" "$url" || { echo "FAIL $(basename "$dest")"; rm -f "$dest"; return 1; }
  fi
  # ворота HF отдают вместо весов html с 401/403 — файл в килобайты, это не веса
  if [ "$(stat -c %s "$dest")" -lt 1000000 ]; then echo "FAIL $(basename "$dest"): ответ не веса ($(stat -c %s "$dest") байт) — лицензия принята? токен тот?"; rm -f "$dest"; return 1; fi
  echo "got $(basename "$dest") $(( $(stat -c %s "$dest")/1000000 )) MB in $(( $(date +%s) - t0 ))s"
}

fetch https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Union-Control/resolve/main/ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors \
  "$M/loras/ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors" || exit 1
fetch https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler/resolve/main/ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors \
  "$M/loras/ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors" gated || true
fetch https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler/resolve/main/ltx-2.3-22b-ic-lora-pixel-spatial-upscaler-x4-0.9.safetensors \
  "$M/loras/ltx-2.3-22b-ic-lora-pixel-spatial-upscaler-x4-0.9.safetensors" gated || true

# перезапуск ComfyUI: узлы подхватываются только при старте
pkill -f "main.py --listen 0.0.0.0 --port 8188" 2>/dev/null; sleep 2
cd "$NEW" && nohup python3 main.py --listen 0.0.0.0 --port 8188 --enable-cors-header --cache-lru 8 > /workspace/out/comfy.log 2>&1 < /dev/null &
for i in $(seq 1 60); do sleep 3; curl -s -o /dev/null http://127.0.0.1:8188/system_stats && break; done
python3 - <<'EOF'
import json, urllib.request
info = json.load(urllib.request.urlopen("http://127.0.0.1:8188/object_info"))

def options(node, field):
    # значения combo-поля: старые узлы — [[...], {...}], узлы V3 — ["COMBO", {"options": [...]}]
    inputs = info.get(node, {}).get("input", {})
    spec = inputs.get("required", {}).get(field) or inputs.get("optional", {}).get(field) or []
    if spec and isinstance(spec[0], list):
        return spec[0]
    if len(spec) > 1 and isinstance(spec[1], dict):
        return list(spec[1].get("options") or [])
    return []

for k in ("LTXICLoRALoaderModelOnly", "LTXAddVideoICLoRAGuide", "DWPreprocessor", "CannyEdgePreprocessor", "LTXVCropGuides", "GetVideoComponents", "VAEEncode"):
    print(k, "OK" if k in info else "MISSING")
print("loras:", [x for x in options("LTXICLoRALoaderModelOnly", "lora_name") if "ic-lora" in x])
EOF
echo ICLORA_DONE
