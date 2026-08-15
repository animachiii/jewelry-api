"""Phase 18 Step 3 — POST /api/v2/match, operation-aware presign for MATCH.

Same stack as tests/integration/test_background_operations.py:
testcontainers Postgres, real local Redis (idempotency, rate limiting), real
Supabase Storage (never mocked). Deliberately does NOT assert on job/sub-job
terminal status the way test_background_operations.py does — Phase 18 Step 4
(fan-out dispatch, worker) is not built yet, so a real MATCH job sits at
PENDING with no worker triggered. This file only covers creation,
validation, and idempotency, exactly what Checkpoint 3 in
phases/phase-18-match.md asks for.
"""

import io
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import httpx
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import JobStatus, Operation, SourceType, SubJobStatus, SyncStatus
from app.db.models.jobs import Job, SubJob
from app.db.session import get_db
from app.main import app
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()


def _match_payload() -> dict:
    """CATEGORY_PAYLOAD (scripts/seed_dev.py) doesn't seed operations.MATCH
    by default — only migration 0014 does, against the real DB. Build a
    payload with MATCH enabled on top of it, same technique
    test_background_operations.py's own
    test_remove_background_disabled_operation_422 uses to inject a
    non-default operations block.
    """
    payload = dict(CATEGORY_PAYLOAD)
    payload["global"] = dict(payload["global"])
    payload["global"]["operations"] = {
        **payload["global"]["operations"],
        "MATCH": {
            "enabled": True,
            "prompt": (
                "Using the provided jewelry piece as a style reference, design a "
                "matching {target_category} intended to be worn as part of the same set."
            ),
            "unit_cost_usd": 0.02,
        },
    }
    return payload


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _make_client(
    db_session: AsyncSession, name: str, scope: str = "client"
) -> tuple[ApiClient, str]:
    import secrets

    raw = secrets.token_urlsafe(32)
    api_client = ApiClient(
        name=name, key_prefix=raw[:8], key_hash=_hasher.hash(raw), scope=scope, is_active=True
    )
    db_session.add(api_client)
    await db_session.flush()
    return api_client, raw


@pytest.fixture
async def active_config(db_session: AsyncSession) -> ConfigVersion:
    cv = ConfigVersion(
        version_number=1,
        source_hash="match-test-hash",
        payload=_match_payload(),
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    return cv


@pytest.fixture
async def api_client_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "match-test-client")
    await db_session.commit()
    return raw


def _real_jpeg_bytes(size: tuple[int, int] = (32, 24)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 200, 10)).save(buf, format="JPEG")
    return buf.getvalue()


async def _presign_and_upload_match(client: AsyncClient, key: str) -> str:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": key},
        json={"operation": "MATCH"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["angles"] == []
    upload = body["operation_upload"]
    assert upload["operation"] == "MATCH"
    put_resp = httpx.put(
        upload["upload_url"], content=_real_jpeg_bytes(), headers={"Content-Type": "image/jpeg"}
    )
    assert put_resp.status_code == 200
    return str(upload["storage_path"])


# --- presign -------------------------------------------------------------


async def test_presign_operation_mode_accepts_match(
    client: AsyncClient, api_client_key: str
) -> None:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": api_client_key},
        json={"operation": "MATCH"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["angles"] == []
    assert body["operation_upload"] is not None
    assert body["operation_upload"]["operation"] == "MATCH"
    assert body["operation_upload"]["storage_path"] is not None


# --- POST /api/v2/match ----------------------------------------------------


async def test_match_creates_job_and_variant_sub_jobs(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    storage_path = await _presign_and_upload_match(client, api_client_key)

    resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "match-1"},
        json={
            "storage_path": storage_path,
            "target_category": "RING",
            "variant_count": 3,
            "sku_reference": "SKU-MATCH-1",
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["angles"] == []
    assert sorted(v["variant_index"] for v in body["variants"]) == [0, 1, 2]
    assert all(v["status"] == "PENDING" for v in body["variants"])

    job = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(body["job_id"])))
    ).scalar_one()
    assert job.operation == Operation.MATCH
    assert job.category_code == "RING"
    assert job.requested_angles == 3
    assert job.sku_reference == "SKU-MATCH-1"
    assert job.status == JobStatus.PENDING  # no worker dispatched yet — Step 4

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 3
    assert sorted(sj.variant_index for sj in sub_jobs) == [0, 1, 2]
    assert all(sj.angle is None for sj in sub_jobs)
    assert all(sj.status == SubJobStatus.PENDING for sj in sub_jobs)
    assert all(sj.source_type == SourceType.UPLOADED for sj in sub_jobs)
    # All variants share one input asset — same reference photo.
    input_asset_ids = {sj.input_asset_id for sj in sub_jobs}
    assert len(input_asset_ids) == 1
    assert None not in input_asset_ids


