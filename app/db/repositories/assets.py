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
    width_px: int | None = None,
    height_px: int | None = None,
    bytes_: int | None = None,
    checksum_sha256: str | None = None,
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
        width_px=width_px,
        height_px=height_px,
        bytes=bytes_,
        checksum_sha256=checksum_sha256,
    )
    session.add(asset)
    return asset


async def get_expired_unpurged(
    session: AsyncSession, *, now: datetime, limit: int = 500
) -> list[Asset]:
    """Assets whose retention deadline has passed and whose bytes have not
    yet been removed from storage. Never returns rows without `expires_at`
    (indefinite retention) — see app/services/retention_policy.py.
    """
    result = await session.execute(
        select(Asset)
        .where(
            Asset.expires_at.is_not(None),
            Asset.expires_at <= now,
            Asset.purged_at.is_(None),
        )
        .order_by(Asset.expires_at)
        .limit(limit)
    )
    return list(result.scalars().all())


def mark_purged(asset: Asset, when: datetime) -> None:
    """Marks bytes as removed. Does not commit and never touches any other
    column — the row itself is never deleted (CLAUDE.md Hard Rule 10)."""
    asset.purged_at = when
