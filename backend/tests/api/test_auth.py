"""Clerk authentication (issue #16).

Covers the ticket's acceptance criteria end to end, driving the real
`get_current_user` -> `api.auth.verify_clerk_request` path (no
`app.dependency_overrides` shortcut, unlike most other test modules'
`as_user` fixture) plus focused unit coverage of `verify_clerk_request` and
`db.get_or_create_user_by_clerk_id` in isolation:

  1. No/invalid token -> 401 in clerk mode.
  2. First login of a new Clerk user creates a local `users` row keyed by
     `clerk_user_id`; a later request with the same `sub` but a changed
     email resolves the SAME local user (no duplicate) and syncs the email.
  3. Inter-user isolation: two distinct Clerk `sub`s -> two local users,
     neither can see/act on the other's resources.
  4. Missing `CLERK_SECRET_KEY` in clerk mode -> typed `AuthConfigError`
     (500), never a silent dev fallback.
  5. `AUTH_MODE=dev` resolves the configured dev identity without any
     token, and never attempts Clerk verification.

Clerk itself is never contacted: `api.auth.get_clerk_client` (the seam the
module's own docstring calls out as mockable) is monkeypatched to a fake
client whose `authenticate_request` reads the *real* request's
`Authorization` header against a table of tokens the test controls. So
`get_current_user`/`verify_clerk_request` run unmodified for every HTTP-level
test here; only the actual network round trip to Clerk is faked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from api import auth, db
from api.models import User
from api.session import engine
from fastapi import HTTPException
from sqlmodel import Session, select
from starlette.testclient import TestClient

# --- Fake Clerk SDK seam -----------------------------------------------------


@dataclass
class _FakeAuthState:
    """Duck-types the bits of the SDK's `RequestState` that `verify_clerk_request`
    actually reads (`is_signed_in`, `message`, `payload`) — cheaper than building a
    real `clerk_backend_api` `RequestState`, which needs a signed token to reach a
    SIGNED_IN status."""

    is_signed_in: bool
    message: str | None = None
    payload: dict[str, Any] | None = None


class _FakeClerkClient:
    """Stands in for `clerk_backend_api.Clerk`: `authenticate_request` reads the
    *real* request's `Authorization` header and resolves it against a table of
    tokens this test controls, so the full `get_current_user` ->
    `verify_clerk_request` -> SDK-call chain runs for real; only the Clerk network
    round trip is faked."""

    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}

    def issue(self, token: str, *, sub: str, email: str | None = None) -> None:
        self.tokens[token] = {"sub": sub, "email": email}

    def authenticate_request(self, request: Any, options: Any) -> _FakeAuthState:
        authz = request.headers.get("authorization") or request.headers.get("Authorization")
        token = authz[len("Bearer ") :].strip() if authz and authz.startswith("Bearer ") else None
        if token is None:
            return _FakeAuthState(is_signed_in=False, message="no session token in the request")
        claims = self.tokens.get(token)
        if claims is None:
            return _FakeAuthState(is_signed_in=False, message="session token failed verification")
        return _FakeAuthState(is_signed_in=True, payload=dict(claims))


@pytest.fixture
def fake_clerk(monkeypatch: pytest.MonkeyPatch) -> _FakeClerkClient:
    """Force clerk mode + a present (fake) secret key, then wire the fake client
    into the documented mock seam (`api.auth.get_clerk_client`)."""
    monkeypatch.setattr(auth, "AUTH_MODE", "clerk")
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    client = _FakeClerkClient()
    monkeypatch.setattr(auth, "get_clerk_client", lambda: client)
    return client


def _user_row(clerk_user_id: str) -> User | None:
    with Session(engine) as s:
        return s.exec(select(User).where(User.clerk_user_id == clerk_user_id)).one_or_none()


def _all_users() -> list[User]:
    with Session(engine) as s:
        return list(s.exec(select(User)).all())


# --- 1. No/invalid token -> 401 (through the real HTTP route) ---------------


def test_no_token_returns_401(client: TestClient, fake_clerk: _FakeClerkClient) -> None:
    resp = client.get("/projects")
    assert resp.status_code == 401


def test_bogus_token_returns_401(client: TestClient, fake_clerk: _FakeClerkClient) -> None:
    resp = client.get("/projects", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_malformed_authorization_header_returns_401(
    client: TestClient, fake_clerk: _FakeClerkClient
) -> None:
    """No `Bearer ` prefix at all — still a hard 401, not a 500/422."""
    resp = client.get("/projects", headers={"Authorization": "not-even-bearer-shaped"})
    assert resp.status_code == 401


def test_failed_auth_never_creates_a_user_row(
    client: TestClient, fake_clerk: _FakeClerkClient
) -> None:
    client.get("/projects", headers={"Authorization": "Bearer garbage"})
    assert _all_users() == []


# --- 2. First login creates a local user; later login syncs, no duplicate --


def test_first_login_creates_local_user_and_scopes_resources(
    client: TestClient, fake_clerk: _FakeClerkClient
) -> None:
    fake_clerk.issue("tok-a", sub="clerk_user_aaa", email="alice@example.com")
    headers = {"Authorization": "Bearer tok-a"}

    resp = client.post("/brand-kits", json={"id": "kit-a", "name": "A"}, headers=headers)
    assert resp.status_code == 201

    row = _user_row("clerk_user_aaa")
    assert row is not None
    assert row.email == "alice@example.com"

    listed = client.get("/brand-kits", headers=headers).json()
    assert [k["id"] for k in listed] == ["kit-a"]


def test_second_login_same_sub_changed_email_resolves_same_user_no_duplicate(
    client: TestClient, fake_clerk: _FakeClerkClient
) -> None:
    fake_clerk.issue("tok-b1", sub="clerk_user_bbb", email="bob@old.example.com")
    headers1 = {"Authorization": "Bearer tok-b1"}
    assert (
        client.post("/brand-kits", json={"id": "kit-b", "name": "B"}, headers=headers1).status_code
        == 201
    )
    first_id = _user_row("clerk_user_bbb")
    assert first_id is not None

    fake_clerk.issue("tok-b2", sub="clerk_user_bbb", email="bob@new.example.com")
    headers2 = {"Authorization": "Bearer tok-b2"}
    # Same underlying user resolved -> still sees the kit created under the old email.
    listed = client.get("/brand-kits", headers=headers2).json()
    assert [k["id"] for k in listed] == ["kit-b"]

    with Session(engine) as s:
        rows = s.exec(select(User).where(User.clerk_user_id == "clerk_user_bbb")).all()
    assert len(rows) == 1  # no duplicate row
    assert rows[0].id == first_id.id
    assert rows[0].email == "bob@new.example.com"  # synced


def test_get_or_create_user_by_clerk_id_creates_then_reuses() -> None:
    """Unit-level: `db.get_or_create_user_by_clerk_id` directly, no HTTP/Clerk."""
    uid1 = db.get_or_create_user_by_clerk_id("sub-1", "x@test.local")
    uid2 = db.get_or_create_user_by_clerk_id("sub-1", "x@test.local")
    assert uid1 == uid2
    with Session(engine) as s:
        rows = s.exec(select(User).where(User.clerk_user_id == "sub-1")).all()
    assert len(rows) == 1


def test_get_or_create_user_by_clerk_id_syncs_changed_email() -> None:
    uid = db.get_or_create_user_by_clerk_id("sub-2", "old@test.local")
    uid_again = db.get_or_create_user_by_clerk_id("sub-2", "new@test.local")
    assert uid == uid_again
    with Session(engine) as s:
        row = s.get(User, uuid.UUID(uid))
    assert row is not None
    assert row.email == "new@test.local"


def test_get_or_create_user_by_clerk_id_accepts_missing_email_claim() -> None:
    """Not every Clerk token carries an `email` claim (see `ClerkIdentity` docstring)
    — must not require one to create/resolve the local user."""
    uid = db.get_or_create_user_by_clerk_id("sub-3", None)
    with Session(engine) as s:
        row = s.get(User, uuid.UUID(uid))
    assert row is not None
    assert row.email is None
    assert row.clerk_user_id == "sub-3"


# --- 3. Inter-user isolation --------------------------------------------------


def test_inter_user_isolation_through_clerk_identities(
    client: TestClient, fake_clerk: _FakeClerkClient
) -> None:
    """Two distinct Clerk `sub`s resolve to two distinct local users (PRO-01
    scoping does the rest): a stranger's project id 404s, same as an unknown one."""
    uid_a = db.get_or_create_user_by_clerk_id("clerk-user-a", "a@example.com")
    version_id = db.upsert_brand_kit({"id": "kit-iso", "name": "iso"}, uid_a)
    vid = uuid.uuid4().hex[:12]
    db.create_video(vid, uid_a, version_id, "A's video")

    fake_clerk.issue("tok-a", sub="clerk-user-a", email="a@example.com")
    fake_clerk.issue("tok-b", sub="clerk-user-b", email="b@example.com")

    resp_owner = client.get(f"/projects/{vid}", headers={"Authorization": "Bearer tok-a"})
    assert resp_owner.status_code == 200

    resp_stranger = client.get(f"/projects/{vid}", headers={"Authorization": "Bearer tok-b"})
    assert resp_stranger.status_code == 404

    # The stranger's own login must still have created their own local user...
    uid_b = _user_row("clerk-user-b")
    assert uid_b is not None
    # ...distinct from A's.
    assert str(uid_b.id) != uid_a


