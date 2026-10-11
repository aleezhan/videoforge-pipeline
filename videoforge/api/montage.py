"""Episode montage view: timeline tracks for the dashboard.

Why. Everything about a finished video was already available as numbers: how many
lines, which track, where the effects are (`montage.json`, `timeline.json`,
`edit_plan.json`, `captions.ass`, timing `.srt` files). But the question a person actually
asks is different: **at this second, does what is on screen match what is heard?**
Answering it meant opening four files and keeping the timing in your head. The tracks
answer it in a second: one time scale with scenes, on-screen words, speech, lyrics, the
audio waveform and effects on it.

Like the rest of the dashboard, there is no state of its own: every request reads the same
files the build reads. Nothing is recomputed "as it would have been" - what is on disk is
used: lines are parsed from the FINISHED `captions.ass` (what was drawn is what is shown),
scene timing comes from the measured `timeline.json`, and the waveform from the video
file itself.

Deliberately absent: editing. The dashboard only shows; an offset spotted by eye goes into
the edit plan or the build script, and ffmpeg applies it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from videoforge.api.data import NotFound, file_url, poster_path
from videoforge.core.captions import (
    AssEvent,
    Word,
    lyrics_problems,
    overlay_window,
    parse_ass_events,
    parse_srt_words,
    read_lyrics,
)
from videoforge.core.editplan import EditPlan, load_plan
from videoforge.core.ffmpeg import GAIN_RAMP_SEC, FfmpegError, detect_cuts, frames_at
from videoforge.core.materials import (
    ROLE_TITLES,
    Materials,
    load_materials,
    resolve,
    state,
)
from videoforge.core.pauses import scene_lead_in
from videoforge.core.project import load_project_from_dir
from videoforge.core.resources import KINDS, MusicTrack, Sfx, load_resource, search_dirs
from videoforge.core.storyboard import Scene, Storyboard, load_storyboard
from videoforge.core.waveform import PEAKS_PER_SEC, cached_envelope
from videoforge.paths import EPISODES_DIR, ROOT, episode_dir, project_dir, projects_dir

log = logging.getLogger("videoforge.api")

ASSEMBLE_DIR = "05_assemble"
TIMELINE_FILE = "timeline.json"
MONTAGE_FILE = "montage.json"

OVERLAY_STYLE = "Overlay"  # as `build_ass` names plate styles: Overlay, OverlayLabel, ...
SHORT_WORD_SEC = 0.12  # shorter words cannot be read in time; almost always a timing defect
# A SHARE is counted, not a number. One 0.05 s line out of three is a defect and visible at
# once; two words out of forty in karaoke are just a fast line, and a warning about them
# fired on every video, i.e. meant nothing
SHORT_SHARE = 0.15
SILENT_LEVEL = 0.015  # below this a track counts as silent (RMS 0..1)
# There is deliberately no "word held five times longer than its neighbours" check: it was
# written for an old parser bug (since fixed) and on songs it only flagged long sustained
# notes, which are real, not defects. A check whose only hits are false is not a check.


@dataclass
class Entry:
    """A scene on the finished file's timeline, as measured by the build."""

    scene_id: str
    start: float
    end: float
    lead: float = 0.0

    @property
    def box_start(self) -> float:
        """The second from which the scene is "its own": during a transition the start
        still belongs to the previous scene."""
        return self.start + self.lead


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("не прочитан %s: %s", path, exc)
        return None


def _timeline(edir: Path, sb: Storyboard) -> list[Entry]:
    """The build's measured timeline, or the sum of scene durations if it is missing.

    The difference matters: `timeline.json` is written by the build from ACTUAL clip
    lengths, while the sum of `duration_sec` is the plan. `measured` shows which was used."""
    data = _read_json(edir / ASSEMBLE_DIR / TIMELINE_FILE)
    if isinstance(data, list) and data:
        return [
            Entry(
                str(row.get("scene_id", "")),
                float(row.get("start", 0.0)),
                float(row.get("end", 0.0)),
                float(row.get("lead", 0.0)),
            )
            for row in data
            if isinstance(row, dict)
        ]
    entries: list[Entry] = []
    cursor = 0.0
    for scene in sb.ordered_scenes():
        length = scene.measured_sec.get("clip") or scene.duration_sec
        entries.append(Entry(scene.id, round(cursor, 3), round(cursor + length, 3)))
        cursor += length
    return entries


def _scene_track(edir: Path, sb: Storyboard, entries: list[Entry]) -> dict[str, Any] | None:
    """Scene track: each scene's box with a frame and its narration line.

    No storyboard means no track, even if a `timeline.json` is left over from an earlier
    life of the episode: boxes with no frame and no line show nothing, and shots detected
    from the file itself answer the same question more honestly."""
    if not sb.scenes:
        return None
    by_id: dict[str, Scene] = {s.id: s for s in sb.scenes}
    items: list[dict[str, Any]] = []
    for e in entries:
        scene = by_id.get(e.scene_id)
        thumb = None
        if scene is not None:
            for rel in (*scene.assets.thumbs[:1], scene.assets.image):
                if rel and (edir / rel).exists():
                    thumb = file_url(edir / rel)
                    break
        badges: list[str] = []
        if scene is not None:
            if scene.montage.transition_in and scene.montage.transition_in != "cut":
                badges.append(scene.montage.transition_in)
            if scene.montage.shots > 1:
                badges.append(f"отрезков {scene.montage.shots}")
            if scene.continues:
                badges.append(f"продолжает {scene.continues}")
            if scene.montage.zoom > 1.0:  # the seam between continuations is hidden by a crop
                badges.append(f"кроп ×{scene.montage.zoom:.3f}")
        items.append(
            {
                "start": round(e.start, 3),
                "end": round(e.end, 3),
                "label": e.scene_id,
                "text": scene.narration if scene else "",
                "thumb_url": thumb,
                "badges": badges,
                "clip_url": file_url(edir / scene.assets.clip)
                if scene and scene.assets.clip and (edir / scene.assets.clip).exists()
                else None,
            }
        )
    if not items:
        return None
    return {
        "id": "scenes",
        "kind": "clips",
        "title": "Сцены",
        "icon": "video",
        "hint": "коробка сцены на таймлайне финала — измерена сборкой по длине клипа",
        "items": items,
    }


