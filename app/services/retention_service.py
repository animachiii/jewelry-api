"""Asset retention/expiry lifecycle (Phase 4). See docs/business-rules.md §11.

Owns the business logic; `app/workers/retention.py` owns the Celery task
wrapper and the session lifecycle, per docs/conventions.md layering
(workers call services, services own the transaction, repositories own the
query). Kept here rather than inline in the worker so it can be exercised
directly against testcontainers Postgres in tests without a Celery/session
factory dependency.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import assets as assets_repo
from app.services import storage_service


async def expire_assets(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 500
) -> int:
    """Removes Storage bytes for every asset past its `expires_at` deadline
    and marks `purged_at`. Never deletes the row — CLAUDE.md Hard Rule 10.
    Storage delete is treated as idempotent: Supabase's remove() does not
    raise on an already-missing object, so a crash between delete and
    commit is safely re-attempted on the next sweep.
    """
    now = now if now is not None else datetime.now(UTC)
    expired = await assets_repo.get_expired_unpurged(session, now=now, limit=limit)
    for asset in expired:
        storage_service.delete(asset.bucket, asset.storage_path)
        assets_repo.mark_purged(asset, now)
    await session.commit()
    return len(expired)
