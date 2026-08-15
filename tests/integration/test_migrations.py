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


async def _indexes(async_url: str, table: str) -> set[str]:
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn) -> set[str]:  # type: ignore[no-untyped-def]
                inspector: Inspector = inspect(sync_conn)
                return {ix["name"] for ix in inspector.get_indexes(table)}

            return await conn.run_sync(_inspect)
    finally:
        await engine.dispose()


def test_0006_adds_job_operation_and_makes_angle_nullable(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 15 Step 2 Checkpoint 2 — migration applies against a database
    holding a real angle job, every pre-existing row reads
    operation='ANGLE_GENERATION', and downgrade/upgrade round-trips."""
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
    command.upgrade(cfg, "0005")
    assert "operation" not in asyncio.run(_columns(async_url, "jobs"))

    async def _seed_real_angle_job(async_url: str) -> str:
        engine = create_async_engine(async_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO api_clients (id, name, key_prefix, key_hash)
                        VALUES ('11111111-1111-1111-1111-111111111111', 'test',
                                'testpfx', 'hash')
                        """
                    )
                )
                await conn.execute(
                    text(
                        """
                        INSERT INTO config_versions
                            (id, version_number, source_hash, payload, sync_status,
                             is_active, synced_at, activated_at)
                        VALUES ('22222222-2222-2222-2222-222222222222', 99,
                                'seed-hash', '{}'::jsonb, 'SUCCESS', true, now(), now())
                        """
                    )
                )
                result = await conn.execute(
                    text(
                        """
                        INSERT INTO jobs
                            (client_id, idempotency_key, payload_hash, category_code,
                             config_version_id, requested_angles)
                        VALUES ('11111111-1111-1111-1111-111111111111', 'idem-1', 'ph',
                                'RING', '22222222-2222-2222-2222-222222222222', 1)
                        RETURNING id
                        """
                    )
                )
                job_id = result.scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO sub_jobs (job_id, angle, source_type) "
                        "VALUES (:job_id, 'FRONT', 'UPLOADED')"
                    ),
                    {"job_id": job_id},
                )
                return str(job_id)
        finally:
            await engine.dispose()

    job_id = asyncio.run(_seed_real_angle_job(async_url))

    command.upgrade(cfg, "0006")
    assert "operation" in asyncio.run(_columns(async_url, "jobs"))
    assert {"ux_sub_jobs_job_angle", "ux_sub_jobs_job_single"} <= asyncio.run(
        _indexes(async_url, "sub_jobs")
    )

    async def _job_operation(async_url: str, job_id: str) -> str:
        engine = create_async_engine(async_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT operation FROM jobs WHERE id = :job_id"), {"job_id": job_id}
                )
                return str(result.scalar_one())
        finally:
            await engine.dispose()

    assert asyncio.run(_job_operation(async_url, job_id)) == "ANGLE_GENERATION"

    command.downgrade(cfg, "0005")
    assert "operation" not in asyncio.run(_columns(async_url, "jobs"))
    assert not (
        {"ux_sub_jobs_job_angle", "ux_sub_jobs_job_single"}
        & asyncio.run(_indexes(async_url, "sub_jobs"))
    )

    command.upgrade(cfg, "head")


