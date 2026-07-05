#!/usr/bin/env python3
"""
Persistence layer (SQLModel over PostgreSQL) — relational schema (doc §10, ADR-06).

Table models live in models.py; the engine/session in session.py. This module exposes
domain functions returning plain dicts: the generation pipeline (fill/tts/pack_render)
is dict-based, so we convert ORM rows → dicts at this boundary.
"""

import uuid
from typing import Any

from sqlmodel import Session, col, select

from .models import Asset, BrandKit, BrandKitVersion, Job, Scene, User, Video
from .session import engine, init_db

__all__ = [
    "assets_of_version",
    "create_job",
    "create_video",
    "ensure_user",
    "get_job",
    "get_scenes",
    "get_video",
    "init_db",
    "kit_from_version",
    "latest_version_id",
    "list_brand_kits",
    "list_videos",
    "replace_scenes",
    "set_job_status",
    "set_job_step",
    "set_mp4",
    "set_status",
    "set_total",
    "upsert_brand_kit",
    "upsert_scene",
]

# Asset fields kept as dedicated columns; everything else goes to meta.
_ASSET_COLS = ("id", "type", "emoji", "glyph", "file", "usage", "primary")


# --- Users -----------------------------------------------------------------


def ensure_user(email: str) -> str:
    with Session(engine) as s:
        user = s.exec(select(User).where(User.email == email)).one_or_none()
        if user is None:
            user = User(email=email)
            s.add(user)
            s.commit()
            s.refresh(user)
        return str(user.id)


# --- Brand kits + versions -------------------------------------------------


