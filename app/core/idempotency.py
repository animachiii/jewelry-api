"""Idempotency-Key storage and replay. See docs/schema.md — idem:{client_id}:{key}."""

import redis.asyncio as redis

from app.config import settings

_redis: redis.Redis | None = None

TTL_SECONDS = 24 * 60 * 60


def _client() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)  # type: ignore[no-untyped-call]
    return _redis


async def get_job_id(client_id: str, idempotency_key: str) -> str | None:
    value = await _client().get(f"idem:{client_id}:{idempotency_key}")
    return str(value) if value is not None else None


async def store(client_id: str, idempotency_key: str, job_id: str) -> None:
    await _client().set(f"idem:{client_id}:{idempotency_key}", job_id, ex=TTL_SECONDS)
