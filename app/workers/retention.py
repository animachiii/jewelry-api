"""Asset retention/expiry lifecycle Celery task (Phase 4).

Runs on the Celery beat schedule (see celery_app.py's `asset-retention`
entry). Session lifecycle only — the actual expiry logic lives in
app/services/retention_service.py so it can be tested directly against
testcontainers Postgres without going through Celery.

Phase 16: was `async_session_factory()` (bound at import time) + bare
`asyncio.run()` — the exact shape app/workers/config.py's own docstring
documents as having crashed every tick in production
(`RuntimeError("Task ... got Future ... attached to a different loop")`)
once its cron started firing repeatedly, because a connection checked out
under one asyncio.run() call's loop gets reused on the next call under a
new one. Found while building app/workers/reconciliation.py (Phase 16 Step
2, same beat-scheduled shape) rather than by inspection — retention's own
daily cron plus this container's frequent free-tier restarts (confirmed
live: celery worker restarts every 1-4 hours, see Render Events) meant this
had never yet survived to a second same-process tick to actually surface
it. Fixed to the same per-call engine + `run_async` pattern config.py
already established.
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services import retention_service
from app.workers._async_utils import run_async
from app.workers.celery_app import celery_app


async def _run() -> int:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            return await retention_service.expire_assets(session)
    finally:
        await engine.dispose()


@celery_app.task(name="retention.expire_assets")  # type: ignore[untyped-decorator]
def expire_assets() -> dict[str, int]:
    purged = run_async(_run())
    return {"purged": purged}
