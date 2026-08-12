"""Phase 15 Step 5 Checkpoint 5 — background.process (and qa.score_background)
are registered with Celery and routed to the io queue. Mirrors
tests/unit/test_generation_task_registration.py; the task bodies themselves
(session lifecycle only) are exercised via app.services.background_service /
app.services.qa_service directly in
tests/integration/test_background_operations.py.
"""

from app.workers import background as background_worker
from app.workers import qa as qa_worker
from app.workers.celery_app import celery_app


def test_background_process_registered() -> None:
    assert "background.process" in celery_app.tasks


def test_background_process_routed_to_io_queue() -> None:
    routes = celery_app.conf.task_routes
    assert routes["background.*"]["queue"] == "io"


def test_background_process_task_callable_is_the_registered_task() -> None:
    assert background_worker.process_task.name == "background.process"


def test_score_background_registered() -> None:
    assert "qa.score_background" in celery_app.tasks


def test_score_background_routed_to_io_queue() -> None:
    routes = celery_app.conf.task_routes
    assert routes["qa.*"]["queue"] == "io"


def test_score_background_task_callable_is_the_registered_task() -> None:
    assert qa_worker.score_background.name == "qa.score_background"
