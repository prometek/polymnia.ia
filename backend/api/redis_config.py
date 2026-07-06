#!/usr/bin/env python3
"""Single source of truth for the Redis connection URL.

Both the Celery broker (api/celery_app.py) and the job-events pub/sub relay
(api/job_events.py, issue #10) point at the same Redis — this module centralizes
the env var read + default so the two can't drift out of sync with each other.
"""

import os

from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
