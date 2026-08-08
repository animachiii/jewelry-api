"""Phase 6 Checkpoint 4 — generation.transform_photo is registered with Celery
and routed to the io queue. The task body itself (session lifecycle only) is
exercised via app.services.generation_service directly in
tests/integration/test_generation_worker.py — same split as
tests/unit/test_config_beat_schedule.py for config.sync.
"""

from app.workers import generation as generation_worker
from app.workers.celery_app import celery_app


def test_transform_photo_registered() -> None:
    assert "generation.transform_photo" in celery_app.tasks


def test_transform_photo_routed_to_io_queue() -> None:
    routes = celery_app.conf.task_routes
    assert routes["generation.*"]["queue"] == "io"


def test_transform_photo_task_callable_is_the_registered_task() -> None:
    assert generation_worker.transform_photo_task.name == "generation.transform_photo"
