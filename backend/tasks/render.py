#!/usr/bin/env python3
"""Render task: pack a project's scenes and render the MP4 via Remotion."""

import logging
from typing import Any

from api import db, service
from api.celery_app import celery_app

from tasks.base import DeadLetterTask

logger = logging.getLogger("polymnia.tasks.render")

Scene = dict[str, Any]
Kit = dict[str, Any]

# Bounded retries (issue #11 / PRO-10): render is the heaviest CPU/RAM workload
# (§13 architecture, Chrome headless + ffmpeg per job) and the costliest to redo, so
# it gets fewer attempts than generation, with a longer backoff cap to avoid
# hammering an already-saturated render worker.
_MAX_RETRIES = 2
_RETRY_BACKOFF_MAX_S = 600  # cap exponential backoff at 10 min between attempts


@celery_app.task(
    name="render.render",
    base=DeadLetterTask,
    autoretry_for=(Exception,),
    max_retries=_MAX_RETRIES,
    retry_backoff=True,
    retry_backoff_max=_RETRY_BACKOFF_MAX_S,
    retry_jitter=True,
)  # type: ignore[untyped-decorator]  # celery is untyped
def render_task(job_id: str, pid: str, scenes: list[Scene], kit: Kit) -> None:
    """Render project `pid`, driving the `jobs` row through its lifecycle.

    `service.run_render` owns the *video* status transitions (rendering -> ready/error).
    This wrapper owns the *job* status (queued -> running -> done/error/dead) and
    re-raises on failure so Celery's bounded retry (`autoretry_for` above) can retry
    with backoff; once `_MAX_RETRIES` is exhausted, `DeadLetterTask.on_failure` moves
    the job to `dead` (issue #11) — no infinite retry loop.
    """
    db.set_job_status(job_id, "running")
    try:
        service.run_render(pid, scenes, kit, job_id)
    except Exception as exc:
        db.set_job_status(job_id, "error", error=str(exc))
        logger.exception("render job %s failed for project %s", job_id, pid)
        raise
    db.set_job_status(job_id, "done")
