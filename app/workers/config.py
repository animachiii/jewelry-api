"""Celery beat task: `config.sync`. Runs `app.services.config_sync_service.sync_config`
on the `CONFIG_SYNC_CRON` schedule registered in `app/workers/celery_app.py`. Workers
call services, never query directly — see docs/conventions.md.
"""

import asyncio

import structlog

from app.core.redis_client import get_redis_client
from app.db.session import async_session_factory
from app.services.config_sync_service import sync_config
from app.workers.celery_app import celery_app

logger = structlog.get_logger()


async def _run_sync() -> str:
    async with async_session_factory() as session:
        version = await sync_config(session, get_redis_client())
        return f"version={version.version_number} status={version.sync_status.value}"


@celery_app.task(name="config.sync")  # type: ignore[untyped-decorator]
def sync() -> str:
    result = asyncio.run(_run_sync())
    logger.info("config_sync_task_complete", result=result)
    return result
