"""Phase 16 Checkpoint 1 — task_time_limit/task_soft_time_limit are set and
visible in celery_app.conf at import time, so a future refactor can't
silently drop them. See app/workers/celery_app.py's comment on why these are
documented/future-proofing rather than the actual enforcement mechanism
under the live `--pool=solo` deployment (that's WORKER_TASK_TIMEOUT_SECONDS,
covered by tests/integration/test_task_timeout.py).
"""

from app.workers.celery_app import celery_app


def test_task_time_limit_configured() -> None:
    assert celery_app.conf.task_time_limit == 180


def test_task_soft_time_limit_configured() -> None:
    assert celery_app.conf.task_soft_time_limit == 150


def test_soft_limit_is_lower_than_hard_limit() -> None:
    assert celery_app.conf.task_soft_time_limit < celery_app.conf.task_time_limit
