"""Redis token bucket shared across every worker process. See docs/schema.md
`provider:gemini:tokens` and docs/ai-integration.md: without this, four
sub-tasks per job multiplied across concurrent jobs bursts straight into
429s, which under fail-fast converts directly into PARTIAL_SUCCESS for every
in-flight job at once.

Fixed-window counter, not a true leaky/token-bucket algorithm — simple,
correct for "N calls per minute," and matches the existing
docs/schema.md key-pattern language ("tokens", "rolling"). A more precise
algorithm is a straightforward later swap behind the same `acquire` seam.
"""

import time

import redis.asyncio as redis

from app.config import settings

KEY_PREFIX = "provider:gemini:tokens"
WINDOW_SECONDS = 60


def _window_key(now: float | None = None) -> str:
    window = int((now if now is not None else time.time()) // WINDOW_SECONDS)
    return f"{KEY_PREFIX}:{window}"


async def acquire(redis_client: redis.Redis, now: float | None = None) -> bool:
    """Increments this window's counter and returns whether the caller is
    still under `settings.GEMINI_RATE_LIMIT_PER_MINUTE`. The first caller to
    touch a window sets its expiry so stale windows don't accumulate.
    """
    key = _window_key(now)
    count = int(await redis_client.incr(key))
    if count == 1:
        await redis_client.expire(key, WINDOW_SECONDS * 2)
    return count <= settings.GEMINI_RATE_LIMIT_PER_MINUTE
