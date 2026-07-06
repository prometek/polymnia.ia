"""Unit tests for `api/job_events.py` — the Redis pub/sub relay for job status/step
transitions (issue #10).

These exercise the module in isolation from the DB/HTTP layers, faking only the
Redis boundary: `_publisher_client` (the sync client `publish_job_event` calls)
and `aioredis.from_url` (the async client `event_stream` builds its `pubsub()`
from) — everything else (`channel_name`, `publish_job_event`'s try/except,
`_sse_event`'s byte framing, `event_stream`'s subscribe-first/snapshot/relay/
keepalive/degrade logic) is the real code.
"""

import asyncio
import json
import logging
import queue as queue_mod
from collections.abc import AsyncIterator
from typing import Any

import pytest
import redis
from api import job_events

# --- channel_name ------------------------------------------------------------


def test_channel_name_is_scoped_per_job() -> None:
    assert job_events.channel_name("abc-123") == "jobs.abc-123.status"
    # Two different jobs never share a channel (a client only ever gets its own job).
    assert job_events.channel_name("abc-123") != job_events.channel_name("xyz-999")


# --- publish_job_event --------------------------------------------------------


class _RecordingRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def publish(self, channel: str, data: str) -> None:
        self.calls.append((channel, data))


class _FailingRedis:
    def publish(self, channel: str, data: str) -> None:
        raise redis.RedisError("connection refused")


def test_publish_job_event_publishes_json_snapshot_on_the_jobs_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingRedis()
    monkeypatch.setattr(job_events, "_publisher_client", lambda: fake)

    job = {
        "id": "job-1",
        "video_id": "v-1",
        "type": "generation",
        "status": "running",
        "step": "outline",
        "error": None,
    }
    job_events.publish_job_event(job)

    assert len(fake.calls) == 1
    channel, data = fake.calls[0]
    assert channel == "jobs.job-1.status"
    assert json.loads(data) == job


def test_publish_job_event_called_again_on_a_later_step_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step change (not just a status change) must also publish — the ticket
    requires every worker-side step/status transition to reach the stream."""
    fake = _RecordingRedis()
    monkeypatch.setattr(job_events, "_publisher_client", lambda: fake)

    job_events.publish_job_event({"id": "job-1", "status": "running", "step": "plan"})
    job_events.publish_job_event({"id": "job-1", "status": "running", "step": "outline"})

    assert [json.loads(data)["step"] for _channel, data in fake.calls] == ["plan", "outline"]


def test_publish_job_event_is_best_effort_on_redis_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broker hiccup must never raise out of `publish_job_event` — the `jobs` row
    already committed; `GET /jobs/{id}` polling must stay a correct fallback
    regardless of pub/sub health (module docstring's whole point)."""
    monkeypatch.setattr(job_events, "_publisher_client", lambda: _FailingRedis())

    with caplog.at_level(logging.WARNING, logger="polymnia.job_events"):
        job_events.publish_job_event({"id": "job-1", "status": "running"})  # must not raise

    assert "job-1" in caplog.text


# --- _sse_event ----------------------------------------------------------------


def test_sse_event_frame_format() -> None:
    payload = {"id": "job-1", "status": "done"}
    frame = job_events._sse_event(3, payload)
    assert frame == f"id: 3\ndata: {json.dumps(payload)}\n\n".encode()


# --- fake aioredis pubsub boundary (get_message-based, matching the real API) --


class _FakePubSub:
    """Stands in for `redis.asyncio.Redis().pubsub()`. Backed by a plain
    `queue.Queue` fed by the test; `get_message` mirrors the real contract:
    `None` on timeout (drives `event_stream`'s keepalive), a `{"type":
    "message", "data": ...}` dict otherwise."""

    def __init__(self, queue: queue_mod.Queue[str], *, fail_on_subscribe: bool = False) -> None:
        self._queue = queue
        self._fail_on_subscribe = fail_on_subscribe
        self.subscribed_channels: list[str] = []

    async def subscribe(self, channel: str) -> None:
        if self._fail_on_subscribe:
            raise redis.RedisError("connection refused")
        self.subscribed_channels.append(channel)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float = 15.0
    ) -> dict[str, Any] | None:
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
    def __init__(self, pubsub: Any) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> Any:
        return self._pubsub

    async def aclose(self) -> None:
        pass


