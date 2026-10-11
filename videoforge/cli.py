"""CLI.

videoforge project  new | list | show      project = channel/series
videoforge episode  new | list             episode = one video
videoforge run | status | approve | timings <project>/<episode>
                                           timings: time spent per stage and per step
videoforge yield [<project>]               attempts it takes to get an accepted frame or clip
videoforge publish  <project>/<episode> --to youtube,tiktok
                                           upload a finished video; without --yes, dry run
videoforge estimate | report | preview <project>/<episode>
                                           forecast · HTML report · platform UI safe zones
videoforge resource new | show | list | add-ref | gen-portfolio
                                           characters, styles, voices, niches
videoforge analyze  new | run | status | approve | report | list
                                           analysis of third-party content (research/)
videoforge source   add | list | show | highlights | pick
                                           clip sources and highlights from them
videoforge lib      decisions | list | show | new | check | index
                                           technique library: what to apply and when
videoforge account  new | list | show | sync | login
                                           platform accounts (accounts/): where the
                                           project's videos go and stats snapshots;
                                           login: manual sign-in for browser publishing
videoforge feature new | list | show | status | areas
                                           plans and capabilities (features/)
videoforge link    new | list | show | kinds
                                           useful external sites and services (links/)
videoforge voices | say                    pick a voice by ear
videoforge ui                              browser UI on top of the same files
videoforge doctor
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import typer
import yaml

from videoforge import __version__
from videoforge.clips.highlights import DEFAULT_COUNT
from videoforge.config import ProviderNames, has_key, load_env
from videoforge.core.ffmpeg import SCENE_THRESHOLD
from videoforge.core.manifest import StageRecord, load_manifest
from videoforge.core.production import KINDS as YIELD_KINDS
from videoforge.core.production import Yield, load_episodes, measure
from videoforge.core.project import (
    PLATFORMS,
    ProjectSpec,
    approve_stage,
    create_episode,
    create_project,
    list_episodes,
    list_projects,
    load_project,
    load_rules_from_dir,
    missing_resources,
    parse_ref,
    project_blueprint,
    resolve_episode,
)
from videoforge.core.resources import (
    KINDS,
    VISUAL_KINDS,
    Character,
    MusicTrack,
    Style,
    VisualResource,
    Voice,
    add_reference_image,
    create_resource,
    list_resources,
    load_resource,
    rename_resource,
    resource_kind,
    search_dirs,
)
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard
from videoforge.core.workflow import list_workflows, load_workflow
from videoforge.paths import (
    ROOT,
    episodes_dir,
    project_config_path,
    project_dir,
    projects_dir,
    resources_dir,
)
from videoforge.providers.base import ProviderError


def force_utf8_output() -> None:
    """Write UTF-8 to any console.

    The Windows console defaults to cp1251, and any string with an arrow or dash
    (even in `--help`) crashes the command with UnicodeEncodeError. Called at
    module import, before typer prints anything.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # stream replaced (captured in tests): leave it alone
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # stream already closed or not reconfigurable
            pass


force_utf8_output()

app = typer.Typer(
    help="videoforge: a file-based pipeline for producing video content.",
    no_args_is_help=True,
    add_completion=False,
)
project_app = typer.Typer(help="Project = channel/series: project.yaml + rules.md + episodes/.")
episode_app = typer.Typer(help="Episode = one video inside a project.")
resource_app = typer.Typer(help="Resources: characters, styles, voices, niches (resources/).")
analyze_app = typer.Typer(
    help="Third-party content analysis: channel → dynamics → outliers (research/)."
)
source_app = typer.Typer(
    help="Clip sources: a long video + transcript (projects/<project>/sources/)."
)
lib_app = typer.Typer(
    help="Technique library: what to apply at a decision point and when (library/)."
)
gpu_app = typer.Typer(
    help="Rented GPU for ComfyUI (RunPod): up → .gpu/pod.json, down terminates it."
)
feature_app = typer.Typer(
    help="Plans and capabilities: what the tool can do and what was decided (features/)."
)
link_app = typer.Typer(help="Useful external sources: sites, services, channels, articles.")
todo_app = typer.Typer(help="Human tasks: things to do by hand (todo.md), each with a code: T-07.")
account_app = typer.Typer(
    help="Platform accounts (accounts/): which project each belongs to, stats snapshots."
)
bench_app = typer.Typer(
    help="Benchmarks: bench.yaml → takes with provenance → verdicts in the UI → "
    "summary (projects/_bench/<id>/)."
)
app.add_typer(project_app, name="project")
app.add_typer(episode_app, name="episode")
app.add_typer(resource_app, name="resource")
app.add_typer(analyze_app, name="analyze")
app.add_typer(source_app, name="source")
app.add_typer(lib_app, name="lib")
app.add_typer(gpu_app, name="gpu")
app.add_typer(account_app, name="account")
app.add_typer(feature_app, name="feature")
app.add_typer(link_app, name="link")
app.add_typer(todo_app, name="todo")
app.add_typer(bench_app, name="bench")


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v", help="verbose log")) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_env(ROOT)


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _episode(ref: str) -> Path:
    try:
        return resolve_episode(ref)
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
        raise AssertionError from None  # unreachable


# ---------------------------------------------------------------- project


@project_app.command("new")
def project_new(
    project_id: str = typer.Argument(help="Latin snake_case id: country_telling"),
    name: str = typer.Option("", help="human-readable name"),
    description: str = typer.Option("", help="what the channel is about, 1-2 sentences"),
    workflow: str = typer.Option(
        "ai_video", help="ai_video (from scratch) | clips_from_long (clips from a long video)"
    ),
    kind: str = typer.Option(
        "channel", help="channel: channel/series | test: quality sandbox (the Tests page)"
    ),
    channel_name: str = typer.Option("", help="channel/account name"),
    platforms: str = typer.Option(
        "youtube_shorts", help="comma-separated: " + ", ".join(PLATFORMS)
    ),
    language: str = typer.Option("ru", help="script and voiceover language"),
    aspect: str | None = typer.Option(None, help="9:16 | 16:9 | 1:1 (default: from the platform)"),
    duration: int | None = typer.Option(None, help="target duration, sec (from the platform)"),
    scene_duration: float = typer.Option(6.0, help="duration of one clip, sec"),
    character: str | None = typer.Option(None, help="id from resources/characters"),
    style: str | None = typer.Option(None, help="id from resources/styles"),
    voice: str | None = typer.Option(None, help="id from resources/voices"),
    niche: str | None = typer.Option(None, help="id from resources/niches"),
    budget: float = typer.Option(5.0, help="per-episode limit, USD"),
    research: list[str] = typer.Option(
        [], "--research", help="analysis id from research/: its blueprint goes to the planner"
    ),
) -> None:
    """Create a project: project.yaml + rules.md (commented templates) + episodes/."""
    spec = ProjectSpec(
        id=project_id,
        name=name,
        description=description,
        workflow=workflow,
        kind=kind,  # type: ignore[arg-type]
        channel_name=channel_name,
        platforms=[p.strip() for p in platforms.split(",") if p.strip()],
        language=language,
        aspect_ratio=aspect,  # type: ignore[arg-type]
        target_duration_sec=duration,
        scene_duration_sec=scene_duration,
        character=character,
        style=style,
        voice=voice,
        niche=niche,
        budget_per_episode_usd=budget,
        research=list(research),
    )
    try:
        pdir, warnings = create_project(spec)
    except (FileExistsError, ValueError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"создан проект {pdir}")
    typer.echo(f"  {pdir / 'project.yaml'}  — настройки")
    typer.echo(f"  {pdir / 'rules.md'}     — правила (заполни разделы)")
    for w in warnings:
        typer.echo(f"  ! {w}")
    typer.echo(
        f"дальше: videoforge project show {project_id} · videoforge episode new {project_id}/ep001"
    )


@project_app.command("list")
def project_list() -> None:
    """List projects: with accounts (live) and without (demo / in development)."""
    from videoforge.core.accounts import accounts_for_project

    ids = list_projects()
    if not ids:
        typer.echo(f"проектов нет ({projects_dir()})")
        return
    for pid in ids:
        cfg = load_project(pid)
        eps = list_episodes(pid)
        platforms = ", ".join(cfg.channel.platforms) or "—"
        accounts = accounts_for_project(pid)
        status = ", ".join(a.id for a in accounts) if accounts else "демо"
        if cfg.kind == "test":
            status = "полигон качества · раздел «Тесты» в пульте"
        if cfg.hidden:
            status += " · образец, в пульте скрыт"
        typer.echo(
            f"{pid:<20} {cfg.name}  · площадки: {platforms} · эпизодов: {len(eps)} · {status}"
        )


@project_app.command("show")
def project_show(project_id: str) -> None:
    """Validate and show project settings."""
    try:
        cfg = load_project(project_id)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    typer.echo(yaml.safe_dump(cfg.model_dump(), allow_unicode=True, sort_keys=False).rstrip())
    # an estimate for humans, not a quota: content decides scene length
    typer.echo(f"сцен на эпизод при медианной длине (оценка): {cfg.scenes_estimate}")
    rules = load_rules_from_dir(project_dir(project_id), cfg)
    filled = sum(1 for line in rules.splitlines() if line.startswith("- ") and line.strip() != "-")
    typer.echo(f"rules.md: {'есть' if rules else 'нет'}, заполненных пунктов: {filled}")
    for m in missing_resources(cfg.resources, search_dirs(project_dir(project_id))):
        typer.echo(f"! ресурс {m} не найден")
    eps = list_episodes(project_id)
    typer.echo(f"эпизоды: {', '.join(eps) if eps else '—'}")


# ---------------------------------------------------------------- episode


@episode_app.command("new")
def episode_new(
    ref: str = typer.Argument(help="<project>/<episode>, e.g. country_telling/ep001_japan"),
    title: str = typer.Option("", help="episode title"),
    brief: str = typer.Option("", help="what the episode is about: topic, key facts, mood"),
    check: str = typer.Option(
        "", help="what this video is testing (→ check.md next to the storyboard)"
    ),
) -> None:
    """Create an episode with the project's settings (format, resources, budget)."""
    try:
        project_id, episode_id = parse_ref(ref)
        edir = create_episode(project_id, episode_id, title=title, brief=brief, check=check)
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"создан эпизод {edir}")
    typer.echo(
        "дальше: заполни scenes в storyboard.json (или дождись планнера), "
        f"потом: videoforge run {ref} --only images --mock"
    )


@episode_app.command("list")
def episode_list(project_id: str) -> None:
    """A project's episodes and their state."""
    eps = list_episodes(project_id)
    if not eps:
        typer.echo("эпизодов нет")
        return
    for ep in eps:
        edir = project_dir(project_id) / "episodes" / ep
        m = load_manifest(edir)
        sb = load_storyboard(edir)
        done = [s for s, r in m.stages.items() if r.status == "done"]
        final = sb.final.video or "—"
        typer.echo(
            f"{ep:<20} сцен: {len(sb.scenes):<3} готово стадий: {len(done)}  "
            f"${m.spent_usd:.2f}  итог: {final}"
        )


@episode_app.command("thumbnail")
def episode_thumbnail(
    ref: str = typer.Argument(help="<project>/<episode>"),
    at: float | None = typer.Option(
        None, "--at", help="use the frame at this second instead of the rule's pick"
    ),
    candidates: bool = typer.Option(
        False, "--candidates", help="write candidates to 05_assemble/_thumb/ and show paths"
    ),
) -> None:
    """Video cover: the frame viewers see before they tap.

    By default uses the same rule as the publish stage: a face in frame plus a
    title word on screen. The rule does not know WHOSE face it is
    (`features/thumbnail_hero`), so fix misses by eye: `--candidates` lists
    options, `--at` sets the exact second."""
    from videoforge.core.storyboard import load_storyboard
    from videoforge.core.thumbnail import THUMB_FILE, choose, keywords, moments_with

    edir = _episode(ref)
    sb = load_storyboard(edir)
    video = edir / (sb.final.video or "05_assemble/final.mp4")
    if not video.exists():
        _fail(f"нет готового ролика: {video}")
        return
    title = (sb.publish.title if sb.publish else "") or sb.title
    ass = video.parent / "captions.ass"
    ass_text = ass.read_text(encoding="utf-8") if ass.exists() else ""

    if candidates:
        words = keywords(title)
        typer.echo(f"слова заголовка: {', '.join(words) or '—'}")
        for sec, word in moments_with(ass_text, words):
            typer.echo(f"  {sec:6.1f} с · на экране «{word}»")
        typer.echo(f"кадр по секунде: videoforge episode thumbnail {ref} --at <секунда>")
        return

    if at is not None:
        from videoforge.core.ffmpeg import frame_at

        dest = video.parent / THUMB_FILE
        frame_at(video, at, dest)
        typer.echo(f"{dest} — кадр {at:.1f} с (выбран руками)")
        _remember_cover(edir, dest, f"{at:.1f} с, выбран руками")
        return

    pick = choose(video, title=title, ass_text=ass_text)
    if pick is None:
        _fail("кадр под обложку не выбрался — задай секунду: --at")
        return
    typer.echo(f"{pick.path} — {pick.why}")
    _remember_cover(edir, pick.path, pick.why)


def _remember_cover(edir: Path, image: Path, why: str) -> None:
    """Record in the packaging WHICH cover now sits next to the video.

    Otherwise the record keeps the previous choice: the file was replaced by
    hand, but the packaging still describes the automatic frame and gives a
    wrong answer to "which cover is set"."""
    from videoforge.stages.publish import DATA_FILE, OUT_DIR
    from videoforge.stages.publish_upload import load_packaging

    path = edir / OUT_DIR / DATA_FILE
    if not path.exists():  # no packaging yet: the publish stage will write it
        return
    try:
        plan = load_packaging(edir)
    except Exception as exc:  # noqa: BLE001 - broken packaging must not fail the cover
        typer.echo(f"  ! упаковка не обновлена ({exc})")
        return
    plan.thumbnail = image.resolve().relative_to(edir.resolve()).as_posix()
    plan.thumbnail_why = why
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(f"  упаковка обновлена: {path.name}")


@episode_app.command("materials")
def episode_materials(
    ref: str = typer.Argument(help="<project>/<episode>"),
    scan: bool = typer.Option(False, "--scan", help="refresh file snapshots and show what changed"),
) -> None:
    """Video passport: what it is made of and where it came from (05_assemble/materials.json).

    Needed for videos the pipeline did not make (scripted builds, copies of
    ready-made files). Pipeline videos derive this from provenance, resources
    and source.json, so they get no passport.

    Without `--scan`, shows the passport (creating an empty one if missing).
    With `--scan`, re-stamps each file's size and mtime and marks files that
    changed or disappeared."""
    from videoforge.core.materials import (
        ROLE_TITLES,
        Material,
        Materials,
        load_materials,
        materials_path,
        resolve,
        save_materials,
        stamp,
        state,
    )
    from videoforge.paths import ROOT

    edir = _episode(ref)
    passport = load_materials(edir)
    if passport is None:
        passport = Materials(
            made_by="",
            rebuild="",
            note="Опись материалов ролика. Заполняется руками для роликов, собранных "
            "мимо конвейера: путь ИЛИ ссылка, откуда взято, права. Ссылка важнее пути — "
            "медиа под .gitignore и переживает уборку хуже.",
            items=[Material(role="other", title="", path="", link="", note="")],
        )
        save_materials(edir, passport)
        typer.echo(f"создан {materials_path(edir)} — заполни руками")
        return

    if scan:
        passport.items = [stamp(item, resolve(edir, ROOT, item.path)) for item in passport.items]
        save_materials(edir, passport)

    typer.echo(
        f"{ref}: материалов {len(passport.items)}"
        + (f" · {passport.made_by}" if passport.made_by else "")
    )
    for item in passport.items:
        path = resolve(edir, ROOT, item.path)
        mark = {"ok": " ", "changed": "~", "gone": "!"}.get(state(item, path), " ")
        where = item.link or item.path or "—"
        typer.echo(
            f" {mark} {ROLE_TITLES.get(item.role, item.role):<14} {item.title[:40]:<40} {where}"
        )
    if passport.rebuild:
        typer.echo(f"пересборка: {passport.rebuild}")


@episode_app.command("cleanup")
def episode_cleanup(
    ref: str = typer.Argument(help="<project>/<episode>"),
    classes: list[str] | None = typer.Option(
        None,
        "--class",
        help="what to delete: junk | regenerable | downloaded (repeatable); default junk",
    ),
    yes: bool = typer.Option(False, "--yes", help="delete; without it, a dry run"),
) -> None:
    """Remove an episode's intermediate files: scene clips after assembly, caches,
    check frames (junk), unselected generation attempts (regenerable), and
    downloaded clip sources (downloaded, only with a confirmed link).

    Without `--yes` nothing is deleted: shows a table of classes with file
    counts and megabytes, plus the paths. Deletion runs under the episode lock
    and is recorded in manifest.json (`cleanup`). Class definitions live in
    `core/cleanup.py`."""
    from videoforge.core.cleanup import (
        CLASS_TITLES,
        CLASSES,
        DEFAULT_CLASSES,
        MB,
        CleanupRefused,
        cleanup,
    )
    from videoforge.core.runner import Busy

    edir = _episode(ref)
    chosen = tuple(classes) if classes else DEFAULT_CLASSES
    try:
        result = cleanup(edir, chosen, apply=yes)
    except (CleanupRefused, ValueError, Busy) as exc:
        _fail(str(exc))
        return

    groups = result.by_class()
    total = sum(e.size for e in result.entries)
    typer.echo(f"{ref}: файлов {len(result.entries)}, {total / MB:.1f} МБ")
    typer.echo(f"  {'класс':<12} {'файлов':>6} {'МБ':>8}")
    for cls in CLASSES:
        items = groups[cls]
        size = sum(e.size for e in items) / MB
        mark = " ← удалить" if cls in chosen and cls != "keep" else ""
        typer.echo(f"  {cls:<12} {len(items):>6} {size:>8.1f}  {CLASS_TITLES[cls]}{mark}")

    if result.applied:
        typer.echo(
            f"удалено файлов: {len(result.deleted)}, освобождено {result.freed_bytes / MB:.1f} МБ"
            " · след в manifest.json (cleanup)"
        )
    else:
        for cls in CLASSES:
            items = groups[cls]
            if cls == "keep" or not items:
                continue
            limit = None if cls == "junk" else 20  # all junk, first 20 of the rest
            shown = items if limit is None else items[:limit]
            typer.echo(f"{cls}:")
            for e in shown:
                flag = "" if e.restorable else "  [нет ссылки — не удалится]"
                typer.echo(f"  {e.path}  {e.mb:.1f} МБ{flag}")
            if limit is not None and len(items) > limit:
                typer.echo(f"  … ещё {len(items) - limit}")
        if result.chosen:
            typer.echo(
                f"удалить: {len(result.chosen)} файлов, {result.would_free_bytes / MB:.1f} МБ — "
                f"videoforge episode cleanup {ref}"
                + "".join(f" --class {c}" for c in chosen)
                + " --yes"
            )
        else:
            typer.echo("удалять нечего")
    for e in result.blocked:
        typer.echo(f"  не удалится {e.path}: {e.why}")


