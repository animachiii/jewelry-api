"""GET /api/v2/jobs, GET /api/v2/jobs/{job_id}/cost. Ops-only. See docs/api-routes.md.

Not listed in CLAUDE.md's original Phase 0 folder sketch — the ops job-listing
and cost-report routes weren't split out from generate.py/status.py at that
point. Splitting them into their own module keeps route files aligned with
one route family each, consistent with the rest of api/v2/.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.jobs import CostEventItem, JobCostResponse, JobListResponse, JobSummary
from app.core.auth import require_ops_scope
from app.core.errors import NotFoundError
from app.db.models.api_clients import ApiClient
from app.db.models.enums import JobStatus
from app.db.repositories import cost_events as cost_events_repo
from app.db.repositories import jobs as jobs_repo
from app.db.session import get_db

router = APIRouter(tags=["jobs"])


@router.get(
    "/jobs",
    response_model=JobListResponse,
    responses={401: {"description": "Invalid API key"}, 403: {"description": "Insufficient scope"}},
)
async def list_jobs(
    client: Annotated[ApiClient, Depends(require_ops_scope)],
    session: Annotated[AsyncSession, Depends(get_db)],
    status: JobStatus | None = None,
    category_code: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JobListResponse:
    jobs, total = await jobs_repo.list_jobs(
        session,
        status=status,
        category_code=category_code,
        created_after=created_after,
        created_before=created_before,
        page=page,
        page_size=page_size,
    )
    return JobListResponse(
        jobs=[
            JobSummary(
                job_id=str(job.id),
                operation=job.operation,
                status=job.status,
                category_code=job.category_code,
                requested_angles=job.requested_angles,
                succeeded_angles=job.succeeded_angles,
                failed_angles=job.failed_angles,
                created_at=job.created_at,
                completed_at=job.completed_at,
            )
            for job in jobs
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/jobs/{job_id}/cost",
    response_model=JobCostResponse,
    responses={
        401: {"description": "Invalid API key"},
        403: {"description": "Insufficient scope"},
        404: {"description": "Job not found"},
    },
)
async def get_job_cost(
    job_id: str,
    client: Annotated[ApiClient, Depends(require_ops_scope)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobCostResponse:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise NotFoundError("Job not found.", details={"job_id": job_id}) from exc

    # Ops has no client to scope against, unlike GET /status — a plain 404,
    # not the client-scoped 404-not-403 masking that route uses.
    job = await jobs_repo.get_by_id(session, job_uuid)
    if job is None:
        raise NotFoundError("Job not found.", details={"job_id": job_id})

    rows = await cost_events_repo.get_by_job(session, job_uuid)
    events = [
        CostEventItem(
            sub_job_id=str(event.sub_job_id) if event.sub_job_id is not None else None,
            angle=angle.value if angle is not None else None,
            attempt_count=attempt_count,
            provider=event.provider,
            operation=event.operation,
            model_version=event.model_version,
            units=event.units,
            unit_cost_usd=float(event.unit_cost_usd),
            total_cost_usd=float(event.total_cost_usd),
            created_at=event.created_at,
        )
        for event, angle, attempt_count in rows
    ]
    return JobCostResponse(
        job_id=str(job.id),
        total_cost_usd=sum(e.total_cost_usd for e in events),
        events=events,
    )
