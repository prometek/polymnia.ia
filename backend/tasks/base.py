#!/usr/bin/env python3
"""Shared dead-letter queue (DLQ) behaviour for Celery tasks (issue #11 / PRO-10).

Bounded retries are configured per task type (see tasks/generation.py, tasks/
render.py: `autoretry_for` + `max_retries` + exponential backoff). Celery's own
retry machinery re-raises the task's exception once retries are exhausted, and
the worker then calls `Task.on_failure` exactly once for that *permanent*
failure — an in-flight retry never reaches `on_failure` (it raises `Retry`,
handled by `on_retry` instead), so this is the single correct choke point for
"retries exhausted" without duplicating that logic in every task module.

The DLQ is the `dead` job status, not a separate broker queue: a `dead` job stays
inspectable via the existing `GET /jobs/{id}` (status + error), no new endpoint
needed to satisfy that acceptance criterion.
"""

import logging
from typing import Any

from api import db
from celery import Task

logger = logging.getLogger("polymnia.tasks.dlq")


class DeadLetterTask(Task):  # type: ignore[misc]  # celery.Task has no stubs (ignore_missing_imports)
    """Task base: on permanent failure (bounded retries exhausted), move the job
    to `dead` instead of leaving it stuck on the transient `error` its last
    attempt wrote, and emit a structured log line so the DLQ transition itself
    is observable (not just the resulting row).
    """

    def on_failure(
        self,
        exc: BaseException,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: Any,
    ) -> None:
        # Convention shared by every task using this base: job_id is the first
        # positional arg (tasks/generation.py, tasks/render.py).
        job_id = args[0]
        db.set_job_status(job_id, "dead", error=str(exc))
        logger.error(
            "job %s exhausted retries -> dead-letter queue (task=%s, task_id=%s)",
            job_id,
            self.name,
            task_id,
            exc_info=exc,
        )
