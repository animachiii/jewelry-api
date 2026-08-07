"""All queries against `jobs` and `sub_jobs`. See docs/conventions.md."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import Angle
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
    """MOCK_MODE-only: stands in for real job creation (Phase 2). Picks a
    deterministic existing job for this client to represent as "the job just
    created" — see phases/phase-1-api-contract.md Step 3.
    """
    result = await session.execute(
        select(Job).where(Job.client_id == client_id).order_by(Job.created_at).limit(1)
    )
    return result.scalar_one_or_none()
