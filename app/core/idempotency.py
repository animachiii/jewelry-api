"""Idempotency-Key storage and replay. See docs/schema.md — idem:{client_id}:{key}."""

from typing import Annotated

import redis.asyncio as redis
from fastapi import Header

from app.config import settings
from app.core.errors import AppError, ErrorCode

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


class IdempotencyKeyRequiredError(AppError):
    code = ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    http_status = 400


async def require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if not idempotency_key:
        raise IdempotencyKeyRequiredError("The Idempotency-Key header is required.")
    return idempotency_key
