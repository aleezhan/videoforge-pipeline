"""ComfyUI по HTTP на арендованной карте: картинки (klein 4B + LoRA персонажа) и
видео (LTX-2.5 distilled или Wan 2.2 + lightx2v).

Провайдер ничего не знает про RunPod: ему нужен адрес ComfyUI и ставка карты в
час. И то и другое лежит в `.gpu/pod.json`, который пишет `videoforge gpu up`
(providers/runpod_pod.py) — или задаётся `VF_COMFY_URL`/`VF_COMFY_RATE_USD_HR`
руками, если ComfyUI поднят иначе. Стоимость вызова = секунды генерации ×
ставка: карта арендована целиком, других счётчиков у неё нет.

Графы — в API-формате ComfyUI, собраны руками из официальных шаблонов и
проверены на живом поде (projects/_bench, Test 2, 08.09.2026). Числа времени в
PRICES — замеры того прогона на RTX PRO 4500 32 ГБ, а не прайс.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from videoforge.core.ffmpeg import run as ffmpeg_run
from videoforge.core.storyboard import ChainLink
from videoforge.providers.base import (
    GenResult,
    ImageProvider,
    MotionGuide,
    ProviderError,
    VideoProvider,
)

log = logging.getLogger("videoforge.comfy")

POD_STATE_FILE = ".gpu/pod.json"
DEFAULT_RATE_USD_HR = 0.72  # RTX PRO 4500 Blackwell 32 ГБ, Secure, EU-RO-1 — прайс 07.09.2026

# Замеры Test 2 (08.09.2026, RTX PRO 4500): секунд на единицу работы. Для
# прогноза бюджета; факт провайдер меряет сам на каждом вызове.
MEASURED_SEC: dict[str, float] = {
    "klein4b_1280x720": 1.8,  # 4 шага, LoRA rank 32 (Test 1c: 1.83–2.16 с)
    "ltx25_704p_per_frame": 39.0 / 121,  # 39 с на 121 кадр @24 fps, два прохода
    "wan480_per_frame": 52.5 / 81,  # 52.5 с на 81 кадр @16 fps
    "wan720_per_frame": 199.0 / 81,  # 199 с на 81 кадр @16 fps, пик VRAM 31.2 ГБ (Test 2)
    # Замер 17.09.2026 (anime_keyframe / anime_clip, RTX PRO 4500): AniSora 720p — 437–451 с
    # на 81 кадр, 8 шагов; Qwen-Image-Edit 4 шага — 20–22 с на кадр (первый с загрузкой
    # 31 с); klein с тремя референсами — 7–10 с, прогноз klein × (1 + картинок) сходится
    "anisora720_per_frame": 441.0 / 81,
    "anisora480_per_frame": 441.0 / 81 * (832 * 480) / (1280 * 720),  # оценка по площади
    # H3 турбо, 864×480, 124 кадра: 50 с на клип i2v (8 шагов) и 39–40 с ref (4 шага) на L40S
    # (OC-AU-1, 17.09.2026); первый клип с загрузкой — 60–78 с
    "h3_480p_per_frame": 50.0 / 124,
    "qwen_edit_1mp": 21.0,
    # anime_keyframe_recipe (17.09.2026, L40S 48 ГБ): FLUX.2 dev 32B fp8, 20 шагов, ~1 Мп —
    # 52–54 с только с панелью (первый с загрузкой 89 с), 84–126 с с одним-двумя
    # референсами; Qwen-Image-Edit без Lightning (40 шагов, cfg 4) там же — 91–99 с
    "flux2dev_1mp": 53.0,
}

# --- модели на поде (имена файлов в models/ ComfyUI; качает assets/gpu/comfy_setup.sh)
KLEIN_UNET = "flux-2-klein-4b.safetensors"
KLEIN_CLIP = "qwen_3_4b.safetensors"
KLEIN_VAE = "flux2-vae.safetensors"

LTX_UNET = "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors"
# Два декодера LTX-2.5: conv (по умолчанию) и диффузионный (шаблон ComfyUI).
# Замер Test 6 (08.09.2026, 6 пар при одном сиде, 2D и фотореал): кадры
# совпадают с точностью 1–1.5% (разница 2.5–4/255), conv на 10–17% быстрее и
# чуть резче по краям; чужой замер на 5090 — −22% времени и PSNR выше. Вернуть
# диффузионный: VF_COMFY_LTX_VAE=diffusion. Оба качает comfy_setup.sh
LTX_VAE = (
    "ltx-2.5-video-vae-bf16.safetensors"
    if os.environ.get("VF_COMFY_LTX_VAE", "").lower() in ("diffusion", "bf16")
    else "ltx-2.5-video-vae-conv-bf16.safetensors"
)
LTX_AVAE = "ltx-2.5-audio-vae-bf16.safetensors"
LTX_CLIP = "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors"
LTX_UPSCALER = "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"
LTX_SIGMAS_PASS1 = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"
LTX_SIGMAS_PASS2 = "0.85, 0.7250, 0.4219, 0.0"
LTX_FPS = 24

WAN_HIGH = "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
WAN_LOW = "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"
WAN_LORA_HIGH = "Wan_2_2_I2V_A14B_HIGH_lightx2v_4step_lora_260412_rank_64_fp16.safetensors"
WAN_LORA_LOW = "Wan_2_2_I2V_A14B_LOW_lightx2v_4step_lora_260412_rank_64_fp16.safetensors"
WAN_CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
WAN_VAE = "wan_2.1_vae.safetensors"
WAN_FPS = 16

# Index-AniSora V3.2 (bilibili, Apache-2.0) — дообученный на аниме Wan 2.2 I2V A14B,
# fp8 Kijai с родными ключами Wan (грузится UNETLoader, как файл Animate). Уже
# дистиллирован: без lightx2v, 8 шагов, shift 5, cfg 1, переключение экспертов на
# 0.9 при shift 5 → 3 шага high + 5 low (anisoraV3.2/README.md, configs/wan_i2v_A14B.py)
ANISORA_HIGH = "Wan2_2-I2V_AniSoraV3_2_HIGH_14B_fp8_e4m3fn_scaled_KJ.safetensors"
ANISORA_LOW = "Wan2_2-I2V_AniSoraV3_2_LOW_14B_fp8_e4m3fn_scaled_KJ.safetensors"
# при cfg 1 негатив Wan не работает; оставлен на случай варианта с cfg > 1
WAN_NEGATIVE = (
    "photorealistic, 3D render, photograph, blurry, distorted face, extra limbs, text, watermark"
)


@dataclass(frozen=True)
class VideoPreset:
    id: str
    width: int  # для 16:9; 9:16 — поворот
    height: int
    fps: int
    durations: tuple[int, ...]
    sec_per_frame: float  # замер, для прогноза
    family: str = "ltx"  # ltx | wan — какой граф собирать
    recipe: str = "lightx2v"  # у wan: чем сэмплировать — WAN_RECIPES


@dataclass(frozen=True)
class WanRecipe:
    """Веса и шаги графа Wan 2.2 I2V: базовые эксперты + lightx2v на 4 шага (2 + 2)
    или аниме-дообучение AniSora V3.2, уже дистиллированное (8 шагов, 3 + 5)."""

    high: str
    low: str
    lora_high: str | None
    lora_low: str | None
    steps: int
    switch: int  # шаг, на котором high-эксперт отдаёт латент low
    shift: float


WAN_RECIPES: dict[str, WanRecipe] = {
    "lightx2v": WanRecipe(WAN_HIGH, WAN_LOW, WAN_LORA_HIGH, WAN_LORA_LOW, 4, 2, 5.0),
    "anisora": WanRecipe(ANISORA_HIGH, ANISORA_LOW, None, None, 8, 3, 5.0),
}


# Длительности — по VRAM: на 32 ГБ 5-секундные клипы обеих моделей пикуют на
# 31 ГБ (Test 2); длиннее — только после замера (см. `videoforge gpu up --smoke`).
# wan720 в конвейере не используется (199 с на клип), но замер сравнивает и его.
VIDEO_PRESETS: dict[str, VideoPreset] = {
    # 2 с = 49 кадров: одному событию (удар) некуда растягиваться; замер cat_punch_speed
    "ltx25": VideoPreset(
        "ltx25", 1280, 704, LTX_FPS, (2, 3, 4, 5, 6), MEASURED_SEC["ltx25_704p_per_frame"], "ltx"
    ),
    "wan480": VideoPreset(
        "wan480", 832, 480, WAN_FPS, (3, 4, 5), MEASURED_SEC["wan480_per_frame"], "wan"
    ),
    "wan720": VideoPreset(
        "wan720", 1280, 720, WAN_FPS, (3, 4, 5), MEASURED_SEC["wan720_per_frame"], "wan"
    ),
    # аниме: AniSora обучена на 1280×720 (официальный размер), 480p — дешёвая проба
    "anisora720": VideoPreset(
        "anisora720",
        1280,
        720,
        WAN_FPS,
        (3, 4, 5),
        MEASURED_SEC["anisora720_per_frame"],
        "wan",
        "anisora",
    ),
    "anisora480": VideoPreset(
        "anisora480",
        832,
        480,
        WAN_FPS,
        (3, 4, 5),
        MEASURED_SEC["anisora480_per_frame"],
        "wan",
        "anisora",
    ),
}


def model_files(preset: VideoPreset) -> dict[str, str]:
    """Роль → файл весов, которыми рендерит этот пресет: запись дубля замера
    хранит их явно, чтобы «чем сделано» не зависело от того, что лежит в коде
    через месяц."""
    if preset.family == "wan":
        r = WAN_RECIPES[preset.recipe]
        files = {"unet_high": r.high, "unet_low": r.low}
        if r.lora_high and r.lora_low:
            files |= {"lora_high": r.lora_high, "lora_low": r.lora_low}
        return files | {"clip": WAN_CLIP, "vae": WAN_VAE}
    return {
        "unet": LTX_UNET,
        "vae": LTX_VAE,
        "audio_vae": LTX_AVAE,
        "clip": LTX_CLIP,
        "upscaler": LTX_UPSCALER,
    }


def i2v_plan(
    preset: VideoPreset,
    image_name: str,
    prompt: str,
    *,
    aspect_ratio: str,
    duration_sec: int,
    seed: int,
    prefix: str,
    audio: bool = False,
    negative: str | None = None,
    frame_rate_cond: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Граф картинка → клип для пресета плюс его условия (размер, кадры, fps,
    негатив, условие темпа). Одна точка на провайдер и на замер: что уходит на
    под в продакшене, то и сравнивается на полигоне."""
    w, h = size_for(aspect_ratio, preset.width, preset.height)
    frames = frames_for(duration_sec, preset.fps)
    if preset.family == "wan":
        if frame_rate_cond:
            raise ProviderError("условие темпа frame_rate_cond есть только у LTX")
        neg = WAN_NEGATIVE if negative is None else negative
        graph = wan_graph(
            image_name,
            prompt,
            neg,
            width=w,
            height=h,
            frames=frames,
            seed=seed,
            prefix=prefix,
            recipe=WAN_RECIPES[preset.recipe],
        )
    else:
        neg = ""
        graph = ltx_graph(
            image_name,
            prompt,
            width=w,
            height=h,
            frames=frames,
            seed=seed,
            prefix=prefix,
            audio=audio,
            frame_rate_cond=frame_rate_cond,
        )
    meta = {
        "preset": preset.id,
        "frames": frames,
        "fps": preset.fps,
        "size": f"{w}x{h}",
        "seed": seed,
        "negative": neg,
        "frame_rate_cond": float(frame_rate_cond or preset.fps),
    }
    return graph, meta


