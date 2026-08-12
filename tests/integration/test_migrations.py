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
import json

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


def test_0004_adds_and_removes_assets_purged_at(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 4 — assets.purged_at backs the retention worker's "already
    swept" check (app/services/retention_service.py)."""
    async_url = postgres_container.get_connection_url()
    monkeypatch.setattr("app.config.settings.DATABASE_URL", async_url)

    async def _reset_schema() -> None:
        engine = create_async_engine(async_url)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(_reset_schema())

    cfg = Config("alembic.ini")

    command.upgrade(cfg, "0003")
    assert "purged_at" not in asyncio.run(_columns(async_url, "assets"))

    command.upgrade(cfg, "0004")
    assert "purged_at" in asyncio.run(_columns(async_url, "assets"))

    command.downgrade(cfg, "0003")
    assert "purged_at" not in asyncio.run(_columns(async_url, "assets"))

    command.upgrade(cfg, "head")


async def _seed_active_config_version(async_url: str, model_version: str) -> None:
    engine = create_async_engine(async_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO config_versions
                        (id, version_number, source_hash, payload, sync_status,
                         is_active, synced_at, activated_at)
                    VALUES (gen_random_uuid(), 1, 'seed-hash',
                            CAST(:payload AS jsonb), 'SUCCESS', true, now(), now())
                    """
                ),
                {
                    "payload": json.dumps(
                        {"categories": [], "global": {"model_version": model_version}}
                    )
                },
            )
    finally:
        await engine.dispose()


async def _active_config_version(async_url: str) -> dict[str, object]:
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT version_number, payload::text AS payload_text "
                    "FROM config_versions WHERE is_active = true"
                )
            )
            row = result.mappings().first()
            assert row is not None
            return dict(row)
    finally:
        await engine.dispose()


def test_0005_fixes_gemini_model_version(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrects the 404-ing `gemini-2.5-flash-image-preview` model string
    without mutating the existing row (CLAUDE.md Hard Rule 11) — see the
    migration's own docstring for the live incident this fixes."""
    async_url = postgres_container.get_connection_url()
    monkeypatch.setattr("app.config.settings.DATABASE_URL", async_url)

    async def _reset_schema() -> None:
        engine = create_async_engine(async_url)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(_reset_schema())

    cfg = Config("alembic.ini")

    command.upgrade(cfg, "0004")
    asyncio.run(_seed_active_config_version(async_url, "gemini-2.5-flash-image-preview"))

    command.upgrade(cfg, "0005")
    active = asyncio.run(_active_config_version(async_url))
    assert active["version_number"] == 2
    payload = json.loads(active["payload_text"])  # type: ignore[arg-type]
    assert payload["global"]["model_version"] == "gemini-3.1-flash-image"

    command.downgrade(cfg, "0004")
    reverted = asyncio.run(_active_config_version(async_url))
    assert reverted["version_number"] == 1
    reverted_payload = json.loads(reverted["payload_text"])  # type: ignore[arg-type]
    assert reverted_payload["global"]["model_version"] == "gemini-2.5-flash-image-preview"

    command.upgrade(cfg, "head")


def test_0005_is_a_noop_with_no_active_config_version(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh/CI databases have no seeded config_versions row at all — the
    migration must not error just because production has data and CI
    doesn't."""
    async_url = postgres_container.get_connection_url()
    monkeypatch.setattr("app.config.settings.DATABASE_URL", async_url)

    async def _reset_schema() -> None:
        engine = create_async_engine(async_url)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(_reset_schema())

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")  # must not raise
