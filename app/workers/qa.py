"""Celery task: `qa.score_similarity`. Session/transaction lifecycle only —
the actual scoring logic lives in app/services/qa_service.py, same split as
every worker task since Phase 4.

Dispatched by app/workers/generation.py after a QA_REVIEW-landing
transform_photo call commits — see phases/phase-9-qa-gate.md Step 2 for why
dispatch lives there and not inside the service function itself. Builds its
own engine per call from settings.DATABASE_URL, same cross-loop reasoning as
every other worker task since Phase 7 (app/workers/_async_utils.py).
"""

import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services.qa_service import score_synthetic_angle
from app.workers._async_utils import run_async
from app.workers.celery_app import celery_app


async def _run(sub_job_id: str) -> str:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sub_job = await score_synthetic_angle(session, uuid.UUID(sub_job_id))
            await session.commit()
            return sub_job.status.value
    finally:
        await engine.dispose()


@celery_app.task(name="qa.score_similarity")  # type: ignore[untyped-decorator]
def score_similarity(sub_job_id: str) -> str:
    return run_async(_run(sub_job_id))
