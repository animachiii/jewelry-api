"""All queries against `jobs` and `sub_jobs`. See docs/conventions.md."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import Angle, JobStatus, Operation, QAStatus, SourceType, SubJobStatus
from app.db.models.jobs import Job, SubJob


async def get_by_id_for_client(
    session: AsyncSession, job_id: uuid.UUID, client_id: uuid.UUID
) -> Job | None:
    """Scoped to the owning client — see docs/api-routes.md: another client's
    job_id must return 404, never 403.
    """
    result = await session.execute(select(Job).where(Job.id == job_id, Job.client_id == client_id))
    return result.scalar_one_or_none()


async def get_by_id(session: AsyncSession, job_id: uuid.UUID) -> Job | None:
    """Unscoped — for worker use, where there is no requesting client to
    scope against. Routes must use `get_by_id_for_client` instead.
    """
    result = await session.execute(select(Job).where(Job.id == job_id))
    return result.scalar_one_or_none()


async def get_sub_job_by_id(session: AsyncSession, sub_job_id: uuid.UUID) -> SubJob | None:
    result = await session.execute(select(SubJob).where(SubJob.id == sub_job_id))
    return result.scalar_one_or_none()


async def get_sub_jobs(session: AsyncSession, job_id: uuid.UUID) -> list[SubJob]:
    result = await session.execute(
        select(SubJob).where(SubJob.job_id == job_id).order_by(SubJob.angle)
    )
    return list(result.scalars().all())


async def get_by_idempotency_key(
    session: AsyncSession, client_id: uuid.UUID, idempotency_key: str
) -> Job | None:
    result = await session.execute(
        select(Job).where(Job.client_id == client_id, Job.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


async def get_sub_job(session: AsyncSession, job_id: uuid.UUID, angle: Angle) -> SubJob | None:
    result = await session.execute(
        select(SubJob).where(SubJob.job_id == job_id, SubJob.angle == angle)
    )
    return result.scalar_one_or_none()


async def count_created_today(session: AsyncSession, client_id: uuid.UUID) -> int:
    """Postgres-backed, not a new Redis key — see phases/phase-10-auth-security.md
    Step 1: daily_job_quota has no corresponding entry in docs/schema.md's
    Redis table, matching the "Postgres is the system of record" decision
    already made for everything else client-visible.
    """
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(
        select(func.count())
        .select_from(Job)
        .where(Job.client_id == client_id, Job.created_at >= today_start)
    )
    return int(result.scalar_one())


BACKGROUND_OPERATIONS = (Operation.BACKGROUND_REMOVAL, Operation.BACKGROUND_REPLACEMENT)


async def get_flagged_background_operations(
    session: AsyncSession, *, unscored_only: bool
) -> list[tuple[SubJob, Job]]:
    """Flagged background-operation sub-jobs, for the ops re-score route.

    Narrower than get_flagged_qa_review in two ways it cannot express: it
    joins the parent job (the caller needs `job.id` to record an event
    against) and filters to the two background operations, since the
    subject-preservation judge is the only one being re-run.

    `unscored_only` restricts to `qa_score IS NULL` — the population left
    by the 2026-08-21 `_parse_response` bug, which never got a real judge
    verdict at all. Without it, sub-jobs that *were* scored by the old
    shared judge prompt are included too; see
    app/services/qa_service.py::rescore_flagged_background_operations.
    """
    stmt = (
        select(SubJob, Job)
        .join(Job, Job.id == SubJob.job_id)
        .where(
            SubJob.status == SubJobStatus.QA_REVIEW,
            SubJob.qa_status == QAStatus.FLAGGED,
            SubJob.angle.is_(None),
            Job.operation.in_(BACKGROUND_OPERATIONS),
        )
        .order_by(Job.created_at)
    )
    if unscored_only:
        stmt = stmt.where(SubJob.qa_score.is_(None))
    return [(sub_job, job) for sub_job, job in (await session.execute(stmt)).all()]


async def get_flagged_qa_review(session: AsyncSession) -> list[SubJob]:
    """Ops-wide, unscoped by client — see docs/api-routes.md GET
    /qa/review-queue (ops-only scope). Narrower than "all QA_REVIEW" —
    see phases/phase-9-qa-gate.md Step 3: a sub-job mid-scoring (not yet
    FLAGGED or PASSED) shouldn't surface in a human queue.
    """
    result = await session.execute(
        select(SubJob)
        .where(SubJob.status == SubJobStatus.QA_REVIEW, SubJob.qa_status == QAStatus.FLAGGED)
        .order_by(SubJob.started_at)
    )
    return list(result.scalars().all())


async def list_jobs(
    session: AsyncSession,
    *,
    status: JobStatus | None,
    category_code: str | None,
    created_after: datetime | None,
    created_before: datetime | None,
    page: int,
    page_size: int,
) -> tuple[list[Job], int]:
    """Unscoped by client — ops sees every client's jobs, unlike every other
    query in this repository. See phases/phase-11-observability-cost-tracking.md
    Step 1. Two separate queries (page + count) rather than a window
    function: this table has no ops-wide index, so either shape scans; two
    simple queries are easier to test independently.
    """
    filters = []
    if status is not None:
        filters.append(Job.status == status)
    if category_code is not None:
        filters.append(Job.category_code == category_code)
    if created_after is not None:
        filters.append(Job.created_at >= created_after)
    if created_before is not None:
        filters.append(Job.created_at <= created_before)

    page_result = await session.execute(
        select(Job)
        .where(*filters)
        .order_by(Job.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    count_result = await session.execute(select(func.count()).select_from(Job).where(*filters))
    return list(page_result.scalars().all()), int(count_result.scalar_one())


def create_job(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    idempotency_key: str,
    payload_hash: str,
    config_version_id: uuid.UUID,
    requested_angles: int,
    sku_reference: str | None,
    metadata: dict[str, Any],
    category_code: str | None = None,
    preset_code: str | None = None,
    operation: Operation = Operation.ANGLE_GENERATION,
) -> Job:
    """Adds and returns a new Job row (status defaults to PENDING — nothing
    executes here, see phases/phase-2-data-model.md). Does not commit; the
    caller controls the transaction boundary.

    `operation` defaults to ANGLE_GENERATION so every existing `/generate`
    call site is unaffected — see migration 0006 and
    phases/phase-15-background-operations.md Step 2.
    """
    job = Job(
        client_id=client_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        category_code=category_code,
        preset_code=preset_code,
        config_version_id=config_version_id,
        status=JobStatus.PENDING,
        operation=operation,
        requested_angles=requested_angles,
        sku_reference=sku_reference,
        job_metadata=metadata,
    )
    session.add(job)
    return job


def create_sub_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    status: SubJobStatus,
    source_type: SourceType,
    angle: Angle | None = None,
    input_asset_id: uuid.UUID | None = None,
    background_asset_id: uuid.UUID | None = None,
    variant_index: int | None = None,
    mask_asset_id: uuid.UUID | None = None,
    palette_code: str | None = None,
    secondary_input_asset_id: uuid.UUID | None = None,
    secondary_mask_asset_id: uuid.UUID | None = None,
) -> SubJob:
    """`angle` is None for a background-operation, MATCH, RECOLOR, or MIX
    sub-job. `background_asset_id` is set only for a BACKGROUND_REPLACEMENT
    sub-job that used an uploaded background photo instead of a preset.
    `variant_index` is set only for a MATCH sub-job (0-based position within
    the job's requested companion-piece variants — see migration 0013 and
    phases/phase-18-match.md Step 1). `mask_asset_id`/`palette_code` are set
    only for a RECOLOR sub-job — see migration 0015 and
    phases/phase-19-recolor.md Step 1. `secondary_input_asset_id`/
    `secondary_mask_asset_id` are set only for a MIX sub-job — the second
    source photo and its mask — see migration 0017 and
    phases/phase-20-mix.md Step 1. Callers must have already validated
    operation/angle consistency — see
    app/services/job_service.py::validate_operation_angle_consistency.
    """
    sub_job = SubJob(
        job_id=job_id,
        angle=angle,
        status=status,
        source_type=source_type,
        input_asset_id=input_asset_id,
        background_asset_id=background_asset_id,
        variant_index=variant_index,
        mask_asset_id=mask_asset_id,
        palette_code=palette_code,
        secondary_input_asset_id=secondary_input_asset_id,
        secondary_mask_asset_id=secondary_mask_asset_id,
    )
    session.add(sub_job)
    return sub_job
