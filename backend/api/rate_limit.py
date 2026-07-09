#!/usr/bin/env python3
"""Per-user sliding-window rate limiting (issue #17) on the API's expensive,
job-triggering endpoints (`POST /projects`, `POST /projects/{id}/render`,
`POST /projects/{id}/scenes/{order}/ai-edit`) — bounds LLM/TTS/render cost abuse
and worker-queue saturation (architecture.md §15/§18), ahead of the later
business credit/quota-per-job ticket (PRO-21, out of this ticket's scope: no
per-job debit here, only a request-rate cap).

Shared counter in Redis (the same broker the job queue uses, `REDIS_URL`
centralized in api/redis_config.py) rather than an in-process counter: several
stateless API instances (architecture.md §13) must agree on one budget per user,
and only a store external to any single process can do that.

Algorithm: a sliding-window log per `(scope, user_id)`, kept in a Redis ZSET
(`ratelimit:{scope}:{user_id}`) — each member is one request's timestamp (in ms,
read from Redis's own `TIME` command rather than the caller's wall clock, so this
stays correct even if two API instances' local clocks drift). `ZREMRANGEBYSCORE`
evicts everything older than the window, `ZCARD` counts what's left, and the
admission check + insertion happen in one atomic Lua script run server-side — no
race between "check" and "consume" across concurrent requests from the same user
landing on different API instances.

`scope` isolates the three endpoints from each other (a burst of renders doesn't
also block cheaper project creation) — all scopes share the same configured
quota (`RATE_LIMIT_MAX_REQUESTS` per `RATE_LIMIT_WINDOW_S`).
"""

import os
import uuid
from dataclasses import dataclass

import redis
from fastapi import HTTPException

from .redis_config import REDIS_URL


class RateLimitConfigError(Exception):
    """`RATE_LIMIT_MAX_REQUESTS`/`RATE_LIMIT_WINDOW_S` env config is invalid.

    Mirrors `api/auth.py`'s `AuthConfigError` / `api/storage.py`'s
    `StorageConfigError`: fail at import (config boundary), not on the first
    request.
    """


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RateLimitConfigError(f"{name}={raw!r} is not a valid integer") from exc
    if value <= 0:
        raise RateLimitConfigError(f"{name}={value} must be a positive integer")
    return value


# Quota shared by every rate-limited scope — configurable per environment (a prod
# deploy protecting paid LLM/TTS/render calls may want a stricter cap than local
# dev/CI). Defaults (20 requests / 60s per scope+user) are generous enough not to
# trip on normal usage while still bounding a spam burst.
RATE_LIMIT_MAX_REQUESTS = _positive_int_env("RATE_LIMIT_MAX_REQUESTS", 20)
RATE_LIMIT_WINDOW_S = _positive_int_env("RATE_LIMIT_WINDOW_S", 60)

# Atomic admission check: evict expired entries, count what's left, and — only if
# under quota — record this request; all in one Redis round trip so concurrent
# requests from the same user (possibly hitting different stateless API
# instances) can never both observe "under quota" before either records itself.
# Uses Redis's own clock (`TIME`), not the caller's wall clock, so multi-instance
# clock drift can't skew the window.
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local window_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local member = ARGV[3]

local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - window_ms)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now_ms, member)
    redis.call('PEXPIRE', key, window_ms)
    return {1, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local oldest_ms = tonumber(oldest[2])
local retry_after_ms = oldest_ms + window_ms - now_ms
if retry_after_ms < 0 then
    retry_after_ms = 0
end
return {0, retry_after_ms}
"""

_client: redis.Redis | None = None
_script: redis.commands.core.Script | None = None


def _redis_client() -> redis.Redis:
    """Lazy singleton, same pattern as `api/queue_metrics.py`'s
    `_redis_client()` — kept as its own seam (rather than importing that one) so
    a test can fake this module's Redis independently of the queue-metrics one."""
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL)
    return _client


def _admission_script() -> redis.commands.core.Script:
    global _script
    if _script is None:
        _script = _redis_client().register_script(_SLIDING_WINDOW_LUA)
    return _script


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_s: int  # only meaningful when `allowed` is False


def check_rate_limit(scope: str, user_id: str) -> RateLimitResult:
    """Consume (or reject) one request against `(scope, user_id)`'s sliding-window
    budget. Pure Redis I/O, no HTTP concern — kept separate from `enforce` so the
    admission logic is testable without a FastAPI request/response cycle."""
    key = f"ratelimit:{scope}:{user_id}"
    window_ms = RATE_LIMIT_WINDOW_S * 1000
    # A unique member per call: two requests landing in the same millisecond must
    # still count as two entries in the ZSET, not collapse into one.
    member = f"{uuid.uuid4().hex}"
    allowed_raw, retry_after_ms = _admission_script()(
        keys=[key], args=[window_ms, RATE_LIMIT_MAX_REQUESTS, member]
    )
    if allowed_raw:
        return RateLimitResult(allowed=True, retry_after_s=0)
    # Ceil so a client never retries a moment too early because of truncation.
    retry_after_s = (int(retry_after_ms) + 999) // 1000
    return RateLimitResult(allowed=False, retry_after_s=retry_after_s)


def enforce(scope: str, user_id: str) -> None:
    """Raise `HTTPException(429)` with a `Retry-After` header if `user_id` is over
    quota for `scope`; no-op otherwise. The single call site FastAPI route
    dependencies use (see `api/main.py`'s `rate_limited`)."""
    result = check_rate_limit(scope, user_id)
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"rate limit exceeded: max {RATE_LIMIT_MAX_REQUESTS} requests "
                f"per {RATE_LIMIT_WINDOW_S}s, try again later"
            ),
            headers={"Retry-After": str(result.retry_after_s)},
        )
