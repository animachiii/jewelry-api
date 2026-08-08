"""Phase 10 Checkpoint 1 — real per-client rate limiting and daily quota on
POST /generate. Real testcontainers Postgres, real local Redis (the
fixed-window counter genuinely lives there, not fakeredis — see
app/core/ratelimit.py), real Supabase Storage, fixture-driven Gemini
generation (tests/conftest.py's autouse fixture).
"""

import io
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import httpx
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import SyncStatus
from app.db.session import get_db
from app.main import app
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def active_config(db_session: AsyncSession) -> ConfigVersion:
    cv = ConfigVersion(
        version_number=1,
        source_hash="test-hash",
        payload=CATEGORY_PAYLOAD,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    return cv


async def _make_key(
    db_session: AsyncSession,
    *,
    rate_limit_per_min: int = 60,
    daily_job_quota: int | None = None,
) -> str:
    import secrets

    raw = secrets.token_urlsafe(32)
    api_client = ApiClient(
        name="rate-limit-test-client",
        key_prefix=raw[:8],
        key_hash=_hasher.hash(raw),
        scope="client",
        is_active=True,
        rate_limit_per_min=rate_limit_per_min,
        daily_job_quota=daily_job_quota,
    )
    db_session.add(api_client)
    await db_session.commit()
    return raw


def _real_jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color=(50, 50, 200)).save(buf, format="JPEG")
    return buf.getvalue()


async def _presign_and_upload(client: AsyncClient, key: str, angle: str) -> str:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": key},
        json={"category_code": "RING", "angles": [angle]},
    )
    assert resp.status_code == 200, resp.text
    presigned = resp.json()["angles"][0]
    put_resp = httpx.put(
        presigned["upload_url"],
        content=_real_jpeg_bytes(),
        headers={"Content-Type": "image/jpeg"},
    )
    assert put_resp.status_code == 200
    return str(presigned["storage_path"])


async def _generate(client: AsyncClient, key: str, idem_key: str) -> httpx.Response:
    front_path = await _presign_and_upload(client, key, "FRONT")
    return await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": key, "Idempotency-Key": idem_key},
        json={"category_code": "RING", "angles": {"FRONT": {"storage_path": front_path}}},
    )


async def test_rate_limit_exceeded_returns_429_with_retry_after(
    client: AsyncClient, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    key = await _make_key(db_session, rate_limit_per_min=1)

    resp1 = await _generate(client, key, "rl-1")
    assert resp1.status_code == 202, resp1.text

    resp2 = await _generate(client, key, "rl-2")
    assert resp2.status_code == 429
    assert resp2.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert "Retry-After" in resp2.headers


async def test_under_rate_limit_unaffected(
    client: AsyncClient, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    key = await _make_key(db_session, rate_limit_per_min=10)

    resp1 = await _generate(client, key, "rl-under-1")
    assert resp1.status_code == 202, resp1.text
    resp2 = await _generate(client, key, "rl-under-2")
    assert resp2.status_code == 202, resp2.text


async def test_daily_quota_exceeded_returns_429_with_retry_after(
    client: AsyncClient, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    key = await _make_key(db_session, rate_limit_per_min=100, daily_job_quota=1)

    resp1 = await _generate(client, key, "quota-1")
    assert resp1.status_code == 202, resp1.text

    resp2 = await _generate(client, key, "quota-2")
    assert resp2.status_code == 429
    assert resp2.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert "Retry-After" in resp2.headers


async def test_unlimited_quota_never_blocked(
    client: AsyncClient, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    key = await _make_key(db_session, rate_limit_per_min=100, daily_job_quota=None)

    resp1 = await _generate(client, key, "unlimited-1")
    assert resp1.status_code == 202, resp1.text
    resp2 = await _generate(client, key, "unlimited-2")
    assert resp2.status_code == 202, resp2.text
    resp3 = await _generate(client, key, "unlimited-3")
    assert resp3.status_code == 202, resp3.text


async def test_idempotent_replay_does_not_consume_rate_limit_or_quota(
    client: AsyncClient, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    key = await _make_key(db_session, rate_limit_per_min=1, daily_job_quota=1)

    front_path = await _presign_and_upload(client, key, "FRONT")
    body = {"category_code": "RING", "angles": {"FRONT": {"storage_path": front_path}}}
    headers = {"X-API-Key": key, "Idempotency-Key": "replay-no-consume"}

    resp1 = await client.post("/api/v2/generate", headers=headers, json=body)
    assert resp1.status_code == 202, resp1.text

    # Replays of the SAME key must not consume a second rate-limit token or
    # quota slot — both are already at their ceiling of 1 from the first
    # real call above.
    resp2 = await client.post("/api/v2/generate", headers=headers, json=body)
    assert resp2.status_code == 202, resp2.text
    assert resp2.json()["job_id"] == resp1.json()["job_id"]
