"""Phase 6 Checkpoint 1 — Redis token bucket rate limiter."""

from collections.abc import AsyncGenerator

import fakeredis.aioredis
import pytest

from app.config import settings
from app.services import rate_limiter


@pytest.fixture
async def redis_client() -> AsyncGenerator[fakeredis.aioredis.FakeRedis, None]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


async def test_acquire_true_under_limit_false_over(
    redis_client: fakeredis.aioredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "GEMINI_RATE_LIMIT_PER_MINUTE", 3)
    now = 1_000_000.0

    results = [await rate_limiter.acquire(redis_client, now=now) for _ in range(4)]
    assert results == [True, True, True, False]


async def test_new_window_resets_the_count(
    redis_client: fakeredis.aioredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "GEMINI_RATE_LIMIT_PER_MINUTE", 1)
    now = 1_000_000.0

    assert await rate_limiter.acquire(redis_client, now=now) is True
    assert await rate_limiter.acquire(redis_client, now=now) is False

    next_window = now + rate_limiter.WINDOW_SECONDS
    assert await rate_limiter.acquire(redis_client, now=next_window) is True


def test_window_key_buckets_by_minute() -> None:
    base = 1_000_000.0
    assert rate_limiter._window_key(base) == rate_limiter._window_key(base + 1)
    assert rate_limiter._window_key(base) != rate_limiter._window_key(
        base + rate_limiter.WINDOW_SECONDS
    )
