#!/bin/bash
# Подготовка пода RunPod с образом runpod/comfyui под videoforge: ComfyUI 0.34
# (нужен для LTX-2.5; для H3 — 0.36), модели klein 4B + видео по списку,
# запуск ComfyUI на 8188. Идемпотентно: готовое не качает и не клонирует заново.
#
#   bash comfy_setup.sh --video ltx25|wan480|both|animate|anisora|h3[,…]
#                       [--image-models qwen_edit,flux2_dev] [--no-klein] [--comfy v0.36.0]
#
# Наборы через запятую: `--video ltx25,anisora --image-models qwen_edit` — замер аниме
# по манге на одной карте 32 ГБ (T-66). H3 ставится ОТДЕЛЬНЫМ подом вне ЕС и США
# (лицензия MiniMax H3) и по умолчанию тянет ComfyUI v0.36.0 (экономнее VAE).
#
# Пишет ход в stdout; последняя строка успешного прогона — SETUP_DONE.
# Имена файлов моделей совпадают с videoforge/providers/comfy_http.py.
set -u
VIDEO=ltx25; IMAGE=""; KLEIN=1; COMFY_REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --video) VIDEO="$2"; shift 2 ;;
    --image-models) IMAGE="$2"; shift 2 ;;
    --comfy) COMFY_REF="$2"; shift 2 ;;
    --no-klein) KLEIN=0; shift ;;
    *) echo "неизвестный аргумент: $1"; exit 2 ;;
  esac
done
[ "$VIDEO" = both ] && VIDEO=ltx25,wan480
want() { case ",$VIDEO," in *",$1,"*) return 0 ;; esac; return 1; }
want_image() { case ",$IMAGE," in *",$1,"*) return 0 ;; esac; return 1; }
if [ -z "$COMFY_REF" ]; then
  if want h3; then COMFY_REF=v0.36.0; else COMFY_REF=v0.34.0; fi
fi

OLD=$(ls -d /workspace/runpod-slim/ComfyUI /workspace/ComfyUI /ComfyUI 2>/dev/null | head -1)
NEW=/workspace/videoforge-comfy
[ "$COMFY_REF" = v0.34.0 ] || NEW=/workspace/videoforge-comfy-$COMFY_REF
COMFY_VER=$(echo "${COMFY_REF#v}" | cut -d. -f1,2)  # v0.36.0 → 0.36: так его зовёт system_stats
M="$OLD/models"
mkdir -p "$M/diffusion_models" "$M/text_encoders" "$M/vae" "$M/loras" "$M/latent_upscale_models" "$OLD/input" "$OLD/output" /workspace/out
echo "COMFY_OLD=$OLD MODELS=$M"

command -v aria2c >/dev/null 2>&1 || { apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq aria2 >/dev/null 2>&1 && echo "aria2 installed" || echo "aria2 unavailable, curl"; }

dl() {  # dl <url> <dst>
  local url=$1 dst=$2
  if [ -s "$dst" ]; then echo "have $(basename "$dst")"; return 0; fi
  local t0=$(date +%s)
  if command -v aria2c >/dev/null 2>&1; then
    aria2c -x8 -s8 -c -q --file-allocation=none -d "$(dirname "$dst")" -o "$(basename "$dst")" "$url" || { echo "FAIL $url"; return 1; }
  else
    curl -L -sS --retry 3 -o "$dst" "$url" || { echo "FAIL $url"; return 1; }
  fi
  local dt=$(( $(date +%s) - t0 )); local sz=$(stat -c %s "$dst")
  echo "got $(basename "$dst") $((sz/1000000)) MB in ${dt}s"
}

HF=https://huggingface.co
# --- klein 4B (картинки персонажа с LoRA)
if [ "$KLEIN" = 1 ]; then
  KL=$HF/Comfy-Org/flux2-klein-4B/resolve/main/split_files
  dl $KL/diffusion_models/flux-2-klein-4b.safetensors "$M/diffusion_models/flux-2-klein-4b.safetensors"
  dl $KL/text_encoders/qwen_3_4b.safetensors "$M/text_encoders/qwen_3_4b.safetensors"
  dl $KL/vae/flux2-vae.safetensors "$M/vae/flux2-vae.safetensors"
