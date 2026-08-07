"""POST /api/v2/generate. See docs/api-routes.md and docs/business-rules.md §1, §8."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v2.schemas.generate import GenerateJobRequest, JobAcceptedResponse
from app.core.auth import require_client_scope
from app.core.idempotency import require_idempotency_key
from app.db.models.api_clients import ApiClient

router = APIRouter(tags=["generate"])


@router.post(
    "/generate",
    status_code=202,
    response_model=JobAcceptedResponse,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        409: {"description": "Idempotency key conflict"},
        422: {"description": "Bad category, disabled angle, or synthetic not allowed"},
        429: {"description": "Rate limit or quota exceeded"},
    },
)
async def create_job(
    body: GenerateJobRequest,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> JobAcceptedResponse:
    raise NotImplementedError("Real job creation lands in Phase 2 / mock fixture in Step 3.")
