#!/usr/bin/env python3
"""Clerk authentication (issue #16): token verification, isolated from the FastAPI
dependency layer so it's unit-testable without a live Clerk instance (mock
`get_clerk_client`/`verify_clerk_request` directly, no HTTP needed).

Two modes, selected by `AUTH_MODE` (`clerk` default, or `dev`), read once at import
time — never silently switched at runtime:
  - `clerk`: every request must carry a valid Clerk session token (`Authorization:
    Bearer <token>`); verified via the `clerk-backend-api` SDK (no manual JWT/JWKS
    handling — `Clerk.authenticate_request`, which networklessly verifies against
    `CLERK_JWT_KEY` if set, or fetches/caches Clerk's JWKS otherwise).
  - `dev`: for local `./run.sh`/`uvicorn --reload` only — skips Clerk and resolves a
    single configured dev identity instead (see `api/main.py`'s `get_current_user`).
    Never a fallback *within* `clerk` mode: a missing/invalid token there is always
    a hard 401, and missing Clerk config is always a hard `AuthConfigError`.
"""

import os
from dataclasses import dataclass
from functools import lru_cache

from clerk_backend_api import Clerk
from clerk_backend_api.security.types import AuthenticateRequestOptions, Requestish, TokenType
from fastapi import HTTPException


class AuthConfigError(Exception):
    """Auth configuration is invalid (unknown `AUTH_MODE`, or `CLERK_SECRET_KEY`
    missing while `AUTH_MODE=clerk`).

    Mirrors `api/storage.py`'s `StorageConfigError`: a clear, typed error raised at
    the config boundary / point of use, not a silent fallback to another mode.
    """


_VALID_AUTH_MODES = frozenset({"clerk", "dev"})

AUTH_MODE = os.environ.get("AUTH_MODE", "clerk")  # clerk | dev
if AUTH_MODE not in _VALID_AUTH_MODES:
    # Fail at import (config boundary), not on the first request — an unknown mode
    # is a deploy misconfiguration, never something to silently coerce to "clerk".
    raise AuthConfigError(f"unknown AUTH_MODE={AUTH_MODE!r}; expected 'clerk' or 'dev'")

# AUTH_MODE=dev only: the single local identity every request resolves to (mirrors
# the old unconditional DEV_EMAIL seed, now opt-in — issue #16).
DEV_EMAIL = os.environ.get("AUTH_DEV_EMAIL", "dev@polymnia.local")

# Networkless verification key (PEM), read once at the config boundary rather than
# per-request (code-standards.md: env access confined to the frontier, not scattered
# through logic). Optional — unset means the SDK fetches/caches Clerk's JWKS instead.
CLERK_JWT_KEY = os.environ.get("CLERK_JWT_KEY") or None

# Optional allowlist of authorized parties (`azp` claim, typically the calling
# origin) — comma-separated, e.g. "https://app.polymnia.ai,https://staging...".
# Unset -> None -> the SDK applies no restriction (unchanged prior behavior); set,
# this blocks a session token minted for one origin being replayed against another.
_authorized_parties_raw = os.environ.get("CLERK_AUTHORIZED_PARTIES", "")
CLERK_AUTHORIZED_PARTIES = (
    [p.strip() for p in _authorized_parties_raw.split(",") if p.strip()]
    if _authorized_parties_raw
    else None
)


@dataclass(frozen=True)
class ClerkIdentity:
    """A verified Clerk identity, resolved from a session token's claims."""

    clerk_user_id: str  # `sub` claim — stable, survives email changes
    email: str | None  # `email` claim — not guaranteed present on every token


def _clerk_secret_key() -> str:
    key = os.environ.get("CLERK_SECRET_KEY")
    if not key:
        raise AuthConfigError("AUTH_MODE=clerk requires CLERK_SECRET_KEY to be set.")
    return key


@lru_cache(maxsize=1)
def get_clerk_client() -> Clerk:
    """The `Clerk` SDK client, built once per process. A separate seam from
    `verify_clerk_request` so a test can monkeypatch either: this one to swap in a
    fake client, or the other to skip the SDK call entirely."""
    return Clerk(bearer_auth=_clerk_secret_key())


def verify_clerk_request(request: Requestish) -> ClerkIdentity:
    """Verify `request`'s bearer/session token via the Clerk SDK and resolve the
    stable Clerk identity. `request` only needs a `.headers` mapping (Starlette's
    `Request` satisfies this) — kept structurally typed so this stays callable from
    a unit test without building a real FastAPI `Request`.

    Raises `HTTPException(401)` if the token is missing/invalid/expired. Raises
    `AuthConfigError` (uncaught here, propagates as a 500) if `CLERK_SECRET_KEY`
    isn't configured — a deploy misconfiguration, not a client auth failure.
    """
    options = AuthenticateRequestOptions(
        secret_key=_clerk_secret_key(),
        jwt_key=CLERK_JWT_KEY,
        authorized_parties=CLERK_AUTHORIZED_PARTIES,
        # This app only ever authenticates end-user login sessions (ticket scope) —
        # without this, `accepts_token` defaults to `['any']` and also validates
        # machine/OAuth/API-key tokens, which happen to get rejected downstream only
        # incidentally (no `sub` claim), not by an explicit, intentional check
        # (issue #16 code review).
        accepts_token=[TokenType.SESSION_TOKEN.value],
    )
    state = get_clerk_client().authenticate_request(request, options)
    if not state.is_signed_in:
        raise HTTPException(401, state.message or "missing or invalid session token")
    if state.payload is None:
        # is_signed_in implies a payload per the SDK's own contract (RequestState) —
        # guarded rather than asserted so a future SDK change fails as a clean 401
        # instead of an unhandled AssertionError leaking a 500.
        raise HTTPException(401, "session token carried no claims")
    sub = state.payload.get("sub")
    if not sub:
        raise HTTPException(401, "session token missing 'sub' claim")
    return ClerkIdentity(clerk_user_id=sub, email=state.payload.get("email"))
