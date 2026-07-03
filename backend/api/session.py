#!/usr/bin/env python3
"""
Engine + session for SQLModel. One engine per process; one Session per unit of work.

Production schema changes go through Alembic. `init_db` (create_all) is dev/POC only.
"""

import logging
import os
from collections.abc import Iterator

from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine

from . import models  # noqa: F401  (import registers tables on SQLModel.metadata)

load_dotenv()

logger = logging.getLogger("polymnia.session")

# Explicit opt-in for the dev/POC create_all path. Prod schema is owned by Alembic
# (`alembic upgrade head`); without this flag, init_db never touches the schema, so a
# prod process can't silently bypass migrations.
DEV_CREATE_ALL = os.environ.get("POLYMNIA_DEV_CREATE_ALL") == "1"


def _normalize(url: str) -> str:
    """SQLAlchemy needs an explicit driver; this project uses psycopg 3."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


DATABASE_URL = _normalize(os.environ["DATABASE_URL"])
engine = create_engine(DATABASE_URL)


def init_db() -> None:
    """Dev/POC only: create tables from the models via create_all.

    No-op unless POLYMNIA_DEV_CREATE_ALL=1 — prod schema goes through Alembic
    (`alembic upgrade head`). Tests opt in explicitly (see tests/conftest.py).
    """
    if not DEV_CREATE_ALL:
        logger.info("init_db skipped: POLYMNIA_DEV_CREATE_ALL not set (schema owned by Alembic)")
        return
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, auto-closed."""
    with Session(engine) as session:
        yield session