@episode_app.command("translate-prompts")
def episode_translate_prompts(
    ref: str = typer.Argument(help="<project>/<episode>"),
    lang: str = typer.Option("ru", help="target language"),
    force: bool = typer.Option(False, help="re-translate all scenes"),
    llm: str | None = typer.Option(None, help="LLM provider (default VF_LLM_PROVIDER)"),
) -> None:
    """Translate scene prompts (frame and motion) for reading in the UI.

    Prompts stay in English since models read them. The translation is saved
    next to them in prompts.<lang>.json and shown in `videoforge ui`; only
    scenes with no translation or a changed prompt are translated."""
    from videoforge.providers.registry import build_llm
    from videoforge.stages.prompt_translate import translate_prompts

    edir = _episode(ref)
    name = llm or ProviderNames.from_env().llm
    try:
        data, done, cost = translate_prompts(
            edir, llm=build_llm(name), lang=lang, force=force, log=typer.echo
        )
    except (ProviderError, ValueError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"переведено сцен: {done} · всего в файле: {len(data.scenes)} · ${cost:.4f}")


# ---------------------------------------------------------------- source (clips)


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@source_app.command("add")
def source_add(
    project_id: str = typer.Argument(help="project the clips belong to"),
    url: str = typer.Argument(
        help="path to your own long-form video file, or a link to your own YouTube upload"
    ),
    source_id: str | None = typer.Option(None, "--id", help="source id (default: the video id)"),
    language: str | None = typer.Option(None, help="speech language; default: video metadata"),
    transcriber: str = typer.Option(
        "auto",
        help=(
            "auto: youtube for a YouTube link, whisper for a file or other sites "
            "(a source with a transcript keeps its transcriber) | youtube | whisper | mock"
        ),
    ),
    proxy: bool = typer.Option(False, help="download a 480p preview (proxy.mp4) for picking"),
    force: bool = typer.Option(False, help="re-fetch metadata and transcript"),
    stream: bool = typer.Option(
        False, help="the source is a recording of your own live stream with a facecam"
    ),
) -> None:
    """Register your own long-form video as a clip source.

    Stores metadata, chapters, a word-level transcript and sentences under
    ``projects/<project>/sources/<id>/``. A local file is used in place; for a
    link to your own upload only the selected segments are fetched later in full
    resolution. Sources without captions are transcribed locally by Whisper."""
    from videoforge.clips.prepare import prepare_source
    from videoforge.clips.source import (
        PROXY_FILE,
        WORDS_FILE,
        SourceInfo,
        load_source,
        save_source,
    )
    from videoforge.core.ffmpeg import FfmpegError, probe_duration, probe_size
    from videoforge.paths import source_dir
    from videoforge.providers.registry import build_transcriber, transcriber_for
    from videoforge.providers.whisper_local import WhisperLocal
    from videoforge.providers.youtube import (
        download_proxy,
        fetch_info,
        is_youtube_url,
        source_info_from,
    )

    if not project_config_path(project_id).exists():
        _fail(f"проекта {project_id} нет: {project_config_path(project_id)}")
    is_url = url.startswith(("http://", "https://"))
    if is_url and not is_youtube_url(url):
        _fail("a source is a local video file or a link to your own YouTube upload")
    sdir = source_dir(project_id, source_id) if source_id else None
    existing = load_source(sdir) if sdir else None
    try:
        if existing is not None and not force:
            info = existing
        elif is_url:
            typer.echo("читаю метаданные…")
            info = source_info_from(fetch_info(url), language=language)
        else:
            path = Path(url)
            if not path.exists():
                _fail(f"файла нет: {path}")
            size = probe_size(path)
            info = SourceInfo(
                id=source_id or path.stem,
                file=str(path.resolve()),
                title=path.stem,
                duration_sec=probe_duration(path),
                width=size[0] if size else None,
                height=size[1] if size else None,
                language=language or "ru",
            )
    except (ProviderError, FfmpegError) as exc:
        _fail(str(exc))
        return
    if source_id:
        info.id = source_id
    if language:
        info.language = language
    if stream:
        info.stream = True
    sdir = source_dir(project_id, info.id)
    previous = load_source(sdir)
    if transcriber == "auto":
        # a transcribed source keeps its transcriber; a new one is chosen by target
        kept = previous.transcriber if previous is not None and not force else ""
        transcriber = kept or transcriber_for(url)
    try:
        t = build_transcriber(transcriber)
    except ValueError as exc:
        _fail(str(exc))
        return
    typer.echo(f"источник {info.id}: «{info.title}» · {_fmt_duration(info.duration_sec)} · "
               f"глав: {len(info.chapters)} · транскрибер: {t.name}")  # fmt: skip
    fresh = previous is not None and previous.transcriber == t.name and (sdir / WORDS_FILE).exists()
    if isinstance(t, WhisperLocal) and (force or not fresh):
        est = t.estimate_sec(info.duration_sec)
        if est:
            typer.echo(
                f"whisper {t.model_name} на {t.device}: примерно {est / 60:.0f} мин счёта на "
                f"{_fmt_duration(info.duration_sec)} записи; VF_WHISPER_DEVICE=cuda быстрее"
            )
    try:
        report = prepare_source(sdir, info, t, force=force)
    except ProviderError as exc:
        _fail(str(exc))
        return
    if proxy and info.url:
        typer.echo("качаю 480p превью…")
        try:
            path = download_proxy(info.url, sdir / PROXY_FILE)
        except ProviderError as exc:
            _fail(str(exc))
            return
        report.info.proxy = path.name
        save_source(sdir, report.info)
    done = ", ".join(report.steps) or "всё уже было"
    typer.echo(
        f"{sdir}\n  слов: {report.info.word_count} · предложений: "
        f"{len(report.transcript.sentences)} · язык транскрипта: "
        f"{report.info.transcript_language or '—'} · сделано: {done} · ${report.cost_usd:.3f}"
    )
    typer.echo(f"дальше: videoforge source show {project_id} {info.id}")


@source_app.command("list")
def source_list(project_id: str = typer.Argument(help="project")) -> None:
    """A project's sources: id, title, duration, words, chapters."""
    from videoforge.clips.source import list_sources
    from videoforge.paths import sources_dir

    found = list_sources(sources_dir(project_id))
    if not found:
        typer.echo("источников нет: videoforge source add <проект> <url>")
        return
    for s in found:
        typer.echo(
            f"  {s.id:<14} {_fmt_duration(s.duration_sec):>8}  слов {s.word_count:>6}  "
            f"глав {len(s.chapters):>2}  {s.transcriber or '—':<8} {s.title}"
        )


@source_app.command("show")
def source_show(
    project_id: str = typer.Argument(help="project"),
    source_id: str = typer.Argument(help="source id"),
    sentences: int = typer.Option(12, help="how many leading sentences to show"),
) -> None:
    """Source metadata, chapters and the start of the transcript."""
    from videoforge.clips.source import (
        chapter_at,
        load_source,
        load_transcript,
        top_peaks,
    )
    from videoforge.paths import source_dir

    sdir = source_dir(project_id, source_id)
    info = load_source(sdir)
    if info is None:
        _fail(f"источника {source_id} нет в {sdir.parent}")
        return
    signal = "most replayed (YouTube)"
    typer.echo(f"{info.title}\n  {info.url or info.file}\n  канал: {info.channel or '—'} · "
               f"{_fmt_duration(info.duration_sec)} · {info.width}x{info.height} · "
               f"язык речи: {info.language} · просмотров: {info.views or '—'}")  # fmt: skip
    typer.echo(f"  транскрипт: {info.transcriber or 'нет'} ({info.transcript_language or '—'}), "
               f"слов {info.word_count} · превью: {info.proxy or 'нет'}")  # fmt: skip
    if info.chapters:
        typer.echo(f"главы ({len(info.chapters)}):")
        for ch in info.chapters:
            typer.echo(f"  {_fmt_duration(ch.start_sec):>8}  {ch.title}")
    peaks = top_peaks(info.heatmap)
    if peaks:
        typer.echo(f"{signal}, {len(info.heatmap)} окон, топ {len(peaks)}:")
        for h in sorted(peaks, key=lambda h: h.start_sec):
            where = chapter_at(info.chapters, h.start_sec)
            typer.echo(f"  {_fmt_duration(h.start_sec):>8}  {h.value:.2f}  {where}")
    transcript = load_transcript(sdir)
    if transcript is not None:
        typer.echo(f"предложения ({len(transcript.sentences)}), первые {sentences}:")
        for s in transcript.sentences[:sentences]:
            typer.echo(f"  {_fmt_duration(s.start_sec):>8}  {s.text}")


@source_app.command("highlights")
def source_highlights(
    project_id: str = typer.Argument(help="project"),
    source_id: str = typer.Argument(help="source id"),
    llm: str | None = typer.Option(
        None, help="LLM: claude_cli (subscription, default) | gemini | mock"
    ),
    count: int = typer.Option(DEFAULT_COUNT, help="how many candidates to ask the model for"),
    force: bool = typer.Option(False, help="re-select even if highlights.json is fresh"),
    show: bool = typer.Option(False, "--open", help="open the report in a browser"),
) -> None:
    """Pick moments for shorts: transcript → highlights.json + highlights.html.

    The model ranks and explains; Python computes length, sentence boundaries
    and replays. A human then reviews the report and runs `source pick`."""
    from videoforge.clips.highlights import select_highlights
    from videoforge.clips.report import render_highlights_report
    from videoforge.clips.source import load_source, load_transcript
    from videoforge.config import intel_llm_provider
    from videoforge.paths import source_dir
    from videoforge.providers.registry import build_llm, missing_keys

    sdir = source_dir(project_id, source_id)
    info = load_source(sdir)
    if info is None:
        _fail(f"источника {source_id} нет в {sdir.parent}")
        return
    transcript = load_transcript(sdir)
    if transcript is None or not transcript.sentences:
        _fail(f"нет транскрипта: videoforge source add {project_id} {info.url or info.file}")
        return
    llm_name = llm or intel_llm_provider()
    try:
        provider = build_llm(llm_name)
    except ValueError as exc:
        _fail(f"LLM: {exc}")
        return
    if missing := missing_keys([llm_name]):
        _fail(f"нет ключей: {', '.join(missing)} (задай в .env) — или --llm claude_cli/mock")
    cfg = load_project(project_id)
    blueprint = project_blueprint(cfg)
    typer.echo(
        f"выбираю моменты: {len(transcript.sentences)} предложений, "
        f"глав {len(info.chapters)} · LLM: {provider.name}"
    )
    if blueprint.note:
        typer.echo(blueprint.note)
    try:
        report = select_highlights(
            sdir,
            info=info,
            transcript=transcript,
            llm=provider,
            rules=load_rules_from_dir(project_dir(project_id), cfg),
            blueprint=blueprint.text,
            platforms=cfg.channel.platforms,
            target_duration_sec=cfg.video.target_duration_sec,
            # the critic writes the title and description, i.e. viewer-facing text;
            # a clips channel may have Russian speech but English on-screen text
            language=cfg.video.captions_language or cfg.channel.language,
            count=count,
            force=force,
            headline=cfg.video.headline,
            tighten=cfg.video.tighten,
        )
    except (ProviderError, ValueError) as exc:
        _fail(str(exc))
        return
    data = report.highlights
    if report.skipped:
        typer.echo(f"{report.path}: кандидаты уже выбраны моделью {data.llm} — --force переделает")
    else:
        typer.echo(
            f"{report.path}: кандидатов {len(data.candidates)} · вызовов модели {data.calls} · "
            f"{report.seconds:.0f} с · ${report.cost_usd:.3f}"
        )
    for line in data.fixes:
        typer.echo(f"  подвинуто: {line}")
    for line in data.warnings:
        typer.echo(f"  выброшено: {line}", err=True)
    for c in data.candidates:
        typer.echo(
            f"  {c.id}  {_fmt_duration(c.start_sec):>8}  {c.duration_sec:>3.0f}с  "
            f"heat {c.heat:.2f}  {c.title}"
        )
    path = render_highlights_report(sdir)
    typer.echo(f"отчёт: {path}")
    if data.candidates:
        typer.echo(
            f"дальше: videoforge source pick {project_id} {source_id} "
            f"{' '.join(c.id for c in data.candidates[:2])}"
        )
    if show:
        typer.launch(str(path))


@source_app.command("pick")
def source_pick(
    project_id: str = typer.Argument(help="project"),
    source_id: str = typer.Argument(help="source id"),
    ids: list[str] | None = typer.Argument(
        None, help="candidate ids from highlights.json: h01 h03 …"
    ),
    at: str | None = typer.Option(
        None,
        "--at",
        help="a manual span instead of ids: 20:41-21:33 or 1:20:41-1:21:33 (bounds "
        "snap to sentences); the episode gets id <source>_at2041",
    ),
    title: str | None = typer.Option(None, help="moment title, required with --at"),
) -> None:
    """Picked moments → episodes: one source-span scene per episode.

    By id: a candidate from the latest analysis. By `--at`: any span of the
    source. Highlight selection is non-deterministic, so this recovers a good
    moment from an earlier run (or trims an overlong one) without re-running it.
    Such a moment has no critic: a human gives the title, and `publish` asks
    the model for the description and hashtags."""
    from videoforge.clips.highlights import episode_from_candidate, load_highlights
    from videoforge.paths import source_dir

    sdir = source_dir(project_id, source_id)
    if at is not None:
        if ids:
            _fail("либо id кандидатов, либо --at: момент берётся или из разбора, или по времени")
        _pick_at(project_id, source_id, sdir, at, title or "")
        return
    if title is not None:
        _fail("--title только вместе с --at: у кандидата из разбора заголовок пишет критик")
    if not ids:
        _fail(
            f"нужны id кандидатов (h01 h03 …) или отрезок: videoforge source pick "
            f"{project_id} {source_id} --at 20:41-21:33 --title «…»"
        )
        return
    data = load_highlights(sdir)
    if data is None:
        _fail(f"нет highlights.json: videoforge source highlights {project_id} {source_id}")
        return
    by_id = {c.id: c for c in data.candidates}
    if unknown := [i for i in ids if i not in by_id]:
        _fail(f"нет таких кандидатов: {', '.join(unknown)} (есть: {', '.join(by_id) or '—'})")
    for cid in ids:
        cand = by_id[cid]
        try:
            edir = episode_from_candidate(project_id, source_id, cand)
        except FileExistsError as exc:
            typer.echo(f"  {cid}: {exc}", err=True)
            continue
        typer.echo(
            f"  {cid} → {project_id}/{edir.name}  {_fmt_duration(cand.start_sec)}"
            f"–{_fmt_duration(cand.end_sec)} · {cand.duration_sec:.0f} с · {cand.title}"
        )
    typer.echo(f"дальше: videoforge run {project_id}/<эпизод>")


def _pick_at(project_id: str, source_id: str, sdir: Path, at: str, title: str) -> None:
    """`source pick --at`: a source span → an episode. Needs no highlights.json,
    so it works before analysis or after a re-run lost the moment. An id that is
    already taken is refused rather than silently overwritten."""
    from videoforge.clips.highlights import candidate_from_span, episode_from_candidate, parse_span
    from videoforge.clips.source import load_source, load_transcript

    if not title.strip():
        _fail("с --at нужен --title: критика у такого момента нет, заголовок даёт человек")
        return
    try:
        start, end = parse_span(at)
    except ValueError as exc:
        _fail(str(exc))
        return
    info = load_source(sdir)
    if info is None:
        _fail(f"источника {source_id} нет в {sdir.parent}")
        return
    transcript = load_transcript(sdir)
    if transcript is None or not transcript.sentences:
        _fail(f"нет транскрипта: videoforge source add {project_id} {info.url or info.file}")
        return
    try:
        cand, notes = candidate_from_span(
            start,
            end,
            title=title,
            sentences=transcript.sentences,
            chapters=info.chapters,
            heatmap=info.heatmap,
        )
    except ValueError as exc:
        _fail(str(exc))
        return
    try:
        edir = episode_from_candidate(project_id, source_id, cand)
    except FileExistsError as exc:
        _fail(f"{exc} — момент с {_fmt_duration(start)} уже взят; другое начало или убери папку")
        return
    typer.echo(
        f"  {cand.id} → {project_id}/{edir.name}  {_fmt_duration(cand.start_sec)}"
        f"–{_fmt_duration(cand.end_sec)} · {cand.duration_sec:.0f} с · {cand.title}"
    )
    for note in notes:
        typer.echo(f"  {note}", err=note.startswith("ПРОВЕРЬ"))
    typer.echo(f"дальше: videoforge run {project_id}/{edir.name}")


# ---------------------------------------------------------------- run / status / approve


@app.command()
def preview(
    ref: str = typer.Argument(help="<project>/<episode>"),
    platform: str | None = typer.Option(None, help="one platform instead of all in project.yaml"),
    at: float | None = typer.Option(None, help="frame second; default: the longest caption line"),
    video: bool = typer.Option(False, help="the whole video with zones, not a single frame"),
    show: bool = typer.Option(False, "--open", help="open the result"),
) -> None:
    """How the video will look in the app: a frame with the platform's UI zones.

    Red marks areas covered by the UI (buttons on the right, name and audio at
    the bottom); green outlines the clear area. A mockup, not an emulator: it
    draws boundaries, not buttons."""
    from videoforge.core.ffmpeg import aspect_size, probe_size
    from videoforge.core.preview import busiest_caption_sec, preview_frame, preview_video
    from videoforge.core.runner import project_context
    from videoforge.stages.assemble import ASS_FILE, OUT_DIR

    edir = _episode(ref)
    sb = load_storyboard(edir)
    if not sb.final.video or not (edir / sb.final.video).exists():
        _fail(f"нет готового видео у {ref} — сначала videoforge run")
    final = edir / (sb.final.video or "")
    project, _ = project_context(edir)
    names = [platform] if platform else (project.channel.platforms if project else [])
    if not names:
        _fail("у проекта не указаны площадки — задай --platform")
    unknown = [n for n in names if n not in PLATFORMS]
    if unknown:
        _fail(f"неизвестные площадки {unknown}; допустимы: {sorted(PLATFORMS)}")

    size = probe_size(final) or aspect_size(sb.aspect_ratio)
    moment = at if at is not None else busiest_caption_sec(edir / OUT_DIR / ASS_FILE)
    if moment is None:
        moment = (sb.final.duration_sec or 0.0) / 2
    out_dir = edir / OUT_DIR
    made: list[Path] = []
    for name in names:
        spec = PLATFORMS[name]
        zone = spec.get("ui_zone") or {}
        label = str(spec["title"])
        if video:
            path = preview_video(final, out_dir / f"preview_{name}.mp4", zone=zone, size=size,
                                 label=label)  # fmt: skip
        else:
            path = preview_frame(final, out_dir / f"preview_{name}.png", at_sec=moment,
                                 zone=zone, size=size, label=label)  # fmt: skip
        made.append(path)
        covered = f"снизу {zone.get('bottom', 0):.0%}, справа {zone.get('right', 0):.0%}"
        typer.echo(f"  {label}: {path.name} ({covered})")
    if not video:
        typer.echo(
            f"кадр на {moment:.1f} с · красное закрыто интерфейсом, зелёное — чистая область"
        )
    if show and made:
        typer.launch(str(made[0]))


