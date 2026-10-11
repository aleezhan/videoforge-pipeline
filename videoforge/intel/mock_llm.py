"""Заглушка LLM для анализа: весь конвейер проходит без реальных вызовов.

Отличает запросы по содержимому промпта: "playbook" — вклад в свод правил,
"CONTENT FORMULA" — формула, "MEASURED EDITING RHYTHM" — визуальный разбор,
"TRANSCRIPT" — разбор видео (deep), ключ "video_id" — классификация пакета,
иначе — таксономия. Ответы — детерминированный валидный JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from videoforge.providers.base import CheckResult, LlmProvider, LlmResult, Reference

_FORMULA = {
    "what_works": ["mock works: 2 из 4 разборов начинаются с вопроса"],
    "content_formula": [
        {"step": "хук-вопрос", "purpose": "mock: открыть пробел любопытства"},
        {"step": "эскалация", "purpose": "mock: удерживать"},
    ],
    "title_formulas": [{"pattern": "mock pattern", "example": "Mock example title"}],
    "hook_rules": ["mock rule"],
    "format_advice": [{"format": "mock_a", "verdict": "делать", "why": "mock"}],
    "shot_recipe": {
        "scene_duration_sec": 2.0,
        "shot_range_sec": "1-3",
        "cuts_per_min": 30.0,
        "rhythm": "mock: темп ускоряется к финалу",
        "camera_moves": ["mock наезд"],
        "staging": ["mock: число рядом с бытовым предметом"],
    },
    "production_notes": "mock production notes",
    "video_ideas": [
        {"title": "Mock idea", "format": "mock_a", "hook": "mock hook", "outline": "mock outline"}
    ],
    "hit_vs_control": ["mock: у хитов вопрос в первой фразе, у контрольных — нет"],
    "caveats": ["mock caveat: выборка маленькая"],
}

_VISUAL = {
    "shot_language": "mock: общий план чередуется с крупным",
    "camera_moves": ["mock наезд", "mock панорама"],
    "staging_patterns": ["mock: число рядом с бытовым предметом"],
    "text_on_screen": "mock: цифра появляется вместе с озвучкой",
    "palette": "mock: насыщенные плоские цвета",
    "thumbnail": {
        "elements": "mock обложка",
        "text": "MOCK",
        "why_clickable": "mock: контраст и одно слово",
    },
    "transferable": ["mock приём монтажа"],
}

_DEEP = {
    "hook": {
        "type": "question",
        "text": "Mock opening question?",
        "why_it_works": "mock: открывает пробел любопытства",
    },
    "structure": [
        {"t": "00:00", "beat": "хук", "purpose": "mock: удержать первые секунды"},
        {"t": "00:10", "beat": "контекст", "purpose": "mock: ввести в тему"},
    ],
    "narration_style": "mock style",
    "curiosity_gaps": ["mock gap"],
    "cta": "",
    "takeaways": ["mock takeaway"],
}

_DISTILL = {
    "verdicts": [],
    "new_rules": [
        {
            "id": "mock_hook_question",
            "text": "mock: начинать с вопроса в первой фразе",
            "tier": "should",
            "scope": "universal",
        },
        {
            "id": "mock_clip_length",
            "text": "mock: клип 2 секунды",
            "tier": "must",
            "scope": "mock niche",
        },
    ],
}

_TAXONOMY = {
    "niche": "mock niche",
    "audience": "mock audience",
    "formats": [
        {"id": "mock_a", "name": "Формат А", "description": "первый мок-формат"},
        {"id": "mock_b", "name": "Формат Б", "description": "второй мок-формат"},
    ],
    "rpm_suggestion": {"long": [1.0, 2.0], "shorts": [0.1, 0.2]},
    "notes": "mock notes",
}


class MockIntelLlm(LlmProvider):
    name = "mock"

    def __init__(self) -> None:
        self.calls = 0  # тесты идемпотентности считают реальные обращения
        self.image_calls = 0  # сколько раз спрашивали с картинками

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        images: list[Path] | None = None,
    ) -> LlmResult:
        self.calls += 1
        self.image_calls += 1 if images else 0
        if "playbook" in prompt:
            return LlmResult(text=json.dumps(_DISTILL, ensure_ascii=False))
        if "CONTENT FORMULA" in prompt:
            return LlmResult(text=json.dumps(_FORMULA, ensure_ascii=False))
        if "MEASURED EDITING RHYTHM" in prompt:
            return LlmResult(text=json.dumps(_VISUAL, ensure_ascii=False))
        if "TRANSCRIPT" in prompt:
            return LlmResult(text=json.dumps(_DEEP, ensure_ascii=False))
        if '"video_id"' not in prompt:
            return LlmResult(text=json.dumps(_TAXONOMY, ensure_ascii=False))
        ids = re.findall(r'"video_id":\s*"([^"]+)"', prompt)
        answer = [
            {
                "video_id": vid,
                "format": "mock_a" if i % 2 == 0 else "mock_b",
                "topic": "mock topic",
                "title_pattern": "mock pattern",
            }
            for i, vid in enumerate(ids)
        ]
        return LlmResult(text=json.dumps(answer, ensure_ascii=False))

    def check_images(
        self, images: list[Path], *, references: list[Reference], checklist: list[str], context: str
    ) -> CheckResult:
        return CheckResult(passed=True, raw="mock")
