"""Celery task: `generation.transform_photo`. Session/transaction lifecycle
only — the actual generation logic lives in
`app/services/generation_service.py` so it can be tested directly against
testcontainers Postgres + real Redis, without going through Celery (same
split as `app/workers/retention.py` in Phase 4).

Now dispatched for real by `orchestration.fan_out_job` (Phase 7) — see
`app/workers/_async_utils.py` for why this can't be a plain `asyncio.run()`.
Builds its own engine per call from `settings.DATABASE_URL` (read live, not
the shared `app.db.session.async_session_factory` bound at import time) so
that a cross-thread invocation (the `run_async` fallback path) never reuses
connections checked out on a different thread's event loop — asyncpg
connections are not shareable across loops. Tests redirect this by
monkeypatching `settings.DATABASE_URL` (see tests/conftest.py's `db_session`
fixture), same pattern as `tests/integration/test_migrations.py`.

Phase 9: if the committed sub-job landed in QA_REVIEW (a successful
synthetic-angle generation), dispatches `qa.score_similarity` after the
commit — same dispatch-after-commit placement `orchestration.fan_out_job`
uses for this exact task, so a QA dispatch never reads a sub-job row before
its own creating transaction has landed.
"""

import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.redis_client import get_redis_client
from app.db.models.enums import SubJobStatus
from app.services.generation_service import transform_photo
from app.workers._async_utils import run_async
from app.workers.celery_app import celery_app


async def _run(sub_job_id: str) -> str:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sub_job = await transform_photo(session, get_redis_client(), uuid.UUID(sub_job_id))
            await session.commit()
            return sub_job.status.value
    finally:
        await engine.dispose()


@celery_app.task(name="generation.transform_photo")  # type: ignore[untyped-decorator]
def transform_photo_task(sub_job_id: str) -> str:
    status = run_async(_run(sub_job_id))
    if status == SubJobStatus.QA_REVIEW.value:
        from app.workers.qa import score_similarity

        score_similarity.delay(sub_job_id)
    return status
