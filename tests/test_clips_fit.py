"""Нарезки мультсериала (17.09.2026, cartoon_clips): кадр целиком на поле
(`reframe_fit`), подпись над кадром (`clip_headline`) и выброс скучных реплик
внутри момента (`clip_tighten`). Всё за флагами проекта — без них нарезки
подкастов собираются и выбираются как раньше."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from videoforge.clips.highlight_prompts import critique_prompt
from videoforge.clips.highlights import (
    TIGHT_MIN_SEC,
    Candidate,
    Piece,
    apply_verdicts,
    episode_from_candidate,
    moment_sentences,
    tighten_pieces,
)
from videoforge.clips.source import Sentence
from videoforge.core import headline as headlines
from videoforge.core.editplan import default_plan
from videoforge.core.ffmpeg import fit_rect, fit_video, frame_at, run
from videoforge.core.io import write_model
from videoforge.core.manifest import load_manifest
from videoforge.core.project import ProjectConfig, ProjectSpec, VideoConfig, create_project
from videoforge.core.project import load_project as load_project_cfg
from videoforge.core.resources import CaptionStyle, load_resources, search_dirs
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions, StageContext
from videoforge.core.storyboard import (
    ReframeWindow,
    Scene,
    Storyboard,
    load_storyboard,
    save_storyboard,
)
from videoforge.providers.base import ProviderSet
from videoforge.stages.assemble import fit_band, headline_for, inputs_fingerprint, under_fit_frame
from videoforge.stages.reframe import (
    Reframe,
    Window,
    _summary,
    fit_frame,
    fit_plan,
    stacked_spans,
    window_for_scene,
)

OUT = (1080, 1920)

# ---------------------------------------------------------------- кадр на поле


def test_fit_puts_the_whole_frame_above_the_middle() -> None:
    """16:9 целиком по ширине вертикали: 1080×608, центр на 45% высоты — над
    кадром место под подпись, под ним под субтитры, всё выше зоны интерфейса."""
    r = fit_rect((1920, 1080), OUT)
    assert (r.crop_x, r.crop_w, r.crop_h) == (0, 1920, 1080)
    assert (r.x, r.width, r.height) == (0, 1080, 608)
    assert r.y == 560 and r.y + r.height < 1920 * (1 - 0.17)
    # punch 16/9 — квадрат из середины исходника, кадр крупнее
    sq = fit_rect((1920, 1080), OUT, punch=16 / 9)
    assert (sq.crop_w, sq.width, sq.height, sq.crop_x) == (1080, 1080, 1080, 420)
    # кусок у края не выезжает за исходник
    assert fit_rect((1920, 1080), OUT, punch=16 / 9, x_share=0.0).crop_x == 0
    assert fit_rect((1920, 1080), OUT, punch=16 / 9, x_share=1.0).crop_x == 840
    # горизонтальный выход: 16:9 в 16:9 — кадр во весь экран
    full = fit_rect((1920, 1080), (1920, 1080))
    assert (full.x, full.y, full.width, full.height) == (0, 0, 1920, 1080)


def test_fit_zoom_cuts_the_source_logo_off_the_bottom() -> None:
    """Логотип канала-источника вшит в склейки на y 973–1003 из 1080. zoom 1.13,
    прижатый к верху, режет кадр по 954 — логотип за краем, пропорция 16:9."""
    r = fit_rect((1920, 1080), OUT, zoom=1.13, y_share=0.0)
    assert (r.crop_y, r.crop_h) == (0, 954) and r.crop_y + r.crop_h < 973
    assert r.crop_x == (1920 - r.crop_w) // 2 // 2 * 2
    assert abs(r.crop_w / r.crop_h - 16 / 9) < 0.01 and r.width == 1080
    # без zoom кусок во всю высоту, как было: паспорта до поля не устаревают
    assert fit_rect((1920, 1080), OUT).crop_y == 0
    plan = fit_plan(
        ReframeWindow(layout="fit", zoom=1.13, y=0.0), width=1920, height=1080, target=OUT
    )
    assert plan.y == 0 and plan.crop_h == 954
    with pytest.raises(ValueError):
        ReframeWindow(layout="fit", zoom=0.9)


def test_fit_plan_is_one_static_frame_not_a_stack() -> None:
    """Раскладка fit не зовёт детектор и не считается стеком: QA не ищет шов
    половин, а сообщение стадии говорит, где лежит кадр."""
    plan = fit_plan(ReframeWindow(layout="fit", fill="blur"), width=1920, height=1080, target=OUT)
    assert plan.by == "fit" and plan.fill == "blur"
    assert plan.frame == (0, 560, 1080, 608)
    assert [w.layout for w in plan.windows] == ["fit"]
    assert stacked_spans(plan.windows, 30.0) == []
    assert "1080×608" in _summary(plan) and "blur" in _summary(plan)
    # старые раскладки считаются стеком, как и раньше
    two = [Window(start_sec=0.0, x=0, layout="two_up")]
    assert stacked_spans(two, 10.0) == [(0.0, 10.0)]


def test_channel_window_applies_where_the_scene_has_none(projects_root: Path) -> None:
    """Окно канала (`video.reframe`) — для сцен без своего; своё побеждает. У
    шаблона проекта окна нет, подписи и выброса реплик — тоже."""
    pdir, _ = create_project(ProjectSpec(id="rm", workflow="clips_from_long"))
    cfg = load_project_cfg("rm")
    assert cfg.video.reframe is None and not cfg.video.headline and not cfg.video.tighten
    text = (pdir / "project.yaml").read_text(encoding="utf-8")
    assert "reframe: null" in text and "headline: false" in text and "tighten: false" in text

    channel = ReframeWindow(layout="fit")
    ctx = SimpleNamespace(project=ProjectConfig(id="rm", video=VideoConfig(reframe=channel)))
    own = ReframeWindow(layout="single", x=0.3)
    assert window_for_scene(ctx, Scene(id="s1", order=1)) == channel  # type: ignore[arg-type]
    assert window_for_scene(ctx, Scene(id="s2", order=2, reframe=own)) == own  # type: ignore[arg-type]
    assert window_for_scene(SimpleNamespace(project=None), Scene(id="s", order=1)) is None  # type: ignore[arg-type]


def test_fit_video_draws_the_frame_on_black_or_blur(tmp_path: Path) -> None:
    """Рендер: кадр на своём месте, поле над ним чёрное (black) или цветное
    (blur — размытая копия того же кадра)."""
    src = tmp_path / "src.mp4"
    run(["-f", "lavfi", "-i", "color=c=red:size=320x180:rate=10:duration=0.5",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)])  # fmt: skip
    size = (180, 320)
    rect = fit_rect((320, 180), size)
    for fill in ("black", "blur"):
        out = tmp_path / f"{fill}.mp4"
        fit_video(src, out, rect=rect, size=size, fill=fill, fps=10)
        png = tmp_path / f"{fill}.png"
        frame_at(out, 0.2, png)
        img = Image.open(png).convert("RGB")
        assert img.size == size
        inside = img.getpixel((size[0] // 2, rect.y + rect.height // 2))
        above = img.getpixel((size[0] // 2, max(0, rect.y - 20)))
        assert inside[0] > 150 and inside[1] < 80  # красный кадр
        if fill == "black":
            assert max(above) < 30
        else:
            assert above[0] > 60  # размытая красная копия, а не чернота


def _passport(video: Path, frame: tuple[int, int, int, int] | None) -> None:
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"")
    write_model(
        video.with_suffix(".json"),
        Reframe(
            source="03_video/s.mp4",
            width=1920,
            height=1080,
            windows=[Window(start_sec=0.0, x=0, layout="fit" if frame else "single")],
            by="fit" if frame else "center",
            crop_w=1920,
            crop_h=1080,
            frame=frame,
        ),
    )


def _ctx(tmp_path: Path, frames: list[tuple[int, int, int, int] | None], **video: object):  # type: ignore[no-untyped-def]
    scenes = []
    for n, frame in enumerate(frames, start=1):
        rel = f"03_video/s{n:02d}_reframed.mp4"
        _passport(tmp_path / rel, frame)
        scene = Scene(id=f"s{n:02d}", order=n)
        scene.assets.video = rel
        scenes.append(scene)
    sb = Storyboard(project_id="rm", scenes=scenes, headline="no way he said that 😭")
    return SimpleNamespace(
        storyboard=sb,
        abs=lambda rel: tmp_path / rel,
        project=ProjectConfig(id="rm", video=VideoConfig(**video)),  # type: ignore[arg-type]
    )


def test_captions_go_under_the_fit_frame_only_when_every_scene_is_fit(tmp_path: Path) -> None:
    """Субтитры под самым низким кадром эпизода; хоть одна сцена кадрирована
    иначе — стиль как есть (кадр во весь экран)."""
    style = CaptionStyle(id="x", position="bottom", margin_pct=22.0)
    ctx = _ctx(tmp_path, [(0, 560, 1080, 608), (0, 324, 1080, 1080)])
    assert fit_band(ctx) == (324, 1404)  # type: ignore[arg-type]
    moved = under_fit_frame(ctx, style)  # type: ignore[arg-type]
    assert moved.position == "top" and moved.margin_pct == pytest.approx(
        1404 / 1920 * 100 + 2, 0.01
    )
    assert style.position == "bottom"  # ресурс не тронут — копия

    mixed = _ctx(tmp_path / "m", [(0, 560, 1080, 608), None])
    assert fit_band(mixed) is None  # type: ignore[arg-type]
    assert under_fit_frame(mixed, style) == style  # type: ignore[arg-type]
    assert fit_frame(tmp_path / "nope.mp4") is None


# ---------------------------------------------------------------- подпись над кадром


def test_headline_wraps_and_sits_above_the_frame(tmp_path: Path) -> None:
    out = tmp_path / "h.png"
    text = "Jerry was genuinely INCHES from death he's so lucky they ran out 😭😭"
    w, h = headlines.render(text, out, frame_width=1080)
    assert out.exists() and Image.open(out).size == (w, h)
    assert w <= 1080 and h < 400  # две-три строки, а не абзац
    x, y = headlines.place((w, h), size=OUT, frame=(0, 560, 1080, 608))
    assert x == (1080 - w) // 2 and y + h < 560  # низом над кадром
    # без кадра fit — у верха, на TOP_SHARE
    assert headlines.place((w, h), size=OUT, frame=None)[1] == round(1920 * headlines.TOP_SHARE)
    # над кадром не влезает — не уезжает за верхний край
    assert headlines.place((w, 900), size=OUT, frame=(0, 300, 1080, 1080))[1] >= 0
    # отпечаток — от текста: та же подпись даёт тот же файл
    assert headlines.fingerprint(text) == headlines.fingerprint(f" {text} ")
    assert headlines.fingerprint(text) != headlines.fingerprint(text + "!")


@pytest.mark.skipif(
    not any(p.exists() for p in headlines.EMOJI_FONTS), reason="нет шрифта цветных эмодзи"
)
def test_headline_emoji_are_drawn_in_colour(tmp_path: Path) -> None:
    """libass цветных эмодзи не рисует — ради этого подпись и картинка."""
    out = tmp_path / "e.png"
    headlines.render("😭", out, frame_width=1080)
    img = Image.open(out).convert("RGBA")
    coloured = [
        px for px in img.get_flattened_data() if px[3] > 200 and max(px[:3]) - min(px[:3]) > 80
    ]
    assert len(coloured) > 100


def test_headline_is_drawn_only_when_the_channel_wants_it(tmp_path: Path) -> None:
    ctx_off = _ctx(tmp_path / "off", [(0, 560, 1080, 608)])
    assert headline_for(ctx_off, tmp_path / "off" / "05", OUT) is None  # type: ignore[arg-type]
    ctx = _ctx(tmp_path / "on", [(0, 560, 1080, 608)], headline=True)
    made = headline_for(ctx, tmp_path / "on" / "05", OUT)  # type: ignore[arg-type]
    assert made is not None and made.path.exists() and made.y + made.height < 560
    ctx.storyboard.headline = "  "
    assert headline_for(ctx, tmp_path / "on" / "05", OUT) is None  # type: ignore[arg-type]


def test_headline_changes_the_assemble_fingerprint_only_when_present(
    episode: Path, mock_providers: ProviderSet, projects_root: Path
) -> None:
    """Подпись — часть картинки; её нет — отпечаток тот же, что до поля."""
    sb = load_storyboard(episode)
    pdir = projects_root / "t"
    ctx = StageContext(
        stage_name="assemble",
        project_dir=episode,
        storyboard=sb,
        manifest=load_manifest(episode),
        providers=mock_providers,
        resources=load_resources(sb.resources, search_dirs(pdir)),
        options=RunOptions(),
        project=load_project_cfg("t"),
        project_root=pdir,
        resource_dirs=search_dirs(pdir),
    )
    scenes = sb.ordered_scenes()
    cfg = ctx.project.video if ctx.project else VideoConfig()

    def fp(headline: headlines.Headline | None) -> str:
        return inputs_fingerprint(
            ctx, scenes, plan=default_plan(sb), style=None, track=None, cfg=cfg,
            headline=headline,
        )  # fmt: skip

    made = headlines.Headline(Path("headline_a.png"), "t", 89, 291, 902, 227)
    other = headlines.Headline(Path("headline_b.png"), "u", 89, 291, 902, 227)
    before = inputs_fingerprint(ctx, scenes, plan=default_plan(sb), style=None, track=None, cfg=cfg)
    assert before == fp(None) and len({before, fp(made), fp(other)}) == 3


@pytest.mark.render
def test_cartoon_clip_goes_fit_with_headline_and_captions_under_the_frame(
    clip_episode: Path, mock_providers: ProviderSet
) -> None:
    """Сквозной путь канала мультсериала на заглушке: окно канала fit → кадр на
    поле, подпись над кадром, субтитры под ним; смена поля перекадрирует."""
    pdir = clip_episode.parent.parent
    cfg = pdir / "project.yaml"
    text = cfg.read_text(encoding="utf-8")
    text = text.replace("  reframe: null", "  reframe: {layout: fit}", 1)
    text = text.replace("  headline: false", "  headline: true", 1)
    cfg.write_text(text, encoding="utf-8")
    sb = load_storyboard(clip_episode)
    sb.headline = "genuinely the best line in the show 😭"
    save_storyboard(clip_episode, sb)

    for stage in ("cut", "reframe", "assemble"):
        report = run_episode(clip_episode, mock_providers, RunOptions(only=stage))
        assert report.ok, report.reason
    sb = load_storyboard(clip_episode)
    scene = sb.scenes[0]
    assert scene.provenance["reframe"] == "fit" and "reframe" not in scene.checks.notes
    frame = fit_frame(clip_episode / scene.assets.video)
    assert frame is not None
    _, top, _, height = frame
    assert sb.final.headline == sb.headline
    ass = (clip_episode / "05_assemble" / "captions.ass").read_text(encoding="utf-8")
    margin_v = int(
        next(x for x in ass.splitlines() if x.startswith("Style: Default")).split(",")[-2]
    )
    assert margin_v > top + height  # верх реплики под кадром

    png = frame_at(clip_episode / sb.final.video, 5.0, clip_episode / "f.png")  # type: ignore[operator]
    img = Image.open(png).convert("RGB")
    band = [img.getpixel((x, y)) for x in range(0, 1080, 4) for y in range(top - 200, top - 40, 4)]
    assert sum(1 for px in band if min(px) > 200) > 50  # белые буквы подписи над кадром
    assert max(img.getpixel((540, 40))) < 30  # над подписью — чёрное поле

    # поле канала сменили — клип перекадрирован, а не взят готовый
    out = clip_episode / scene.assets.video
    stamp = out.stat().st_mtime_ns
    cfg.write_text(
        text.replace("  reframe: {layout: fit}", "  reframe: {layout: fit, fill: blur}", 1),
        encoding="utf-8",
    )
    assert run_episode(clip_episode, mock_providers, RunOptions(only="reframe")).ok
    assert out.stat().st_mtime_ns != stamp


# ---------------------------------------------------------------- выброс реплик


def _lines() -> list[Sentence]:
    """Шесть реплик сцены по 5 с с паузой 1.5 с: 0–5, 6.5–11.5, …"""
    return [
        Sentence(start_sec=i * 6.5, end_sec=i * 6.5 + 5, text=f"Line {i + 1}.") for i in range(6)
    ]


def test_tighten_cuts_the_middle_and_never_the_edges() -> None:
    lines = _lines()
    pieces, removed, note = tighten_pieces(lines, {3, 4})
    assert not note and removed == ["Line 3.", "Line 4."]
    assert [(p.start_sec, p.end_sec) for p in pieces] == [(0.0, 11.5), (26.0, 37.5)]
    assert pieces[0].text == "Line 1. Line 2."
    # первая и последняя реплики не выбрасываются: начало держит хук, конец — развязку
    assert tighten_pieces(lines, {1, 6}) == ([], [], "")
    # выброс, после которого осталось бы меньше TIGHT_MIN_SEC, не принимается
    pieces, removed, note = tighten_pieces(lines, {2, 3, 4, 5})
    assert not pieces and not removed and f"{TIGHT_MIN_SEC:g}" in note


def test_tighten_does_not_cut_for_half_a_second() -> None:
    """Вырез короче MIN_DROP_SEC склеивается обратно: реплика без паузы вокруг."""
    tight = [
        Sentence(start_sec=0.0, end_sec=9.0, text="A."),
        Sentence(start_sec=9.1, end_sec=9.6, text="Uh."),
        Sentence(start_sec=9.7, end_sec=20.0, text="B."),
        Sentence(start_sec=21.5, end_sec=30.0, text="C."),
    ]
    assert tighten_pieces(tight, {2}) == ([], [], "")


def test_verdict_brings_the_headline_and_the_pieces() -> None:
    lines = _lines()
    cands = [
        Candidate(id="h01", start_sec=0.0, end_sec=37.5, text="…"),
        Candidate(id="h02", start_sec=0.0, end_sec=37.5, text="…"),
    ]
    raw = {
        "verdicts": [
            {"id": "h01", "keep": True, "why": "lands", "title": "T", "headline": "bro 😭",
             "drop": [3, "4", "x"]},
            {"id": "h02", "keep": True, "why": "ok", "title": "U", "drop": [2, 3, 4, 5]},
        ]
    }  # fmt: skip
    fixes: list[str] = []
    kept, _ = apply_verdicts(cands, raw, sentences=lines, fixes=fixes)
    assert kept[0].headline == "bro 😭" and len(kept[0].pieces) == 2
    assert kept[0].played_sec == pytest.approx(23.0) and kept[0].duration_sec == 37.5
    assert not kept[1].pieces and fixes and fixes[0].startswith("h02")
    # без транскрипта (канал не включил выброс) drop не читается
    fresh = [Candidate(id="h01", start_sec=0.0, end_sec=37.5)]
    kept, _ = apply_verdicts(fresh, raw)
    assert not kept[0].pieces
    assert len(moment_sentences(lines, Candidate(id="x", start_sec=6.5, end_sec=24.5))) == 3


def test_tightened_moment_becomes_one_scene_per_piece(projects_root: Path) -> None:
    create_project(ProjectSpec(id="c", workflow="clips_from_long"))
    cand = Candidate(
        id="h01",
        start_sec=0.0,
        end_sec=37.5,
        text="all",
        publish_title="Title",
        headline="no way 😭",
        pieces=[
            Piece(start_sec=0.0, end_sec=11.5, text="Line 1. Line 2."),
            Piece(start_sec=26.0, end_sec=37.5, text="Line 5. Line 6."),
        ],
    )
    sb = load_storyboard(episode_from_candidate("c", "vid1", cand))
    assert sb.headline == "no way 😭"
    assert [s.id for s in sb.scenes] == ["s01", "s02"]
    assert [(s.source.start_sec, s.source.end_sec) for s in sb.scenes if s.source] == [
        (0.0, 11.5),
        (26.0, 37.5),
    ]
    assert sb.scenes[1].narration == "Line 5. Line 6." and sb.scenes[1].duration_sec == 11.5
    # без выброса — одна сцена на весь момент, как было
    whole = cand.model_copy(update={"id": "h02", "pieces": []})
    (scene,) = load_storyboard(episode_from_candidate("c", "vid1", whole)).scenes
    assert scene.id == "s01" and scene.source and scene.source.end_sec == 37.5


def test_critic_prompt_is_unchanged_without_channel_flags() -> None:
    """Каналы без флагов получают тот же промпт критика: без правил канала,
    без подписи, без номеров реплик."""
    lines = _lines()
    cands = [Candidate(id="h01", start_sec=0.0, end_sec=37.5, text="Line 1. Line 2.")]
    plain = critique_prompt(cands, source_title="S", language="en", title_max=90, rules="RULES")
    assert "CHANNEL RULES" not in plain and "headline" not in plain and "[1]" not in plain
    assert '"drop"' not in plain and "RULES" not in plain

    full = critique_prompt(
        cands,
        source_title="S",
        language="en",
        title_max=90,
        rules="RULES",
        headline=True,
        tighten=True,
        sentences=lines,
    )
    assert "CHANNEL RULES" in full and "RULES" in full
    assert '"headline": "..."' in full and '"drop": []' in full
    assert "[1] (5.0 s) Line 1." in full and "[6] (5.0 s) Line 6." in full


def test_captions_stay_on_the_picture_when_the_room_under_it_is_platform_ui(
    tmp_path: Path,
) -> None:
    """Высокий кадр (кроп 3:4 с размытыми полями) оставляет под собой полосу,
    которую закрывает блок «канал / описание» площадки: реплика под кадром там
    не читается, и стиль остаётся как есть — слово поверх картинки."""
    style = CaptionStyle(id="x", position="bottom", margin_pct=30.0)
    tall = _ctx(tmp_path, [(0, 240, 1080, 1440)])
    tall.project.channel.platforms = ["youtube_shorts"]
    assert under_fit_frame(tall, style) == style  # type: ignore[arg-type]


def test_new_empty_style_fields_keep_old_finals_fresh() -> None:
    """Новое поле стиля не должно разом состарить все собранные финалы: пустой
    градиент и выключенная чистка знаков в отпечаток сборки не идут."""
    import inspect

    from videoforge.stages import assemble

    source = inspect.getsource(assemble.inputs_fingerprint)
    assert '("gradient_to", "strip_punctuation") if not getattr(style, name)' in source


def test_episode_grade_beats_project_grade_and_names_a_real_filter(tmp_path: Path) -> None:
    """Коррекция ролика: поле эпизода побеждает поле проекта (пробы одного
    материала в разных коррекциях лежат соседними эпизодами), а каждое имя
    пресета — настоящая цепочка фильтров ffmpeg."""
    from videoforge.core.ffmpeg import GRADES
    from videoforge.stages.assemble import grade_for

    ctx = _ctx(tmp_path, [(0, 240, 1080, 1440)], grade="lift")
    assert grade_for(ctx) == "lift"  # type: ignore[arg-type]
    ctx.storyboard.grade = "warm"
    assert grade_for(ctx) == "warm"  # type: ignore[arg-type]
    assert set(GRADES) == {"lift", "punch", "vivid", "warm"}
    assert all("=" in chain for chain in GRADES.values())


def test_every_episode_gets_its_own_part_of_the_track(tmp_path: Path) -> None:
    """Трек с точками входа отдаёт каждому ролику свою часть — по id эпизода, а
    не случайно: пересборка музыку не меняет. Посадка из плана монтажа
    побеждает; трек без точек идёт с начала."""
    from videoforge.core.editplan import EditPlan, MusicPlan
    from videoforge.core.resources import MusicTrack
    from videoforge.stages.assemble import music_start

    track = MusicTrack(id="t", starts=[0, 36.25, 74.94, 110.1])
    picked = set()
    for n in range(40):
        ctx = _ctx(tmp_path / str(n), [(0, 240, 1080, 1440)])
        ctx.storyboard.episode_id = f"ep{n:03d}"
        start = music_start(ctx, EditPlan(), track)  # type: ignore[arg-type]
        assert start == music_start(ctx, EditPlan(), track)  # type: ignore[arg-type]
        assert -start in track.starts
        picked.add(start)
    assert len(picked) == len(track.starts)  # сорок роликов разошлись по всем точкам

    planned = EditPlan(music=MusicPlan(start_sec=-12.0))
    assert music_start(ctx, planned, track) == -12.0  # type: ignore[arg-type]
    assert music_start(ctx, EditPlan(), MusicTrack(id="plain")) == 0.0  # type: ignore[arg-type]
