"""Assembles JobStatusResponse from a Job + its sub_jobs. See docs/api-routes.md
and docs/business-rules.md §3-4.

Real read against Postgres + live signed URLs — not a canned fixture. Only
`/generate` and `/retry` (whose real business logic needs Phase 2/8) are
gated behind `MOCK_MODE`; a status read is exactly what production will do.
"""

from app.api.v2.schemas.status import AngleStatus, JobStatusResponse
from app.db.models.enums import FailureClass, JobStatus, SourceType, SubJobStatus
from app.db.models.jobs import Job, SubJob
from app.services import storage_service

_RETRYABLE_FAILURE_CLASSES = {
    FailureClass.RATE_LIMITED,
    FailureClass.TRANSIENT_PROVIDER,
    FailureClass.TRANSIENT_NETWORK,
    FailureClass.INTERNAL,
}

_NON_TERMINAL_STATUSES = {JobStatus.PENDING, JobStatus.PROCESSING}


def build_angle_status(
    job: Job, sub_job: SubJob, output_bucket_and_path: tuple[str, str] | None
) -> AngleStatus:
    retryable = (
        sub_job.status == SubJobStatus.FAILED
        and sub_job.failure_class in _RETRYABLE_FAILURE_CLASSES
    )

    image_url: str | None = None
    if sub_job.status == SubJobStatus.COMPLETED and output_bucket_and_path is not None:
        bucket, path = output_bucket_and_path
        image_url = storage_service.generate_signed_url(bucket, path)

    return AngleStatus(
        angle=sub_job.angle,
        status=sub_job.status,
        source_type=sub_job.source_type,
        synthetic=sub_job.source_type == SourceType.SYNTHETIC,
        image_url=image_url,
        qa_status=sub_job.qa_status,
        qa_score=float(sub_job.qa_score) if sub_job.qa_score is not None else None,
        failure_class=sub_job.failure_class,
        error_message=sub_job.error_message,
        retryable=retryable,
        retry_url=(
            f"/api/v2/jobs/{job.id}/angles/{sub_job.angle.value}/retry" if retryable else None
        ),
    )


def build_job_status_response(
    job: Job,
    angle_statuses: list[AngleStatus],
) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        category_code=job.category_code,
        requested_angles=job.requested_angles,
        succeeded_angles=job.succeeded_angles,
        failed_angles=job.failed_angles,
        sku_reference=job.sku_reference,
        created_at=job.created_at,
        completed_at=job.completed_at,
        angles=angle_statuses,
    )


def is_non_terminal(job: Job) -> bool:
    return job.status in _NON_TERMINAL_STATUSES
