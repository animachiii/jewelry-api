"""GET /api/v2/config, POST /api/v2/internal/config/sync. See docs/api-routes.md."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v2.schemas.config import ConfigResponse
from app.core.auth import require_client_scope, require_ops_scope
from app.db.models.api_clients import ApiClient

router = APIRouter(tags=["config"])


@router.get(
    "/config",
    response_model=ConfigResponse,
    responses={401: {"description": "Invalid API key"}, 503: {"description": "Config unavailable"}},
)
async def get_config(
    client: Annotated[ApiClient, Depends(require_client_scope)],
) -> ConfigResponse:
    raise NotImplementedError("Real config lookup lands in Phase 3 / mock fixture in Step 3.")


@router.post(
    "/internal/config/sync",
    status_code=202,
    responses={
        401: {"description": "Invalid API key"},
        403: {"description": "Insufficient scope"},
    },
)
async def sync_config(
    client: Annotated[ApiClient, Depends(require_ops_scope)],
) -> None:
    raise NotImplementedError("Sheets sync lands in Phase 3.")
