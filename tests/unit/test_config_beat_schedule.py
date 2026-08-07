"""Phase 3 — Celery beat schedule wiring for the config sync task.
See docs/api-routes.md: "Also runs on a Celery beat schedule."
"""

from app.workers import config as config_worker
from app.workers.celery_app import celery_app


def test_config_sync_task_is_registered() -> None:
    assert "config.sync" in celery_app.tasks


def test_config_sync_routed_to_io_queue() -> None:
    assert celery_app.conf.task_routes["config.sync"] == {"queue": "io"}


def test_beat_schedule_has_config_sync_entry_on_configured_cron() -> None:
    entry = celery_app.conf.beat_schedule["config-sync"]
    assert entry["task"] == "config.sync"
    assert entry["schedule"] is not None


def test_sync_task_callable_is_the_registered_task() -> None:
    assert config_worker.sync.name == "config.sync"