def test_0006_duplicate_angle_in_one_job_still_raises_unique_violation(
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

    asyncio.run(_reset_schema())
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    async def _attempt() -> None:
        from sqlalchemy.exc import IntegrityError

        engine = create_async_engine(async_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO api_clients (id, name, key_prefix, key_hash)
                        VALUES ('33333333-3333-3333-3333-333333333333', 't', 'pfx2', 'h')
                        """
                    )
                )
                await conn.execute(
                    text(
                        """
                        INSERT INTO config_versions
                            (id, version_number, source_hash, payload, sync_status,
                             is_active, synced_at, activated_at)
                        VALUES ('44444444-4444-4444-4444-444444444444', 100, 'h2',
                                '{}'::jsonb, 'SUCCESS', true, now(), now())
                        """
                    )
                )
                result = await conn.execute(
                    text(
                        """
                        INSERT INTO jobs
                            (client_id, idempotency_key, payload_hash, category_code,
                             config_version_id, requested_angles)
                        VALUES ('33333333-3333-3333-3333-333333333333', 'idem-2', 'ph2',
                                'RING', '44444444-4444-4444-4444-444444444444', 2)
                        RETURNING id
                        """
                    )
                )
                job_id = result.scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO sub_jobs (job_id, angle, source_type) "
                        "VALUES (:job_id, 'FRONT', 'UPLOADED')"
                    ),
                    {"job_id": job_id},
                )
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO sub_jobs (job_id, angle, source_type) "
                                "VALUES (:job_id, 'FRONT', 'UPLOADED')"
                            ),
                            {"job_id": job_id},
                        )
        finally:
            await engine.dispose()

    asyncio.run(_attempt())


def test_0006_second_angle_less_sub_job_on_same_job_raises_unique_violation(
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

    asyncio.run(_reset_schema())
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    async def _attempt() -> None:
        from sqlalchemy.exc import IntegrityError

        engine = create_async_engine(async_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO api_clients (id, name, key_prefix, key_hash)
                        VALUES ('55555555-5555-5555-5555-555555555555', 't', 'pfx3', 'h')
                        """
                    )
                )
                await conn.execute(
                    text(
                        """
                        INSERT INTO config_versions
                            (id, version_number, source_hash, payload, sync_status,
                             is_active, synced_at, activated_at)
                        VALUES ('66666666-6666-6666-6666-666666666666', 101, 'h3',
                                '{}'::jsonb, 'SUCCESS', true, now(), now())
                        """
                    )
                )
                result = await conn.execute(
                    text(
                        """
                        INSERT INTO jobs
                            (client_id, idempotency_key, payload_hash, category_code,
                             config_version_id, requested_angles, operation)
                        VALUES ('55555555-5555-5555-5555-555555555555', 'idem-3', 'ph3',
                                'RING', '66666666-6666-6666-6666-666666666666', 1,
                                'BACKGROUND_REMOVAL')
                        RETURNING id
                        """
                    )
                )
                job_id = result.scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO sub_jobs (job_id, angle, source_type) "
                        "VALUES (:job_id, NULL, 'UPLOADED')"
                    ),
                    {"job_id": job_id},
                )
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO sub_jobs (job_id, angle, source_type) "
                                "VALUES (:job_id, NULL, 'UPLOADED')"
                            ),
                            {"job_id": job_id},
                        )
        finally:
            await engine.dispose()

    asyncio.run(_attempt())


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


