"""One-time re-score of background-operation sub-jobs flagged by a broken judge.

Two separate QA bugs left a pile of `QA_REVIEW`/`FLAGGED` background sub-jobs
in the human review queue that were never actually judged on their merits:

1. **2026-08-13 -> 2026-08-21** — `GeminiQaProvider._parse_response` read a
   `parts[0]["json"]` key the real SDK never produces, so every real QA call
   raised `INTERNAL` and fail-open-flagged with `qa_score: NULL`. Fixed by
   commit `130f310`. Live count on 2026-08-28: **17 sub-jobs**, all with
   `qa_score IS NULL`, spanning two client_ids.
2. **through 2026-08-27** — background operations were scored by the
   synthetic-angle piece-identity prompt, which counts an intended pose/crop/
   background change as "a materially different piece". Fixed by introducing
   `SUBJECT_PRESERVATION_JUDGE_PROMPT`; see `app/providers/gemini_qa.py`.

Neither set will ever clear itself: nothing re-runs a judge on an already-
flagged sub-job, and a human working the queue would be adjudicating outputs
the system never had an opinion about.

**Deploy the judge fix before running this.** Re-scoring against the old
prompt reproduces the flags it is meant to clear.

Usage:
    python scripts/rescore_flagged_background.py                 # dry run
    python scripts/rescore_flagged_background.py --apply         # dispatch
    python scripts/rescore_flagged_background.py --apply --unscored-only

`--unscored-only` restricts to bug 1's population (`qa_score IS NULL`);
without it, every flagged background sub-job is re-scored, bug 2's included.

**Dispatches `qa.score_background` directly rather than going through
`POST /jobs/{job_id}/retry`.** Two reasons, both deliberate:

- That route is client-scoped, and the affected rows span two `client_id`s —
  clearing them through the API would need every affected client's own key.
- `execute_qa_retry` increments `attempt_count` against
  `MAX_RETRY_ATTEMPTS`. These rows were flagged by our bug, not by a bad
  output, and 6 of the 17 already sit at `attempt_count = 2`; spending a
  client's retry budget to clean up after a backend defect would leave them
  with one attempt left on an output nobody ever judged. This script
  therefore leaves `attempt_count` untouched.

It records a `QA_RESCORE_REQUESTED` `job_events` row per sub-job instead, so
the re-score is as auditable as a retry would have been — the audit trail is
the part worth keeping, not the counter.

Follows `scripts/reconcile_legacy_orphans.py`'s single-`asyncio.run()` shape
for the same reason its docstring gives: one event loop for the whole script,
so a pooled connection is never reused across loops.
"""

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import QAStatus, SubJobStatus
from app.db.models.jobs import Job, SubJob
from app.db.repositories import job_events as job_events_repo
from app.db.session import async_session_factory

BACKGROUND_OPERATIONS = ("BACKGROUND_REMOVAL", "BACKGROUND_REPLACEMENT")


async def _find(session: AsyncSession, unscored_only: bool) -> list[tuple[SubJob, Job]]:
    stmt = (
        select(SubJob, Job)
        .join(Job, Job.id == SubJob.job_id)
        .where(
            SubJob.status == SubJobStatus.QA_REVIEW,
            SubJob.qa_status == QAStatus.FLAGGED,
            SubJob.angle.is_(None),
            Job.operation.in_(BACKGROUND_OPERATIONS),
        )
        .order_by(Job.created_at)
    )
    if unscored_only:
        stmt = stmt.where(SubJob.qa_score.is_(None))
    return [(sub_job, job) for sub_job, job in (await session.execute(stmt)).all()]


def _print_report(rows: list[tuple[SubJob, Job]]) -> None:
    print(f"Found {len(rows)} flagged background sub-job(s) to re-score:")
    for sub_job, job in rows:
        score = "NULL" if sub_job.qa_score is None else f"{float(sub_job.qa_score):.3f}"
        print(
            f"  sub_job={sub_job.id} job={job.id} op={job.operation.value} "
            f"qa_score={score} attempt_count={sub_job.attempt_count} "
            f"created={job.created_at.isoformat()}"
        )


async def _main_async() -> None:
    apply = "--apply" in sys.argv
    unscored_only = "--unscored-only" in sys.argv

    async with async_session_factory() as session:
        rows = await _find(session, unscored_only)
        _print_report(rows)

        if not rows:
            return
        if not apply:
            print("\nDry run. Re-run with --apply to dispatch these re-scores.")
            return

        # Imported here, not at module scope: importing the worker module
        # builds the Celery app and its broker connection, which a dry run
        # has no business doing.
        from app.workers.qa import score_background

        for sub_job, job in rows:
            job_events_repo.record_event(
                session,
                job.id,
                "QA_RESCORE_REQUESTED",
                sub_job_id=sub_job.id,
                from_status=sub_job.status.value,
                to_status=sub_job.status.value,
                detail={
                    "angle": None,
                    "reason": "operator re-score after judge fix; attempt_count untouched",
                    "previous_qa_score": (
                        None if sub_job.qa_score is None else float(sub_job.qa_score)
                    ),
                },
            )
        await session.commit()

        for sub_job, _job in rows:
            score_background.delay(str(sub_job.id))
            print(f"  dispatched qa.score_background sub_job={sub_job.id}")

        print(
            f"\nDispatched {len(rows)} re-score(s). "
            "Each overwrites qa_score/qa_status with the fresh judge outcome; "
            "the ones that now pass leave the review queue on their own."
        )


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
