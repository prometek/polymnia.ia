"""Task-queue routing (issue #8).

The render worker container consumes only the `render` queue, so it can scale
independently from generation (heaviest CPU/RAM workload = Remotion). This is a
pure-config assertion — no broker/Redis needed.
"""

from api.celery_app import celery_app


def test_render_task_routed_to_render_queue() -> None:
    routes = celery_app.conf.task_routes
    assert routes["render.render"] == {"queue": "render"}


def test_generation_task_not_routed_to_render_queue() -> None:
    routes = celery_app.conf.task_routes
    assert routes["generation.generate"] == {"queue": "generation"}
    assert routes["generation.generate"]["queue"] != "render"
