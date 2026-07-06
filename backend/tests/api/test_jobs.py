"""Job queue wiring (issue #6 / PRO-06).

The two long tasks (generation, render) must *enqueue* onto the durable queue
instead of running in the request thread, and a `jobs` row must track the work
through its lifecycle (queued -> running -> done/error).
"""

import os
import subprocess
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from api import db, service
from api.celery_app import celery_app
from api.session import engine
from api.storage import get_storage
from sqlalchemy import text
from starlette.testclient import TestClient
from tasks import generation
from tasks import render as render_jobs

import pack_render


def _jobs_for(video_id: str) -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT type, status, error FROM jobs WHERE video_id = :v"), {"v": video_id}
        ).mappings()
        return [dict(r) for r in rows]


def _job_row(video_id: str) -> dict[str, Any]:
    """Full row (incl. `id`/`step`) for a video's (single) job — used by step-progression
    and endpoint tests that need more than `_jobs_for`'s type/status/error subset."""
    with engine.begin() as conn:
        row = (
            conn.execute(
                text("SELECT id, type, status, step, error FROM jobs WHERE video_id = :v"),
                {"v": video_id},
            )
            .mappings()
            .one()
        )
        return dict(row)


def _scene() -> dict[str, Any]:
    return {
        "order": 0,
        "type": "statement",
        "composition": "centered",
        "props": {},
        "asset_refs": [],
        "timing": {"duration_s": 1.0, "audio_path": "audio/s0.wav"},
    }


# --- db layer --------------------------------------------------------------