def _kit_content(kit: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw kit JSON into the versioned parts (assets handled separately)."""
    return {
        "cosmetic": kit.get("cosmetic", {}),
        "style": {
            "visualStyle": kit.get("visualStyle"),
            "kicker_style": kit.get("kicker_style", "thematic"),
            "voice": kit.get("voice", {}),
        },
    }


def _row_to_asset(a: Asset) -> dict[str, Any]:
    """assets row -> kit asset dict (id = ref, primary = is_primary, meta merged)."""
    out: dict[str, Any] = {"id": a.ref, "type": a.type}
    for k in ("emoji", "glyph", "file", "usage"):
        val = getattr(a, k)
        if val is not None:
            out[k] = val
    if a.is_primary:
        out["primary"] = True
    out.update(a.meta or {})
    return out


def _norm_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical form for change detection (order-independent)."""
    return sorted(
        (
            {k: a.get(k) for k in ("id", "type", "emoji", "glyph", "file", "usage", "primary")}
            | {"meta": {k: v for k, v in a.items() if k not in _ASSET_COLS}}
            for a in assets
        ),
        key=lambda x: x.get("id") or "",
    )


def _insert_assets(s: Session, version_id: uuid.UUID, assets: list[dict[str, Any]]) -> None:
    for a in assets:
        meta = {k: v for k, v in a.items() if k not in _ASSET_COLS}
        s.add(
            Asset(
                brand_kit_version_id=version_id,
                ref=a.get("id"),
                type=a.get("type"),
                emoji=a.get("emoji"),
                glyph=a.get("glyph"),
                file=a.get("file"),
                usage=a.get("usage"),
                is_primary=bool(a.get("primary", False)),
                meta=meta,
            )
        )


def _assets_of_version(s: Session, version_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = s.exec(
        select(Asset).where(Asset.brand_kit_version_id == version_id).order_by(col(Asset.ref))
    ).all()
    return [_row_to_asset(r) for r in rows]


def upsert_brand_kit(kit: dict[str, Any], user_id: str) -> str:
    """Upsert the kit; create a new VERSION only if cosmetic/style/assets changed."""
    bk_id = kit["id"]
    content = _kit_content(kit)
    new_assets = kit.get("assets", [])
    with Session(engine) as s:
        bk = s.get(BrandKit, bk_id)
        if bk is None:
            s.add(BrandKit(id=bk_id, user_id=uuid.UUID(user_id), name=kit.get("name")))
        else:
            bk.name = kit.get("name")
        latest = s.exec(
            select(BrandKitVersion)
            .where(BrandKitVersion.brand_kit_id == bk_id)
            .order_by(col(BrandKitVersion.version).desc())
        ).first()
        if latest is not None:
            assert latest.id is not None  # PK is set on a persisted row
            same = (
                latest.cosmetic == content["cosmetic"]
                and latest.style == content["style"]
                and _norm_assets(_assets_of_version(s, latest.id)) == _norm_assets(new_assets)
            )
            if same:
                s.commit()  # persist any name change
                return str(latest.id)
        version = (latest.version + 1) if latest else 1
        v = BrandKitVersion(
            brand_kit_id=bk_id,
            version=version,
            cosmetic=content["cosmetic"],
            style=content["style"],
        )
        s.add(v)
        s.commit()
        s.refresh(v)
        assert v.id is not None  # PK populated by the DB on commit
        _insert_assets(s, v.id, new_assets)
        s.commit()
        return str(v.id)


def assets_of_version(version_id: str) -> list[dict[str, Any]]:
    with Session(engine) as s:
        return _assets_of_version(s, uuid.UUID(version_id))


def latest_version_id(brand_kit_id: str, user_id: str) -> str | None:
    with Session(engine) as s:
        vid = s.exec(
            select(BrandKitVersion.id)
            .join(BrandKit)
            .where(
                BrandKitVersion.brand_kit_id == brand_kit_id,
                BrandKit.user_id == uuid.UUID(user_id),
            )
            .order_by(col(BrandKitVersion.version).desc())
        ).first()
        return str(vid) if vid else None


def kit_from_version(version_id: str, user_id: str) -> dict[str, Any] | None:
    """Reconstruct the kit dict the pipeline expects, from a frozen version.

    Returns None if the version doesn't exist or its brand kit belongs to another
    user (ownership check — callers surface this as 404/409, no existence leak).
    """
    with Session(engine) as s:
        v = s.get(BrandKitVersion, uuid.UUID(version_id))
        if v is None:
            return None
        assert v.id is not None
        bk = s.get(BrandKit, v.brand_kit_id)
        if bk is None or bk.user_id != uuid.UUID(user_id):
            return None
        style = v.style or {}
        return {
            "id": v.brand_kit_id,
            "name": bk.name if bk else None,
            "visualStyle": style.get("visualStyle"),
            "kicker_style": style.get("kicker_style", "thematic"),
            "voice": style.get("voice", {}),
            "cosmetic": v.cosmetic,
            "assets": _assets_of_version(s, v.id),
        }


def list_brand_kits(user_id: str) -> list[dict[str, Any]]:
    with Session(engine) as s:
        rows = s.exec(
            select(BrandKit)
            .where(BrandKit.user_id == uuid.UUID(user_id))
            .order_by(col(BrandKit.created_at))
        ).all()
        return [{"id": b.id, "name": b.name} for b in rows]


# --- Videos ----------------------------------------------------------------


def _video_to_dict(v: Video) -> dict[str, Any]:
    return {
        "id": v.id,
        "name": v.name,
        "brand_kit_version_id": str(v.brand_kit_version_id),
        "status": v.status,
        "total_duration_s": v.total_duration_s,
        "mp4_path": v.mp4_path,
        "created_at": v.created_at,
        "updated_at": v.updated_at,
    }


def create_video(vid: str, user_id: str, version_id: str, name: str) -> None:
    with Session(engine) as s:
        s.add(
            Video(
                id=vid,
                user_id=uuid.UUID(user_id),
                brand_kit_version_id=uuid.UUID(version_id),
                name=name,
                status="generating",
            )
        )
        s.commit()


def _set_video(vid: str, **fields: Any) -> None:
    with Session(engine) as s:
        v = s.get(Video, vid)
        if v is None:
            return
        for k, val in fields.items():
            setattr(v, k, val)
        s.add(v)
        s.commit()  # updated_at bumped by onupdate=func.now()


def set_status(vid: str, status: str) -> None:
    _set_video(vid, status=status)


def set_total(vid: str, total: float) -> None:
    _set_video(vid, total_duration_s=total)


def set_mp4(vid: str, mp4_path: str) -> None:
    _set_video(vid, mp4_path=mp4_path)


def get_video(vid: str, user_id: str) -> dict[str, Any] | None:
    with Session(engine) as s:
        v = s.exec(
            select(Video).where(Video.id == vid, Video.user_id == uuid.UUID(user_id))
        ).one_or_none()
        if v is None:
            return None
        out = _video_to_dict(v)
        out["scenes"] = _scenes(s, vid)
        return out


def list_videos(user_id: str) -> list[dict[str, Any]]:
    with Session(engine) as s:
        rows = s.exec(
            select(Video)
            .where(Video.user_id == uuid.UUID(user_id))
            .order_by(col(Video.created_at).desc())
        ).all()
        return [_video_to_dict(v) for v in rows]


# --- Scenes ----------------------------------------------------------------


def _row_to_scene(r: Scene) -> dict[str, Any]:
    return {
        "order": r.ord,
        "type": r.type,
        "composition": r.composition,
        "props": r.props,
        "asset_refs": r.asset_refs,
        "timing": r.timing,
    }


def _scenes(s: Session, vid: str) -> list[dict[str, Any]]:
    rows = s.exec(select(Scene).where(Scene.video_id == vid).order_by(col(Scene.ord))).all()
    return [_row_to_scene(r) for r in rows]


def get_scenes(vid: str) -> list[dict[str, Any]]:
    with Session(engine) as s:
        return _scenes(s, vid)


def _upsert_scene(s: Session, vid: str, sc: dict[str, Any]) -> None:
    existing = s.exec(
        select(Scene).where(Scene.video_id == vid, Scene.ord == sc["order"])
    ).one_or_none()
    target = existing or Scene(video_id=vid, ord=sc["order"], type=sc["type"])
    target.type = sc["type"]
    target.composition = sc.get("composition")
    target.props = sc.get("props", {})
    target.asset_refs = sc.get("asset_refs", [])
    target.timing = sc.get("timing", {})
    s.add(target)


def replace_scenes(vid: str, scenes: list[dict[str, Any]]) -> None:
    """Write the whole scene set (initial generation), atomically."""
    with Session(engine) as s:
        for old in s.exec(select(Scene).where(Scene.video_id == vid)).all():
            s.delete(old)
        s.flush()
        for sc in scenes:
            _upsert_scene(s, vid, sc)
        s.commit()


def upsert_scene(vid: str, scene: dict[str, Any]) -> None:
    """Persist a single scene (scoped edit -> one row update)."""
    with Session(engine) as s:
        _upsert_scene(s, vid, scene)
        s.commit()


# --- Jobs ------------------------------------------------------------------


def create_job(video_id: str, job_type: str) -> str:
    """Create a queued job for a video and return its id. `job_type` = generation | render."""
    with Session(engine) as s:
        job = Job(video_id=video_id, type=job_type, status="queued")
        s.add(job)
        s.commit()
        s.refresh(job)
        assert job.id is not None  # PK populated by the DB on commit
        return str(job.id)


def set_job_status(job_id: str, status: str, error: str | None = None) -> None:
    """Transition a job (queued -> running -> done/error). `error` set on failure only."""
    with Session(engine) as s:
        job = s.get(Job, uuid.UUID(job_id))
        if job is None:
            return
        job.status = status
        if error is not None:
            job.error = error
        s.add(job)
        s.commit()  # updated_at bumped by onupdate=func.now()


def set_job_step(job_id: str, step: str) -> None:
    """Record the worker's current step (generation: plan/outline/fill/tts; render:
    packing/render — issue #9). Best-effort no-op if the job vanished, same as
    `set_job_status`: progress reporting never blocks the pipeline."""
    with Session(engine) as s:
        job = s.get(Job, uuid.UUID(job_id))
        if job is None:
            return
        job.step = step
        s.add(job)
        s.commit()


def _job_to_dict(j: Job) -> dict[str, Any]:
    return {
        "id": str(j.id),
        "video_id": j.video_id,
        "type": j.type,
        "status": j.status,
        "step": j.step,
        "error": j.error,
    }


def get_job(job_id: str, user_id: str) -> dict[str, Any] | None:
    """Fetch a job scoped to its video's owner (`jobs` has no `user_id` of its own —
    join through `videos`). Returns None for an unknown id, a malformed UUID, or a
    job owned by another user (no existence leak — same pattern as `get_video`)."""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        return None
    with Session(engine) as s:
        job = s.exec(
            select(Job).join(Video).where(Job.id == job_uuid, Video.user_id == uuid.UUID(user_id))
        ).one_or_none()
        return _job_to_dict(job) if job else None
