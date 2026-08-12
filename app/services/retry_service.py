"""Real retry execution. See docs/business-rules.md §5 and
phases/phase-8-failure-retry.md.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import JobStatus, SubJobStatus
from app.db.models.jobs import Job, SubJob
from app.db.repositories import job_events as job_events_repo
from app.services.status_rollup import TERMINAL_STATUSES


def execute_retry(session: AsyncSession, job: Job, sub_job: SubJob) -> None:
    """Resets one FAILED sub-job to PENDING for a fresh
    generation.transform_photo dispatch. Does not commit or dispatch — the
    caller (app/api/v2/retry.py) controls both, after also storing the
    idempotency marker as part of the same accepted request.

    attempt_count is NOT incremented here:
    app/services/generation_service.py::transform_photo already increments it
    once per provider attempt inside its own retry loop, regardless of
    whether that call was triggered by a fresh dispatch or a client retry —
    it's a single shared counter (docs/schema.md), and
    check_retry_preconditions already enforces the combined ceiling before
    this function runs. Incrementing it again here would double-count a
    single client retry against that ceiling.
    """
    from_sub_status = sub_job.status
    sub_job.status = SubJobStatus.PENDING
    sub_job.failure_class = None
    sub_job.error_message = None

    from_job_status = job.status
    if job.status in TERMINAL_STATUSES:
        # Mirrors orchestration_service.dispatch_job's own eager PROCESSING
        # write (Phase 7) — without this, a GET /status read in the gap
        # between accept and the retried angle's actual completion would
        # show a stale terminal parent next to a PENDING sub-job. The
        # authoritative recompute still runs inside transform_photo once the
        # retry finishes (docs/business-rules.md §2).
        job.status = JobStatus.PROCESSING
        job.completed_at = None

    job_events_repo.record_event(
        session,
        job.id,
        "RETRY_REQUESTED",
        sub_job_id=sub_job.id,
        from_status=from_sub_status.value,
        to_status=SubJobStatus.PENDING.value,
        detail={
            "angle": sub_job.angle.value if sub_job.angle is not None else None,
            "attempt_count": sub_job.attempt_count,
            "job_from_status": from_job_status.value,
        },
    )
