# Портфолио Амира — происхождение

## Текущий канон (2026-09-08, Test 1c): klein 4B + LoRA v2

Семь кадров ядра нарисованы художником проекта — **FLUX.2 klein 4B + `lora/amir_klein4b_v2.safetensors`** (сила 1.0, 4 шага, guidance 1.0) на поде RunPod, **сид 42** на всех кадрах, промпт без паспорта лица: `amir, <стиль без эффектов>. <план>. <гигиена>`. 1.8–2.2 с и ≈$0.0004 за кадр, весь заход ≈$0.04. Выбор из 4 сидов — лист `projects/_bench/out/test1c/portfolio_klein_sheet.png`; полные промпты и md5 — `portfolio_provenance.json`. Художника выбрал пользователь («менее мультяшная, детальная, взрослая»), кадры на сид 42 — Claude (одна линия света и одежды, чистые руки).

Известная слабость: родинка — klein рисует её то на правой щеке (фронтальные кадры), то на левой (три четверти); в приметы полагаться на неё не стоит.

Прежний канон (линия NB2, ниже) лежит в `archive/nb2/` и в референсы не подхватывается.

## Прежний канон (2026-09-07, Test 0): Z-Image seed 1234 → Nano Banana 2 Edit

Собрано 2026-09-07T15:47:23+00:00 в Test 0 (план v2, `projects/_bench/LOG.md`). Линия: **Z-Image seed 1234 → Nano Banana 2 Edit**. Итого $0.530.

Стиль референсов — `flat_cartoon_warm` без эффектов (частицы/свечение вычищены: протекают в сцены). Одежда в референсах нейтральная и не входит в идентичность.

| Кадр | Модель | Эндпоинт | seed | Стоимость | Время | md5 |
|---|---|---|---|---|---|---|
| `face_front.png` | Z-Image Turbo (Tongyi, Apache 2.0) | `z-image-turbo` | 1234 | $0.0050 | 5884 мс | `75b349bc` |
| `face_three_quarter.png` | Nano Banana 2 Edit (Google gemini-3.1-flash-image через RunPod) | `google-nano-banana-2-edit` | — | $0.0875 | 27658 мс | `96b1bd31` |
| `face_profile.png` | Nano Banana 2 Edit (Google gemini-3.1-flash-image через RunPod) | `google-nano-banana-2-edit` | — | $0.0875 | 28355 мс | `48c64a28` |
| `face_expression_smile.png` | Nano Banana 2 Edit (Google gemini-3.1-flash-image через RunPod) | `google-nano-banana-2-edit` | — | $0.0875 | 35279 мс | `667ca630` |
| `body_waist_up.png` | Nano Banana 2 Edit (Google gemini-3.1-flash-image через RunPod) | `google-nano-banana-2-edit` | — | $0.0875 | 24158 мс | `0e4db656` |
| `body_full_front.png` | Nano Banana 2 Edit (Google gemini-3.1-flash-image через RunPod) | `google-nano-banana-2-edit` | — | $0.0875 | 38737 мс | `2d55ef4b` |
| `body_full_back.png` | Nano Banana 2 Edit (Google gemini-3.1-flash-image через RunPod) | `google-nano-banana-2-edit` | — | $0.0875 | 31515 мс | `09bac10f` |

## Промпт сида (face_front, Z-Image Turbo, seed 1234)

```
soft 2D digital illustration, gentle thin outlines, cel shading with smooth gradients, glowing light and floating particles, warm golden key light against cool blue shadows, calm and reverent mood,. adult man around 35, medium athletic build, warm olive skin, short neatly trimmed full black beard with a few gray strands at the chin, short black hair, dark brown eyes, thick straight black eyebrows, small dark mole under the left eye, calm expressive face. head-and-shoulders portrait, facing the camera directly, neutral expression. Plain light-gray studio background, soft even lighting, no dramatic shadows. Wearing simple plain casual clothes in muted colors. No props, no text, no watermarks, exactly one character in frame. one clear focal subject, simple readable composition, wide 16:9 framing with breathing room, high quality.
```

## Промпт редактирования (Nano Banana 2 Edit, вход = URL сида)

Для каждого кадра: `Keep the exact same person from the input image … Redraw him as a <план>` + гигиена + стиль без эффектов. Полные тексты — в `portfolio_provenance.json`.

## Отклонённая линия

Qwen-Image-Edit-2511 (12 кадров, $0.245): другой человек и стиль, блёстки из префикса стиля; файлы projects/_bench/out/test0/core/

## Кандидаты

12 сид-лиц Z-Image (seeds 11…4096) — `projects/_bench/out/test0/candidates/`, лист `candidates_sheet.png`; пользователь выбрал 1234 (мои кандидаты были 512/2024/77).

## Лист персонажа

`_candidates/character_sheet_nb2.png` — 7 кадров одной картинкой 4×2 для моделей с лимитом референсов (NB2: ≤4 персонажа; Qwen Edit: 1–3 картинки).