@app.command()
def run(
    ref: str = typer.Argument(help="<project>/<episode>"),
    mock: bool = typer.Option(False, help="all providers are free stubs"),
    yes: bool = typer.Option(False, "--yes", "-y", help="do not stop at gates"),
    ignore_budget: bool = typer.Option(False, help="do not check the forecast against budget"),
    only: str | None = typer.Option(None, help="run a single stage"),
    start: str | None = typer.Option(None, "--from", help="start from this stage"),
    stop: str | None = typer.Option(None, "--to", help="stop after this stage (inclusive)"),
    max_attempts: int = typer.Option(3, help="regenerations per scene when a check fails"),
    force: bool = typer.Option(False, help="redo even what passed checks"),
) -> None:
    """Run the workflow stages on an episode."""
    from videoforge.core.runner import Busy, required_providers, run_episode
    from videoforge.providers.registry import build_providers, missing_keys

    edir = _episode(ref)
    names = ProviderNames.mock() if mock else ProviderNames.from_env()
    try:
        providers = build_providers(names)
    except (ProviderError, ValueError) as exc:
        _fail(f"провайдеры: {exc}")
        return

    options = RunOptions(
        auto_approve=yes,
        ignore_budget=ignore_budget,
        only=only,
        start=start,
        stop=stop,
        max_attempts=max_attempts,
        force=force,
    )
    try:
        # only require keys for providers the selected stages will call
        needed = required_providers(edir, options)
    except ValueError as exc:
        _fail(str(exc))
        return
    used = names.as_dict()
    missing = missing_keys([used[kind] for kind in needed if kind in used])
    if missing:
        _fail(f"нет ключей: {', '.join(missing)} (задай в .env) — или запусти с --mock")
    typer.echo(f"провайдеры: {providers.names()}")

    try:
        report = run_episode(edir, providers, options)
    except (ValueError, Busy) as exc:
        # Busy: the episode is already being worked on (a rebuild from the UI,
        # another terminal); report it as a message, not a traceback
        _fail(str(exc))
        return

    manifest = load_manifest(edir)
    typer.echo(f"потрачено: ${manifest.spent_usd:.3f} из ${manifest.budget_usd:.2f}")
    if report.awaiting_approval:
        typer.echo(
            f"\nстоп: результат стадии '{report.awaiting_approval}' ждёт просмотра. "
            f"Посмотри файлы в {edir} и продолжи:\n"
            f"  videoforge approve {ref} {report.awaiting_approval}\n"
            f"  videoforge run {ref}"
        )
        raise typer.Exit(2)
    if report.reason:
        typer.echo(f"\nстоп на '{report.stopped_at}': {report.reason}", err=True)
        raise typer.Exit(1)
    sb = load_storyboard(edir)
    if sb.final.video:
        typer.echo(f"\nготово: {edir / sb.final.video}")


