"""POST /api/v2/uploads/presign. See docs/api-routes.md."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v2.schemas.uploads import PresignUploadRequest, PresignUploadResponse
from app.core.auth import require_client_scope
from app.db.models.api_clients import ApiClient

router = APIRouter(tags=["uploads"])


@router.post(
    "/uploads/presign",
    response_model=PresignUploadResponse,
    responses={
        401: {"description": "Invalid API key"},
        422: {"description": "Bad category or angle"},
    },
)
async def presign_uploads(
    body: PresignUploadRequest,
    client: Annotated[ApiClient, Depends(require_client_scope)],
) -> PresignUploadResponse:
    raise NotImplementedError("Real presign lands in Phase 4 / mock fixture in Step 3.")
