#!/usr/bin/env python3
"""
Polymnia API (FastAPI) — relational persistence over the generation pipeline.

Run from backend/:  .venv/bin/uvicorn api.main:app --reload
Docs: http://localhost:8000/docs

Layers (project standard): main.py (routes, validation, delegation — no business
logic) -> service.py (pipeline orchestration + persistence) -> db.py (SQL).

Endpoints are sync `def` on purpose: psycopg 3 is sync I/O, so FastAPI runs them in
its threadpool. Do not mix async I/O into these handlers.

Authentication (issue #16): `get_current_user` resolves every request to a local
user id — via a verified Clerk session token (`AUTH_MODE=clerk`, default) or a
single configured dev identity (`AUTH_MODE=dev`, local `./run.sh`/uvicorn only). See
`api/auth.py` for token verification.

Rate limiting (issue #17): the three job-triggering endpoints (`POST /projects`,
`.../render`, `.../scenes/{order}/ai-edit`) carry a `rate_limited(scope)`
dependency — a per-user sliding-window quota shared via Redis (see
`api/rate_limit.py`), so cost-bearing LLM/TTS/render calls stay bounded even
behind several stateless API instances. Read (`GET`) endpoints are unaffected.
"""

import glob
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from tasks import generation
from tasks import render as render_jobs

from . import auth, db, job_events, queue_metrics, rate_limit, service
from .storage import StorageKeyNotFoundError, get_storage

