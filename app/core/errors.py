"""Exception hierarchy + the standard error envelope. See docs/conventions.md.

Out of scope for Phase 0 (no API endpoints yet) — defined now because
AppError.http_status is a natural anchor for the FastAPI exception handlers
Phase 1 will register, and nothing here depends on Phase 1 code.
"""

from typing import Any


class AppError(Exception):
    code: str = "INTERNAL_ERROR"
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
    code = "VALIDATION_ERROR"
    http_status = 422


class NotFoundError(AppError):
    code = "NOT_FOUND"
    http_status = 404


class ConflictError(AppError):
    code = "CONFLICT"
    http_status = 409


class AuthError(AppError):
    code = "AUTH_ERROR"
    http_status = 401


class RateLimitError(AppError):
    code = "RATE_LIMITED"
    http_status = 429


class ProviderError(AppError):
    """Carries failure_class, mapped by the worker. See docs/business-rules.md §4."""

    code = "PROVIDER_ERROR"
    http_status = 502

    def __init__(
        self,
        message: str,
        failure_class: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.failure_class = failure_class
