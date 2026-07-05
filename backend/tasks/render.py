#!/usr/bin/env python3
"""Render task: pack a project's scenes and render the MP4 via Remotion."""

import logging
from typing import Any

from api import db, service
from api.celery_app import celery_app

logger = logging.getLogger("polymnia.tasks.render")

Scene = dict[str, Any]
Kit = dict[str, Any]


@celery_app.task(name="render.render")  # type: ignore[untyped-decorator]  # celery is untyped
def render_task(job_id: str, pid: str, scenes: list[Scene], kit: Kit) -> None:
    """Render project `pid`, driving the `jobs` row through its lifecycle.

    `service.run_render` owns the *video* status transitions (rendering -> ready/error).
    This wrapper owns the *job* status and re-raises on failure (retries/DLQ = PRO-10).
    """
    db.set_job_status(job_id, "running")
    try:
        service.run_render(pid, scenes, kit, job_id)
    except Exception as exc:
        db.set_job_status(job_id, "error", error=str(exc))
        logger.exception("render job %s failed for project %s", job_id, pid)
        raise
    db.set_job_status(job_id, "done")
