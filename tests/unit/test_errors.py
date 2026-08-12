"""Step 1 checkpoint tests — error envelope, exception handlers, request-ID middleware."""

from collections.abc import AsyncGenerator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, model_validator

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


class _MutuallyExclusiveBody(BaseModel):
    a: str | None = None
    b: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "_MutuallyExclusiveBody":
        if (self.a is None) == (self.b is None):
            raise ValueError("exactly one of a or b must be set")
        return self


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

    @app.post("/mode-check")
    async def mode_check(body: _MutuallyExclusiveBody) -> dict[str, str]:
        return {"ok": "true"}

    return app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
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


async def test_model_validator_value_error_returns_clean_422_not_500(client: AsyncClient) -> None:
    """A @model_validator raising a bare ValueError (e.g.
    AngleSpec._exactly_one_mode, PresignUploadRequest._exactly_one_mode) puts
    the exception object itself in the pydantic error's ctx.error — plain
    json.dumps can't serialize that. Found while adding Phase 15's presign
    mode validator, which hit this as a real 500 — see
    phases/phase-15-background-operations.md Step 4 and
    app/core/errors.py::_validation_error_handler's jsonable_encoder fix.
    """
    resp = await client.post("/mode-check", json={"a": "x", "b": "y"})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == ErrorCode.VALIDATION_ERROR
    assert "exactly one of a or b" in body["error"]["message"]


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
        "OPERATION_DISABLED",
        "PRESET_NOT_FOUND",
        "PRESET_INACTIVE",
        "ANGLE_JOB_RETRY_NOT_ALLOWED",
    }
    actual = {member.value for member in ErrorCode}
    assert required <= actual