@app.command()
def estimate(
    ref: str = typer.Argument(help="<project>/<episode>"),
    mock: bool = typer.Option(False, help="stub prices (zeros) instead of real ones"),
) -> None:
    """Cost forecast from the storyboard: no run, no keys, no spending.

    Computes what the runner checks against the budget: only work not yet done."""
    from videoforge.core.runner import estimate_episode
    from videoforge.providers.registry import build_providers

    edir = _episode(ref)
    names = ProviderNames.mock() if mock else ProviderNames.from_env()
    try:
        providers = build_providers(names)
        rows = estimate_episode(edir, providers)
    except (ProviderError, ValueError) as exc:
        _fail(str(exc))
        return
    m = load_manifest(edir)
    sb = load_storyboard(edir)
    typer.echo(f"{ref} · workflow {m.workflow} · сцен: {len(sb.scenes)}")
    typer.echo(f"провайдеры: {providers.names()}")
    total = 0.0
    for r in rows:
        gate = " [ворота]" if r.gate_after else ""
        if r.cost_usd is None:
            typer.echo(f"  {r.stage:<10}       — не реализована{gate}")
            continue
        total += r.cost_usd
        typer.echo(f"  {r.stage:<10} ${r.cost_usd:7.3f}{gate}")
    typer.echo(
        f"осталось по прогнозу: ${total:.3f} · потрачено: ${m.spent_usd:.3f} · "
        f"бюджет: ${m.budget_usd:.2f}"
    )
    if m.spent_usd + total > m.budget_usd:
        typer.echo(
            "НЕ влезает в бюджет: сократи сцены или подними budget_usd в manifest.json",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo("влезает в бюджет")


@app.command()
def report(
    ref: str = typer.Argument(help="<project>/<episode>"),
    show: bool = typer.Option(False, "--open", help="open in a browser"),
) -> None:
    """Build the episode HTML report: scenes with prompts, statuses, costs, forecast.

    report.html is written to the episode folder; asset links are relative."""
    from videoforge.core.report import render_report
    from videoforge.core.runner import estimate_episode
    from videoforge.providers.registry import build_providers

    edir = _episode(ref)
    estimates: dict[str, float | None] | None = None
    try:
        rows = estimate_episode(edir, build_providers(ProviderNames.from_env()))
        estimates = {r.stage: r.cost_usd for r in rows}
    except (ProviderError, ValueError):
        pass  # the report is useful without a forecast
    path = render_report(edir, load_storyboard(edir), load_manifest(edir), estimates=estimates)
    typer.echo(f"отчёт: {path}")
    if show:
        typer.launch(str(path))


@app.command()
def status(ref: str = typer.Argument(help="<project>/<episode>")) -> None:
    """Stage statuses, scene checks, costs."""
    edir = _episode(ref)
    m = load_manifest(edir)
    sb = load_storyboard(edir)
    typer.echo(
        f"{ref} · workflow {m.workflow} · {sb.aspect_ratio} · "
        f"бюджет ${m.budget_usd:.2f} · потрачено ${m.spent_usd:.3f}"
    )
    if m.awaiting_approval:
        typer.echo(
            f"ждёт одобрения: {m.awaiting_approval} → "
            f"videoforge approve {ref} {m.awaiting_approval}"
        )
    typer.echo("стадии:")
    for ws in load_workflow(m.workflow).stages:
        rec = m.stages.get(ws.name, StageRecord())
        gate = ""
        if ws.gate_after:
            gate = " [ворота: одобрено]" if ws.name in m.approvals else " [ворота]"
        cost = m.stage_cost(ws.name)
        cost_s = f" ${cost:.3f}" if cost else ""
        typer.echo(f"  {ws.name:<10} {rec.status:<9}{cost_s}{gate} {rec.error or rec.message}")
    typer.echo(f"сцены ({len(sb.scenes)}):")
    for s in sb.ordered_scenes():
        spent = sum(s.cost_usd.values())
        typer.echo(
            f"  {s.id:<8} img:{s.checks.image:<8} vid:{s.checks.video:<8} "
            f"aud:{s.checks.audio:<8} ${spent:.3f}"
        )
        for key, note in s.checks.notes.items():
            typer.echo(f"           {key}: {note}")
    if sb.final.video:
        typer.echo(f"итог: {sb.final.video} ({sb.final.duration_sec or 0:.1f}s)")


StageTiming = tuple[str, float, list[tuple[str, float, str]]]  # stage, seconds, steps


def _timings_rows(edir: Path) -> tuple[list[StageTiming], float]:
    """An episode's stages with their steps, and the latest run's total time."""
    m = load_manifest(edir)
    rows: list[StageTiming] = []
    total = 0.0
    for ws in load_workflow(m.workflow).stages:
        rec = m.stages.get(ws.name)
        if rec is None or rec.elapsed_sec is None:
            continue
        total += rec.elapsed_sec
        rows.append((ws.name, rec.elapsed_sec, [(s.name, s.sec, s.note) for s in rec.steps]))
    return rows, total


def _fmt_sec(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:5.1f}с"
    return f"{int(seconds // 60)}м{int(seconds % 60):02d}с"


@app.command()
def timings(
    ref: str = typer.Argument(help="<project>/<episode>, or <project> for all episodes"),
    steps: bool = typer.Option(True, help="show steps inside stages"),
) -> None:
    """How long the latest run's stages took, and where the time went inside them.

    The run itself records timings (`manifest.json`), so no re-run is needed.
    A skipped stage does not reset earlier numbers: `skipped` keeps the timings
    of the run where the work was actually done."""
    if "/" in ref:
        refs = [ref]
    else:
        base = episodes_dir(ref)
        if not base.is_dir():
            _fail(f"нет проекта {ref} (и это не <проект>/<эпизод>)")
        refs = [f"{ref}/{p.name}" for p in sorted(base.iterdir()) if (p / "manifest.json").exists()]
        if not refs:
            _fail(f"в проекте {ref} нет эпизодов с манифестом")

    grand: dict[str, list[float]] = {}
    for one in refs:
        edir = _episode(one)
        rows, total = _timings_rows(edir)
        sb = load_storyboard(edir)
        length = sb.final.duration_sec or 0.0
        head = f"{one} · {_fmt_sec(total)}"
        if length:
            head += f" на {length:.0f} с ролика ({total / length:.1f}× хронометража)"
        typer.echo(head)
        for name, elapsed, rec_steps in rows:
            share = f"{elapsed / total:4.0%}" if total else "   —"
            typer.echo(f"  {name:<10} {_fmt_sec(elapsed)} {share}")
            grand.setdefault(name, []).append(elapsed)
            if not steps:
                continue
            measured = sum(s for _, s, _ in rec_steps)
            for sname, ssec, note in rec_steps:
                tail = f" · {note}" if note else ""
                typer.echo(f"      {sname:<24} {_fmt_sec(ssec)}{tail}")
            if rec_steps and elapsed - measured > 0.5:
                typer.echo(f"      {'прочее (не замерено)':<24} {_fmt_sec(elapsed - measured)}")
        if len(refs) > 1:
            typer.echo("")

    if len(refs) > 1:
        typer.echo("среднее по эпизодам:")
        overall = sum(sum(v) / len(v) for v in grand.values())
        for name, values in grand.items():
            avg = sum(values) / len(values)
            share = f"{avg / overall:4.0%}" if overall else "   —"
            spread = ""
            if len(values) > 1:
                spread = f" (от {_fmt_sec(min(values))} до {_fmt_sec(max(values))})"
            typer.echo(f"  {name:<10} {_fmt_sec(avg)} {share}{spread}")
        typer.echo(f"  {'ИТОГО':<10} {_fmt_sec(overall)} на эпизод")


def _yield_lines(result: Yield) -> list[str]:
    """Yield summary as a table: one row per kind × provider × format × project,
    rejection reasons under each row, and what was excluded (and why) at the bottom."""
    head = (
        "стадия",
        "провайдер",
        "формат",
        "проект",
        "принято",
        "попыток",
        "на принятый",
        "с 1-й",
        "отвергнуто",
        "$ за отвергнутые",
    )
    cells: list[tuple[str, ...]] = []
    for r in result.rows:
        per = f"{r.mean:.2f} / {r.median:g}" if r.mean is not None and r.median is not None else "—"
        first = f"{r.first_try_share:.0%}" if r.first_try_share is not None else "—"
        # ≈ means the cost is estimated from the average attempt price, not the ledger
        usd = f"{'≈' if r.estimated else ''}${r.rejected_usd:.4f}"
        cells.append(
            (
                r.kind,
                r.provider,
                r.aspect_ratio,
                r.project,
                str(r.accepted),
                str(r.attempts),
                per,
                first,
                str(r.rejected),
                usd,
            )
        )
    widths = [max([len(h)] + [len(c[i]) for c in cells]) for i, h in enumerate(head)]
    text_cols = 4  # first four columns are text; the rest are numbers, right-aligned

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(
            v.ljust(widths[i]) if i < text_cols else v.rjust(widths[i]) for i, v in enumerate(row)
        ).rstrip()

    lines = [
        "попыток на принятый кадр или клип — по раскадровкам и журналу расходов "
        "(среднее / медиана; ≈ — оценка по средней цене попытки)",
        fmt(head),
    ]
    for r, row in zip(result.rows, cells, strict=True):
        lines.append(fmt(row))
        if r.unchecked:
            # accepted "first try" only because nobody checked (checks disabled
            # or the checker declined), so the share says nothing about quality
            lines.append(
                f"    без проверки: {r.unchecked} из {r.accepted} принятых "
                "(checks: skipped) — «с 1-й» здесь не сигнал качества"
            )
        if r.rejected == 0:
            continue
        parts = [f"«{text}» ×{n}" for text, n in r.top_reasons()]
        if r.unexplained:
            parts.append(f"причины не сохранены: {r.unexplained} (прогон без журнала попыток)")
        lines.append("    причины: " + " · ".join(parts))
    if not result.rows:
        lines.append("строк нет: ни одной сцены с попытками у живого провайдера")
    skipped = []
    if result.mock_scenes:
        skipped.append(f"сцен на заглушке (mock) — {result.mock_scenes}")
    if result.cut_scenes:
        skipped.append(f"сцен-отрезков нарезок — {result.cut_scenes} (у cut нет проверки)")
    if result.skipped_episodes:
        skipped.append(f"эпизодов раньше --since — {result.skipped_episodes}")
    if skipped:
        lines.append("не считались: " + "; ".join(skipped))
    return lines


@app.command("yield")
def yield_(
    project: str | None = typer.Argument(None, help="project; without it, all projects"),
    since: str | None = typer.Option(
        None, "--since", help="only episodes whose stages ran on or after: 2026-09-01"
    ),
    kind: str | None = typer.Option(None, "--kind", help="image | video; default both"),
    as_json: bool = typer.Option(False, "--json", help="the same result as a JSON dict"),
) -> None:
    """Attempts per accepted frame or clip, from storyboards and the cost ledger.

    One row per asset kind × provider × format × project: accepted, attempts,
    attempts per accepted (mean / median), first-try share, rejected and their
    cost, rejection reasons (only for runs with an attempt log). Mocks and
    clip-cut scenes are excluded and counted at the bottom. A scene's attempts
    are the max of its counter and the ledger, since the counter can be reset
    by hand but ledger costs remain."""
    if kind is not None and kind not in YIELD_KINDS:
        _fail(f"--kind принимает {' | '.join(YIELD_KINDS)}, а не {kind!r}")
    start: date | None = None
    if since:
        try:
            start = date.fromisoformat(since)
        except ValueError:
            _fail(f"--since ждёт дату вида 2026-09-01, а не {since!r}")
    if project is not None and not project_config_path(project).exists():
        _fail(f"нет проекта {project}")
    episodes = load_episodes(None if project is None else [project])
    result = measure(episodes, kinds=YIELD_KINDS if kind is None else (kind,), since=start)
    if as_json:
        typer.echo(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return
    for line in _yield_lines(result):
        typer.echo(line)


BARS = " ▁▂▃▄▅▆▇█"  # loudness in one character: one bar per second of video


def _bar(values: list[float], start: float, end: float, per_sec: int, scale: float) -> str:
    """A window's loudness as one character. Each track is scaled to itself:
    music sits ~16 dB below the voice and would vanish on a shared scale."""
    a, b = int(start * per_sec), max(int(start * per_sec) + 1, int(end * per_sec))
    window = values[a:b]
    peak = (max(window) if window else 0.0) * scale
    return BARS[max(0, min(len(BARS) - 1, round(peak * (len(BARS) - 1))))]


@app.command()
def montage(
    ref: str = typer.Argument(help="<project>/<episode>"),
    at: float = typer.Option(0.0, "--from", help="start second"),
    to: float = typer.Option(0.0, "--to", help="end second (0 = to the end)"),
) -> None:
    """Edit sheet of a finished video: what is on screen and in the audio each second.

    A text version of the montage tab in `videoforge ui`: scenes, on-screen
    text, overlays, word-level speech, lyrics, effects and sounds on one
    timeline, plus a per-second loudness bar. Useful without a screen: an
    audio/visual mismatch shows up as two adjacent lines."""
    from videoforge.api.montage import episode_montage

    edir = _episode(ref)
    project_id, _, episode_id = str(edir.parent.parent.name), "", edir.name
    try:
        data = episode_montage(project_id, episode_id)
    except LookupError as exc:
        _fail(str(exc))
        return
    end = to or data["duration_sec"]
    typer.echo(
        f"{data['ref']} · {data['duration_sec']:.2f} с · план {data['plan']['author']}"
        # only note this when there are scenes: a scripted video has none (shots
        # come from the file itself), so "not measured" would be a false alarm
        + (
            ""
            if data["measured"] or not any(t["id"] == "scenes" for t in data["tracks"])
            else " · время сцен не измерено (нет timeline.json)"
        )
    )
    for note in data["notes"]:
        typer.echo(f"  ! {note['text']}")

    tracks = {t["id"]: t for t in data["tracks"]}
    waves = [
        (t["title"], t, 1 / (max(t["values"]) or 1)) for t in data["tracks"] if t["kind"] == "wave"
    ]
    events: list[tuple[float, str, str]] = []
    for track in data["tracks"]:
        for item in track.get("items") or []:
            text = item.get("text") or item.get("label") or ""
            if track["kind"] == "clips":
                text = f"{item.get('label')} — {text}"
            events.append((item["start"], track["title"], text))
    for mark in (tracks.get("music") or {}).get("marks") or []:
        events.append((mark["at"], "Музыка", f"{mark['label']} ({mark.get('why', '')})"))

    typer.echo("")
    step = 1.0
    second = at
    shown = [e for e in sorted(events) if at <= e[0] < end]
    i = 0
    while second < end:
        bars = "".join(
            _bar(t["values"], second, second + step, t["per_sec"], scale) for _, t, scale in waves
        )
        line = f"{second:6.1f} {bars}"
        head = True
        while i < len(shown) and shown[i][0] < second + step:
            when, track, text = shown[i]
            prefix = line if head else " " * len(line)
            typer.echo(f"{prefix} {when:6.2f} {track:<10} {text[:88]}")
            head, i = False, i + 1
        if head:
            typer.echo(line)
        second += step
    if waves:
        typer.echo("")
        typer.echo("столбики: " + ", ".join(title for title, _, _ in waves))
    # materials: what the video is built from, so "where is this track from"
    # is answered here instead of needing another command
    if data["materials"]:
        typer.echo("")
        typer.echo(f"материалы ({len(data['materials'])}):")
        for row in data["materials"]:
            tail = " · ".join(
                part
                for part in (row["origin"], row["license"], row["link"] or row["resource"] or "")
                if part
            )
            mark = {"changed": " [изменился]", "gone": " [файла нет]"}.get(row["state"], "")
            typer.echo(f"  {row['title']:<22} {row['value'][:52]}{mark}")
            if tail:
                typer.echo(f"  {'':<22} {tail[:88]}")
    if data["stale"]:
        typer.echo("")
        typer.echo("ФИНАЛ УСТАРЕЛ, входы новее готового файла: " + ", ".join(data["stale"]))


@app.command()
def approve(
    ref: str = typer.Argument(help="<project>/<episode>"),
    stage: str = typer.Argument(help="stage name: images, assemble …"),
) -> None:
    """Approve a stage result (lift the gate)."""
    edir = _episode(ref)
    approve_stage(edir, stage)
    typer.echo(f"одобрено: {stage}. Дальше: videoforge run {ref}")


TICK_NOT_DUE = 3  # `queue --tick` exit code: not due; 0 = due, run.ps1 starts publishing


@app.command()
def queue(
    project_id: str | None = typer.Argument(
        None,
        help=(
            "project id; without it, lists projects with an active schedule, one per line "
            "(the scheduler tick walks them; test and hidden projects never publish)"
        ),
    ),
    times: str | None = typer.Option(
        None,
        "--times",
        help="set daily slots: '12:00,18:00' (local time); an empty string clears the schedule",
    ),
    pause: bool = typer.Option(
        False, "--pause", help="pause scheduled publishing, keeping the slots"
    ),
    resume: bool = typer.Option(False, "--resume", help="unpause"),
    tick: bool = typer.Option(
        False,
        "--tick",
        help=(
            "scheduler tick: is it time to publish. Exit 0 = due (slot time passed, "
            "nothing published since, network up), 3 = not due. If the target is closer "
            "than the tick interval, the tick waits for it. Trace: queue.tick.json"
        ),
    ),
    tick_result: int | None = typer.Option(
        None,
        "--tick-result",
        help=(
            "publish exit code after a tick that returned 0: non-zero (or the platform did "
            "not respond) hands the slot to the next tick, up to 3 attempts"
        ),
    ),
    every: int = typer.Option(
        15,
        "--every",
        help=(
            "scheduler task interval in minutes: how long a tick waits for the slot time, "
            "also used to detect a silent scheduler (run.ps1 -Every)"
        ),
    ),
) -> None:
    """A project's publishing queue: what went out, what goes next and when, what is pending.

    Order comes from `projects/<project>/queue.yaml` (`episodes`, next on top);
    unlisted episodes go last. State is derived from episode files (manifest,
    final.mp4), not stored. The `schedule` block holds daily slots; the plan
    and buffer are computed from it and the queue. Without a project, prints
    unpaused scheduled project ids, one per line (read by `run.ps1 -Project all`)."""
    from videoforge.core import queue as q

    if project_id is None:
        if times is not None or pause or resume or tick or tick_result is not None:
            _fail("для --times, --pause, --resume, --tick и --tick-result нужен id проекта")
            return
        live = q.scheduled_projects()
        for pid in live:
            typer.echo(pid)
        if not live:
            typer.echo("проектов с расписанием нет", err=True)
        return

    if times is not None or pause or resume:
        current = q.load_schedule(project_id)
        slots = current.times
        if times is not None:
            try:
                slots = q.parse_times([t for t in times.split(",") if t.strip()])
            except ValueError as exc:
                _fail(str(exc))
                return
        paused = True if pause else False if resume else current.paused
        path = q.save_schedule(
            project_id, q.Schedule(times=slots, paused=paused, spread_min=current.spread_min)
        )
        typer.echo(f"расписание записано: {path}")

    if tick_result is not None:
        done = q.tick_result(project_id, tick_result)
        typer.echo(done.why if done else "тика с запуском не было — записывать нечего")
        return

    if tick:
        import time as _time

        if every < 1:
            _fail("--every: шаг задачи в минутах, меньше одной не бывает")
            return
        result = q.tick(project_id, online=q.is_online(), every_min=every)
        wait = result.wait_sec(q.local_now())
        if wait:
            # slot time is closer than the task interval: wait here so the video
            # goes out on time rather than on the task's grid
            typer.echo(f"жду {wait} с: {result.why}")
            _time.sleep(wait)
            result = q.tick(project_id, online=q.is_online(), every_min=every)
        typer.echo(("пора: " if result.due else "не пора: ") + result.why)
        if not result.due:
            raise typer.Exit(TICK_NOT_DUE)
        return

    try:
        rows = q.queue(project_id)
    except FileNotFoundError as exc:
        _fail(str(exc))
        return
    if not rows:
        typer.echo(f"у проекта {project_id} нет эпизодов")
        return
    for item in rows:
        typer.echo(f"  {item.line}")
    ready = sum(r.state == "ready" for r in rows)
    published = sum(r.state == "published" for r in rows)
    nxt = q.next_ready(project_id)
    typer.echo("")
    typer.echo(f"готовых к загрузке: {ready} · уехало: {published}")
    if nxt is not None:
        typer.echo(f"следующий: {nxt.episode} — videoforge publish {project_id} --to youtube")
    path = q.queue_path(project_id)
    if not path.exists():
        typer.echo(f"порядок не задан (нет {path.name}) — берётся порядок эпизодов на диске")

    schedule = q.load_schedule(project_id)
    if not schedule.times:
        typer.echo("расписания нет — videoforge queue <проект> --times 12:00,18:00")
    else:
        state = " · ПАУЗА" if schedule.paused else ""
        spread = f", разброс до {schedule.spread_min} мин" if schedule.spread_min else ""
        typer.echo(
            f"расписание: {', '.join(schedule.times)} ({schedule.per_day} в день{spread}){state}"
        )
        for slot in q.plan(project_id):
            what = (
                f"{slot.item.episode} · {slot.item.title}"
                if slot.item
                else "— нечего: очередь кончилась"
            )
            mark = "  ← ждёт тика" if slot.pending else ""
            typer.echo(f"  {slot.at:%d.%m %H:%M}  {what}{mark}")
    last = q.load_tick(project_id)
    if last is not None:
        typer.echo(f"тик планировщика: {last.at:%d.%m %H:%M} — {last.why}")
    for note in q.notices(project_id, live=schedule.active):
        typer.echo(
            f"{'!!' if note.level == 'bad' else '! ' if note.level == 'warn' else '  '} {note.text}"
        )


@app.command()
def publish(
    ref: str = typer.Argument(help="<project>/<episode>, or just <project> for the next in queue"),
    to: str = typer.Option(
        "", "--to", help="targets: youtube,tiktok,instagram (empty = the project's platforms)"
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="actually upload. Without it the command only shows what would be sent",
    ),
    dry_run: bool | None = typer.Option(
        None, "--dry-run/--no-dry-run", help="dry run; the default without --yes"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "re-publish something already published; with --update, bypass the gate "
            "and re-send the cover even if it was already sent"
        ),
    ),
    update: bool = typer.Option(
        False,
        "--update",
        help="do not upload; update a published video: visibility, text, languages, cover",
    ),
    status: bool = typer.Option(
        False,
        "--status",
        help=(
            "do not upload; ask the platform about a sent video: processed, still "
            "processing, rejected and why (without an episode: all sent in the project)"
        ),
    ),
    privacy: str | None = typer.Option(
        None,
        "--privacy",
        help="visibility: private | unlisted | public (default: the project's channel.privacy)",
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="provider: mock | youtube_api | browser (VF_PUBLISH_PROVIDER)"
    ),
) -> None:
    """Publish a finished video to platforms.

    Takes `05_assemble/final.mp4` and the packaging `06_publish/publish.json`
    and hands them to a provider: YouTube via the Data API, TikTok and Instagram
    via the account's browser session.

    **Dry run by default.** Real uploads need `--yes`, because an upload cannot
    be taken back. Already published videos are not re-uploaded (unless
    `--force`); state lives in the episode's manifest.json.

    `--update` edits a video already on the channel (same link and views),
    syncing visibility, title, description, tags, languages and cover with the
    packaging; the dry run shows before → after per field. YouTube needs
    extended access for this and says so if re-authorization is required.

    `--status` only asks the platform what happened to a sent video (read-only,
    1 quota unit per video). The manifest is fixed in one case only: an upload
    that dropped without a response but was found processed is recorded as
    sent; other mismatches are flagged for a human.

    Without an episode name (`videoforge publish podcast_clips`), the next ready
    episode from the project queue is used, as shown by `videoforge queue`."""
    from videoforge.core.queue import next_ready
    from videoforge.core.runner import Busy
    from videoforge.providers.registry import build_publisher
    from videoforge.stages.publish_upload import NotReady, publish_episode, update_episode

    if status:
        if update:
            _fail("--status и --update вместе не бывает: либо спросить, либо править")
            return
        _publish_status(ref, [p.strip() for p in to.split(",") if p.strip()], provider)
        return
    if "/" not in ref.strip("/") and update:
        # the queue yields episodes READY to upload, i.e. not yet sent, so it has
        # nothing to update; silently picking another episode is worse than refusing
        _fail("правке нужен конкретный эпизод: videoforge publish <проект>/<эпизод> --update")
        return
    if "/" not in ref.strip("/"):
        project_id = ref.strip("/")
        item = next_ready(project_id)
        if item is None:
            _fail(
                f"в очереди проекта {project_id} нет готовых роликов — "
                f"videoforge queue {project_id} покажет, чего каждому не хватает"
            )
            return
        typer.echo(f"следующий в очереди: {item.episode} · {item.title}")
        ref = f"{project_id}/{item.episode}"
    edir = _episode(ref)
    targets = [p.strip() for p in to.split(",") if p.strip()]
    if privacy and privacy not in ("private", "unlisted", "public"):
        _fail(f"видимость {privacy!r} не бывает: private | unlisted | public")
        return
    # only an explicit --yes sends; `--dry-run` alongside it wins, so a command
    # can be checked safely without fear of a typo in another flag
    really = yes and dry_run is not True
    try:
        if update:
            report = update_episode(
                edir,
                to=targets,
                dry_run=not really,
                force=force,
                privacy=privacy,
                build=(lambda platform, account: build_publisher(platform, account, name=provider)),
                log_fn=typer.echo,
            )
        else:
            report = publish_episode(
                edir,
                to=targets,
                dry_run=not really,
                force=force,
                privacy=privacy,
                build=(lambda platform, account: build_publisher(platform, account, name=provider)),
                log_fn=typer.echo,
            )
    except (NotReady, ProviderError, Busy) as exc:
        _fail(str(exc))
        return
    what, of = ("правка", "правки") if update else ("публикация", "публикации")
    head = f"сухой прогон {of}" if report.dry_run else what
    typer.echo(f"{head}: «{report.title}» · {report.video}")
    for w in report.warnings:
        typer.echo(f"  ! {w}")
    for row in report.platforms:
        typer.echo(f"  {row.line}")
        for change in row.changes:
            typer.echo(f"    {change}")
        for w in row.warnings:
            typer.echo(f"    ! {w}")
    if report.dry_run:
        tail = "Поправить по-настоящему" if update else "Загрузить по-настоящему"
        typer.echo(f"ничего не отправлено. {tail}: добавь --yes")
    if not report.ok:
        raise typer.Exit(1)


def _publish_status(ref: str, targets: list[str], provider: str | None) -> None:
    """`publish … --status`: what the platform did with a sent video.

    One episode or all sent episodes of a project, platform by platform.
    Manifest mismatches are printed as a check line; the only fix (a dropped
    upload found processed) is printed as recorded. Exit code 1 means some
    platform could not be queried; a mismatch is an answer, not a failure."""
    from videoforge.core.runner import Busy
    from videoforge.paths import project_dir
    from videoforge.providers.registry import build_publisher
    from videoforge.stages.publish_upload import NotReady, check_episode, sent_episodes

    ref = ref.strip("/")
    if "/" in ref:
        refs = [ref]
    else:
        if not project_dir(ref).is_dir():
            _fail(f"проекта {ref} нет: {project_dir(ref)}")
            return
        episodes = sent_episodes(ref)
        if not episodes:
            _fail(f"у проекта {ref} ни один ролик площадке не отдавали — спрашивать не о чем")
            return
        refs = [f"{ref}/{ep}" for ep in episodes]
    bad = False
    for one in refs:
        edir = _episode(one)
        typer.echo(f"{one}:")
        try:
            report = check_episode(
                edir,
                to=targets,
                build=(lambda platform, account: build_publisher(platform, account, name=provider)),
            )
        except (NotReady, ProviderError, Busy) as exc:
            typer.echo(f"  {exc}")
            bad = True
            continue
        for row in report.platforms:
            typer.echo(f"  {row.line}")
            if row.fixed:
                typer.echo(f"    записано в манифест: {row.fixed}")
            for check in row.checks:
                typer.echo(f"    ПРОВЕРЬ: {check}")
            for note in row.notes:
                typer.echo(f"    ! {note}")
        bad = bad or not report.ok
    if bad:
        raise typer.Exit(1)


# ---------------------------------------------------------------- analyze


def _analysis(slug: str) -> Path:
    from videoforge.intel.analysis import resolve_analysis

    try:
        return resolve_analysis(slug)
    except FileNotFoundError as exc:
        _fail(str(exc))
        raise AssertionError from None  # unreachable


@analyze_app.command("new")
def analyze_new(
    slug: str = typer.Argument(help="Latin snake_case id: extreme_living"),
    url: list[str] = typer.Option([], "--url", help="channel link; several for competitors"),
    mode: str = typer.Option("channel", help="channel | video (phase 3) | competitors (phase 4)"),
    collector: str = typer.Option("ytdlp", help="ytdlp (phase 1) | mock: a free stub"),
    goal: str = typer.Option("", help="purpose: what we want to understand and replicate"),
    language: str = typer.Option("ru", help="language of the report's conclusions"),
    max_videos: int = typer.Option(0, help="0 = all videos; N = only the latest N"),
    threshold: str = typer.Option(
        "auto", help="outlier threshold: auto (from channel spread) or a number, e.g. 3.0"
    ),
    deep_top: int = typer.Option(5, help="how many top videos to analyze in depth"),
    scene_threshold: float = typer.Option(
        SCENE_THRESHOLD, help="shot-change detection: 0.05 for 2D animation, 0.3 for live action"
    ),
    budget: float = typer.Option(3.0, help="analysis spending limit, USD"),
) -> None:
    """Create an analysis: research/<slug>/ with analysis.yaml (commented template)."""
    from videoforge.intel.analysis import AnalysisSpec, create_analysis

    if not url:
        _fail("нужна хотя бы одна ссылка: --url https://www.youtube.com/@channel")
    parsed_threshold: float | str = "auto"
    if threshold != "auto":
        try:
            parsed_threshold = float(threshold)
        except ValueError:
            _fail(f"--threshold: 'auto' или число, получено '{threshold}'")
    spec = AnalysisSpec(
        id=slug,
        urls=url,
        mode=mode,  # type: ignore[arg-type]
        collector=collector,
        goal=goal,
        language=language,
        max_videos=max_videos,
        outlier_threshold=parsed_threshold,  # type: ignore[arg-type]
        deep_top=deep_top,
        scene_threshold=scene_threshold,
        budget_usd=budget,
    )
    try:
        adir = create_analysis(spec)
    except (FileExistsError, ValueError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"создан анализ {adir}")
    typer.echo(f"  {adir / 'analysis.yaml'}  — настройки")
    typer.echo(f"дальше: videoforge analyze run {slug}")


@analyze_app.command("run")
def analyze_run(
    slug: str = typer.Argument(help="analysis id from research/"),
    mock: bool = typer.Option(False, help="collector and LLM are free stubs"),
    collector: str | None = typer.Option(None, help="override the collector in analysis.yaml"),
    llm: str | None = typer.Option(
        None, help="LLM for classify: claude_cli (subscription) | gemini | mock"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="do not stop at gates"),
    ignore_budget: bool = typer.Option(False, help="do not check the forecast against budget"),
    only: str | None = typer.Option(None, help="run a single stage"),
    start: str | None = typer.Option(None, "--from", help="start from this stage"),
    stop: str | None = typer.Option(None, "--to", help="stop after this stage (inclusive)"),
    force: bool = typer.Option(False, help="redo even what is already done"),
) -> None:
    """Run analysis stages: collect → stats → classify (→ deep → formula)."""
    from videoforge.config import intel_llm_provider
    from videoforge.intel.analysis import load_analysis_from_dir
    from videoforge.intel.collectors import build_collector
    from videoforge.intel.runner import run_analysis
    from videoforge.providers.base import LlmProvider
    from videoforge.providers.registry import build_llm, missing_keys

    adir = _analysis(slug)
    cfg = load_analysis_from_dir(adir)
    name = "mock" if mock else (collector or cfg.collector)
    llm_name = "mock" if mock else (llm or intel_llm_provider())
    try:
        coll = build_collector(name)
    except ValueError as exc:
        _fail(f"коллектор: {exc}")
        return
    llm_provider: LlmProvider
    if llm_name == "mock":
        from videoforge.intel.mock_llm import MockIntelLlm

        llm_provider = MockIntelLlm()
    else:
        try:
            llm_provider = build_llm(llm_name)
        except ValueError as exc:
            _fail(f"LLM: {exc}")
            return
        missing = missing_keys([llm_name])
        if missing:
            _fail(f"нет ключей: {', '.join(missing)} (задай в .env) — или --llm claude_cli/--mock")
    typer.echo(f"коллектор: {coll.name} · LLM: {llm_provider.name}")
    options = RunOptions(
        auto_approve=yes,
        ignore_budget=ignore_budget,
        only=only,
        start=start,
        stop=stop,
        force=force,
    )
    report = run_analysis(adir, coll, options, llm=llm_provider)

    manifest = load_manifest(adir)
    typer.echo(f"потрачено: ${manifest.spent_usd:.3f} из ${manifest.budget_usd:.2f}")
    if report.awaiting_approval:
        typer.echo(
            f"\nстоп: результат стадии '{report.awaiting_approval}' ждёт просмотра:\n"
            f"  videoforge analyze report {slug} --open\n"
            f"  videoforge analyze approve {slug} {report.awaiting_approval}\n"
            f"  videoforge analyze run {slug}"
        )
        raise typer.Exit(2)
    if report.reason:
        typer.echo(f"\nстоп на '{report.stopped_at}': {report.reason}", err=True)
        raise typer.Exit(1)


@analyze_app.command("report")
def analyze_report(
    slug: str = typer.Argument(help="analysis id from research/"),
    show: bool = typer.Option(False, "--open", help="open in a browser"),
) -> None:
    """Build the analysis HTML report: view dynamics, outliers, tables.

    report.html is written to the analysis folder."""
    from videoforge.intel.report import render_analysis_report

    adir = _analysis(slug)
    path = render_analysis_report(adir)
    typer.echo(f"отчёт: {path}")
    if show:
        typer.launch(str(path))


@analyze_app.command("status")
def analyze_status(slug: str = typer.Argument(help="analysis id from research/")) -> None:
    """Stage statuses, costs, top outliers."""
    from videoforge.core.io import read_model
    from videoforge.intel.analysis import load_analysis_from_dir
    from videoforge.intel.models import StatsReport
    from videoforge.intel.stages.stats import STATS_DIR, STATS_FILE

    adir = _analysis(slug)
    cfg = load_analysis_from_dir(adir)
    m = load_manifest(adir)
    typer.echo(
        f"{slug} · workflow {m.workflow} · {cfg.mode}: {', '.join(cfg.urls)} · "
        f"бюджет ${m.budget_usd:.2f} · потрачено ${m.spent_usd:.3f}"
    )
    if m.awaiting_approval:
        typer.echo(
            f"ждёт одобрения: {m.awaiting_approval} → "
            f"videoforge analyze approve {slug} {m.awaiting_approval}"
        )
    typer.echo("стадии:")
    for ws in load_workflow(m.workflow).stages:
        rec = m.stages.get(ws.name, StageRecord())
        gate = ""
        if ws.gate_after:
            gate = " [ворота: одобрено]" if ws.name in m.approvals else " [ворота]"
        cost = m.stage_cost(ws.name)
        cost_s = f" ${cost:.3f}" if cost else ""
        typer.echo(f"  {ws.name:<10} {rec.status:<9}{cost_s}{gate} {rec.error or rec.message}")
    stats_path = adir / STATS_DIR / STATS_FILE
    if stats_path.exists():
        report = read_model(stats_path, StatsReport)
        cadence = ""
        if report.cadence_per_week:
            cadence = f" · публикаций в неделю: {report.cadence_per_week}"
        typer.echo(
            f"видео: {report.counted_videos} из {report.total_videos} с просмотрами{cadence}"
        )
        for o in report.outliers[:5]:
            typer.echo(f"  ×{o.ratio:<6} {o.views:>10} [{o.group}] {o.title} ({o.video_id})")
        if len(report.outliers) > 5:
            typer.echo(f"  … всего выбросов: {len(report.outliers)}")
        typer.echo(f"полная таблица: {stats_path}")
    from videoforge.intel.models import ClassifyReport
    from videoforge.intel.stages.classify import classify_path

    if classify_path(adir).exists():
        cls_report = read_model(classify_path(adir), ClassifyReport)
        formats = ", ".join(f"{f.name or f.format} ({f.count})" for f in cls_report.formats[:5])
        typer.echo(f"ниша: {cls_report.taxonomy.niche}")
        typer.echo(f"форматы: {formats or '—'}")
    from videoforge.intel.models import DeepReport
    from videoforge.intel.stages.deep import deep_index_path

    if deep_index_path(adir).exists():
        deep = read_model(deep_index_path(adir), DeepReport)
        done = sum(1 for v in deep.videos if v.status == "done")
        typer.echo(f"deep: разобрано {done} из {len(deep.videos)} ({adir / '04_deep'})")
    from videoforge.intel.stages.formula import blueprint_path, formula_path

    if formula_path(adir).exists():
        typer.echo(f"формула готова: {blueprint_path(adir)}")


@analyze_app.command("approve")
def analyze_approve(
    slug: str = typer.Argument(help="analysis id from research/"),
    stage: str = typer.Argument(help="stage name: stats, formula …"),
) -> None:
    """Approve an analysis stage result (lift the gate)."""
    adir = _analysis(slug)
    approve_stage(adir, stage)
    typer.echo(f"одобрено: {stage}. Дальше: videoforge analyze run {slug}")


@analyze_app.command("index")
def analyze_index() -> None:
    """Rebuild research/index.md, the table of contents of all analyses."""
    from videoforge.intel.index import write_index

    typer.echo(f"оглавление: {write_index()}")


@analyze_app.command("list")
def analyze_list() -> None:
    """List analyses and their state."""
    from videoforge.intel.analysis import list_analyses, load_analysis_from_dir
    from videoforge.paths import analysis_dir, research_dir

    slugs = list_analyses()
    if not slugs:
        typer.echo(f"анализов нет ({research_dir()})")
        return
    for slug in slugs:
        adir = analysis_dir(slug)
        cfg = load_analysis_from_dir(adir)
        m = load_manifest(adir)
        # for analysis stages, skipped = "done earlier, artifacts are fresh"
        done = [s for s, r in m.stages.items() if r.status in ("done", "skipped")]
        typer.echo(
            f"{slug:<20} {cfg.mode:<11} {cfg.urls[0]:<50} "
            f"готово стадий: {len(done)}  ${m.spent_usd:.2f}"
        )


# ---------------------------------------------------------------- resources


def _project_base(project: str | None) -> Path | None:
    """The project's resources/ dir, after checking that the project exists."""
    if not project:
        return None
    if not project_config_path(project).exists():
        _fail(f"проекта '{project}' нет: {project_config_path(project)}")
    return project_dir(project) / "resources"


def _resource_dirs(project: str | None) -> list[Path]:
    return search_dirs(project_dir(project)) if project else search_dirs(None)


@resource_app.command("new")
def resource_new(
    kind: str = typer.Argument(help="character | location | prop | style | voice | niche"),
    resource_id: str = typer.Argument(help="Latin id: timur, compass, pixar3d"),
    name: str = typer.Option("", help="human-readable name"),
    project: str | None = typer.Option(
        None, help="create inside a project (projects/<id>/resources): a series-specific cast"
    ),
) -> None:
    """Create a resource from a commented template. Without --project it is shared."""
    base = _project_base(project)
    try:
        path = create_resource(kind, resource_id, name=name, base=base)
    except (ValueError, FileExistsError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"создан {path}")
    if resource_kind(kind) in VISUAL_KINDS:
        typer.echo(f"портфолио (фото-референсы) — клади в {path.parent / resource_id}/")
    suffix = f" --project {project}" if project else ""
    typer.echo(f"заполни поля, потом: videoforge resource show {kind} {resource_id}{suffix}")


@resource_app.command("rename")
def resource_rename(
    kind: str = typer.Argument(help="voice | style | caption_style | character | …"),
    resource_id: str = typer.Argument(help="resource id (unchanged)"),
    name: str = typer.Argument(help="new human-readable name"),
    project: str | None = typer.Option(None, help="also search the project's resources"),
) -> None:
    """Rename a resource. Only `name` changes; the id and all references stay,
    same as the pencil icon on the resource page in the UI."""
    try:
        path = rename_resource(kind, resource_id, name, _resource_dirs(project))
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"{path}: {name}")


