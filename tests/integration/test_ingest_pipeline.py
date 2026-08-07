"""Phase 4 — Storage & Ingest Pipeline.

Real image validation on /generate, asset metadata persistence, and the
retention/expiry worker. Uses testcontainers Postgres for the DB and the
real Supabase project for Storage (same approach as
tests/integration/test_generate_real.py) — Supabase Storage is never mocked
in this repo.
"""

import io
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.api_clients import ApiClient
from app.db.models.assets import Asset
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import AssetKind, SyncStatus
from app.db.models.jobs import Job
from app.db.session import get_db
from app.main import app
from app.services import retention_service, storage_service
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


async def _make_client(db_session: AsyncSession, name: str) -> tuple[ApiClient, str]:
    import secrets

    raw = secrets.token_urlsafe(32)
    api_client = ApiClient(
        name=name, key_prefix=raw[:8], key_hash=_hasher.hash(raw), scope="client", is_active=True
    )
    db_session.add(api_client)
    await db_session.flush()
    return api_client, raw


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


@pytest.fixture
async def api_client_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    api_client, raw = await _make_client(db_session, "ingest-test-client")
    await db_session.commit()
    return raw


async def _presign(client: AsyncClient, key: str, angle: str = "FRONT") -> tuple[str, str]:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": key},
        json={"category_code": "RING", "angles": [angle]},
    )
    assert resp.status_code == 200, resp.text
    angle_resp = resp.json()["angles"][0]
    return str(angle_resp["storage_path"]), str(angle_resp["upload_url"])


async def test_corrupt_image_rejected_with_validation_error(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    storage_path, upload_url = await _presign(client, api_client_key)
    put_resp = httpx.put(
        upload_url,
        content=b"not-a-real-image-just-garbage-bytes",
        headers={"Content-Type": "image/jpeg"},
    )
    assert put_resp.status_code == 200

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "corrupt-image-1"},
        json={
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": storage_path},
                "SIDE": {"skip": True},
                "DIAGONAL": {"skip": True},
                "TOP": {"skip": True},
            },
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    jobs = (await db_session.execute(select(Job))).scalars().all()
    assert jobs == []


async def test_empty_upload_rejected_with_validation_error(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path, upload_url = await _presign(client, api_client_key)
    put_resp = httpx.put(upload_url, content=b"", headers={"Content-Type": "image/jpeg"})
    assert put_resp.status_code == 200

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "empty-image-1"},
        json={
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": storage_path},
                "SIDE": {"skip": True},
                "DIAGONAL": {"skip": True},
                "TOP": {"skip": True},
            },
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_unsupported_format_rejected_with_validation_error(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path, upload_url = await _presign(client, api_client_key)
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="BMP")
    put_resp = httpx.put(upload_url, content=buf.getvalue(), headers={"Content-Type": "image/bmp"})
    assert put_resp.status_code == 200

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "bmp-image-1"},
        json={
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": storage_path},
                "SIDE": {"skip": True},
                "DIAGONAL": {"skip": True},
                "TOP": {"skip": True},
            },
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_valid_jpeg_extracts_real_metadata(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    storage_path, upload_url = await _presign(client, api_client_key)
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color=(1, 2, 3)).save(buf, format="PNG")
    png_bytes = buf.getvalue()
    put_resp = httpx.put(upload_url, content=png_bytes, headers={"Content-Type": "image/png"})
    assert put_resp.status_code == 200

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "valid-png-1"},
        json={
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": storage_path},
                "SIDE": {"skip": True},
                "DIAGONAL": {"skip": True},
                "TOP": {"skip": True},
            },
        },
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])

    # Phase 7: /generate now dispatches real generation, so this job also
    # gets an OUTPUT asset once the (fixture-faked) cascade completes —
    # filter to the INPUT row this test actually cares about.
    asset = (
        await db_session.execute(
            select(Asset).where(Asset.job_id == job_id, Asset.kind == AssetKind.INPUT)
        )
    ).scalar_one()
    assert asset.mime_type == "image/png"
    assert asset.width_px == 64
    assert asset.height_px == 48
    assert asset.bytes == len(png_bytes)


async def test_retention_worker_purges_bytes_but_keeps_row(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    """Simulates an INPUT asset past its retention deadline:
    retention_service.expire_assets removes the Supabase Storage object and
    stamps `purged_at`, but the row survives (CLAUDE.md Hard Rule 10)."""
    api_client, _raw = await _make_client(db_session, "retention-test-client")
    await db_session.flush()

    job = Job(
        client_id=api_client.id,
        idempotency_key="retention-test",
        payload_hash="hash",
        category_code="RING",
        config_version_id=active_config.id,
        requested_angles=1,
    )
    db_session.add(job)
    await db_session.flush()

    storage_path = f"retention-test/{uuid.uuid4().hex}/FRONT/input_{uuid.uuid4().hex[:8]}.jpg"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="JPEG")
    storage_service.get_client().storage.from_(settings.BUCKET_INPUTS).upload(
        storage_path, buf.getvalue(), {"content-type": "image/jpeg"}
    )
    assert storage_service.exists(settings.BUCKET_INPUTS, storage_path)

    asset = Asset(
        job_id=job.id,
        kind=AssetKind.INPUT,
        bucket=settings.BUCKET_INPUTS,
        storage_path=storage_path,
        mime_type="image/jpeg",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(asset)
    await db_session.commit()

    purged = await retention_service.expire_assets(db_session, now=datetime.now(UTC))
    assert purged >= 1

    await db_session.refresh(asset)
    assert asset.purged_at is not None
    assert not storage_service.exists(settings.BUCKET_INPUTS, storage_path)

    # The row is never deleted.
    still_there = (await db_session.execute(select(Asset).where(Asset.id == asset.id))).scalar_one()
    assert still_there.id == asset.id


async def test_expire_assets_ignores_indefinite_retention(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    """An OUTPUT asset with expires_at=None (indefinite, pending client
    policy — see phases/phase-roadmap.md open decision #5) is never swept."""
    api_client, _raw = await _make_client(db_session, "retention-indefinite-client")
    await db_session.flush()

    job = Job(
        client_id=api_client.id,
        idempotency_key="retention-indefinite-test",
        payload_hash="hash",
        category_code="RING",
        config_version_id=active_config.id,
        requested_angles=1,
    )
    db_session.add(job)
    await db_session.flush()

    asset = Asset(
        job_id=job.id,
        kind=AssetKind.OUTPUT,
        bucket=settings.BUCKET_OUTPUTS,
        storage_path=f"retention-indefinite/{uuid.uuid4().hex}.jpg",
        mime_type="image/jpeg",
        expires_at=None,
    )
    db_session.add(asset)
    await db_session.commit()

    purged = await retention_service.expire_assets(db_session, now=datetime.now(UTC))
    assert purged == 0

    await db_session.refresh(asset)
    assert asset.purged_at is None
