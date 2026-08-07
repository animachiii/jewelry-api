"""All queries against `assets`. See docs/conventions.md."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.assets import Asset


async def get_by_id(session: AsyncSession, asset_id: uuid.UUID) -> Asset | None:
    result = await session.execute(select(Asset).where(Asset.id == asset_id))
    return result.scalar_one_or_none()