IC_LORA_UNION = "ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors"  # Lightricks, 655 МБ
CONTROL_SIGNALS = ("pose", "canny", "depth", "raw")
# Пиксельный апскейлер Lightricks (IC-LoRA Pixel Spatial Upscaler): дорисовывает
# детали, а не интерполирует. Для LTX-2.5 есть только ×2 (327 МБ); ×4 — адаптер
# LTX-2.3 (654 МБ), на 2.5 не обещан — как и Union Control, проверяет замер.
# Оба репозитория за воротами HF (gated: auto): лицензию принимает человек своим
# аккаунтом, под качает с HF_TOKEN (assets/gpu/iclora_setup.sh). 13.09.2026
IC_LORA_UPSCALE: dict[int, str] = {
    2: "ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors",
    4: "ltx-2.3-22b-ic-lora-pixel-spatial-upscaler-x4-0.9.safetensors",
}


def control_graph(
    image_name: str,
    reference_name: str,
    prompt: str,
    *,
    width: int,
    height: int,
    frames: int,
    seed: int,
    prefix: str,
    signal: str = "pose",
    lora: str = IC_LORA_UNION,
    strength: float = 1.0,
    two_stage: bool = False,
    vae_name: str | None = None,
) -> dict[str, Any]:
    """LTX-2.5 + IC-LoRA Union Control: движение из референсного видео.

    Повторяет официальный граф `LTX-2.5_ICLoRA_Union_Control_Distilled.json`
    (Lightricks/ComfyUI-LTXVideo, 12.09.2026) в API-формате: стартовый кадр идёт
    в латент как в обычном I2V, а `LTXAddVideoICLoRAGuide` добавляет к нему кадры
    управляющего сигнала — скелет (DWPose), контуры (Canny) или готовую
    последовательность (`raw`) из референсного видео; после первого прохода
    `LTXVCropGuides` вырезает управляющие токены. Разрешение и число кадров
    задаются здесь и обязаны совпадать с референсом (8n+1 кадров). Узлы
    `LTXICLoRALoaderModelOnly` и `LTXAddVideoICLoRAGuide` — из пакета
    ComfyUI-LTXVideo, аннотаторы — из comfyui_controlnet_aux
    (`assets/gpu/iclora_setup.sh`). Совместимость адаптера с нашей int8-сборкой
    трансформера документацией не обещана — это и проверяет замер.
    `two_stage=False` — декод после первого прохода (техпроверка, вдвое меньше
    памяти); True — апскейл ×2 и второй проход, как в шаблоне."""
    if signal not in CONTROL_SIGNALS:
        raise ProviderError(f"сигнал управления {signal!r}: {CONTROL_SIGNALS}")
    if (frames - 1) % 8:
        raise ProviderError(f"кадров должно быть 8n+1, а не {frames}")
    if width % 64 or height % 64:
        # латент 32 px на клетку, адаптер ref0.5 делит его ещё пополам: узел
        # отвергает нечётную сетку («Latent spatial size 17x30 must be divisible by 2»)
        raise ProviderError(f"размер {width}x{height} должен быть кратен 64")
    scale = {"upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}
    g: dict[str, Any] = {
        "u": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": LTX_UNET, "weight_dtype": "default"},
        },
        "ic": {
            "class_type": "LTXICLoRALoaderModelOnly",
            "inputs": {"model": ["u", 0], "lora_name": lora, "strength_model": 1.0},
        },
        "vv": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name or LTX_VAE}},
        "va": {"class_type": "VAELoader", "inputs": {"vae_name": LTX_AVAE}},
        "c": {"class_type": "CLIPLoader", "inputs": {"clip_name": LTX_CLIP, "type": "ltxv"}},
        "p": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["c", 0], "text": prompt}},
        "n": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["c", 0], "text": ""}},
        "cond": {
            "class_type": "LTXVConditioning",
            "inputs": {"positive": ["p", 0], "negative": ["n", 0], "frame_rate": float(LTX_FPS)},
        },
        "img": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "imgs": {"class_type": "ImageScale", "inputs": {"image": ["img", 0], **scale}},
        "ref": {"class_type": "LoadVideo", "inputs": {"file": reference_name}},
        "refc": {"class_type": "GetVideoComponents", "inputs": {"video": ["ref", 0]}},
        "refs": {"class_type": "ImageScale", "inputs": {"image": ["refc", 0], **scale}},
    }
    if signal == "pose":
        g["ann"] = {
            "class_type": "DWPreprocessor",
            "inputs": {
                "image": ["refs", 0],
                "detect_hand": "enable",
                "detect_body": "enable",
                "detect_face": "enable",
                "resolution": 512,
                "bbox_detector": "yolox_l.onnx",
                "pose_estimator": "dw-ll_ucoco_384_bs5.torchscript.pt",
                "scale_stick_for_xinsr_cn": "disable",
            },
        }
    elif signal == "canny":
        g["ann"] = {
            "class_type": "CannyEdgePreprocessor",
            "inputs": {
                "image": ["refs", 0],
                "low_threshold": 92,
                "high_threshold": 200,
                "resolution": 512,
            },
        }
    elif signal == "depth":
        # покадровая глубина Depth Anything V2 из controlnet_aux (шаблон Lightricks
        # берёт Video Depth Anything — отдельный пакет, для замера не нужен)
        g["ann"] = {
            "class_type": "DepthAnythingV2Preprocessor",
            "inputs": {
                "image": ["refs", 0],
                "ckpt_name": "depth_anything_v2_vitl.pth",
                "resolution": 512,
            },
        }
    ctrl_src: list[Any] = ["ann", 0] if signal != "raw" else ["refs", 0]
    g.update(
        {
            "ctrl": {"class_type": "ImageScale", "inputs": {"image": ctrl_src, **scale}},
            "lat1": {
                "class_type": "EmptyLTXVLatentVideo",
                "inputs": {"width": width, "height": height, "length": frames, "batch_size": 1},
            },
            "i2v1": {
                "class_type": "LTXVImgToVideoInplace",
                "inputs": {
                    "vae": ["vv", 0],
                    "image": ["imgs", 0],
                    "latent": ["lat1", 0],
                    "strength": 1.0,
                    "bypass": False,
                },
            },
            "guide": {
                "class_type": "LTXAddVideoICLoRAGuide",
                "inputs": {
                    "positive": ["cond", 0],
                    "negative": ["cond", 1],
                    "vae": ["vv", 0],
                    "latent": ["i2v1", 0],
                    "image": ["ctrl", 0],
                    "frame_idx": 0,
                    "strength": float(strength),
                    "latent_downscale_factor": ["ic", 1],
                    "crop": "disabled",
                    "use_tiled_encode": False,
                    "tile_size": 256,
                    "tile_overlap": 64,
                },
            },
            "aud": {
                "class_type": "LTXVEmptyLatentAudio",
                "inputs": {
                    "frames_number": frames,
                    "frame_rate": float(LTX_FPS),
                    "batch_size": 1,
                    "audio_vae": ["va", 0],
                },
            },
            "cat1": {
                "class_type": "LTXVConcatAVLatent",
                "inputs": {"video_latent": ["guide", 2], "audio_latent": ["aud", 0]},
            },
            "g1": {
                "class_type": "CFGGuider",
                "inputs": {
                    "model": ["ic", 0],
                    "positive": ["guide", 0],
                    "negative": ["guide", 1],
                    "cfg": 1.0,
                },
            },
            "ks": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler_ancestral"}},
            "sig1": {"class_type": "ManualSigmas", "inputs": {"sigmas": LTX_SIGMAS_PASS1}},
            "noise1": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
            "s1": {
                "class_type": "SamplerCustomAdvanced",
                "inputs": {
                    "noise": ["noise1", 0],
                    "guider": ["g1", 0],
                    "sampler": ["ks", 0],
                    "sigmas": ["sig1", 0],
                    "latent_image": ["cat1", 0],
                },
            },
            "sep1": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["s1", 0]}},
            "crop": {
                "class_type": "LTXVCropGuides",
                "inputs": {
                    "positive": ["guide", 0],
                    "negative": ["guide", 1],
                    "latent": ["sep1", 0],
                },
            },
        }
    )
    final: list[Any] = ["crop", 2]
    if two_stage:
        g.update(
            {
                "upl": {
                    "class_type": "LatentUpscaleModelLoader",
                    "inputs": {"model_name": LTX_UPSCALER},
                },
                "up": {
                    "class_type": "LTXVLatentUpsampler",
                    "inputs": {
                        "samples": ["crop", 2],
                        "upscale_model": ["upl", 0],
                        "vae": ["vv", 0],
                    },
                },
                "imgs2": {
                    "class_type": "ImageScale",
                    "inputs": {
                        "image": ["img", 0],
                        "upscale_method": "lanczos",
                        "width": width * 2,
                        "height": height * 2,
                        "crop": "center",
                    },
                },
                "i2v2": {
                    "class_type": "LTXVImgToVideoInplace",
                    "inputs": {
                        "vae": ["vv", 0],
                        "image": ["imgs2", 0],
                        "latent": ["up", 0],
                        "strength": 1.0,
                        "bypass": False,
                    },
                },
                "cat2": {
                    "class_type": "LTXVConcatAVLatent",
                    "inputs": {"video_latent": ["i2v2", 0], "audio_latent": ["sep1", 1]},
                },
                "g2": {
                    "class_type": "CFGGuider",
                    "inputs": {
                        "model": ["ic", 0],
                        "positive": ["crop", 0],
                        "negative": ["crop", 1],
                        "cfg": 1.0,
                    },
                },
                "sig2": {"class_type": "ManualSigmas", "inputs": {"sigmas": LTX_SIGMAS_PASS2}},
                "noise2": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed + 1}},
                "s2": {
                    "class_type": "SamplerCustomAdvanced",
                    "inputs": {
                        "noise": ["noise2", 0],
                        "guider": ["g2", 0],
                        "sampler": ["ks", 0],
                        "sigmas": ["sig2", 0],
                        "latent_image": ["cat2", 0],
                    },
                },
                "sep2": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["s2", 0]}},
            }
        )
        final = ["sep2", 0]
    g.update(
        {
            "dec": {
                "class_type": "VAEDecodeTiled",
                "inputs": {
                    "samples": final,
                    "vae": ["vv", 0],
                    "tile_size": 512,
                    "overlap": 64,
                    "temporal_size": 64,
                    "temporal_overlap": 16,
                },
            },
            "vid": {
                "class_type": "CreateVideo",
                "inputs": {"images": ["dec", 0], "fps": float(LTX_FPS)},
            },
            "save": {
                "class_type": "SaveVideo",
                "inputs": {
                    "video": ["vid", 0],
                    "filename_prefix": prefix,
                    "format": "mp4",
                    "codec": "h264",
                },
            },
        }
    )
    return g


