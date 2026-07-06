"""Celery task modules, one per domain (project standard: tasks/{domain}.py).

Thin wrappers over api.service: they enqueue/run the pipeline and drive the `jobs`
row lifecycle. The heavy worker logic (chunked steps, progress, PRO-06/07) and
bounded retries + dead-letter queue (PRO-10, issue #11) build on top of these;
the shared DLQ behaviour lives in tasks/base.py (`DeadLetterTask`).
"""
