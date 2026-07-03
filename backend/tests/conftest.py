"""Shared pytest fixtures.

Endpoints are sync `def` (threadpool) — use Starlette's sync TestClient, not an
async client. DB-touching tests use a real test database (no DB mocks), per
docs/code-standards.md.

Isolation: we never touch the dev database. `DATABASE_URL` (from backend/.env,
or the env in CI) points at the dev DB; here we derive a sibling `<db>_test`
database, create it if absent, and set `DATABASE_URL` to it *before* importing
any `api.*` module (the engine is built at import time). Tables are (re)created
once per session and every table is truncated between tests for determinism.
"""

import os
from collections.abc import Iterator

import psycopg
from dotenv import load_dotenv

# --- Point the process at an isolated test DB before api.* imports ----------

load_dotenv()  # backend/.env → DATABASE_URL (same source the app uses)

_dev_url = os.environ["DATABASE_URL"]
_server, _db = _dev_url.rsplit("/", 1)
_db = _db.split("?", 1)[0]
_test_db = _db if _db.endswith("_test") else f"{_db}_test"
TEST_DATABASE_URL = f"{_server}/{_test_db}"


def _ensure_test_database() -> None:
    """Create the test database if it doesn't exist (connect to `postgres` admin db)."""
    admin_url = f"{_server}/postgres"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (_test_db,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{_test_db}"')


_ensure_test_database()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# Tests build the schema via create_all (not Alembic); opt in explicitly before
# importing api.* (the DEV_CREATE_ALL flag is read at import time).
os.environ["POLYMNIA_DEV_CREATE_ALL"] = "1"

# Import only after DATABASE_URL points at the test DB (engine is import-time).
import pytest  # noqa: E402
from api import db  # noqa: E402
from api.main import app, get_user_id  # noqa: E402
from api.session import engine  # noqa: E402
from sqlalchemy import text  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TABLES = ("scenes", "videos", "assets", "brand_kit_versions", "brand_kits", "users")


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    db.init_db()  # create_all — POC/test only, prod goes through Alembic


@pytest.fixture(autouse=True)
def _clean_db() -> Iterator[None]:
    """Truncate every table before each test → deterministic, order-independent."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def as_user() -> Iterator[object]:
    """Override the current-user dependency so a test can act as a chosen user_id."""

    def _set(user_id: str) -> None:
        app.dependency_overrides[get_user_id] = lambda: user_id

    yield _set
    app.dependency_overrides.pop(get_user_id, None)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """TestClient without triggering lifespan (no dev-user seeding — tests seed explicitly)."""
    yield TestClient(app)