fi
# --- LTX-2.5 distilled int8 (зеркало comfyicu; официальный Lightricks/LTX-2.5 закрыт формой)
if want ltx25; then
  L=$HF/comfyicu/LTX-2.5/resolve/main
  dl $L/vae/ltx-2.5-video-vae-bf16.safetensors "$M/vae/ltx-2.5-video-vae-bf16.safetensors"
  dl $L/vae/ltx-2.5-video-vae-conv-bf16.safetensors "$M/vae/ltx-2.5-video-vae-conv-bf16.safetensors"
  dl $L/vae/ltx-2.5-audio-vae-bf16.safetensors "$M/vae/ltx-2.5-audio-vae-bf16.safetensors"
  dl $L/latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors "$M/latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"
  dl $L/text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors "$M/text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors"
  dl $L/diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors "$M/diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors"
fi
# --- Wan 2.2 I2V fp8 + lightx2v (480p)
if want wan480; then
  R=$HF/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files
  K=$HF/Kijai/WanVideo_comfy/resolve/main/LoRAs/Wan22_Lightx2v
  dl $R/vae/wan_2.1_vae.safetensors "$M/vae/wan_2.1_vae.safetensors"
  dl $R/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors "$M/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
  dl $K/Wan_2_2_I2V_A14B_HIGH_lightx2v_4step_lora_260412_rank_64_fp16.safetensors "$M/loras/Wan_2_2_I2V_A14B_HIGH_lightx2v_4step_lora_260412_rank_64_fp16.safetensors"
  dl $K/Wan_2_2_I2V_A14B_LOW_lightx2v_4step_lora_260412_rank_64_fp16.safetensors "$M/loras/Wan_2_2_I2V_A14B_LOW_lightx2v_4step_lora_260412_rank_64_fp16.safetensors"
  dl $R/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors "$M/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
  dl $R/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors "$M/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"
fi
# --- Wan 2.2 Animate 14B fp8 (замена персонажа в чужом видео: движение + мимика +
# свет оригинала; шаблон ComfyUI video_wan2_2_14B_animate, 13.09.2026). Узлы
# (KJNodes, segment-anything-2, controlnet_aux) ставит animate_setup.sh
if want animate; then
  R=$HF/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files
  R1=$HF/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files
  K=$HF/Kijai/WanVideo_comfy/resolve/main
  mkdir -p "$M/clip_vision"
  dl $R/vae/wan_2.1_vae.safetensors "$M/vae/wan_2.1_vae.safetensors"
  dl $R/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors "$M/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
  dl $R1/clip_vision/clip_vision_h.safetensors "$M/clip_vision/clip_vision_h.safetensors"
  dl $K/Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors "$M/loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
  dl $K/LoRAs/Wan22_relight/WanAnimate_relight_lora_fp16.safetensors "$M/loras/WanAnimate_relight_lora_fp16.safetensors"
  dl $HF/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/Wan22Animate/Wan2_2-Animate-14B_fp8_e4m3fn_scaled_KJ.safetensors "$M/diffusion_models/Wan2_2-Animate-14B_fp8_e4m3fn_scaled_KJ.safetensors"
fi
# --- Index-AniSora V3.2: аниме-дообучение Wan 2.2 I2V (Apache-2.0), fp8 Kijai с родными
# ключами Wan — грузится UNETLoader; без lightx2v (уже дистиллирована, 8 шагов)
if want anisora; then
  R=$HF/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files
  A=$HF/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/I2V/AniSora
  dl $R/vae/wan_2.1_vae.safetensors "$M/vae/wan_2.1_vae.safetensors"
  dl $R/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors "$M/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
  dl $A/Wan2_2-I2V_AniSoraV3_2_HIGH_14B_fp8_e4m3fn_scaled_KJ.safetensors "$M/diffusion_models/Wan2_2-I2V_AniSoraV3_2_HIGH_14B_fp8_e4m3fn_scaled_KJ.safetensors"
  dl $A/Wan2_2-I2V_AniSoraV3_2_LOW_14B_fp8_e4m3fn_scaled_KJ.safetensors "$M/diffusion_models/Wan2_2-I2V_AniSoraV3_2_LOW_14B_fp8_e4m3fn_scaled_KJ.safetensors"
