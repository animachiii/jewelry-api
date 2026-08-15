"""Assembles JobStatusResponse from a Job + its sub_jobs. See docs/api-routes.md
and docs/business-rules.md §3-4.

Real read against Postgres + live signed URLs — not a canned fixture.
`/retry` is real as of Phase 8, same as `/generate` (Phase 2) — a status
read is exactly what production does; settings.MOCK_MODE no longer gates
either route.
"""

from app.api.v2.schemas.status import (
    AngleStatus,
    BackgroundResultStatus,
    JobStatusResponse,
    VariantStatus,
)
from app.db.models.enums import FailureClass, JobStatus, QAStatus, SourceType, SubJobStatus
from app.db.models.jobs import Job, SubJob
from app.services import storage_service
from app.services.job_service import MAX_RETRY_ATTEMPTS

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
    # Angle-jobs only — Phase 15 background jobs get their own `results`
    # builder, `angles` stays exactly as it was before that phase (an
    # additive-only status change, see
    # phases/phase-15-background-operations.md Step 4).
    assert sub_job.angle is not None
    # docs/business-rules.md §5: a sub-job at the retry ceiling must not be
    # offered a retry_url even if its failure_class is otherwise retryable —
    # see phases/phase-8-failure-retry.md Step 1 (a gap found in this
    # function, not present when it was originally written in Phase 1/7).
    retryable = (
        sub_job.status == SubJobStatus.FAILED
        and sub_job.failure_class in _RETRYABLE_FAILURE_CLASSES
        and sub_job.attempt_count < MAX_RETRY_ATTEMPTS
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


def build_background_result_status(
    job: Job, sub_job: SubJob, output_bucket_and_path: tuple[str, str] | None
) -> BackgroundResultStatus:
    """Mirrors build_angle_status exactly, minus the angle field and its
    `/angles/{angle}/retry` URL — a background job's retry route is the
    job-level `POST /jobs/{job_id}/retry` (Step 4), not an angle route.
    """
    assert sub_job.angle is None

    # 2026-08-13: retryable now covers two distinct kinds of retry — the
    # existing regenerate-from-scratch path (FAILED) and the newer
    # retry-QA-only path (QA_REVIEW+FLAGGED, including qa_score is None,
    # the real-incident case where the judge call itself failed rather than
    # scoring low). Both share the same job-level retry_url; the route
    # (app/api/v2/retry.py) is what branches on which kind actually applies.
    retryable = (
        sub_job.status == SubJobStatus.FAILED
        and sub_job.failure_class in _RETRYABLE_FAILURE_CLASSES
        and sub_job.attempt_count < MAX_RETRY_ATTEMPTS
    ) or (
        sub_job.status == SubJobStatus.QA_REVIEW
        and sub_job.qa_status == QAStatus.FLAGGED
        and sub_job.attempt_count < MAX_RETRY_ATTEMPTS
    )

    image_url: str | None = None
    preview_image_url: str | None = None
    if output_bucket_and_path is not None:
        bucket, path = output_bucket_and_path
        preview_image_url = storage_service.generate_signed_url(bucket, path)
        if sub_job.status == SubJobStatus.COMPLETED:
            image_url = preview_image_url

    return BackgroundResultStatus(
        status=sub_job.status,
        source_type=sub_job.source_type,
        synthetic=sub_job.source_type == SourceType.SYNTHETIC,
        image_url=image_url,
        preview_image_url=preview_image_url,
        qa_status=sub_job.qa_status,
        qa_score=float(sub_job.qa_score) if sub_job.qa_score is not None else None,
        failure_class=sub_job.failure_class,
        error_message=sub_job.error_message,
        retryable=retryable,
        retry_url=(f"/api/v2/jobs/{job.id}/retry" if retryable else None),
    )


def build_variant_status(
    job: Job, sub_job: SubJob, output_bucket_and_path: tuple[str, str] | None
) -> VariantStatus:
    """Mirrors build_background_result_status closely, but: `retryable`
    only checks the FAILED branch (the QA_REVIEW+FLAGGED branch is
    structurally impossible for MATCH — it never enters QA_REVIEW, see
    phases/phase-18-match.md Step 4); `retry_url` is the same job-level
    `/jobs/{job_id}/retry` route background operations use (Step 4 there
    generalized it to retry every FAILED sub-job on a job, not just one);
    and there's no `preview_image_url` (that's background's QA-preview-
    specific addition, not applicable here).
    """
    assert sub_job.angle is None
    assert sub_job.variant_index is not None

    retryable = (
        sub_job.status == SubJobStatus.FAILED
        and sub_job.failure_class in _RETRYABLE_FAILURE_CLASSES
        and sub_job.attempt_count < MAX_RETRY_ATTEMPTS
    )

    image_url: str | None = None
    if sub_job.status == SubJobStatus.COMPLETED and output_bucket_and_path is not None:
        bucket, path = output_bucket_and_path
        image_url = storage_service.generate_signed_url(bucket, path)

    return VariantStatus(
        variant_index=sub_job.variant_index,
        status=sub_job.status,
        source_type=sub_job.source_type,
        synthetic=sub_job.source_type == SourceType.SYNTHETIC,
        image_url=image_url,
        qa_status=sub_job.qa_status,
        qa_score=float(sub_job.qa_score) if sub_job.qa_score is not None else None,
        failure_class=sub_job.failure_class,
        error_message=sub_job.error_message,
        retryable=retryable,
        retry_url=(f"/api/v2/jobs/{job.id}/retry" if retryable else None),
    )


def build_job_status_response(
    job: Job,
    angle_statuses: list[AngleStatus],
    result_statuses: list[BackgroundResultStatus] | None = None,
    variant_statuses: list[VariantStatus] | None = None,
) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        operation=job.operation,
        category_code=job.category_code,
        requested_angles=job.requested_angles,
        succeeded_angles=job.succeeded_angles,
        failed_angles=job.failed_angles,
        sku_reference=job.sku_reference,
        created_at=job.created_at,
        completed_at=job.completed_at,
        angles=angle_statuses,
        results=result_statuses or [],
        variants=variant_statuses or [],
    )


def is_non_terminal(job: Job) -> bool:
    return job.status in _NON_TERMINAL_STATUSES