def _lyrics_check(track: MusicTrack) -> list[str]:
    """Check that the lyrics file matches the track. Checked here, where the
    material is added; otherwise a transcript of a different recording only
    shows up in the finished video as on-screen text that mismatches vocals."""
    from videoforge.core.captions import lyrics_problems, read_lyrics
    from videoforge.core.ffmpeg import FfmpegError, probe_duration

    path = track.lyrics_path()
    if path is None:
        return []
    words, _, raw = read_lyrics(path)
    try:
        track_sec: float | None = probe_duration(track.path())
    except (FfmpegError, OSError):
        track_sec = None
    return lyrics_problems(raw, words, track_sec=track_sec)


@resource_app.command("show")
def resource_show(
    kind: str,
    resource_id: str,
    project: str | None = typer.Option(None, help="also search the project's resources"),
) -> None:
    """Validate and show a resource (with --project, the project one wins)."""
    try:
        k = resource_kind(kind)
        obj = load_resource(k, resource_id, KINDS[k], _resource_dirs(project))
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
        return
    typer.echo(yaml.safe_dump(obj.model_dump(), allow_unicode=True, sort_keys=False).rstrip())
    if isinstance(obj, VisualResource):
        refs = obj.reference_paths()
        note = "" if refs else f" — клади фото в {obj.folder()}"
        typer.echo(f"референсов в портфолио: {len(refs)}{note}")
    empty = [f for f, v in obj.model_dump().items() if v in ("", [], None) and f != "description"]
    if empty:
        typer.echo(f"не заполнено: {', '.join(empty)}")
    if isinstance(obj, MusicTrack):
        for problem in _lyrics_check(obj):
            typer.echo(f"ПРОВЕРЬ слова песни: {problem}")


@resource_app.command("add-ref")
def resource_add_ref(
    kind: str = typer.Argument(help="character | location | prop"),
    resource_id: str = typer.Argument(),
    image: Path = typer.Argument(help="image file (png/jpg/webp)"),
    project: str | None = typer.Option(None, help="also search the project for the resource"),
    as_name: str | None = typer.Option(
        None, "--as", help="core name by shot content: face_front, body_full_back"
    ),
) -> None:
    """Add a photo to the portfolio by copying it into the resource folder. This is
    how a good frame becomes canonical: later generations use it as a reference."""
    try:
        dest = add_reference_image(
            kind, resource_id, image, _resource_dirs(project), rename=as_name
        )
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"добавлено в портфолио: {dest}")


@resource_app.command("gen-example")
def resource_gen_example(
    kind: str = typer.Argument(help="caption_style | voice (other kinds have no example)"),
    resource_id: str | None = typer.Argument(None, help="resource id; without it, all of the kind"),
    project: str | None = typer.Option(None, help="also search the project's resources"),
    variants: bool = typer.Option(
        False,
        "--variants",
        help="voice: extra delivery samples next to example.mp3, rate ±10%, pitch ±4 Hz",
    ),
) -> None:
    """Build a resource example: a caption-style clip or a voice sample.

    Saved next to the resource as `<kind>/<id>/example.*` and shown in the UI.
    Styles use the same renderer as the final video ($0, ffmpeg); voices use
    edge-tts ($0, needs network). Rebuild after editing a style. `--variants`
    renders the same phrase at other rates and pitches (`example.rate-10.mp3`,
    `example.pitch+4Hz.mp3`) to pick a delivery by ear before setting it in
    the resource or in `Scene.voice`."""
    from videoforge.core.ffmpeg import FfmpegError
    from videoforge.core.resource_examples import NoRecipe, render_example, render_variants
    from videoforge.providers.edge_tts_provider import EdgeTts

    def synth(text: str, voice: Voice, out: Path) -> None:
        EdgeTts().synthesize(text, voice=voice.voice, rate=voice.rate, pitch=voice.pitch,
                             volume=voice.volume, out_path=out)  # fmt: skip

    try:
        k = resource_kind(kind)
    except ValueError as exc:
        _fail(str(exc))
        return
    dirs = _resource_dirs(project)
    ids = [resource_id] if resource_id else list_resources(dirs[0]).get(k, [])
    if not ids:
        _fail(f"ресурсов вида {k} не найдено в {dirs[0]}")
    for rid in ids:
        try:
            obj = load_resource(k, rid, KINDS[k], dirs)
            out = render_example(k, obj, synth=synth)
            probes = render_variants(k, obj, synth=synth) if variants else []
        except NoRecipe as exc:
            _fail(str(exc))
            return
        except (FileNotFoundError, ValueError, FfmpegError, ProviderError) as exc:
            typer.echo(f"  ! {k}/{rid}: {exc}")
            continue
        typer.echo(f"  {k}/{rid}: {out}")
        for probe in probes:
            typer.echo(f"  {k}/{rid}: {probe}")


def _song_name(artist: str, song: str, *, name: str) -> tuple[str, str]:
    """Artist and song from a resource name such as "Artist — Song (live)".

    The separator is a dash, the usual way tracks are named. Without one, the
    whole name is the song title: searching by title alone still works, just
    less well."""
    if artist and song:
        return artist, song
    head, tail = "", name
    for dash in ("—", "–", " - "):
        if dash in name:
            head, tail = (part.strip() for part in name.split(dash, 1))
            break
    return artist or head, song or tail


@resource_app.command("gen-lyrics")
def resource_gen_lyrics(
    resource_id: str = typer.Argument(help="music resource id"),
    project: str | None = typer.Option(None, help="also search the project for the resource"),
    artist: str = typer.Option("", help="artist for lyrics search; empty = from resource name"),
    song: str = typer.Option("", "--track", help="song title; empty = from resource name"),
    no_lyrics: bool = typer.Option(False, help="do not look up lyrics, only transcribe"),
    model: str = typer.Option("", help="model: small | medium | large-v3 (default medium)"),
    device: str = typer.Option("", help="cpu (default) | cuda (needs CUDA libraries)"),
    language: str = typer.Option("", help="vocal language (en, ru); empty = auto-detect"),
    force: bool = typer.Option(False, help="re-transcribe even if the file exists"),
) -> None:
    """Transcribe song lyrics FROM THE TRACK ITSELF → `<track>.srt` next to it.

    A ready-made lyrics file cannot be trusted: it may come from a different
    recording or a lossy machine transcript. Timings and words here come from
    the file actually used in the video, so the lyrics track in the UI is a
    second opinion rather than a copy of the on-screen text.

    Free (runs locally) but slow; needs `uv sync --extra transcribe`. Sung
    vocals transcribe worse than speech, so review the result in the montage."""
    try:
        track = load_resource("music", resource_id, MusicTrack, _resource_dirs(project))
    except (FileNotFoundError, KeyError) as exc:
        _fail(str(exc))
        return
    audio = track.path()
    if not audio.exists():
        _fail(f"файла трека нет: {audio}")
        return
    out = audio.with_suffix(".srt")
    if out.exists() and not force:
        typer.echo(f"уже есть: {out} (--force — перераспознать)")
        return
    from videoforge.core.ffmpeg import FfmpegError, probe_duration
    from videoforge.providers.base import ProviderError
    from videoforge.providers.registry import build_lyrics
    from videoforge.providers.whisper_local import WhisperLocal

    try:
        seconds = probe_duration(audio)
    except (FfmpegError, OSError):
        seconds = 0.0
    # words come from the lyrics, timings from the audio: a third-party file has
    # wrong timings, and speech recognition on singing gets words wrong
    text = ""
    if not no_lyrics:
        who, what = _song_name(artist, song, name=track.name or resource_id)
        try:
            found = build_lyrics().fetch(artist=who, track=what, duration_sec=seconds)
        except ProviderError as exc:
            typer.echo(f"текст песни не искали: {exc}")
            found = None
        if found:
            text = found.text
            typer.echo(f"текст песни: {found.source}")
        else:
            typer.echo(
                f"текста песни не нашлось ({who} — {what}): слова останутся такими, "
                "как их расслышала модель. Это её слабое место на пении"
            )
    provider = WhisperLocal(model=model, device=device, lyrics=text)
    typer.echo(f"распознаю {audio.name}: модель {provider.model_name}, {provider.device}…")
    try:
        result = provider.transcribe(
            url="", video=audio, language=language, duration_sec=0.0, out_path=out
        )
    except (RuntimeError, ValueError, OSError) as exc:
        _fail(str(exc))
        return
    meta = result.meta
    typer.echo(f"слов: {meta.get('words')} · {meta.get('seconds')} с · язык {result.language} · $0")
    typer.echo(f"готово: {out}")
    if track.lyrics and track.lyrics != out.name:
        typer.echo(f"ПОСТАВЬ в {resource_id}.yaml: lyrics: {out.name} (сейчас {track.lyrics})")


@resource_app.command("gen-sfx")
def resource_gen_sfx(
    project: str | None = typer.Option(None, help="write to project resources, not shared"),
    force: bool = typer.Option(False, help="regenerate existing ones"),
) -> None:
    """Build the sound-effects library: hit, whoosh, riser, glitch.

    Sounds are synthesized locally with ffmpeg from formulas in
    `core/audio_synth.py`, so there is no author and no Content ID risk. Each is
    peak-normalized so that "-6 dB" in an edit plan means the same for all.
    Bought or recorded sounds are added as the same resource by hand."""
    from videoforge.core.audio_synth import build_pack
    from videoforge.core.ffmpeg import FfmpegError

    base = _project_base(project) or resources_dir()
    try:
        made = build_pack(base, force=force)
    except FfmpegError as exc:
        _fail(str(exc))
        return
    for rid, note in made:
        typer.echo(f"  sfx/{rid}: {note}")
    typer.echo(f"библиотека: {base / 'sfx'}")


@resource_app.command("gen-music")
def resource_gen_music(
    resource_id: str | None = typer.Argument(
        None, help="resource id for --preset; without it the whole loop pack is built"
    ),
    preset: str | None = typer.Option(
        None, help="a single preset instead of the pack: flat_pad, a flat bed (bed: true)"
    ),
    project: str | None = typer.Option(None, help="write to project resources, not shared"),
    force: bool = typer.Option(False, help="regenerate existing ones"),
) -> None:
    """Build music loops: a dark drive and a heavy drop; with --preset, a single
    resource under its own id (flat_pad: a flat bed for the project's audio.bed).

    Same synthesis as the sound effects, for the same reason: hear the edit
    today with no accounts and no Content ID risk. A placeholder for a real
    track, not a replacement; a bought track goes next to it as a resource."""
    from videoforge.core.audio_synth import build_music_pack, build_preset
    from videoforge.core.ffmpeg import FfmpegError

    base = _project_base(project) or resources_dir()
    try:
        if preset is not None:
            made = [build_preset(base, preset, resource_id, force=force)]
        elif resource_id is not None:
            _fail(f"id {resource_id!r} задан без --preset: какой пресет синтезировать?")
            return
        else:
            made = build_music_pack(base, force=force)
    except (FfmpegError, KeyError) as exc:
        _fail(str(exc))
        return
    for rid, note in made:
        typer.echo(f"  music/{rid}: {note}")
    typer.echo(f"петли: {base / 'music'}")


