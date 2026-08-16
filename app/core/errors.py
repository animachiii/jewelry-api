"""Exception hierarchy + the standard error envelope. See docs/conventions.md.

`ErrorCode` is the frozen contract the Flutter ERP branches on — see
docs/api-routes.md. Adding a value later is fine; renaming or removing one
is a breaking API change.
"""

from enum import StrEnum
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = structlog.get_logger()


class ErrorCode(StrEnum):
    INVALID_API_KEY = "INVALID_API_KEY"
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
    CATEGORY_NOT_FOUND = "CATEGORY_NOT_FOUND"
    CATEGORY_INACTIVE = "CATEGORY_INACTIVE"
    ANGLE_NOT_ENABLED = "ANGLE_NOT_ENABLED"
    SYNTHETIC_NOT_ALLOWED = "SYNTHETIC_NOT_ALLOWED"
    NO_ANGLES_REQUESTED = "NO_ANGLES_REQUESTED"
    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    ASSET_NOT_OWNED = "ASSET_NOT_OWNED"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    SUBJOB_NOT_RETRYABLE = "SUBJOB_NOT_RETRYABLE"
    RETRY_LIMIT_EXCEEDED = "RETRY_LIMIT_EXCEEDED"
    INPUT_ASSET_EXPIRED = "INPUT_ASSET_EXPIRED"
    SUB_JOB_NOT_FOUND = "SUB_JOB_NOT_FOUND"
    QA_NOT_PENDING = "QA_NOT_PENDING"
    OPERATION_DISABLED = "OPERATION_DISABLED"
    PRESET_NOT_FOUND = "PRESET_NOT_FOUND"
    PRESET_INACTIVE = "PRESET_INACTIVE"
    PALETTE_NOT_FOUND = "PALETTE_NOT_FOUND"
    PALETTE_INACTIVE = "PALETTE_INACTIVE"
    ANGLE_JOB_RETRY_NOT_ALLOWED = "ANGLE_JOB_RETRY_NOT_ALLOWED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    CONFIG_UNAVAILABLE = "CONFIG_UNAVAILABLE"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    code: str = ErrorCode.INTERNAL_ERROR
    http_status: int = 500

    def __init__(
        self, message: str, details: dict[str, Any] | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code is not None:
            self.code = code

    def to_envelope(self, request_id: str) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


class ValidationError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 422


class NotFoundError(AppError):
    code = ErrorCode.JOB_NOT_FOUND
    http_status = 404


class ConflictError(AppError):
    code = ErrorCode.IDEMPOTENCY_KEY_CONFLICT
    http_status = 409


class AuthError(AppError):
    code = ErrorCode.INVALID_API_KEY
    http_status = 401


class RateLimitError(AppError):
    code = ErrorCode.RATE_LIMIT_EXCEEDED
    http_status = 429

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        retry_after_seconds: int = 60,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after_seconds = retry_after_seconds


class QuotaExceededError(AppError):
    """See docs/api-routes.md's documented 429 behavior and
    phases/phase-10-auth-security.md Step 1 — wired for real here, the
    ErrorCode existed since Phase 1 with nothing raising it.
    """

    code = ErrorCode.QUOTA_EXCEEDED
    http_status = 429

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        retry_after_seconds: int = 60,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after_seconds = retry_after_seconds


class ProviderError(AppError):
    """Carries failure_class, mapped by the worker. See docs/business-rules.md §4."""

    code = ErrorCode.INTERNAL_ERROR
    http_status = 502

    def __init__(
        self,
        message: str,
        failure_class: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.failure_class = failure_class


def _request_id(request: Request) -> str:
    from app.core.middleware import get_request_id

    return get_request_id(request)


def _envelope(code: str, message: str, request_id: str, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": details, "request_id": request_id}
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Converts every AppError into the standard envelope. An unhandled exception
    never leaks its text to the client — see docs/conventions.md.
    """

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        request_id = _request_id(request)
        if exc.http_status >= 500:
            logger.error("app_error", code=exc.code, message=exc.message, request_id=request_id)
        headers = {"X-Request-ID": request_id}
        retry_after = getattr(exc, "retry_after_seconds", None)
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_envelope(request_id),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = _request_id(request)
        errors = exc.errors()
        first = errors[0] if errors else {}
        field = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = (
            f"{field}: {first.get('msg')}" if field else str(first.get("msg", "Invalid request"))
        )
        # A @model_validator that raises a bare ValueError (e.g.
        # AngleSpec._exactly_one_mode, PresignUploadRequest._exactly_one_mode)
        # puts the exception object itself in ctx.error — plain json.dumps
        # (what JSONResponse uses) can't serialize that. jsonable_encoder
        # converts it to its str repr, same as everything else here already
        # gets converted to JSON-safe types. A real, pre-existing bug: found
        # while adding Phase 15's presign mode validator, not introduced by it
        # — see phases/phase-15-background-operations.md Step 4.
        safe_errors = jsonable_encoder(errors)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope(
                ErrorCode.VALIDATION_ERROR, message, request_id, {"errors": safe_errors}
            ),
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        logger.error("unhandled_exception", request_id=request_id, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope(
                ErrorCode.INTERNAL_ERROR,
                "An unexpected error occurred.",
                request_id,
                {},
            ),
            headers={"X-Request-ID": request_id},
        )
