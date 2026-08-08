"""GET /api/v2/health — no auth. See docs/api-routes.md."""

from typing import Literal

from pydantic import BaseModel


class DependencyStatus(BaseModel):
    db: Literal["ok", "down"]
    redis: Literal["ok", "down"]
    storage: Literal["ok", "down"]
    active_config_version: int | None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    dependencies: DependencyStatus
