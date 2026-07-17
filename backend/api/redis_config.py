#!/usr/bin/env python3
"""Single source of truth for the Redis connection URL.

The Celery broker (api/celery_app.py), the job-events pub/sub relay
(api/job_events.py, issue #10), and the per-user rate limiter's sliding-window
counter (api/rate_limit.py, issue #17) all point at the same Redis — this
module centralizes the env var read + default so the three can't drift out of
sync with each other.
"""

import os

from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
