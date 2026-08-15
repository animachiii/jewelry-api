"""Celery beat task: `reconciliation.sweep_stuck_sub_jobs` (Phase 16 Step 2).

Session lifecycle only — the actual sweep logic lives in
app/services/reconciliation_service.py so it can be tested directly against
testcontainers Postgres, same split as app/workers/retention.py /
app/services/retention_service.py.

Fresh engine per call + `run_async`, not a shared import-time-bound
engine + bare `asyncio.run()` — see app/workers/retention.py's Phase 16
comment for the production incident (app/workers/config.py) that pattern
already caused elsewhere, and why a *frequent* beat task (this one fires
every RECONCILIATION_SWEEP_CRON, deliberately short — see
reconciliation_service.py's docstring) is exactly where it would resurface
fastest.
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services import reconciliation_service
from app.workers._async_utils import run_async
from app.workers.celery_app import celery_app


async def _run() -> int:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            reconciled = await reconciliation_service.reconcile_stuck_sub_jobs(
                session, stale_after_seconds=settings.RECONCILIATION_STALE_AFTER_SECONDS
            )
            await session.commit()
            return reconciled
    finally:
        await engine.dispose()


@celery_app.task(name="reconciliation.sweep_stuck_sub_jobs")  # type: ignore[untyped-decorator]
def sweep_stuck_sub_jobs() -> dict[str, int]:
    reconciled = run_async(_run())
    return {"reconciled": reconciled}
