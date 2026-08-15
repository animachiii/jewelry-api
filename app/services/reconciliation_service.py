"""Reconciliation sweep for stuck sub-jobs (Phase 16 Step 2).

Backstop for a hang the task-timeout mechanism (generation_service/
background_service's `mark_sub_job_timed_out`, dispatched by
app/workers/generation.py / app/workers/background.py) itself fails to
catch — a worker OOM-killed outright, or the container restarting mid-task
(confirmed live 2026-08-15: this deployment's free-tier instance restarts
every 1-4 hours), leaves no running task to hit that timeout at all. Same
"why isn't this in the existing retention worker" split as everything else
here: app/workers/retention.py (app/services/retention_service.py) sweeps
**assets**; this sweeps **jobs**.

**Deliberately scoped to PENDING and GENERATING only — not QA_REVIEW.**
The phase file this was built from sketched "any non-terminal status, any
staleness" as the target. That's wrong against this codebase's own business
rules: docs/business-rules.md §7 and app/services/qa_service.py's
fail-open-to-human design make QA_REVIEW a **correct, unbounded-duration**
holding state — a flagged sub-job waits in `GET /qa/review-queue` for a
human decision, and `compute_parent_status` (app/services/status_rollup.py)
already treats it as "still processing," not stuck. A live query on
2026-08-15 found QA_REVIEW sub-jobs open since 2026-08-13 with no activity
for hours; sweeping those as "stuck" would silently fail a job sitting
correctly in a human's queue. PENDING and GENERATING have no such human
checkpoint — nothing else will ever move them forward once a worker has
lost track of them, which is exactly what this sweep is for.

docs/schema.md has no `sub_jobs.updated_at` column — `job_events` is the
only queryable per-sub-job activity timestamp, confirmed by reading the
schema rather than assumed.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import FailureClass, SubJobStatus
from app.db.models.job_events import JobEvent
from app.db.models.jobs import Job, SubJob
from app.db.repositories import job_events as job_events_repo
from app.db.repositories import jobs as jobs_repo
from app.services.generation_service import recompute_parent_status

STUCK_STATUSES = (SubJobStatus.PENDING, SubJobStatus.GENERATING)


async def _last_activity_at(session: AsyncSession, job: Job, sub_job: SubJob) -> datetime:
    """Most recent job_events timestamp touching this sub-job specifically,
    falling back to the most recent job-level event (sub_job_id IS NULL —
    e.g. the JOB_CREATED row every job gets), falling back to the job's own
    created_at for a sub-job that has never had any event of its own at all
    (never dispatched, or dispatched but its first commit hasn't landed).
    """
    result = await session.execute(
        select(func.max(JobEvent.created_at)).where(JobEvent.sub_job_id == sub_job.id)
    )
    sub_job_ts = result.scalar_one_or_none()
    if sub_job_ts is not None:
        return sub_job_ts

    result = await session.execute(
        select(func.max(JobEvent.created_at)).where(
            JobEvent.job_id == job.id, JobEvent.sub_job_id.is_(None)
        )
    )
    job_ts = result.scalar_one_or_none()
    if job_ts is not None:
        return job_ts

    return job.created_at


async def reconcile_stuck_sub_jobs(
    session: AsyncSession,
    *,
    stale_after_seconds: int,
    before: datetime | None = None,
) -> int:
    """Finds sub_jobs in PENDING or GENERATING whose most recent activity
    (see `_last_activity_at`) is older than `stale_after_seconds`, marks
    them FAILED/INTERNAL, records a RECONCILIATION_SWEEP job_events row, and
    recomputes each affected parent job's status via the existing
    status_rollup logic — same transaction, same shape as every other
    sub-job transition in this codebase. Does not commit; the caller
    controls the transaction boundary, same as every other service function
    here.

    `stale_after_seconds` should be comfortably longer than
    `settings.WORKER_TASK_TIMEOUT_SECONDS` — this sweep is a backstop for
    jobs the timeout mechanism itself failed to catch (a worker OOM-killed
    outright leaves no task running to hit that timeout), not the primary
    mechanism.

    `before`, if given, additionally requires the sub-job's own job to have
    been created strictly before that cutoff — used only by the one-time
    legacy-orphan cleanup (scripts/reconcile_legacy_orphans.py) so it can't
    touch a sub-job that's legitimately in flight right now just because it
    happens to be stale by the same clock. The recurring beat sweep never
    passes this; staleness alone is sufficient for it.

    Returns the count reconciled.
    """
    now = datetime.now(UTC)
    threshold = now - timedelta(seconds=stale_after_seconds)

    result = await session.execute(select(SubJob).where(SubJob.status.in_(STUCK_STATUSES)))
    candidates = list(result.scalars().all())

    reconciled = 0
    for sub_job in candidates:
        job = await jobs_repo.get_by_id(session, sub_job.job_id)
        if job is None:
            continue
        if before is not None and job.created_at >= before:
            continue

        last_activity = await _last_activity_at(session, job, sub_job)
        if last_activity >= threshold:
            continue

        reconciled += await _reconcile_one(
            session, job, sub_job, stale_after_seconds, last_activity
        )

    return reconciled


async def _reconcile_one(
    session: AsyncSession,
    job: Job,
    sub_job: SubJob,
    stale_after_seconds: int,
    last_activity: datetime,
) -> int:
    from_status = sub_job.status
    sub_job.failure_class = FailureClass.INTERNAL
    sub_job.error_message = (
        "Reconciliation sweep: no activity for over "
        f"{stale_after_seconds}s. Marked failed as a stuck-job backstop, "
        "not a real provider failure."
    )
    sub_job.status = SubJobStatus.FAILED

    job_events_repo.record_event(
        session,
        job.id,
        "RECONCILIATION_SWEEP",
        sub_job_id=sub_job.id,
        from_status=from_status.value,
        to_status=SubJobStatus.FAILED.value,
        detail={
            "angle": sub_job.angle.value if sub_job.angle is not None else None,
            "stale_after_seconds": stale_after_seconds,
            "last_activity_at": last_activity.isoformat(),
        },
    )
    await recompute_parent_status(session, job)
    return 1
