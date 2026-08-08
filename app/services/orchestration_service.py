"""Fan-out dispatch. See phases/phase-7-orchestration.md.

No Celery group/chord — see that phase file's reality-check section for why:
docs/conventions.md requires parent-status recompute to happen in the same
transaction as the sub-job transition that triggered it, which a chord
(recomputing once, out-of-band, after the whole group finishes) doesn't
match. `app/services/generation_service.py::transform_photo` already
recomputes per-transition; this module's only job is dispatching one
`generation.transform_photo` call per eligible sub-job.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import JobStatus, SubJobStatus
from app.db.repositories import jobs as jobs_repo


async def dispatch_job(session: AsyncSession, job_id: uuid.UUID) -> list[uuid.UUID]:
    """Marks the job PROCESSING (idempotent — a job already past PENDING is
    left alone, so a duplicate dispatch trigger doesn't corrupt started_at or
    re-list sub-jobs that have already moved past PENDING) and returns the
    sub-job IDs the caller should dispatch `generation.transform_photo` for.
    Does not commit — caller controls the transaction boundary.
    """
    job = await jobs_repo.get_by_id(session, job_id)
    if job is None:
        return []

    sub_jobs = await jobs_repo.get_sub_jobs(session, job_id)
    eligible = [sj.id for sj in sub_jobs if sj.status == SubJobStatus.PENDING]

    if job.status == JobStatus.PENDING:
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(UTC)

    return eligible
