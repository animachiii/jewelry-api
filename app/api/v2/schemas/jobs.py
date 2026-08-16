"""Ops routes — GET /jobs, GET /jobs/{id}/cost. Ops-only scope, see docs/api-routes.md."""

from datetime import datetime

from pydantic import BaseModel

from app.db.models.enums import JobStatus, Operation


class JobSummary(BaseModel):
    job_id: str
    operation: Operation
    status: JobStatus
    # NULL for background operations, RECOLOR, and MIX (docs/schema.md) — only
    # ANGLE_GENERATION and MATCH (which reuses this column for
    # target_category) ever set it. Found while building
    # phases/phase-11-observability-cost-tracking.md's real GET /jobs: this
    # was typed non-optional here since before any operation but
    # ANGLE_GENERATION existed.
    category_code: str | None
    requested_angles: int
    succeeded_angles: int
    failed_angles: int
    created_at: datetime
    completed_at: datetime | None = None


class JobListResponse(BaseModel):
    jobs: list[JobSummary]
    total: int
    page: int
    page_size: int


class CostEventItem(BaseModel):
    sub_job_id: str | None
    angle: str | None
    attempt_count: int
    provider: str
    operation: str
    model_version: str
    units: int
    unit_cost_usd: float
    total_cost_usd: float
    created_at: datetime


class JobCostResponse(BaseModel):
    job_id: str
    total_cost_usd: float
    events: list[CostEventItem]
