"""GET /jobs/{id}/stream — real-time job status relay over SSE (issue #10).

Real Redis isn't available in this test environment, so these tests fake the
pub/sub boundary only: `job_events._publisher_client` (the sync Redis client
`publish_job_event` calls) and `job_events.aioredis.from_url` (the async
client `event_stream` builds its `pubsub()` from — subscribe/get_message/
unsubscribe, matching the real `redis.asyncio` API) are replaced with an
in-process, thread-safe bus. Everything else is the real code path:
`db.set_job_status` / `db.set_job_step` (called from a background thread,
standing in for the Celery worker process), `job_events.publish_job_event`,
`job_events.event_stream`'s subscribe-first/snapshot/relay/keepalive/degrade
logic, and the FastAPI `/jobs/{id}/stream` route + `require_job` dependency
(shared, unmodified, with the polling `/jobs/{id}` endpoint - issue #9). No DB
mock, per docs/code-standards.md ("pas de mock de la DB en intégration").
"""

import json
import queue as queue_mod
import threading
from collections.abc import Callable
from typing import Any

import pytest
import redis
from api import db, job_events
from starlette.testclient import TestClient

# --- fake pub/sub boundary (matches the real get_message-based aioredis API) --


class _FakeBus:
    """In-process stand-in for Redis pub/sub, matching real pub/sub semantics:
    fire-and-forget broadcast to whoever is *currently* subscribed to a channel —
    a message published before any listener attached is dropped, never
    backlogged (this is exactly why `event_stream` subscribes *before* reading
    the DB snapshot: closing that race is the product's job, not the
    transport's — see test_job_events.py's race-window test for that guarantee
    in isolation).

    `subscribed` is set the moment a listener attaches, so a test can hold its
    "worker" writes until the SSE endpoint has actually subscribed — otherwise
    a fast worker could race ahead and publish into the void, same as it could
    against a real Redis broker.
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[queue_mod.Queue[str]]] = {}
        self._lock = threading.Lock()
        self.subscribed = threading.Event()

    def publish(self, channel: str, data: str) -> None:
        with self._lock:
            queues = list(self._listeners.get(channel, ()))
        for q in queues:
            q.put(data)

    def attach(self, channel: str) -> queue_mod.Queue[str]:
        q: queue_mod.Queue[str] = queue_mod.Queue()
        with self._lock:
            self._listeners.setdefault(channel, []).append(q)
        self.subscribed.set()
        return q

    def detach(self, channel: str, q: queue_mod.Queue[str]) -> None:
        with self._lock:
            listeners = self._listeners.get(channel, [])
            if q in listeners:
                listeners.remove(q)


class _FakePubSub:
    """Stands in for `redis.asyncio.Redis().pubsub()`."""

    def __init__(self, bus: _FakeBus) -> None:
        self._bus = bus
        self._channel: str | None = None
        self._queue: queue_mod.Queue[str] | None = None

    async def subscribe(self, channel: str) -> None:
        self._channel = channel
        self._queue = self._bus.attach(channel)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float = 15.0
    ) -> dict[str, Any] | None:
        import asyncio

        assert self._queue is not None, "get_message called before subscribe"

        def _blocking_get() -> str | None:
            try:
                return self._queue.get(timeout=timeout)  # type: ignore[union-attr]
            except queue_mod.Empty:
                return None

        raw = await asyncio.to_thread(_blocking_get)
        return None if raw is None else {"type": "message", "data": raw}

    async def unsubscribe(self, channel: str) -> None:
        if self._queue is not None and self._channel is not None:
            self._bus.detach(self._channel, self._queue)

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


# --- helpers ------------------------------------------------------------------


def _make_job(email: str) -> tuple[str, str, str]:
    """Fresh queued generation job -> (user_id, video_id, job_id)."""
    uid = db.ensure_user(email)
    version_id = db.upsert_brand_kit({"id": f"kit-{email}", "name": "K"}, uid)
    vid = db.uuid.uuid4().hex[:12]
    db.create_video(vid, uid, version_id, "v")
    job_id = db.create_job(vid, "generation")
    return uid, vid, job_id


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """Parse `id: N\\ndata: {...}\\n\\n` frames back into payloads, in stream
    order -- skipping `: keepalive` SSE comment frames (they carry no `data:`
    line and must not corrupt the parsed sequence)."""
    frames = []
    for block in body.strip("\n").split("\n\n"):
        if not block:
            continue
        data_line = next((line for line in block.splitlines() if line.startswith("data: ")), None)
        if data_line is None:
            continue  # e.g. ": keepalive" comment frame
        frames.append(json.loads(data_line.removeprefix("data: ")))
    return frames


# --- content-type / headers ---------------------------------------------------


def test_stream_is_text_event_stream_with_no_cache_headers(
    client: TestClient, as_user: Callable[[str], None], fake_pubsub: _FakeBus
) -> None:
    uid, _vid, job_id = _make_job("stream-headers@test.local")
    as_user(uid)
    db.set_job_status(job_id, "done")  # terminal -> snapshot-only, response completes

    resp = client.get(f"/jobs/{job_id}/stream")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"


# --- ownership / 404 parity with the polling endpoint (issue #9) --------------


def test_stream_404_unknown_job_id(client: TestClient, as_user: Callable[[str], None]) -> None:
    uid = db.ensure_user("stream-404@test.local")
    as_user(uid)

    resp = client.get(f"/jobs/{db.uuid.uuid4()}/stream")
    assert resp.status_code == 404


def test_stream_404_malformed_job_id(client: TestClient, as_user: Callable[[str], None]) -> None:
    uid = db.ensure_user("stream-malformed@test.local")
    as_user(uid)

    resp = client.get("/jobs/not-a-valid-uuid/stream")
    assert resp.status_code == 404


def test_stream_404_other_users_job_same_as_poll_endpoint(
    client: TestClient, as_user: Callable[[str], None]
) -> None:
    """Ownership check must be identical to `GET /jobs/{id}`: a job that exists but
    belongs to someone else 404s on the stream endpoint too — no existence leak,
    and no code path that bypasses `require_job` for the streaming route."""
    _owner_id, _owner_vid, other_job_id = _make_job("stream-owner@test.local")
    requester_id = db.ensure_user("stream-requester@test.local")
    as_user(requester_id)

    poll_resp = client.get(f"/jobs/{other_job_id}")
    stream_resp = client.get(f"/jobs/{other_job_id}/stream")

    assert poll_resp.status_code == 404
    assert stream_resp.status_code == 404


# --- snapshot -> live transitions -> terminal close, end-to-end ---------------


def test_stream_emits_snapshot_then_worker_transitions_then_closes_on_terminal(
    client: TestClient, as_user: Callable[[str], None], fake_pubsub: _FakeBus
) -> None:
    """End-to-end: the request runs in a background thread (the SSE connection is
    long-lived), and the main thread plays the worker — real `db.set_job_step` /
    `set_job_status` calls, exactly what tasks/generation.py and tasks/render.py
    do — only once the endpoint has actually subscribed (mirrors the real race
    against a Redis broker: a message published before anyone is listening is
    never delivered). The collected SSE response must show, in order: the
    connect-time snapshot, then each worker transition, then stop right after
    the terminal one (issue #10's core relay requirement)."""
    uid, _vid, job_id = _make_job("stream-e2e@test.local")
    as_user(uid)

    # State before connecting -- the fresh snapshot `event_stream` takes (after
    # subscribing) must reflect this.
    db.set_job_status(job_id, "running")
    db.set_job_step(job_id, "plan")

    result: dict[str, Any] = {}

    def do_request() -> None:
        result["resp"] = client.get(f"/jobs/{job_id}/stream")

    req_thread = threading.Thread(target=do_request)
    req_thread.start()
    assert fake_pubsub.subscribed.wait(timeout=5), "SSE endpoint never subscribed"

    # The "worker" (this thread) transitions the job for real, exactly like a
    # Celery task would — publish_job_event fires from inside set_job_step/
    # set_job_status themselves.
    db.set_job_step(job_id, "outline")
    db.set_job_status(job_id, "done")

    req_thread.join(timeout=5)
    assert not req_thread.is_alive()  # request really completed, not just timed out
    resp = result["resp"]

    frames = _parse_sse(resp.text)
    assert [f["status"] for f in frames] == ["running", "running", "done"]
    assert [f["step"] for f in frames] == ["plan", "outline", "outline"]

    # The DB itself (source of truth) agrees with what the stream relayed.
    assert db.get_job(job_id, uid) == frames[-1]


def test_stream_closes_on_error_terminal_status(
    client: TestClient, as_user: Callable[[str], None], fake_pubsub: _FakeBus
) -> None:
    uid, _vid, job_id = _make_job("stream-error@test.local")
    as_user(uid)
    db.set_job_status(job_id, "running")

    result: dict[str, Any] = {}

    def do_request() -> None:
        result["resp"] = client.get(f"/jobs/{job_id}/stream")

    req_thread = threading.Thread(target=do_request)
    req_thread.start()
    assert fake_pubsub.subscribed.wait(timeout=5), "SSE endpoint never subscribed"

    db.set_job_status(job_id, "error", error="boom")

    req_thread.join(timeout=5)
    assert not req_thread.is_alive()
    resp = result["resp"]

    frames = _parse_sse(resp.text)
    assert frames[-1]["status"] == "error"
    assert frames[-1]["error"] == "boom"


def test_stream_delivers_a_terminal_transition_raced_in_between_connect_and_snapshot(
    client: TestClient,
    as_user: Callable[[str], None],
    fake_pubsub: _FakeBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end version of the HIGH-severity race fix: force the worker's
    terminal transition to land in the exact window between the endpoint
    subscribing and it reading the DB snapshot, by hooking `db.get_job` (the
    function wired as `event_stream`'s `snapshot` callback in api/main.py) so
    the very first call performs the worker's terminal write *before*
    returning. With the old snapshot-before-subscribe ordering this transition
    would be silently dropped and the connection would hang forever; with
    subscribe-first it must still be delivered and the stream must still
    close."""
    uid, _vid, job_id = _make_job("stream-race@test.local")
    as_user(uid)
    db.set_job_status(job_id, "running")

    real_get_job = db.get_job
    raced = {"done": False}

    def racing_get_job(jid: str, user_id: str) -> dict[str, Any] | None:
        # `db.get_job` is called twice per request: once by `require_job` (the
        # ownership check, *before* the stream even opens -- `fake_pubsub.
        # subscribed` isn't set yet at that point) and once by `event_stream`'s
        # `snapshot` callback, right after `pubsub.subscribe()` returns. Only
        # the second call is the one under test -- gating on `subscribed`
        # pins the race to that exact window, not the earlier ownership check.
        if jid == job_id and fake_pubsub.subscribed.is_set() and not raced["done"]:
            raced["done"] = True
            stale_snapshot = real_get_job(jid, user_id)  # captured before the race write
            # The worker "wins the race": commits + publishes its terminal
            # transition strictly after the endpoint has already subscribed,
            # but strictly before this snapshot call returns.
            db.set_job_status(job_id, "done")
            return stale_snapshot
        return real_get_job(jid, user_id)

    monkeypatch.setattr(db, "get_job", racing_get_job)

    resp = client.get(f"/jobs/{job_id}/stream")

    frames = _parse_sse(resp.text)
    # Snapshot (still "running" -- captured before the race write) followed by
    # the raced "done" transition, delivered because the subscription was
    # already active when it was published.
    assert [f["status"] for f in frames] == ["running", "done"]


# --- keepalive doesn't corrupt the stream (endpoint level) --------------------


def test_stream_keepalive_frames_do_not_corrupt_the_relayed_sequence(
    client: TestClient,
    as_user: Callable[[str], None],
    fake_pubsub: _FakeBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short keepalive interval forces at least one `: keepalive` comment frame
    while the worker takes its time; the real transitions must still come
    through, in order, once published, and `_parse_sse` (which every other test
    here relies on) must keep ignoring the comment frames correctly."""
    monkeypatch.setattr(job_events, "_KEEPALIVE_INTERVAL_S", 0.02)

    uid, _vid, job_id = _make_job("stream-keepalive@test.local")
    as_user(uid)
    db.set_job_status(job_id, "running")

    result: dict[str, Any] = {}

    def do_request() -> None:
        result["resp"] = client.get(f"/jobs/{job_id}/stream")

    req_thread = threading.Thread(target=do_request)
    req_thread.start()
    assert fake_pubsub.subscribed.wait(timeout=5), "SSE endpoint never subscribed"

    import time

    time.sleep(0.08)  # let a couple of keepalive intervals elapse with nothing published
    db.set_job_status(job_id, "done")

    req_thread.join(timeout=5)
    resp = result["resp"]

    assert ": keepalive" in resp.text  # heartbeat really was emitted
    frames = _parse_sse(resp.text)  # ...but doesn't corrupt the parsed data sequence
    assert [f["status"] for f in frames] == ["running", "done"]


# --- broker outage degrades cleanly (no raise) --------------------------------


def test_stream_degrades_cleanly_when_redis_is_unreachable(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Redis is down when a client connects, the endpoint must not 500 or
    hang -- `event_stream` degrades to a clean, empty close (see
    test_job_events.py's unit-level equivalent), leaving the client to fall
    back to polling `GET /jobs/{id}` (still verified working below)."""
    uid, _vid, job_id = _make_job("stream-outage@test.local")
    as_user(uid)
    db.set_job_status(job_id, "running")

    class _BrokenPubSub:
        async def subscribe(self, channel: str) -> None:
            raise redis.RedisError("connection refused")

        async def get_message(self, **kwargs: Any) -> None:
            raise AssertionError("must not be reached: subscribe already failed")

        async def unsubscribe(self, channel: str) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class _BrokenClient:
        def pubsub(self) -> _BrokenPubSub:
            return _BrokenPubSub()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(job_events.aioredis, "from_url", lambda *a, **kw: _BrokenClient())

    resp = client.get(f"/jobs/{job_id}/stream")

    assert resp.status_code == 200  # SSE headers were already committed
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == ""  # degraded cleanly: no frames, no raised/unhandled error

    # The fallback the ticket requires still works despite the outage.
    poll_resp = client.get(f"/jobs/{job_id}")
    assert poll_resp.status_code == 200
    assert poll_resp.json()["status"] == "running"


# --- polling fallback keeps working (regression, issue #9 / PRO-08) -----------


def test_polling_endpoint_still_works_alongside_the_stream_endpoint(
    client: TestClient, as_user: Callable[[str], None], fake_pubsub: _FakeBus
) -> None:
    """The ticket requires `GET /jobs/{id}` to remain a correct fallback. Poll
    before the stream feature is touched, drive the job through the stream
    endpoint, then poll again -> both reads must reflect the true DB state,
    proving the new SSE code path doesn't regress or replace the old one."""
    uid, _vid, job_id = _make_job("stream-fallback@test.local")
    as_user(uid)

    first_poll = client.get(f"/jobs/{job_id}")
    assert first_poll.status_code == 200
    assert first_poll.json()["status"] == "queued"

    db.set_job_status(job_id, "running")
    db.set_job_step(job_id, "outline")
    db.set_job_status(job_id, "done")

    stream_resp = client.get(f"/jobs/{job_id}/stream")
    assert stream_resp.status_code == 200

    final_poll = client.get(f"/jobs/{job_id}")
    assert final_poll.status_code == 200
    assert final_poll.json() == {
        "id": job_id,
        "type": "generation",
        "status": "done",
        "step": "outline",
        "video_id": _vid,
        "error": None,
    }
