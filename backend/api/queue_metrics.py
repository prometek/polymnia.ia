#!/usr/bin/env python3
"""Queue-depth metric (issue #11 / PRO-10): how many jobs are pending per Celery
queue, exposed for later consumption by dashboards/alerts (PRO-23) — that
consumption is explicitly out of this ticket's scope, only the metric itself is.

Reuses the Celery broker connection (`REDIS_URL`, centralized in
api/redis_config.py) rather than adding a second metrics pipeline: with the Redis
transport, a queue's pending (unconsumed) messages are a plain Redis list keyed by
the queue name, so `LLEN` is the depth — no Celery inspection/worker round-trip
needed, this reads the broker directly and stays cheap enough for a metrics poll.
"""

from typing import cast

import redis

from .redis_config import REDIS_URL

# Queues this app actually routes tasks to (see api/celery_app.py task_routes) —
# keep in sync with that config, same "single source of truth per concern" pattern
# as REDIS_URL itself.
QUEUE_NAMES = ("generation", "render")

_client: redis.Redis | None = None


def _redis_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL)
    return _client


def queue_depth(queue_name: str) -> int:
    """Number of pending (not yet picked up by a worker) messages on one queue."""
    # redis-py types `llen` as `Awaitable[int] | int` (its stubs share one signature
    # across the sync/async clients); `_redis_client()` is always the sync client, so
    # the awaitable branch never happens here.
    return cast(int, _redis_client().llen(queue_name))


def queue_depths() -> dict[str, int]:
    """Depth for every queue this app routes tasks to."""
    return {name: queue_depth(name) for name in QUEUE_NAMES}