SHOTS_DIR = "shots"
SHOTS_INDEX = "shots.json"
MAX_SHOT_THUMBS = 60  # the page cannot show the eye more frames than this anyway
MAX_SHOT_TOTAL = 400  # shots per track: an hour-long video has thousands, which is useless


def _shots_track(
    edir: Path, video: Path, duration: float, threshold: float
) -> dict[str, Any] | None:
    """Shots of the video detected from the FILE ITSELF: where the picture changes and
    what is there.

    Needed where there is no storyboard - the video was built by a script or brought in
    finished. Then "what is it made of" can only be answered by looking: the cut detector
    gives shot boundaries, and a frame per shot is what a person wants to see when asking
    "where are the frames".

    The detector threshold depends on the MATERIAL, not on taste: 0.05 for 2D animation,
    where neighbouring frames are nearly identical, 0.3 for live action. So it is a project
    setting (`video.scene_threshold`), not a constant: an anime-edit channel and a podcast
    clips channel need different values, and a shared one is wrong for both. The cache
    remembers the threshold, so changing the setting recomputes the shots."""
    index = edir / ASSEMBLE_DIR / SHOTS_INDEX
    st = video.stat()
    data = _read_json(index)
    cuts: list[float] | None = None
    if (
        isinstance(data, dict)
        and data.get("mtime_ns") == st.st_mtime_ns
        and data.get("threshold") == threshold
    ):
        cuts = [float(t) for t in data.get("cuts") or []]
    if cuts is None:
        try:
            cuts = detect_cuts(video, threshold=threshold)
        except (FfmpegError, OSError) as exc:
            log.warning("планы ролика %s не найдены: %s", video, exc)
            return None
        try:
            index.write_text(
                json.dumps({"mtime_ns": st.st_mtime_ns, "threshold": threshold, "cuts": cuts}),
                encoding="utf-8",
            )
        except OSError:
            pass
    bounds = [0.0, *[c for c in cuts if 0.05 < c < duration - 0.05], duration]
    spans = list(zip(bounds, bounds[1:], strict=False))[:MAX_SHOT_TOTAL]
    folder = edir / ASSEMBLE_DIR / SHOTS_DIR
    # a frame from the MIDDLE of the shot: at the boundary it is often still mid-transition
    mids = [round((a + b) / 2, 3) for a, b in spans[:MAX_SHOT_THUMBS]]
    thumbs: list[Path] = []
    fresh = folder.is_dir() and all(
        p.stat().st_mtime_ns >= st.st_mtime_ns for p in folder.glob("*.jpg")
    )
    if fresh and len(sorted(folder.glob("*.jpg"))) == len(mids):
        thumbs = sorted(folder.glob("*.jpg"))
    else:
        try:
            thumbs = frames_at(video, mids, folder)
        except (FfmpegError, OSError) as exc:
            log.warning("кадры планов %s не сняты: %s", video, exc)
    items: list[dict[str, Any]] = []
    for i, (start, end) in enumerate(spans):
        items.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "label": f"{i + 1}",
                "text": "",
                "thumb_url": file_url(thumbs[i]) if i < len(thumbs) else None,
                "badges": [],
                "clip_url": None,
            }
        )
    if len(items) < 2:
        return None
    return {
        "id": "shots",
        "kind": "clips",
        "title": "Планы",
        "icon": "video",
        "hint": "смены картинки, найденные в готовом файле, и кадр из середины каждого плана",
        "items": items,
    }


def _scene_threshold(pid: str) -> float:
    """Shot detector sensitivity from the project settings.

    Without a project or setting, the global threshold is used: benchmark sandboxes and
    videos brought in finished are shown too, and may have no `project.yaml`."""
    from videoforge.core.ffmpeg import SCENE_THRESHOLD
    from videoforge.core.project import load_project

    try:
        return load_project(pid).video.scene_threshold
    except (FileNotFoundError, OSError, ValueError):
        return SCENE_THRESHOLD


def _ass_files(edir: Path, sb: Storyboard) -> list[Path]:
    """Subtitle files burned into this video.

    The one named by the build (`final.captions`) comes first. Other `.ass` files next to it
    are included too: a hand-built video (an edit to a track) puts its own file next to
    the storyboard, and the dashboard must show the text the person sees on screen, not
    only what went through the pipeline."""
    found: list[Path] = []
    if sb.final.captions and (edir / sb.final.captions).exists():
        found.append(edir / sb.final.captions)
    for folder in (edir, edir / ASSEMBLE_DIR):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.ass")):
            if path not in found:
                found.append(path)
    return found


def _caption_track(events: list[AssEvent]) -> dict[str, Any] | None:
    """On-screen text: lines from the finished ASS, words from karaoke tags."""
    items = [
        {
            "start": ev.start,
            "end": ev.end,
            "text": ev.text,
            "words": [{"start": w.start, "end": w.end, "text": w.text} for w in ev.words],
        }
        for ev in events
    ]
    if not items:
        return None
    return {
        "id": "captions",
        "kind": "lines",
        "title": "На экране",
        "icon": "captions",
        "hint": "разобрано из готового ASS: что нарисовано в ролике, а не что задумано",
        "items": items,
    }


