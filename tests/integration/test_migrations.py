"""Phase 2 Checkpoint 1 — migration 0003 applies and reverses cleanly.

Runs the real Alembic chain (0001 -> 0003) against a fresh testcontainers
Postgres — deliberately not the `db_engine` fixture, which creates schema via
`Base.metadata.create_all` and would collide with Alembic managing the same
tables. `migrations/env.py` always reads `app.config.settings.DATABASE_URL`
(not the Alembic Config object's `sqlalchemy.url`), so this test monkeypatches
that setting to point at the container instead.

Plain `def`, not `async def`: `migrations/env.py` calls `asyncio.run()`
internally, which raises if called from inside an already-running event loop
— so this test must not run under one.
"""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Inspector
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration


async def _columns(async_url: str, table: str) -> set[str]:
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn) -> set[str]:  # type: ignore[no-untyped-def]
                inspector: Inspector = inspect(sync_conn)
                return {c["name"] for c in inspector.get_columns(table)}

            return await conn.run_sync(_inspect)
    finally:
        await engine.dispose()


def test_0003_adds_and_removes_payload_hash(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async_url = postgres_container.get_connection_url()
    monkeypatch.setattr("app.config.settings.DATABASE_URL", async_url)

    async def _reset_schema() -> None:
        engine = create_async_engine(async_url)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    # Clean slate: drop anything a prior test left, including alembic's own
    # version table, so this run starts from a real "nothing migrated" state.
    asyncio.run(_reset_schema())

    cfg = Config("alembic.ini")

    command.upgrade(cfg, "0002")
    assert "payload_hash" not in asyncio.run(_columns(async_url, "jobs"))

    command.upgrade(cfg, "0003")
    assert "payload_hash" in asyncio.run(_columns(async_url, "jobs"))

    command.downgrade(cfg, "0002")
    assert "payload_hash" not in asyncio.run(_columns(async_url, "jobs"))

    command.upgrade(cfg, "head")
