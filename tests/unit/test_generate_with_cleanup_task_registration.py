"""Mirrors tests/unit/test_background_task_registration.py — cleanup.process
must be registered with Celery and routed to the io queue, or a real
worker process started separately from the test suite will never
recognize the task (this exact class of gap bit MATCH/RECOLOR once
already — see docs/schema.md's note on migration 0016's self-audit).
"""

from app.workers import cleanup as cleanup_worker
from app.workers.celery_app import celery_app


def test_cleanup_process_registered() -> None:
    assert "cleanup.process" in celery_app.tasks


def test_cleanup_process_routed_to_io_queue() -> None:
    routes = celery_app.conf.task_routes
    assert routes["cleanup.*"]["queue"] == "io"


def test_cleanup_process_task_callable_is_the_registered_task() -> None:
    assert cleanup_worker.process_task.name == "cleanup.process"