def _overlay_track(
    sb: Storyboard, entries: list[Entry], rendered: list[AssEvent], outro: Outro | None = None
) -> dict[str, Any] | None:
    """Overlay plates (`Scene.overlay`): text over the frame drawn by the build.

    Times come from the storyboard and timeline rather than the ASS: they are the same
    there (checked on a real episode), but here the plate kind is known - takeaway,
    source, label. The plate window (`start_sec`/`end_sec`) is offset from the scene start
    in the SCENE's time, like speech words: the timeline is already stretched at the tail,
    the window is not. Without a storyboard (a hand-built video), the ASS lines themselves
    remain: no kind, but real seconds."""
    by_id = {e.scene_id: e for e in entries}
    items: list[dict[str, Any]] = []
    for scene in sb.ordered_scenes():
        if scene.overlay is None or not scene.overlay.text.strip():
            continue
        e = by_id.get(scene.id)
        if e is None:
            continue
        box_start, box_end = e.box_start, e.end
        if outro is not None:
            box_start, box_end = outro.unstretch(box_start), outro.unstretch(box_end)
        window = overlay_window(box_start, box_end, scene.overlay)
        if window is None:  # window entirely past the scene: the build did not draw it
            continue
        start, end = window
        if outro is not None:
            start, end = outro.stretch(start), outro.stretch(end)
        items.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": scene.overlay.text.strip(),
                "kind": scene.overlay.kind,
                "words": [],
            }
        )
    if not items:
        items = [
            {"start": ev.start, "end": ev.end, "text": ev.text, "kind": ev.style, "words": []}
            for ev in rendered
        ]
    if not items:
        return None
    return {
        "id": "overlays",
        "kind": "lines",
        "title": "Плашки",
        "icon": "type",
        "hint": "Scene.overlay — вывод, источник, ярлык: рисует сборка поверх кадра",
        "items": items,
    }


def _words_items(
    words: list[Word], offset: float = 0.0, within: float | None = None
) -> list[dict[str, Any]]:
    """Words shifted into seconds of the finished file.

    `within` cuts off what is not in the video: a whole song has 288 words, while a
    29-second edit contains thirty. The rest are not just clutter on the track - they
    trigger the too-short-word warnings, so the dashboard would complain about a part of
    the song that never plays in the video."""
    items: list[dict[str, Any]] = [
        {"start": round(w.start + offset, 3), "end": round(w.end + offset, 3), "text": w.text}
        for w in words
    ]
    if within is None:
        return items
    return [i for i in items if i["end"] > 0 and i["start"] < within]


@dataclass(frozen=True)
class Outro:
    """Slowed-down tail: from which second and by what factor slower.

    The only place where final time differs from scene time. The build stretched the
    timeline and captions itself, but `.srt` words were not: they stay in the scene's own
    uncompressed time, and without this conversion the speech track would drift from the
    picture at the tail - exactly where it matters most."""

    from_sec: float
    speed: float

    def stretch(self, t: float) -> float:
        return t if t <= self.from_sec else self.from_sec + (t - self.from_sec) / self.speed

    def unstretch(self, t: float) -> float:
        return t if t <= self.from_sec else self.from_sec + (t - self.from_sec) * self.speed


def _outro(montage: Any, duration: float) -> Outro | None:
    """Tail from `montage.json`: `from_sec` is not stored in the file but derived from the
    final length, the added seconds and the requested tail length."""
    if not isinstance(montage, dict):
        return None
    spec = montage.get("outro")
    added = (montage.get("final") or {}).get("outro_added_sec") or 0.0
    if not isinstance(spec, dict) or not added:
        return None
    take = float(spec.get("take_sec") or 0.0)
    speed = float(spec.get("speed") or 1.0)
    if take <= 0 or not 0 < speed < 1:
        return None
    return Outro(round(duration - added - take, 3), speed)


def _speech_track(
    edir: Path, sb: Storyboard, entries: list[Entry], outro: Outro | None
) -> dict[str, Any] | None:
    """Speech: words with timings, as produced by the voice-over or source transcript.

    This is what is HEARD. Kept separate from on-screen text on purpose: a mismatch between
    "said" and "written" is the most common build error, and it is only visible when both
    tracks sit side by side on one time scale."""
    by_id = {e.scene_id: e for e in entries}
    items: list[dict[str, Any]] = []
    for scene in sb.ordered_scenes():
        rel = scene.assets.captions
        e = by_id.get(scene.id)
        if not rel or e is None or not (edir / rel).exists():
            continue
        words = parse_srt_words((edir / rel).read_text(encoding="utf-8"))
        lead_in = scene_lead_in(scene)  # .srt words start at the voice-over, after the lead-in
        if outro is None:
            items.extend(_words_items(words, e.box_start + lead_in))
            continue
        # the scene start in the timeline is already stretched, the word and lead-in are not:
        # map the scene back to its own time, add lead-in and word, then stretch the result
        base = outro.unstretch(e.box_start) + lead_in
        items.extend(
            {
                "start": round(outro.stretch(base + w.start), 3),
                "end": round(outro.stretch(base + w.end), 3),
                "text": w.text,
            }
            for w in words
        )
    if not items:
        return None
    return {
        "id": "speech",
        "kind": "words",
        "title": "Речь",
        "icon": "mic",
        "hint": "тайминги слов из озвучки (edge-tts) или из транскрипта источника",
        "items": items,
    }


def _music(sb: Storyboard, plan: EditPlan | None, pid: str) -> tuple[MusicTrack | None, float]:
    """The episode's music track and the final-video second at which it starts.

    `start_sec` is negative when the track does not start from its beginning: -50.8 means
    second 50.8 of the track plays at second zero of the video. That is the offset that
    places the lyrics on the timeline.

    There may be no plan at all - an edit to a track is built by a script outside the
    pipeline. Then both the track and the offset come from `final`: the build writes them
    there, and for a hand-made video a person fills them in."""
    track_id = sb.final.music_track or sb.resources.music
    if plan is not None and plan.music is not None:
        if not plan.music.enabled:
            return None, 0.0
        track_id = plan.music.track or track_id
    if not track_id:
        return None, 0.0
    try:
        track = load_resource("music", track_id, MusicTrack, search_dirs(project_dir(pid)))
    except FileNotFoundError:
        log.warning("трек %s не найден — дорожки музыки не будет", track_id)
        return None, 0.0
    # the plan takes precedence on purpose: episodes built before
    # `final.music_start_sec` existed have zero there, and the offset lives only in the plan
    start = (
        plan.music.start_sec
        if plan is not None and plan.music is not None
        else sb.final.music_start_sec
    )
    return track, start


