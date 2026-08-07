"""All queries against `config_versions`. See docs/conventions.md."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.config_versions import ConfigVersion


async def get_active(session: AsyncSession) -> ConfigVersion | None:
    result = await session.execute(select(ConfigVersion).where(ConfigVersion.is_active))
    return result.scalar_one_or_none()
