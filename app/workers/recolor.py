"""Celery task: `recolor.process`. Session/transaction lifecycle only — the
actual logic lives in app/services/recolor_service.py, same split as
app/workers/background.py / app/workers/match.py.

Builds its own engine per call from settings.DATABASE_URL (read live) and
its own owned Redis client per call, closed in `finally` — same cross-loop
reasoning as every other worker task since Phase 7
(app/workers/_async_utils.py).

Phase 16 Step 1: bounded by `settings.WORKER_TASK_TIMEOUT_SECONDS` via
`asyncio.wait_for`, same as background.py/match.py/generation.py.

Unlike background.py: no QA dispatch after a successful run. RECOLOR ships
straight to COMPLETED on a successful generate-then-composite (see
app/services/recolor_service.py's module docstring) — there's nothing
further to dispatch.
"""

import asyncio
import uuid

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.redis_client import new_redis_client
from app.services.generation_service import mark_sub_job_timed_out
from app.services.recolor_service import process
from app.workers._async_utils import run_async
from app.workers.celery_app import celery_app


async def _run(sub_job_id: str) -> str:
    engine = create_async_engine(settings.DATABASE_URL)
    redis_client = new_redis_client()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sub_job = await process(session, redis_client, uuid.UUID(sub_job_id))
            await session.commit()
            return sub_job.status.value
    finally:
        await redis_client.aclose()
        await engine.dispose()


async def _run_timed_out(sub_job_id: str) -> str:
    """See background.py's `_run_timed_out` — same fresh-session reasoning.
    Reuses generation_service.mark_sub_job_timed_out unmodified: it's
    already operation-agnostic, the same way status_rollup.compute_parent_status
    is.
    """
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sub_job = await mark_sub_job_timed_out(session, uuid.UUID(sub_job_id))
            await session.commit()
            return sub_job.status.value
    finally:
        await engine.dispose()


@celery_app.task(name="recolor.process")  # type: ignore[untyped-decorator]
def process_task(sub_job_id: str) -> str:
    try:
        status = run_async(
            asyncio.wait_for(_run(sub_job_id), timeout=settings.WORKER_TASK_TIMEOUT_SECONDS)
        )
    except (TimeoutError, SoftTimeLimitExceeded):
        return run_async(_run_timed_out(sub_job_id))
    return status
