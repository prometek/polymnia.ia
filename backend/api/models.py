#!/usr/bin/env python3
"""
SQLModel table models — schema source of truth (doc §10, ADR-06).

users -> brand_kits -> brand_kit_versions (snapshot) -> assets
users -> videos (frozen to a brand_kit_version) -> scenes (one row per scene)

Layouts / style space are NOT in the DB: versioned with the render code (§11).
Schema changes go through Alembic; create_all is dev/POC only (see session.py).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    REAL,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlmodel import Field, SQLModel


def _uuid_pk() -> Any:
    return Field(
        default=None,
        sa_column=Column(
            PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
        ),
    )


def _created_at() -> Any:
    return Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


class User(SQLModel, table=True):
    """`clerk_user_id` (the Clerk `sub` claim, issue #16) is the stable identity key
    used to map a verified Clerk session to a local user — resolved once at first
    login (`db.get_or_create_user_by_clerk_id`) and never looked up by email again,
    since a Clerk user's email can change. `email` is kept for display/dev-mode
    (`db.ensure_user`, `AUTH_MODE=dev`) but is nullable: a Clerk identity's session
    token isn't guaranteed to carry an `email` claim depending on Clerk instance
    config. Both are unique but optional — a row is expected to have at least one
    of them set, enforced at the call sites (`ensure_user`/`get_or_create_user_by_clerk_id`),
    not a DB constraint (either can legitimately be absent depending on auth mode).
    """

    __tablename__ = "users"

    id: uuid.UUID | None = _uuid_pk()
    email: str | None = Field(default=None, sa_column=Column(Text, unique=True))
    clerk_user_id: str | None = Field(default=None, sa_column=Column(Text, unique=True))
    created_at: datetime | None = _created_at()


class BrandKit(SQLModel, table=True):
    __tablename__ = "brand_kits"

    id: str = Field(sa_column=Column(Text, primary_key=True))  # slug, e.g. 'cap-permis'
    user_id: uuid.UUID = Field(
        sa_column=Column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    )
    name: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime | None = _created_at()


class BrandKitVersion(SQLModel, table=True):
    __tablename__ = "brand_kit_versions"
    __table_args__ = (UniqueConstraint("brand_kit_id", "version"),)

    id: uuid.UUID | None = _uuid_pk()
    brand_kit_id: str = Field(sa_column=Column(Text, ForeignKey("brand_kits.id"), nullable=False))
    version: int = Field(sa_column=Column(Integer, nullable=False))
    cosmetic: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    style: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    created_at: datetime | None = _created_at()


class Asset(SQLModel, table=True):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("brand_kit_version_id", "ref"),)

    id: uuid.UUID | None = _uuid_pk()
    brand_kit_version_id: uuid.UUID = Field(
        sa_column=Column(
            PgUUID(as_uuid=True),
            ForeignKey("brand_kit_versions.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    ref: str = Field(sa_column=Column(Text, nullable=False))  # 'icon-anchor', 'logo-dark'
    type: str = Field(sa_column=Column(Text, nullable=False))  # icon | logo
    emoji: str | None = Field(default=None, sa_column=Column(Text))
    glyph: str | None = Field(default=None, sa_column=Column(Text))
    file: str | None = Field(default=None, sa_column=Column(Text))
    usage: str | None = Field(default=None, sa_column=Column(Text))
    is_primary: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false"))
    )
    meta: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )


class Video(SQLModel, table=True):
    __tablename__ = "videos"

    id: str = Field(sa_column=Column(Text, primary_key=True))
    user_id: uuid.UUID = Field(
        sa_column=Column(PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    )
    brand_kit_version_id: uuid.UUID = Field(  # frozen snapshot (ADR-06)
        sa_column=Column(PgUUID(as_uuid=True), ForeignKey("brand_kit_versions.id"), nullable=False)
    )
    name: str | None = Field(default=None, sa_column=Column(Text))
    status: str = Field(
        default="draft", sa_column=Column(Text, nullable=False, server_default=text("'draft'"))
    )
    total_duration_s: float = Field(
        default=0.0, sa_column=Column(REAL, nullable=False, server_default=text("0"))
    )
    # Storage key (issue #12), not a filesystem path — resolved via api/storage.py.
    mp4_path: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime | None = _created_at()
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
        ),
    )


class Job(SQLModel, table=True):
    """Durable work item for the queue (issue #6): decouples generation/render from
    the API process. `type` says what the worker runs; `status` tracks its lifecycle
    (queued -> running -> retrying -> done/error/dead), separate from the video's
    own status vocabulary. `retrying` and `dead` are issue #11 / PRO-10: a failed
    attempt lands on the non-terminal `retrying` (with that attempt's `error`) while
    a bounded Celery retry is still pending, so `GET /jobs/{id}` and the SSE stream
    (issue #10) don't report a false terminal `error` mid-backoff; once retries are
    exhausted, `dead` is the dead-letter queue proper — `error`/`step` stay populated
    so it's inspectable via the read endpoint (PRO-08).
    """

    __tablename__ = "jobs"

    id: uuid.UUID | None = _uuid_pk()
    video_id: str = Field(
        sa_column=Column(
            Text, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    type: str = Field(sa_column=Column(Text, nullable=False))  # generation | render
    status: str = Field(
        default="queued", sa_column=Column(Text, nullable=False, server_default=text("'queued'"))
    )
    error: str | None = Field(default=None, sa_column=Column(Text))
    step: str | None = Field(default=None, sa_column=Column(Text))  # optional worker progress label
    progress: int | None = Field(default=None, sa_column=Column(Integer))  # optional 0..100
    created_at: datetime | None = _created_at()
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
        ),
    )


class Scene(SQLModel, table=True):
    __tablename__ = "scenes"
    __table_args__ = (UniqueConstraint("video_id", "ord"),)

    id: uuid.UUID | None = _uuid_pk()
    video_id: str = Field(
        sa_column=Column(Text, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    )
    ord: int = Field(sa_column=Column(Integer, nullable=False))
    type: str = Field(sa_column=Column(Text, nullable=False))
    composition: str | None = Field(default=None, sa_column=Column(Text))
    props: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default=text("'{}'"))
    )
    asset_refs: list[Any] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False, server_default=text("'[]'"))
    )
    timing: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default=text("'{}'"))
    )