def _bed_id(sb: Storyboard, plan: EditPlan | None, pid: str) -> str | None:
    """The video's background bed: what the build used (`final.bed_track`), otherwise what
    it will use - the project's `audio.bed`; the plan takes precedence, as for music:
    `{track: null}` turns the layer off for this video."""
    track_id = sb.final.bed_track
    if track_id is None:
        try:
            track_id = load_project_from_dir(project_dir(pid)).audio.bed
        except (FileNotFoundError, ValueError):
            track_id = None
    if plan is not None and plan.bed is not None:
        track_id = plan.bed.track
    return track_id


SLOT_TITLES = {
    "character": "Персонаж",
    "style": "Визуальный стиль",
    "voice": "Голос",
    "niche": "Ниша",
    "caption_style": "Стиль субтитров",
    "music": "Музыка",
}
SLOT_KINDS = {
    "character": "characters",
    "style": "styles",
    "voice": "voices",
    "niche": "niches",
    "caption_style": "caption_styles",
    "music": "music",
}
# what a `Scene.provenance` key means, as a UI label for the "made with" line
MADE_BY = {
    "image": "Кадры",
    "video": "Клипы",
    "audio": "Озвучка",
    "translation": "Перевод субтитров",
    "reframe": "Кадрирование",
}


def _material(kind: str, title: str, value: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "value": value,
        "origin": "",  # where it came from: a link or a description
        "license": "",
        "credit": "",  # attribution line required by the license
        "link": "",  # external link, if separate from origin
        "url": None,  # file in the dashboard
        "resource": None,  # "<kind>/<id>" - resource card
        "from": "конвейер",  # row source: derived from pipeline files or recorded
        "state": "",  # for recorded rows: ok | changed | gone
    }
    row.update(extra)
    return row


def _recorded(edir: Path, materials: Materials) -> list[dict[str, Any]]:
    """Materials from the passport (`05_assemble/materials.json`) - what the pipeline does
    not know about the video: third-party clips, a mask, a track excerpt, a build script."""
    rows: list[dict[str, Any]] = []
    for item in materials.items:
        path = resolve(edir, ROOT, item.path)
        rows.append(
            _material(
                item.role,
                ROLE_TITLES.get(item.role, item.role),
                item.title or item.path or item.link,
                origin=item.note,
                license=item.license,
                credit=item.credit,
                link=item.link,
                url=file_url(path) if path else None,
                **{"from": "паспорт", "state": state(item, path)},
            )
        )
    return rows


def _materials(edir: Path, sb: Storyboard, plan: EditPlan | None, pid: str) -> list[dict[str, Any]]:
    """What the video is made of and where each part came from.

    Two sources, and they are NOT two answers to one question. For a video that went
    through the pipeline, everything was recorded along the way: what made a frame is in
    `Scene.provenance` ("comfy:klein4b"), its cost in `cost_usd`, track rights in its
    resource, the source video of a clip in `sources/<id>/source.json`. We derive from
    these and add nothing new.

    A video built by a script outside the pipeline has no such records - its materials
    live in code. They are listed by the passport `05_assemble/materials.json`, filled in
    by hand (`videoforge episode materials`).

    The two must not be mixed: a video without scenes still carries default resources in
    its storyboard (voice, caption style), and deriving a row from them would be a lie.
    So derived rows are only produced where the pipeline actually ran."""
    rows: list[dict[str, Any]] = []
    dirs = search_dirs(project_dir(pid))
    passport = load_materials(edir)
    # the video was built by the pipeline if it has scenes: they are its work
    from_pipeline = bool(sb.scenes)
    if not from_pipeline:
        return _recorded(edir, passport) if passport else []

    # what made each picture and sound layer: provider, how many times, at what cost
    made: dict[str, dict[str, float]] = {}
    for scene in sb.ordered_scenes():
        for key, provider in scene.provenance.items():
            bucket = made.setdefault(key, {})
            bucket[provider] = bucket.get(provider, 0) + 1
    spent: dict[str, float] = {}
    for scene in sb.ordered_scenes():
        for key, usd in scene.cost_usd.items():
            spent[key] = spent.get(key, 0.0) + usd
    for key, providers in made.items():
        parts = ", ".join(f"{name} × {n:g}" for name, n in sorted(providers.items()))
        cost = spent.get(key, 0.0)
        rows.append(
            _material(
                key,
                MADE_BY.get(key, key),
                parts,
                origin=f"${cost:.2f}" if cost else "$0.00",
            )
        )

    # episode resources: music and sounds require rights info, so it is included here
    for slot, kind in SLOT_KINDS.items():
        rid = getattr(sb.resources, slot, None)
        if not rid:
            continue
        try:
            res = load_resource(kind, rid, KINDS[kind], dirs)
        except (FileNotFoundError, KeyError):
            rows.append(_material(kind, SLOT_TITLES[slot], rid, origin="ресурс не найден"))
            continue
        rows.append(
            _material(
                kind,
                SLOT_TITLES[slot],
                res.name or rid,
                origin=getattr(res, "source", ""),
                license=getattr(res, "license", ""),
                credit=getattr(res, "attribution", ""),
                resource=f"{kind}/{rid}",
                # only audible resources (music, sound) have a file - a style or niche
                # does not, and that is not an error
                url=file_url(res.path()) if isinstance(res, MusicTrack) else None,
            )
        )

    # the bed is a build layer (project audio.bed, the plan may turn it off), not a
    # storyboard slot; its rights are the same as for music
    bed_id = _bed_id(sb, plan, pid)
    if bed_id:
        try:
            bed = load_resource("music", bed_id, MusicTrack, dirs)
        except FileNotFoundError:
            rows.append(_material("music", "Подложка", bed_id, origin="ресурс не найден"))
        else:
            rows.append(
                _material(
                    "music",
                    "Подложка",
                    bed.name or bed_id,
                    origin=bed.source,
                    license=bed.license,
                    credit=bed.attribution,
                    resource=f"music/{bed_id}",
                    url=file_url(bed.path()),
                )
            )

    # sound effects come from the edit plan, not the storyboard
    for cue in plan.sfx if plan else []:
        try:
            sound = load_resource("sfx", cue.id, Sfx, dirs)
        except FileNotFoundError:
            rows.append(_material("sfx", "Звук", cue.id, origin="ресурс не найден"))
            continue
        rows.append(
            _material(
                "sfx",
                "Звук",
                sound.name or cue.id,
                origin=sound.source,
                license=sound.license,
                resource=f"sfx/{cue.id}",
                url=file_url(sound.path()),
            )
        )

    # clips: the source video with a link and the window taken from it
    seen: set[str] = set()
    for scene in sb.ordered_scenes():
        if scene.source is None or scene.source.path in seen:
            continue
        seen.add(scene.source.path)
        # the source path is relative to the PROJECT folder: a dozen shorts are cut from one
        # podcast, so it lives next to all those episodes rather than inside one
        info = _read_json(project_dir(pid) / scene.source.path / "source.json") or {}
        title = info.get("title") or scene.source.path
        window = f"{scene.source.start_sec:.0f}–{scene.source.end_sec:.0f} с"
        rows.append(
            _material(
                "source",
                "Исходное видео",
                str(title),
                origin=f"{info.get('channel', '')} · отрезок {window}".strip(" ·"),
                link=str(info.get("url") or ""),
            )
        )
    # the passport complements the pipeline where it is blind: a third-party font, props,
    # a supplied file. An empty passport on a pipeline video is normal, not a gap
    if passport:
        rows.extend(_recorded(edir, passport))
    return rows


