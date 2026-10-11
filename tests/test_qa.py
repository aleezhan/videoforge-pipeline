"""QA финала: замеры, зона интерфейса площадки, стыки и кадры внутри эпизода
через vision-модель — совет на воротах, не цикл."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core.captions import caption_band
from videoforge.core.manifest import load_manifest
from videoforge.core.qa import load_qa, qa_path
from videoforge.core.report import render_report
from videoforge.core.resources import CaptionStyle
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.providers.mock import MockLlm
from videoforge.stages.qa import (
    FRAME_OFFSET_SEC,
    SPOT_FRAMES,
    _frame_times,
    caption_contrast_finding,
    caption_zone_findings,
    contrast_ratio,
    luminance,
    spot_times,
)

OPTS = RunOptions(auto_approve=True, raise_errors=True)
# вызовов модели на один прогон QA: пакет стыков + один пакет кадров внутри
QA_CALLS = 2


def test_caption_contrast_is_measured_not_judged() -> None:
    """Замер вместо мнения: vision-модель на одном и том же ролике то называла
    субтитры нечитаемыми, то нет. Яркость фона под ними — число."""
    style = CaptionStyle(id="t")  # белый текст, жёлтая подсветка
    assert caption_contrast_finding(style, []) is None
    assert caption_contrast_finding(style, [(float(i), 20.0) for i in range(20)]) is None

    bright = caption_contrast_finding(style, [(float(i), 250.0) for i in range(20)])
    assert bright is not None and bright.severity == "warn" and "сливаются" in bright.message

    tight = caption_contrast_finding(style, [(float(i), 140.0) for i in range(20)])
    assert tight is not None and tight.severity == "info" and "впритык" in tight.message

    dark_text = CaptionStyle(id="t", color="#101010", highlight="#202020")
    assert caption_contrast_finding(dark_text, [(float(i), 250.0) for i in range(20)]) is None

    # подложка лежит между текстом и кадром: белый текст на белой футболке под
    # тёмным стеклом 0.6 — контраст считается к смеси, и замечание уходит
    boxed = CaptionStyle(id="t", box_opacity=0.6)
    assert caption_contrast_finding(boxed, [(float(i), 250.0) for i in range(20)]) is None

    assert luminance("#FFFFFF") == 1.0 and luminance("#000000") == 0.0
    assert round(contrast_ratio(1.0, 0.0), 1) == 21.0  # предельный контраст по WCAG


def test_readability_is_decided_by_the_outline_not_by_the_background() -> None:
    """Букву на пёстром фоне держит ОБВОДКА, а не разница «текст против фона».

    Пока проверка её не знала, она горела на всех четырёх снятых роликах разом
    (1.7 / 2.9 / 2.8 / 3.1 при пороге 4.5), хотя на кадрах текст читался.
    Проверка, которая срабатывает всегда, сигналом быть перестаёт.

    Теперь порог — доля обводки от кегля, и замечание указывает на причину."""
    frame = (1080, 1920)
    white_shirt = [(float(i), 250.0) for i in range(20)]  # худший фон: белое на белом

    thick = CaptionStyle(id="t", size_pct=5.2, outline=5.0)  # 5 px при кегле 100 → 5%
    assert caption_contrast_finding(thick, white_shirt, frame=frame) is None

    thin = CaptionStyle(id="t", size_pct=5.2, outline=2.0)  # 2%
    finding = caption_contrast_finding(thin, white_shirt, frame=frame)
    assert finding is not None and "обводка" in finding.message

    # обводка цвета текста — это отсутствие обводки, какой бы толстой ни была
    invisible = CaptionStyle(id="t", size_pct=5.2, outline=8.0, outline_color="#FFFFFF")
    assert caption_contrast_finding(invisible, white_shirt, frame=frame) is not None


def test_spot_frames_are_spread_over_the_body_of_the_episode() -> None:
    """Кадры внутри берутся равномерно и не с краёв: там fade, а не картинка."""
    times = spot_times(100.0)
    assert len(times) == SPOT_FRAMES
    assert times == sorted(times) and 8.0 < times[0] and times[-1] < 92.0
    assert spot_times(0.0) == []  # нечего смотреть
    assert all(0.0 < t < 1.0 for t in spot_times(1.0))  # короткий ролик — всё равно внутри


def test_cut_frames_are_taken_outside_the_transition() -> None:
    off = FRAME_OFFSET_SEC
    assert _frame_times(10.0, "cut", 0.0) == (10.0 - off, 10.0 + off)
    assert _frame_times(10.0, "crossfade", 0.4) == (10.0 - off, 10.4 + off)  # растворение впереди
    assert _frame_times(10.0, "dip_black", 0.4) == (
        9.8 - off,
        10.2 + off,
    )  # по половине с каждой стороны
    assert _frame_times(10.0, "fade_in", 0.5) == (10.0 - off, 10.5 + off)


def _llm(providers: ProviderSet) -> MockLlm:
    assert isinstance(providers.llm, MockLlm)
    return providers.llm


def test_caption_band_and_platform_zones() -> None:
    style = CaptionStyle(id="t")  # низ, margin 22% — под вертикальные соцсети
    top, bottom = caption_band(style, (1080, 1920))
    assert bottom == 0.78 and 0.7 < top < bottom
    top_style = CaptionStyle(id="t", position="top")
    assert caption_band(top_style, (1080, 1920))[0] == 0.22

    # ради этого 22% и поставлены: строка проходит зону интерфейса всех трёх площадок
    for platform in ("youtube_shorts", "tiktok", "instagram_reels"):
        found = caption_zone_findings(style, (1080, 1920), [platform])
        assert not [f for f in found if f.severity == "warn"], (platform, found)

    low = CaptionStyle(id="t", margin_pct=12)  # как было до 05.09.2026
    codes = {(f.code, f.severity) for f in caption_zone_findings(low, (1080, 1920), ["tiktok"])}
    assert ("caption_zone", "warn") in codes  # 12% от низа < 17% интерфейса TikTok
    assert ("caption_side", "info") in codes  # 6% по бокам < 15% под кнопками
    # горизонтальному видео 22% ни к чему, но и не мешают: там интерфейс 10% снизу
    assert not caption_zone_findings(style, (1920, 1080), ["youtube"])
    wide = CaptionStyle(id="t", margin_pct=12, side_margin_pct=16)
    assert not caption_zone_findings(wide, (1920, 1080), ["youtube"])


@pytest.mark.render
def test_qa_reviews_cuts_and_advises_at_the_gate(
    episode: Path, mock_providers: ProviderSet
) -> None:
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    qa = load_qa(episode)
    assert qa is not None and qa.llm == "mock"
    assert qa.final_fingerprint == load_storyboard(episode).final.inputs_fingerprint
    assert [c.cut_id for c in qa.cuts] == ["s1>s2"]
    cut = qa.cuts[0]
    assert cut.seam == "visible" and cut.captions == "ok"  # вердикт заглушки
    assert cut.transition == "dip_black"  # план заглушки: кадры взяты за пределами затемнения
    assert len(cut.frames) == 2 and all((episode / f).exists() for f in cut.frames)
    codes = {f.code for f in qa.findings}
    # шов от модели + кадр внутри эпизода; зоны интерфейса нет — стиль по
    # умолчанию её проходит, и это как раз то, чего мы добивались
    assert {"cut", "frame"} <= codes and "caption_zone" not in codes
    message = load_manifest(episode).stages["qa"].message
    assert message.startswith("QA:") and "шов виден" in message and "решает человек" in message
    assert _llm(mock_providers).qa_calls == QA_CALLS

    text = render_report(episode, load_storyboard(episode), load_manifest(episode)).read_text(
        encoding="utf-8"
    )
    assert "QA финала" in text and "шов виден" in text and "мок: композиция" in text
    assert "кадров внутри" in text and "мок: субтитры сливаются" in text


@pytest.mark.render
def test_qa_looks_inside_the_episode_not_only_at_the_seams(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Дыра, которую нашёл первый шортс: у эпизода из одной сцены стыков нет, и
    раньше картинку не проверял никто."""
    run_episode(episode, mock_providers, OPTS)
    qa = load_qa(episode)
    assert qa is not None and len(qa.frames) == SPOT_FRAMES
    assert all((episode / f.frame).exists() for f in qa.frames)
    assert all(f.scene_id for f in qa.frames)  # каждый кадр знает свою сцену
    first, rest = qa.frames[0], qa.frames[1:]
    assert first.captions == "hard_to_read" and not first.clean  # вердикт заглушки
    assert all(f.clean for f in rest)
    found = [f for f in qa.findings if f.code == "frame"]
    assert len(found) == 1 and "субтитры плохо читаются" in found[0].message


@pytest.mark.render
def test_qa_is_reused_for_the_same_final(episode: Path, mock_providers: ProviderSet) -> None:
    run_episode(episode, mock_providers, OPTS)
    llm = _llm(mock_providers)
    stamp = qa_path(episode).stat().st_mtime_ns
    report = run_episode(episode, mock_providers, OPTS)
    assert report.ok, report.reason
    assert llm.qa_calls == QA_CALLS and qa_path(episode).stat().st_mtime_ns == stamp
    assert "QA актуален" in load_manifest(episode).stages["qa"].message
    forced = run_episode(
        episode,
        mock_providers,
        RunOptions(only="qa", auto_approve=True, raise_errors=True, force=True),
    )
    assert forced.ok and llm.qa_calls == 2 * QA_CALLS


def test_qa_without_final_fails_honestly(episode: Path, mock_providers: ProviderSet) -> None:
    report = run_episode(episode, mock_providers, RunOptions(only="qa", auto_approve=True))
    assert not report.ok and report.stopped_at == "qa"
    assert "assemble" in (report.reason or "")
