"""GET /api/v2/status/{job_id} — see docs/api-routes.md and docs/business-rules.md §3-4."""

from datetime import datetime

from pydantic import BaseModel

from app.db.models.enums import (
    Angle,
    FailureClass,
    JobStatus,
    Operation,
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


class BackgroundResultStatus(BaseModel):
    """Same per-item fields as AngleStatus, minus `angle` — a background job
    has no angle at all. See phases/phase-15-background-operations.md Step 4:
    `AngleStatus.angle` itself stays non-nullable so `angles` is
    byte-identical to before this phase for existing angle jobs; this is a
    distinct type, not a relaxation of that one.
    """

    status: SubJobStatus
    source_type: SourceType
    synthetic: bool
    image_url: str | None = None
    # Additive, 2026-08-13 — see
    # docs/superpowers/specs/2026-08-13-background-qa-preview-and-retry-design.md.
    # Present whenever an output exists, regardless of status -- unlike
    # image_url, which stays exactly as documented (COMPLETED-only, the
    # real ERP client's existing guarantee). A client that doesn't know
    # about this field sees no behavior change.
    preview_image_url: str | None = None
    qa_status: QAStatus
    qa_score: float | None = None
    failure_class: FailureClass | None = None
    error_message: str | None = None
    retryable: bool
    retry_url: str | None = None


class VariantStatus(BaseModel):
    """Same per-item fields as BackgroundResultStatus, minus `angle` (same
    reason — a MATCH sub-job has no angle either) plus `variant_index` —
    MATCH's 1-4 companion-piece variants need something to order/identify
    themselves by, the same role `angle` plays for AngleStatus. No
    `preview_image_url`: that's background's QA-preview-specific addition
    (2026-08-13), not applicable here since MATCH never enters QA_REVIEW —
    see phases/phase-18-match.md Step 4. `qa_status`/`qa_score` are kept for
    response-shape consistency with the other two status types even though
    they're always NOT_APPLICABLE/None for MATCH (it never enters
    QA_REVIEW) — a client parsing all three shapes the same way shouldn't
    have to special-case MATCH's.
    """

    variant_index: int
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
    # Additive — see phases/phase-15-background-operations.md Step 4 and
    # phases/phase-18-match.md Step 4. Read this first: ANGLE_GENERATION ->
    # read `angles`; a background job -> read `results`; a MATCH job -> read
    # `variants`. `category_code` is None for a background job (migration
    # 0008) but holds the requested target_category for a MATCH job;
    # everything else here is unaffected by operation.
    operation: Operation
    category_code: str | None
    requested_angles: int
    succeeded_angles: int
    failed_angles: int
    sku_reference: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    angles: list[AngleStatus]
    results: list[BackgroundResultStatus] = []
    variants: list[VariantStatus] = []
