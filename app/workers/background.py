"""Celery task: `background.process`. Session/transaction lifecycle only —
the actual logic lives in app/services/background_service.py, same split as
app/workers/generation.py / app/services/generation_service.py.

Builds its own engine per call from settings.DATABASE_URL (read live) and
its own owned Redis client per call, closed in `finally` — the same
cross-loop reasoning as every other worker task since Phase 7
(app/workers/_async_utils.py), pinned by
tests/integration/test_background_worker.py the same way
tests/integration/test_worker_event_loop_lifecycle.py pins generation.py's.

Dispatches `qa.score_background_operation` right after a QA_REVIEW-landing
`process` call commits — same dispatch-after-commit placement
generation.py uses for `qa.score_similarity`, so a QA dispatch never reads
a sub-job row before its own creating transaction has landed.

Phase 16 Step 1: bounded by `settings.WORKER_TASK_TIMEOUT_SECONDS` via
`asyncio.wait_for`, same as generation.py — see that file's docstring and
app/workers/celery_app.py's comment on why Celery's own time limits are
inert under this deployment's `--pool=solo`.
"""

import asyncio
import uuid

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.redis_client import new_redis_client
from app.db.models.enums import SubJobStatus
from app.services.background_service import process
from app.services.generation_service import mark_sub_job_timed_out
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
    """See generation.py's `_run_timed_out` — same fresh-session reasoning.
    Reuses generation_service.mark_sub_job_timed_out unmodified: it's
    already operation-agnostic (keyed on sub-job status, not angle vs
    background), the same way status_rollup.compute_parent_status is.
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


@celery_app.task(name="background.process")  # type: ignore[untyped-decorator]
def process_task(sub_job_id: str) -> str:
    try:
        status = run_async(
            asyncio.wait_for(_run(sub_job_id), timeout=settings.WORKER_TASK_TIMEOUT_SECONDS)
        )
    except (TimeoutError, SoftTimeLimitExceeded):
        return run_async(_run_timed_out(sub_job_id))
    if status == SubJobStatus.QA_REVIEW.value:
        from app.workers.qa import score_background

        score_background.delay(sub_job_id)
    return status