def _stale_inputs(edir: Path, sb: Storyboard, video: Path) -> list[str]:
    """Inputs newer than the finished file, meaning the video was not built from them.

    The build compares an input fingerprint itself and skips redundant work, but that
    fingerprint is only computed AT run time. Between runs a person edits the plan,
    overwrites a clip, re-voices a scene - and watches yesterday's file without knowing it.
    Comparing mtimes is cruder than a fingerprint, but always available and errs on the
    safe side: it says "check" rather than "all good".

    The storyboard and passport are excluded: the build itself and neighbouring stages
    write them, so they are always newer than the final."""
    made = video.stat().st_mtime_ns
    stale: list[str] = []
    watched: list[str] = []
    for scene in sb.ordered_scenes():
        watched.extend(
            rel
            for rel in (
                scene.assets.video,
                scene.assets.audio,
                scene.assets.captions,
                scene.assets.translation,
            )
            if rel
        )
    watched.append("05_edit/edit_plan.json")
    for rel in dict.fromkeys(watched):  # de-duplicated, order preserved
        path = edir / rel
        if path.exists() and path.stat().st_mtime_ns > made:
            stale.append(rel)
    return stale


def _wave(
    path: Path | None, *, duration: float, offset: float = 0.0, per_sec: int = PEAKS_PER_SEC
) -> list[float] | None:
    """A file's waveform placed in seconds of the FINISHED video.

    The offset is not decoration: a track started at second 50 and the voice of the eighth
    scene live in their own time, but are viewed on the shared one."""
    if path is None:
        return None
    env = cached_envelope(path, per_sec=per_sec)
    if env is None or not env.values:
        return None
    total = max(1, round(duration * per_sec))
    out = [0.0] * total
    shift = round(offset * per_sec)
    for i, value in enumerate(env.values):
        j = i + shift
        if 0 <= j < total:
            out[j] = value
    return out


def _wave_track(
    track_id: str, title: str, icon: str, hint: str, values: list[float] | None, **extra: Any
) -> dict[str, Any] | None:
    if not values or not any(values):
        return None
    return {
        "id": track_id,
        "kind": "wave",
        "title": title,
        "icon": icon,
        "hint": hint,
        "per_sec": PEAKS_PER_SEC,
        "values": [round(v, 2) for v in values],
        **extra,
    }


def _voice_wave(edir: Path, sb: Storyboard, entries: list[Entry], duration: float) -> list[float]:
    """Voice separately from the mix: scene voice-overs placed at their positions."""
    per = PEAKS_PER_SEC
    total = max(1, round(duration * per))
    out = [0.0] * total
    by_id = {e.scene_id: e for e in entries}
    for scene in sb.ordered_scenes():
        rel = scene.assets.audio
        e = by_id.get(scene.id)
        if not rel or e is None or not (edir / rel).exists():
            continue
        env = cached_envelope(edir / rel, per_sec=per)
        if env is None:
            continue
        shift = round((e.box_start + scene_lead_in(scene)) * per)  # voice-over follows lead-in
        for i, value in enumerate(env.values):
            j = i + shift
            if 0 <= j < total:
                out[j] = max(out[j], value)
    return out


