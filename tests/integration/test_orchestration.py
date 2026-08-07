"""Phase 7 Checkpoints 2-4 — parent-status recompute and fan-out dispatch,
end-to-end through the real POST /generate -> orchestration.fan_out_job ->
generation.transform_photo cascade. Real testcontainers Postgres, real local
Redis, real Supabase Storage — only the Gemini call itself is faked (see
tests/conftest.py's autouse `_fake_gemini_success_by_default`, overridden
per-test here where a different outcome is needed).
"""

import io
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import SyncStatus
from app.db.models.jobs import Job, SubJob
from app.db.session import get_db
from app.main import app
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gemini"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


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


@pytest.fixture
async def api_client_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    import secrets

    raw = secrets.token_urlsafe(32)
    api_client = ApiClient(
        name="orchestration-test-client",
        key_prefix=raw[:8],
        key_hash=_hasher.hash(raw),
        scope="client",
        is_active=True,
    )
    db_session.add(api_client)
    await db_session.commit()
    return raw


def _real_jpeg_bytes() -> bytes:
    """Phase 4 downloads and decodes every uploaded object — placeholder
    bytes now fail /generate with 422 VALIDATION_ERROR, so tests need a
    real, decodable image."""
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color=(200, 10, 10)).save(buf, format="JPEG")
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


async def test_all_angles_succeed_parent_completed(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    front_path = await _presign_and_upload(client, api_client_key, "FRONT")
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "all-succeed"},
        json={"category_code": "RING", "angles": {"FRONT": {"storage_path": front_path}}},
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status.value == "COMPLETED"
    assert job.completed_at is not None
    assert job.succeeded_angles == 1
    assert job.failed_angles == 0


async def test_mixed_success_and_failure_parent_partial_success(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    front_path = await _presign_and_upload(client, api_client_key, "FRONT")
    side_path = await _presign_and_upload(client, api_client_key, "SIDE")

    import app.providers.gemini as gemini_module

    success_fixture = _load("success.json")
    server_error_fixture = _load("server_error_5xx.json")

    def alternating_call_api(self: object, prompt: str, reference_images: list, seed: int) -> dict:
        # The seeded RING prompts name the angle ("...ring, side view." /
        # "...ring, front view.") — route by that instead of call order,
        # since dispatch ordering isn't a contract this test should depend on.
        if "side view" in prompt.lower():
            raise gemini_module.GeminiAPIError(
                server_error_fixture["error"]["code"], server_error_fixture["error"]["message"]
            )
        return success_fixture

    monkeypatch.setattr(gemini_module.GeminiProvider, "_call_api", alternating_call_api)

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mixed-result"},
        json={
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": front_path},
                "SIDE": {"storage_path": side_path},
            },
        },
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status.value == "PARTIAL_SUCCESS"
    assert job.completed_at is not None
    assert job.succeeded_angles == 1
    assert job.failed_angles == 1

    # Phase 7 Checkpoint 4: GET /status must reflect this correctly, real
    # data flowing through Phase 1's status-assembly logic — retryable/
    # retry_url only on the failed angle, never on the one that succeeded.
    status_resp = await client.get(
        f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key}
    )
    angles_by_name = {a["angle"]: a for a in status_resp.json()["angles"]}
    assert angles_by_name["FRONT"]["status"] == "COMPLETED"
    assert angles_by_name["FRONT"]["retryable"] is False
    assert angles_by_name["FRONT"]["retry_url"] is None
    assert angles_by_name["SIDE"]["status"] == "FAILED"
    assert angles_by_name["SIDE"]["retryable"] is True
    assert angles_by_name["SIDE"]["retry_url"] == f"/api/v2/jobs/{job_id}/angles/SIDE/retry"


async def test_all_angles_fail_parent_failed(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    front_path = await _presign_and_upload(client, api_client_key, "FRONT")

    import app.providers.gemini as gemini_module

    server_error_fixture = _load("server_error_5xx.json")

    def always_fail(self: object, prompt: str, reference_images: list, seed: int) -> dict:
        raise gemini_module.GeminiAPIError(
            server_error_fixture["error"]["code"], server_error_fixture["error"]["message"]
        )

    monkeypatch.setattr(gemini_module.GeminiProvider, "_call_api", always_fail)

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "all-fail"},
        json={"category_code": "RING", "angles": {"FRONT": {"storage_path": front_path}}},
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status.value == "FAILED"
    assert job.completed_at is not None

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.attempt_count == 3


async def test_single_angle_failure_is_failed_not_partial_success(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/business-rules.md §3's explicit "gets wrong" case."""
    front_path = await _presign_and_upload(client, api_client_key, "FRONT")

    import app.providers.gemini as gemini_module

    server_error_fixture = _load("server_error_5xx.json")

    def always_fail(self: object, prompt: str, reference_images: list, seed: int) -> dict:
        raise gemini_module.GeminiAPIError(
            server_error_fixture["error"]["code"], server_error_fixture["error"]["message"]
        )

    monkeypatch.setattr(gemini_module.GeminiProvider, "_call_api", always_fail)

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "single-angle-fail"},
        json={"category_code": "RING", "angles": {"FRONT": {"storage_path": front_path}}},
    )
    job_id = uuid.UUID(resp.json()["job_id"])

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status.value == "FAILED"
    assert job.status.value != "PARTIAL_SUCCESS"


async def test_qa_review_keeps_parent_processing_even_with_other_successes(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    front_path = await _presign_and_upload(client, api_client_key, "FRONT")

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-review-holds"},
        json={
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": front_path},
                "DIAGONAL": {"synthetic": True},
            },
        },
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    # FRONT succeeded but DIAGONAL (synthetic) landed in QA_REVIEW, unscored —
    # the parent must not report a terminal status while a decision is pending.
    assert job.status.value == "PROCESSING"
    assert job.completed_at is None


async def test_skipped_sub_jobs_never_affect_rollup(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    front_path = await _presign_and_upload(client, api_client_key, "FRONT")

    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "skips-excluded"},
        json={
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": front_path},
                "SIDE": {"skip": True},
                "TOP": {"skip": True},
            },
        },
    )
    job_id = uuid.UUID(resp.json()["job_id"])

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.requested_angles == 1
    assert job.status.value == "COMPLETED"
    assert job.succeeded_angles == 1
    assert job.failed_angles == 0


async def test_idempotent_replay_does_not_redispatch(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    body = {"category_code": "RING", "angles": {"DIAGONAL": {"synthetic": True}}}
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "replay-no-redispatch"}

    resp1 = await client.post("/api/v2/generate", headers=headers, json=body)
    job_id = uuid.UUID(resp1.json()["job_id"])

    job_before = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    started_at_before = job_before.started_at

    resp2 = await client.post("/api/v2/generate", headers=headers, json=body)
    assert resp2.json()["job_id"] == str(job_id)

    job_after = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job_after.started_at == started_at_before
