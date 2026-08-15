"""Celery task: `orchestration.fan_out_job`. Session/transaction lifecycle
only — the actual dispatch logic lives in
`app/services/orchestration_service.py` so it can be tested directly against
testcontainers Postgres without going through Celery (same split as every
worker task since Phase 4).

No `celery.group`/`chord` — see phases/phase-7-orchestration.md's
reality-check section. Fan-out is a loop of independent
`generation.transform_photo` dispatches; parent-status recompute happens
per-transition inside that task, not once here after a group completes.

Builds its own engine per call from `settings.DATABASE_URL` — see
`app/workers/generation.py`'s module docstring for why (cross-thread
`run_async` invocations can't share a connection pool across event loops).
"""

import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.orchestration_service import dispatch_job
from app.workers._async_utils import run_async
from app.workers.celery_app import celery_app
from app.workers.generation import transform_photo_task


async def _run(job_id: str) -> list[str]:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sub_job_ids = await dispatch_job(session, uuid.UUID(job_id))
            await session.commit()
            return [str(sub_job_id) for sub_job_id in sub_job_ids]
    finally:
        await engine.dispose()


@celery_app.task(name="orchestration.fan_out_job")  # type: ignore[untyped-decorator]
def fan_out_job(job_id: str) -> list[str]:
    sub_job_ids = run_async(_run(job_id))
    for sub_job_id in sub_job_ids:
        transform_photo_task.delay(sub_job_id)
    return sub_job_ids


@celery_app.task(name="orchestration.fan_out_match_job")  # type: ignore[untyped-decorator]
def fan_out_match_job(job_id: str) -> list[str]:
    """MATCH's own fan-out task (phases/phase-18-match.md Step 4). Reuses
    `_run` (and the `dispatch_job` it wraps) unmodified — `dispatch_job` was
    already fully generic (it just marks the job PROCESSING and returns
    PENDING sub-job IDs, with nothing angle-specific in it), so no
    generalization was needed there. A separate Celery task rather than
    branching `fan_out_job` on `job.operation`: keeps each task's dispatch
    target explicit/typed at the Celery registration level, the same reason
    `background.process` and `generation.transform_photo` are already
    separate named tasks rather than one polymorphic task.
    """
    sub_job_ids = run_async(_run(job_id))
    from app.workers.match import process_task as match_process_task

    for sub_job_id in sub_job_ids:
        match_process_task.delay(sub_job_id)
    return sub_job_ids
