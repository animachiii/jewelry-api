"""Phase 16 Step 2 — Celery beat schedule wiring for the reconciliation
sweep task. Mirrors tests/unit/test_config_beat_schedule.py.
"""

from app.workers import reconciliation as reconciliation_worker
from app.workers.celery_app import celery_app


def test_reconciliation_sweep_task_is_registered() -> None:
    assert "reconciliation.sweep_stuck_sub_jobs" in celery_app.tasks


def test_reconciliation_routed_to_io_queue() -> None:
    assert celery_app.conf.task_routes["reconciliation.*"] == {"queue": "io"}


def test_beat_schedule_has_reconciliation_sweep_entry() -> None:
    entry = celery_app.conf.beat_schedule["reconciliation-sweep"]
    assert entry["task"] == "reconciliation.sweep_stuck_sub_jobs"
    assert entry["schedule"] is not None


def test_sweep_task_callable_is_the_registered_task() -> None:
    assert reconciliation_worker.sweep_stuck_sub_jobs.name == "reconciliation.sweep_stuck_sub_jobs"
