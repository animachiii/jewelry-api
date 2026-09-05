"""Celery task: `cleanup.process`. Session/transaction lifecycle for phase 1
(the cleanup Gemini call, app/services/cleanup_service.py) AND the
phase-1-to-phase-2 Celery dispatch for GENERATE_WITH_CLEANUP jobs. See
docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md section 4.

Builds its own engine per call from settings.DATABASE_URL (read live) and
its own owned Redis client per call, closed in `finally` — the same
cross-loop reasoning as every other worker task since Phase 7.

**The angle sub-job *rows* are created inside `cleanup_service.process`
itself, in the same transaction as the cleanup sub-job's COMPLETED write —
not here.** An earlier version of this file created them in a later,
separate transaction, which left a real window where the job had only its
cleanup sub-job and `recompute_parent_status` stamped it terminal
`COMPLETED` with zero outputs before a single angle sub-job existed —
client-visible during that window, and permanent if the worker crashed
inside it (see `app/services/cleanup_service.py`'s own docstring for the
full incident). This file's only remaining job for phase 2 is dispatching
the Celery tasks for whatever angle sub-job IDs `process` already created —
mirroring exactly how `app/workers/generation.py` dispatches
`qa.score_similarity` from its own sync body, never from inside the async
coroutine (see `process_task` below for why).

If cleanup did not reach COMPLETED (FAILED/REJECTED), `process` returns an
empty ID list and nothing further happens here.

Phase 16 Step 1: bounded by settings.WORKER_TASK_TIMEOUT_SECONDS via
asyncio.wait_for, same as every other worker task.
"""

import asyncio
import uuid

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.redis_client import new_redis_client
from app.services.cleanup_service import process
from app.services.generation_service import mark_sub_job_timed_out
from app.workers._async_utils import run_async
from app.workers.celery_app import celery_app


async def _run(sub_job_id: str) -> tuple[str, list[str]]:
    """Returns (sub_job_status, angle_sub_job_ids) — the caller dispatches
    `transform_photo_task` for each ID from its own sync body, after this
    coroutine (and the session/transaction it ran on) has already closed.
    """
    engine = create_async_engine(settings.DATABASE_URL)
    redis_client = new_redis_client()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sub_job, angle_sub_job_ids = await process(session, redis_client, uuid.UUID(sub_job_id))
            await session.commit()
            return sub_job.status.value, [str(i) for i in angle_sub_job_ids]
    finally:
        await redis_client.aclose()
        await engine.dispose()


async def _run_timed_out(sub_job_id: str) -> tuple[str, str]:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sub_job = await mark_sub_job_timed_out(session, uuid.UUID(sub_job_id))
            await session.commit()
            return sub_job.status.value, str(sub_job.job_id)
    finally:
        await engine.dispose()


@celery_app.task(name="cleanup.process")  # type: ignore[untyped-decorator]
def process_task(sub_job_id: str) -> str:
    try:
        status, angle_sub_job_ids = run_async(
            asyncio.wait_for(_run(sub_job_id), timeout=settings.WORKER_TASK_TIMEOUT_SECONDS)
        )
    except (TimeoutError, SoftTimeLimitExceeded):
        status, _job_id = run_async(_run_timed_out(sub_job_id))
        angle_sub_job_ids = []

    # Dispatched only after `run_async` has returned control to this (the
    # original caller's) thread -- see `_run`'s docstring and
    # `cleanup_service.process`'s own docstring for why this can't happen
    # from inside the async coroutine that created these rows.
    if angle_sub_job_ids:
        from app.workers.generation import transform_photo_task

        for angle_sub_job_id in angle_sub_job_ids:
            transform_photo_task.delay(angle_sub_job_id)

    return status
