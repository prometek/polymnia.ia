#!/usr/bin/env python3
"""
Celery application — the durable job queue (issue #6).

Long tasks (IA generation, Remotion render) used to run in-process via FastAPI
`BackgroundTasks`, so an API restart killed them. They now enqueue onto this queue
(Redis broker) and are picked up by a worker process, independent of the API.

Broker: `REDIS_URL` (default local Redis). Result backend is optional — applicative
status lives in the `jobs` table (see api/models.py), so it defaults to disabled;
set `CELERY_RESULT_BACKEND` to turn on Celery's own result store when needed.

Queues (issue #7): generation is routed to its own `generation` queue so the
generation worker is autonomous and scales independently of the API (and, later,
of the render worker). Render stays on the default `celery` queue until it gets
its own worker + queue in PRO-07.

Run the generation worker from backend/ (this issue):
    uv run celery -A api.celery_app worker -Q generation -n generation@%h --loglevel=info
The default worker (`... worker --loglevel=info`, queue `celery`) still drains
render until PRO-07 splits it out. In dev, one worker can drain both:
    uv run celery -A api.celery_app worker -Q generation,celery --loglevel=info
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
    # Dedicated queues (issue #8): render is the heaviest CPU/RAM workload (Remotion),
    # so it gets its own worker container that scales independently of generation.
    task_default_queue="generation",
    task_routes={
        "render.render": {"queue": "render"},
        "generation.generate": {"queue": "generation"},
    },
)
