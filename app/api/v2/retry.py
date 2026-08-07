"""POST /api/v2/jobs/{job_id}/angles/{angle}/retry.

See docs/api-routes.md and docs/business-rules.md §5.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.auth import require_client_scope
from app.core.idempotency import require_idempotency_key
from app.db.models.api_clients import ApiClient
from app.db.models.enums import Angle

router = APIRouter(tags=["retry"])


@router.post(
    "/jobs/{job_id}/angles/{angle}/retry",
    status_code=202,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        404: {"description": "Not found, or not owned by this client"},
        409: {"description": "Not FAILED, retry ceiling reached, or input expired"},
    },
)
async def retry_angle(
    job_id: str,
    angle: Angle,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> None:
    raise NotImplementedError("Real retry lands in Phase 8 / mock fixture in Step 3.")
