#!/usr/bin/env python3
"""
Celery application — the durable job queue (issue #6).

Long tasks (IA generation, Remotion render) used to run in-process via FastAPI
`BackgroundTasks`, so an API restart killed them. They now enqueue onto this queue
(Redis broker) and are picked up by a worker process, independent of the API.

Broker: `REDIS_URL` (default local Redis). Result backend is optional — applicative
status lives in the `jobs` table (see api/models.py), so it defaults to disabled;
set `CELERY_RESULT_BACKEND` to turn on Celery's own result store when needed.

Run a worker from backend/:  uv run celery -A api.celery_app worker --loglevel=info
"""

import os

from celery import Celery
from dotenv import load_dotenv

load_dotenv()

BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND") or None

celery_app = Celery(
    "polymnia",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    # Task modules are per-domain (project standard). Imported lazily by name so this
    # module has no import cycle with api.service.
    include=["tasks.generation", "tasks.render"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
)
