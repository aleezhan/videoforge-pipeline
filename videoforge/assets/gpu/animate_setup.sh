#!/bin/bash
# Дополнение к comfy_setup.sh --video animate: узлы шаблона ComfyUI
# video_wan2_2_14B_animate — comfyui_controlnet_aux (DWPose: скелет и лицо),
# ComfyUI-KJNodes (DrawMaskOnImage, BlockifyMask), ComfyUI-segment-anything-2
# (маска персонажа по точкам, SAM2 качает сам с HF при первом вызове, ~300 МБ),
# перезапуск ComfyUI и проверка, что узлы на месте. Идемпотентно. Последняя
# строка успешного прогона — ANIMATE_DONE.
set -u
NEW=/workspace/videoforge-comfy
CN="$NEW/custom_nodes"
mkdir -p "$CN"

clone() {  # clone <repo> <dir>
  if [ -d "$CN/$2" ]; then echo "have $2"; return 0; fi
  git clone -q --depth 1 "https://github.com/$1" "$CN/$2" || { echo "FAIL clone $1"; exit 1; }
  if [ -f "$CN/$2/requirements.txt" ]; then
    (cd "$CN/$2" && python3 -m pip install -q -r requirements.txt 2>&1 | grep -v "notice" | tail -n 2)
  fi
  echo "cloned $2"
}
clone Fannovel16/comfyui_controlnet_aux comfyui_controlnet_aux
clone kijai/ComfyUI-KJNodes ComfyUI-KJNodes
clone kijai/ComfyUI-segment-anything-2 ComfyUI-segment-anything-2

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

need =("WanAnimateToVideo", "TrimVideoLatent", "ImageFromBatch", "ImageBatch", "GrowMask", "CLIPVisionLoader",
        "CLIPVisionEncode", "ModelSamplingSD3", "DWPreprocessor", "PixelPerfectResolution",
        "DrawMaskOnImage", "BlockifyMask", "Sam2Segmentation", "DownloadAndLoadSAM2Model", "PrimitiveString")
for k in need:
    print(k, "OK" if k in info else "MISSING")
print("animate unet:", [x for x in options("UNETLoader", "unet_name") if "nimate" in x])
EOF
echo ANIMATE_DONE