def _marks_track(
    track_id: str, title: str, icon: str, hint: str, items: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not items:
        return None
    return {"id": track_id, "kind": "marks", "title": title, "icon": icon, "hint": hint,
            "items": items}  # fmt: skip


SFX_MARK_SEC = 0.25  # a sound is a point in time; on the track it needs a visible width


def _by_time(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: float(item["start"]))


def _plan_marks(plan: EditPlan | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Plan frame effects and sounds, in seconds of the finished file, which is where they live.

    Same shape as the other tracks (`start`/`end`): they are drawn by the same code, and
    mismatched keys always end up as someone's empty track."""
    if plan is None:
        return [], []
    effects = [
        {
            "start": round(e.at_sec, 3),
            "end": round(e.at_sec + e.duration_sec, 3),
            "label": e.kind,
            "kind": e.kind,
            "text": e.why,
        }
        for e in plan.effects
    ]
    sfx = [
        {
            "start": round(c.at_sec, 3),
            "end": round(c.at_sec + SFX_MARK_SEC, 3),
            "label": c.id,
            "kind": "sfx",
            "text": c.why,
        }
        for c in plan.sfx
    ]
    # sorted: a person writes the plan by hand, effects may be in any order, and the track
    # finds "what is now" by binary search over time
    return _by_time(effects), _by_time(sfx)


def _music_marks(
    track: MusicTrack | None, start_sec: float, plan: EditPlan | None, montage: Any
) -> list[dict[str, Any]]:
    """What happens to the music: where the track enters, its drop, and gain steps.

    A step is not instantaneous: `gain_expr` ramps the volume to the new level linearly
    over a second AFTER the mark (`GAIN_RAMP_SEC`). Whoever places a drop exactly on the
    climax will hear it a second late - this should be visible on the track, not
    discovered in the finished file."""
    marks: list[dict[str, Any]] = []
    if track is not None and track.drop_sec is not None:
        marks.append(
            {
                "at": round(track.drop_sec + start_sec, 3),
                "label": "дроп",
                "kind": "drop",
                "why": f"drop_sec трека {track.id}",
            }
        )
    steps = (montage or {}).get("music_steps") if isinstance(montage, dict) else None
    if steps:
        for step in steps:
            marks.append(
                {
                    "at": round(float(step.get("at_sec", 0.0)), 3),
                    "label": f"{step.get('gain_db')} дБ",
                    "kind": "gain",
                    "ramp": GAIN_RAMP_SEC,
                    "why": f"{step.get('scene_id') or 'ступень плана'}, выходит на уровень за "
                    f"{GAIN_RAMP_SEC:g} с",
                }
            )
    elif plan is not None and plan.music is not None:
        for cue in plan.music.cues:
            marks.append(
                {
                    "at": round(cue.at_sec, 3),
                    "label": f"{cue.gain_db} дБ",
                    "kind": "gain",
                    "ramp": GAIN_RAMP_SEC,
                    "why": cue.why,
                }  # fmt: skip
            )
    return sorted(marks, key=lambda m: m["at"])


def _level(wave: list[float] | None, start: float, end: float) -> float:
    """Average track loudness over a span, to say "it is quiet here" with a number."""
    if not wave:
        return 0.0
    a = max(0, round(start * PEAKS_PER_SEC))
    b = min(len(wave), max(a + 1, round(end * PEAKS_PER_SEC)))
    window = wave[a:b]
    return round(sum(window) / len(window), 3) if window else 0.0


_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# share of the SMALLER track found in the larger one. A symmetric similarity does not work
# here: only part of the words reach the screen (e.g. an intro drawn by a mask rather than
# captions: 29 words vs 38), and an honest 29 out of 29 gave 0.87
SAME_SOURCE_RATIO = 0.9


def _tokens(items: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for item in items:
        out.extend(m.group(0).lower() for m in _WORD_RE.finditer(str(item.get("text", ""))))
    return out


def _circular_note(tracks: dict[str, Any]) -> dict[str, Any] | None:
    """The "On screen" and "Lyrics" tracks from one file are a mirror, not a cross-check.

    The tracks sit side by side to catch "written != sung". But if the on-screen text was
    MADE from the same word file, they will always match - including when both are wrong.
    This once produced a false "verified": the on-screen word and the lyric word matched to
    the second while the vocal was singing something else entirely. Such a case must not
    go unmentioned: a match looks more convincing than a mismatch, and a person reads it
    as proof."""
    captions, lyrics = tracks.get("captions"), tracks.get("lyrics")
    if not captions or not lyrics:
        return None
    a, b = _tokens(captions["items"]), _tokens(lyrics["items"])
    if not a or not b:
        return None
    matched = sum(block.size for block in SequenceMatcher(None, a, b).get_matching_blocks())
    if matched / min(len(a), len(b)) < SAME_SOURCE_RATIO:
        return None
    return {
        "severity": "warn",
        "at_sec": 0.0,
        "text": (
            "«На экране» и «Слова песни» совпадают дословно — это ОДИН источник, "
            "а не две дорожки для сверки: текст на экране собран из того же файла слов. "
            "Проверить по нему, попадает ли текст в вокал, нельзя — только ушами"
        ),
    }


def _notes(
    tracks: list[dict[str, Any]], sound: list[float] | None, lyrics_issues: list[str] | None = None
) -> list[dict[str, Any]]:
    """Notes computed by Python rather than by eye.

    Only measurable things: text that cannot be read in time, and text over silence.
    Everything else is "look for yourself" - that is what the tracks are drawn for.

    The speech track is deliberately excluded: short words really do sound for 0.09 s,
    and that is true, not a defect. A word shorter than a frame ON SCREEN is different:
    nobody can read it, and it almost always means timings glued together in a batch."""
    notes: list[dict[str, Any]] = []
    by_id = {t["id"]: t for t in tracks}
    # first, what makes the other text notes moot: the word file is not from this
    # recording, so none of its times mean anything
    notes.extend(
        {"severity": "warn", "at_sec": 0.0, "text": f"Слова песни: {issue}"}
        for issue in lyrics_issues or []
    )
    if (circular := _circular_note(by_id)) is not None:
        notes.append(circular)
    # A too-short word is looked for ONLY on screen: "cannot be read in time" makes sense
    # for text the viewer sees. The "Lyrics" track is for reference, nobody reads it, and
    # it used to produce the same note a second time: two yellow blocks about one fact.
    #
    # And the threshold is a share, not a single word: a couple of short words out of forty
    # is a fast line, not a timing defect. A check that fires on every video stops being
    # a signal.
    captions = by_id.get("captions")
    items = (captions or {}).get("items") or []
    short = [i for i in items if i["end"] - i["start"] < SHORT_WORD_SEC]
    # `captions` in the condition is not redundant: without the track items is empty, so is
    # short, and the branch would not fire - but that relies on the two lines above and
    # would break silently if they changed. `captions["title"]` is read below, and the
    # right to do so should be visible here rather than inferred
    if captions and short and len(short) >= SHORT_SHARE * len(items):
        first = short[0]
        notes.append(
            {
                "severity": "warn",
                "at_sec": first["start"],
                "text": (
                    f"«{captions['title']}»: {len(short)} из {len(items)} держатся меньше "
                    f"{SHORT_WORD_SEC:g} с — первое «{first['text']}» "
                    f"на {first['start']:.2f} с. Так выглядят тайминги, "
                    "слепленные пачкой, а не по произношению"
                ),
            }
        )
    if captions and sound:
        silent = [
            i
            for i in captions["items"]
            if _level(sound, i["start"], i["end"]) < SILENT_LEVEL and i["end"] - i["start"] > 0.3
        ]
        if silent:
            first = silent[0]
            notes.append(
                {
                    "severity": "warn",
                    "at_sec": first["start"],
                    "text": (
                        f"{len(silent)} реплик стоят там, где в ролике тишина — первая "
                        f"«{first['text']}» на {first['start']:.2f} с"
                    ),
                }
            )
    if not by_id.keys() - {"sound"}:
        # an empty montage looks like a failure, but it is not: there is simply nothing
        # next to the video. Saying so is more honest than showing a lone waveform
        notes.append(
            {
                "severity": "info",
                "at_sec": 0.0,
                "text": (
                    "Кроме готового файла рядом ничего нет — ни раскадровки, ни субтитров, "
                    "ни плана монтажа. Так выглядит копия чужого ролика в галерее и сборка "
                    "скриптом мимо конвейера: дорожки живут там, где этот ролик собирали"
                ),
            }
        )
    return notes


def _copy_origin(edir: Path, sb: Storyboard) -> tuple[str, str, Path] | None:
    """Whose file this is, if the video is a copy of another episode's finished file.

    The passport (`materials.json`) records this with the `copy_of` role and the path to
    the original, from which `<project>/<episode>` is derived. Only for videos without
    scenes: an episode with a storyboard has its own tracks and needs no one else's."""
    if sb.scenes or not (passport := load_materials(edir)):
        return None
    for item in passport.items:
        if item.role != "copy_of":
            continue
        path = resolve(edir, ROOT, item.path)
        if path is None:
            continue
        path = path.resolve()
        # .../projects/<p>/episodes/<e>/05_assemble/<file>
        if len(path.parents) < 5 or path.parent.name != ASSEMBLE_DIR:
            continue
        if path.parents[2].name != EPISODES_DIR:
            continue
        if path.parents[4] != projects_dir().resolve():
            continue
        return path.parents[3].name, path.parents[1].name, path
    return None


def _adopt_origin(
    pid: str, ep: str, edir: Path, sb: Storyboard, video: Path
) -> dict[str, Any] | None:
    """A copy's montage is the original's montage, as long as the files match.

    A copy of someone else's finished file (the test gallery) carries just one waveform on
    its own: scenes, captions and the plan with effects and sounds live where the video
    was built. The passport knows where, and the tracks are taken from there - but only if
    the original file is the one that was copied (same size, byte for byte): a rebuilt
    original describes a different video, and showing its tracks under this file would
    pass a coincidence off as proof."""
    origin = _copy_origin(edir, sb)
    if origin is None:
        return None
    opid, oep, ofile = origin
    if (opid, oep) == (pid, ep):
        return None
    ref = f"{opid}/{oep}"
    if ofile.stat().st_size != video.stat().st_size:
        return {
            "ref": ref,
            "same": False,
            "note": {
                "severity": "warn",
                "at_sec": 0.0,
                "text": (
                    f"Это копия ролика {ref}, но оригинал с тех пор пересобран (размер файла "
                    "другой): его дорожки этому файлу не соответствуют и не показаны. "
                    f"Дорожки смотреть у {ref}; свежую копию положить сюда заново и обновить "
                    f"паспорт: videoforge episode materials {pid}/{ep} --scan"
                ),
            },
        }
    try:
        original = episode_montage(opid, oep, follow_copy=False)
    except NotFound as exc:
        return {
            "ref": ref,
            "same": False,
            "note": {
                "severity": "warn",
                "at_sec": 0.0,
                "text": f"Это копия ролика {ref}, но его монтаж не открывается: {exc}",
            },
        }
    return {
        "ref": ref,
        "same": True,
        "montage": original,
        "note": {
            "severity": "info",
            "at_sec": 0.0,
            "text": (
                f"Это копия ролика {ref}: файлы совпадают, и дорожки — сцены, субтитры, план с "
                "эффектами и звуками — взяты у него. Правки делаются там, здесь только просмотр"
            ),
        },
    }


def episode_montage(pid: str, ep: str, *, follow_copy: bool = True) -> dict[str, Any]:
    """Everything needed to draw the episode timeline: the video and the tracks below it.

    `follow_copy` - whether to follow the passport to the original if the video is a copy
    of another finished file. It is turned off after one step: a copy of a copy is not
    unwound."""
    edir = episode_dir(pid, ep)
    try:
        sb = load_storyboard(edir)
    except FileNotFoundError as exc:
        raise NotFound(f"эпизода {pid}/{ep} нет") from exc
    video = edir / sb.final.video if sb.final.video else None
    if video is None or not video.exists():
        raise NotFound(f"у эпизода {pid}/{ep} нет готового файла — монтаж показывать не на чем")
    adopted = _adopt_origin(pid, ep, edir, sb, video) if follow_copy else None
    if adopted and adopted["same"]:
        original = adopted["montage"]
        passport = load_materials(edir)
        return {
            **original,
            "ref": f"{pid}/{ep}",
            "project": pid,
            "episode": ep,
            "title": sb.title or sb.publish.title or ep,
            "video_url": file_url(video),
            "poster_url": file_url(poster_path(video)),
            "copy_of": adopted["ref"],
            # first the "copy of video X" row from its own passport, then what the
            # video itself is made of - which the original knows
            "materials": _materials(edir, sb, None, pid) + original["materials"],
            "passport": (
                {"made_by": passport.made_by, "rebuild": passport.rebuild, "note": passport.note}
                if passport
                else None
            ),
            "notes": [adopted["note"], *original["notes"]],
        }
    montage = _read_json(edir / ASSEMBLE_DIR / MONTAGE_FILE)
    plan = load_plan(edir)
    entries = _timeline(edir, sb)
    duration = sb.final.duration_sec or (entries[-1].end if entries else 0.0)
    sound = _wave(video, duration=duration)
    if not duration and sound:
        duration = round(len(sound) / PEAKS_PER_SEC, 3)

    outro = _outro(montage, duration)
    ass_events: list[AssEvent] = []
    for path in _ass_files(edir, sb):
        ass_events.extend(parse_ass_events(path.read_text(encoding="utf-8", errors="replace")))
    ass_events.sort(key=lambda e: e.start)
    # overlays differ from captions by STYLE NAME, not layer: in animated captions layer 1
    # is used for the word lift (156 of 234 lines in a real short), so it cannot tell a
    # plate apart
    rendered_overlays = [e for e in ass_events if e.style.startswith(OVERLAY_STYLE)]
    captions = [e for e in ass_events if not e.style.startswith(OVERLAY_STYLE)]

    overlays = _overlay_track(sb, entries, rendered_overlays, outro)
    track, music_start = _music(sb, plan, pid)
    lyrics_path = track.lyrics_path() if track else None
    lyrics_words, lyrics_text, lyrics_raw = (
        read_lyrics(lyrics_path) if lyrics_path else ([], "", "")
    )
    # the track length comes from the waveform cache rather than a separate ffmpeg call: it
    # is only needed to ask "is this the right word file", and window precision suffices
    track_env = cached_envelope(track.path()) if track else None
    lyrics_issues = (
        lyrics_problems(
            lyrics_raw,
            lyrics_words,
            track_sec=round(len(track_env.values) / PEAKS_PER_SEC, 2) if track_env else None,
        )
        if lyrics_path
        else []
    )
    lyrics_items = _words_items(lyrics_words, music_start, within=duration)
    if lyrics_words and not lyrics_items:
        lyrics_issues.append(
            f"в файле {len(lyrics_words)} слов, но НИ ОДНО не попало в этот отрезок трека "
            f"({-music_start:.1f}–{-music_start + duration:.1f} с). Либо здесь инструментал, "
            "либо распознавание пропустило вокал — на слух это различается сразу"
        )

    tracks: list[dict[str, Any]] = []
    for candidate in (
        # with a storyboard - scenes; without one (built by a script or brought in
        # finished) - shots detected from the file itself: otherwise there would be no
        # track at all answering "what is it made of"
        _scene_track(edir, sb, entries)
        or _shots_track(edir, video, duration, _scene_threshold(pid)),
        _caption_track(captions),
        overlays,
        _speech_track(edir, sb, entries, outro),
        (
            {
                "id": "lyrics",
                "kind": "words",
                "title": "Слова песни",
                "icon": "music",
                "hint": f"{lyrics_path.name} — тайминги файла, сдвинутые на начало трека"
                if lyrics_path
                else "",
                "items": lyrics_items,
            }
            # an empty track reads as "no words", while words exist - just none fall into
            # this span. These are different things, and the latter is reported by a note,
            # not by an empty row on the timeline
            if lyrics_items
            else None
        ),
        _wave_track(
            "sound",
            "Звук",
            "audio",
            "измерено в готовом файле: голос, музыка и звуки вместе",
            sound,
        ),  # fmt: skip
        _wave_track(
            "voice",
            "Голос",
            "mic",
            "озвучки сцен по отдельности, без музыки",
            _voice_wave(edir, sb, entries, duration),
        ),  # fmt: skip
        _wave_track(
            "music",
            "Музыка",
            "music",
            f"трек {track.id} с {-music_start:g} с"
            if track and music_start < 0
            else (f"трек {track.id}" if track else ""),
            _wave(track.path() if track else None, duration=duration, offset=music_start),
            marks=_music_marks(track, music_start, plan, montage),
            text=lyrics_text,
        ),  # fmt: skip
    ):
        if candidate:
            tracks.append(candidate)

    effects, sfx = _plan_marks(plan)
    for candidate in (
        _marks_track("effects", "Эффекты", "effects", "эффекты кадра из плана монтажа", effects),
        _marks_track("sfx", "Звуки", "sfx", "звуковые эффекты из плана монтажа", sfx),
    ):
        if candidate:
            tracks.append(candidate)

    return {
        "ref": f"{pid}/{ep}",
        "project": pid,
        "episode": ep,
        "title": sb.title or sb.publish.title or ep,
        "aspect_ratio": sb.aspect_ratio,
        "language": sb.language,
        "captions_language": sb.captions_language,
        "duration_sec": round(duration, 3),
        "video_url": file_url(video),
        "poster_url": file_url(poster_path(video)),
        "per_sec": PEAKS_PER_SEC,
        "plan": {
            "author": sb.final.edit_author,
            "note": (montage or {}).get("plan") if isinstance(montage, dict) else None,
            "url": file_url(edir / sb.final.edit_plan) if sb.final.edit_plan else None,
        },
        "measured": (edir / ASSEMBLE_DIR / TIMELINE_FILE).exists(),
        # what changed after the build: empty means the file was made from what is here
        "stale": _stale_inputs(edir, sb, video),
        # copy of another finished file: the original's `<project>/<episode>` whose
        # tracks are shown here; None means the video is its own
        "copy_of": None,
        "tracks": tracks,
        "materials": _materials(edir, sb, plan, pid),
        # the video's passport: how it was built and how to rebuild it. Empty for pipeline
        # videos - for them this is already known from the stages
        "passport": (
            {"made_by": passport.made_by, "rebuild": passport.rebuild, "note": passport.note}
            if (passport := load_materials(edir))
            else None
        ),
        "notes": (
            [adopted["note"], *_notes(tracks, sound, lyrics_issues)]
            if adopted
            else _notes(tracks, sound, lyrics_issues)
        ),
    }
