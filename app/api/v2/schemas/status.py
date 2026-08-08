"""GET /api/v2/status/{job_id} — see docs/api-routes.md and docs/business-rules.md §3-4."""

from datetime import datetime

from pydantic import BaseModel

from app.db.models.enums import (
    Angle,
    FailureClass,
    JobStatus,
    QAStatus,
    SourceType,
    SubJobStatus,
)


class AngleStatus(BaseModel):
    angle: Angle
    status: SubJobStatus
    source_type: SourceType
    synthetic: bool
    image_url: str | None = None
    qa_status: QAStatus
    qa_score: float | None = None
    failure_class: FailureClass | None = None
    error_message: str | None = None
    retryable: bool
    retry_url: str | None = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    category_code: str
    requested_angles: int
    succeeded_angles: int
    failed_angles: int
    sku_reference: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    angles: list[AngleStatus]
