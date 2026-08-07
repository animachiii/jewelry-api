"""Celery task: `generation.transform_photo`. Session/transaction lifecycle
only — the actual generation logic lives in
`app/services/generation_service.py` so it can be tested directly against
testcontainers Postgres + real Redis, without going through Celery (same
split as `app/workers/retention.py` in Phase 4).

Not called by anything yet — no orchestrator fans work out to this task
until Phase 7. Calling it directly against a seeded sub_job_id is the
correct way to exercise it in this phase.
"""

import asyncio
import uuid

from app.core.redis_client import get_redis_client
from app.db.session import async_session_factory
from app.services.generation_service import transform_photo
from app.workers.celery_app import celery_app


async def _run(sub_job_id: str) -> str:
    async with async_session_factory() as session:
        sub_job = await transform_photo(session, get_redis_client(), uuid.UUID(sub_job_id))
        await session.commit()
        return sub_job.status.value


@celery_app.task(name="generation.transform_photo")  # type: ignore[untyped-decorator]
def transform_photo_task(sub_job_id: str) -> str:
    return asyncio.run(_run(sub_job_id))
