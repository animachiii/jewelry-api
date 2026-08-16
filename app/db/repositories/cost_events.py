"""Queries against `cost_events`, for GET /jobs/{job_id}/cost. See
phases/phase-11-observability-cost-tracking.md Step 2.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.cost_events import CostEvent
from app.db.models.enums import Angle
from app.db.models.jobs import SubJob


async def get_by_job(
    session: AsyncSession, job_id: uuid.UUID
) -> list[tuple[CostEvent, Angle | None, int]]:
    """Every CostEvent for a job, ordered created_at ASC, each row paired with
    its sub-job's angle (a cost event has no angle column of its own — only
    sub_job_id) and a derived attempt_count: `cost_events` has no
    attempt_count column either, so this numbers each sub-job's own events in
    creation order via ROW_NUMBER() — "attempt count at the time of this
    call" is fully derivable from data that already exists, so no new column
    is justified (docs/conventions.md's migration discipline).
    """
    attempt_number = (
        func.row_number()
        .over(partition_by=CostEvent.sub_job_id, order_by=CostEvent.created_at)
        .label("attempt_number")
    )
    result = await session.execute(
        select(CostEvent, SubJob.angle, attempt_number)
        .outerjoin(SubJob, CostEvent.sub_job_id == SubJob.id)
        .where(CostEvent.job_id == job_id)
        .order_by(CostEvent.created_at)
    )
    return [(event, angle, attempt) for event, angle, attempt in result.all()]
