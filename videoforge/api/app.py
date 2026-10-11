"""FastAPI application: `/api/...` routes, `/files/...` media and the built frontend.

Started with `videoforge ui`. Listens on localhost by default: this is a single-user
dashboard with no authentication. All responses are built by functions in `api/data.py`;
this module only maps "not found" to 404 and serves files.

**About exposing `--host`.** Viewing the dashboard from a phone is fine (`--host 0.0.0.0`),
but only within a private network of your own devices (e.g. Tailscale plus a firewall rule
for its subnet). Never expose it to the internet via port forwarding or a public tunnel:
there is no authentication, and `/files/` serves any file under its roots, including
`accounts/.../.profile/` with live social-media sessions. The origin check below does not
help here - it protects against a foreign page, not a foreign person.

**About mutating requests.** While the dashboard was read-only, having no authentication
cost nothing. Once a POST that can publish a video appeared, "no auth on localhost" stopped
being harmless: any page open in the browser can POST to `localhost:8765`, no cookies
needed. So every unsafe method goes through an origin check (`_same_origin`): a foreign
`Origin`/`Referer` or `Sec-Fetch-Site: cross-site` gets 403. Reads (GET/HEAD/OPTIONS)
are left alone.
"""

from __future__ import annotations

import sys
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from videoforge import __version__
from videoforge.api import agent_tasks, bench, data, inbox, montage, planedit
from videoforge.paths import episode_dir, ui_dist_dir

if TYPE_CHECKING:  # stages are imported inside functions so dashboard startup does not wait
    from videoforge.stages.publish_upload import PublishReport, StatusReport

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def silence_disconnect_noise() -> None:
    """Silence the traceback that video seeking prints to the terminal.

    When the user drags the slider, the browser player aborts the pending range request.
    On Windows the event loop closes such a socket via `shutdown()`, gets
    `ConnectionResetError: [WinError 10054]` and prints a full traceback. The server is
    fine and the request was served, but in the terminal it looks like a crash and hides
    the real messages (most visible in the montage view, where seeking is constant).

    The suppression is narrow: only `ConnectionResetError` and only while closing the
    connection. Any other error is still shown."""
    if sys.platform != "win32":
        return
    from asyncio.proactor_events import _ProactorBasePipeTransport

    # a private stdlib method: not in the type stubs, but present since 3.8 with no reason
    # to change - if it disappears, so does the noise
    original = getattr(_ProactorBasePipeTransport, "_call_connection_lost", None)
    if original is None or getattr(original, "_videoforge_quiet", False):
        return

    @wraps(original)
    def quiet(self: Any, exc: BaseException | None) -> None:
        try:
            original(self, exc)
        except ConnectionResetError:
            pass

    quiet._videoforge_quiet = True  # type: ignore[attr-defined]
    _ProactorBasePipeTransport._call_connection_lost = quiet  # type: ignore[attr-defined]


def _host_of(value: str) -> str:
    """Host from Origin/Referer: scheme and port do not matter, the name does."""
    parsed = urlsplit(value if "//" in value else f"//{value}")
    return (parsed.netloc or parsed.path).split("@")[-1].lower()


def same_origin(request: Request) -> bool:
    """The request came from the dashboard's own page, not from a foreign site.

    Checked using what the browser sets itself and a page cannot forge: `Sec-Fetch-Site`,
    `Origin`, `Referer`. A request with none of these headers is not from a browser (curl,
    a script) and is allowed: CSRF cannot happen there, and there is no reason to break
    `videoforge` in the console."""
    site = request.headers.get("sec-fetch-site", "").lower()
    if site and site not in ("same-origin", "same-site", "none"):
        return False
    host = _host_of(request.headers.get("host", ""))
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value or value.lower() == "null":
            continue
        return _host_of(value) == host and bool(host)
    return True


class PlanPatch(BaseModel):
    """Plan edit body: the whole plan and the fingerprint of what the dashboard read.

    The whole plan rather than "field = value": pydantic validates the schema, so an
    invented field cannot exist. The fingerprint guards against overwriting another edit."""

    plan: dict[str, object]
    base_fingerprint: str = ""