fi
# --- MiniMax H3: FL2VA (от первого кадра) и Ref2VA (по референсам), срезанные int8 по 21 ГБ,
# энкодер Qwen3-VL-32B nvfp4 15.7 ГБ, турбо-LoRA 544p. ~67 ГБ: диск пода ≥ 120 ГБ
if want h3; then
  H=$HF/Comfy-Org/MiniMax-H3/resolve/main
  dl $H/vae/minimax_h3_video_vae_fp16.safetensors "$M/vae/minimax_h3_video_vae_fp16.safetensors"
  dl $H/vae/minimax_h3_audio_vae_fp32.safetensors "$M/vae/minimax_h3_audio_vae_fp32.safetensors"
  dl $H/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors "$M/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
  dl $H/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors "$M/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
  dl $H/loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors "$M/loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
  dl $H/loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors "$M/loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
  dl $H/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors "$M/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"
  dl $H/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors "$M/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors"
fi
# --- Qwen-Image-Edit-2511 fp8 + Lightning 4 шага: панель манги → кадр аниме по листам героев
if want_image qwen_edit; then
  Q=$HF/Comfy-Org/Qwen-Image-Edit_ComfyUI/resolve/main/split_files
  QI=$HF/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files
  dl $QI/vae/qwen_image_vae.safetensors "$M/vae/qwen_image_vae.safetensors"
  dl $QI/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors "$M/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors"
  dl $HF/lightx2v/Qwen-Image-Edit-2511-Lightning/resolve/main/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors "$M/loras/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"
  dl $Q/diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors "$M/diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors"
fi
# --- FLUX.2 [dev] 32B fp8 + Mistral fp8: ~54 ГБ, карта от 48 ГБ (копия Comfy-Org без токена)
if want_image flux2_dev; then
  F=$HF/Comfy-Org/flux2-dev/resolve/main/split_files
  dl $F/vae/flux2-vae.safetensors "$M/vae/flux2-vae.safetensors"
  dl $F/text_encoders/mistral_3_small_flux2_fp8.safetensors "$M/text_encoders/mistral_3_small_flux2_fp8.safetensors"
  dl $F/diffusion_models/flux2_dev_fp8mixed.safetensors "$M/diffusion_models/flux2_dev_fp8mixed.safetensors"
fi

# --- ComfyUI нужной версии рядом со старым (модели/вход/выход — общие)
if [ ! -f "$NEW/main.py" ]; then
  git clone -q --depth 1 --branch $COMFY_REF https://github.com/comfyanonymous/ComfyUI "$NEW" || { echo "FAIL clone ComfyUI"; exit 1; }
  rm -rf "$NEW/models" "$NEW/input" "$NEW/output"
  ln -s "$M" "$NEW/models"; ln -s "$OLD/input" "$NEW/input"; ln -s "$OLD/output" "$NEW/output"
  (cd "$NEW" && python3 -m pip install -q -r requirements.txt 2>&1 | grep -v "notice" | tail -n 2)
  echo "ComfyUI $COMFY_REF ready"
else
  echo "have ComfyUI $NEW"
fi

# --- перезапуск: старый экземпляр на 8188 → новый
if ! curl -s http://127.0.0.1:8188/system_stats | grep -q "\"comfyui_version\": \"$COMFY_VER"; then
  pkill -f "main.py --listen 0.0.0.0 --port 8188" 2>/dev/null; sleep 2
  # --cache-lru 8: контейнер RunPod ограничен cgroup (62 ГБ RAM при 251 на хосте), а
  # «RAM pressure cache» ComfyUI смотрит на хост и растит кэш выходов узлов
  # (декодированные кадры клипа ≈1.3 ГБ каждый) до OOM-kill посреди партии
  # (Test 3). --cache-none лечило RAM, но роняло картинки с 3 до 10–15 с/кадр:
  # без кэша текстовый энкодер и кондишены пересчитываются на каждом вызове.
  # LRU на 8 выходов держит RAM в пределах ~10 ГБ и сохраняет скорость
  cd "$NEW" && nohup python3 main.py --listen 0.0.0.0 --port 8188 --enable-cors-header --cache-lru 8 > /workspace/out/comfy.log 2>&1 < /dev/null &
  for i in $(seq 1 60); do sleep 3; curl -s -o /dev/null http://127.0.0.1:8188/system_stats && break; done
fi
curl -s http://127.0.0.1:8188/system_stats | head -c 200; echo
# логгер VRAM для замеров
pgrep -f "nvidia-smi --query-gpu=timestamp" >/dev/null || nohup nvidia-smi --query-gpu=timestamp,memory.used,utilization.gpu --format=csv -l 2 > /workspace/out/nvsmi.csv 2>/dev/null &
df -h / | tail -1
echo SETUP_DONE
