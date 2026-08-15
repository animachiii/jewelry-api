"""One-time cleanup of pre-2026-08-13 orphaned sub-jobs (Phase 16 Step 2).

Live query on 2026-08-15 found sub-jobs stuck in PENDING/GENERATING from
before the 2026-08-13 18:16 UTC deploy that fixed the two root causes
(app/core/redis_client.py's event-loop bug, the missing
REDIS_SOCKET_TIMEOUT_SECONDS) — these predate both the fix and the ongoing
reconciliation sweep (app/workers/reconciliation.py), and nothing else will
ever clean them up. Reuses reconcile_stuck_sub_jobs unmodified: `before`
guarantees a job created on/after the cutoff (which could legitimately still
be in flight right now) is never touched, no matter how stale it looks by
this script's own clock; `stale_after_seconds=0` means "any" for the jobs
that do pass the cutoff — every one of them is already many hours to days
old at minimum.

Usage:
    python scripts/reconcile_legacy_orphans.py           # dry run, reports only
    python scripts/reconcile_legacy_orphans.py --apply    # actually reconciles

Runs its report/apply/verify steps inside a single `asyncio.run()` call, all
sharing one session — **not** one `asyncio.run()` per step. A first version
of this script called `asyncio.run()` twice against the shared
`app.db.session.async_session_factory` (report, then apply) and reproduced,
live, the exact `RuntimeError("Task ... got Future ... attached to a
different loop")` app/workers/config.py's docstring already documented as a
past production incident — a fresh event loop's connection reusing a pooled
connection object bound to the previous asyncio.run() call's now-closed
loop. One loop for the whole script sidesteps it entirely; a worker task
instead needs a fresh engine per call (see app/workers/reconciliation.py),
since each Celery invocation is its own asyncio.run().
"""

import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.jobs import Job, SubJob
from app.db.session import async_session_factory
from app.services import reconciliation_service

CUTOFF = datetime(2026, 8, 13, 18, 16, tzinfo=UTC)


async def _report(session: AsyncSession) -> list[tuple[SubJob, datetime]]:
    result = await session.execute(
        select(SubJob, Job.created_at)
        .join(Job, Job.id == SubJob.job_id)
        .where(
            SubJob.status.in_(reconciliation_service.STUCK_STATUSES),
            Job.created_at < CUTOFF,
        )
    )
    return list(result.all())


def _print_report(rows: list[tuple[SubJob, datetime]]) -> None:
    print(f"Found {len(rows)} pre-cutoff ({CUTOFF.isoformat()}) stuck sub-jobs:")
    for sub_job, job_created_at in rows:
        print(
            f"  sub_job={sub_job.id} status={sub_job.status.value} "
            f"angle={sub_job.angle.value if sub_job.angle else None} "
            f"job_created_at={job_created_at.isoformat()}"
        )


async def _main_async() -> None:
    apply = "--apply" in sys.argv

    async with async_session_factory() as session:
        rows = await _report(session)
        _print_report(rows)

        if not apply:
            print("\nDry run only — pass --apply to reconcile these for real.")
            return

        reconciled = await reconciliation_service.reconcile_stuck_sub_jobs(
            session, stale_after_seconds=0, before=CUTOFF
        )
        await session.commit()
        print(f"\nReconciled {reconciled} sub-jobs.")

        remaining = await _report(session)
        if remaining:
            print(
                f"WARNING: {len(remaining)} pre-cutoff sub-jobs still unreconciled — investigate."
            )
            sys.exit(1)
        print("All pre-cutoff orphans reconciled.")


if __name__ == "__main__":
    asyncio.run(_main_async())
