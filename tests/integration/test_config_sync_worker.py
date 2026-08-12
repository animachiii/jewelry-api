"""Regression test for the config.sync worker task's cross-event-loop crash
found live in production on 2026-08-12 (see app/workers/config.py's module
docstring). Calls the actual Celery task callable directly, not just
config_sync_service.sync_config, and calls it twice in the same test to
reproduce the repeated-beat-tick scenario that crashed the old
shared-session-factory + bare-asyncio.run() implementation with
RuntimeError("... attached to a different loop").

Real testcontainers Postgres, real local Redis — same pattern as
tests/integration/test_generation_worker.py. Google Sheets is never
configured in this environment (no GOOGLE_SERVICE_ACCOUNT_JSON/
CONFIG_SHEET_ID), so sync_config takes its documented SheetsUnavailable
fallback path and returns the seeded active version unchanged — this test
is about the task wrapper's event-loop/engine handling, not sync_config's
own logic (already covered by tests/unit/test_config_sync_service.py).
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import redis.asyncio as real_redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import SyncStatus
from app.workers import config as config_worker

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_client() -> AsyncGenerator[real_redis.Redis, None]:
    client = real_redis.from_url(settings.REDIS_URL, decode_responses=True)  # type: ignore[no-untyped-call]
    yield client
    await client.aclose()


async def _seed_active_config(db_session: AsyncSession) -> None:
    cv = ConfigVersion(
        version_number=1,
        source_hash="test-hash",
        payload={"categories": [], "global": {"model_version": "gemini-3.1-flash-image"}},
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()


async def test_sync_task_survives_repeated_ticks_without_cross_loop_crash(
    db_session: AsyncSession, redis_client: real_redis.Redis
) -> None:
    await _seed_active_config(db_session)

    first = config_worker.sync()
    assert first == "version=1 status=SUCCESS"

    # The production crash only ever surfaced on the second-and-later tick —
    # the first call's fresh engine had nothing stale to collide with.
    second = config_worker.sync()
    assert second == "version=1 status=SUCCESS"


def test_sync_task_never_imports_google_genai_at_module_level() -> None:
    import app.workers.config as module

    assert not hasattr(module, "genai")
