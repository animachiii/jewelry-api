"""Phase 3 — GET /api/v2/config Redis caching, POST /internal/config/sync, and
the config_sync_service pipeline against real local Redis + testcontainers
Postgres. See phases/phase-3-config-service.md.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import fakeredis.aioredis
import pytest
import redis.asyncio as real_redis
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.redis_client import get_redis
from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import SyncStatus
from app.db.session import get_db
from app.main import app
from app.providers.sheets import SheetRows
from app.services.config_service import CACHE_KEY, CACHE_TTL_SECONDS
from app.services.config_sync_service import ConfigValidationError, sync_config
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()

_VALID_ANGLE_ROWS = [
    ["RING", "Rings", "TRUE", "FRONT", "TRUE", "FALSE", "Front view", "", ""],
    ["RING", "Rings", "TRUE", "SIDE", "TRUE", "FALSE", "Side view", "", ""],
    ["RING", "Rings", "TRUE", "DIAGONAL", "TRUE", "TRUE", "Diagonal view", "", ""],
    ["RING", "Rings", "TRUE", "TOP", "FALSE", "FALSE", "", "", ""],
]
_VALID_GLOBAL_ROWS = [
    ["model_version", "gemini-2.5-flash-image-preview"],
    ["qa_similarity_threshold", "0.9"],
    ["default_negative_prompt", "blurry"],
]


async def _make_client(session: AsyncSession, name: str, scope: str) -> tuple[ApiClient, str]:
    raw = f"raw-{uuid.uuid4()}"
    api_client = ApiClient(
        name=name, key_prefix=raw[:8], key_hash=_hasher.hash(raw), scope=scope, is_active=True
    )
    session.add(api_client)
    await session.flush()
    return api_client, raw


@pytest.fixture
async def real_redis_client() -> AsyncGenerator[real_redis.Redis, None]:
    """Real local Redis (not fakeredis) — checkpoint verification needs real
    cache hit/miss/invalidation/TTL behavior, per phases/phase-3-config-service.md.
    """
    client = real_redis.from_url(settings.REDIS_URL, decode_responses=True)  # type: ignore[no-untyped-call]
    await client.delete(CACHE_KEY)
    yield client
    await client.delete(CACHE_KEY)
    await client.aclose()


@pytest.fixture
async def active_config(db_session: AsyncSession) -> ConfigVersion:
    cv = ConfigVersion(
        version_number=1,
        source_hash="seed-hash-1",
        payload=CATEGORY_PAYLOAD,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    return cv


@pytest.fixture
async def client(
    db_session: AsyncSession, real_redis_client: real_redis.Redis
) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_get_redis() -> AsyncGenerator[real_redis.Redis, None]:
        yield real_redis_client

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = _override_get_redis
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def client_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "config-test-client", "client")
    await db_session.commit()
    return raw


@pytest.fixture
async def ops_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "config-test-ops", "ops")
    await db_session.commit()
    return raw


# --- GET /config caching ---------------------------------------------------


async def test_cache_miss_reads_postgres_and_populates_cache(
    client: AsyncClient,
    client_key: str,
    real_redis_client: real_redis.Redis,
    active_config: ConfigVersion,
) -> None:
    assert await real_redis_client.get(CACHE_KEY) is None

    resp = await client.get("/api/v2/config", headers={"X-API-Key": client_key})

    assert resp.status_code == 200, resp.text
    assert resp.json()["config_version"] == active_config.version_number

    cached = await real_redis_client.get(CACHE_KEY)
    assert cached is not None
    ttl = await real_redis_client.ttl(CACHE_KEY)
    assert 0 < ttl <= CACHE_TTL_SECONDS


async def test_cache_hit_serves_cached_payload_without_matching_postgres(
    client: AsyncClient,
    client_key: str,
    real_redis_client: real_redis.Redis,
    active_config: ConfigVersion,
) -> None:
    """Prove the cache is actually consulted first: seed a *different*
    `config_version` directly into Redis and confirm the response reflects
    the cache, not the real active Postgres row.
    """
    stale_payload = (
        '{"config_version": 999, "categories": '
        '[{"code": "RING", "name": "Rings", "is_active": true, '
        '"angles": {"FRONT": {"enabled": true, "synthetic_allowed": false}, '
        '"SIDE": {"enabled": true, "synthetic_allowed": false}, '
        '"DIAGONAL": {"enabled": true, "synthetic_allowed": true}, '
        '"TOP": {"enabled": false, "synthetic_allowed": false}}}]}'
    )
    await real_redis_client.set(CACHE_KEY, stale_payload, ex=CACHE_TTL_SECONDS)

    resp = await client.get("/api/v2/config", headers={"X-API-Key": client_key})

    assert resp.status_code == 200
    assert resp.json()["config_version"] == 999
    assert resp.json()["config_version"] != active_config.version_number


async def test_config_never_exposes_prompts_or_reference_urls(
    client: AsyncClient, client_key: str, active_config: ConfigVersion
) -> None:
    """Phase 15 Step 3 — CATEGORY_PAYLOAD (scripts/seed_dev.py) now includes
    a STUDIO_WHITE background preset with a real `prompt` field, so this
    extends the existing angle-prompt leak check to cover preset prompts
    too, the exact case the phase file's Checkpoint 3 asks for.
    """
    resp = await client.get("/api/v2/config", headers={"X-API-Key": client_key})
    body = resp.text
    assert "prompt" not in body
    assert "reference_image_urls" not in body


async def test_config_exposes_active_preset_code_and_name_only(
    client: AsyncClient, client_key: str, active_config: ConfigVersion
) -> None:
    resp = await client.get("/api/v2/config", headers={"X-API-Key": client_key})
    assert resp.status_code == 200, resp.text
    presets = resp.json()["background_presets"]
    assert presets == [{"code": "STUDIO_WHITE", "name": "Studio White"}]


# --- POST /internal/config/sync ---------------------------------------------


async def test_sync_requires_ops_scope(client: AsyncClient, client_key: str) -> None:
    resp = await client.post("/api/v2/internal/config/sync", headers={"X-API-Key": client_key})
    assert resp.status_code == 403


async def test_sync_falls_back_to_active_version_when_sheets_unconfigured(
    client: AsyncClient, ops_key: str, active_config: ConfigVersion, db_session: AsyncSession
) -> None:
    """No real Sheets project is configured (GOOGLE_SERVICE_ACCOUNT_JSON /
    CONFIG_SHEET_ID empty — see phases/phase-3-config-service.md). The sync
    endpoint must not fail; it must fall back to the active version per
    docs/business-rules.md §9.
    """
    resp = await client.post("/api/v2/internal/config/sync", headers={"X-API-Key": ops_key})

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["config_version"] == active_config.version_number
    assert body["activated"] is True

    result = await db_session.execute(select(ConfigVersion))
    versions = result.scalars().all()
    assert len(versions) == 1, "no new row should be written on a Sheets outage"


async def test_sync_invalidates_cache_on_new_version(
    db_session: AsyncSession, real_redis_client: real_redis.Redis, active_config: ConfigVersion
) -> None:
    await real_redis_client.set(CACHE_KEY, '{"config_version": 1, "categories": []}', ex=900)

    new_rows = SheetRows(_VALID_ANGLE_ROWS, _VALID_GLOBAL_ROWS)
    version = await sync_config(db_session, real_redis_client, fetch_rows=lambda: new_rows)

    assert version.version_number == active_config.version_number + 1
    assert version.is_active is True
    assert await real_redis_client.get(CACHE_KEY) is None


async def test_sync_unchanged_hash_creates_no_new_version(
    db_session: AsyncSession, real_redis_client: real_redis.Redis
) -> None:
    rows = SheetRows(_VALID_ANGLE_ROWS, _VALID_GLOBAL_ROWS)
    first = await sync_config(db_session, real_redis_client, fetch_rows=lambda: rows)
    second = await sync_config(db_session, real_redis_client, fetch_rows=lambda: rows)

    assert first.id == second.id
    result = await db_session.execute(select(ConfigVersion))
    assert len(result.scalars().all()) == 1


async def test_sync_changed_hash_creates_new_version_and_deactivates_old(
    db_session: AsyncSession, real_redis_client: real_redis.Redis
) -> None:
    rows_v1 = SheetRows(_VALID_ANGLE_ROWS, _VALID_GLOBAL_ROWS)
    rows_v2 = SheetRows(
        _VALID_ANGLE_ROWS,
        [*_VALID_GLOBAL_ROWS[:1], ["qa_similarity_threshold", "0.5"], _VALID_GLOBAL_ROWS[2]],
    )

    v1 = await sync_config(db_session, real_redis_client, fetch_rows=lambda: rows_v1)
    v2 = await sync_config(db_session, real_redis_client, fetch_rows=lambda: rows_v2)

    assert v2.id != v1.id
    assert v2.version_number == v1.version_number + 1
    assert v2.is_active is True

    await db_session.refresh(v1)
    assert v1.is_active is False

    result = await db_session.execute(select(ConfigVersion))
    assert len(result.scalars().all()) == 2


async def test_sync_only_one_active_row_at_a_time(
    db_session: AsyncSession, real_redis_client: real_redis.Redis
) -> None:
    rows_v1 = SheetRows(_VALID_ANGLE_ROWS, _VALID_GLOBAL_ROWS)
    rows_v2 = SheetRows(
        _VALID_ANGLE_ROWS,
        [*_VALID_GLOBAL_ROWS[:1], ["qa_similarity_threshold", "0.6"], _VALID_GLOBAL_ROWS[2]],
    )
    await sync_config(db_session, real_redis_client, fetch_rows=lambda: rows_v1)
    await sync_config(db_session, real_redis_client, fetch_rows=lambda: rows_v2)

    result = await db_session.execute(select(ConfigVersion).where(ConfigVersion.is_active))
    active_rows = result.scalars().all()
    assert len(active_rows) == 1


async def test_sync_validation_failure_records_failed_row_and_keeps_previous_active(
    db_session: AsyncSession, real_redis_client: real_redis.Redis, active_config: ConfigVersion
) -> None:
    bad_rows = SheetRows([["RING", "Rings", "TRUE", "BOTTOM", "TRUE", "FALSE", "x", "", ""]], [])

    result = await sync_config(db_session, real_redis_client, fetch_rows=lambda: bad_rows)

    assert result.id == active_config.id, "previous version stays active on validation failure"

    rows = await db_session.execute(
        select(ConfigVersion).where(ConfigVersion.sync_status == SyncStatus.FAILED)
    )
    failed = rows.scalars().one()
    assert failed.is_active is False
    assert failed.error_message is not None

    active_result = await db_session.execute(select(ConfigVersion).where(ConfigVersion.is_active))
    assert active_result.scalar_one().id == active_config.id


def test_normalize_raises_config_validation_error_is_a_value_error() -> None:
    assert issubclass(ConfigValidationError, ValueError)


# --- fakeredis smoke test (dev-dependency path also exercised) -------------


async def test_config_cache_helpers_work_with_fakeredis() -> None:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    from app.services.config_service import (  # noqa: PLC0415
        _read_cache,
        _write_cache,
        build_config_response,
        invalidate_cache,
    )

    cv = ConfigVersion(
        id=uuid.uuid4(),
        version_number=7,
        source_hash="x",
        payload=CATEGORY_PAYLOAD,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
    )
    response = build_config_response(cv)

    assert await _read_cache(fake) is None
    await _write_cache(fake, response)
    cached = await _read_cache(fake)
    assert cached is not None
    assert cached.config_version == 7

    await invalidate_cache(fake)
    assert await _read_cache(fake) is None
    await fake.aclose()