class _GetMessageRaises:
    """A pubsub whose `subscribe` succeeds but `get_message` blows up mid-stream —
    models a broker outage that starts *after* a client is already connected."""

    async def subscribe(self, channel: str) -> None:
        pass

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float = 15.0
    ) -> Any:
        raise redis.RedisError("connection lost")

    async def unsubscribe(self, channel: str) -> None:
        pass

    async def aclose(self) -> None:
        pass


def _patch_aioredis(monkeypatch: pytest.MonkeyPatch, pubsub: Any) -> None:
    monkeypatch.setattr(
        job_events.aioredis, "from_url", lambda *a, **kw: _FakeAsyncRedisClient(pubsub)
    )


async def _collect(ait: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in ait]


def _data_payloads(chunks: list[bytes]) -> list[dict[str, Any]]:
    """Parse only `data:` frames, skipping `: keepalive` SSE comment frames."""
    out = []
    for chunk in chunks:
        lines = chunk.decode().splitlines()
        data_line = next((line for line in lines if line.startswith("data: ")), None)
        if data_line is not None:
            out.append(json.loads(data_line.removeprefix("data: ")))
    return out


# --- event_stream: subscribe -> snapshot -> relay -> terminal close ------------


def test_event_stream_yields_only_the_snapshot_when_job_already_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client connecting after the job already finished must get one frame (the
    DB snapshot) and an immediate close — no wait on a message that will never
    arrive for a job that's already done."""
    q: queue_mod.Queue[str] = queue_mod.Queue()
    _patch_aioredis(monkeypatch, _FakePubSub(q))

    job = {"id": "job-1", "status": "done", "step": "tts"}
    chunks = asyncio.run(_collect(job_events.event_stream("job-1", lambda: job)))

    assert _data_payloads(chunks) == [job]


@pytest.mark.parametrize("terminal_status", ["done", "error"])
def test_event_stream_relays_live_transitions_in_order_then_closes(
    monkeypatch: pytest.MonkeyPatch, terminal_status: str
) -> None:
    """Transitions published after connect must be relayed in publish order, and
    the stream must stop once a terminal status arrives (done or error)."""
    q: queue_mod.Queue[str] = queue_mod.Queue()
    _patch_aioredis(monkeypatch, _FakePubSub(q))
    q.put(json.dumps({"id": "job-1", "status": "running", "step": "outline"}))
    q.put(json.dumps({"id": "job-1", "status": terminal_status, "step": "tts"}))

    job = {"id": "job-1", "status": "queued", "step": None}
    chunks = asyncio.run(_collect(job_events.event_stream("job-1", lambda: job)))

    assert _data_payloads(chunks) == [
        job,
        {"id": "job-1", "status": "running", "step": "outline"},
        {"id": "job-1", "status": terminal_status, "step": "tts"},
    ]


def test_event_stream_ends_if_the_job_vanishes_before_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`snapshot()` returning None (job deleted between the caller's own 404 check
    and the generator starting) must end the stream, not raise or hang."""
    q: queue_mod.Queue[str] = queue_mod.Queue()
    _patch_aioredis(monkeypatch, _FakePubSub(q))

    chunks = asyncio.run(_collect(job_events.event_stream("job-1", lambda: None)))

    assert chunks == []


# --- the HIGH-severity race fix: subscribe-before-snapshot -------------------


def test_event_stream_delivers_a_terminal_transition_published_in_the_race_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of subscribing *before* taking the snapshot: a transition
    (here, the terminal one) published in the narrow window between `subscribe`
    completing and `snapshot()` being called must still be delivered, not lost.

    With the old (snapshot-then-subscribe) ordering, a publish landing in that
    window would vanish forever (pub/sub has no history) and, if it was the
    terminal transition, the stream would then hang forever waiting for a
    message that will never come. Here the fake `snapshot` callback publishes
    the race message itself the moment it's invoked -- i.e. strictly after
    `pubsub.subscribe()` has already returned -- modelling that exact window.
    """
    q: queue_mod.Queue[str] = queue_mod.Queue()
    _patch_aioredis(monkeypatch, _FakePubSub(q))

    def snapshot() -> dict[str, Any]:
        # By the time this runs, `event_stream` has already subscribed (that's
        # the ordering under test) -- so publishing here simulates a worker
        # racing to "done" in the gap and must not be lost.
        q.put(json.dumps({"id": "job-1", "status": "done", "step": "tts"}))
        return {"id": "job-1", "status": "running", "step": "outline"}  # stale pre-race read

    chunks = asyncio.run(_collect(job_events.event_stream("job-1", snapshot)))

    payloads = _data_payloads(chunks)
    assert payloads == [
        {"id": "job-1", "status": "running", "step": "outline"},  # snapshot (stale, harmless dup)
        {"id": "job-1", "status": "done", "step": "tts"},  # race transition, still delivered
    ]
    # And it actually closed right after -- no hang waiting on a job that already finished.


