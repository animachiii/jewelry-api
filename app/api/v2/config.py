"""GET /api/v2/config, POST /api/v2/internal/config/sync. See docs/api-routes.md."""

from typing import Annotated

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.config import ConfigResponse, ConfigSyncResponse
from app.core.auth import require_client_scope, require_ops_scope
from app.core.redis_client import get_redis
from app.db.models.api_clients import ApiClient
from app.db.session import get_db
from app.services.config_service import get_config_response
from app.services.config_sync_service import sync_config as sync_config_service

router = APIRouter(tags=["config"])


@router.get(
    "/config",
    response_model=ConfigResponse,
    responses={401: {"description": "Invalid API key"}, 503: {"description": "Config unavailable"}},
)
async def get_config(
    client: Annotated[ApiClient, Depends(require_client_scope)],
    session: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ConfigResponse:
    return await get_config_response(session, redis)


@router.post(
    "/internal/config/sync",
    status_code=202,
    response_model=ConfigSyncResponse,
    responses={
        401: {"description": "Invalid API key"},
        403: {"description": "Insufficient scope"},
    },
)
async def sync_config(
    client: Annotated[ApiClient, Depends(require_ops_scope)],
    session: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ConfigSyncResponse:
    version = await sync_config_service(session, redis)
    return ConfigSyncResponse(
        config_version=version.version_number,
        sync_status=version.sync_status.value,
        activated=version.is_active,
    )
