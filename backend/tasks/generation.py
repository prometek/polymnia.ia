#!/usr/bin/env python3
"""Generation task: full project generation (plan -> outline -> fill -> TTS)."""

import logging
from typing import Any

from api import db, service
from api.celery_app import celery_app

logger = logging.getLogger("polymnia.tasks.generation")

Kit = dict[str, Any]


@celery_app.task(name="generation.generate")  # type: ignore[untyped-decorator]  # celery is untyped
def generate_task(job_id: str, pid: str, input_text: str, kit: Kit) -> None:
    """Run generation for project `pid`, driving the `jobs` row through its lifecycle.

    `service.run_generation` owns the *video* status transitions (generating -> ready/
    error). This wrapper owns the *job* status (queued -> running -> done/error) and
    re-raises on failure so the broker can retry/monitor (retries + DLQ = PRO-10).
    """
    db.set_job_status(job_id, "running")
    try:
        service.run_generation(pid, input_text, kit, job_id)
    except Exception as exc:
        db.set_job_status(job_id, "error", error=str(exc))
        logger.exception("generation job %s failed for project %s", job_id, pid)
        raise
    db.set_job_status(job_id, "done")
