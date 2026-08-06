"""Redis token bucket. See docs/schema.md — ratelimit:{client_id}:{minute}."""

import time

import redis.asyncio as redis

from app.config import settings

_redis: redis.Redis | None = None


def _client() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)  # type: ignore[no-untyped-call]
    return _redis


async def allow(client_id: str, limit_per_minute: int) -> bool:
    """Fixed-window token bucket keyed by client + current minute.

    Returns True and consumes one token if under the limit, False otherwise.
    """
    minute_bucket = int(time.time() // 60)
    key = f"ratelimit:{client_id}:{minute_bucket}"
    r = _client()
    count = int(await r.incr(key))
    if count == 1:
        await r.expire(key, 120)
    return count <= limit_per_minute
