"""Celery beat task: `config.sync`. Runs `app.services.config_sync_service.sync_config`
on the `CONFIG_SYNC_CRON` schedule registered in `app/workers/celery_app.py`. Workers
call services, never query directly — see docs/conventions.md.

Builds its own engine per call from settings.DATABASE_URL rather than the
shared app.db.session.async_session_factory bound at import time, and routes
through _async_utils.run_async rather than a bare asyncio.run() -- same
cross-loop reasoning as every other worker task since Phase 7
(app/workers/_async_utils.py, app/workers/generation.py, app/workers/qa.py).
This task originally used the shared factory + bare asyncio.run() directly;
live in production it crashed every single tick with
`RuntimeError("Task ... got Future ... attached to a different loop")` once
CONFIG_SYNC_CRON started firing hourly on real infrastructure -- the shared
engine's connections were already bound to a different loop than the fresh
one asyncio.run() creates on each call.
"""

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.redis_client import get_redis_client
from app.services.config_sync_service import sync_config
from app.workers._async_utils import run_async
from app.workers.celery_app import celery_app

logger = structlog.get_logger()


async def _run_sync() -> str:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            version = await sync_config(session, get_redis_client())
            return f"version={version.version_number} status={version.sync_status.value}"
    finally:
        await engine.dispose()


@celery_app.task(name="config.sync")  # type: ignore[untyped-decorator]
def sync() -> str:
    result = run_async(_run_sync())
    logger.info("config_sync_task_complete", result=result)
    return result
