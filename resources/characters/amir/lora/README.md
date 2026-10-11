# LoRA Амира — обученные веса (Test 1–1b, 07–08.09.2026)

Файлы v1 (klein и Z-Image, датасет 15 кадров) удалены 09.09.2026 при уборке:
обе заменены v2. Датасет v1 (`../lora_dataset/`) удалён в тот же день — все
его 15 кадров байт в байт входят в `../lora_dataset_v2/` (первые 15 файлов без
префикса `v2_`); конфиги обучения — в `projects/_bench/lora_*_amir.yaml`.
Строки ниже сохранены как история замеров.

| Файл | База | Обучение | Инференс | Сила | Статус |
|---|---|---|---|---|---|
| `amir_klein4b_v1.safetensors` (92 МБ) | black-forest-labs/FLUX.2-klein-base-4B, arch `flux2_klein_4b` | ai-toolkit, 1500 шагов, rank 32, lr 1e-4, датасет `../lora_dataset/` (15 кадров), RTX PRO 4500, 30 мин | distilled `FLUX.2-klein-4B`, 4 шага, guidance 1.0, diffusers `Flux2KleinPipeline.load_lora_weights` | 1.0 | v1, лицо держится, стиль klein-овский |
| `amir_zimage_v1.safetensors` (85 МБ) | Tongyi-MAI/Z-Image-Turbo + адаптер ostris v2, arch `zimage` | ai-toolkit, 2000 шагов, rank 16, lr 2.5e-4, тот же датасет, 60 мин | Z-Image-Turbo, 8 шагов, guidance 0, `ZImagePipeline.load_lora_weights` | 0.7–0.85 (при 1.0 теряет послушание композиции) | v1, стиль = портфолио, переобучена на анфас |
| `amir_klein4b_v2.safetensors` (92 МБ) | та же база и arch | те же настройки, датасет `../lora_dataset_v2/` (26 кадров: 15 v1 + 11 ракурсов от Z-Image v1), 38 мин, 08.09.2026 | как v1 | 1.0 | **v2, выбрана художником проекта (08.09.2026)**: стиль мягче v1, ракурсы держит; родинка иногда двоится — открытый дефект |
| `amir_zimage_v2.safetensors` (85 МБ) | та же база и arch | те же настройки, датасет `../lora_dataset_v2/`, 62 мин, 08.09.2026 | как v1 | 0.8 | **v2, рабочая**: стиль = портфолио; ракурсы держит, если в промпте НЕТ паспорта лица |

**Промпт при LoRA: триггер `amir` + стиль + сцена, без `prompt_block`.** Блок паспорта («adult man around 35 … mole … calm expressive face») тянет лицо в кадр: «со спины» превращается в анфас, «профиль» — в три четверти, «в левой трети» — в центр. Проверено A/B на 10 сценах при одном сиде (`projects/_bench/out/test1b/compare_nopassport.png`). Паспорт нужен только художникам без LoRA (Nano Banana / Qwen Edit с референсами).

Конфиги обучения: `projects/_bench/lora_klein4b_amir*.yaml`, `lora_zimage_amir*.yaml`; журнал и сэмплы — `projects/_bench/LOG.md`, `out/test1/`, `out/test1b/`. Подпапка `lora/` в референсы сцен не подхватывается.