@resource_app.command("gen-portfolio")
def resource_gen_portfolio(
    kind: str = typer.Argument(help="only character for now"),
    resource_id: str = typer.Argument(),
    shots: str | None = typer.Option(
        None, help="comma-separated shots (face_front,body_full_back); default: missing core"
    ),
    count: int = typer.Option(1, help="candidates per shot (3 for picking a face)"),
    style: str | None = typer.Option(
        None, help="style id: references use the production style, which carries into scenes"
    ),
    project: str | None = typer.Option(None, help="also search the project for character/style"),
    mock: bool = typer.Option(False, help="free stub instead of the provider"),
    yes: bool = typer.Option(False, "--yes", "-y", help="accept the cost forecast"),
) -> None:
    """Generate core portfolio shots into <portfolio>/_candidates/ (paid: without
    --yes, prints the forecast and exits). The existing core is used as reference
    so new shots keep the same lineage. Approve a shot with add-ref --as."""
    from videoforge.providers.registry import build_image, missing_keys
    from videoforge.stages.portfolio import (
        CORE_SHOT_NAMES,
        SHOTS_BY_NAME,
        generate_shots,
        missing_core_shots,
    )

    dirs = _resource_dirs(project)
    try:
        if resource_kind(kind) != "characters":
            _fail("генерация портфолио пока только для персонажей")
        character = load_resource("characters", resource_id, Character, dirs)
        style_res = load_resource("styles", style, Style, dirs) if style else None
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc))
        return
    if shots:
        names = [s.strip() for s in shots.split(",") if s.strip()]
    else:
        names = missing_core_shots(character)
    unknown = [n for n in names if n not in SHOTS_BY_NAME]
    if unknown:
        _fail(f"неизвестные кадры: {', '.join(unknown)}; ядро: {', '.join(CORE_SHOT_NAMES)}")
    if not names:
        typer.echo("ядро портфолио уже полное — укажи --shots, чтобы переделать конкретные кадры")
        return
    if not character.prompt_block.strip() and not character.reference_paths():
        _fail(
            "у персонажа нет ни паспорта (prompt_block), ни референсов — "
            "генерации не на что опереться"
        )
    image_name = "mock" if mock else ProviderNames.from_env().image
    try:
        provider = build_image(image_name)
    except (ProviderError, ValueError) as exc:
        _fail(f"провайдер картинок: {exc}")
        return
    missing = missing_keys([image_name])
    if missing:
        _fail(f"нет ключей: {', '.join(missing)} (задай в .env) — или запусти с --mock")
    estimate = len(names) * count * provider.price_usd
    typer.echo(
        f"кадры: {', '.join(names)} × {count} · провайдер {provider.name} · прогноз ${estimate:.2f}"
    )
    if estimate > 0 and not yes:
        _fail("платная генерация: перезапусти с --yes, если согласен с прогнозом")
    try:
        results = generate_shots(provider, character, names, style=style_res, count=count)
    except ProviderError as exc:
        _fail(str(exc))
        return
    for r in results:
        typer.echo(f"  {r.path}")
    typer.echo(f"потрачено: ${sum(r.cost_usd for r in results):.3f}")
    suffix = f" --project {project}" if project else ""
    typer.echo(
        "посмотри кандидатов; удачный кадр — в ядро:\n"
        f"  videoforge resource add-ref character {resource_id} <файл> --as <имя-кадра>{suffix}"
    )


@resource_app.command("list")
def resource_list(
    project: str | None = typer.Option(None, help="also show the project's resources"),
) -> None:
    """List resources: shared and (with --project) project-specific."""
    typer.echo(f"общие ({resources_dir()}):")
    for kind, ids in list_resources().items():
        typer.echo(f"  {kind}: {', '.join(ids) if ids else '—'}")
    base = _project_base(project)
    if base is not None:
        typer.echo(f"проектные ({base}):")
        for kind, ids in list_resources(base).items():
            typer.echo(f"  {kind}: {', '.join(ids) if ids else '—'}")


@app.command()
def voices(
    lang: str = typer.Option("ru", "--lang", help="locale prefix: ru, en, en-US"),
    provider: str = typer.Option("edge", "--provider", help="edge | elevenlabs"),
) -> None:
    """Voices for a language: edge-tts or ElevenLabs (own and library; needs a key)."""
    if provider == "elevenlabs":
        from videoforge.providers.elevenlabs import list_voices as eleven_voices

        try:
            found = eleven_voices(lang[:2])
        except ProviderError as exc:
            _fail(str(exc))
            return
        for v in found:
            labels = ", ".join(f"{k}={val}" for k, val in (v.get("labels") or {}).items())
            typer.echo(f"{v['id']:<24} {v['source']:<10} {v['name'][:28]:<28} {labels}")
        typer.echo('\nпослушать: videoforge say <id> "текст" --provider elevenlabs')
        return
    from videoforge.providers.edge_tts_provider import list_voices

    rows = list_voices(lang)
    if not rows:
        typer.echo(f"голосов для '{lang}' не найдено")
        return
    for v in rows:
        tags = v.get("VoiceTag", {}) or {}
        personalities = ", ".join(tags.get("VoicePersonalities", []) or [])
        typer.echo(f"{v['ShortName']:<32} {v['Gender']:<7} {personalities}")
    typer.echo(f'\nпослушать: videoforge say {rows[0]["ShortName"]} "Привет, это проба голоса"')


@app.command()
def say(
    voice: str = typer.Argument(
        help="voice ShortName (ru-RU-DmitryNeural) or an id from resources/voices"
    ),
    text: str = typer.Argument(help="text to speak"),
    out: Path | None = typer.Option(None, help="where to save the mp3"),
    rate: str | None = typer.Option(None, help='rate: "-10%", "+15%"'),
    pitch: str | None = typer.Option(None, help='pitch: "-20Hz", "+10Hz"'),
    provider: str | None = typer.Option(
        None, "--provider", help="edge | elevenlabs (default: from the resource, or edge)"
    ),
) -> None:
    """Speak a phrase and save an mp3, to pick a voice by ear."""
    from videoforge.providers.base import TtsProvider
    from videoforge.providers.edge_tts_provider import EdgeTts

    base_rate, base_pitch, base_provider = "+0%", "+0Hz", "edge"
    try:
        spec = load_resource("voices", voice, Voice)
        voice, base_rate, base_pitch = spec.voice, spec.rate, spec.pitch
        base_provider = spec.provider or "edge"
    except FileNotFoundError:
        pass
    rate = rate or base_rate
    pitch = pitch or base_pitch
    tts: TtsProvider = EdgeTts()
    if (provider or base_provider) == "elevenlabs":
        from videoforge.providers.elevenlabs import ElevenLabsTts

        tts = ElevenLabsTts()
    out = out or resources_dir() / "voices" / "samples" / f"{voice}.mp3"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        tts.synthesize(text, voice=voice, rate=rate, pitch=pitch, out_path=out)
    except ProviderError as exc:
        _fail(str(exc))
        return
    typer.echo(f"записано: {out}")


# ---------------------------------------------------------------- gpu


@gpu_app.command("up")
def gpu_up(
    project: str | None = typer.Argument(
        None, help="project: its character supplies the LoRA for the image model"
    ),
    video: str = typer.Option(
        "ltx25",
        help="video models to download, comma-separated: ltx25 | wan480 | both | animate "
        "(Wan 2.2 Animate: character replacement in a video; nodes: assets/gpu/animate_setup.sh) "
        "| anisora (anime fine-tune of Wan 2.2) | h3 (MiniMax H3: license excludes the EU and "
        "US, only with --dc outside them, ComfyUI 0.36)",
    ),
    image_models: str = typer.Option(
        "",
        help="image models beyond klein, comma-separated: qwen_edit (Qwen-Image-Edit-2511, "
        "panel → frame) | flux2_dev (FLUX.2 dev 32B, ~54 GB, needs a 48 GB+ GPU)",
    ),
    comfy: str = typer.Option("", help="ComfyUI tag (default v0.34.0, v0.36.0 with h3)"),
    gpu: str = typer.Option(None, help="GPU id in the RunPod catalog (default RTX PRO 4500 32 GB)"),
    dc: str = typer.Option(None, help="data center (default EU-RO-1)"),
    disk: int = typer.Option(None, help="container disk, GB (default 150)"),
    no_klein: bool = typer.Option(False, help="no image models (video only)"),
) -> None:
    """Start a pod with ComfyUI and models; then `run` with VF_IMAGE_PROVIDER=comfy
    VF_VIDEO_PROVIDER=comfy. It costs money until `gpu down`."""
    from videoforge.providers import runpod_pod

    banned = ("US-", "EU-", "UK-", "KR-")
    if "h3" in video.split(",") and (not dc or dc.upper().startswith(banned)):
        # the MiniMax H3 license excludes the EU, UK, Korea and US; the default pod
        # lands in EU-RO-1 (EU). EUR-IS / EUR-NO are EEA, not EU: a legal question
        _fail(
            f"h3: дата-центр {dc or 'EU-RO-1 (по умолчанию)'} внутри запрета лицензии MiniMax H3 "
            "(ЕС, Великобритания, Корея, США) — --dc CA-MTL-1 | AP-JP-1 | OC-AU-1, "
            "links/minimax_h3.yaml"
        )
        return
    spec = runpod_pod.PodSpec(
        video=video, klein=not no_klein, image_models=image_models, comfy=comfy
    )
    if gpu:
        spec.gpu = gpu
    if dc:
        spec.dc = dc
    if disk:
        spec.disk_gb = disk
    lora_file: Path | None = None
    strength = 1.0
    if project:
        cfg = load_project(project)
        if cfg.resources.character:
            character = load_resource(
                "characters", cfg.resources.character, Character, search_dirs(project_dir(project))
            )
            if character.lora:
                lora_file = character.lora_path()
                strength = character.lora.strength
                typer.echo(f"LoRA персонажа '{character.id}': {lora_file}")
            else:
                typer.echo(
                    f"у персонажа '{character.id}' нет lora: художник будет рисовать без неё"
                )
    try:
        state = runpod_pod.up(spec, lora_file=lora_file, lora_strength=strength, echo=typer.echo)
    except ProviderError as exc:
        _fail(str(exc))
        return
    typer.echo(
        f"готово: {state['base_url']} · ставка ${state['rate_usd_hr']}/ч · "
        f"состояние в {runpod_pod.STATE_FILE}\n"
        "дальше: VF_IMAGE_PROVIDER=comfy VF_VIDEO_PROVIDER=comfy videoforge run <проект>/<эпизод>\n"
        "закончил — videoforge gpu down"
    )


@gpu_app.command("down")
def gpu_down() -> None:
    """Terminate the pod and log the session to .gpu/sessions.csv."""
    from videoforge.providers import runpod_pod

    try:
        runpod_pod.down(echo=typer.echo)
    except ProviderError as exc:
        _fail(str(exc))


@gpu_app.command("status")
def gpu_status() -> None:
    """Is the pod alive, does ComfyUI respond, how much has it cost so far."""
    from videoforge.providers import runpod_pod

    try:
        st = runpod_pod.status()
    except ProviderError as exc:
        _fail(str(exc))
        return
    if not st.get("up"):
        typer.echo("под не поднят")
        return
    keys = (
        "pod_id", "base_url", "ssh", "gpu", "dc", "video", "lora", "runpod_status",
        "uptime_sec", "queue_running", "queue_pending", "comfy", "minutes", "cost_so_far_usd",
    )  # fmt: skip
    for key in keys:
        if key in st:
            typer.echo(f"{key}: {st[key]}")


# ---------------------------------------------------------------- doctor


def extras_status() -> list[tuple[str, list[str], list[str]]]:
    """Optional package parts: (extra, what it includes, what is missing).

    Extras are read from installed package metadata rather than hardcoded: a
    recreated venv silently loses extras, and a hardcoded list would miss them."""
    import re
    from importlib.metadata import PackageNotFoundError, metadata, version

    try:
        meta = metadata("videoforge")
    except PackageNotFoundError:
        return []
    wanted: dict[str, list[str]] = {e: [] for e in meta.get_all("Provides-Extra") or []}
    for line in meta.get_all("Requires-Dist") or []:
        requirement, _, marker = line.partition(";")
        extra = re.search(r"""extra\s*==\s*['"]([^'"]+)['"]""", marker)
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement.strip())
        if extra and name and extra.group(1) in wanted:
            wanted[extra.group(1)].append(name.group(0))
    rows = []
    # not `extra`: that name holds the re.search Match above, and mypy saw two types
    for extra_id, dists in wanted.items():
        missing = []
        for dist in dists:
            try:
                version(dist)
            except PackageNotFoundError:
                missing.append(dist)
        rows.append((extra_id, dists, missing))
    return rows


def still_status(names: ProviderNames) -> list[str]:
    """Doctor lines about still-image scenes: which projects need them and whether
    the provider (`VF_STILL_PROVIDER`) is ready. A project with `video.clip_share`
    < 1 sends scenes to it, and a missing Node.js makes the video stage fail on
    the first such scene, so it is better to find out here."""
    from videoforge.providers.registry import build_still

    mixed: list[str] = []
    for project_id in list_projects():
        try:
            share = load_project(project_id).video.clip_share
        except Exception:  # noqa: BLE001 - a broken project.yaml is reported elsewhere
            continue
        if share < 1:
            mixed.append(f"{project_id} ({share:.0%} клипами)")
    head = f"картинки-сцены (VF_STILL_PROVIDER): {names.still}"
    if not mixed:
        return [f"{head} — не нужны: у всех проектов video.clip_share 1.0"]
    try:
        problem = build_still(names.still).not_ready()
    except (ProviderError, ValueError) as exc:
        problem = str(exc)
    lines = [f"{head} — нужны проектам: {', '.join(mixed)}"]
    if problem:
        lines.append(f"  ! НЕ готов: {problem} — стадия video откажет до первой картинки-сцены")
    elif names.still == "mock":
        lines.append("  ! заглушка mock: для живого прогона задай VF_STILL_PROVIDER=remotion")
    else:
        lines.append("  готов")
    return lines


def controller_status(names: ProviderNames) -> list[str]:
    """Doctor lines about the frame and clip checker: who checks, whether it has
    a key, whether the price table knows its model. Only presence is reported;
    key values are never printed.

    A `claude_cli` checker triggers a warning: it does not look at images in
    production, so images/video would run with no real check
    (`features/vision_check_provider`)."""
    from videoforge.config import frame_review_enabled, vision_check_enabled
    from videoforge.providers.registry import REQUIRED_KEYS, build_llm

    check = names.check or names.llm
    same = "" if names.check and names.check != names.llm else " (тот же, что llm)"
    lines = [f"контролёр кадров и клипов (VF_CHECK_PROVIDER): {check}{same}"]
    # second check: against the description, batched via the llm; plus a reference
    # search for off-topic frames (`features/scene_reference_image`)
    if not frame_review_enabled():
        review = "выключена (VF_FRAME_REVIEW=off)"
    elif getattr(build_llm(names.llm), "reviews_frames", False):
        review = f"пачками по 20 кадров через llm {names.llm}"
    else:
        review = f"НЕТ — llm {names.llm} не смотрит кадры пачкой"
    lines.append(f"проверка кадров по описанию (VF_FRAME_REVIEW): {review}")
    lines.append(f"поиск референса к кадру «не про то» (VF_SEARCH_PROVIDER): {names.search}")
    if key := REQUIRED_KEYS.get(check):
        lines.append(f"  ключ {key}: {'задан' if has_key(key) else 'НЕ задан'}")
    if check == "claude_cli":
        lines.append(
            "  ! claude_cli картинки в продакшене не смотрит: кадры и клипы никто не "
            "проверит — задай VF_CHECK_PROVIDER=gemini (flash-lite на бесплатном тире)"
        )
    if check == "gemini":
        from videoforge.providers.gemini import GeminiLlm, rpm_limit

        llm = GeminiLlm()
        lines.append(
            f"  модель {llm.model} (GEMINI_TEXT_MODEL), темп не выше {rpm_limit()} "
            f"вызовов/мин (VF_GEMINI_RPM), повтор на 429/503"
        )
        if not llm.price_known:
            lines.append(
                f"  ! цена модели {llm.model} неизвестна: расходы контролёра пишутся как "
                f"$0 — добавь её в TEXT_PRICES (providers/gemini.py)"
            )
    if not vision_check_enabled():
        lines.append(
            "  ! VF_VISION_CHECK=off: проверки кадров и клипов выключены, решает человек на воротах"
        )
    return lines


@app.command()
def doctor() -> None:
    """Check the environment: ffmpeg, keys, SDKs, resources, projects."""
    from videoforge.core.ffmpeg import FfmpegError, ffmpeg_exe

    typer.echo(f"videoforge {__version__}")
    try:
        typer.echo(f"ffmpeg: {ffmpeg_exe()}")
        from videoforge.core.ffmpeg import available_filters

        # editing relies on these filters; another ffmpeg build in PATH may lack
        # libass (no captions), so it is better to find out here
        need = ("ass", "sidechaincompress", "loudnorm", "ebur128",
                "blackdetect", "freezedetect", "silencedetect")  # fmt: skip
        absent = [f for f in need if f not in available_filters()]
        typer.echo(f"ffmpeg-фильтры монтажа: {'ok' if not absent else 'НЕТ ' + ', '.join(absent)}")
    except FfmpegError as exc:
        typer.echo(f"ffmpeg: НЕТ — {exc}")
    from videoforge.paths import fonts_dir

    fonts = sorted(p.name for p in fonts_dir().glob("*.ttf")) if fonts_dir().exists() else []
    typer.echo(f"шрифты субтитров ({fonts_dir()}): {', '.join(fonts) or 'НЕТ'}")
    from videoforge.config import runpod_key_var

    for key in ("GEMINI_API_KEY", "FAL_KEY", runpod_key_var()):
        typer.echo(f"{key}: {'задан' if has_key(key) else 'не задан'}")
    from videoforge.providers.runpod_pod import load_state

    gpu_state = load_state()
    gpu_line = (
        f"под {gpu_state['pod_id']} → {gpu_state['base_url']}"
        if gpu_state
        else "не поднят (videoforge gpu up)"
    )
    typer.echo(f"gpu (ComfyUI на арендованной карте): {gpu_line}")
    for module in ("google.genai", "fal_client", "edge_tts", "PIL", "yt_dlp"):
        try:
            __import__(module)
            typer.echo(f"{module}: ok")
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"{module}: НЕТ — {exc}")
    for extra, dists, missing in extras_status():
        state = "ok" if not missing else f"НЕТ {', '.join(missing)} — uv sync --extra {extra}"
        typer.echo(f"extra {extra} ({', '.join(dists)}): {state}")
    from videoforge.providers.remotion_video import RemotionVideo

    remotion = RemotionVideo()
    problem = remotion.not_ready()
    typer.echo(
        f"remotion: НЕТ — {problem}" if problem else f"remotion: ok ({remotion.project_dir})"
    )
    names = ProviderNames.from_env()
    typer.echo(f"провайдеры по умолчанию: {names.as_dict()}")
    for line in controller_status(names):
        typer.echo(line)
    for line in still_status(names):
        typer.echo(line)
    typer.echo(f"workflows: {list_workflows()}")
    typer.echo(f"ресурсы ({resources_dir()}): {list_resources()}")
    typer.echo(f"проекты ({projects_dir()}): {list_projects() or '—'}")
    from videoforge.intel.analysis import list_analyses
    from videoforge.paths import research_dir

    typer.echo(f"анализы ({research_dir()}): {list_analyses() or '—'}")
    from videoforge.core.accounts import list_accounts, validate_account
    from videoforge.paths import accounts_dir

    accounts = list_accounts()
    typer.echo(f"аккаунты ({accounts_dir()}): {[a.id for a in accounts] or '—'}")
    for acc in accounts:
        for problem in validate_account(acc):
            typer.echo(f"  ! {acc.id}: {problem}")
    from videoforge.paths import ui_dist_dir

    built = (ui_dist_dir() / "index.html").exists()
    ui_state = "фронт собран" if built else "фронт НЕ собран — .\\check.ps1 ui"
    typer.echo(f"интерфейс (videoforge ui): {ui_state}")

    # publishing: report only whether access exists, never values. Each YouTube
    # account has its own token, hence one line per account
    from videoforge.providers.youtube_api import legacy_token_path, token_path

    yt_accounts = [a for a in accounts if a.network == "youtube"]
    if not yt_accounts:
        typer.echo("публикация YouTube: аккаунтов youtube нет — videoforge account new")
    for acc in yt_accounts:
        token = token_path(acc.id)
        yt = (
            "токен есть"
            if token.exists()
            else f"токена нет ({token}) — videoforge account login {acc.id}"
        )
        typer.echo(f"публикация YouTube ({acc.id}): {yt}")
    old = legacy_token_path()
    if old.exists():
        typer.echo(
            f"  ! общий токен {old} из прежней раскладки: чей он, файл не знает — "
            f"перенеси в .secrets/<аккаунт>/ или удали"
        )
    try:
        __import__("playwright")
        playwright = "ok"
    except Exception:  # noqa: BLE001 - not installed, which is fine
        playwright = "нет — uv sync --extra publish (нужен только TikTok/Instagram)"
    typer.echo(f"публикация браузером (playwright): {playwright}")


