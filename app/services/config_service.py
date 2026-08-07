"""GET /api/v2/config — active angle matrix, no prompts/reference images exposed.

See docs/api-routes.md and docs/schema.md (config_versions.payload shape).
Phase 3 adds the Redis `config:active` cache in front of the Postgres read —
see docs/schema.md "What lives in Redis" and docs/business-rules.md §9's
fallback order: Redis cache -> active Postgres row -> hard failure only if
both are unavailable. Redis being unreachable is treated the same as a cache
miss, never as a request failure — see docs/api-routes.md "Served from the
Redis config:active cache; falls back to the active config_versions row in
Postgres on cache miss."
"""

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.config import AngleAvailability, CategoryConfig, ConfigResponse
from app.core.errors import AppError, ErrorCode
from app.db.models.config_versions import ConfigVersion
from app.db.repositories import config_versions as config_versions_repo

logger = structlog.get_logger()

CACHE_KEY = "config:active"
CACHE_TTL_SECONDS = 15 * 60


class ConfigUnavailableError(AppError):
    code = ErrorCode.CONFIG_UNAVAILABLE
    http_status = 503


def build_config_response(config_version: ConfigVersion) -> ConfigResponse:
    categories = []
    for cat in config_version.payload["categories"]:
        angles = {
            angle: AngleAvailability(
                enabled=spec["enabled"], synthetic_allowed=spec["synthetic_allowed"]
            )
            for angle, spec in cat["angles"].items()
        }
        categories.append(
            CategoryConfig(
                code=cat["code"], name=cat["name"], is_active=cat["is_active"], angles=angles
            )
        )
    return ConfigResponse(
        config_version=config_version.version_number,
        categories=categories,
    )


async def _read_cache(redis: Redis) -> ConfigResponse | None:
    try:
        cached = await redis.get(CACHE_KEY)
    except RedisError:
        logger.warning("config_cache_read_failed")
        return None
    if cached is None:
        return None
    return ConfigResponse.model_validate_json(cached)


async def _write_cache(redis: Redis, response: ConfigResponse) -> None:
    try:
        await redis.set(CACHE_KEY, response.model_dump_json(), ex=CACHE_TTL_SECONDS)
    except RedisError:
        # Cache-write failure must never fail the request — see docs/business-rules.md
        # §9 fallback order. The next request just misses the cache again.
        logger.warning("config_cache_write_failed")


async def get_config_response(session: AsyncSession, redis: Redis) -> ConfigResponse:
    """Redis cache -> active Postgres row -> `ConfigUnavailableError`.

    Never calls Google Sheets inline — see docs/api-routes.md.
    """
    cached = await _read_cache(redis)
    if cached is not None:
        return cached

    active = await config_versions_repo.get_active(session)
    if active is None:
        raise ConfigUnavailableError("No active config version found.")

    response = build_config_response(active)
    await _write_cache(redis, response)
    return response


async def invalidate_cache(redis: Redis) -> None:
    """Called after a sync activates a new version — docs/api-routes.md
    `POST /internal/config/sync`: "Activates it and invalidates the Redis cache."
    """
    try:
        await redis.delete(CACHE_KEY)
    except RedisError:
        logger.warning("config_cache_invalidate_failed")