def test_inter_user_isolation_project_listing(
    client: TestClient, fake_clerk: _FakeClerkClient
) -> None:
    uid_a = db.get_or_create_user_by_clerk_id("clerk-user-list-a", "la@example.com")
    uid_b = db.get_or_create_user_by_clerk_id("clerk-user-list-b", "lb@example.com")
    version_a = db.upsert_brand_kit({"id": "kit-list-a", "name": "a"}, uid_a)
    version_b = db.upsert_brand_kit({"id": "kit-list-b", "name": "b"}, uid_b)
    vid_a = uuid.uuid4().hex[:12]
    vid_b = uuid.uuid4().hex[:12]
    db.create_video(vid_a, uid_a, version_a, "A")
    db.create_video(vid_b, uid_b, version_b, "B")

    fake_clerk.issue("tok-la", sub="clerk-user-list-a", email="la@example.com")

    resp = client.get("/projects", headers={"Authorization": "Bearer tok-la"})
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()}
    assert ids == {vid_a}
    assert vid_b not in ids


# --- 4. Missing Clerk config -> typed AuthConfigError (500) ------------------


def test_missing_clerk_secret_key_raises_auth_config_error_through_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "AUTH_MODE", "clerk")
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    with pytest.raises(auth.AuthConfigError, match="CLERK_SECRET_KEY"):
        client.get("/projects")


