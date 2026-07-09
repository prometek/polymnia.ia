"""Per-user rate limiting on job-triggering endpoints (issue #17).

Acceptance criteria under test (from the issue, not the implementation):
  1. Under-quota requests succeed as normal.
  2. Exceeding the per-user quota within the window returns 429.
  3. The 429 response carries a `Retry-After` header.
  4. The limit is per user_id: user A hitting their limit must not throttle user B.
  5. GET/read endpoints are not throttled the same way.
  6. The counter is a shared sliding window: once entries age out of the window,
     requests are admitted again.
  7. At least one end-to-end test drives a real endpoint through the FastAPI app
     (TestClient) and asserts the 429 + Retry-After behavior.

Each test uses a fresh, unique `user_id` (and therefore a fresh Redis ZSET key
`ratelimit:{scope}:{user_id}`), so tests shouldn't collide with each other in
practice. Redis itself is real here (not mocked, unlike `queue_metrics`/
`job_events` elsewhere in this suite) and — unlike the Postgres tables
`conftest.py` truncates before every test — nothing else resets it between
runs. `_flush_rate_limit_keys` below is a deterministic belt-and-suspenders
guard on top of the uniqueness discipline: it wipes every `ratelimit:*` key
before each test in this module, so a future test here that reuses a scope/id,
or a prior interrupted run that left keys behind, can't leak state across
tests.

Requires a reachable Redis (`REDIS_URL`) - same as `api/rate_limit.py` itself;
CI provisions one as a service container (see `.github/workflows/ci.yml`).
`RATE_LIMIT_MAX_REQUESTS`/`RATE_LIMIT_WINDOW_S` are read once at import time
into module-level constants (see `api/rate_limit.py`), so tests that need a
different quota monkeypatch those constants directly rather than the env var
(setting `os.environ` after import would have no effect).
"""

import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from api import db, rate_limit, service
from fastapi import HTTPException
from starlette.testclient import TestClient
from tasks import generation
from tasks import render as render_jobs


@pytest.fixture(autouse=True)
def _flush_rate_limit_keys() -> Iterator[None]:
    """Delete every `ratelimit:*` key before each test in this module (see the
    module docstring for why this exists on top of the unique-`user_id`
    discipline every test already follows)."""
    client = rate_limit._redis_client()
    keys = client.keys("ratelimit:*")
    if keys:
        client.delete(*keys)
    yield


def _unique_email() -> str:
    return f"ratelimit-{uuid.uuid4().hex}@test.local"


def _seed_project_with_scene(user_id: str) -> str:
    """Brand kit + video with one scene, owned by `user_id` — enough to exercise
    render/ai-edit, which both require an existing project."""
    version_id = db.upsert_brand_kit({"id": f"kit-{uuid.uuid4().hex}", "name": "K"}, user_id)
    vid = uuid.uuid4().hex[:12]
    db.create_video(vid, user_id, version_id, "v")
    db.replace_scenes(
        vid,
        [
            {
                "order": 0,
                "type": "statement",
                "composition": "centered",
                "props": {},
                "asset_refs": [],
                "timing": {"duration_s": 1.0, "audio_path": "audio/s0.wav"},
            }
        ],
    )
    return vid


# --- internal helper: `check_rate_limit`/`enforce` (api/rate_limit.py) ------


def test_check_rate_limit_admits_under_quota_then_rejects_over_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 3)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)
    scope = "unit:test"
    user_id = uuid.uuid4().hex

    for _ in range(3):
        result = rate_limit.check_rate_limit(scope, user_id)
        assert result.allowed is True

    over_quota = rate_limit.check_rate_limit(scope, user_id)
    assert over_quota.allowed is False
    assert over_quota.retry_after_s > 0


def test_check_rate_limit_is_scoped_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 1)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)
    scope = "unit:per-user"
    user_a = uuid.uuid4().hex
    user_b = uuid.uuid4().hex

    assert rate_limit.check_rate_limit(scope, user_a).allowed is True
    # user A is now over quota...
    assert rate_limit.check_rate_limit(scope, user_a).allowed is False
    # ...but user B has an independent budget, unaffected by A's usage.
    assert rate_limit.check_rate_limit(scope, user_b).allowed is True


