#!/usr/bin/env python3
"""Generation task: full project generation (plan -> outline -> fill -> TTS)."""

import logging
from typing import Any

from api import db, service
from api.celery_app import celery_app

from tasks.base import DeadLetterTask

logger = logging.getLogger("polymnia.tasks.generation")

Kit = dict[str, Any]

# Bounded retries (issue #11 / PRO-10): LLM/TTS calls (Mistral) fail transiently on
# rate limits/network blips (§14 architecture, "Indispo LLM/TTS -> retry + file") and
# are cheap to redo, so generation gets more attempts than render.
_MAX_RETRIES = 3
_RETRY_BACKOFF_MAX_S = 300  # cap exponential backoff at 5 min between attempts


@celery_app.task(
    name="generation.generate",
    base=DeadLetterTask,
    autoretry_for=(Exception,),
    max_retries=_MAX_RETRIES,
    retry_backoff=True,
    retry_backoff_max=_RETRY_BACKOFF_MAX_S,
    retry_jitter=True,
)  # type: ignore[untyped-decorator]  # celery is untyped
def generate_task(job_id: str, pid: str, input_text: str, kit: Kit) -> None:
    """Run generation for project `pid`, driving the `jobs` row through its lifecycle.

    `service.run_generation` owns the *video* status transitions (generating -> ready/
    error). This wrapper owns the *job* status (queued -> running -> done/error/dead)
    and re-raises on failure so Celery's bounded retry (`autoretry_for` above) can
    retry with backoff; once `_MAX_RETRIES` is exhausted, `DeadLetterTask.on_failure`
    moves the job to `dead` (issue #11) — no infinite retry loop.
    """
    db.set_job_status(job_id, "running")
    try:
        service.run_generation(pid, input_text, kit, job_id)
    except Exception as exc:
        db.set_job_status(job_id, "error", error=str(exc))
        logger.exception("generation job %s failed for project %s", job_id, pid)
        raise
    db.set_job_status(job_id, "done")
