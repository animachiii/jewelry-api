"""Celery task: `cleanup.process`. Session/transaction lifecycle for phase 1
(the cleanup Gemini call, app/services/cleanup_service.py) AND the
phase-1-to-phase-2 handoff for GENERATE_WITH_CLEANUP jobs. See
docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md section 4.

Builds its own engine per call from settings.DATABASE_URL (read live) and
its own owned Redis client per call, closed in `finally` — the same
cross-loop reasoning as every other worker task since Phase 7.

**The phase-2 dispatch below is the reason this file exists rather than
just calling cleanup_service.process from somewhere else.** If the cleanup
sub-job reached COMPLETED, this creates the job's angle sub-jobs (reading
Job.requested_angle_codes, since the original request body is long gone by
now) and dispatches generation.transform_photo_task for each — mirroring
exactly how app/workers/generation.py dispatches qa.score_similarity right
after a QA_REVIEW-landing transform_photo commits: from the WORKER layer,
never the service, so the next phase never reads a row before its own
creating transaction has landed. This is a SEPARATE transaction from the
one cleanup_service.process already committed inside `_run`.

If cleanup did not reach COMPLETED (FAILED/REJECTED), nothing further
happens here — compute_parent_status already made the job FAILED inside
cleanup_service.process itself (it counts real rows; with only the one
FAILED cleanup sub-job existing, F == R == 1 falls out of the unmodified
rollup with no special-casing). No angle sub-jobs are ever created for a
job whose cleanup step failed.

Phase 16 Step 1: bounded by settings.WORKER_TASK_TIMEOUT_SECONDS via
asyncio.wait_for, same as every other worker task.
"""

import asyncio
import uuid

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.redis_client import new_redis_client
from app.db.models.enums import Angle, SourceType, SubJobStatus
from app.db.models.jobs import SubJob
from app.db.repositories import jobs as jobs_repo
from app.services.cleanup_service import process
from app.services.generation_service import mark_sub_job_timed_out
from app.services.job_service import validate_operation_angle_consistency
from app.workers._async_utils import run_async
from app.workers.celery_app import celery_app


async def _run(sub_job_id: str) -> tuple[str, str]:
    """Returns (sub_job_status, job_id) — the caller needs job_id to
    trigger phase 2 without a second DB round-trip inside this function's
    own session, which has already closed by the time the caller runs.
    """
    engine = create_async_engine(settings.DATABASE_URL)
    redis_client = new_redis_client()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sub_job = await process(session, redis_client, uuid.UUID(sub_job_id))
            await session.commit()
            return sub_job.status.value, str(sub_job.job_id)
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


async def _get_cleanup_sub_job(session: AsyncSession, job_id: uuid.UUID) -> SubJob | None:
    """The job's one angle-less sub-job -- ux_sub_jobs_job_single guarantees
    at most one exists (angle IS NULL AND variant_index IS NULL)."""
    result = await session.execute(
        select(SubJob).where(SubJob.job_id == job_id, SubJob.angle.is_(None))
    )
    return result.scalar_one_or_none()


async def _dispatch_angle_phase(job_id: str) -> None:
    """Phase 2: create and dispatch the job's angle sub-jobs. Runs in its
    own fresh engine/session, separate from `_run`'s — by the time this is
    called, `_run`'s session has already closed and committed.
    """
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            job = await jobs_repo.get_by_id(session, uuid.UUID(job_id))
            assert job is not None, f"Job {job_id} vanished between phase 1 and phase 2"
            assert job.requested_angle_codes, (
                f"Job {job_id} has no requested_angle_codes -- cannot start phase 2"
            )

            cleanup_sub_job = await _get_cleanup_sub_job(session, job.id)
            assert cleanup_sub_job is not None
            assert cleanup_sub_job.output_asset_id is not None

            angle_sub_job_ids: list[uuid.UUID] = []
            for code in job.requested_angle_codes:
                angle = Angle(code)
                validate_operation_angle_consistency(job.operation, angle)
                angle_sub_job = jobs_repo.create_sub_job(
                    session,
                    job_id=job.id,
                    angle=angle,
                    status=SubJobStatus.PENDING,
                    source_type=SourceType.UPLOADED,
                    input_asset_id=cleanup_sub_job.output_asset_id,
                )
                await session.flush()  # assigns angle_sub_job.id
                angle_sub_job_ids.append(angle_sub_job.id)

            await session.commit()
    finally:
        await engine.dispose()

    # Dispatched only after the creating transaction has committed -- same
    # dispatch-after-commit rule this module's own docstring cites.
    from app.workers.generation import transform_photo_task

    for angle_sub_job_id in angle_sub_job_ids:
        transform_photo_task.delay(str(angle_sub_job_id))


@celery_app.task(name="cleanup.process")  # type: ignore[untyped-decorator]
def process_task(sub_job_id: str) -> str:
    try:
        status, job_id = run_async(
            asyncio.wait_for(_run(sub_job_id), timeout=settings.WORKER_TASK_TIMEOUT_SECONDS)
        )
    except (TimeoutError, SoftTimeLimitExceeded):
        status, job_id = run_async(_run_timed_out(sub_job_id))

    if status == SubJobStatus.COMPLETED.value:
        run_async(_dispatch_angle_phase(job_id))

    return status
