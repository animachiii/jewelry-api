"""All queries against `jobs` and `sub_jobs`. See docs/conventions.md."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import Angle, JobStatus, SourceType, SubJobStatus
from app.db.models.jobs import Job, SubJob


async def get_by_id_for_client(
    session: AsyncSession, job_id: uuid.UUID, client_id: uuid.UUID
) -> Job | None:
    """Scoped to the owning client — see docs/api-routes.md: another client's
    job_id must return 404, never 403.
    """
    result = await session.execute(select(Job).where(Job.id == job_id, Job.client_id == client_id))
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


async def get_any_for_client(session: AsyncSession, client_id: uuid.UUID) -> Job | None:
    """MOCK_MODE-only: stands in for real job creation on /retry, which is
    still mock in Phase 2 — see phases/phase-1-api-contract.md Step 3. Real
    /generate no longer uses this (see create_job below).
    """
    result = await session.execute(
        select(Job).where(Job.client_id == client_id).order_by(Job.created_at).limit(1)
    )
    return result.scalar_one_or_none()


def create_job(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    idempotency_key: str,
    payload_hash: str,
    category_code: str,
    config_version_id: uuid.UUID,
    requested_angles: int,
    sku_reference: str | None,
    metadata: dict[str, Any],
) -> Job:
    """Adds and returns a new Job row (status defaults to PENDING — nothing
    executes here, see phases/phase-2-data-model.md). Does not commit; the
    caller controls the transaction boundary.
    """
    job = Job(
        client_id=client_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        category_code=category_code,
        config_version_id=config_version_id,
        status=JobStatus.PENDING,
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
    angle: Angle,
    status: SubJobStatus,
    source_type: SourceType,
    input_asset_id: uuid.UUID | None = None,
) -> SubJob:
    sub_job = SubJob(
        job_id=job_id,
        angle=angle,
        status=status,
        source_type=source_type,
        input_asset_id=input_asset_id,
    )
    session.add(sub_job)
    return sub_job
