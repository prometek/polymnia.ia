#!/usr/bin/env python3
"""
Polymnia API (FastAPI) — relational persistence over the generation pipeline.

Run from backend/:  .venv/bin/uvicorn api.main:app --reload
Docs: http://localhost:8000/docs

Layers (project standard): main.py (routes, validation, delegation — no business
logic) -> service.py (pipeline orchestration + persistence) -> db.py (SQL).

Endpoints are sync `def` on purpose: psycopg 3 is sync I/O, so FastAPI runs them in
its threadpool. Do not mix async I/O into these handlers.

Single-tenant for now: all data belongs to a default dev user (schema is multi-tenant
ready; auth wiring comes later).
"""

import glob
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from tasks import generation
from tasks import render as render_jobs

from . import db, job_events, service

DEV_EMAIL = "dev@polymnia.local"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    app.state.user_id = db.ensure_user(DEV_EMAIL)
    for path in glob.glob(os.path.join(service.BACKEND, "inputs", "brand_kit*.json")):
        with open(path, encoding="utf-8") as f:
            db.upsert_brand_kit(json.load(f), app.state.user_id)
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


def get_user_id(request: Request) -> str:
    user_id: str = request.app.state.user_id
    return user_id


UserId = Annotated[str, Depends(get_user_id)]


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


@app.post("/projects", status_code=202, response_model=JobStarted)
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


@app.post("/projects/{pid}/scenes/{order}/ai-edit", response_model=SceneEdited)
def ai_edit(pid: str, order: int, body: AiEdit, video: Video, kit: Kit) -> SceneEdited:
    scene = service.edit_ai(pid, video["scenes"], order, body.instruction, kit)
    return SceneEdited(order=order, scene=scene)


@app.patch("/projects/{pid}/scenes/{order}", response_model=SceneEdited)
def direct_edit(pid: str, order: int, body: DirectEdit, video: Video, kit: Kit) -> SceneEdited:
    scene = service.edit_direct(pid, video["scenes"], order, body.sets, body.swap, kit)
    return SceneEdited(order=order, scene=scene)


@app.post("/projects/{pid}/render", status_code=202, response_model=JobStarted)
def render(pid: str, video: Video, kit: Kit) -> JobStarted:
    if not video["scenes"]:
        raise HTTPException(409, "project has no scenes yet")
    db.set_status(pid, "rendering")
    job_id = db.create_job(pid, "render")
    render_jobs.render_task.delay(job_id, pid, video["scenes"], kit)
    return JobStarted(id=pid, status="rendering")


@app.get("/projects/{pid}/video")
def download_video(video: Video) -> FileResponse:
    if not video["mp4_path"] or not os.path.exists(video["mp4_path"]):
        raise HTTPException(404, "no rendered video yet")
    return FileResponse(video["mp4_path"], media_type="video/mp4", filename=f"{video['id']}.mp4")


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