def test_check_rate_limit_admits_again_after_window_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 1)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 1)
    scope = "unit:window-expiry"
    user_id = uuid.uuid4().hex

    assert rate_limit.check_rate_limit(scope, user_id).allowed is True
    assert rate_limit.check_rate_limit(scope, user_id).allowed is False

    time.sleep(1.2)  # let the 1s sliding window fully age out

    assert rate_limit.check_rate_limit(scope, user_id).allowed is True


def test_enforce_raises_429_with_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 1)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)
    scope = "unit:enforce"
    user_id = uuid.uuid4().hex

    rate_limit.enforce(scope, user_id)  # 1st call: under quota, no-op

    with pytest.raises(HTTPException) as exc_info:
        rate_limit.enforce(scope, user_id)  # 2nd call: over quota

    headers = exc_info.value.headers
    assert headers is not None
    assert "Retry-After" in headers
    assert int(headers["Retry-After"]) > 0
    assert exc_info.value.status_code == 429


# --- end-to-end: POST /projects (TestClient) --------------------------------


def test_e2e_new_project_under_quota_succeeds(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 1: under-quota requests succeed as normal."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 5)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)
    monkeypatch.setattr(generation.generate_task, "delay", lambda *a: None)

    uid = db.ensure_user(_unique_email())
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    as_user(uid)

    for _ in range(3):
        resp = client.post("/projects", json={"input_text": "hello", "brand_kit_id": "kit-a"})
        assert resp.status_code == 202


def test_e2e_new_project_over_quota_returns_429_with_retry_after(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criteria 2, 3, 7: exceeding quota on a real endpoint -> 429 + Retry-After,
    driven through the FastAPI app via TestClient (not just the internal helper)."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 2)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)
    monkeypatch.setattr(generation.generate_task, "delay", lambda *a: None)

    uid = db.ensure_user(_unique_email())
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    as_user(uid)

    body = {"input_text": "hello", "brand_kit_id": "kit-a"}
    for _ in range(2):
        assert client.post("/projects", json=body).status_code == 202

    resp = client.post("/projects", json=body)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0


def test_e2e_render_endpoint_over_quota_returns_429_with_retry_after(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same criteria, on the render endpoint specifically."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 1)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)
    monkeypatch.setattr(render_jobs.render_task, "delay", lambda *a: None)

    uid = db.ensure_user(_unique_email())
    vid = _seed_project_with_scene(uid)
    as_user(uid)

    assert client.post(f"/projects/{vid}/render").status_code == 202

    resp = client.post(f"/projects/{vid}/render")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0


def test_e2e_ai_edit_endpoint_over_quota_returns_429_with_retry_after(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same criteria, on the ai-edit endpoint specifically. `service.edit_ai` is
    stubbed out (it drives the LLM/TTS pipeline) — only the rate-limit boundary
    is under test here."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 1)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)

    def _fake_edit_ai(
        pid: str, scenes: list[dict[str, Any]], order: int, instruction: str, kit: Any
    ) -> dict[str, Any]:
        return scenes[order]

    monkeypatch.setattr(service, "edit_ai", _fake_edit_ai)

    uid = db.ensure_user(_unique_email())
    vid = _seed_project_with_scene(uid)
    as_user(uid)

    body = {"instruction": "make it punchier"}
    assert client.post(f"/projects/{vid}/scenes/0/ai-edit", json=body).status_code == 200

    resp = client.post(f"/projects/{vid}/scenes/0/ai-edit", json=body)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0


def test_e2e_rate_limit_is_per_user_not_global(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 4: user A hitting their limit must not throttle user B."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 1)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)
    monkeypatch.setattr(generation.generate_task, "delay", lambda *a: None)

    uid_a = db.ensure_user(_unique_email())
    uid_b = db.ensure_user(_unique_email())
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid_a)
    db.upsert_brand_kit({"id": "kit-b", "name": "B"}, uid_b)

    as_user(uid_a)
    body_a = {"input_text": "hello", "brand_kit_id": "kit-a"}
    assert client.post("/projects", json=body_a).status_code == 202
    # A is now over quota.
    assert client.post("/projects", json=body_a).status_code == 429

    # B has an independent budget on the same scope/endpoint.
    as_user(uid_b)
    body_b = {"input_text": "hello", "brand_kit_id": "kit-b"}
    assert client.post("/projects", json=body_b).status_code == 202


def test_e2e_get_projects_not_rate_limited_by_write_quota(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 5: a GET burst that would exceed the write quota still returns
    200 — reads are not throttled the same way as the job-triggering writes."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 2)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)

    uid = db.ensure_user(_unique_email())
    as_user(uid)

    # More requests than the write quota (2) allows for a rate-limited endpoint.
    for _ in range(10):
        resp = client.get("/projects")
        assert resp.status_code == 200


def test_e2e_rate_limit_window_resets_admits_again(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 6: the sliding window is shared/consistent — once the window
    elapses, a previously-throttled user is admitted again."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 1)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 1)
    monkeypatch.setattr(generation.generate_task, "delay", lambda *a: None)

    uid = db.ensure_user(_unique_email())
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    as_user(uid)

    body = {"input_text": "hello", "brand_kit_id": "kit-a"}
    assert client.post("/projects", json=body).status_code == 202
    assert client.post("/projects", json=body).status_code == 429

    time.sleep(1.2)  # let the 1s window fully age out

    assert client.post("/projects", json=body).status_code == 202


# --- edge cases / failure modes ----------------------------------------------


def test_check_rate_limit_config_rejects_non_positive_values() -> None:
    """Boundary: `RATE_LIMIT_MAX_REQUESTS`/`RATE_LIMIT_WINDOW_S` must be positive
    integers — invalid config fails loudly (import-time `RateLimitConfigError`),
    never a silent fallback."""
    import os

    old = os.environ.get("RATE_LIMIT_MAX_REQUESTS")
    os.environ["RATE_LIMIT_MAX_REQUESTS"] = "0"
    try:
        with pytest.raises(rate_limit.RateLimitConfigError):
            rate_limit._positive_int_env("RATE_LIMIT_MAX_REQUESTS", 20)
    finally:
        if old is None:
            os.environ.pop("RATE_LIMIT_MAX_REQUESTS", None)
        else:
            os.environ["RATE_LIMIT_MAX_REQUESTS"] = old

    os.environ["RATE_LIMIT_MAX_REQUESTS"] = "not-a-number"
    try:
        with pytest.raises(rate_limit.RateLimitConfigError):
            rate_limit._positive_int_env("RATE_LIMIT_MAX_REQUESTS", 20)
    finally:
        if old is None:
            os.environ.pop("RATE_LIMIT_MAX_REQUESTS", None)
        else:
            os.environ["RATE_LIMIT_MAX_REQUESTS"] = old


def test_check_rate_limit_exactly_at_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary value: exactly `RATE_LIMIT_MAX_REQUESTS` requests must all be
    admitted; only the request that would exceed it is rejected."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 5)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)
    scope = "unit:boundary"
    user_id = uuid.uuid4().hex

    results = [rate_limit.check_rate_limit(scope, user_id).allowed for _ in range(5)]
    assert results == [True] * 5
    assert rate_limit.check_rate_limit(scope, user_id).allowed is False


def test_e2e_different_scopes_have_independent_budgets(
    client: TestClient, as_user: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edge case: exhausting one endpoint's quota (`projects:create`) does not
    block a different job-triggering endpoint (`projects:render`) for the same
    user — each scope has its own budget."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_MAX_REQUESTS", 1)
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_WINDOW_S", 60)
    monkeypatch.setattr(generation.generate_task, "delay", lambda *a: None)
    monkeypatch.setattr(render_jobs.render_task, "delay", lambda *a: None)

    uid = db.ensure_user(_unique_email())
    db.upsert_brand_kit({"id": "kit-a", "name": "A"}, uid)
    vid = _seed_project_with_scene(uid)
    as_user(uid)

    body = {"input_text": "hello", "brand_kit_id": "kit-a"}
    assert client.post("/projects", json=body).status_code == 202
    assert client.post("/projects", json=body).status_code == 429  # projects:create exhausted

    # projects:render is a different scope, still has its own budget.
    assert client.post(f"/projects/{vid}/render").status_code == 202
