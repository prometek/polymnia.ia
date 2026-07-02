#!/usr/bin/env python3
"""
Engine + session for SQLModel. One engine per process; one Session per unit of work.

Production schema changes go through Alembic. `init_db` (create_all) is dev/POC only.
"""

import os
from collections.abc import Iterator

from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine

from . import models  # noqa: F401  (import registers tables on SQLModel.metadata)

load_dotenv()


def _normalize(url: str) -> str:
    """SQLAlchemy needs an explicit driver; this project uses psycopg 3."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


DATABASE_URL = _normalize(os.environ["DATABASE_URL"])
engine = create_engine(DATABASE_URL)


def init_db() -> None:
    """POC: create tables if absent. Real schema changes go through Alembic."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, auto-closed."""
    with Session(engine) as session:
        yield session