class ResourceRename(BaseModel):
    """Resource rename body: the name only. `id` never changes - projects and scenes
    reference it, and relinking is not something a dashboard button does."""

    name: str


class CleanupBody(BaseModel):
    """Which classes of intermediate files to delete (`core/cleanup.py`). Defaults to
    `junk` only - things a rebuild restores in seconds."""

    classes: list[str] = Field(default_factory=lambda: ["junk"])


class VerdictBody(BaseModel):
    """Human verdict on a benchmark take. `base` is the fingerprint of the verdict file the
    dashboard read: a mismatch means the rating was already saved from another tab."""

    reviewer: str = ""
    criteria: dict[str, str] = Field(default_factory=dict)
    defect_times: list[float] = Field(default_factory=list)
    accepted_interval: list[float] | None = None
    notes: str = ""
    base: str = ""


class PublishRequest(BaseModel):
    """Publish button body. `dry_run` defaults to True: the dashboard shows what would be
    sent, and the decision to upload is a separate click."""

    to: list[str] = Field(default_factory=list)
    dry_run: bool = True
    force: bool = False


class StatusRequest(BaseModel):
    """Body of the "what happened to the video" query: platforms only (empty means all it
    was published to). No dry run or force here - this is a read."""

    to: list[str] = Field(default_factory=list)


class SchedulePatch(BaseModel):
    """The full schedule body: daily slots and the gap. The whole thing rather than "add a
    slot": the file is small, and this keeps the screen and the file in the same state."""

    times: list[str] = Field(default_factory=list)
    paused: bool = False


def _publish_body(report: PublishReport) -> dict[str, object]:
    """Publish or update report -> dict for the dashboard. One for both buttons: it is the
    same report, and there is no reason for the two to diverge."""
    return {
        "dry_run": report.dry_run,
        "state": report.state,
        "ok": report.ok,
        "title": report.title,
        "video": str(report.video),
        "warnings": report.warnings,
        "platforms": [
            {
                "platform": p.platform,
                "state": p.state,
                "video_id": p.video_id,
                "url": p.url,
                "provider": p.provider,
                "account": p.account,
                "warnings": p.warnings,
                "error": p.error,
                "changes": p.changes,
            }
            for p in report.platforms
        ],
    }


def _status_body(report: StatusReport) -> dict[str, object]:
    """Platform response about a video -> dict for the dashboard: the same lines the CLI
    prints, only `checks` without the "CHECK" prefix, which the UI adds."""
    return {
        "ok": report.ok,
        "project": report.project_id,
        "episode": report.episode_id,
        "platforms": [
            {
                "platform": p.platform,
                "where": p.where,
                "video_id": p.video_id,
                "url": p.url,
                "state": p.state,
                "reason": p.reason,
                "privacy": p.privacy,
                "provider": p.provider,
                "account": p.account,
                "fixed": p.fixed,
                "checks": p.checks,
                "notes": p.notes,
                "error": p.error,
            }
            for p in report.platforms
        ],
    }


