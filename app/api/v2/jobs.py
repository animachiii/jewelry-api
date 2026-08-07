"""GET /api/v2/jobs, GET /api/v2/jobs/{job_id}/cost. Ops-only. See docs/api-routes.md.

Not listed in CLAUDE.md's original Phase 0 folder sketch — the ops job-listing
and cost-report routes weren't split out from generate.py/status.py at that
point. Splitting them into their own module keeps route files aligned with
one route family each, consistent with the rest of api/v2/.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.v2.schemas.jobs import JobCostResponse, JobListResponse
from app.core.auth import require_ops_scope
from app.db.models.api_clients import ApiClient
from app.db.models.enums import JobStatus

router = APIRouter(tags=["jobs"])


@router.get(
    "/jobs",
    response_model=JobListResponse,
    responses={401: {"description": "Invalid API key"}, 403: {"description": "Insufficient scope"}},
)
async def list_jobs(
    client: Annotated[ApiClient, Depends(require_ops_scope)],
    status: JobStatus | None = None,
    category_code: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JobListResponse:
    raise NotImplementedError("Real job listing lands in Phase 11 / mock fixture in Step 3.")


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
) -> JobCostResponse:
    raise NotImplementedError("Real cost report lands in Phase 11 / mock fixture in Step 3.")
