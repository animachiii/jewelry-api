"""GET /api/v2/health — liveness + dependency check. No auth. See docs/api-routes.md."""

from typing import Annotated

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.health import DependencyStatus, HealthResponse
from app.config import settings
from app.db.models.config_versions import ConfigVersion
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, responses={503: {"description": "Down"}})
async def get_health(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> HealthResponse:
    db_ok = True
    active_version: int | None = None
    try:
        result = await session.execute(
            select(ConfigVersion.version_number).where(ConfigVersion.is_active)
        )
        active_version = result.scalar_one_or_none()
    except SQLAlchemyError:
        db_ok = False

    redis_ok = True
    try:
        client = redis.from_url(settings.REDIS_URL)  # type: ignore[no-untyped-call]
        await client.ping()
        await client.aclose()
    except Exception:
        redis_ok = False

    if not (db_ok and redis_ok):
        response.status_code = 503

    return HealthResponse(
        status="ok" if (db_ok and redis_ok) else "degraded",
        dependencies=DependencyStatus(
            db="ok" if db_ok else "down",
            redis="ok" if redis_ok else "down",
            storage="ok",
            active_config_version=active_version,
        ),
    )
