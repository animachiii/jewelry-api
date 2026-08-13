"""Shared async Redis client — one connection pool per process, same singleton
pattern as `app/core/idempotency.py`. FastAPI routes obtain it via `Depends(get_redis)`
so services stay framework-free (see docs/conventions.md layering rules) while still
receiving an already-constructed client instead of importing settings themselves.
"""

from collections.abc import AsyncGenerator

import redis.asyncio as redis

from app.config import settings

_redis: redis.Redis | None = None


def _timeout_kwargs() -> dict[str, int]:
    """`settings.REDIS_SOCKET_TIMEOUT_SECONDS` -- see its own docstring for
    the incident this closes. Centralized so every client-construction site
    gets it from one place rather than repeating the same two kwargs.
    """
    return {
        "socket_timeout": settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        "socket_connect_timeout": settings.REDIS_SOCKET_TIMEOUT_SECONDS,
    }


def get_redis_client() -> redis.Redis:
    """Process-wide singleton. Safe **only** where one event loop lives for the
    life of the process — i.e. the FastAPI app. A Celery task must use
    `new_redis_client()` instead: see that function's docstring.
    """
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True, **_timeout_kwargs())  # type: ignore[no-untyped-call]
    return _redis


def new_redis_client() -> redis.Redis:
    """A fresh, unshared client the caller owns and must `aclose()`.

    Celery task bodies run through `app/workers/_async_utils.py::run_async`,
    which calls `asyncio.run()` — that *closes* the loop when the task
    finishes. A cached client's connections stay bound to that dead loop, so
    the next task in the same worker process fails with
    `RuntimeError('Event loop is closed')` (observed live 2026-08-12: the
    first angle of a job succeeded, every subsequent one died, and because
    the error escapes the task body rather than going through
    `generation_service`'s handling, the sub-job was orphaned at GENERATING
    instead of landing on FAILED). Third occurrence of this same
    cached-module-global-Redis-client bug — `app/core/idempotency.py` (Phase
    8) and `app/core/ratelimit.py` (Phase 10) were both fixed the same way.

    Also carries `settings.REDIS_SOCKET_TIMEOUT_SECONDS` since 2026-08-13 —
    a *different* bug from the one above (fresh client, correct loop, but no
    socket timeout at all) that hung the entire single-worker queue. Both
    `idempotency.py::_client` and `ratelimit.py::_client` now delegate here
    instead of duplicating `redis.from_url(...)`, so a future third bug in
    client construction only needs fixing once.
    """
    client: redis.Redis = redis.from_url(
        settings.REDIS_URL, decode_responses=True, **_timeout_kwargs()
    )  # type: ignore[no-untyped-call]
    return client


async def get_redis() -> AsyncGenerator[redis.Redis, None]:
    """FastAPI dependency form — `Depends(get_redis)` in routes."""
    yield get_redis_client()
