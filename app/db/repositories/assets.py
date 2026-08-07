"""All queries against `assets`. See docs/conventions.md."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.assets import Asset
from app.db.models.enums import AssetKind


async def get_by_id(session: AsyncSession, asset_id: uuid.UUID) -> Asset | None:
    result = await session.execute(select(Asset).where(Asset.id == asset_id))
    return result.scalar_one_or_none()


def create_asset(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    kind: AssetKind,
    bucket: str,
    storage_path: str,
    mime_type: str = "image/jpeg",
    sub_job_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> Asset:
    """Adds and returns a new Asset row. Does not commit — caller controls
    the transaction boundary."""
    asset = Asset(
        job_id=job_id,
        sub_job_id=sub_job_id,
        kind=kind,
        bucket=bucket,
        storage_path=storage_path,
        mime_type=mime_type,
        expires_at=expires_at,
    )
    session.add(asset)
    return asset
