"""Step 1 checkpoint tests — error envelope, exception handlers, request-ID middleware."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import (
    AppError,
    AuthError,
    ConflictError,
    ErrorCode,
    NotFoundError,
    ProviderError,
    RateLimitError,
    ValidationError,
    register_exception_handlers,
)
from app.core.middleware import RequestIDMiddleware


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    @app.get("/boom/{code}")
    async def boom(code: str) -> None:
        errors: dict[str, AppError] = {
            "validation": ValidationError("bad input"),
            "notfound": NotFoundError("missing"),
            "conflict": ConflictError("conflict"),
            "auth": AuthError("bad key"),
            "ratelimit": RateLimitError("slow down"),
            "provider": ProviderError("provider blew up", failure_class="INTERNAL"),
        }
        raise errors[code]

    @app.get("/zerodiv")
    async def zerodiv() -> None:
        1 / 0  # noqa: B018

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"hello": "world"}

    return app


@pytest.fixture
async def client() -> AsyncClient:
    app = _build_test_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.parametrize(
    ("code", "expected_status", "expected_error_code"),
    [
        ("validation", 422, ErrorCode.VALIDATION_ERROR),
        ("notfound", 404, ErrorCode.JOB_NOT_FOUND),
        ("conflict", 409, ErrorCode.IDEMPOTENCY_KEY_CONFLICT),
        ("auth", 401, ErrorCode.INVALID_API_KEY),
        ("ratelimit", 429, ErrorCode.RATE_LIMIT_EXCEEDED),
        ("provider", 502, ErrorCode.INTERNAL_ERROR),
    ],
)
async def test_app_error_produces_exact_envelope_shape(
    client: AsyncClient, code: str, expected_status: int, expected_error_code: str
) -> None:
    resp = await client.get(f"/boom/{code}")
    assert resp.status_code == expected_status
    body = resp.json()
    assert set(body["error"].keys()) == {"code", "message", "details", "request_id"}
    assert body["error"]["code"] == expected_error_code
    assert isinstance(body["error"]["request_id"], str) and body["error"]["request_id"]


async def test_unhandled_exception_returns_internal_error_no_leak(client: AsyncClient) -> None:
    resp = await client.get("/zerodiv")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == ErrorCode.INTERNAL_ERROR
    assert "ZeroDivisionError" not in body["error"]["message"]
    assert "division" not in body["error"]["message"].lower()
    assert body["error"]["request_id"]
    assert resp.headers["X-Request-ID"] == body["error"]["request_id"]


async def test_request_id_present_and_matches_on_success(client: AsyncClient) -> None:
    resp = await client.get("/ok")
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"]


async def test_request_id_present_and_matches_on_failure(client: AsyncClient) -> None:
    resp = await client.get("/boom/notfound")
    body = resp.json()
    assert resp.headers["X-Request-ID"] == body["error"]["request_id"]


def test_error_code_enum_contains_every_documented_code() -> None:
    required = {
        "INVALID_API_KEY",
        "INSUFFICIENT_SCOPE",
        "CATEGORY_NOT_FOUND",
        "CATEGORY_INACTIVE",
        "ANGLE_NOT_ENABLED",
        "SYNTHETIC_NOT_ALLOWED",
        "NO_ANGLES_REQUESTED",
        "ASSET_NOT_FOUND",
        "ASSET_NOT_OWNED",
        "IDEMPOTENCY_KEY_REQUIRED",
        "IDEMPOTENCY_KEY_CONFLICT",
        "JOB_NOT_FOUND",
        "SUBJOB_NOT_RETRYABLE",
        "RETRY_LIMIT_EXCEEDED",
        "INPUT_ASSET_EXPIRED",
        "RATE_LIMIT_EXCEEDED",
        "QUOTA_EXCEEDED",
        "CONFIG_UNAVAILABLE",
        "INTERNAL_ERROR",
    }
    actual = {member.value for member in ErrorCode}
    assert required <= actual