def create_app(*, ui_dist: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="videoforge",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    api = APIRouter(prefix="/api")

    @app.exception_handler(data.NotFound)
    async def _not_found(_: Request, exc: data.NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.middleware("http")
    async def _guard(request: Request, call_next: object) -> Response:
        """Anything that changes the world is accepted only from the dashboard's own page."""
        if request.method not in SAFE_METHODS and not same_origin(request):
            return JSONResponse(
                status_code=403,
                content={"detail": "запрос с чужого источника: пульт принимает только свои"},
            )
        return await call_next(request)  # type: ignore[operator, no-any-return]

    @api.get("/meta")
    def meta() -> dict[str, object]:
        return {"version": __version__, "ui": ui_dist is not None and ui_dist.is_dir()}

    # ------------------------------------------------------------ accounts
    @api.get("/accounts")
    def accounts() -> dict[str, object]:
        return data.accounts_overview()

    @api.get("/accounts/{account_id}")
    def account(account_id: str) -> dict[str, object]:
        return data.account_detail(account_id)

    # ------------------------------------------------------------ projects
    @api.get("/projects")
    def projects() -> dict[str, object]:
        return data.projects_overview()

    @api.get("/projects/{project_id}")
    def project(project_id: str) -> dict[str, object]:
        return data.project_detail(project_id)

    @api.get("/projects/{project_id}/queue")
    def project_queue(project_id: str) -> dict[str, object]:
        """Queue with schedule: the "when -> what" plan, stock, warnings and the
        scheduler's last tick. Same as `videoforge queue <project>`."""
        from videoforge.core.project import load_project

        try:
            load_project(project_id)
        except FileNotFoundError as exc:
            raise data.NotFound(str(exc)) from exc
        return data.queue_view(project_id)

    @api.patch("/projects/{project_id}/schedule")
    def project_schedule(project_id: str, body: SchedulePatch) -> dict[str, object]:
        """Slots and gap from the dashboard - the same write to `queue.yaml` as
        `videoforge queue --times`. The scheduler task is not touched: it ticks every
        15 minutes and reads the file, so the new time takes effect without
        re-registration."""
        try:
            return data.update_schedule(project_id, body.times, body.paused)
        except ValueError as exc:  # "25:00" and other non-times
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/projects/{project_id}/episodes/{episode_id}")
    def episode(project_id: str, episode_id: str) -> dict[str, object]:
        return data.episode_detail(project_id, episode_id)

    @api.get("/projects/{project_id}/episodes/{episode_id}/montage")
    def episode_montage(project_id: str, episode_id: str) -> dict[str, object]:
        """Timeline tracks of the finished video: scenes, text, speech, waveform, effects."""
        return montage.episode_montage(project_id, episode_id)

    @api.get("/projects/{project_id}/episodes/{episode_id}/plan")
    def episode_plan(project_id: str, episode_id: str) -> dict[str, object]:
        """Episode edit plan: the plan, its fingerprint and the list of editable fields."""
        return planedit.read_plan(project_id, episode_id)

    @api.patch("/projects/{project_id}/episodes/{episode_id}/plan")
    def episode_plan_patch(project_id: str, episode_id: str, body: PlanPatch) -> dict[str, object]:
        """Plan edit from the dashboard: field whitelist, fingerprint against concurrent
        writes, same lint as the stage. The finished file is not touched - it is rebuilt
        separately, with a deliberate click."""
        try:
            update = planedit.write_plan(project_id, episode_id, body.plan, body.base_fingerprint)
        except planedit.PlanConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (planedit.PlanForbidden, planedit.PlanBlocked) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:  # failed pydantic schema validation
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return update.dict()

    @api.post("/projects/{project_id}/episodes/{episode_id}/rebuild")
    def episode_rebuild(project_id: str, episode_id: str) -> dict[str, object]:
        """Rebuild the video from the edited plan - the same function as the CLI
        (`videoforge run <ref> --only assemble`).

        Rebuilds ASSEMBLY only: nothing paid is called, no keys are needed, and the
        "what made this episode" record is untouched. While it runs, a second click gets
        409 instead of a second ffmpeg writing the same file."""
        from videoforge.config import ProviderNames
        from videoforge.core.runner import Busy, run_episode
        from videoforge.core.stage import RunOptions
        from videoforge.providers.registry import build_providers

        edir = episode_dir(project_id, episode_id)
        if not edir.is_dir():
            raise data.NotFound(f"эпизода {project_id}/{episode_id} нет")
        try:
            report = run_episode(
                edir,
                build_providers(ProviderNames.mock()),  # assembly needs no providers
                RunOptions(only="assemble", auto_approve=True),
            )
        except Busy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        stage = report.executed[0][1] if report.executed else None
        return {
            "ok": report.ok,
            "status": stage.status if stage else "failed",
            "message": stage.message if stage else (report.reason or "стадия не запускалась"),
            "reason": report.reason,
        }

    @api.get("/projects/{project_id}/episodes/{episode_id}/cleanup")
    def episode_cleanup_plan(project_id: str, episode_id: str) -> dict[str, object]:
        """Cleanup dry run: episode files by class with sizes - same as
        `videoforge episode cleanup <ref>` without `--yes`. Deletes nothing."""
        from videoforge.core import cleanup
        from videoforge.core.storyboard import STORYBOARD_FILE

        edir = episode_dir(project_id, episode_id)
        if not (edir / STORYBOARD_FILE).exists():
            raise data.NotFound(f"эпизода {project_id}/{episode_id} нет")
        try:
            return cleanup.to_dict(cleanup.cleanup(edir, apply=False))
        except cleanup.CleanupRefused as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @api.post("/projects/{project_id}/episodes/{episode_id}/cleanup")
    def episode_cleanup_run(
        project_id: str, episode_id: str, body: CleanupBody
    ) -> dict[str, object]:
        """Delete the selected classes - the same function as the CLI with `--yes`: under
        the episode lock (busy -> 409), `downloaded` only with a confirmed link, and
        recorded in the manifest."""
        from videoforge.core import cleanup
        from videoforge.core.runner import Busy
        from videoforge.core.storyboard import STORYBOARD_FILE

        edir = episode_dir(project_id, episode_id)
        if not (edir / STORYBOARD_FILE).exists():
            raise data.NotFound(f"эпизода {project_id}/{episode_id} нет")
        try:
            return cleanup.to_dict(cleanup.cleanup(edir, tuple(body.classes), apply=True))
        except Busy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except cleanup.CleanupRefused as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:  # unknown class
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/projects/{project_id}/episodes/{episode_id}/report")
    def episode_report(project_id: str, episode_id: str) -> RedirectResponse:
        """An existing episode HTML report: served from the episode folder so its
        relative links to frames work."""
        from videoforge.core.manifest import load_manifest
        from videoforge.core.report import render_report
        from videoforge.core.storyboard import load_storyboard
        from videoforge.paths import episode_dir

        edir = episode_dir(project_id, episode_id)
        try:
            path = render_report(edir, load_storyboard(edir), load_manifest(edir))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        url = data.file_url(path)
        if url is None:
            raise HTTPException(status_code=404, detail="отчёт вне корней файлов")
        return RedirectResponse(url)

    @api.post("/projects/{project_id}/episodes/{episode_id}/publish")
    def episode_publish(
        project_id: str, episode_id: str, body: PublishRequest | None = None
    ) -> dict[str, object]:
        """The same function as the CLI (`videoforge publish`), with the same state file.

        Dry run by default: the dashboard shows what would go where. A real upload is
        `dry_run: false`; the server has no publishing logic of its own."""
        from videoforge.core.runner import Busy
        from videoforge.providers.base import ProviderError
        from videoforge.stages.publish_upload import NotReady, publish_episode

        req = body or PublishRequest()
        edir = episode_dir(project_id, episode_id)
        if not edir.is_dir():
            raise data.NotFound(f"эпизода {project_id}/{episode_id} нет")
        try:
            report = publish_episode(
                edir, to=req.to, dry_run=req.dry_run, force=req.force, log_fn=lambda _: None
            )
        except (NotReady, ProviderError, Busy) as exc:
            # Busy: the episode is already being worked on (a terminal run, a second
            # button click). Same 409 as for rebuild: this is not a failure
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _publish_body(report)

    @api.post("/projects/{project_id}/episodes/{episode_id}/update")
    def episode_update(
        project_id: str, episode_id: str, body: PublishRequest | None = None
    ) -> dict[str, object]:
        """Update an already published video - the same function as the CLI
        (`videoforge publish ... --update`), with the same state file.

        Dry run by default: it doubles as the confirmation screen, since it returns a
        "before -> after" list for each field. Visibility is deliberately not among the
        fields - making a video public is decided by `project.yaml`, not by a browser
        click."""
        from videoforge.core.runner import Busy
        from videoforge.providers.base import ProviderError
        from videoforge.stages.publish_upload import NotReady, update_episode

        req = body or PublishRequest()
        edir = episode_dir(project_id, episode_id)
        if not edir.is_dir():
            raise data.NotFound(f"эпизода {project_id}/{episode_id} нет")
        try:
            report = update_episode(
                edir, to=req.to, dry_run=req.dry_run, force=req.force, log_fn=lambda _: None
            )
        except (NotReady, ProviderError, Busy) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _publish_body(report)

    @api.post("/projects/{project_id}/episodes/{episode_id}/publish/status")
    def episode_publish_status(
        project_id: str, episode_id: str, body: StatusRequest | None = None
    ) -> dict[str, object]:
        """Ask the platform what happened to an already published video - the same
        function as the CLI (`videoforge publish ... --status`), with the same state file.

        POST, not GET: it is a read with one side effect - an interrupted upload whose
        video the platform found and processed is moved from in_flight to platforms.
        Everything that writes files goes through the origin check, and that only covers
        unsafe methods."""
        from videoforge.core.runner import Busy
        from videoforge.providers.base import ProviderError
        from videoforge.stages.publish_upload import NotReady, check_episode

        req = body or StatusRequest()
        edir = episode_dir(project_id, episode_id)
        if not edir.is_dir():
            raise data.NotFound(f"эпизода {project_id}/{episode_id} нет")
        try:
            report = check_episode(edir, to=req.to)
        except (NotReady, ProviderError, Busy) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _status_body(report)

    # ------------------------------------------------------------ tests
    @api.get("/tests")
    def tests() -> dict[str, object]:
        """Gallery of test videos: projects with `kind: test` in project.yaml."""
        return data.tests_overview()

    # ------------------------------------------------------------ resources
    @api.get("/resources")
    def resources() -> dict[str, object]:
        return data.resources_overview()

    @api.get("/resources/{kind}")
    def resources_kind(kind: str) -> dict[str, object]:
        return data.resources_list(kind)

    @api.get("/resources/{kind}/{resource_id}")
    def resource(kind: str, resource_id: str, project: str | None = None) -> dict[str, object]:
        return data.resource_detail(kind, resource_id, project)

    @api.patch("/resources/{kind}/{resource_id}")
    def resource_rename(
        kind: str, resource_id: str, body: ResourceRename, project: str | None = None
    ) -> dict[str, object]:
        """Rename a resource from the dashboard - the same function as the CLI
        (`videoforge resource rename`), same file. Only the `name:` line changes; the
        template's comments stay in place."""
        from videoforge.core.resources import rename_resource, search_dirs
        from videoforge.paths import project_dir, resources_dir

        dirs = search_dirs(project_dir(project)) if project else [resources_dir()]
        try:
            rename_resource(kind, resource_id, body.name, dirs)
        except FileNotFoundError as exc:
            raise data.NotFound(str(exc)) from exc
        except ValueError as exc:  # empty name or unknown kind
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return data.resource_detail(kind, resource_id, project)

    # ------------------------------------------------------------ library
    @api.get("/library")
    def library() -> dict[str, object]:
        return data.library_overview()

    @api.get("/library/{decision}")
    def library_decision(decision: str) -> dict[str, object]:
        return data.library_list(decision)

    @api.get("/library/{decision}/{technique_id}")
    def library_technique(decision: str, technique_id: str) -> dict[str, object]:
        return data.library_technique(decision, technique_id)

    # ------------------------------------------------------------ human tasks
    @api.get("/todo")
    def todo() -> dict[str, object]:
        return data.todo_overview()

    @api.post("/todo/{code}/done")
    def todo_done(code: str, undo: bool = False) -> dict[str, object]:
        """The "done" button - the same function as `videoforge todo done`.

        The only dashboard action that neither runs nor spends anything: it edits a line
        in `todo.md`. It is an unsafe method, so it goes through the same origin check as
        rebuild and publish."""
        return data.close_todo(code, done=not undo)

    # ------------------------------------------------------------ plans and capabilities
    @api.get("/features")
    def features() -> dict[str, object]:
        return data.features_overview()

    @api.get("/features/{feature_id}")
    def feature(feature_id: str) -> dict[str, object]:
        return data.feature_detail(feature_id)

    # ------------------------------------------------------------ useful sources
    @api.get("/links")
    def links() -> dict[str, object]:
        return data.links_overview()

    @api.get("/links/{link_id}")
    def link(link_id: str) -> dict[str, object]:
        return data.link_detail(link_id)

    # ------------------------------------------------------------ inbox files
    @api.get("/inbox")
    def inbox_overview() -> dict[str, object]:
        """What is in inbox/, by day: today first."""
        return inbox.overview()

    @api.post("/inbox")
    async def inbox_upload(request: Request, name: str = "") -> dict[str, object]:
        """File upload: the request body is the file itself, the name is in `?name=`.

        Not multipart, and not to save effort: multipart needs an extra dependency and
        builds a form, while exactly one file is needed here. The body is written to disk
        in chunks as it arrives - phones upload videos, and there is no reason to hold one
        in server memory. If the connection drops midway, the partial file is deleted
        rather than left behind broken."""
        if not name.strip():
            raise HTTPException(status_code=400, detail="нет имени файла (?name=)")
        target = inbox.reserve(name)
        size = 0
        try:
            with target.open("wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    fh.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if size == 0:  # a drop on the first byte looks like success, but there is no file
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="пустой файл: не долетело")
        return inbox.entry(target)

    @api.delete("/inbox/{day}/{name}")
    def inbox_delete(day: str, name: str) -> dict[str, object]:
        """Remove a file. Without cleanup a drop box turns into a dump within a week, and
        there is no Explorer to reach for when the dashboard is open on a phone."""
        return inbox.delete(day, name)

    # ------------------------------------------------------------ benchmark sandbox
    @api.get("/bench")
    def bench_overview() -> dict[str, object]:
        return bench.bench_overview()

    @api.get("/bench/tests/{test_id}")
    def bench_test(test_id: str) -> dict[str, object]:
        return bench.bench_test(test_id)

    @api.get("/bench/text/{rel:path}")
    def bench_text(rel: str) -> dict[str, object]:
        return bench.bench_text(rel)

    @api.get("/bench/{bench_id}")
    def bench_detail(bench_id: str) -> dict[str, object]:
        """Spec-driven benchmark: takes with full provenance, verdicts, summary."""
        return bench.bench_detail(bench_id)

    @api.post("/bench/{bench_id}/takes/{take_id}/verdict")
    def bench_verdict(bench_id: str, take_id: str, body: VerdictBody) -> dict[str, object]:
        """Rate a take by eye -> `takes/<take>.verdict.json`. Only the dashboard writes the
        verdict file, the runner never touches it; the fingerprint rejects overwriting
        someone else's rating with 409, as with the edit plan."""
        try:
            return bench.write_verdict(bench_id, take_id, body.model_dump())
        except bench.Conflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:  # unknown criterion or rating outside pass/fail/na
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    app.include_router(api)
    app.include_router(agent_tasks.router)

    # ------------------------------------------------------------ files
    @app.get("/files/{root}/{rel:path}")
    def files(root: str, rel: str, download: int = 0) -> FileResponse:
        """`?download=1` serves the file as an attachment so the browser saves it rather
        than trying to display it. Without this a phone opens a PDF in its own viewer and
        "download" turns into a hunt through menus."""
        path = data.resolve_file(root, rel)
        return FileResponse(path, filename=path.name if download else None)

    # ------------------------------------------------------------ frontend
    dist = ui_dist if ui_dist is not None else ui_dist_dir()
    if dist.is_dir() and (dist / "index.html").exists():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        def _index() -> FileResponse:
            """index.html is served with revalidation on every request.

            Build file names contain a hash and change with every build, but index.html
            does not: a browser that cached it keeps loading the PREVIOUS build and shows
            the old dashboard. On a phone this looks like "the fix was not made". no-cache
            does not forbid caching; it forces a fingerprint check: unchanged -> 304 and
            the same page from cache."""
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

        @app.exception_handler(StarletteHTTPException)
        async def _spa_fallback(request: Request, exc: StarletteHTTPException) -> Response:
            """SPA routes (/projects/x) do not exist on the server - serve index.html and
            let the browser router handle them. API and file paths stay honest 404s."""
            path = request.url.path
            if exc.status_code == 404 and not path.startswith(("/api/", "/files/", "/assets/")):
                return _index()
            return await http_exception_handler(request, exc)

        @app.get("/")
        def index() -> FileResponse:
            return _index()

    return app