def test_0007_seeds_operations_and_background_presets(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 15 Step 3 Checkpoint 3 — data migration seeds `operations` and
    `background_presets` into a new active config version; the previous row
    is untouched and deactivated."""
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
    command.upgrade(cfg, "0006")
    asyncio.run(_seed_active_config_version(async_url, "gemini-3.1-flash-image"))
    previous = asyncio.run(_active_config_version(async_url))
    assert previous["version_number"] == 1

    command.upgrade(cfg, "0007")
    active = asyncio.run(_active_config_version(async_url))
    assert active["version_number"] == 2  # previous row deactivated, not mutated
    payload = json.loads(active["payload_text"])  # type: ignore[arg-type]
    assert payload["global"]["operations"]["BACKGROUND_REMOVAL"]["enabled"] is True
    assert payload["global"]["operations"]["BACKGROUND_REPLACEMENT"]["enabled"] is True
    presets = payload["global"]["background_presets"]
    assert any(p["code"] == "STUDIO_WHITE" and p["is_active"] for p in presets)
    # model_version (seeded via _seed_active_config_version, carried through
    # migration 0005/0006's own no-op path) must survive untouched.
    assert payload["global"]["model_version"] == "gemini-3.1-flash-image"

    async def _previous_row_payload(async_url: str) -> dict[str, object]:
        engine = create_async_engine(async_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT payload::text AS payload_text, is_active "
                        "FROM config_versions WHERE version_number = 1"
                    )
                )
                row = result.mappings().first()
                assert row is not None
                return dict(row)
        finally:
            await engine.dispose()

    prev_row = asyncio.run(_previous_row_payload(async_url))
    assert prev_row["is_active"] is False
    prev_payload = json.loads(prev_row["payload_text"])  # type: ignore[arg-type]
    assert "operations" not in prev_payload["global"]  # untouched, not mutated

    command.downgrade(cfg, "0006")
    reverted = asyncio.run(_active_config_version(async_url))
    assert reverted["version_number"] == 1

    command.upgrade(cfg, "head")


def test_0007_is_a_noop_with_no_active_config_version(
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

    asyncio.run(_reset_schema())

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")  # must not raise


def test_0009_adds_and_removes_jobs_preset_code(
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

    asyncio.run(_reset_schema())

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0008")
    assert "preset_code" not in asyncio.run(_columns(async_url, "jobs"))

    command.upgrade(cfg, "0009")
    assert "preset_code" in asyncio.run(_columns(async_url, "jobs"))

    command.downgrade(cfg, "0008")
    assert "preset_code" not in asyncio.run(_columns(async_url, "jobs"))

    command.upgrade(cfg, "head")


def test_0010_seeds_background_qa_similarity_threshold(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 15 Step 5 — the subject-preservation gate's own threshold,
    seeded independently of (and expected higher than) qa_similarity_threshold.
    """
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
    command.upgrade(cfg, "0009")
    asyncio.run(_seed_active_config_version(async_url, "gemini-3.1-flash-image"))
    previous = asyncio.run(_active_config_version(async_url))
    assert previous["version_number"] == 1

    command.upgrade(cfg, "0010")
    active = asyncio.run(_active_config_version(async_url))
    assert active["version_number"] == 2
    payload = json.loads(active["payload_text"])  # type: ignore[arg-type]
    assert payload["global"]["background_qa_similarity_threshold"] == 0.92
    assert (
        payload["global"]["background_qa_similarity_threshold"] > 0.82
    )  # > qa_similarity_threshold

    command.downgrade(cfg, "0009")
    reverted = asyncio.run(_active_config_version(async_url))
    assert reverted["version_number"] == 1
    reverted_payload = json.loads(reverted["payload_text"])  # type: ignore[arg-type]
    assert "background_qa_similarity_threshold" not in reverted_payload["global"]

    command.upgrade(cfg, "head")


def test_0010_is_a_noop_with_no_active_config_version(
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

    asyncio.run(_reset_schema())

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")  # must not raise


async def _enum_values(async_url: str, enum_type: str) -> list[str]:
    engine = create_async_engine(async_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(f"SELECT unnest(enum_range(NULL::{enum_type}))"))
            return [row[0] for row in result.fetchall()]
    finally:
        await engine.dispose()


def test_0013_adds_match_operation_and_variant_index(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 18 Step 1 Checkpoint 1 — migration 0013 adds `MATCH` to
    `operation_t`, adds `sub_jobs.variant_index`, and swaps in the narrowed
    `ux_sub_jobs_job_single` plus the new `ux_sub_jobs_job_variant`."""
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
    command.upgrade(cfg, "0012")
    assert "variant_index" not in asyncio.run(_columns(async_url, "sub_jobs"))
    assert "MATCH" not in asyncio.run(_enum_values(async_url, "operation_t"))

    command.upgrade(cfg, "0013")
    assert "variant_index" in asyncio.run(_columns(async_url, "sub_jobs"))
    assert "MATCH" in asyncio.run(_enum_values(async_url, "operation_t"))
    indexes = asyncio.run(_indexes(async_url, "sub_jobs"))
    assert {"ux_sub_jobs_job_single", "ux_sub_jobs_job_variant", "ux_sub_jobs_job_angle"} <= indexes

    command.downgrade(cfg, "0012")
    assert "variant_index" not in asyncio.run(_columns(async_url, "sub_jobs"))
    # MATCH is deliberately NOT removed on downgrade — Postgres has no
    # ALTER TYPE ... DROP VALUE, and this project's convention (enums.py's
    # own docstring, docs/conventions.md) is that enum values are never
    # removed once shipped. See migration 0013's docstring.
    assert "MATCH" in asyncio.run(_enum_values(async_url, "operation_t"))

    command.upgrade(cfg, "head")


async def _insert_client_and_config(
    async_url: str, client_id: str, key_prefix: str, config_id: str, version_number: int
) -> None:
    engine = create_async_engine(async_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO api_clients (id, name, key_prefix, key_hash)
                    VALUES (:client_id, 't', :key_prefix, 'h')
                    """
                ),
                {"client_id": client_id, "key_prefix": key_prefix},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO config_versions
                        (id, version_number, source_hash, payload, sync_status,
                         is_active, synced_at, activated_at)
                    VALUES (:config_id, :version_number, :hash,
                            '{}'::jsonb, 'SUCCESS', true, now(), now())
                    """
                ),
                {
                    "config_id": config_id,
                    "version_number": version_number,
                    "hash": f"h{version_number}",
                },
            )
    finally:
        await engine.dispose()


def test_0013_two_variant_sub_jobs_same_job_succeed(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 18 Step 1 Checkpoint 1 — two angle-less sub-jobs with distinct
    variant_index values under the same job_id must both insert cleanly; the
    narrowed ux_sub_jobs_job_single no longer blocks this (the whole reason
    Step 1 exists — see phases/phase-18-match.md's "Reality check" section)."""
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
    command.upgrade(cfg, "head")

    client_id = "77777777-7777-7777-7777-777777777777"
    config_id = "88888888-8888-8888-8888-888888888888"
    asyncio.run(_insert_client_and_config(async_url, client_id, "pfx7", config_id, 200))

    async def _attempt() -> None:
        engine = create_async_engine(async_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    text(
                        """
                        INSERT INTO jobs
                            (client_id, idempotency_key, payload_hash, category_code,
                             config_version_id, requested_angles, operation)
                        VALUES (:client_id, 'idem-match-1', 'ph-match-1', 'EARRING',
                                :config_id, 2, 'MATCH')
                        RETURNING id
                        """
                    ),
                    {"client_id": client_id, "config_id": config_id},
                )
                job_id = result.scalar_one()

                # Both succeed: angle IS NULL, variant_index distinct.
                await conn.execute(
                    text(
                        "INSERT INTO sub_jobs (job_id, angle, variant_index, source_type) "
                        "VALUES (:job_id, NULL, 0, 'UPLOADED')"
                    ),
                    {"job_id": job_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO sub_jobs (job_id, angle, variant_index, source_type) "
                        "VALUES (:job_id, NULL, 1, 'UPLOADED')"
                    ),
                    {"job_id": job_id},
                )

                count = (
                    await conn.execute(
                        text("SELECT count(*) FROM sub_jobs WHERE job_id = :job_id"),
                        {"job_id": job_id},
                    )
                ).scalar_one()
                assert count == 2
        finally:
            await engine.dispose()

    asyncio.run(_attempt())


def test_0013_second_angle_less_variant_less_sub_job_still_raises_unique_violation(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 18 Step 1 Checkpoint 1 — the narrowed ux_sub_jobs_job_single
    still enforces "exactly one angle-less, variant-less sub-job per job",
    the invariant BACKGROUND_REMOVAL/BACKGROUND_REPLACEMENT depend on."""
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
    command.upgrade(cfg, "head")

    client_id = "99999999-9999-9999-9999-999999999999"
    config_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    asyncio.run(_insert_client_and_config(async_url, client_id, "pfx8", config_id, 201))

    async def _attempt() -> None:
        from sqlalchemy.exc import IntegrityError

        engine = create_async_engine(async_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    text(
                        """
                        INSERT INTO jobs
                            (client_id, idempotency_key, payload_hash, category_code,
                             config_version_id, requested_angles, operation)
                        VALUES (:client_id, 'idem-bg-1', 'ph-bg-1', 'RING',
                                :config_id, 1, 'BACKGROUND_REMOVAL')
                        RETURNING id
                        """
                    ),
                    {"client_id": client_id, "config_id": config_id},
                )
                job_id = result.scalar_one()

                await conn.execute(
                    text(
                        "INSERT INTO sub_jobs (job_id, angle, variant_index, source_type) "
                        "VALUES (:job_id, NULL, NULL, 'UPLOADED')"
                    ),
                    {"job_id": job_id},
                )
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO sub_jobs (job_id, angle, variant_index, source_type) "
                                "VALUES (:job_id, NULL, NULL, 'UPLOADED')"
                            ),
                            {"job_id": job_id},
                        )
        finally:
            await engine.dispose()

    asyncio.run(_attempt())


def test_0013_model_matches_live_schema_no_autogenerate_diff(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 18 Step 1 Checkpoint 1 — after `alembic upgrade head`, the live
    schema and `Base.metadata` (app/db/models) must agree exactly for the
    objects this migration touches: `sub_jobs.variant_index`,
    `ux_sub_jobs_job_single`, `ux_sub_jobs_job_variant`, and `operation_t`.

    No existing test in this project runs a full autogenerate diff (searched
    for "compare_metadata"/"autogenerate" across tests/ — nothing), so this
    doesn't reuse machinery that doesn't exist; it's a targeted Alembic
    `compare_metadata` run scoped by asserting there are zero diffs touching
    the objects this migration changed, rather than inventing a new
    project-wide schema-parity fixture out of scope for Step 1."""
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
    command.upgrade(cfg, "head")

    async def _diff() -> list[object]:
        from alembic.autogenerate import compare_metadata
        from alembic.runtime.migration import MigrationContext

        from app.db.models import Base

        engine = create_async_engine(async_url)
        try:
            async with engine.connect() as conn:

                def _compare(sync_conn):  # type: ignore[no-untyped-def]
                    mc = MigrationContext.configure(sync_conn)
                    return compare_metadata(mc, Base.metadata)

                return await conn.run_sync(_compare)
        finally:
            await engine.dispose()

    diffs = asyncio.run(_diff())
    # Full-schema diff is empty (verified manually against this migration
    # too), but assert precisely on the objects in scope for Step 1 so this
    # test's intent is legible even if something unrelated drifts later.
    relevant = [d for d in diffs if "sub_jobs" in repr(d) or "operation_t" in repr(d)]
    assert relevant == [], f"unexpected schema diff for Step 1 objects: {relevant}"
    assert diffs == [], f"unexpected schema diff (whole schema): {diffs}"


def test_0014_seeds_match_into_operations_alongside_background_ops(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 18 Step 2 Checkpoint 2 — migration 0014 inserts a new active
    config_versions row with `MATCH` merged into `payload.global.operations`
    alongside whatever `0007` already seeded there (BACKGROUND_REMOVAL/
    BACKGROUND_REPLACEMENT must survive, not be clobbered); the previous row
    is deactivated, not mutated."""
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
    # Upgrade through 0007 so `operations.BACKGROUND_REMOVAL`/
    # `BACKGROUND_REPLACEMENT` already exist before 0014 runs — the
    # realistic case this migration must merge into, not clobber.
    command.upgrade(cfg, "0006")
    asyncio.run(_seed_active_config_version(async_url, "gemini-3.1-flash-image"))
    command.upgrade(cfg, "0013")
    before = asyncio.run(_active_config_version(async_url))
    before_payload = json.loads(before["payload_text"])  # type: ignore[arg-type]
    assert before_payload["global"]["operations"]["BACKGROUND_REMOVAL"]["enabled"] is True
    assert "MATCH" not in before_payload["global"]["operations"]

    command.upgrade(cfg, "0014")
    active = asyncio.run(_active_config_version(async_url))
    assert active["version_number"] == before["version_number"] + 1  # new row, not mutated
    payload = json.loads(active["payload_text"])  # type: ignore[arg-type]
    match_config = payload["global"]["operations"]["MATCH"]
    assert match_config["enabled"] is True
    assert match_config["unit_cost_usd"] == 0.02
    assert "{target_category}" in match_config["prompt"]
    # BACKGROUND_REMOVAL/BACKGROUND_REPLACEMENT must survive the merge,
    # not be dropped by a naive setdefault("operations", NEW_OPERATIONS).
    assert payload["global"]["operations"]["BACKGROUND_REMOVAL"]["enabled"] is True
    assert payload["global"]["operations"]["BACKGROUND_REPLACEMENT"]["enabled"] is True
    assert payload["global"]["model_version"] == "gemini-3.1-flash-image"

    async def _row_payload(async_url: str, version_number: int) -> dict[str, object]:
        engine = create_async_engine(async_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT payload::text AS payload_text, is_active "
                        "FROM config_versions WHERE version_number = :v"
                    ),
                    {"v": version_number},
                )
                row = result.mappings().first()
                assert row is not None
                return dict(row)
        finally:
            await engine.dispose()

    prev_row = asyncio.run(_row_payload(async_url, before["version_number"]))
    assert prev_row["is_active"] is False
    prev_payload = json.loads(prev_row["payload_text"])  # type: ignore[arg-type]
    assert "MATCH" not in prev_payload["global"]["operations"]  # untouched, not mutated

    command.downgrade(cfg, "0013")
    reverted = asyncio.run(_active_config_version(async_url))
    assert reverted["version_number"] == before["version_number"]

    command.upgrade(cfg, "head")


def test_0014_is_idempotent_when_match_already_seeded(
    postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running 0014 against a database where MATCH is already present in
    `operations` (not just where `operations` exists at all — that's the
    part `0007`'s own "already extended" guard doesn't have to distinguish,
    since it was the first migration to touch the key) must no-op: no new
    config_versions row, same active version_number."""
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
    command.upgrade(cfg, "0006")
    asyncio.run(_seed_active_config_version(async_url, "gemini-3.1-flash-image"))
    command.upgrade(cfg, "head")  # runs 0014 for the first time

    first_active = asyncio.run(_active_config_version(async_url))

    # Re-run 0014's upgrade() directly against the now-already-seeded
    # database — simulates a second application of the same migration
    # without going through Alembic's full command/version-tracking
    # machinery (which would refuse to re-run an already-stamped revision).
    # The module filename starts with a digit, so it isn't a valid `import`
    # target; load it the same way Alembic itself loads versioned scripts.
    # The migration only ever calls `op.get_bind()` (all its actual work is
    # plain `bind.execute(sa.text(...))`, no schema DDL), so a minimal stub
    # standing in for `alembic.op` is enough — no need to wire up a real
    # Alembic Operations/MigrationContext proxy for this.
    import importlib.util
    from unittest.mock import patch

    module_path = "migrations/versions/0014_add_match_config.py"
    spec = importlib.util.spec_from_file_location("match_config_migration_0014", module_path)
    assert spec is not None and spec.loader is not None
    match_config_migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(match_config_migration)

    async def _rerun_upgrade() -> None:
        engine = create_async_engine(async_url)
        try:
            async with engine.begin() as conn:

                def _run(sync_conn):  # type: ignore[no-untyped-def]
                    class _StubOp:
                        @staticmethod
                        def get_bind():  # type: ignore[no-untyped-def]
                            return sync_conn

                    with patch.object(match_config_migration, "op", _StubOp):
                        match_config_migration.upgrade()

                await conn.run_sync(_run)
        finally:
            await engine.dispose()

    asyncio.run(_rerun_upgrade())

    second_active = asyncio.run(_active_config_version(async_url))
    assert second_active["version_number"] == first_active["version_number"]  # no-op, no new row

    command.upgrade(cfg, "head")


def test_0014_is_a_noop_with_no_active_config_version(
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

    asyncio.run(_reset_schema())

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")  # must not raise