# ---------------------------------------------------------------- ui


@app.command()
def ui(
    port: int = typer.Option(8765, help="port"),
    host: str = typer.Option("127.0.0.1", help="address; a single-user UI, so localhost"),
    show: bool = typer.Option(False, "--open", help="open in a browser"),
) -> None:
    """UI on top of the files: accounts, projects, episodes, resources, library,
    benchmarks, capabilities, plans and useful links.

    The server has no state of its own; every request reads the same files as
    the CLI. The frontend is served from ui/dist (built with `npm run build` in
    ui/); without it only the API works (`/api/docs`)."""
    import uvicorn

    from videoforge.api.app import create_app, silence_disconnect_noise
    from videoforge.paths import ui_dist_dir

    if sys.stdout is None or sys.stderr is None:
        # pythonw (a scheduled task in the user session, no console window) has no
        # output streams, and uvicorn configures logging via sys.stderr and fails on
        # formatter 'default'. Log to a temp file instead; console python would open
        # a window whose closing kills the UI
        import tempfile

        log = Path(tempfile.gettempdir()) / "videoforge-ui.log"
        stream = log.open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
        typer.echo(f"--- пульт запущен без консоли, вывод здесь: {log}")

    silence_disconnect_noise()  # seeking video in the browser is not a server error
    dist = ui_dist_dir()
    if not (dist / "index.html").exists():
        typer.echo(
            f"фронт не собран ({dist}): работает только API — http://{host}:{port}/api/docs\n"
            "собрать: cd ui && npm install && npm run build"
        )
    url = f"http://{host}:{port}/"
    typer.echo(f"интерфейс: {url}  (Ctrl+C — остановить)")
    if show:
        typer.launch(url)
    uvicorn.run(create_app(ui_dist=dist), host=host, port=port, log_level="warning")


# ---------------------------------------------------------------- feature


@feature_app.command("new")
def feature_new(
    feature_id: str = typer.Argument(help="Latin id: blender_motion_control"),
    title: str = typer.Option(..., help="title in plain language"),
    area: str = typer.Option("other", help="where it lives: video | edit | ui | … (feature areas)"),
    topic: str = typer.Option("", help="topic in the area: thumbnail | voice … (feature areas)"),
    status: str = typer.Option("idea", help="idea | planned | in_progress | done | dropped"),
    summary: str = typer.Option("", help="what it is, explained in a minute"),
    gives: str = typer.Option("", help="what we gain"),
    cost: str = typer.Option("", help="what it affects: time, money, memory, human effort"),
    source: str = typer.Option("", help="where it came from: a conversation, doc, other product"),
) -> None:
    """Record an idea or capability: features/<id>.yaml (commented template)."""
    from videoforge.core.features import create_feature

    try:
        path, problems = create_feature(
            feature_id,
            title=title,
            area=area,
            topic=topic,
            status=status,
            summary=summary,
            gives=gives,
            cost=cost,
            source=source,
        )
    except (ValueError, FileExistsError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"создан {path}")
    for p in problems:
        typer.echo(f"  ! {p}")


@feature_app.command("areas")
def feature_areas() -> None:
    """Entry areas, topics and statuses: the vocabularies for --area, --topic, --status."""
    from videoforge.core.features import AREAS, STATUSES, TOPICS

    typer.echo("области:")
    for key, title in AREAS.items():
        typer.echo(f"  {key:<12} {title}")
    typer.echo("темы (внутри области; карта «Возможности» показывает тему одной карточкой):")
    for key, title in TOPICS.items():
        typer.echo(f"  {key:<12} {title}")
    typer.echo("статусы:")
    for key, title in STATUSES.items():
        typer.echo(f"  {key:<12} {title}")


@feature_app.command("list")
def feature_list(
    status: str | None = typer.Option(None, help="only this status"),
    area: str | None = typer.Option(None, help="only this area"),
) -> None:
    """Entries by status: in progress → planned → ideas → done → dropped."""
    from videoforge.core.features import (
        STATUSES,
        list_features,
        sort_key,
        summary_line,
        validate_feature,
    )
    from videoforge.paths import features_dir

    items = sorted(list_features(), key=sort_key)
    if status:
        items = [f for f in items if f.status == status]
    if area:
        items = [f for f in items if f.area == area]
    if not items:
        typer.echo(f"записей нет ({features_dir()}) — videoforge feature new")
        return
    for key, title in STATUSES.items():
        group = [f for f in items if f.status == key]
        if not group:
            continue
        typer.echo(f"{title} ({len(group)})")
        for f in group:
            typer.echo("  " + summary_line(f))
            for problem in validate_feature(f):
                typer.echo(f"    ! {problem}")


@feature_app.command("show")
def feature_show(feature_id: str) -> None:
    """Show the full entry."""
    from videoforge.core.features import dump_yaml, load_feature, validate_feature

    try:
        feat = load_feature(feature_id)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    typer.echo(dump_yaml(feat))
    for problem in validate_feature(feat):
        typer.echo(f"! {problem}")


@feature_app.command("status")
def feature_status(
    feature_id: str = typer.Argument(help="entry id"),
    status: str = typer.Argument(help="idea | planned | in_progress | done | dropped"),
) -> None:
    """Change the status without touching the rest of the file. done sets done_at
    and requires how_to_use; dropped requires a reason in notes."""
    from videoforge.core.features import set_status

    try:
        path, problems = set_status(feature_id, status)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"{path}: status → {status}")
    for p in problems:
        typer.echo(f"  ! {p}")


# ---------------------------------------------------------------- link


@link_app.command("new")
def link_new(
    link_id: str = typer.Argument(help="Latin id: some_service"),
    title: str = typer.Option(..., help="name in plain language"),
    url: str = typer.Option(..., help="full link, with http(s)"),
    kind: str = typer.Option("other", help="service | tool | channel | article | docs | assets"),
    area: str = typer.Option("other", help="where it may help (videoforge link kinds)"),
    summary: str = typer.Option("", help="what it is, explained in a minute"),
    why: str = typer.Option("", help="why we care: where it helps our work"),
    cost: str = typer.Option("", help="price and terms"),
    source: str = typer.Option("", help="where it came from: a conversation, channel, article"),
) -> None:
    """Record a useful source: links/<id>.yaml (commented template)."""
    from videoforge.core.links import create_link

    try:
        path, problems = create_link(
            link_id,
            title=title,
            url=url,
            kind=kind,
            area=area,
            summary=summary,
            why=why,
            cost=cost,
            source=source,
        )
    except (ValueError, FileExistsError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"создан {path}")
    for p in problems:
        typer.echo(f"  ! {p}")


@link_app.command("kinds")
def link_kinds() -> None:
    """Source kinds and areas: the vocabularies for --kind and --area."""
    from videoforge.core.features import AREAS
    from videoforge.core.links import KINDS

    typer.echo("виды:")
    for key, title in KINDS.items():
        typer.echo(f"  {key:<12} {title}")
    typer.echo("области:")
    for key, title in AREAS.items():
        typer.echo(f"  {key:<12} {title}")


@todo_app.callback(invoke_without_command=True)
def todo_list(ctx: typer.Context) -> None:
    """Human tasks: things to do by hand. Without a subcommand, lists them.

    One file for everything (`todo.md` at the root), not per project: what gets
    forgotten is what lives where you do not look. A task code (`T-07`) stays
    with the task for good and is never reused, so tasks can be named by code."""
    if ctx.invoked_subcommand:
        return
    from videoforge.core.todo import load_tasks, todo_path

    tasks = load_tasks()
    if not tasks:
        typer.echo(f"задач нет ({todo_path()}) — завести: videoforge todo add «текст»")
        return
    for t in [t for t in tasks if not t.done]:
        tag = f"  [{t.tag}]" if t.tag else ""
        link = f"  → features/{t.feature}" if t.feature else ""
        typer.echo(f"  {t.code}  {t.text}{tag}{link}")
    done = [t for t in tasks if t.done]
    typer.echo(f"\nждут: {len(tasks) - len(done)} · сделано: {len(done)} · {todo_path()}")


@todo_app.command("add")
def todo_add(
    text: str = typer.Argument(help="what to do, in one sentence"),
    tag: str = typer.Option("", "--tag", help="project or area: podcast_clips, publish"),
    feature: str = typer.Option(
        "",
        "--feature",
        help="features/ entry id whose last step this is: done will remind you to close it",
    ),
) -> None:
    """Add a task. The code is assigned automatically."""
    from videoforge.core.todo import add_task

    try:
        task = add_task(text, tag=tag, feature=feature)
    except ValueError as exc:  # unknown entry id: a typo is costlier to catch at done
        _fail(str(exc))
        return
    link = f"  → features/{task.feature}" if task.feature else ""
    typer.echo(f"{task.code}  {task.text}{link}")


@todo_app.command("done")
def todo_done(
    code: str = typer.Argument(help="task code: T-07 or just 7"),
    undo: bool = typer.Option(False, "--undo", help="reopen instead"),
) -> None:
    """Close a task: it moves to done but stays in the file.

    A task linked to `features/<id>` names the entry that was waiting for this
    step; a human decides whether to close it or note what remains. The
    command does not edit features/."""
    from videoforge.core.todo import close_task, feature_hint

    task = close_task(code, done=not undo)
    if task is None:
        _fail(f"задачи {code} нет — videoforge todo покажет список")
        return
    typer.echo(f"{task.code}  {'сделано' if task.done else 'снова в работе'}: {task.text}")
    if hint := feature_hint(task):
        typer.echo(hint)


@link_app.command("list")
def link_list(
    kind: str | None = typer.Option(None, help="only this kind"),
    area: str | None = typer.Option(None, help="only this area"),
) -> None:
    """Sources by kind: services → tools → channels → articles → …"""
    from videoforge.core.links import KINDS, list_links, sort_key, summary_line, validate_link
    from videoforge.paths import links_dir

    items = sorted(list_links(), key=sort_key)
    if kind:
        items = [link for link in items if link.kind == kind]
    if area:
        items = [link for link in items if link.area == area]
    if not items:
        typer.echo(f"источников нет ({links_dir()}) — videoforge link new")
        return
    for key, title in KINDS.items():
        group = [link for link in items if link.kind == key]
        if not group:
            continue
        typer.echo(f"{title} ({len(group)})")
        for link in group:
            typer.echo("  " + summary_line(link))
            for problem in validate_link(link):
                typer.echo(f"    ! {problem}")


@link_app.command("show")
def link_show(link_id: str) -> None:
    """Show the full entry."""
    from videoforge.core.links import dump_yaml, load_link, validate_link

    try:
        link = load_link(link_id)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    typer.echo(dump_yaml(link))
    for problem in validate_link(link):
        typer.echo(f"! {problem}")


# ---------------------------------------------------------------- account


@account_app.command("new")
def account_new(
    account_id: str = typer.Argument(help="Latin id: doc_channel_youtube"),
    network: str = typer.Option(..., help="youtube | tiktok | instagram"),
    project: str = typer.Option(
        "", help="id of the project in projects/ whose content it publishes; empty = none yet"
    ),
    handle: str = typer.Option("", help="@handle on the platform"),
    url: str = typer.Option("", help="channel/profile link"),
    channel_id: str = typer.Option("", help="technical id (UC… on YouTube)"),
    title: str = typer.Option("", help="channel title as viewers see it"),
) -> None:
    """Add an account: accounts/<id>.yaml (commented template) + a snapshots folder."""
    from videoforge.core.accounts import create_account

    try:
        path, problems = create_account(
            account_id,
            network=network,
            project=project,
            handle=handle,
            url=url,
            channel_id=channel_id,
            title=title,
        )
    except (ValueError, FileExistsError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"создан {path}")
    for p in problems:
        typer.echo(f"  ! {p}")
    typer.echo(f"дальше: videoforge account sync {account_id}")


@account_app.command("list")
def account_list() -> None:
    """Accounts by network, with the latest snapshot."""
    from videoforge.core.accounts import NETWORKS, list_accounts, summary_line, validate_account
    from videoforge.paths import accounts_dir

    accounts = list_accounts()
    if not accounts:
        typer.echo(f"аккаунтов нет ({accounts_dir()}) — videoforge account new")
        return
    projects = list_projects()
    for network, info in NETWORKS.items():
        group = [a for a in accounts if a.network == network]
        if not group:
            continue
        typer.echo(info.title)
        for acc in group:
            typer.echo("  " + summary_line(acc))
            for problem in validate_account(acc, projects):
                typer.echo(f"    ! {problem}")


@account_app.command("show")
def account_show(account_id: str) -> None:
    """Full account: settings, snapshot history, number of listed videos."""
    from videoforge.core.accounts import (
        dump_yaml,
        load_account,
        load_history,
        load_videos,
        validate_account,
    )

    try:
        acc = load_account(account_id)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    typer.echo(dump_yaml(acc))
    for problem in validate_account(acc):
        typer.echo(f"! {problem}")
    history = load_history(acc)
    typer.echo(f"\nзамеров: {len(history)}")
    for s in history[-10:]:
        fields = {
            "подписчиков": s.subscribers,
            "просмотров": s.views_total,
            "лайков": s.likes_total,
            "роликов": s.video_count,
        }
        shown = " · ".join(
            f"{k} {v:,}".replace(",", " ") for k, v in fields.items() if v is not None
        )
        typer.echo(f"  {s.at:%Y-%m-%d %H:%M} [{s.source}] {shown or '—'}")
    videos = load_videos(acc)
    if videos:
        typer.echo(
            f"роликов в списке: {len(videos.videos)} (собрано {videos.collected_at:%Y-%m-%d})"
        )


@account_app.command("sync")
def account_sync(
    account_id: str = typer.Argument(help="account id"),
    manual: list[str] = typer.Option(
        [],
        "--manual",
        help='manual snapshot (TikTok, Instagram): --manual "subscribers=1200 views_total=34000"',
    ),
    note: str = typer.Option("", help="note for a manual snapshot"),
    collector: str = typer.Option("ytdlp", help="ytdlp | mock"),
    limit: int = typer.Option(
        0, help="collect only the latest N videos: faster, but no view and like totals"
    ),
) -> None:
    """Take a stats snapshot: YouTube via yt-dlp ($0) and, with a token, the Analytics
    API (source youtube_api, daily series in analytics.jsonl); other networks: --manual."""
    from videoforge.core.accounts import add_snapshot, load_account, manual_snapshot, parse_manual
    from videoforge.intel.account_sync import sync_account
    from videoforge.intel.collectors.base import CollectorError

    try:
        acc = load_account(account_id)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    api_lines: list[str] = []  # Analytics API snapshot, printed after the collector line
    try:
        if manual:
            snapshot = manual_snapshot(parse_manual(manual), note=note)
            path = add_snapshot(acc, snapshot)
        else:
            snapshot = sync_account(acc, collector=collector, limit=limit, log_fn=api_lines.append)
            path = acc.folder() / "stats.jsonl"
    except (ValueError, CollectorError) as exc:
        _fail(str(exc))
        return

    def _n(value: int | None) -> str:
        return "?" if value is None else f"{value:,}".replace(",", " ")

    typer.echo(
        f"записан замер в {path}: подписчиков {_n(snapshot.subscribers)}"
        f" · просмотров {_n(snapshot.views_total)} · роликов {_n(snapshot.video_count)}"
    )
    for line in api_lines:
        typer.echo(line)


def _video_ids(acc: Any, given: list[str]) -> list[str]:
    """Videos for the command: those given, or the whole list from the last sync."""
    from videoforge.core.accounts import load_videos

    if given:
        return given
    videos = load_videos(acc)
    return [v.id for v in videos.videos] if videos else []


@account_app.command("studio")
def account_studio(
    account_id: str = typer.Argument(help="account id"),
    video_ids: list[str] = typer.Argument(None, help="video ids (default: all from sync)"),
    headless: bool = typer.Option(False, "--headless", help="no window (Google may block it)"),
) -> None:
    """Read YouTube Studio in a browser: impressions and thumbnail CTR, not in the API.

    Opens each video's analytics in the account profile (sign in once with
    `account login <id> --studio`), captures screenshots and tab text, and saves
    the numbers to `accounts/<id>/video_stats/<video>.studio.json`. Read-only."""
    from videoforge.core.accounts import load_account, save_video_stats, video_stats_dir
    from videoforge.providers.youtube_studio import read_videos

    try:
        acc = load_account(account_id)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    ids = _video_ids(acc, video_ids or [])
    if not ids:
        _fail(f"роликов нет — сначала videoforge account sync {acc.id}")
        return
    try:
        read = read_videos(acc.id, ids, shots_dir=video_stats_dir(acc), headless=headless)
    except ProviderError as exc:
        _fail(str(exc))
        return
    for stats in read:
        path = save_video_stats(acc, stats)
        ctr = "?" if stats.ctr_pct is None else f"{stats.ctr_pct:.1f}%"
        shown = "?" if stats.impressions is None else str(stats.impressions)
        typer.echo(f"{stats.video_id}: показов {shown} · CTR {ctr} → {path}")


