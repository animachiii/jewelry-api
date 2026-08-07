"""GET /api/v2/status/{job_id}. See docs/api-routes.md and docs/business-rules.md §3."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v2.schemas.status import JobStatusResponse
from app.core.auth import require_client_scope
from app.db.models.api_clients import ApiClient

router = APIRouter(tags=["status"])


@router.get(
    "/status/{job_id}",
    response_model=JobStatusResponse,
    responses={
        401: {"description": "Invalid API key"},
        404: {"description": "Not found, or not owned by this client"},
    },
)
async def get_status(
    job_id: str,
    client: Annotated[ApiClient, Depends(require_client_scope)],
) -> JobStatusResponse:
    raise NotImplementedError("Real status read lands in Phase 7 / mock fixture in Step 3.")
