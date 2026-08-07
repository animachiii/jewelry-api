"""All queries against `config_versions`. See docs/conventions.md."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import SyncStatus


async def get_active(session: AsyncSession) -> ConfigVersion | None:
    result = await session.execute(select(ConfigVersion).where(ConfigVersion.is_active))
    return result.scalar_one_or_none()


async def get_next_version_number(session: AsyncSession) -> int:
    """Monotonic version numbers — see docs/schema.md `version_number`."""
    result = await session.execute(select(func.max(ConfigVersion.version_number)))
    current_max = result.scalar_one_or_none()
    return (current_max or 0) + 1


async def create_version(
    session: AsyncSession,
    *,
    version_number: int,
    source_hash: str,
    payload: dict[str, Any],
    sync_status: SyncStatus,
    error_message: str | None = None,
) -> ConfigVersion:
    """Adds a new immutable row. Does not commit or activate — see
    docs/conventions.md on transaction boundaries living in the service layer.
    Never mutates an existing row (Hard Rule 11 in CLAUDE.md).
    """
    version = ConfigVersion(
        version_number=version_number,
        source_hash=source_hash,
        payload=payload,
        sync_status=sync_status,
        error_message=error_message,
        is_active=False,
    )
    session.add(version)
    await session.flush()
    return version


async def activate_version(session: AsyncSession, version: ConfigVersion) -> None:
    """Deactivates whatever is currently active and activates `version` — kept
    inside one transaction so the partial unique index
    (`config_versions.is_active WHERE is_active`) is never briefly violated
    across two committed statements.
    """
    current_active = await get_active(session)
    if current_active is not None and current_active.id != version.id:
        current_active.is_active = False
        await session.flush()
    version.is_active = True
    version.activated_at = datetime.now(UTC)
    await session.flush()