@account_app.command("video")
def account_video(
    account_id: str = typer.Argument(help="account id"),
    video_id: str = typer.Argument("", help="video id (default: the latest)"),
) -> None:
    """Breakdown of one video: where viewers came from, where they leave, and what
    is in the video at those seconds.

    Reads `video_stats/` (filled by `account sync` and `account studio`), maps
    the retention curve to seconds, and for the steepest drops prints the speech
    and scenes from the source episode's edit sheet."""
    from videoforge.core.accounts import (
        load_account,
        load_studio_stats,
        load_video_stats,
        load_videos,
    )
    from videoforge.core.retention import HOOK_SEC, drops, episode_for_video, watching_at

    try:
        acc = load_account(account_id)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    videos = load_videos(acc)
    listed = {v.id: v for v in (videos.videos if videos else [])}
    vid = video_id or next(iter(listed), "")
    if not vid:
        _fail(f"роликов нет — сначала videoforge account sync {acc.id}")
        return
    meta = listed.get(vid)
    stats = load_video_stats(acc, vid)
    studio = load_studio_stats(acc, vid)
    duration = (meta.duration_sec if meta else None) or 0.0
    typer.echo(f"{vid} · {meta.title if meta else ''} · {duration:.0f} с")
    if studio:
        ctr = "?" if studio.ctr_pct is None else f"{studio.ctr_pct:.1f}%"
        typer.echo(
            f"Studio ({studio.at:%Y-%m-%d %H:%M}): показов {studio.impressions or '?'} · CTR {ctr}"
            f" · просмотров {studio.views or '?'}"
        )
    else:
        typer.echo(f"Studio: не читали — videoforge account studio {acc.id} {vid}")
    if stats is None:
        typer.echo(f"API: снимка нет — videoforge account sync {acc.id}")
        return
    hold = "?" if stats.avg_view_pct is None else f"{stats.avg_view_pct:.1f}%"
    typer.echo(
        f"API ({stats.at:%Y-%m-%d}, с запаздыванием ~3 дня): просмотров {stats.views}"
        f" · в среднем {stats.avg_view_sec or 0:.0f} с ({hold}) · лайков {stats.likes}"
        f" · подписок +{stats.subs_gained}"
    )
    if stats.traffic:
        total = sum(t.views for t in stats.traffic.values()) or 1
        parts = sorted(stats.traffic.items(), key=lambda kv: -kv[1].views)
        typer.echo(
            "откуда: "
            + " · ".join(f"{k} {t.views} ({t.views * 100 // total}%)" for k, t in parts if t.views)
        )
    curve = stats.retention
    if not curve or not duration:
        typer.echo("кривой удержания ещё нет: площадка считает её, когда просмотров наберётся")
        return
    marks = sorted(
        [(f"{HOOK_SEC:.0f} с", HOOK_SEC / duration)]
        + [(f"{q}%", q / 100) for q in (25, 50, 75, 95)],
        key=lambda m: m[1],
    )
    typer.echo(
        "досмотрели до: "
        + " · ".join(f"{name} {watching_at(curve, r) * 100:.0f}%" for name, r in marks)  # type: ignore[operator]
    )
    ref = episode_for_video(vid)
    speech: list[dict[str, Any]] = []
    if ref:
        from videoforge.api.montage import episode_montage

        pid, ep = ref.split("/", 1)
        try:
            tracks = {t["id"]: t for t in episode_montage(pid, ep)["tracks"]}
            speech = list((tracks.get("speech") or tracks.get("captions") or {}).get("items") or [])
        except LookupError:
            speech = []
    typer.echo(f"\nгде уходят (эпизод {ref or 'не найден'}):")
    for d in drops(curve, duration):
        typer.echo(f"  {d.start_sec:6.0f}–{d.end_sec:.0f} с  −{d.lost * 100:.0f}% зрителей")
        said = " ".join(
            str(i.get("text") or "") for i in speech if d.start_sec - 3 <= i["start"] < d.end_sec
        )
        if said:
            typer.echo(f"         «{said[:160]}»")
        if ref:
            typer.echo(
                f"         videoforge montage {ref} --from {max(0, d.start_sec - 5):.0f}"
                f" --to {d.end_sec:.0f}"
            )


@account_app.command("login")
def account_login(
    account_id: str = typer.Argument(help="account id"),
    url: str = typer.Option("", help="open another address (default: the upload form)"),
    client_json: Path | None = typer.Option(
        None,
        "--client-json",
        help="YouTube: OAuth client file from the Google console (Clients → Desktop app)",
    ),
    client_id: str = typer.Option("", "--client-id", help="YouTube: if the file is not at hand"),
    client_secret: str = typer.Option("", "--client-secret", help="YouTube: likewise"),
    force: bool = typer.Option(False, "--force", help="YouTube: sign in again over the token"),
    studio: bool = typer.Option(
        False,
        "--studio",
        help="YouTube: sign in to Studio in a browser, for impressions and CTR (account studio)",
    ),
) -> None:
    """Sign in to an account by hand, once, so publishing works.

    **YouTube** uses OAuth: the command opens a browser, catches Google's reply
    on `127.0.0.1`, and stores a refresh token in
    `.secrets/<id>/youtube_token.json` (one per account). Needs the
    `client_id` and `client_secret` of a Desktop app client; easiest is to pass
    the downloaded file via `--client-json`. Re-login (`--force`) reuses the
    client from the token.

    It requests two scopes: `youtube.upload` and `youtube.force-ssl` (to set
    visibility and covers from the command line). Both must be enabled in the
    Google console (Data Access); see docs/PUBLISHING.md.

    **TikTok and Instagram** sign in via a browser: opens Chromium with a
    persistent profile `accounts/<id>/.profile/<network>/`. Log in manually
    and close the window; `videoforge publish … --yes` then reuses the session.
    Publishes nothing."""
    from videoforge.core.accounts import load_account
    from videoforge.providers.browser_publish import open_session, profile_dir

    try:
        acc = load_account(account_id)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    if studio:
        from videoforge.providers.youtube_studio import open_login

        if acc.network != "youtube":
            _fail("--studio — только для YouTube")
            return
        typer.echo("открываю Studio в Chrome: войди в Google и закрой окно")
        try:
            saved_profile = open_login(acc.id)
        except ProviderError as exc:
            _fail(str(exc))
            return
        typer.echo(f"профиль сохранён: {saved_profile}")
        return
    if acc.network == "youtube":
        from videoforge.providers.youtube_api import (
            authorize,
            client_from_json,
            known_client,
            token_path,
        )

        path = token_path(acc.id)
        if path.exists() and not force:
            typer.echo(f"токен уже есть: {path} (войти заново — --force)")
            return
        if client_json is not None:
            try:
                client_id, client_secret = client_from_json(client_json)
            except ProviderError as exc:
                _fail(str(exc))
                return
        # .env is the usual place for keys in this project, and simpler than
        # locating the downloaded file
        client_id = client_id or os.environ.get("YOUTUBE_CLIENT_ID", "")
        client_secret = client_secret or os.environ.get("YOUTUBE_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            # a re-login or a second channel uses the same OAuth client, which is
            # already stored in some token; no need to hunt for the file again
            found = known_client(prefer=acc.id)
            if found is not None:
                client_id, client_secret, source = found
                typer.echo(f"клиент OAuth беру из {source}")
        if not client_id or not client_secret:
            _fail(
                "нужны client_id и client_secret: YOUTUBE_CLIENT_ID и "
                "YOUTUBE_CLIENT_SECRET в .env, --client-json <файл из консоли Google> "
                "или --client-id/--client-secret (docs/PUBLISHING.md)"
            )
            return
        try:
            saved = authorize(client_id, client_secret, account_id=acc.id, log_fn=typer.echo)
        except ProviderError as exc:
            _fail(str(exc))
            return
        typer.echo(f"вход выполнен, токен лежит в {saved}")
        return
    typer.echo(f"профиль: {profile_dir(acc.id, acc.network)}")
    try:
        open_session(acc.id, acc.network, url=url)
    except ProviderError as exc:
        _fail(str(exc))
        return
    typer.echo("окно закрыто, сессия сохранена в профиле")


def _lib_ctx(pairs: list[str]) -> dict[str, object]:
    """Conditions from the command line: --when format=anime_edit --when has_speech=true."""
    from videoforge.core.library import CONDITION_KEYS

    ctx: dict[str, object] = {}
    for item in pairs:
        key, _, value = item.partition("=")
        key = key.strip()
        if key not in CONDITION_KEYS:
            _fail(f"условие '{key}' вне словаря; есть: {', '.join(CONDITION_KEYS)}")
            raise typer.Exit(1)
        ctx[key] = _lib_value(value.strip())
    return ctx


def _lib_value(text: str) -> object:
    low = text.lower()
    if low in ("true", "да"):
        return True
    if low in ("false", "нет"):
        return False
    return text


@lib_app.command("decisions")
def lib_decisions() -> None:
    """Decision points and the condition vocabulary: what there is to choose from."""
    from videoforge.core.library import CONDITION_KEYS, DECISIONS, load_all

    counts: dict[str, int] = {}
    for t in load_all():
        counts[t.decision] = counts.get(t.decision, 0) + 1
    typer.echo("точки решения:")
    for key, title in DECISIONS.items():
        typer.echo(f"  {key:<16} {title}  ({counts.get(key, 0)})")
    typer.echo("\nусловия:")
    for key, hint in CONDITION_KEYS.items():
        typer.echo(f"  {key:<16} {hint}")


@lib_app.command("list")
def lib_list(
    decision: str | None = typer.Argument(None, help="decision point; without it, all"),
    when: list[str] = typer.Option(
        [], "--when", help="video condition: format=anime_edit, has_speech=true"
    ),
    all_: bool = typer.Option(False, "--all", help="also show non-matching ones"),
) -> None:
    """What can be applied. Matching techniques come first; unimplemented are marked."""
    from videoforge.core.library import DECISIONS, for_decision, load_all

    ctx = _lib_ctx(when)
    keys = [decision] if decision else sorted({t.decision for t in load_all()})
    if decision and decision not in DECISIONS:
        _fail("нет такой точки решения; см. videoforge lib decisions")
        return
    shown = 0
    for key in keys:
        rows = for_decision(key, ctx)
        if not all_:
            rows = [r for r in rows if r[1] != "не подходит"]
        if not rows:
            continue
        typer.echo(f"\n{key} — {DECISIONS.get(key, key)}")
        for t, verdict, unknown in rows:
            mark = "✔" if t.status == "implemented" else "?"
            tail = f"  [{verdict}" + (f": {', '.join(unknown)}" if unknown else "") + "]"
            typer.echo(f"  {mark} {t.id:<22} {t.title}{tail if verdict != 'подходит' else ''}")
            if t.summary:
                typer.echo(f"      {t.summary}")
            shown += 1
    if not shown:
        typer.echo("подходящих приёмов нет — videoforge lib list --all")


@lib_app.command("show")
def lib_show(technique_id: str) -> None:
    """Show a technique in full and validate it against the library rules."""
    from videoforge.core.library import load_all, validate_technique

    found = [t for t in load_all() if t.id == technique_id]
    if not found:
        _fail(f"приёма '{technique_id}' нет; videoforge lib list")
        return
    t = found[0]
    typer.echo(yaml.safe_dump(t.model_dump(), allow_unicode=True, sort_keys=False).rstrip())
    problems = validate_technique(t)
    if problems:
        typer.echo("\nпроблемы:")
        for p in problems:
            typer.echo(f"  - {p}")


@lib_app.command("new")
def lib_new(
    decision: str = typer.Argument(help="decision point; see videoforge lib decisions"),
    technique_id: str = typer.Argument(help="Latin id: fade_out_soft"),
    title: str = typer.Option("", help="human-readable name"),
) -> None:
    """Create a technique from a commented template."""
    from videoforge.core.library import DECISIONS
    from videoforge.core.templates import render
    from videoforge.paths import library_dir

    if decision not in DECISIONS:
        _fail("нет такой точки решения; см. videoforge lib decisions")
        return
    path = library_dir() / decision / f"{technique_id}.yaml"
    if path.exists():
        _fail(f"уже есть: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render(
            "technique.yaml",
            {"id": technique_id, "decision": decision, "title": title or technique_id},
        ),
        encoding="utf-8",
    )
    typer.echo(f"создан {path}")
    typer.echo("заполни условия и evidence, потом: videoforge lib check")


@lib_app.command("example")
def lib_example(
    technique_id: str = typer.Argument(help="technique id"),
    after: Path = typer.Option(
        ..., "--after", help="the 'after' file: png/jpg/webp, mp4/webm, wav/mp3"
    ),
    before: Path | None = typer.Option(
        None, "--before", help="the 'before' file: the same frame or span without the technique"
    ),
) -> None:
    """Store a before → after example in library/<decision>/<id>/, used to pick
    the technique by eye in the UI; replaces the previous example."""
    from videoforge.core.library import add_example, load_all

    found = [t for t in load_all() if t.id == technique_id]
    if not found:
        _fail(f"приёма '{technique_id}' нет; videoforge lib list")
        return
    try:
        ex = add_example(found[0], after, before)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"пример ({ex.kind}): {ex.after}" + (f" · было: {ex.before}" if ex.before else ""))


@lib_app.command("render-example")
def lib_render_example(
    technique_id: str | None = typer.Argument(
        None, help="technique id; without it, all with a recipe"
    ),
    all_: bool = typer.Option(False, "--all", help="all techniques that have a recipe"),
) -> None:
    """Render a before → after example from a recipe in core/library_examples and
    store it next to the technique. A technique without a recipe or material is
    reported rather than raised, so the other examples still get built."""
    import tempfile

    from videoforge.core.library import add_example, load_all
    from videoforge.core.library_examples import RECIPES, MissingMaterial, render

    by_id = {t.id: t for t in load_all()}
    if all_:
        ids = [i for i in RECIPES if i in by_id]
    elif technique_id:
        ids = [technique_id]
    else:
        _fail("укажи id приёма или --all; рецепты есть у: " + ", ".join(RECIPES))
        return
    problems: list[str] = []
    for tid in ids:
        t = by_id.get(tid)
        if t is None:
            problems.append(f"{tid}: приёма нет в библиотеке")
            continue
        if tid not in RECIPES:
            problems.append(f"{tid}: рецепта нет — положить руками: videoforge lib example {tid} …")
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="vf_example_") as tmp:
                rendered = render(tid, Path(tmp))
                ex = add_example(t, rendered.after, rendered.before)
        except MissingMaterial as exc:
            problems.append(f"{tid}: нет материала — {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one failed recipe must not stop --all
            problems.append(f"{tid}: рецепт упал — {exc}")
            continue
        typer.echo(
            f"{tid}: {ex.kind}, {ex.after.name}" + (f" + {ex.before.name}" if ex.before else "")
        )
    if problems:
        typer.echo("\nне собрано:")
        for p in problems:
            typer.echo(f"  - {p}")
        raise typer.Exit(1)


@lib_app.command("check")
def lib_check() -> None:
    """Validate the whole library: conditions, evidence, decision points."""
    from videoforge.core.library import load_all, validate_technique

    items = load_all()
    bad = 0
    for t in items:
        problems = validate_technique(t)
        if problems:
            bad += 1
            typer.echo(f"{t.decision}/{t.id}:")
            for p in problems:
                typer.echo(f"  - {p}")
    typer.echo(f"\nприёмов {len(items)}, с проблемами {bad}")
    if bad:
        raise typer.Exit(1)


@bench_app.command("new")
def bench_new(
    bench_id: str = typer.Argument(help="Latin id: cat_i2v_screen"),
    question: str = typer.Option("", help="the benchmark question in one sentence"),
) -> None:
    """Create a benchmark from a commented template (bench.yaml + inputs/)."""
    from datetime import date

    from videoforge.bench.spec import INPUTS_DIR, SPEC_FILE, BenchError, bench_path
    from videoforge.core.templates import render, yaml_str

    try:
        folder = bench_path(bench_id)
    except BenchError as exc:
        _fail(str(exc))
        return
    path = folder / SPEC_FILE
    if path.exists():
        _fail(f"уже есть: {path}")
        return
    (folder / INPUTS_DIR).mkdir(parents=True, exist_ok=True)
    path.write_text(
        render(
            "bench.yaml",
            {
                "id": bench_id,
                "question": yaml_str(question or "что сравниваем и зачем"),
                "date": date.today().isoformat(),
            },
        ),
        encoding="utf-8",
    )
    typer.echo(f"создан {path}")
    typer.echo(f"положи стартовые кадры в {folder / INPUTS_DIR}")
    typer.echo(f"потом: videoforge bench run {bench_id}")


@bench_app.command("run")
def bench_run(
    bench_id: str = typer.Argument(help="benchmark id: projects/_bench/<id>/bench.yaml"),
    scene: list[str] | None = typer.Option(None, "--scene", help="only these scenes"),
    branch: list[str] | None = typer.Option(None, "--branch", help="only these branches"),
    seed: list[int] | None = typer.Option(None, "--seed", help="only these seeds"),
    mock: bool = typer.Option(False, "--mock", help="GPU stub: schema and files, $0"),
    dry: bool = typer.Option(False, "--dry", help="write plans and forecast, run nothing"),
    force: bool = typer.Option(False, "--force", help="re-run finished takes too"),
) -> None:
    """Run the scene × branch × seed matrix. A take with the same fingerprint is
    not re-run; stops on budget (checked before each call) or a branch error."""
    from videoforge.bench.run import run_bench
    from videoforge.bench.spec import BenchError, bench_path
    from videoforge.bench.summary import write_summary

    try:
        folder = bench_path(bench_id)
        report = run_bench(
            folder,
            scenes=scene or None,
            branches=branch or None,
            seeds=seed or None,
            mock=mock,
            dry=dry,
            force=force,
            say=typer.echo,
        )
    except BenchError as exc:
        _fail(str(exc))
        return
    typer.echo(report.line())
    if report.done or report.failed:
        path, _ = write_summary(folder)
        typer.echo(f"сводка: {path}")
    typer.echo(f"оценка глазами: пульт → Замеры → {bench_id}")


@bench_app.command("summary")
def bench_summary(
    bench_id: str = typer.Argument(help="benchmark id"),
) -> None:
    """Numeric summary: acceptance rate, cost per accepted second, defects by
    criterion, automatic metrics vs human verdicts → summary.md."""
    from videoforge.bench.spec import BenchError, bench_path
    from videoforge.bench.summary import write_summary

    try:
        path, _ = write_summary(bench_path(bench_id))
    except BenchError as exc:
        _fail(str(exc))
        return
    typer.echo(path.read_text(encoding="utf-8"))


@bench_app.command("list")
def bench_list() -> None:
    """Benchmarks: question, number of takes, spent vs budget."""
    from videoforge.bench.spec import BenchError, list_benches, load_spec, load_takes

    folders = list_benches()
    if not folders:
        typer.echo("замеров нет: videoforge bench new <id> --question «…»")
        return
    for folder in folders:
        try:
            spec = load_spec(folder)
        except BenchError as exc:
            typer.echo(f"{folder.name:24} схема негодна: {exc}")
            continue
        takes = load_takes(folder, spec)
        spent = sum(t.cost_usd for t in takes)
        typer.echo(
            f"{spec.id:24} {spec.question} · дублей {len(takes)} · "
            f"${spent:.3f} из ${spec.budget_usd:.2f}"
        )


@lib_app.command("index")
def lib_index() -> None:
    """Rebuild library/index.md: what we can do vs what we have only seen."""
    from videoforge.core.library import write_index

    typer.echo(f"записан {write_index()}")


if __name__ == "__main__":
    app()
