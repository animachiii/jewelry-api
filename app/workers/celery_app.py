"""Celery config, queue routing, beat schedule.

Single "io" queue — the gpu/io split existed only to isolate VRAM-bound
local matting from network-bound Gemini calls. Background removal now
happens in the same Gemini call as everything else (see
docs/decisions/0001-drop-local-matting.md), so there is no VRAM-bound work
left to isolate.
"""

from celery import Celery
from celery.schedules import crontab

from app.config import settings


def _crontab_from_string(cron: str) -> crontab:
    minute, hour, day_of_month, month_of_year, day_of_week = cron.split()
    return crontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
    )


celery_app = Celery(
    "jewelry_api",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.generation",
        "app.workers.qa",
        "app.workers.orchestration",
        "app.workers.health",
        "app.workers.config",
    ],
)

celery_app.conf.task_routes = {
    "generation.*": {"queue": "io"},
    "qa.*": {"queue": "io"},
    "config.sync": {"queue": "io"},
    "health.ping_io": {"queue": "io"},
}

celery_app.conf.beat_schedule = {
    "config-sync": {
        "task": "config.sync",
        "schedule": _crontab_from_string(settings.CONFIG_SYNC_CRON),
    },
}

celery_app.conf.task_serializer = "json"
celery_app.conf.result_serializer = "json"
celery_app.conf.accept_content = ["json"]
celery_app.conf.timezone = "UTC"