def test_create_and_transition_job() -> None:
    uid = db.ensure_user("j@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-j", "name": "J"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")

    job_id = db.create_job(vid, "generation")
    assert _jobs_for(vid) == [{"type": "generation", "status": "queued", "error": None}]

    db.set_job_status(job_id, "running")
    assert _jobs_for(vid)[0]["status"] == "running"

    db.set_job_status(job_id, "error", error="boom")
    row = _jobs_for(vid)[0]
    assert row["status"] == "error"
    assert row["error"] == "boom"


# --- endpoints enqueue (no execution) --------------------------------------


def test_new_project_enqueues_generation_job(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = db.ensure_user("a@test.local")
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    as_user(uid)

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(generation.generate_task, "delay", lambda *a: calls.append(a))

    resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-a"})
    assert resp.status_code == 202
    pid = resp.json()["id"]

    # enqueued, not executed in the request thread → a queued job exists
    assert _jobs_for(pid) == [{"type": "generation", "status": "queued", "error": None}]
    assert len(calls) == 1
    _job_id, task_pid, input_text, _kit = calls[0]
    assert (task_pid, input_text) == (pid, "hello")


def test_render_enqueues_render_job(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = db.ensure_user("a@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    db.replace_scenes(vid, [_scene()])
    as_user(uid)

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(render_jobs.render_task, "delay", lambda *a: calls.append(a))

    resp = client.post(f"/projects/{vid}/render")
    assert resp.status_code == 202
    assert _jobs_for(vid) == [{"type": "render", "status": "queued", "error": None}]
    assert len(calls) == 1
    assert calls[0][1] == vid


def test_render_no_scenes_does_not_enqueue(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = db.ensure_user("a@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    as_user(uid)

    calls: list[Any] = []
    monkeypatch.setattr(render_jobs.render_task, "delay", lambda *a: calls.append(a))

    resp = client.post(f"/projects/{vid}/render")
    assert resp.status_code == 409  # no scenes yet
    assert _jobs_for(vid) == []  # nothing enqueued
    assert calls == []


# --- queue routing (issue #7: autonomous generation worker) -----------------


def test_generation_routes_to_its_own_queue() -> None:
    """generation.generate lands on the dedicated `generation` queue so its worker
    (`celery ... -Q generation`) scales independently of the API and render."""
    route = celery_app.amqp.router.route({}, generation.generate_task.name)
    assert route["queue"].name == "generation"


def test_render_routes_to_its_own_queue() -> None:
    """render.render lands on the dedicated `render` queue (issue #8): the
    containerized render worker (`celery ... -Q render`) scales independently."""
    route = celery_app.amqp.router.route({}, render_jobs.render_task.name)
    assert route["queue"].name == "render"


# --- task lifecycle (executed inline via Celery eager) ----------------------


@pytest.fixture
def eager_celery() -> Iterator[None]:
    """Run tasks synchronously in-process (no broker) for a lifecycle assertion."""
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        yield
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False


def test_generation_task_drives_job_to_done(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    eager_celery: None,
) -> None:
    uid = db.ensure_user("a@test.local")
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    as_user(uid)

    # Stub the heavy pipeline: mark the video ready without calling the real LLM/TTS.
    # NB: run_generation's 4th positional arg is `job_id` (issue #9, worker step
    # reporting) — the stub's signature must match the real call site.
    def fake_run(pid: str, input_text: str, kit: dict[str, Any], job_id: str) -> None:
        db.set_status(pid, "ready")

    monkeypatch.setattr(service, "run_generation", fake_run)

    resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-a"})
    assert resp.status_code == 202
    pid = resp.json()["id"]

    # eager task ran inline: queued -> running -> done
    assert _jobs_for(pid) == [{"type": "generation", "status": "done", "error": None}]


def test_render_task_drives_job_to_done(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    eager_celery: None,
) -> None:
    """Analogous to test_generation_task_drives_job_to_done, for the render task —
    same 4-arg (..., job_id) call-site shape (issue #9)."""
    uid = db.ensure_user("a@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    db.replace_scenes(vid, [_scene()])
    as_user(uid)

    def fake_run(pid: str, scenes: list[dict[str, Any]], kit: dict[str, Any], job_id: str) -> None:
        db.set_status(pid, "ready")

    monkeypatch.setattr(service, "run_render", fake_run)

    resp = client.post(f"/projects/{vid}/render")
    assert resp.status_code == 202

    # eager task ran inline: queued -> running -> done
    assert _jobs_for(vid) == [{"type": "render", "status": "done", "error": None}]


# --- GET /jobs/{id} (issue #9 / PRO-08) -------------------------------------


def test_job_status_returns_full_shape(client: TestClient, as_user: Callable[[str], None]) -> None:
    uid = db.ensure_user("a@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    as_user(uid)

    job_id = db.create_job(vid, "generation")
    db.set_job_status(job_id, "running")
    db.set_job_step(job_id, "outline")

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    # Exact shape: {id, type, status, step, video_id, error} — no extra leakage
    # (e.g. no raw user_id, no created_at/updated_at).
    assert resp.json() == {
        "id": job_id,
        "type": "generation",
        "status": "running",
        "step": "outline",
        "video_id": vid,
        "error": None,
    }


def test_job_status_404_unknown_id(client: TestClient, as_user: Callable[[str], None]) -> None:
    uid = db.ensure_user("a@test.local")
    as_user(uid)

    resp = client.get(f"/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_job_status_404_malformed_id(client: TestClient, as_user: Callable[[str], None]) -> None:
    """A non-UUID path param must 404, not 500 (no unhandled parsing exception)."""
    uid = db.ensure_user("a@test.local")
    as_user(uid)

    resp = client.get("/jobs/not-a-valid-uuid")
    assert resp.status_code == 404


def test_job_status_404_other_users_job(client: TestClient, as_user: Callable[[str], None]) -> None:
    """A job that exists but belongs to another user must 404 — same response as an
    unknown id (no existence leak)."""
    owner_id = db.ensure_user("owner@test.local")
    owner_version_id = db.upsert_brand_kit({"id": "kit-owner", "name": "Owner"}, owner_id)
    owner_vid = db.uuid.uuid4().hex[:12]
    db.create_video(owner_vid, owner_id, owner_version_id, "v")
    other_job_id = db.create_job(owner_vid, "generation")

    requester_id = db.ensure_user("requester@test.local")
    as_user(requester_id)

    resp = client.get(f"/jobs/{other_job_id}")
    assert resp.status_code == 404


# --- Step progression end-to-end (issue #9) ---------------------------------


def test_generation_step_progresses_through_pipeline(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    eager_celery: None,
) -> None:
    """Drives the real worker path (tasks.generation.generate_task -> service.run_generation
    -> service.generate) with only the external LLM/TTS boundaries stubbed, and asserts
    `jobs.step` advances through plan -> outline -> fill -> tts, in order."""
    uid = db.ensure_user("a@test.local")
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    as_user(uid)

    def fake_generate_plan(source_text: str) -> dict[str, Any]:
        return {"topic": source_text}

    def fake_build_outline(plan: dict[str, Any], kicker_style: str) -> dict[str, Any]:
        return {"scenes": [{"order": 0, "type": "statement"}]}

    def fake_fill_scene(
        scene: dict[str, Any], brand_kit: dict[str, Any], instruction: str | None = None
    ) -> dict[str, Any]:
        return {**scene, "composition": "centered", "props": {}, "asset_refs": []}

    def fake_voiceover_scene(scene: dict[str, Any], audio_dir: str) -> dict[str, Any]:
        # Real tts.voiceover_scene writes a real WAV under `audio_dir` and returns
        # that on-disk path (api/service.py::_voiceover_and_store then reads it and
        # promotes it into Storage under a key, issue #12) — a fake that returns a
        # path nothing was ever written to breaks that promotion with a
        # FileNotFoundError. Match the real contract here.
        os.makedirs(audio_dir, exist_ok=True)
        path = os.path.join(audio_dir, f"scene-{scene.get('order')}.wav")
        with open(path, "wb") as f:
            f.write(b"fake-wav-bytes")
        return {**scene, "timing": {"duration_s": 1.0, "audio_path": path}}

    monkeypatch.setattr(service.generate_plan, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(service.outline, "build_outline", fake_build_outline)
    monkeypatch.setattr(service.fill, "fill_scene", fake_fill_scene)
    monkeypatch.setattr(service.tts, "voiceover_scene", fake_voiceover_scene)

    seen_steps: list[str] = []
    real_set_job_step = db.set_job_step

    def tracking_set_job_step(job_id: str, step: str) -> None:
        seen_steps.append(step)
        real_set_job_step(job_id, step)

    monkeypatch.setattr(db, "set_job_step", tracking_set_job_step)

    resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-a"})
    assert resp.status_code == 202
    pid = resp.json()["id"]

    assert seen_steps == ["plan", "outline", "fill", "tts"]

    row = _job_row(pid)
    assert row["status"] == "done"
    assert row["step"] == "tts"  # last step written, still visible after completion
    video = db.get_video(pid, uid)
    assert video is not None
    assert video["status"] == "ready"

    # issue #12: the persisted scene's audio_path is a Storage KEY, not the tts.py
    # scratch filesystem path — and the bytes are actually retrievable through the
    # abstraction, not just present on disk at the scratch location.
    scene = video["scenes"][0]
    audio_key = scene["timing"]["audio_path"]
    assert audio_key == f"projects/{pid}/audio/scene-0.wav"
    assert not os.path.isabs(audio_key)
    assert get_storage().get(audio_key) == b"fake-wav-bytes"


def test_render_step_progresses_through_pipeline(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    eager_celery: None,
    tmp_path: Any,
) -> None:
    """Drives the real worker path (tasks.render.render_task -> service.run_render ->
    service.render_project) with only the Remotion subprocess stubbed, and asserts
    `jobs.step` advances through packing -> render, in order."""
    uid = db.ensure_user("a@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")

    # issue #12: `timing.audio_path` is a Storage KEY (set by generation, via
    # _voiceover_and_store), never a raw filesystem path — put the bytes through
    # the same abstraction render_project() will read them back through.
    audio_key = f"projects/{vid}/audio/scene-0.wav"
    get_storage().put(audio_key, b"not-a-real-wav")
    scene = {
        "order": 0,
        "type": "statement",
        "composition": "centered",
        "props": {},
        "asset_refs": [],
        "timing": {"duration_s": 1.0, "audio_path": audio_key},
    }
    db.replace_scenes(vid, [scene])
    as_user(uid)

    # Redirect packing output away from the real render-motor tree (POC layout keeps
    # PUBLIC/RENDER_DIR as module attributes read at call time via a local `from
    # pack_render import ...`, so monkeypatching the module is enough).
    public_dir = tmp_path / "public"
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    monkeypatch.setattr(pack_render, "PUBLIC", str(public_dir))
    monkeypatch.setattr(pack_render, "RENDER_DIR", str(render_dir))

    rendered_mp4_bytes = b"fake-mp4-bytes"
    subprocess_calls: list[list[str]] = []

    def fake_subprocess_run(
        cmd: list[str], cwd: str | None = None, check: bool | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        subprocess_calls.append(cmd)
        # Stand in for the real Remotion subprocess, which writes the MP4 to its own
        # local out/ dir (render_project() then promotes that file into Storage,
        # issue #12) — write it here so that promotion has something real to read.
        out_dir = render_dir / "out"
        out_dir.mkdir(exist_ok=True)
        (out_dir / f"{vid}.mp4").write_bytes(rendered_mp4_bytes)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(service.subprocess, "run", fake_subprocess_run)

    seen_steps: list[str] = []
    real_set_job_step = db.set_job_step

    def tracking_set_job_step(job_id: str, step: str) -> None:
        seen_steps.append(step)
        real_set_job_step(job_id, step)

    monkeypatch.setattr(db, "set_job_step", tracking_set_job_step)

    resp = client.post(f"/projects/{vid}/render")
    assert resp.status_code == 202

    assert seen_steps == ["packing", "render"]
    assert subprocess_calls and subprocess_calls[0][:3] == ["npx", "remotion", "render"]

    row = _job_row(vid)
    assert row["status"] == "done"
    assert row["step"] == "render"  # last step written, still visible after completion
    video = db.get_video(vid, uid)
    assert video["status"] == "ready"

    # issue #12: mp4_path persisted in the DB is a Storage KEY, not the Remotion
    # subprocess's local out/ path — and the bytes are retrievable both directly
    # through Storage and through the real download endpoint (byte-identical).
    video_key = video["mp4_path"]
    assert video_key == f"projects/{vid}/render.mp4"
    assert get_storage().get(video_key) == rendered_mp4_bytes

    download = client.get(f"/projects/{vid}/video")
    assert download.status_code == 200
    assert download.content == rendered_mp4_bytes
