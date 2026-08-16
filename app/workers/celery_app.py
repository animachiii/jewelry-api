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
        "app.workers.background",
        "app.workers.match",
        "app.workers.recolor",
        "app.workers.mix",
        "app.workers.qa",
        "app.workers.orchestration",
        "app.workers.health",
        "app.workers.config",
        "app.workers.retention",
        "app.workers.reconciliation",
    ],
)

celery_app.conf.task_routes = {
    "generation.*": {"queue": "io"},
    "background.*": {"queue": "io"},
    "match.*": {"queue": "io"},
    "recolor.*": {"queue": "io"},
    "mix.*": {"queue": "io"},
    "orchestration.*": {"queue": "io"},
    "qa.*": {"queue": "io"},
    "config.sync": {"queue": "io"},
    "health.ping_io": {"queue": "io"},
    "retention.*": {"queue": "io"},
    "reconciliation.*": {"queue": "io"},
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
    "reconciliation-sweep": {
        "task": "reconciliation.sweep_stuck_sub_jobs",
        "schedule": _crontab_from_string(settings.RECONCILIATION_SWEEP_CRON),
    },
}

celery_app.conf.task_serializer = "json"
celery_app.conf.result_serializer = "json"
celery_app.conf.accept_content = ["json"]
celery_app.conf.timezone = "UTC"

# Phase 16 Step 1: bounds a hung task (worker OOM-killed mid-task, an
# unhandled path that doesn't reach generation_service's own classification,
# or a future bug of the same shape as the two REDIS_SOCKET_TIMEOUT_SECONDS/
# GEMINI_REQUEST_TIMEOUT_SECONDS incidents already fixed). 150/180 gives a
# legitimate slow call (120s Gemini timeout + a few seconds of Redis/Storage
# margin) room to finish while still bounding a genuine hang to minutes, not
# forever.
#
# **Inert under the live deployment as of this phase.** scripts/render_start.sh
# runs `--pool=solo` (2026-08-13, the OOM fix) specifically because a forked
# prefork child no longer fits the free tier's 512MB — but Celery's soft/hard
# time limits are enforced by the pool timing *child processes* and signaling
# them; solo has no child and no timer (confirmed against the installed
# celery==5.6.3: `celery.concurrency.solo.TaskPool` reports `timeouts: ()`,
# and `concurrency.base.apply_target` — solo's `on_apply` — accepts and
# silently discards a `timeout`/`soft_timeout` kwarg without ever enforcing
# it). Setting these here is still correct — they document the intended
# ceiling and take effect for free if the pool is ever switched back to
# prefork — but they are not what actually bounds a hang today. The real
# enforcement is `settings.WORKER_TASK_TIMEOUT_SECONDS`, applied via
# `asyncio.wait_for` inside app/workers/generation.py and
# app/workers/background.py, which works inside a single process regardless
# of Celery's pool.
celery_app.conf.task_time_limit = 180
celery_app.conf.task_soft_time_limit = 150

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