# Рецепт управления в конвейере — двухпроходный из замера mocap_follow_8s
# (13.09.2026, ветка skel_2p): первый проход 384×704, латентный апскейл ×2 и
# досэмпл → 768×1408. Ближе всего к 704×1280 обычного клипа, и лицо перестаёт
# быть пятном; 193 кадра (8 с) карта 32 ГБ на нём держала. Один проход
# 576×1024 (ветка skel) — техпроверка, не рецепт. Стороны кратны 64: адаптер
# ref0.5 делит латент пополам
CONTROL_WIDTH, CONTROL_HEIGHT = 704, 384  # 16:9; 9:16 — поворот (size_for)
CONTROL_TWO_STAGE = True
# прогноз времени: обычный проход плюс аннотатор и управляющие токены — ×2
# против i2v того же числа кадров (так же считает bench/run.py); замер уточнит
CONTROL_COST_FACTOR = 2.0


def _require_ltx(preset: VideoPreset) -> None:
    """Адаптер IC-LoRA Union Control сделан под LTX-2.5; у Wan управления по
    референсу в конвейере нет, и подменять его обычным i2v молча нельзя."""
    if preset.family != "ltx":
        raise ProviderError(
            f"референс движения умеет только LTX-2.5 (IC-LoRA), а не пресет {preset.id}: "
            "VF_COMFY_VIDEO=ltx25"
        )