async def test_match_default_variant_count_is_one(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    storage_path = await _presign_and_upload_match(client, api_client_key)

    resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "match-default-1"},
        json={"storage_path": storage_path, "target_category": "RING"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert len(body["variants"]) == 1
    assert body["variants"][0]["variant_index"] == 0


async def test_match_variant_count_out_of_range_422(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload_match(client, api_client_key)

    resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "match-oob-1"},
        json={"storage_path": storage_path, "target_category": "RING", "variant_count": 5},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_match_unknown_category_returns_category_not_found(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload_match(client, api_client_key)

    resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "match-bad-category-1"},
        json={"storage_path": storage_path, "target_category": "NOT_A_CATEGORY"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "CATEGORY_NOT_FOUND"


async def test_match_disabled_operation_422(client: AsyncClient, db_session: AsyncSession) -> None:
    payload = dict(CATEGORY_PAYLOAD)
    payload["global"] = dict(payload["global"])
    payload["global"]["operations"] = {
        **payload["global"]["operations"],
        "MATCH": {"enabled": False},
    }
    cv = ConfigVersion(
        version_number=1,
        source_hash="match-disabled-hash",
        payload=payload,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    _, key = await _make_client(db_session, "match-disabled-client")
    await db_session.commit()

    storage_path = await _presign_and_upload_match(client, key)
    resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": key, "Idempotency-Key": "match-disabled-1"},
        json={"storage_path": storage_path, "target_category": "RING"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "OPERATION_DISABLED"


async def test_match_storage_path_owned_by_other_client(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    other_client, other_key = await _make_client(db_session, "other-match-client")
    await db_session.commit()
    storage_path = await _presign_and_upload_match(client, other_key)

    resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "match-other-1"},
        json={"storage_path": storage_path, "target_category": "RING"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "ASSET_NOT_OWNED"


async def test_match_requires_idempotency_key(client: AsyncClient, api_client_key: str) -> None:
    resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": api_client_key},
        json={"storage_path": "pending/x/y/MATCH/input_x.jpg", "target_category": "RING"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_match_requires_api_key(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v2/match",
        json={"storage_path": "pending/x/y/MATCH/input_x.jpg", "target_category": "RING"},
    )
    assert resp.status_code == 401, resp.text


async def test_match_replay_returns_original_job_id(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    storage_path = await _presign_and_upload_match(client, api_client_key)
    payload = {"storage_path": storage_path, "target_category": "RING", "variant_count": 2}
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "match-replay-1"}

    first = await client.post("/api/v2/match", headers=headers, json=payload)
    second = await client.post("/api/v2/match", headers=headers, json=payload)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["variants"] == second.json()["variants"]

    jobs = (
        (await db_session.execute(select(Job).where(Job.operation == Operation.MATCH)))
        .scalars()
        .all()
    )
    assert len(jobs) == 1


async def test_match_same_key_different_payload_409(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload_match(client, api_client_key)
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "match-conflict-1"}

    first = await client.post(
        "/api/v2/match",
        headers=headers,
        json={"storage_path": storage_path, "target_category": "RING"},
    )
    assert first.status_code == 202, first.text

    second = await client.post(
        "/api/v2/match",
        headers=headers,
        json={"storage_path": storage_path, "target_category": "RING", "variant_count": 2},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
