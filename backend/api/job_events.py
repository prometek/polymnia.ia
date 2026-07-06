#!/usr/bin/env python3
"""Real-time relay of job status/step transitions over Redis pub/sub (issue #10).

Workers mutate the durable `jobs` row via `api.db.set_job_status` / `set_job_step`
(PostgreSQL is the source of truth, ADR-independent); those two functions call
`publish_job_event` right after their commit — the single choke point, so every
worker/API status transition (generation + render, present and future) reaches
subscribers without each call site having to remember to publish.

Reuses the Celery broker (`REDIS_URL`, centralized in api/redis_config.py) — one
Redis, two uses (job queue + pub/sub), no second message system to add or operate.

`GET /jobs/{id}/stream` (api/main.py) subscribes per job and relays to the
client as Server-Sent Events; `GET /jobs/{id}` (PRO-08) stays the polling
fallback the ticket requires — publish failures here must never fail a job, and
a degraded stream here must never do more than push a client back onto that poll.
"""

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import redis
import redis.asyncio as aioredis

from .redis_config import REDIS_URL

logger = logging.getLogger("polymnia.job_events")

# Terminal job statuses (see api/models.py Job docstring: queued -> running ->
# retrying -> done/error/dead) — once reached, no further event will ever be
# published for that job. `dead` (issue #11, DLQ: bounded retries exhausted) is
# terminal exactly like `error`. `retrying` (issue #11) is deliberately NOT here: a
# failed attempt with retries still pending must keep the stream open — the job may
# yet recover (`done`) or exhaust its budget (`dead`), so it isn't terminal.
_TERMINAL_STATUSES = frozenset({"done", "error", "dead"})

_CONNECT_TIMEOUT_S = 2.0
# generation/render steps can be minutes apart: without a heartbeat, an idle
# proxy/LB sitting between the client and this API can drop the connection
# between real transitions. `get_message`'s own `timeout` doubles as the wait
# for the next pub/sub message and the heartbeat clock.
_KEEPALIVE_INTERVAL_S = 15.0

# Lazy, one sync client per process (publish side runs inside sync worker/API code,
# same "one client per process" pattern as the SQLModel `engine` in session.py).
_publisher: redis.Redis | None = None


def channel_name(job_id: str) -> str:
    """Per-job channel — a client only ever subscribes to its own job's events."""
    return f"jobs.{job_id}.status"


def _publisher_client() -> redis.Redis:
    global _publisher
    if _publisher is None:
        _publisher = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=_CONNECT_TIMEOUT_S)
    return _publisher


def publish_job_event(job: dict[str, Any]) -> None:
    """Publish a job's current status/step snapshot to its channel.

    Best-effort by design (see module docstring): the `jobs` row already
    committed before this is called, and `GET /jobs/{id}` remains a correct
    fallback — a broker hiccup here must only delay a client's next poll, it
    must never fail the job itself. Logged (not silent) so it's observable.
    """
    try:
        _publisher_client().publish(channel_name(job["id"]), json.dumps(job))
    except redis.RedisError:
        logger.warning("failed to publish job event for job %s", job["id"], exc_info=True)


def _sse_event(seq: int, payload: dict[str, Any]) -> bytes:
    """Format one SSE frame. `id:` lets a reconnecting `EventSource` send
    `Last-Event-ID`, but there's no replay log behind Redis pub/sub (fire-and-
    forget) — the first frame of every (re)connection is always a fresh DB
    snapshot (see `event_stream`), so a client is never stuck on stale state
    regardless of what it missed while disconnected.
    """
    return f"id: {seq}\ndata: {json.dumps(payload)}\n\n".encode()


async def event_stream(
    job_id: str, snapshot: Callable[[], dict[str, Any] | None]
) -> AsyncIterator[bytes]:
    """SSE byte stream for one job: subscribe FIRST, then snapshot, then relay.

    Ordering matters — it closes a lost-terminal-transition race. Reading the
    snapshot before subscribing leaves a window where a transition published in
    between is gone forever (pub/sub is fire-and-forget, no history); if that
    transition happened to be the terminal one, the stream would then block on
    `get_message` forever (nothing more will ever be published for a finished
    job), leaking a Redis connection/task per hung client. Subscribing first
    guarantees every transition from that point on is queued for us, so the
    `snapshot()` taken right after is *at least* as fresh as "now" — the only
    downside is a possible duplicate frame (the snapshot and a live message
    describing the same transition), which is harmless for a status feed.

    `snapshot` is injected by the caller (api/main.py, backed by `api.db.get_job`)
    rather than this module importing `api.db` directly, to avoid a cycle:
    `db.py` already imports this module to publish from its two status-transition
    functions.

    Degrades to a clean, logged end-of-stream on a Redis error (broker outage,
    connect timeout) — same best-effort posture as `publish_job_event` — so the
    client just falls back to polling `GET /jobs/{id}` instead of hanging or
    surfacing an unhandled mid-stream ASGI error.
    """
    client = aioredis.from_url(  # type: ignore[no-untyped-call]  # untyped **kwargs signature
        REDIS_URL, socket_connect_timeout=_CONNECT_TIMEOUT_S
    )
    pubsub = client.pubsub()
    channel = channel_name(job_id)
    try:
        try:
            await pubsub.subscribe(channel)

            job = snapshot()
            if job is None:  # job vanished between the caller's own check and here
                return
            yield _sse_event(0, job)
            if job["status"] in _TERMINAL_STATUSES:
                return

            seq = 1
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=_KEEPALIVE_INTERVAL_S
                )
                if message is None:  # no transition within the interval -> heartbeat
                    # SSE comment frame: ignored by EventSource, keeps proxies/LBs from
                    # dropping an idle connection between real (minutes-apart) transitions.
                    yield b": keepalive\n\n"
                    continue
                payload = json.loads(message["data"])
                yield _sse_event(seq, payload)
                if payload["status"] in _TERMINAL_STATUSES:
                    return
                seq += 1
        except redis.RedisError:
            logger.warning("job event stream for %s degraded: redis error", job_id, exc_info=True)
            return
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await client.aclose()
        except redis.RedisError:
            logger.warning("failed to clean up job event stream for %s", job_id, exc_info=True)
