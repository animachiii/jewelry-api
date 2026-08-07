"""GET /api/v2/config, POST /api/v2/internal/config/sync. See docs/api-routes.md."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.config import ConfigResponse
from app.core.auth import require_client_scope, require_ops_scope
from app.db.models.api_clients import ApiClient
from app.db.repositories import config_versions as config_versions_repo
from app.db.session import get_db
from app.services.config_service import ConfigUnavailableError, build_config_response

router = APIRouter(tags=["config"])


@router.get(
    "/config",
    response_model=ConfigResponse,
    responses={401: {"description": "Invalid API key"}, 503: {"description": "Config unavailable"}},
)
async def get_config(
    client: Annotated[ApiClient, Depends(require_client_scope)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> ConfigResponse:
    active = await config_versions_repo.get_active(session)
    if active is None:
        raise ConfigUnavailableError("No active config version found.")
    return build_config_response(active)


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
