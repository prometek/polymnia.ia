"""Bounded retries + dead-letter queue (issue #11 / PRO-10).

Acceptance criteria under test:
  1. A task that fails repeatedly gets a bounded number of attempts
     (max_retries + 1), never an infinite retry loop, and the bound differs
     per task type (generation vs render) per their `_MAX_RETRIES`.
  2. Once retries are exhausted the job moves to `dead`, its error is
     persisted, and it stays inspectable via the existing read path
     (`GET /jobs/{id}` / `api.db.get_job`) -- no new endpoint.
  3. A queue-depth metric is exposed per Celery queue (`GET /metrics/queues`).
  4. `dead` is a terminal status for the SSE job stream, exactly like
     `done`/`error` -- the stream stops, it doesn't hang waiting for more.
  5. Fix cycle 1: an intermediate failed attempt writes the non-terminal
     `retrying` status (not `error`) -- the SSE stream must keep listening
     through it, not treat it as terminal -- and a job that then recovers
     within its budget must report `status=done` with `error=None` (no stale
     message from an abandoned attempt), including through the actual
     `GET /jobs/{id}` read path, not just the DB row.
  6. Fix cycle 1: `DeadLetterTask._extract_job_id` must accept `job_id` as
     either the first positional arg or a `job_id` kwarg, and raise an
     explicit `ValueError` (not an opaque `IndexError`/`KeyError`) when
     neither is present.

Retry-exhaustion is exercised via Celery's own eager-execution recursion
(`task_always_eager` + `task_eager_propagates=False`), not a live broker: with
`propagate=False`, `Task.apply()` recurses on its own `Retry` signature
(`retval.sig.apply(retries=retries + 1)`, see `celery.app.task.Task.apply`),
so the task's `run` really gets invoked once per attempt, retry backoff
scheduling included, but with no actual `sleep` -- deterministic and fast.
This is deliberately a *different* eager fixture from the one shared in
test_jobs.py (`task_eager_propagates=True`): that one is for asserting a
single-attempt happy path; `propagate=False` is what's needed here to let the
retry recursion (and therefore `on_failure`) actually run instead of the
`Retry` control-flow exception escaping the first `.apply()` call.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from api import db, job_events, queue_metrics
from api.celery_app import celery_app
from api.session import engine
from sqlalchemy import text
from starlette.testclient import TestClient
from tasks import generation
from tasks import render as render_jobs
from tasks.base import DeadLetterTask


def _job_row(video_id: str) -> dict[str, Any]:
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


@pytest.fixture
def eager_celery_retrying() -> Iterator[None]:
    """Like test_jobs.py's `eager_celery`, but with `task_eager_propagates=False`
    so a task's own retry recursion actually runs to exhaustion in-process
    (see module docstring) instead of the first `Retry` escaping immediately.
    """
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    try:
        yield
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False


# --- 1. Bounded retries, per task type ---------------------------------------


def test_generation_task_retries_are_bounded_then_job_goes_dead(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    eager_celery_retrying: None,
) -> None:
    """A generation job whose pipeline call always raises must be attempted
    exactly `_MAX_RETRIES + 1` times (bounded, no infinite loop), and end in
    `dead` with the failure persisted as its error."""
    uid = db.ensure_user("a@test.local")
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    as_user(uid)

    attempts = {"n": 0}

    def always_fails(pid: str, input_text: str, kit: dict[str, Any], job_id: str) -> None:
        attempts["n"] += 1
        raise RuntimeError("mistral unavailable")

    monkeypatch.setattr("api.service.run_generation", always_fails)

    resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-a"})
    assert resp.status_code == 202
    pid = resp.json()["id"]

    assert attempts["n"] == generation._MAX_RETRIES + 1  # bounded, never more

    row = _job_row(pid)
    assert row["status"] == "dead"  # DLQ, not stuck on the last attempt's "error"
    assert "mistral unavailable" in (row["error"] or "")


def test_render_task_retries_are_bounded_then_job_goes_dead(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    eager_celery_retrying: None,
) -> None:
    """Same guarantee, for the render task, whose bound is smaller (issue #11:
    render is the costliest workload to redo)."""
    uid = db.ensure_user("a@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    db.replace_scenes(vid, [_scene()])
    as_user(uid)

    attempts = {"n": 0}

    def always_fails(
        pid: str, scenes: list[dict[str, Any]], kit: dict[str, Any], job_id: str
    ) -> None:
        attempts["n"] += 1
        raise RuntimeError("remotion render crashed")

    monkeypatch.setattr("api.service.run_render", always_fails)

    resp = client.post(f"/projects/{vid}/render")
    assert resp.status_code == 202

    assert attempts["n"] == render_jobs._MAX_RETRIES + 1  # bounded, never more

    row = _job_row(vid)
    assert row["status"] == "dead"
    assert "remotion render crashed" in (row["error"] or "")


def test_generation_task_recovers_and_does_not_go_dead_if_it_succeeds_within_the_budget(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    eager_celery_retrying: None,
) -> None:
    """Retry exists to absorb *transient* failures (issue #11: LLM/TTS
    hiccups) -- a task that fails a couple of times but then succeeds, still
    within its retry budget, must end `done`, not `dead`. This is the
    complementary case to the exhaustion tests above: it proves
    `DeadLetterTask.on_failure` only fires on a *permanent* failure, not on
    every individual attempt."""
    uid = db.ensure_user("a@test.local")
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    as_user(uid)

    attempts = {"n": 0}

    def fails_twice_then_succeeds(
        pid: str, input_text: str, kit: dict[str, Any], job_id: str
    ) -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError("transient mistral hiccup")
        db.set_status(pid, "ready")

    monkeypatch.setattr("api.service.run_generation", fails_twice_then_succeeds)

    resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-a"})
    pid = resp.json()["id"]

    assert attempts["n"] == 3  # 2 failures + 1 recovering attempt, well within max_retries=3
    row = _job_row(pid)
    assert row["status"] == "done"  # recovered -- not dead-lettered
    assert row["error"] is None


def test_retry_bound_differs_between_generation_and_render() -> None:
    """The ticket requires the bound to be set *per task type* -- assert the
    two are actually different, not the same constant duplicated."""
    assert generation._MAX_RETRIES != render_jobs._MAX_RETRIES
    assert generation._MAX_RETRIES == 3
    assert render_jobs._MAX_RETRIES == 2


def test_generation_and_render_get_a_different_number_of_attempts(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    eager_celery_retrying: None,
) -> None:
    """End-to-end: drive both task types to exhaustion in the same test and
    assert their observed attempt counts actually differ, closing the loop
    between the per-type constants and real runtime behaviour."""
    uid = db.ensure_user("a@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    as_user(uid)

    gen_attempts = {"n": 0}

    def gen_fails(pid: str, input_text: str, kit: dict[str, Any], job_id: str) -> None:
        gen_attempts["n"] += 1
        raise RuntimeError("boom-generation")

    monkeypatch.setattr("api.service.run_generation", gen_fails)
    resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-a"})
    gen_pid = resp.json()["id"]

    render_attempts = {"n": 0}
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    db.replace_scenes(vid, [_scene()])

    def render_fails(
        pid: str, scenes: list[dict[str, Any]], kit: dict[str, Any], job_id: str
    ) -> None:
        render_attempts["n"] += 1
        raise RuntimeError("boom-render")

    monkeypatch.setattr("api.service.run_render", render_fails)
    client.post(f"/projects/{vid}/render")

    assert gen_attempts["n"] != render_attempts["n"]
    assert _job_row(gen_pid)["status"] == "dead"
    assert _job_row(vid)["status"] == "dead"


# --- 2. dead job is inspectable via GET /jobs/{id} ---------------------------


def test_dead_job_is_inspectable_via_job_status_endpoint(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    eager_celery_retrying: None,
) -> None:
    """End-to-end: after retries are exhausted, `GET /jobs/{id}` (the existing
    job-read path, issue #9) must report status `dead` and the persisted
    error -- no separate DLQ endpoint needed."""
    uid = db.ensure_user("a@test.local")
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    as_user(uid)

    def always_fails(pid: str, input_text: str, kit: dict[str, Any], job_id: str) -> None:
        raise RuntimeError("llm down")

    monkeypatch.setattr("api.service.run_generation", always_fails)

    resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-a"})
    pid = resp.json()["id"]
    job_id = _job_row(pid)["id"]

    job_resp = client.get(f"/jobs/{job_id}")
    assert job_resp.status_code == 200
    body = job_resp.json()
    assert body["status"] == "dead"
    assert body["error"] is not None and "llm down" in body["error"]


def test_dead_status_survives_a_direct_db_read_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lower-level check of the same guarantee, independent of the HTTP layer:
    `api.db.get_job` (what the endpoint delegates to) must surface `dead` +
    error directly."""
    uid = db.ensure_user("b@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-b", "name": "B"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    job_id = db.create_job(vid, "generation")

    db.set_job_status(job_id, "dead", error="exhausted retries: boom")

    job = db.get_job(job_id, uid)
    assert job is not None
    assert job["status"] == "dead"
    assert job["error"] == "exhausted retries: boom"


# --- 3. queue-depth metric ----------------------------------------------------


class _FakeRedisLLen:
    """Stands in for the sync Redis client `queue_metrics._redis_client()`
    returns -- fakes only the `LLEN` boundary (no live Redis in this test
    environment), matching the pattern already used for the pub/sub boundary
    in test_job_events.py."""

    def __init__(self, depths: dict[str, int]) -> None:
        self._depths = depths

    def llen(self, queue_name: str) -> int:
        return self._depths.get(queue_name, 0)


def test_queue_depths_reports_per_queue_length(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedisLLen({"generation": 5, "render": 2})
    monkeypatch.setattr(queue_metrics, "_redis_client", lambda: fake)

    assert queue_metrics.queue_depths() == {"generation": 5, "render": 2}


def test_queue_depths_reports_zero_for_an_empty_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedisLLen({})  # nothing pending on either queue
    monkeypatch.setattr(queue_metrics, "_redis_client", lambda: fake)

    assert queue_metrics.queue_depths() == {"generation": 0, "render": 0}


def test_queue_metrics_endpoint_exposes_per_queue_depth_end_to_end(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: `GET /metrics/queues` (no auth/ownership scoping -- it's an
    operational signal, not tenant data) must reflect the broker's actual
    per-queue pending count, with something genuinely sitting on the queue."""
    fake = _FakeRedisLLen({"generation": 3, "render": 0})
    monkeypatch.setattr(queue_metrics, "_redis_client", lambda: fake)

    resp = client.get("/metrics/queues")
    assert resp.status_code == 200
    assert resp.json() == {"generation": 3, "render": 0}


def test_queue_metrics_endpoint_reflects_a_change_in_queue_depth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a fixed snapshot: a later poll after the depth changes (e.g. more
    jobs enqueued) must reflect the new value."""
    depths = {"generation": 1, "render": 0}
    fake = _FakeRedisLLen(depths)
    monkeypatch.setattr(queue_metrics, "_redis_client", lambda: fake)

    first = client.get("/metrics/queues").json()
    depths["generation"] = 9
    second = client.get("/metrics/queues").json()

    assert first == {"generation": 1, "render": 0}
    assert second == {"generation": 9, "render": 0}


# --- 4. `dead` is terminal for the SSE stream ---------------------------------


class _FakeBus:
    """Minimal in-process pub/sub stand-in, same shape as test_job_stream.py's
    `_FakeBus` -- kept local/minimal here since this file only needs one
    terminal-status scenario, not the full stream test surface."""

    def __init__(self) -> None:
        import queue as queue_mod
        import threading

        self._listeners: dict[str, list[Any]] = {}
        self._lock = threading.Lock()
        self.subscribed = threading.Event()
        self._queue_mod = queue_mod

    def publish(self, channel: str, data: str) -> None:
        with self._lock:
            queues = list(self._listeners.get(channel, ()))
        for q in queues:
            q.put(data)

    def attach(self, channel: str) -> Any:
        q = self._queue_mod.Queue()
        with self._lock:
            self._listeners.setdefault(channel, []).append(q)
        self.subscribed.set()
        return q


class _FakePubSub:
    def __init__(self, bus: _FakeBus) -> None:
        self._bus = bus
        self._queue: Any = None

    async def subscribe(self, channel: str) -> None:
        self._queue = self._bus.attach(channel)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float = 15.0
    ) -> dict[str, Any] | None:
        import asyncio
        import queue as queue_mod

        def _blocking_get() -> str | None:
            try:
                return self._queue.get(timeout=timeout)
            except queue_mod.Empty:
                return None

        raw = await asyncio.to_thread(_blocking_get)
        return None if raw is None else {"type": "message", "data": raw}

    async def unsubscribe(self, channel: str) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _FakeAsyncRedisClient:
    def __init__(self, bus: _FakeBus) -> None:
        self._bus = bus

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self._bus)

    async def aclose(self) -> None:
        pass


class _FakeRedisPublisher:
    def __init__(self, bus: _FakeBus) -> None:
        self._bus = bus

    def publish(self, channel: str, data: str) -> None:
        self._bus.publish(channel, data)


@pytest.fixture
def fake_pubsub(monkeypatch: pytest.MonkeyPatch) -> _FakeBus:
    bus = _FakeBus()
    monkeypatch.setattr(job_events, "_publisher_client", lambda: _FakeRedisPublisher(bus))
    monkeypatch.setattr(
        job_events.aioredis, "from_url", lambda *a, **kw: _FakeAsyncRedisClient(bus)
    )
    return bus


def test_event_stream_treats_dead_as_terminal_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit-level: `job_events.event_stream` must close right after a `dead`
    transition, exactly like `done`/`error` -- not hang waiting for a message
    that (per the DLQ, issue #11) will never arrive for a dead-lettered job."""
    import asyncio
    import json
    import queue as queue_mod

    q: queue_mod.Queue[str] = queue_mod.Queue()

    class _Pub:
        async def subscribe(self, channel: str) -> None:
            pass

        async def get_message(
            self, ignore_subscribe_messages: bool = True, timeout: float = 15.0
        ) -> dict[str, Any] | None:
            def _blocking_get() -> str | None:
                try:
                    return q.get(timeout=timeout)
                except queue_mod.Empty:
                    return None

            raw = await asyncio.to_thread(_blocking_get)
            return None if raw is None else {"type": "message", "data": raw}

        async def unsubscribe(self, channel: str) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class _Client:
        def pubsub(self) -> _Pub:
            return _Pub()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(job_events.aioredis, "from_url", lambda *a, **kw: _Client())
    q.put(json.dumps({"id": "job-1", "status": "dead", "step": "tts", "error": "exhausted"}))

    async def _collect() -> list[bytes]:
        job = {"id": "job-1", "status": "running", "step": "tts"}
        return [chunk async for chunk in job_events.event_stream("job-1", lambda: job)]

    chunks = asyncio.run(_collect())

    payloads = []
    for chunk in chunks:
        for line in chunk.decode().splitlines():
            if line.startswith("data: "):
                payloads.append(json.loads(line.removeprefix("data: ")))
    assert [p["status"] for p in payloads] == ["running", "dead"]
    # And the generator actually finished (no further frames) -- `_collect`
    # completing at all already proves the stream didn't hang forever.


def test_stream_endpoint_closes_when_job_is_already_dead(
    client: TestClient, as_user: Callable[[str], None], fake_pubsub: _FakeBus
) -> None:
    """End-to-end (issue #10's endpoint + issue #11's new terminal status): a
    client connecting to `GET /jobs/{id}/stream` for an already-`dead` job
    must get exactly one frame (the DB snapshot) and an immediate close, same
    as for `done`/`error` -- the request must not hang."""
    uid = db.ensure_user("stream-dead@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-dead", "name": "D"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    job_id = db.create_job(vid, "generation")
    as_user(uid)

    db.set_job_status(job_id, "dead", error="exhausted retries")

    resp = client.get(f"/jobs/{job_id}/stream")

    assert resp.status_code == 200
    frames = []
    for block in resp.text.strip("\n").split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                import json as _json

                frames.append(_json.loads(line.removeprefix("data: ")))
    assert len(frames) == 1
    assert frames[0]["status"] == "dead"
    assert frames[0]["error"] == "exhausted retries"


# --- 5. `retrying` is NON-terminal for the SSE stream (fix cycle 1) -----------


def test_retrying_is_not_in_the_terminal_statuses_set() -> None:
    """Direct guard against a regression re-adding `retrying` to the terminal
    set: it's the whole point of the fix (see module docstring, point 5) that
    an intermediate failed attempt must not stop the stream."""
    assert "retrying" not in job_events._TERMINAL_STATUSES
    assert {"done", "error", "dead"} == job_events._TERMINAL_STATUSES


def test_event_stream_treats_retrying_as_non_terminal_and_keeps_streaming() -> None:
    """Unit-level: `job_events.event_stream` must NOT close after a `retrying`
    frame -- it should keep relaying until an actual terminal status (`done`
    here) arrives. This is the direct counterpart of
    `test_event_stream_treats_dead_as_terminal_and_stops` above, proving
    `retrying` behaves oppositely."""
    import asyncio
    import json
    import queue as queue_mod

    q: queue_mod.Queue[str] = queue_mod.Queue()

    class _Pub:
        async def subscribe(self, channel: str) -> None:
            pass

        async def get_message(
            self, ignore_subscribe_messages: bool = True, timeout: float = 15.0
        ) -> dict[str, Any] | None:
            def _blocking_get() -> str | None:
                try:
                    return q.get(timeout=timeout)
                except queue_mod.Empty:
                    return None

            raw = await asyncio.to_thread(_blocking_get)
            return None if raw is None else {"type": "message", "data": raw}

        async def unsubscribe(self, channel: str) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class _Client:
        def pubsub(self) -> _Pub:
            return _Pub()

        async def aclose(self) -> None:
            pass

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(job_events.aioredis, "from_url", lambda *a, **kw: _Client())
    try:
        q.put(
            json.dumps(
                {"id": "job-1", "status": "retrying", "step": "tts", "error": "transient hiccup"}
            )
        )
        q.put(json.dumps({"id": "job-1", "status": "done", "step": "tts", "error": None}))

        async def _collect() -> list[bytes]:
            job = {"id": "job-1", "status": "running", "step": "tts"}
            return [chunk async for chunk in job_events.event_stream("job-1", lambda: job)]

        chunks = asyncio.run(_collect())
    finally:
        monkeypatch.undo()

    payloads = []
    for chunk in chunks:
        for line in chunk.decode().splitlines():
            if line.startswith("data: "):
                payloads.append(json.loads(line.removeprefix("data: ")))
    # The stream survived the `retrying` frame (didn't stop there) and only
    # closed after `done` -- if `retrying` were (wrongly) terminal, `_collect`
    # would have returned after just ["running", "retrying"] and never picked
    # up the queued "done" message at all.
    assert [p["status"] for p in payloads] == ["running", "retrying", "done"]


def test_stream_endpoint_keeps_streaming_through_an_intermediate_retrying_attempt(
    client: TestClient, as_user: Callable[[str], None], fake_pubsub: _FakeBus
) -> None:
    """End-to-end: a client connected to `GET /jobs/{id}/stream` while the
    worker publishes a `retrying` transition (a failed attempt with retries
    still pending) must keep receiving frames past it -- the connection must
    only close once the job actually reaches a terminal status (`done` here),
    proving the SSE feature (issue #10) isn't broken by an in-flight retry
    (issue #11 fix cycle 1)."""
    import threading

    uid = db.ensure_user("stream-retrying@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-retrying", "name": "R"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    job_id = db.create_job(vid, "generation")
    as_user(uid)

    db.set_job_status(job_id, "running")

    result: dict[str, Any] = {}

    def do_request() -> None:
        result["resp"] = client.get(f"/jobs/{job_id}/stream")

    req_thread = threading.Thread(target=do_request)
    req_thread.start()
    assert fake_pubsub.subscribed.wait(timeout=5), "SSE endpoint never subscribed"

    # A failed attempt, still within budget -- non-terminal.
    db.set_job_status(job_id, "retrying", error="transient hiccup")
    # The request must still be open at this point (retrying didn't close it).
    req_thread.join(timeout=0.5)
    assert req_thread.is_alive(), "stream closed on a non-terminal `retrying` status"

    # The job then recovers.
    db.set_job_status(job_id, "done")

    req_thread.join(timeout=5)
    assert not req_thread.is_alive()
    resp = result["resp"]

    frames = []
    for block in resp.text.strip("\n").split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                import json as _json

                frames.append(_json.loads(line.removeprefix("data: ")))

    assert [f["status"] for f in frames] == ["running", "retrying", "done"]
    assert frames[-1]["error"] is None  # cleared on the recovering `done` transition


# --- 6. recovered job reports done + error=None via the real read path --------


def test_recovered_job_reports_done_and_no_error_via_job_status_endpoint(
    client: TestClient,
    as_user: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    eager_celery_retrying: None,
) -> None:
    """End-to-end: complementary to
    `test_generation_task_recovers_and_does_not_go_dead_if_it_succeeds_within_the_budget`
    (which checks the DB row directly) -- the same guarantee must hold through
    the actual client-facing path, `GET /jobs/{id}` (issue #9): a job that
    fails once (writing `retrying` + an error) then recovers must report
    `status=done` AND `error=None`, not a stale message from the abandoned
    attempt."""
    uid = db.ensure_user("recovers-via-endpoint@test.local")
    db.upsert_brand_kit({"id": "kit-recover", "name": "K"}, uid)
    as_user(uid)

    attempts = {"n": 0}

    def fails_once_then_succeeds(
        pid: str, input_text: str, kit: dict[str, Any], job_id: str
    ) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient mistral hiccup")
        db.set_status(pid, "ready")

    monkeypatch.setattr("api.service.run_generation", fails_once_then_succeeds)

    resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-recover"})
    pid = resp.json()["id"]
    job_id = _job_row(pid)["id"]

    job_resp = client.get(f"/jobs/{job_id}")
    assert job_resp.status_code == 200
    body = job_resp.json()
    assert body["status"] == "done"
    assert body["error"] is None


# --- 7. `DeadLetterTask._extract_job_id` guard (fix cycle 1) ------------------


def test_on_failure_reaches_the_dlq_for_a_kwargs_only_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task invoked kwargs-only (e.g. `.delay(job_id=...)`, legal Celery
    usage even if every task in this codebase currently uses positional args)
    must still reach the DLQ transition -- `_extract_job_id` must not assume
    `args[0]` unconditionally."""
    uid = db.ensure_user("dlq-kwargs@test.local")
    version_id = db.upsert_brand_kit({"id": "kit-dlq-kwargs", "name": "K"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    job_id = db.create_job(vid, "generation")

    task = DeadLetterTask()
    task.on_failure(
        RuntimeError("boom"),
        task_id="task-1",
        args=(),
        kwargs={"job_id": job_id},
        einfo=None,
    )

    job = db.get_job(job_id, uid)
    assert job is not None
    assert job["status"] == "dead"
    assert job["error"] == "boom"


def test_on_failure_raises_explicit_error_when_job_id_is_missing() -> None:
    """Misconfiguration (neither a positional nor a `job_id` kwarg) must fail
    loudly with an actionable `ValueError`, not an opaque `IndexError`/
    `KeyError` that would otherwise silently skip the DLQ transition and leave
    the job stuck on its last transient status forever."""
    task = DeadLetterTask()

    with pytest.raises(ValueError, match="no job_id found"):
        task.on_failure(RuntimeError("boom"), task_id="task-1", args=(), kwargs={}, einfo=None)
