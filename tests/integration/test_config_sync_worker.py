"""Regression tests for the config.sync worker task, covering two distinct
production incidents on 2026-08-12 (see app/workers/config.py and
app/core/redis_client.py::new_redis_client for the full write-ups).

**Plain `def`, not `async def` — this matters.** `_async_utils.run_async`
branches on whether a loop is already running: under an async test it routes
onto the persistent background loop, which is *not* the production path and
hides both bugs below. A Celery prefork worker has no running loop, so it
takes `asyncio.run()`, which closes the loop when each task finishes. Only a
sync test reproduces that. (Same reasoning as
tests/integration/test_migrations.py's own "plain def" note.)

Both tests call the task **twice**, because both bugs are invisible on the
first call and only bite on the second — exactly how they presented live: the
first angle of a job succeeded and every subsequent one failed.

Google Sheets is never configured in this environment, so sync_config takes
its documented SheetsUnavailable fallback and returns the seeded active
version unchanged — these tests are about the task wrapper's event-loop,
engine and Redis-client lifecycle, not sync_config's own logic (covered by
tests/unit/test_config_sync_service.py).
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from app.db.models import Base
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import SyncStatus
from app.workers import config as config_worker

pytestmark = pytest.mark.integration


def _reset_schema(async_url: str) -> None:
    async def _run() -> None:
        engine = create_async_engine(async_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP SCHEMA public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _prepare_database(async_url: str) -> None:
    async def _setup() -> None:
        engine = create_async_engine(async_url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add(
                    ConfigVersion(
                        version_number=1,
                        source_hash="test-hash",
                        payload={
                            "categories": [],
                            "global": {"model_version": "gemini-3.1-flash-image"},
                        },
                        sync_status=SyncStatus.SUCCESS,
                        is_active=True,
                        activated_at=datetime.now(UTC),
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    _reset_schema(async_url)
    asyncio.run(_setup())


@pytest.fixture
def seeded_database(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """Seeds a clean schema and — crucially — tears it back down.

    `postgres_container` is session-scoped and shared with every other
    integration test, and this module manages its own schema rather than
    going through the per-test `db_engine` fixture (it needs a plain `def`
    test, which cannot use async fixtures). Leaving the seeded
    `config_versions` row behind made `test_cost_service.py` fail on a
    duplicate `version_number=1` — so this resets the schema on the way out
    as well as on the way in.
    """
    async_url = postgres_container.get_connection_url()
    monkeypatch.setattr("app.config.settings.DATABASE_URL", async_url)
    _prepare_database(async_url)
    try:
        yield async_url
    finally:
        _reset_schema(async_url)


def test_sync_task_survives_repeated_ticks_in_one_process(seeded_database: str) -> None:
    """Guards the shared-import-time-session-factory regression: reverting
    `_run_sync` to `app.db.session.async_session_factory` fails this test
    (verified locally). Note that factory ignores the monkeypatched
    `DATABASE_URL` entirely -- it binds at import — which is exactly why the
    old code reached the *production* database from a test run.

    Redis's own closed-loop failure is covered separately in
    test_worker_event_loop_lifecycle.py: with Sheets unconfigured,
    `sync_config` returns before ever touching Redis, so this test cannot
    reach it.
    """
    first = config_worker.sync()
    assert first == "version=1 status=SUCCESS"

    second = config_worker.sync()
    assert second == "version=1 status=SUCCESS"

    third = config_worker.sync()
    assert third == "version=1 status=SUCCESS"


def test_sync_task_never_imports_google_genai_at_module_level() -> None:
    import app.workers.config as module

    assert not hasattr(module, "genai")
