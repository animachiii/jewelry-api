"""Redis token bucket. See docs/schema.md — ratelimit:{client_id}:{minute}."""

import time

import redis.asyncio as redis

from app.core.redis_client import new_redis_client


def _client() -> redis.Redis:
    """A fresh client per call, deliberately not cached at module scope —
    see app/core/idempotency.py::_client's docstring for why (Phase 8 hit
    this exact bug first: a cached module-global client binds its
    connection to whichever event loop created it, and pytest-asyncio's
    per-test event loop makes a cached singleton here crash the moment a
    second test reuses it). This module was never actually exercised by any
    test until Phase 10 wired allow() into /generate — same first-real-use
    exposure as idempotency.py's Redis calls were in Phase 8.

    Delegates to `new_redis_client()` (rather than its own `redis.from_url`
    call, as before 2026-08-13) so this module gets `REDIS_SOCKET_TIMEOUT_
    SECONDS` for free — see that function's docstring for the incident.
    """
    return new_redis_client()


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