# --- keepalive on idle ---------------------------------------------------------


def test_event_stream_emits_keepalive_on_idle_then_keeps_relaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `get_message` timeout (no transition within the keepalive interval) must
    surface as an SSE comment frame (ignored by `EventSource`, keeps an idle
    proxy/LB from dropping the connection) -- not end the stream. A real
    transition arriving afterwards must still be relayed normally."""
    monkeypatch.setattr(job_events, "_KEEPALIVE_INTERVAL_S", 0.02)
    q: queue_mod.Queue[str] = queue_mod.Queue()
    _patch_aioredis(monkeypatch, _FakePubSub(q))

    async def _delayed_publish() -> None:
        await asyncio.sleep(0.08)  # long enough to force a couple of keepalive timeouts first
        q.put(json.dumps({"id": "job-1", "status": "done"}))

    async def _run() -> list[bytes]:
        publisher = asyncio.create_task(_delayed_publish())
        job = {"id": "job-1", "status": "running"}
        chunks = await _collect(job_events.event_stream("job-1", lambda: job))
        await publisher
        return chunks

    chunks = asyncio.run(_run())

    assert chunks.count(b": keepalive\n\n") >= 1
    # keepalive frames don't corrupt the stream: the real data frames are still
    # exactly the snapshot + the terminal transition, in order.
    assert _data_payloads(chunks) == [
        {"id": "job-1", "status": "running"},
        {"id": "job-1", "status": "done"},
    ]
    # and the keepalive frame(s) are genuinely interleaved before the real close.
    assert chunks[-1] != b": keepalive\n\n"


# --- broker outage degrades cleanly (no raise) ---------------------------------


def test_event_stream_degrades_cleanly_when_subscribe_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broker outage at connect time (subscribe itself fails) must end the
    stream cleanly -- no unhandled exception out of the async generator, so the
    ASGI response just completes (client falls back to polling)."""
    q: queue_mod.Queue[str] = queue_mod.Queue()
    _patch_aioredis(monkeypatch, _FakePubSub(q, fail_on_subscribe=True))

    with caplog.at_level(logging.WARNING, logger="polymnia.job_events"):
        chunks = asyncio.run(
            _collect(job_events.event_stream("job-1", lambda: {"id": "job-1", "status": "running"}))
        )

    assert chunks == []  # no snapshot even sent -- subscribe failed before we got that far
    assert "job-1" in caplog.text


def test_event_stream_degrades_cleanly_when_get_message_fails_mid_stream(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broker outage that starts *after* the client is already connected (past
    the snapshot) must also end the stream cleanly, not raise mid-response."""
    _patch_aioredis(monkeypatch, _GetMessageRaises())

    with caplog.at_level(logging.WARNING, logger="polymnia.job_events"):
        chunks = asyncio.run(
            _collect(job_events.event_stream("job-1", lambda: {"id": "job-1", "status": "running"}))
        )

    # The snapshot (taken before the outage hit) was already relayed; the stream
    # then ends cleanly instead of raising out of get_message's failure.
    assert _data_payloads(chunks) == [{"id": "job-1", "status": "running"}]
    assert "job-1" in caplog.text
