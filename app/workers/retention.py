"""Asset retention/expiry lifecycle Celery task (Phase 4).

Runs on the Celery beat schedule (see celery_app.py's `asset-retention`
entry). Session lifecycle only — the actual expiry logic lives in
app/services/retention_service.py so it can be tested directly against
testcontainers Postgres without going through Celery.
"""

import asyncio

from app.db.session import async_session_factory
from app.services import retention_service
from app.workers.celery_app import celery_app


async def _run() -> int:
    async with async_session_factory() as session:
        return await retention_service.expire_assets(session)


@celery_app.task(name="retention.expire_assets")  # type: ignore[untyped-decorator]
def expire_assets() -> dict[str, int]:
    purged = asyncio.run(_run())
    return {"purged": purged}
