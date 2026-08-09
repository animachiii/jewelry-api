"""Celery config, queue routing, beat schedule.

Single "io" queue — the gpu/io split existed only to isolate VRAM-bound
local matting from network-bound Gemini calls. Background removal now
happens in the same Gemini call as everything else (see
docs/decisions/0001-drop-local-matting.md), so there is no VRAM-bound work
left to isolate.
"""

import ssl
from urllib.parse import urlparse

from celery import Celery
from celery.schedules import crontab

from app.config import settings


def _redis_ssl_options(url: str) -> dict[str, ssl.VerifyMode] | None:
    """TLS options for a `rediss://` broker/backend, or None for plain `redis://`.

    Celery hard-fails a `rediss://` URL that doesn't state `ssl_cert_reqs`
    (`ValueError: E_REDIS_SSL_CERT_REQS_MISSING_INVALID`) rather than picking
    a default, so this has to be set explicitly. Upstash — the free-tier Redis
    behind both deploy paths — is TLS-only, so every real deployment hits this;
    local docker-compose Redis is plain `redis://` and must not get SSL options,
    which is why this is conditional on the scheme rather than always-on. That
    split is exactly why Phase 12's "verified locally" Render run didn't catch
    it: it ran against local Redis, never Upstash.

    `CERT_REQUIRED` (validate the chain), not `CERT_NONE` — Upstash presents a
    valid certificate, and disabling verification to silence this error would
    trade a startup crash for a silent MITM window on every broker message.
    """
    if urlparse(url).scheme != "rediss":
        return None
    return {"ssl_cert_reqs": ssl.CERT_REQUIRED}


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
        "app.workers.retention",
    ],
)

celery_app.conf.task_routes = {
    "generation.*": {"queue": "io"},
    "orchestration.*": {"queue": "io"},
    "qa.*": {"queue": "io"},
    "config.sync": {"queue": "io"},
    "health.ping_io": {"queue": "io"},
    "retention.*": {"queue": "io"},
}

celery_app.conf.beat_schedule = {
    "config-sync": {
        "task": "config.sync",
        "schedule": _crontab_from_string(settings.CONFIG_SYNC_CRON),
    },
    "asset-retention": {
        "task": "retention.expire_assets",
        "schedule": _crontab_from_string(settings.RETENTION_SWEEP_CRON),
    },
}

celery_app.conf.task_serializer = "json"
celery_app.conf.result_serializer = "json"
celery_app.conf.accept_content = ["json"]
celery_app.conf.timezone = "UTC"

# Broker and result backend are separately configured even though both point at
# the same Upstash instance today (one database is all the free tier allows —
# docs/deployment-free-tier.md); they're independent Celery settings and a
# future split must not silently lose TLS on one of them.
_broker_ssl = _redis_ssl_options(settings.CELERY_BROKER_URL)
if _broker_ssl is not None:
    celery_app.conf.broker_use_ssl = _broker_ssl

_backend_ssl = _redis_ssl_options(settings.CELERY_RESULT_BACKEND)
if _backend_ssl is not None:
    celery_app.conf.redis_backend_use_ssl = _backend_ssl