# How long a redirect to a CloudFront signed URL stays valid (issue #12/#14) — long
# enough for a client to start streaming the video, short enough that a leaked link
# doesn't grant durable access to a private bucket object. Configurable per
# environment (e.g. a slower prod network needs more headroom than dev).
VIDEO_SIGNED_URL_TTL_S = int(os.environ.get("STORAGE_CLOUDFRONT_SIGNED_URL_TTL_S", "300"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    # Dev-only seeding (issue #16): the old unconditional DEV_EMAIL seed is now
    # gated behind AUTH_MODE=dev — a `clerk`-mode (prod) boot never creates or
    # depends on this local identity, and brand kits ship with real users instead.
    if auth.AUTH_MODE == "dev":
        dev_user_id = db.ensure_user(auth.DEV_EMAIL)
        for path in glob.glob(os.path.join(service.BACKEND, "inputs", "brand_kit*.json")):
            with open(path, encoding="utf-8") as f:
                db.upsert_brand_kit(json.load(f), dev_user_id)
    yield


app = FastAPI(title="Polymnia API", lifespan=lifespan)


# --- Schemas ---------------------------------------------------------------


class CreateProject(BaseModel):
    input_text: str
    brand_kit_id: str
    name: str | None = None


class AiEdit(BaseModel):
    instruction: str


class DirectEdit(BaseModel):
    sets: dict[str, str] | None = None  # {"items.0.text": "..."}  (path within props)
    swap: str | None = None  # icon asset id


class JobStarted(BaseModel):
    id: str
    status: str
    brand_kit_version_id: str | None = None


class SceneEdited(BaseModel):
    order: int
    scene: dict[str, Any]


class BrandKitCreated(BaseModel):
    id: str
    version_id: str


class BrandKitSummary(BaseModel):
    id: str
    name: str | None = None


class VideoSummary(BaseModel):
    id: str
    name: str | None = None
    brand_kit_version_id: str
    status: str
    total_duration_s: float
    mp4_path: str | None = None
    updated_at: datetime


class VideoRead(VideoSummary):
    scenes: list[dict[str, Any]]


class JobStatus(BaseModel):
    id: str
    type: str
    status: str
    step: str | None = None
    video_id: str
    error: str | None = None


# --- Dependencies ----------------------------------------------------------


def get_current_user(request: Request) -> str:
    """Resolve the authenticated caller for this request -> local `users.id`
    (issue #16). This is the app's single user seam: every route depends on it
    (directly or via `UserId`), and tests override this exact dependency (see
    `tests/conftest.py`'s `as_user` fixture) to act as a chosen user without a
    live Clerk instance.

    `AUTH_MODE=clerk` (default): verifies the request's bearer/session token via
    the Clerk SDK (`api/auth.py`) and maps the stable Clerk identity to a local
    user, creating one on first login. `AUTH_MODE=dev`: resolves the single
    configured dev identity instead — local `./run.sh`/uvicorn only, never a
    fallback taken from within `clerk` mode (a missing/invalid token there is
    always a 401, never silently treated as "use the dev user"). `AUTH_MODE`
    itself is validated once at import (`api/auth.py`), not per-request here — an
    unknown value fails the process at boot, not on the first inbound request.
    """
    if auth.AUTH_MODE == "dev":
        return db.ensure_user(auth.DEV_EMAIL)
    identity = auth.verify_clerk_request(request)
    return db.get_or_create_user_by_clerk_id(identity.clerk_user_id, identity.email)


UserId = Annotated[str, Depends(get_current_user)]


def rate_limited(scope: str) -> Callable[[UserId], None]:
    """Dependency factory: wrap in `Depends(...)` and pass via a route's
    `dependencies=[...]` (issue #17) for a per-user sliding-window rate limit,
    shared across API instances via Redis (see `api/rate_limit.py`). `scope`
    names the budget (isolates e.g. renders from project creation); over quota
    -> `HTTPException(429)` with `Retry-After`.

    Depends on `UserId`, already resolved once per request by the route's own
    handler/other dependencies (FastAPI's per-request dependency cache) — no
    extra Clerk verification round trip just to rate-limit.
    """

    def check(user_id: UserId) -> None:
        rate_limit.enforce(scope, user_id)

    return check


def require_video(pid: str, user_id: UserId) -> dict[str, Any]:
    v = db.get_video(pid, user_id)
    if not v:  # unknown id OR owned by another user → same 404 (no existence leak)
        raise HTTPException(404, "project not found")
    return v


def require_kit(
    video: Annotated[dict[str, Any], Depends(require_video)], user_id: UserId
) -> dict[str, Any]:
    kit = db.kit_from_version(video["brand_kit_version_id"], user_id)
    if not kit:
        raise HTTPException(409, "brand kit version missing")
    return kit


def require_job(job_id: str, user_id: UserId) -> dict[str, Any]:
    job = db.get_job(job_id, user_id)
    if not job:  # unknown id, malformed uuid, OR owned by another user → same 404
        raise HTTPException(404, "job not found")
    return job


Video = Annotated[dict[str, Any], Depends(require_video)]  # cached per request → fetched once
Kit = Annotated[dict[str, Any], Depends(require_kit)]
JobRead = Annotated[dict[str, Any], Depends(require_job)]


# --- Brand kits ------------------------------------------------------------


@app.get("/brand-kits", response_model=list[BrandKitSummary])
def brand_kits(user_id: UserId) -> list[dict[str, Any]]:
    return db.list_brand_kits(user_id)


@app.post("/brand-kits", status_code=201, response_model=BrandKitCreated)
def create_brand_kit(kit: dict[str, Any], user_id: UserId) -> BrandKitCreated:
    if "id" not in kit:
        raise HTTPException(400, "brand kit needs an 'id'")
    service.bake_kit_assets(kit)  # logo/background → Storage keys (issue #15)
    version_id = db.upsert_brand_kit(kit, user_id)
    return BrandKitCreated(id=kit["id"], version_id=version_id)


@app.get("/brand-kits/{bk_id}/assets")
def brand_kit_assets(bk_id: str, user_id: UserId) -> list[dict[str, Any]]:
    # Asset shape is dynamic (kit-specific meta merged in) → no response_model.
    version_id = db.latest_version_id(bk_id, user_id)
    if not version_id:
        raise HTTPException(404, f"brand kit '{bk_id}' not found")
    return db.assets_of_version(version_id)


# --- Projects (videos) -----------------------------------------------------


@app.get("/projects", response_model=list[VideoSummary])
def projects(user_id: UserId) -> list[dict[str, Any]]:
    return db.list_videos(user_id)


@app.get("/projects/{pid}", response_model=VideoRead)
def project(video: Video) -> dict[str, Any]:
    return video


@app.post(
    "/projects",
    status_code=202,
    response_model=JobStarted,
    dependencies=[Depends(rate_limited("projects:create"))],
)
def new_project(body: CreateProject, user_id: UserId) -> JobStarted:
    version_id = db.latest_version_id(body.brand_kit_id, user_id)
    if not version_id:
        raise HTTPException(404, f"brand kit '{body.brand_kit_id}' not found")
    kit = db.kit_from_version(version_id, user_id)
    if not kit:
        raise HTTPException(409, "brand kit version missing")
    pid = uuid.uuid4().hex[:12]
    db.create_video(pid, user_id, version_id, body.name or "Untitled")
    # Enqueue on the durable queue (issue #6) — survives an API restart, unlike the
    # former in-process BackgroundTasks. A worker picks it up (workers = PRO-06/07).
    job_id = db.create_job(pid, "generation")
    generation.generate_task.delay(job_id, pid, body.input_text, kit)
    return JobStarted(id=pid, status="generating", brand_kit_version_id=version_id)


@app.post(
    "/projects/{pid}/scenes/{order}/ai-edit",
    response_model=SceneEdited,
    dependencies=[Depends(rate_limited("scenes:ai-edit"))],
)
def ai_edit(pid: str, order: int, body: AiEdit, video: Video, kit: Kit) -> SceneEdited:
    scene = service.edit_ai(pid, video["scenes"], order, body.instruction, kit)
    return SceneEdited(order=order, scene=scene)


@app.patch("/projects/{pid}/scenes/{order}", response_model=SceneEdited)
def direct_edit(pid: str, order: int, body: DirectEdit, video: Video, kit: Kit) -> SceneEdited:
    scene = service.edit_direct(pid, video["scenes"], order, body.sets, body.swap, kit)
    return SceneEdited(order=order, scene=scene)


@app.post(
    "/projects/{pid}/render",
    status_code=202,
    response_model=JobStarted,
    dependencies=[Depends(rate_limited("projects:render"))],
)
def render(pid: str, video: Video, kit: Kit) -> JobStarted:
    if not video["scenes"]:
        raise HTTPException(409, "project has no scenes yet")
    db.set_status(pid, "rendering")
    job_id = db.create_job(pid, "render")
    render_jobs.render_task.delay(job_id, pid, video["scenes"], kit)
    return JobStarted(id=pid, status="rendering")


@app.get("/projects/{pid}/video")
def download_video(video: Video) -> Response:
    """Serve the rendered MP4 via Storage (issue #12), not a raw filesystem path —
    `mp4_path` is a storage key, resolved by whichever backend `STORAGE_BACKEND`
    selects.

    The two backends are served differently on purpose (Storage.local_path is the
    seam): local dev streams the file straight off disk via `FileResponse`, which
    gives HTTP Range support (in-browser seeking) for free and never loads the whole
    MP4 into process memory; S3 has no local filesystem representation, so we
    `302`-redirect to a short-lived CloudFront signed URL instead of proxying bytes
    through this process (issue #14 / architecture §12 — private bucket, CDN + signed
    URL delivery; the bucket itself is never reachable directly).
    """
    key = video["mp4_path"]
    if not key:
        raise HTTPException(404, "no rendered video yet")
    storage = get_storage()
    filename = f"{video['id']}.mp4"
    try:
        path = storage.local_path(key)
        # `local_path` returns None for backends with no filesystem representation
        # (S3) without checking existence — verify here so a dangling key (recorded
        # in the DB but never uploaded, or since deleted from the bucket) 404s the
        # same way the local backend already does, instead of redirecting to a
        # signed URL for an object that isn't there. One extra round trip
        # (`head_object`), paid only on this branch — local dev never hits it.
        if path is None and not storage.exists(key):
            raise StorageKeyNotFoundError(key)
    except StorageKeyNotFoundError as exc:
        # Dangling `mp4_path` (recorded in the DB but absent from the backing store)
        # — surface the same 404 a caller would see for "never rendered".
        raise HTTPException(404, "no rendered video yet") from exc
    if path is not None:
        return FileResponse(path, media_type="video/mp4", filename=filename)
    # 302, not 307: this is a delivery redirect to a fresh short-lived URL every time,
    # never a semantic "resource permanently lives elsewhere" — 307 would additionally
    # imply clients should replay the exact method/body, which doesn't matter for a
    # GET but isn't the contract we want callers to rely on (issue #14 acceptance
    # criteria are explicit about 302).
    return RedirectResponse(storage.signed_url(key, VIDEO_SIGNED_URL_TTL_S), status_code=302)


# --- Metrics -----------------------------------------------------------------


@app.get("/metrics/queues")
def queue_metrics_endpoint() -> dict[str, int]:
    """Queue-depth metric (issue #11 / PRO-10): pending job count per Celery queue,
    for later consumption by dashboards/alerts (PRO-23) — not user/tenant data
    (it's an operational signal about the queue, not a project), so unlike the
    endpoints below it isn't scoped behind `UserId`/ownership.
    """
    return queue_metrics.queue_depths()


# --- Jobs --------------------------------------------------------------------


@app.get("/jobs/{job_id}", response_model=JobStatus)
def job_status(job: JobRead) -> dict[str, Any]:
    return job


@app.get("/jobs/{job_id}/stream")
async def job_status_stream(job_id: str, job: JobRead, user_id: UserId) -> StreamingResponse:
    """Real-time relay of a job's status/step transitions via SSE (issue #10) —
    the polling endpoint above stays the fallback (ticket scope), unchanged.

    `async def` on purpose, unlike the sync `def` convention for this API's other
    handlers: this is a long-lived stream waiting on Redis pub/sub, not a short
    blocking DB call, so it must not occupy a threadpool thread for its whole
    lifetime. `job` (via `require_job`, a sync dependency FastAPI already runs in
    the threadpool) still checks existence/ownership once, up front, before the
    stream opens — its value isn't otherwise used: `event_stream` takes its own
    fresh snapshot *after* subscribing (see its docstring for why the ordering
    matters), via the injected `snapshot` callback below rather than this value,
    to avoid ever emitting a snapshot older than the point we started listening.
    """
    return StreamingResponse(
        job_events.event_stream(job_id, lambda: db.get_job(job_id, user_id)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering (e.g. nginx) of the stream
        },
    )
