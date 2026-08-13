"""Idempotency-Key storage and replay. See docs/schema.md — idem:{client_id}:{key}."""

from typing import Annotated

import redis.asyncio as redis
from fastapi import Header

from app.core.errors import AppError, ErrorCode
from app.core.redis_client import new_redis_client

TTL_SECONDS = 24 * 60 * 60


def _client() -> redis.Redis:
    """A fresh client per call, deliberately not cached at module scope.

    Phase 8 was the first code path to actually exercise this module's
    Redis calls (get_replay/store_replay predate it but were dead code —
    only /generate's real dedup lived in Postgres from Phase 2 on). A cached
    module-global client bound its connection to whichever event loop
    created it; under pytest-asyncio's per-test event loop, a second test
    reusing the same process-wide singleton hit `RuntimeError: Event loop is
    closed` the moment it touched a connection opened by a prior test's now-
    closed loop. `app/core/redis_client.py` avoids this the same way workers
    do (Phase 7) — building fresh per call rather than caching across a
    boundary that isn't guaranteed to share one loop. This endpoint isn't
    hot enough for connection reuse to matter.

    Delegates to `new_redis_client()` (rather than its own `redis.from_url`
    call, as before 2026-08-13) so this module gets `REDIS_SOCKET_TIMEOUT_
    SECONDS` for free — see that function's docstring for the incident.
    """
    return new_redis_client()


async def get_job_id(client_id: str, idempotency_key: str) -> str | None:
    value = await _client().get(f"idem:{client_id}:{idempotency_key}")
    return str(value) if value is not None else None


async def store(client_id: str, idempotency_key: str, job_id: str) -> None:
    await _client().set(f"idem:{client_id}:{idempotency_key}", job_id, ex=TTL_SECONDS)


class IdempotencyKeyConflictError(AppError):
    code = ErrorCode.IDEMPOTENCY_KEY_CONFLICT
    http_status = 409


async def get_replay(client_id: str, idempotency_key: str) -> tuple[str, str] | None:
    """Returns (job_id, payload_hash) if this key has been used before."""
    value = await _client().get(f"idem:{client_id}:{idempotency_key}")
    if value is None:
        return None
    job_id, _, payload_hash = str(value).partition("|")
    return job_id, payload_hash


async def store_replay(
    client_id: str, idempotency_key: str, job_id: str, payload_hash: str
) -> None:
    await _client().set(
        f"idem:{client_id}:{idempotency_key}", f"{job_id}|{payload_hash}", ex=TTL_SECONDS
    )


async def get_retry_target(client_id: str, idempotency_key: str) -> str | None:
    """Separate `retryidem:` namespace from /generate's `idem:` prefix —
    see phases/phase-8-failure-retry.md's reality-check section. Redis-only,
    24h TTL: unlike /generate's durable Postgres dedup (jobs.payload_hash,
    Phase 2), a retry has no row of its own to persist a marker on. A replay
    outside the TTL executes as a fresh retry — accepted given the 3-attempt
    ceiling, not hidden.
    """
    value = await _client().get(f"retryidem:{client_id}:{idempotency_key}")
    return str(value) if value is not None else None


async def store_retry_target(client_id: str, idempotency_key: str, target: str) -> None:
    await _client().set(f"retryidem:{client_id}:{idempotency_key}", target, ex=TTL_SECONDS)


class IdempotencyKeyRequiredError(AppError):
    code = ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    http_status = 400


async def require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if not idempotency_key:
        raise IdempotencyKeyRequiredError("The Idempotency-Key header is required.")
    return idempotency_key
