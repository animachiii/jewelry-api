"""Shared async Redis client — one connection pool per process, same singleton
pattern as `app/core/idempotency.py`. FastAPI routes obtain it via `Depends(get_redis)`
so services stay framework-free (see docs/conventions.md layering rules) while still
receiving an already-constructed client instead of importing settings themselves.
"""

from collections.abc import AsyncGenerator

import redis.asyncio as redis

from app.config import settings

_redis: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)  # type: ignore[no-untyped-call]
    return _redis


async def get_redis() -> AsyncGenerator[redis.Redis, None]:
    """FastAPI dependency form — `Depends(get_redis)` in routes."""
    yield get_redis_client()
