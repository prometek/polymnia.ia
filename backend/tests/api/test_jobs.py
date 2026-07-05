"""Job queue wiring (issue #6 / PRO-06).

The two long tasks (generation, render) must *enqueue* onto the durable queue
instead of running in the request thread, and a `jobs` row must track the work
through its lifecycle (queued -> running -> done/error).
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from api import db, service
from api.celery_app import celery_app
from api.session import engine
from sqlalchemy import text
from starlette.testclient import TestClient
from tasks import generation
from tasks import render as render_jobs


def _jobs_for(video_id: str) -> list[dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT type, status, error FROM jobs WHERE video_id = :v"), {"v": video_id}
        ).mappings()
        return [dict(r) for r in rows]


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


def test_render_stays_on_default_queue() -> None:
    """Render is unrouted for now → default `celery` queue (own queue = PRO-07)."""
    route = celery_app.amqp.router.route({}, render_jobs.render_task.name)
    assert route["queue"].name == "celery"


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
    def fake_run(pid: str, input_text: str, kit: dict[str, Any]) -> None:
        db.set_status(pid, "ready")

    monkeypatch.setattr(service, "run_generation", fake_run)

    resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-a"})
    assert resp.status_code == 202
    pid = resp.json()["id"]

    # eager task ran inline: queued -> running -> done
    assert _jobs_for(pid) == [{"type": "generation", "status": "done", "error": None}]
