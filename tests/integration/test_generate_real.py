"""Phase 2 Checkpoint 4 — real POST /generate: validation, job/sub-job/asset
creation, and idempotency. Uses testcontainers Postgres for the DB and the
real Supabase project for the presign -> PUT -> /generate round trip (same
approach as tests/integration/test_mock_fixtures.py).
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

from app.config import settings
from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import SyncStatus
from app.db.models.job_events import JobEvent
from app.db.models.jobs import Job, SubJob
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
    api_client, raw = await _make_client(db_session, "generate-test-client")
    await db_session.commit()
    return raw


def _real_jpeg_bytes(size: tuple[int, int] = (32, 24)) -> bytes:
    """A tiny but fully valid JPEG — Phase 4 makes /generate actually decode
    uploaded bytes, so fixtures need a real image, not placeholder text."""
    buf = io.BytesIO()
    Image.new("RGB", size, color=(200, 10, 10)).save(buf, format="JPEG")
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


async def test_happy_path_creates_job_sub_jobs_asset_and_event(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    front_path = await _presign_and_upload(client, api_client_key, "FRONT")

    # Phase 9: QA scoring now runs for real (tests/conftest.py's autouse
    # fixture defaults to a high-similarity fixture, which would complete
    # DIAGONAL immediately) — this test wants the lingering QA_REVIEW state
    # it originally asserted, so it forces a below-threshold score instead.
    import json
    from pathlib import Path

    from app.providers.gemini_qa import GeminiQaProvider

    qa_fixture = json.loads(
        (
            Path(__file__).resolve().parent.parent / "fixtures" / "qa" / "low_similarity.json"
        ).read_text()
    )
    monkeypatch.setattr(GeminiQaProvider, "_call_api", lambda self, *a, **k: qa_fixture)

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "happy-path-1"},
        json={
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": front_path},
                "SIDE": {"skip": True},
                "DIAGONAL": {"synthetic": True},
                "TOP": {"skip": True},
            },
        },
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])
    assert resp.json()["status"] == "PENDING"

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.requested_angles == 2
    # Phase 7: /generate now dispatches real work, which under
    # task_always_eager runs inline with a fake-success Gemini response
    # (tests/conftest.py). FRONT (real-photo) completes straight to
    # COMPLETED; DIAGONAL (synthetic) lands in QA_REVIEW pending Phase 9's
    # scoring — which holds the parent at PROCESSING even though the other
    # angle already succeeded (docs/business-rules.md §3/§7).
    assert job.status.value == "PROCESSING"

    sub_jobs = {
        sj.angle.value: sj
        for sj in (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id)))
        .scalars()
        .all()
    }
    assert sub_jobs["FRONT"].status.value == "COMPLETED"
    assert sub_jobs["FRONT"].source_type.value == "UPLOADED"
    assert sub_jobs["FRONT"].input_asset_id is not None
    assert sub_jobs["FRONT"].output_asset_id is not None
    assert sub_jobs["DIAGONAL"].status.value == "QA_REVIEW"
    assert sub_jobs["DIAGONAL"].source_type.value == "SYNTHETIC"
    assert sub_jobs["SIDE"].status.value == "SKIPPED"
    assert sub_jobs["TOP"].status.value == "SKIPPED"

    from app.db.models.assets import Asset

    input_asset = (
        await db_session.execute(select(Asset).where(Asset.id == sub_jobs["FRONT"].input_asset_id))
    ).scalar_one()
    assert input_asset.mime_type == "image/jpeg"
    assert input_asset.width_px == 32
    assert input_asset.height_px == 24
    assert input_asset.bytes is not None and input_asset.bytes > 0
    assert input_asset.checksum_sha256 is not None and len(input_asset.checksum_sha256) == 64
    assert input_asset.expires_at is not None
    delta_days = (input_asset.expires_at - input_asset.created_at).days
    assert 89 <= delta_days <= 90

    events = (
        (await db_session.execute(select(JobEvent).where(JobEvent.job_id == job_id)))
        .scalars()
        .all()
    )
    assert any(e.event_type == "JOB_CREATED" for e in events)

    status_resp = await client.get(
        f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key}
    )
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body["status"] == "PROCESSING"
    angles_by_name = {a["angle"]: a for a in status_body["angles"]}
    assert angles_by_name["FRONT"]["status"] == "COMPLETED"
    assert angles_by_name["FRONT"]["image_url"] is not None
    assert angles_by_name["DIAGONAL"]["status"] == "QA_REVIEW"
    assert angles_by_name["DIAGONAL"]["image_url"] is None
    assert angles_by_name["DIAGONAL"]["synthetic"] is True


async def test_idempotent_replay_creates_no_second_job(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    body = {"category_code": "RING", "angles": {"DIAGONAL": {"synthetic": True}}}
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "replay-key-1"}

    resp1 = await client.post("/api/v2/generate", headers=headers, json=body)
    assert resp1.status_code == 202
    job_id_1 = resp1.json()["job_id"]

    resp2 = await client.post("/api/v2/generate", headers=headers, json=body)
    assert resp2.status_code == 202
    assert resp2.json()["job_id"] == job_id_1

    count = (
        (await db_session.execute(select(Job).where(Job.idempotency_key == "replay-key-1")))
        .scalars()
        .all()
    )
    assert len(count) == 1


async def test_same_key_different_payload_409(client: AsyncClient, api_client_key: str) -> None:
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "conflict-key-1"}
    resp1 = await client.post(
        "/api/v2/generate",
        headers=headers,
        json={"category_code": "RING", "angles": {"DIAGONAL": {"synthetic": True}}},
    )
    assert resp1.status_code == 202

    resp2 = await client.post(
        "/api/v2/generate",
        headers=headers,
        json={"category_code": "NECKLACE", "angles": {"FRONT": {"skip": True}}},
    )
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


async def test_unknown_category_422(client: AsyncClient, api_client_key: str) -> None:
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cat-404"},
        json={"category_code": "BRACELET", "angles": {"FRONT": {"synthetic": True}}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "CATEGORY_NOT_FOUND"


async def test_disabled_angle_422(client: AsyncClient, api_client_key: str) -> None:
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "angle-disabled"},
        json={"category_code": "RING", "angles": {"TOP": {"storage_path": "x"}}},
    )
    # TOP is disabled for RING in CATEGORY_PAYLOAD
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "ANGLE_NOT_ENABLED"


async def test_synthetic_not_allowed_422(client: AsyncClient, api_client_key: str) -> None:
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "synth-not-allowed"},
        json={"category_code": "RING", "angles": {"FRONT": {"synthetic": True}}},
    )
    # FRONT has synthetic_allowed=False for RING
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "SYNTHETIC_NOT_ALLOWED"


async def test_all_angles_skipped_422(client: AsyncClient, api_client_key: str) -> None:
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "all-skipped"},
        json={"category_code": "RING", "angles": {"FRONT": {"skip": True}}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "NO_ANGLES_REQUESTED"


async def test_nonexistent_storage_path_422(client: AsyncClient, api_client_key: str) -> None:
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "asset-404"},
        json={
            "category_code": "RING",
            "angles": {"FRONT": {"storage_path": "pending/does-not-exist/x/FRONT/input_x.jpg"}},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "ASSET_NOT_FOUND"


async def test_storage_path_owned_by_other_client_422(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    other_client, other_key = await _make_client(db_session, "other-generate-client")
    await db_session.commit()
    other_path = await _presign_and_upload(client, other_key, "FRONT")

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "asset-not-owned"},
        json={"category_code": "RING", "angles": {"FRONT": {"storage_path": other_path}}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "ASSET_NOT_OWNED"


async def test_validation_failure_creates_no_job_row(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "no-job-on-failure"},
        json={"category_code": "BRACELET", "angles": {"FRONT": {"synthetic": True}}},
    )
    assert resp.status_code == 422

    rows = (
        (await db_session.execute(select(Job).where(Job.idempotency_key == "no-job-on-failure")))
        .scalars()
        .all()
    )
    assert rows == []


async def test_generate_works_regardless_of_mock_mode(
    client: AsyncClient, api_client_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/generate became real in Phase 2 — it no longer checks MOCK_MODE at
    all (only /retry still does, see app/api/v2/retry.py)."""
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mock-mode-false-check"},
        json={"category_code": "RING", "angles": {"DIAGONAL": {"synthetic": True}}},
    )
    assert resp.status_code == 202, resp.text