def test_missing_clerk_secret_key_is_not_a_silent_dev_fallback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing config in clerk mode must never resolve the dev identity — it's a
    hard failure, not an implicit `AUTH_MODE=dev`."""
    monkeypatch.setattr(auth, "AUTH_MODE", "clerk")
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    with pytest.raises(auth.AuthConfigError):
        client.get("/projects")
    assert _all_users() == []  # never fell through to db.ensure_user(DEV_EMAIL)


# --- 5. AUTH_MODE=dev ---------------------------------------------------------


def test_dev_mode_resolves_dev_identity_without_any_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "AUTH_MODE", "dev")

    resp = client.get("/projects")
    assert resp.status_code == 200
    assert resp.json() == []

    dev_uid = db.ensure_user(auth.DEV_EMAIL)
    version_id = db.upsert_brand_kit({"id": "kit-dev", "name": "dev"}, dev_uid)
    vid = uuid.uuid4().hex[:12]
    db.create_video(vid, dev_uid, version_id, "dev video")

    resp2 = client.get("/projects")
    assert resp2.status_code == 200
    assert [p["id"] for p in resp2.json()] == [vid]


def test_dev_mode_never_attempts_clerk_verification(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dev mode must not call into `verify_clerk_request`/Clerk at all — a garbage
    (or absent) `Authorization` header is irrelevant in this mode, unlike clerk
    mode where the same header would 401."""
    monkeypatch.setattr(auth, "AUTH_MODE", "dev")

    def _boom(request: Any) -> Any:
        raise AssertionError("verify_clerk_request must not be called in AUTH_MODE=dev")

    monkeypatch.setattr(auth, "verify_clerk_request", _boom)

    resp = client.get("/projects", headers={"Authorization": "Bearer totally-not-a-token"})
    assert resp.status_code == 200