def control_plan(
    preset: VideoPreset,
    image_name: str,
    reference_name: str,
    prompt: str,
    *,
    aspect_ratio: str,
    duration_sec: int,
    seed: int,
    prefix: str,
    signal: str,
    strength: float = 1.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Граф кадр + референс → клип и его условия — как `i2v_plan`, но по
    `control_graph`: что стадия video шлёт на под, то и гоняет замер
    (`bench/run.py: ComfyControl`). Референс к этому моменту уже подогнан под
    `frames` (см. `conform_reference`): граф длину не проверяет, а под
    отвергает несовпадение уже после загрузки моделей."""
    _require_ltx(preset)
    w, h = size_for(aspect_ratio, CONTROL_WIDTH, CONTROL_HEIGHT)
    frames = frames_for(duration_sec, preset.fps)
    graph = control_graph(
        image_name,
        reference_name,
        prompt,
        width=w,
        height=h,
        frames=frames,
        seed=seed,
        prefix=prefix,
        signal=signal,
        strength=strength,
        two_stage=CONTROL_TWO_STAGE,
    )
    meta = {
        "preset": preset.id,
        "frames": frames,
        "fps": preset.fps,
        "size": f"{w}x{h}" + ("→×2" if CONTROL_TWO_STAGE else ""),
        "seed": seed,
        "signal": signal,
        "strength": float(strength),
        "reference": reference_name,
        "ic_lora": IC_LORA_UNION,
    }
    return graph, meta


def conform_reference(src: Path, out: Path, *, frames: int, fps: int = LTX_FPS) -> Path:
    """Референс под клип: ровно `frames` кадров при `fps`.

    Адаптер требует, чтобы референс совпадал с латентом по числу кадров
    (8n+1), а файл на диске — какой есть: скелет из захвата движений
    отрендерен под 73 или 193 кадра замера, видео с телефона — 30 fps и
    сколько сняли. Лишнее режется с конца, недостающее — удержание последнего
    кадра (`tpad clone`): поза замирает, а не обрывается в чёрный. Первый кадр
    не трогается — стартовый кадр сцены обязан стоять в нём
    (`storyboard.MotionReference`). Звук долой: у скелета его нет, у видео с
    телефона он модели не нужен. crf 12 — как рендерит `bench/skeleton.py`:
    тонкие линии скелета от сжатия расплываются."""
    if (frames - 1) % 8:
        raise ProviderError(f"кадров должно быть 8n+1, а не {frames}")
    out.parent.mkdir(parents=True, exist_ok=True)
    pad = f"{frames / fps + 1.0:.3f}"  # с запасом: -frames:v остановит раньше
    ffmpeg_run(
        [
            "-i", str(src),
            "-vf", f"fps={fps},tpad=stop_mode=clone:stop_duration={pad}",
            "-frames:v", str(frames), "-an",
            "-c:v", "libx264", "-preset", "fast", "-crf", "12", "-pix_fmt", "yuv420p",
            str(out),
        ]
    )  # fmt: skip
    return out


def upscale_graph(
    video_name: str,
    prompt: str,
    *,
    factor: int,
    width: int,
    height: int,
    frames: int,
    seed: int,
    prefix: str,
    lora: str = "",
    strength: float = 1.0,
    skip_sigmas: int = 0,
    vae_name: str | None = None,
) -> dict[str, Any]:
    """LTX-2.5 + IC-LoRA Pixel Spatial Upscaler: клип → тот же клип в `factor`
    раз крупнее, детали дорисованы моделью.

    Повторяет шаблон `LTX-2.3_ICLoRA_Pixel_Spatial_Upscaler_Distilled.json`
    (Lightricks/ComfyUI-LTXVideo, 13.09.2026) в API-формате: кадры исходного
    клипа растягиваются lanczos-ом до целевого размера `width`×`height`,
    кодируются VAE в стартовый латент и они же уходят в `LTXAddVideoICLoRAGuide`
    как направляющие — адаптер сжимает их в `factor` раз (выход
    `latent_downscale_factor` загрузчика), то есть модель видит исходник в его
    настоящем разрешении и рисует целевое. Полный проход по сигмам первого
    прохода; `skip_sigmas` убирает верхние сигмы — «ближе к исходнику», как
    советует заметка шаблона; `strength` — сила направляющих. Аудио пустое: у
    наших дублей звука нет. Целевой размер кратен 32·factor — иначе направляющий
    латент не делится на factor."""
    if factor not in IC_LORA_UPSCALE:
        raise ProviderError(f"апскейл ×{factor}: есть только {sorted(IC_LORA_UPSCALE)}")
    if (frames - 1) % 8:
        raise ProviderError(f"кадров должно быть 8n+1, а не {frames}")
    step = 32 * factor
    if width % step or height % step:
        raise ProviderError(f"размер {width}x{height} должен быть кратен {step} (32·{factor})")
    sigmas = [s.strip() for s in LTX_SIGMAS_PASS1.split(",")]
    if not 0 <= skip_sigmas < len(sigmas) - 1:
        raise ProviderError(f"skip_sigmas {skip_sigmas}: сигм {len(sigmas)}, убрать можно не все")
    scale = {"upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}
    return {
        "u": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": LTX_UNET, "weight_dtype": "default"},
        },
        "ic": {
            "class_type": "LTXICLoRALoaderModelOnly",
            "inputs": {
                "model": ["u", 0],
                "lora_name": lora or IC_LORA_UPSCALE[factor],
                "strength_model": 1.0,
            },
        },
        "vv": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name or LTX_VAE}},
        "va": {"class_type": "VAELoader", "inputs": {"vae_name": LTX_AVAE}},
        "c": {"class_type": "CLIPLoader", "inputs": {"clip_name": LTX_CLIP, "type": "ltxv"}},
        "p": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["c", 0], "text": prompt}},
        "n": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["c", 0], "text": ""}},
        "cond": {
            "class_type": "LTXVConditioning",
            "inputs": {"positive": ["p", 0], "negative": ["n", 0], "frame_rate": float(LTX_FPS)},
        },
        "ref": {"class_type": "LoadVideo", "inputs": {"file": video_name}},
        "refc": {"class_type": "GetVideoComponents", "inputs": {"video": ["ref", 0]}},
        "refs": {"class_type": "ImageScale", "inputs": {"image": ["refc", 0], **scale}},
        "enc": {"class_type": "VAEEncode", "inputs": {"pixels": ["refs", 0], "vae": ["vv", 0]}},
        "guide": {
            "class_type": "LTXAddVideoICLoRAGuide",
            "inputs": {
                "positive": ["cond", 0],
                "negative": ["cond", 1],
                "vae": ["vv", 0],
                "latent": ["enc", 0],
                "image": ["refs", 0],
                "frame_idx": 0,
                "strength": float(strength),
                "latent_downscale_factor": ["ic", 1],
                "crop": "disabled",
                "use_tiled_encode": False,
                "tile_size": 256,
                "tile_overlap": 64,
            },
        },
        "aud": {
            "class_type": "LTXVEmptyLatentAudio",
            "inputs": {
                "frames_number": frames,
                "frame_rate": float(LTX_FPS),
                "batch_size": 1,
                "audio_vae": ["va", 0],
            },
        },
        "cat1": {
            "class_type": "LTXVConcatAVLatent",
            "inputs": {"video_latent": ["guide", 2], "audio_latent": ["aud", 0]},
        },
        "g1": {
            "class_type": "CFGGuider",
            "inputs": {
                "model": ["ic", 0],
                "positive": ["guide", 0],
                "negative": ["guide", 1],
                "cfg": 1.0,
            },
        },
        "ks": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler_ancestral"}},
        "sig1": {
            "class_type": "ManualSigmas",
            "inputs": {"sigmas": ", ".join(sigmas[skip_sigmas:])},
        },
        "noise1": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "s1": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["noise1", 0],
                "guider": ["g1", 0],
                "sampler": ["ks", 0],
                "sigmas": ["sig1", 0],
                "latent_image": ["cat1", 0],
            },
        },
        "sep1": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["s1", 0]}},
        "crop": {
            "class_type": "LTXVCropGuides",
            "inputs": {"positive": ["guide", 0], "negative": ["guide", 1], "latent": ["sep1", 0]},
        },
        "dec": {
            "class_type": "VAEDecodeTiled",
            "inputs": {
                "samples": ["crop", 2],
                "vae": ["vv", 0],
                "tile_size": 512,
                "overlap": 64,
                "temporal_size": 64,
                "temporal_overlap": 16,
            },
        },
        "vid": {
            "class_type": "CreateVideo",
            "inputs": {"images": ["dec", 0], "fps": float(LTX_FPS)},
        },
        "save": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["vid", 0],
                "filename_prefix": prefix,
                "format": "mp4",
                "codec": "h264",
            },
        },
    }


ANIMATE_UNET = "Wan2_2-Animate-14B_fp8_e4m3fn_scaled_KJ.safetensors"  # Kijai, 18.4 ГБ
ANIMATE_LIGHTX2V = "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
ANIMATE_RELIGHT = "WanAnimate_relight_lora_fp16.safetensors"
WAN_CLIP_VISION = "clip_vision_h.safetensors"
ANIMATE_SEGMENT = 77  # кадров за один проход WanAnimateToVideo (4n+1)
ANIMATE_OVERLAP = 5  # continue_motion_max_frames: столько кадров продолжение берёт из прошлого
# стандартный негатив Wan из шаблона (по-китайски — так модель училась)
WAN_ANIMATE_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，"
    "低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，"
    "毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)


def animate_segments(frames: int) -> int:
    """Сколько проходов по 77 кадров покрывают клип: продолжение повторяет
    ANIMATE_OVERLAP кадров прошлого прохода, поэтому каждый следующий даёт
    77 − 5 новых."""
    if frames <= ANIMATE_SEGMENT:
        return 1
    step = ANIMATE_SEGMENT - ANIMATE_OVERLAP
    return 1 + -(-(frames - ANIMATE_SEGMENT) // step)


def animate_graph(
    image_name: str,
    video_name: str,
    prompt: str,
    *,
    width: int,
    height: int,
    frames: int,
    fps: float,
    seed: int,
    prefix: str,
    points_positive: Sequence[tuple[int, int]],
    points_negative: Sequence[tuple[int, int]] = (),
    steps: int = 6,
    negative: str = WAN_ANIMATE_NEGATIVE,
    mask_grow: int = 10,
    mask_block: int = 32,
    relight: float = 1.0,
) -> dict[str, Any]:
    """Wan 2.2 Animate, режим замены (mix): персонаж с картинки встаёт на место
    человека в чужом видео — тело по скелету DWPose, мимика по точкам лица,
    фон и свет — кадры оригинала с вырезанным человеком (маска SAM2 по точкам
    первого кадра, растянутая на все кадры), relight LoRA подгоняет освещение.

    Повторяет шаблон ComfyUI `video_wan2_2_14B_animate` (13.09.2026) в
    API-формате без подграфов: первый проход WanAnimateToVideo на 77 кадров,
    дальше продолжения по `video_frame_offset` с `continue_motion` из
    предыдущего декода — столько, сколько нужно на `frames` (`animate_segments`).
    Кадры видео масштабируются к `width`×`height` (кратно 16), скелет и лицо
    рисуются в том же размере (PixelPerfectResolution). Точки маски — в
    координатах этого размера. Сэмплер как в шаблоне: lightx2v-дистилляция,
    6 шагов, cfg 1, euler/simple, shift 8. Звук — дорожка исходника в CreateVideo."""
    if width % 16 or height % 16:
        raise ProviderError(f"размер {width}x{height} должен быть кратен 16")
    if not points_positive:
        raise ProviderError("нужна хотя бы одна точка на персонаже (points_positive)")
    n_seg = animate_segments(frames)
    pts = json.dumps([{"x": int(x), "y": int(y)} for x, y in points_positive])
    neg_pts = json.dumps([{"x": int(x), "y": int(y)} for x, y in points_negative])
    g: dict[str, Any] = {
        "u": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": ANIMATE_UNET, "weight_dtype": "default"},
        },
        "lx": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": ["u", 0], "lora_name": ANIMATE_LIGHTX2V, "strength_model": 1.0},
        },
        "rl": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["lx", 0],
                "lora_name": ANIMATE_RELIGHT,
                "strength_model": float(relight),
            },
        },
        "ms": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["rl", 0], "shift": 8.0}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": WAN_CLIP, "type": "wan"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": WAN_VAE}},
        "cv": {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": WAN_CLIP_VISION}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": negative}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "cve": {
            "class_type": "CLIPVisionEncode",
            "inputs": {"clip_vision": ["cv", 0], "image": ["img", 0], "crop": "none"},
        },
        "ref": {"class_type": "LoadVideo", "inputs": {"file": video_name}},
        "refc": {"class_type": "GetVideoComponents", "inputs": {"video": ["ref", 0]}},
        "frames": {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["refc", 0],
                "upscale_method": "lanczos",
                "width": width,
                "height": height,
                "crop": "center",
            },
        },
        "res": {
            "class_type": "PixelPerfectResolution",
            "inputs": {
                "original_image": ["refc", 0],
                "image_gen_width": width,
                "image_gen_height": height,
                "resize_mode": "Just Resize",
            },
        },
        "face": {
            "class_type": "DWPreprocessor",
            "inputs": {
                "image": ["frames", 0],
                "detect_hand": "disable",
                "detect_body": "disable",
                "detect_face": "enable",
                "resolution": ["res", 0],
                "bbox_detector": "yolox_l.onnx",
                "pose_estimator": "dw-ll_ucoco_384_bs5.torchscript.pt",
                "scale_stick_for_xinsr_cn": "disable",
            },
        },
        "pose": {
            "class_type": "DWPreprocessor",
            "inputs": {
                "image": ["frames", 0],
                "detect_hand": "enable",
                "detect_body": "enable",
                "detect_face": "disable",
                "resolution": ["res", 0],
                "bbox_detector": "yolox_l.onnx",
                "pose_estimator": "dw-ll_ucoco_384_bs5.torchscript.pt",
                "scale_stick_for_xinsr_cn": "disable",
            },
        },
        "sam": {
            "class_type": "DownloadAndLoadSAM2Model",
            "inputs": {
                "model": "sam2_hiera_base_plus.safetensors",
                "segmentor": "video",
                "device": "cuda",
                "precision": "fp16",
            },
        },
        "pts": {"class_type": "PrimitiveString", "inputs": {"value": pts}},
        "npts": {"class_type": "PrimitiveString", "inputs": {"value": neg_pts}},
        "seg": {
            "class_type": "Sam2Segmentation",
            "inputs": {
                "sam2_model": ["sam", 0],
                "image": ["frames", 0],
                "keep_model_loaded": True,
                "coordinates_positive": ["pts", 0],
                "individual_objects": False,
                **({"coordinates_negative": ["npts", 0]} if points_negative else {}),
            },
        },
        "grow": {
            "class_type": "GrowMask",
            "inputs": {"mask": ["seg", 0], "expand": int(mask_grow), "tapered_corners": True},
        },
        "mask": {
            "class_type": "BlockifyMask",
            "inputs": {"masks": ["grow", 0], "block_size": int(mask_block)},
        },
        "bg": {
            "class_type": "DrawMaskOnImage",
            "inputs": {"image": ["frames", 0], "mask": ["mask", 0], "color": "0, 0, 0"},
        },
    }
    common = {
        "positive": ["pos", 0],
        "negative": ["neg", 0],
        "vae": ["vae", 0],
        "width": width,
        "height": height,
        "length": ANIMATE_SEGMENT,
        "batch_size": 1,
        "clip_vision_output": ["cve", 0],
        "reference_image": ["img", 0],
        "face_video": ["face", 0],
        "pose_video": ["pose", 0],
        "continue_motion_max_frames": ANIMATE_OVERLAP,
        "background_video": ["bg", 0],
        "character_mask": ["mask", 0],
    }
    images: list[Any] = []
    for i in range(n_seg):
        a, k, t, d, b = f"a{i}", f"k{i}", f"t{i}", f"d{i}", f"b{i}"
        extra: dict[str, Any] = {"video_frame_offset": 0}
        if i:
            extra = {"video_frame_offset": [f"a{i - 1}", 5], "continue_motion": [f"d{i - 1}", 0]}
        g[a] = {"class_type": "WanAnimateToVideo", "inputs": {**common, **extra}}
        g[k] = {
            "class_type": "KSampler",
            "inputs": {
                "model": ["ms", 0],
                "positive": [a, 0],
                "negative": [a, 1],
                "latent_image": [a, 2],
                "seed": seed + i,
                "steps": int(steps),
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        }
        g[t] = {
            "class_type": "TrimVideoLatent",
            "inputs": {"samples": [k, 0], "trim_amount": [a, 3]},
        }
        g[d] = {"class_type": "VAEDecode", "inputs": {"samples": [t, 0], "vae": ["vae", 0]}}
        # первые trim_image кадров продолжения — повтор хвоста прошлого прохода
        g[b] = {
            "class_type": "ImageFromBatch",
            "inputs": {"image": [d, 0], "batch_index": [a, 4], "length": 4096},
        }
        images.append([b, 0])
    out = images[0]
    for i in range(1, n_seg):
        g[f"cat{i}"] = {"class_type": "ImageBatch", "inputs": {"image1": out, "image2": images[i]}}
        out = [f"cat{i}", 0]
    # последний проход длиннее остатка клипа — хвост за концом исходника долой
    g["fit"] = {
        "class_type": "ImageFromBatch",
        "inputs": {"image": out, "batch_index": 0, "length": int(frames)},
    }
    out = ["fit", 0]
    g["vid"] = {
        "class_type": "CreateVideo",
        "inputs": {"images": out, "audio": ["refc", 1], "fps": float(fps)},
    }
    g["save"] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["vid", 0],
            "filename_prefix": prefix,
            "format": "mp4",
            "codec": "h264",
        },
    }
    return g


# ------------------------------------------------------------------ аниме по манге
# Замеры anime_keyframe / anime_clip (T-66, 17.09.2026): панель манги → кадр аниме →
# клип. Графы переписаны с официальных шаблонов workflow_templates (Comfy-Org) и
# сверены с исходниками ComfyUI v0.36.0; на поде до первого прогона не проверялись.

# Qwen-Image-Edit-2511 (Apache-2.0): панель и листы героев — до трёх картинок в
# TextEncodeQwenImageEditPlus; Lightning 4 шага от lightx2v, cfg 1
QWEN_EDIT_UNET = "qwen_image_edit_2511_fp8mixed.safetensors"  # 20.5 ГБ
QWEN_EDIT_CLIP = "qwen_2.5_vl_7b_fp8_scaled.safetensors"  # 9.4 ГБ
QWEN_EDIT_VAE = "qwen_image_vae.safetensors"
QWEN_EDIT_LIGHTNING = "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"


def qwen_edit_graph(
    image_names: list[str],
    prompt: str,
    *,
    width: int,
    height: int,
    seed: int,
    prefix: str,
    lightning: bool = True,
) -> dict[str, Any]:
    """Qwen-Image-Edit-2511 по шаблону image_qwen_image_edit_2511: AuraFlow shift 3.1
    → CFGNorm → (Lightning) → KSampler euler/simple. Первая картинка — то, что
    перерисовать (панель), остальные — референсы; позитив и негатив кодируются с
    теми же картинками и помечаются index_timestep_zero. Размер выхода задаёт
    пустой латент, а не панель: панель бывает квадратной, кадр серии — 16:9.
    Без Lightning — 40 шагов и cfg 4, как в шаблоне."""
    if not 1 <= len(image_names) <= 3:
        raise ProviderError(f"Qwen-Image-Edit берёт 1–3 картинки, дали {len(image_names)}")
    model: list[Any] = ["cfgnorm", 0]
    g: dict[str, Any] = {
        "unet": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": QWEN_EDIT_UNET, "weight_dtype": "default"},
        },
        "clip": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": QWEN_EDIT_CLIP, "type": "qwen_image", "device": "default"},
        },
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_EDIT_VAE}},
        "msa": {
            "class_type": "ModelSamplingAuraFlow",
            "inputs": {"model": ["unet", 0], "shift": 3.1},
        },
        "cfgnorm": {"class_type": "CFGNorm", "inputs": {"model": ["msa", 0], "strength": 1.0}},
    }
    if lightning:
        g["lora"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": model, "lora_name": QWEN_EDIT_LIGHTNING, "strength_model": 1.0},
        }
        model = ["lora", 0]
    images: dict[str, Any] = {}
    for i, name in enumerate(image_names, 1):
        g[f"img{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        images[f"image{i}"] = [f"img{i}", 0]
    g["img1s"] = {"class_type": "FluxKontextImageScale", "inputs": {"image": ["img1", 0]}}
    images["image1"] = ["img1s", 0]
    for key, text in (("pos", prompt), ("neg", "")):
        g[key] = {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"clip": ["clip", 0], "vae": ["vae", 0], "prompt": text, **images},
        }
        g[f"{key}_m"] = {
            "class_type": "FluxKontextMultiReferenceLatentMethod",
            "inputs": {"conditioning": [key, 0], "reference_latents_method": "index_timestep_zero"},
        }
    return g | {
        "lat": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "ks": {
            "class_type": "KSampler",
            "inputs": {
                "model": model,
                "positive": ["pos_m", 0],
                "negative": ["neg_m", 0],
                "latent_image": ["lat", 0],
                "seed": seed,
                "steps": 4 if lightning else 40,
                "cfg": 1.0 if lightning else 4.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "save": {
            "class_type": "SaveImage",
            "inputs": {"images": ["dec", 0], "filename_prefix": prefix},
        },
    }


# MiniMax H3 (открытые веса 03.08.2026; лицензия исключает ЕС, Великобританию, Корею
# и США — под только вне них, links/minimax_h3.yaml). Два чекпойнта: FL2VA — от
# первого кадра, Ref2VA — по картинкам-референсам (<Picture N> в промпте). Турбо-LoRA
# 544p обучены при shift 12/3 — это умолчание модели, узел сдвига не нужен.
H3_UNET = {
    "i2v": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",  # 21 ГБ
    "ref": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",  # 21 ГБ
}
H3_TURBO = {
    "i2v": ("minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors", 8),
    "ref": ("minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors", 4),
}
# Турбо 768p (1344×768): мелкие лица на общих планах плывут в 864×480 у всех моделей
# (оценка владельца anime_clip p5, 17.09.2026). Обучена при shift 6/3, а умолчание модели
# 12/3 — нужен MiniMaxH3SigmaShift (README ModelTC/Minimax-H3-Turbo). Только FL2VA.
H3_TURBO_768 = ("minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors", 4)
H3_SHIFT_768 = (6.0, 3.0)
H3_CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"  # 15.7 ГБ, Blackwell не нужен
H3_VAE = "minimax_h3_video_vae_fp16.safetensors"
H3_AVAE = "minimax_h3_audio_vae_fp32.safetensors"
H3_FPS = 24
H3_MAX_REFS = 9


def h3_frames(duration_sec: int) -> int:
    """Длина H3 — 17k + 5 кадров при 24 fps (узел сам её так округляет): 5 с → 124."""
    k = max(1, round((duration_sec * H3_FPS - 5) / 17))
    return 17 * k + 5


def h3_graph(
    mode: str,
    prompt: str,
    *,
    width: int,
    height: int,
    frames: int,
    seed: int,
    prefix: str,
    first_frame: str = "",
    refs: list[str] | None = None,
    turbo: bool = True,
    ref_image_size: str = "match",
    turbo_768: bool = False,
) -> dict[str, Any]:
    """Граф H3 по шаблонам ComfyUI: BasicScheduler simple → res_multistep →
    BasicGuider (без негатива и cfg) → один латент на VAEDecode и VAEDecodeAudio →
    CreateVideo 24 fps со звуком. `i2v` — MiniMaxH3ImageToVideo с `first_frame`;
    `ref` — MiniMaxH3ReferenceToVideo, картинки в `ref_images.ref_image_0..8`,
    в промпте они <Picture 1..N>. Турбо: i2v 8 шагов, ref 4 шага (v0.1); без турбо
    — 20 шагов шаблона. `turbo_768` — турбо 768p для i2v: 4 шага и сдвиг сигм 6/3."""
    if mode not in H3_UNET:
        raise ProviderError(f"режим H3 '{mode}': {sorted(H3_UNET)}")
    refs = refs or []
    if mode == "i2v" and not first_frame:
        raise ProviderError("H3 i2v: нужен первый кадр")
    if mode == "ref" and not 1 <= len(refs) <= H3_MAX_REFS:
        raise ProviderError(f"H3 ref: картинок-референсов 1–{H3_MAX_REFS}, дали {len(refs)}")
    if turbo_768 and (mode != "i2v" or not turbo):
        raise ProviderError("H3: турбо 768p есть только у i2v с турбо")
    lora_name, turbo_steps = H3_TURBO_768 if turbo_768 else H3_TURBO[mode]
    model: list[Any] = ["unet", 0]
    g: dict[str, Any] = {
        "unet": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": H3_UNET[mode], "weight_dtype": "default"},
        },
        "clip": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": H3_CLIP, "type": "minimax", "device": "default"},
        },
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VAE}},
        "avae": {"class_type": "VAELoader", "inputs": {"vae_name": H3_AVAE}},
    }
    if turbo:
        g["lora"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": model, "lora_name": lora_name, "strength_model": 1.0},
        }
        model = ["lora", 0]
    if turbo_768:
        g["shift"] = {
            "class_type": "MiniMaxH3SigmaShift",
            "inputs": {
                "model": model,
                "shift_video": H3_SHIFT_768[0],
                "shift_audio": H3_SHIFT_768[1],
            },
        }
        model = ["shift", 0]
    common = {
        "clip": ["clip", 0],
        "vae": ["vae", 0],
        "prompt": prompt,
        "width": width,
        "height": height,
        "length": frames,
    }
    if mode == "i2v":
        g["first"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
        g["cond"] = {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {**common, "first_frame": ["first", 0]},
        }
    else:
        inputs: dict[str, Any] = {
            **common,
            "audio_vae": ["avae", 0],
            "ref_image_size": ref_image_size,
        }
        for i, name in enumerate(refs):
            g[f"ref{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            inputs[f"ref_images.ref_image_{i}"] = [f"ref{i}", 0]
        g["cond"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": inputs}
    return g | {
        "sch": {
            "class_type": "BasicScheduler",
            "inputs": {
                "model": model,
                "scheduler": "simple",
                "steps": turbo_steps if turbo else 20,
                "denoise": 1.0,
            },
        },
        "ks": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "guider": {
            "class_type": "BasicGuider",
            "inputs": {"model": model, "conditioning": ["cond", 0]},
        },
        "s": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["noise", 0],
                "guider": ["guider", 0],
                "sampler": ["ks", 0],
                "sigmas": ["sch", 0],
                "latent_image": ["cond", 1],
            },
        },
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["s", 0], "vae": ["vae", 0]}},
        "adec": {
            "class_type": "VAEDecodeAudio",
            "inputs": {"samples": ["s", 0], "vae": ["avae", 0]},
        },
        "vid": {
            "class_type": "CreateVideo",
            "inputs": {
                "images": ["dec", 0],
                "audio": ["adec", 0],
                "fps": float(H3_FPS),
            },
        },
        "save": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["vid", 0],
                "filename_prefix": prefix,
                "format": "mp4",
                "codec": "h264",
            },
        },
    }


def with_image_name(graph: dict[str, Any], image_name: str) -> dict[str, Any]:
    """Тот же граф с другим именем загруженного кадра (ComfyUI может переименовать
    загрузку при совпадении имён) — копия, исходный план не трогается."""
    out = json.loads(json.dumps(graph))
    for node in out.values():
        if isinstance(node, dict) and node.get("class_type") == "LoadImage":
            node["inputs"]["image"] = image_name
    return out


def frames_for(duration_sec: int, fps: int) -> int:
    """Оба семейства хотят 8k+1 кадров; берём ближайшее не короче длительности."""
    n = duration_sec * fps + 1
    return n if (n - 1) % 8 == 0 else ((n - 1) // 8 + 1) * 8 + 1


@dataclass
class PodState:
    base_url: str
    rate_usd_hr: float = DEFAULT_RATE_USD_HR
    lora: str = ""  # имя файла LoRA персонажа в models/loras на поде
    lora_strength: float = 1.0
    video: str = "ltx25"
    pod_id: str = ""

    @classmethod
    def load(cls, root: Path | None = None) -> PodState:
        """`VF_COMFY_URL` побеждает файл; без того и другого — понятная ошибка."""
        url = os.environ.get("VF_COMFY_URL", "")
        rate = float(os.environ.get("VF_COMFY_RATE_USD_HR", "0") or 0)
        if url:
            return cls(
                base_url=url.rstrip("/"),
                rate_usd_hr=rate or DEFAULT_RATE_USD_HR,
                lora=os.environ.get("VF_COMFY_LORA", ""),
                video=os.environ.get("VF_COMFY_VIDEO", "ltx25"),
            )
        from videoforge.paths import ROOT

        path = (root or ROOT) / POD_STATE_FILE
        if not path.exists():
            raise ProviderError(
                "ComfyUI не поднят: запусти `videoforge gpu up <проект>` "
                "(или задай VF_COMFY_URL для своего ComfyUI)"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            base_url=str(data["base_url"]).rstrip("/"),
            rate_usd_hr=float(data.get("rate_usd_hr") or rate or DEFAULT_RATE_USD_HR),
            lora=str(data.get("lora") or ""),
            lora_strength=float(data.get("lora_strength") or 1.0),
            video=str(data.get("video") or os.environ.get("VF_COMFY_VIDEO", "ltx25")),
            pod_id=str(data.get("pod_id") or ""),
        )


# Ожидание результата (`ComfyClient.wait`). Сбои опроса подряд дольше
# DEAD_AFTER_S — процесс на поде мёртв. 60 с: моргание прокси Cloudflare длится
# секунды, перезапуск ComfyUI руками — десятки секунд, но после него задача всё
# равно потеряна; а минута ложного ожидания стоит $0.01 против получаса карты.
DEAD_AFTER_S = 60.0
POLL_TIMEOUT_S = 60.0  # один опрос дольше — сбой: висящий прокси тоже молчание
LOST_AFTER_S = 30.0  # история пуста дольше — сверить с очередью, знает ли под задачу


class _NoAnswer(Exception):
    """Опрос ComfyUI не дал внятного ответа: обрыв, код не 200, не JSON."""


class ComfyClient:
    """Минимум API ComfyUI: залить картинку, поставить граф, дождаться, скачать."""

    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)

    def system_stats(self) -> dict[str, Any]:
        r = self.http.get(f"{self.base}/system_stats")
        r.raise_for_status()
        return r.json()

    def object_info(self) -> dict[str, Any]:
        r = self.http.get(f"{self.base}/object_info")
        r.raise_for_status()
        return r.json()

    def upload_image(self, path: Path, name: str | None = None) -> str:
        """`name` — имя на поде: листы разных героев зовутся одинаково
        (`full_body_turnaround.png`), и второй затёр бы первый."""
        r = self.http.post(
            f"{self.base}/upload/image",
            files={"image": (name or path.name, path.read_bytes(), "image/png")},
            data={"overwrite": "true"},
        )
        r.raise_for_status()
        return str(r.json()["name"])

    def queue(self, graph: dict[str, Any]) -> str:
        r = self.http.post(f"{self.base}/prompt", json={"prompt": graph, "client_id": "videoforge"})
        if r.status_code != 200:
            raise ProviderError(f"ComfyUI /prompt {r.status_code}: {r.text[:600]}")
        data = r.json()
        if data.get("node_errors"):
            raise ProviderError(f"ComfyUI отверг граф: {json.dumps(data['node_errors'])[:600]}")
        return str(data["prompt_id"])

    def free(self) -> None:
        """Выгрузить модели из VRAM и RAM. Смена семейства (klein → LTX) без этого
        роняла ComfyUI: выгруженные в RAM klein + Qwen3 + LTX-набор превышали лимит
        памяти контейнера (62 ГБ), и процесс убивался посреди первого клипа
        (Test 3, 08.09.2026). Ошибка вызова не фатальна — старый ComfyUI без /free."""
        try:
            self.http.post(f"{self.base}/free", json={"unload_models": True, "free_memory": True})
        except httpx.HTTPError as exc:
            log.warning("ComfyUI /free: %s", exc)

    def wait(
        self,
        prompt_id: str,
        *,
        timeout_s: float = 1800.0,
        poll_s: float = 2.0,
        dead_after_s: float = DEAD_AFTER_S,
    ) -> dict[str, Any]:
        """Дождаться истории задачи. Внятный ответ пода — код 200 и JSON-объект;
        всё прочее (код не 200, HTML вместо JSON, обрыв) — сбой опроса, а не «ещё
        не готово». Сбои подряд дольше `dead_after_s` — ProviderError: процесс
        на поде мёртв, и ждать его до `timeout_s` значит платить за карту впустую.

        До 16.09.2026 ответ не 200 читался как пустая история и обнулял счёт
        сбоев, а неразобранный ответ очереди глотался: ComfyUI убил OOM, прокси
        RunPod отдавал 404 с пустым телом, и клип ждал мёртвый процесс
        одиннадцать минут из тридцати (t07_lora_motion_ref)."""
        t0 = time.perf_counter()
        silent_since: float | None = None  # начало первого опроса без ответа подряд
        failures = 0
        while time.perf_counter() - t0 < timeout_s:
            asked = time.perf_counter()
            try:
                hist = self._check(prompt_id, verify_queue=asked - t0 > LOST_AFTER_S)
            except _NoAnswer as exc:
                failures += 1
                silent_since = asked if silent_since is None else silent_since
                silent = time.perf_counter() - silent_since
                # два сбоя минимум: один долгий опрос (таймаут) сам по себе — не приговор
                if failures >= 2 and silent >= dead_after_s:
                    raise ProviderError(
                        f"ComfyUI не отвечает ({exc}) уже {silent:.0f} с, опросов подряд: "
                        f"{failures} — процесс на поде мёртв? Задача {prompt_id} брошена; "
                        "лог — /workspace/out/comfy.log на поде, перезапуск — "
                        "bash /workspace/comfy_setup.sh"
                    ) from None
                log.warning(
                    "ComfyUI: опрос %s без ответа (%s), %.0f с подряд", prompt_id, exc, silent
                )
                time.sleep(poll_s * 3)
                continue
            failures, silent_since = 0, None
            if hist is not None:
                return hist
            time.sleep(poll_s)
        try:
            self.http.post(f"{self.base}/interrupt", timeout=POLL_TIMEOUT_S)
        except httpx.HTTPError as exc:
            log.warning("ComfyUI /interrupt: %s", exc)
        raise ProviderError(f"ComfyUI: {prompt_id} не завершился за {timeout_s:.0f} с")

    def _poll(self, path: str) -> dict[str, Any]:
        """GET опроса: JSON-объект или `_NoAnswer` с тем, что пришло вместо него."""
        try:
            r = self.http.get(f"{self.base}{path}", timeout=POLL_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise _NoAnswer(f"{path}: {type(exc).__name__}") from None
        if r.status_code != 200:
            body = r.text.strip()
            raise _NoAnswer(
                f"{path}: код {r.status_code}, "
                + (f"тело {body[:80]!r}" if body else "пустое тело")
            )
        try:
            data = r.json()
        except ValueError:
            # прокси на мёртвый процесс отдаёт HTML «Waiting for service» с кодом 200
            raise _NoAnswer(f"{path}: код 200, не JSON {r.text.strip()[:80]!r}") from None
        if not isinstance(data, dict):
            raise _NoAnswer(f"{path}: код 200, JSON не объект")
        return data

    def _check(self, prompt_id: str, *, verify_queue: bool) -> dict[str, Any] | None:
        """Один опрос: история готовой задачи или None — ещё идёт.

        `verify_queue` — история пуста уже долго: сверить с очередью. Перезапущенный
        ComfyUI не знает наш prompt_id, и ждать его полчаса незачем. Сбой опроса
        очереди — такой же сбой, как у истории, а не повод промолчать."""
        data = self._poll(f"/history/{prompt_id}")
        if prompt_id not in data and verify_queue:
            q = self._poll("/queue")
            alive = {
                job[1]
                for key in ("queue_running", "queue_pending")
                for job in q.get(key) or []
                if isinstance(job, list) and len(job) > 1
            }
            if prompt_id not in alive:
                # задача могла завершиться между двумя запросами — история
                # перечитывается, и только пустая дважды означает потерю
                data = self._poll(f"/history/{prompt_id}")
                if prompt_id not in data:
                    raise ProviderError(
                        f"ComfyUI потерял задачу {prompt_id} (перезапуск?) — повтори стадию"
                    )
        if prompt_id not in data:
            return None
        hist: dict[str, Any] = data[prompt_id]
        status = hist.get("status") or {}
        if status.get("status_str") == "error" or status.get("completed") is False:
            msgs = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
            detail = json.dumps(msgs[-1][1])[:600] if msgs else json.dumps(status)[:600]
            raise ProviderError(f"ComfyUI: ошибка выполнения: {detail}")
        return hist

    @staticmethod
    def find_output(hist: dict[str, Any], suffixes: tuple[str, ...]) -> dict[str, Any] | None:
        for node_out in (hist.get("outputs") or {}).values():
            for items in node_out.values():
                if not isinstance(items, list):
                    continue
                for it in items:
                    if isinstance(it, dict) and str(it.get("filename", "")).lower().endswith(
                        suffixes
                    ):
                        return it
        return None

    def download(self, item: dict[str, Any], out_path: Path) -> Path:
        r = self.http.get(
            f"{self.base}/view",
            params={
                "filename": item["filename"],
                "subfolder": item.get("subfolder", ""),
                "type": item.get("type", "output"),
            },
        )
        r.raise_for_status()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(r.content)
        return out_path


# ------------------------------------------------------------------ графы


def size_for(aspect_ratio: str, w16: int, h16: int) -> tuple[int, int]:
    if aspect_ratio == "9:16":
        return h16, w16
    if aspect_ratio == "1:1":
        side = min(w16, h16)
        return side, side
    return w16, h16


def klein_graph(
    prompt: str, *, width: int, height: int, seed: int, lora: str, lora_strength: float, prefix: str
) -> dict[str, Any]:
    """FLUX.2 klein 4B distilled — субграф «Text to Image (Flux.2 Klein 4B
    Distilled)» официального шаблона image_flux2_klein_text_to_image: 4 шага
    Flux2Scheduler, euler, CFGGuider cfg 1 с обнулённым негативом; LoRA
    персонажа поверх модели (ai-toolkit пишет ключи в префиксе ComfyUI)."""
    model: list[Any] = ["unet", 0]
    g: dict[str, Any] = {
        "unet": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": KLEIN_UNET, "weight_dtype": "default"},
        },
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": KLEIN_CLIP, "type": "flux2"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": KLEIN_VAE}},
    }
    if lora:
        g["lora"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": model, "lora_name": lora, "strength_model": lora_strength},
        }
        model = ["lora", 0]
    g.update(
        {
            "pos": {
                "class_type": "CLIPTextEncode",
                "inputs": {"clip": ["clip", 0], "text": prompt},
            },
            "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
            "latent": {
                "class_type": "EmptyFlux2LatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
            "sigmas": {
                "class_type": "Flux2Scheduler",
                "inputs": {"steps": 4, "width": width, "height": height},
            },
            "sampler_sel": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
            "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
            "guider": {
                "class_type": "CFGGuider",
                "inputs": {
                    "model": model,
                    "positive": ["pos", 0],
                    "negative": ["neg", 0],
                    "cfg": 1.0,
                },
            },
            "sampler": {
                "class_type": "SamplerCustomAdvanced",
                "inputs": {
                    "noise": ["noise", 0],
                    "guider": ["guider", 0],
                    "sampler": ["sampler_sel", 0],
                    "sigmas": ["sigmas", 0],
                    "latent_image": ["latent", 0],
                },
            },
            "decode": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]},
            },
            "save": {
                "class_type": "SaveImage",
                "inputs": {"images": ["decode", 0], "filename_prefix": prefix},
            },
        }
    )
    return g


REFERENCE_MEGAPIXELS = 1.0  # референс композиции: около мегапикселя (test8b: 0.25 Мп не чище)


def klein_reference_graph(
    graph: dict[str, Any],
    image_names: list[str],
    *,
    megapixels: float = REFERENCE_MEGAPIXELS,
    guider_input: str = "positive",
) -> dict[str, Any]:
    """Граф klein + референсы композиции по официальному рецепту редактирования
    klein: LoadImage → ImageScaleToTotalPixels → VAEEncode → ReferenceLatent
    поверх позитивного кондишена, по цепочке на каждую картинку. Остальное —
    как у `klein_graph`; негатив обнулён, cfg 1 — как в продакшене.

    Замер 14.09.2026 (`projects/_bench/test8b`): фото с нужной расстановкой +
    промпт конвейера как есть дают композицию фото в стиле промпта, содержимое
    фото (постаменты, люди) не протекает; промпт-инструкция «замени / убери»
    ломает кадр, контуры вместо фото хуже. `resolution_steps` у масштабирования
    обязателен в ComfyUI 0.34 — 16, кратность латента Flux.2."""
    g = dict(graph)
    conditioning: list[Any] = ["pos", 0]
    for i, name in enumerate(image_names, 1):
        g[f"ref_img{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g[f"ref_scale{i}"] = {
            "class_type": "ImageScaleToTotalPixels",
            "inputs": {
                "image": [f"ref_img{i}", 0],
                "upscale_method": "lanczos",
                "megapixels": megapixels,
                "resolution_steps": 16,
            },
        }
        g[f"ref_lat{i}"] = {
            "class_type": "VAEEncode",
            "inputs": {"pixels": [f"ref_scale{i}", 0], "vae": ["vae", 0]},
        }
        g[f"pos_ref{i}"] = {
            "class_type": "ReferenceLatent",
            "inputs": {"conditioning": conditioning, "latent": [f"ref_lat{i}", 0]},
        }
        conditioning = [f"pos_ref{i}", 0]
    guider = dict(g["guider"])
    guider["inputs"] = {**guider["inputs"], guider_input: conditioning}
    g["guider"] = guider
    return g


# FLUX.2 [dev] 32B (замер anime_keyframe_recipe, T-66, 17.09.2026): самая крупная открытая
# модель правки картинок на эту дату — отвечает, в размере ли klein 4B дело или в рецепте.
# Копия Comfy-Org открыта без токена (оригинал BFL закрыт формой): модель 35.5 ГБ +
# Mistral fp8 18 ГБ; VAE общий с klein
FLUX2_DEV_UNET = "flux2_dev_fp8mixed.safetensors"
FLUX2_DEV_CLIP = "mistral_3_small_flux2_fp8.safetensors"


def flux2_dev_graph(
    image_names: list[str],
    prompt: str,
    *,
    width: int,
    height: int,
    seed: int,
    prefix: str,
    steps: int = 20,
    guidance: float = 4.0,
) -> dict[str, Any]:
    """FLUX.2 [dev] по субграфу «Image Edit (Flux.2 Dev)» шаблонов ComfyUI
    (blueprints/image_edit_flux_2_dev): CLIPTextEncode → FluxGuidance 4 →
    ReferenceLatent на каждую картинку → BasicGuider, Flux2Scheduler 20 шагов, euler.
    Картинки масштабируются к мегапикселю, как у klein; размер выхода задаёт пустой
    латент. Turbo-LoRA шаблона не берём: вопрос замера — потолок качества, не скорость."""
    g: dict[str, Any] = {
        "unet": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": FLUX2_DEV_UNET, "weight_dtype": "default"},
        },
        "clip": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": FLUX2_DEV_CLIP, "type": "flux2"},
        },
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": KLEIN_VAE}},
        "text": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "pos": {
            "class_type": "FluxGuidance",
            "inputs": {"conditioning": ["text", 0], "guidance": guidance},
        },
        "latent": {
            "class_type": "EmptyFlux2LatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "sigmas": {
            "class_type": "Flux2Scheduler",
            "inputs": {"steps": steps, "width": width, "height": height},
        },
        "sampler_sel": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "guider": {
            "class_type": "BasicGuider",
            "inputs": {"model": ["unet", 0], "conditioning": ["pos", 0]},
        },
        "sampler": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["noise", 0],
                "guider": ["guider", 0],
                "sampler": ["sampler_sel", 0],
                "sigmas": ["sigmas", 0],
                "latent_image": ["latent", 0],
            },
        },
        "decode": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]},
        },
        "save": {
            "class_type": "SaveImage",
            "inputs": {"images": ["decode", 0], "filename_prefix": prefix},
        },
    }
    return klein_reference_graph(g, image_names, guider_input="conditioning")


def ltx_graph(
    image_name: str,
    prompt: str,
    *,
    width: int,
    height: int,
    frames: int,
    seed: int,
    prefix: str,
    vae_name: str | None = None,
    audio: bool = False,
    frame_rate_cond: float | None = None,
) -> dict[str, Any]:
    """LTX-2.5 22B distilled, официальный двухпроходный рецепт шаблона
    video_ltx2_5_i2v (ComfyUI ≥ 0.32): половинное разрешение, 8 сигм, кадр в
    латент с силой 0.7 → latent-апскейл ×2 → 4 сигмы, сила 1.0. Аудио-латент
    модели нужен (совместный AV-трансформер); `audio=True` декодирует и его —
    LTX-2.5 сочиняет звук сцены (толпа, удар) по промпту.

    `frame_rate_cond` — условие темпа для модели (LTXVConditioning и
    аудио-латент), НЕ частота выходного файла: та всегда LTX_FPS. Заниженное
    условие (16 при выводе 24) говорит модели «этих кадров хватает на больше
    секунд действия», и в кадр укладывается больше движения (t02: +30%
    энергии); проверяется замером как одна переменная, в конвейере не включено."""

    cond_fps = float(frame_rate_cond or LTX_FPS)
    video_inputs: dict[str, Any] = {"images": ["dec", 0], "fps": float(LTX_FPS)}
    extra: dict[str, Any] = {}
    if audio:
        extra["adec"] = {
            "class_type": "LTXVAudioVAEDecode",
            "inputs": {"samples": ["sep2", 1], "audio_vae": ["va", 0]},
        }
        video_inputs["audio"] = ["adec", 0]

    def guider(key: str) -> dict[str, Any]:
        return {
            "class_type": "LTXVDualCFGGuider",
            "inputs": {
                "model": ["u", 0],
                "positive": ["cond", 0],
                "negative": ["cond", 1],
                "video_cfg": 1.0,
                "audio_cfg": 1.0,
            },
        }

    return {
        "u": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": LTX_UNET, "weight_dtype": "default"},
        },
        "vv": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name or LTX_VAE}},
        "va": {"class_type": "VAELoader", "inputs": {"vae_name": LTX_AVAE}},
        "c": {"class_type": "CLIPLoader", "inputs": {"clip_name": LTX_CLIP, "type": "ltxv"}},
        "p": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["c", 0], "text": prompt}},
        "n": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["c", 0], "text": ""}},
        "cond": {
            "class_type": "LTXVConditioning",
            "inputs": {"positive": ["p", 0], "negative": ["n", 0], "frame_rate": cond_fps},
        },
        "img": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "pre": {
            "class_type": "LTXVPreprocess",
            "inputs": {"image": ["img", 0], "img_compression": 18},
        },
        "lat1": {
            "class_type": "EmptyLTXVLatentVideo",
            "inputs": {
                "width": width // 2,
                "height": height // 2,
                "length": frames,
                "batch_size": 1,
            },
        },
        "i2v1": {
            "class_type": "LTXVImgToVideoInplace",
            "inputs": {
                "vae": ["vv", 0],
                "image": ["pre", 0],
                "latent": ["lat1", 0],
                "strength": 0.7,
                "bypass": False,
            },
        },
        "aud": {
            "class_type": "LTXVEmptyLatentAudio",
            "inputs": {
                "frames_number": frames,
                "frame_rate": cond_fps,
                "batch_size": 1,
                "audio_vae": ["va", 0],
            },
        },
        "cat1": {
            "class_type": "LTXVConcatAVLatent",
            "inputs": {"video_latent": ["i2v1", 0], "audio_latent": ["aud", 0]},
        },
        "g1": guider("g1"),
        "ks": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler_ancestral"}},
        "sig1": {"class_type": "ManualSigmas", "inputs": {"sigmas": LTX_SIGMAS_PASS1}},
        "noise1": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "s1": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["noise1", 0],
                "guider": ["g1", 0],
                "sampler": ["ks", 0],
                "sigmas": ["sig1", 0],
                "latent_image": ["cat1", 0],
            },
        },
        "sep1": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["s1", 0]}},
        "upl": {"class_type": "LatentUpscaleModelLoader", "inputs": {"model_name": LTX_UPSCALER}},
        "up": {
            "class_type": "LTXVLatentUpsampler",
            "inputs": {"samples": ["sep1", 0], "upscale_model": ["upl", 0], "vae": ["vv", 0]},
        },
        "i2v2": {
            "class_type": "LTXVImgToVideoInplace",
            "inputs": {
                "vae": ["vv", 0],
                "image": ["pre", 0],
                "latent": ["up", 0],
                "strength": 1.0,
                "bypass": False,
            },
        },
        "cat2": {
            "class_type": "LTXVConcatAVLatent",
            "inputs": {"video_latent": ["i2v2", 0], "audio_latent": ["sep1", 1]},
        },
        "g2": guider("g2"),
        "sig2": {"class_type": "ManualSigmas", "inputs": {"sigmas": LTX_SIGMAS_PASS2}},
        "noise2": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed + 1}},
        "s2": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["noise2", 0],
                "guider": ["g2", 0],
                "sampler": ["ks", 0],
                "sigmas": ["sig2", 0],
                "latent_image": ["cat2", 0],
            },
        },
        "sep2": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["s2", 0]}},
        "dec": {
            "class_type": "VAEDecodeTiled",
            "inputs": {
                "samples": ["sep2", 0],
                "vae": ["vv", 0],
                "tile_size": 512,
                "overlap": 64,
                "temporal_size": 64,
                "temporal_overlap": 16,
            },
        },
        "vid": {"class_type": "CreateVideo", "inputs": video_inputs},
        "save": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["vid", 0],
                "filename_prefix": prefix,
                "format": "mp4",
                "codec": "h264",
            },
        },
        **extra,
    }


def wan_graph(
    image_name: str,
    prompt: str,
    negative: str,
    *,
    width: int,
    height: int,
    frames: int,
    seed: int,
    prefix: str,
    recipe: WanRecipe | None = None,
) -> dict[str, Any]:
    """Wan 2.2 I2V A14B fp8 + lightx2v 4-step: 2 шага high-noise + 2 low-noise,
    cfg 1, shift 5 — официальный шаблон Comfy-Org с веткой Lightning. Рецепт
    `anisora` — те же узлы без LoRA: 8 шагов, 3 high + 5 low (AniSora V3.2)."""
    r = recipe or WAN_RECIPES["lightx2v"]
    ks = {
        "sampler_name": "euler",
        "scheduler": "simple",
        "positive": ["i2v", 0],
        "negative": ["i2v", 1],
    }
    g: dict[str, Any] = {
        "uh": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": r.high, "weight_dtype": "default"},
        },
        "ul": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": r.low, "weight_dtype": "default"},
        },
    }
    high, low = "uh", "ul"
    if r.lora_high and r.lora_low:
        g["lh"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": ["uh", 0], "lora_name": r.lora_high, "strength_model": 1.0},
        }
        g["ll"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": ["ul", 0], "lora_name": r.lora_low, "strength_model": 1.0},
        }
        high, low = "lh", "ll"
    return g | {
        "mh": {"class_type": "ModelSamplingSD3", "inputs": {"model": [high, 0], "shift": r.shift}},
        "ml": {"class_type": "ModelSamplingSD3", "inputs": {"model": [low, 0], "shift": r.shift}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": WAN_CLIP, "type": "wan"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": WAN_VAE}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": negative}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "i2v": {
            "class_type": "WanImageToVideo",
            "inputs": {
                "positive": ["pos", 0],
                "negative": ["neg", 0],
                "vae": ["vae", 0],
                "width": width,
                "height": height,
                "length": frames,
                "batch_size": 1,
                "start_image": ["img", 0],
            },
        },
        "s1": {
            "class_type": "KSamplerAdvanced",
            "inputs": {
                **ks,
                "model": ["mh", 0],
                "add_noise": "enable",
                "noise_seed": seed,
                "steps": r.steps,
                "cfg": 1.0,
                "latent_image": ["i2v", 2],
                "start_at_step": 0,
                "end_at_step": r.switch,
                "return_with_leftover_noise": "enable",
            },
        },
        "s2": {
            "class_type": "KSamplerAdvanced",
            "inputs": {
                **ks,
                "model": ["ml", 0],
                "add_noise": "disable",
                "noise_seed": seed,
                "steps": r.steps,
                "cfg": 1.0,
                "latent_image": ["s1", 0],
                "start_at_step": r.switch,
                "end_at_step": r.steps,
                "return_with_leftover_noise": "disable",
            },
        },
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["s2", 0], "vae": ["vae", 0]}},
        "vid": {
            "class_type": "CreateVideo",
            "inputs": {"images": ["dec", 0], "fps": float(WAN_FPS)},
        },
        "save": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["vid", 0],
                "filename_prefix": prefix,
                "format": "mp4",
                "codec": "h264",
            },
        },
    }


# ------------------------------------------------------------------ провайдеры


def _seed(text: str) -> int:
    """Сид от промпта: повторный вызов с тем же промптом даёт тот же кадр —
    идемпотентность стадии не ломается о случайность."""
    import hashlib

    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16)


class ComfyImage(ImageProvider):
    """FLUX.2 klein 4B + LoRA персонажа. Портфолио каста не принимает — лицо
    держит LoRA, и промпт должен идти без паспорта (stages/prompts.py,
    lora_mode). Референс композиции (`Reference.role == "composition"`,
    картинка «как надо» у сцены) принимает: `klein_reference_graph`."""

    name = "comfy:klein4b"
    uses_lora = True

    def __init__(self, state: PodState | None = None) -> None:
        self._state = state
        self._freed = False  # /free один раз на процесс: сменить семейство моделей
        self.price_usd = round(MEASURED_SEC["klein4b_1280x720"] * DEFAULT_RATE_USD_HR / 3600, 6)

    @property
    def state(self) -> PodState:
        if self._state is None:
            self._state = PodState.load()
        return self._state

    def weights(self) -> str | None:
        """LoRA персонажа и её сила — с ПОДА (`.gpu/pod.json`), то есть те, что
        `gpu up` реально залил, а не те, что записаны у персонажа. Карта не
        поднята — `None`: чем рендерили бы, неизвестно, и подставлять в
        отпечаток нечего."""
        try:
            st = self.state
        except ProviderError:
            return None
        return f"{st.lora}@{st.lora_strength:g}" if st.lora else ""

    def generate(
        self, prompt: str, *, references: list[Any], aspect_ratio: str, out_path: Path
    ) -> GenResult:
        st = self.state
        client = ComfyClient(st.base_url)
        if not self._freed:
            client.free()
            self._freed = True
        w, h = size_for(aspect_ratio, 1280, 720)
        seed = _seed(prompt + out_path.stem)
        graph = klein_graph(
            prompt,
            width=w,
            height=h,
            seed=seed,
            lora=st.lora,
            lora_strength=st.lora_strength,
            prefix=f"videoforge/{out_path.stem}",
        )
        composition = [r for r in references if getattr(r, "role", "cast") == "composition"]
        if composition:
            names = [client.upload_image(Path(r.path)) for r in composition]
            graph = klein_reference_graph(graph, names)
        t0 = time.perf_counter()
        hist = client.wait(client.queue(graph))
        item = client.find_output(hist, (".png", ".jpg", ".webp"))
        if not item:
            raise ProviderError("ComfyUI не вернул картинку (нет SaveImage в history)")
        client.download(item, out_path)
        sec = time.perf_counter() - t0
        cost = round(sec * st.rate_usd_hr / 3600, 6)
        return GenResult(
            out_path,
            cost,
            {
                "model": KLEIN_UNET,
                "lora": st.lora,
                "seed": seed,
                "size": f"{w}x{h}",
                "gen_sec": round(sec, 1),
                "prompt": prompt,
                "references": [Path(r.path).name for r in composition],
            },
        )


class ComfyVideo(VideoProvider):
    """LTX-2.5 distilled (по умолчанию) или Wan 2.2 480p — `.gpu/pod.json: video`.

    Референс движения (`MotionGuide`) — IC-LoRA Union Control поверх LTX-2.5
    (`control_plan`); на поде нужен `assets/gpu/iclora_setup.sh`. Пресет Wan
    референс не умеет и отвечает ProviderError до единого вызова."""

    name = "comfy:ltx25"
    follows_reference = True

    def __init__(self, state: PodState | None = None, preset: str | None = None) -> None:
        self._state = state
        self._freed = False
        pid = (
            preset
            or os.environ.get("VF_COMFY_VIDEO")
            or (state.video if state else None)
            or "ltx25"
        )
        if pid not in VIDEO_PRESETS:
            raise ProviderError(
                f"неизвестный пресет видео ComfyUI '{pid}': {sorted(VIDEO_PRESETS)}"
            )
        self.preset = VIDEO_PRESETS[pid]
        self.supported_durations = self.preset.durations
        self.name = f"comfy:{pid}"

    @property
    def state(self) -> PodState:
        if self._state is None:
            self._state = PodState.load()
            if (
                self._state.video in VIDEO_PRESETS
                and self._state.video != self.preset.id
                and not os.environ.get("VF_COMFY_VIDEO")
            ):
                self.preset = VIDEO_PRESETS[self._state.video]
                self.supported_durations = self.preset.durations
                self.name = f"comfy:{self.preset.id}"
        return self._state

    def price_for(self, duration: int, *, guided: bool = False) -> float:
        frames = frames_for(duration, self.preset.fps)
        factor = CONTROL_COST_FACTOR if guided else 1.0
        return round(frames * self.preset.sec_per_frame * factor * DEFAULT_RATE_USD_HR / 3600, 6)

    def animate(
        self,
        image: Path,
        prompt: str,
        *,
        duration_sec: int,
        aspect_ratio: str,
        out_path: Path,
        chain: ChainLink | None = None,  # движение задаёт модель: место в цепочке не применимо
        motion_reference: MotionGuide | None = None,
    ) -> GenResult:
        if motion_reference is not None:
            _require_ltx(self.preset)  # до подгонки референса и до вызовов пода
        st = self.state
        client = ComfyClient(st.base_url)
        if not self._freed:
            client.free()
            self._freed = True
        seed = _seed(prompt + out_path.stem)
        name = client.upload_image(image)
        if motion_reference is not None:
            # референс под длину сцены лежит рядом с клипом (`<клип>.ref.mp4`):
            # что модель видела — рядом с тем, что она сделала, как у замеров
            frames = frames_for(duration_sec, self.preset.fps)
            ref = conform_reference(
                motion_reference.path,
                out_path.with_suffix(".ref.mp4"),
                frames=frames,
                fps=self.preset.fps,
            )
            ref_name = client.upload_image(ref)
            graph, meta = control_plan(
                self.preset,
                name,
                ref_name,
                prompt,
                aspect_ratio=aspect_ratio,
                duration_sec=duration_sec,
                seed=seed,
                prefix=f"videoforge/{out_path.stem}",
                signal=motion_reference.signal,
                strength=motion_reference.strength,
            )
            meta["reference_source"] = str(motion_reference.path)
        else:
            graph, meta = i2v_plan(
                self.preset,
                name,
                prompt,
                aspect_ratio=aspect_ratio,
                duration_sec=duration_sec,
                seed=seed,
                prefix=f"videoforge/{out_path.stem}",
            )
        t0 = time.perf_counter()
        hist = client.wait(client.queue(graph))
        item = client.find_output(hist, (".mp4", ".webm", ".mov"))
        if not item:
            raise ProviderError("ComfyUI не вернул видео (нет SaveVideo в history)")
        client.download(item, out_path)
        sec = time.perf_counter() - t0
        cost = round(sec * st.rate_usd_hr / 3600, 6)
        return GenResult(
            out_path,
            cost,
            {
                **{k: v for k, v in meta.items() if k != "negative"},
                "gen_sec": round(sec, 1),
                "prompt": prompt,
            },
        )
