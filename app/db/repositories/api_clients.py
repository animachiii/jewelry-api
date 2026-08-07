"""All queries against `api_clients`. See docs/conventions.md — routes and
dependencies never build a `select()` directly.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient


async def get_by_key_prefix(session: AsyncSession, key_prefix: str) -> ApiClient | None:
    result = await session.execute(select(ApiClient).where(ApiClient.key_prefix == key_prefix))
    return result.scalar_one_or_none()