# --- Unit-level: verify_clerk_request edge cases ------------------------------


def test_verify_clerk_request_returns_identity_for_signed_in_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    fake = _FakeClerkClient()
    fake.issue("tok", sub="sub-x", email="x@test.local")
    monkeypatch.setattr(auth, "get_clerk_client", lambda: fake)

    request = SimpleNamespace(headers={"authorization": "Bearer tok"})
    identity = auth.verify_clerk_request(request)
    assert identity.clerk_user_id == "sub-x"
    assert identity.email == "x@test.local"


def test_verify_clerk_request_raises_401_when_not_signed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    fake = _FakeClerkClient()
    monkeypatch.setattr(auth, "get_clerk_client", lambda: fake)

    request = SimpleNamespace(headers={})
    with pytest.raises(HTTPException) as exc_info:
        auth.verify_clerk_request(request)
    assert exc_info.value.status_code == 401


def test_verify_clerk_request_raises_401_when_payload_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")

    class _NoPayloadClient:
        def authenticate_request(self, request: Any, options: Any) -> _FakeAuthState:
            return _FakeAuthState(is_signed_in=True, payload=None)

    monkeypatch.setattr(auth, "get_clerk_client", lambda: _NoPayloadClient())

    request = SimpleNamespace(headers={"authorization": "Bearer tok"})
    with pytest.raises(HTTPException) as exc_info:
        auth.verify_clerk_request(request)
    assert exc_info.value.status_code == 401


def test_verify_clerk_request_raises_401_when_sub_claim_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")

    class _NoSubClient:
        def authenticate_request(self, request: Any, options: Any) -> _FakeAuthState:
            return _FakeAuthState(is_signed_in=True, payload={"email": "x@test.local"})

    monkeypatch.setattr(auth, "get_clerk_client", lambda: _NoSubClient())

    request = SimpleNamespace(headers={"authorization": "Bearer tok"})
    with pytest.raises(HTTPException) as exc_info:
        auth.verify_clerk_request(request)
    assert exc_info.value.status_code == 401


def test_verify_clerk_request_raises_auth_config_error_when_secret_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    request = SimpleNamespace(headers={})
    with pytest.raises(auth.AuthConfigError):
        auth.verify_clerk_request(request)


# --- Edge case: dev-mode and Clerk identities must never collide on email ---


def test_dev_user_and_clerk_user_with_same_email_do_not_silently_merge(
    client: TestClient, fake_clerk: _FakeClerkClient
) -> None:
    """`ensure_user` (dev/email-keyed) and `get_or_create_user_by_clerk_id`
    (clerk_user_id-keyed) are documented as never-conflated identities (see
    `db.get_or_create_user_by_clerk_id`'s docstring). A dev user and a Clerk login
    sharing the same email is a realistic edge case (e.g. dev->prod transition) —
    exercised here to confirm the app doesn't silently merge two different
    people's accounts just because their email matches. `users.email` also has a
    DB-level uniqueness constraint (`api/models.py`'s `User.email`), so this may
    surface as a raised `IntegrityError` rather than a merge; either way it must
    not silently attribute one user's resources to the other's local account.
    """
    shared_email = "shared@example.com"
    dev_uid = db.ensure_user(shared_email)

    fake_clerk.issue("tok-shared", sub="clerk-user-shared", email=shared_email)
    try:
        resp = client.get("/brand-kits", headers={"Authorization": "Bearer tok-shared"})
    except Exception as exc:  # noqa: BLE001 - documenting actual behavior, not asserting a type
        pytest.fail(
            "get_or_create_user_by_clerk_id raised on an email collision with an "
            f"existing dev user instead of handling it explicitly: {type(exc).__name__}: {exc}"
        )
    else:
        assert resp.status_code == 200
        clerk_row = _user_row("clerk-user-shared")
        assert clerk_row is not None
        # Must resolve to a DIFFERENT local user than the dev account, not merge into it.
        assert str(clerk_row.id) != dev_uid
