"""POST /api/v2/generate-with-cleanup, operation-aware presign, and the real
two-phase dispatch/worker/status/retry machinery. See docs/superpowers/specs/
2026-08-31-generate-with-cleanup-design.md.

Same stack as tests/integration/test_api_mix.py: testcontainers Postgres,
real local Redis, real Supabase Storage (never mocked), fixture-driven
Gemini (tests/conftest.py's autouse `_fake_gemini_success_by_default`).
Under `task_always_eager` (also autouse), `POST /api/v2/generate-with-cleanup`
dispatches `cleanup.process` inline during the request -- and
`cleanup.process` itself dispatches `generation.transform_photo_task` inline
too, so a happy-path request completes the ENTIRE two-phase pipeline
synchronously within the test.
"""

import io
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import JobStatus, Operation, SubJobStatus, SyncStatus
from app.db.models.jobs import Job, SubJob
from app.db.session import get_db
from app.main import app
from app.providers.gemini import GeminiProvider
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()
_GEMINI_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gemini"


def _load_gemini_fixture(name: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((_GEMINI_FIXTURES / name).read_text())
    return result


def _cleanup_payload() -> dict[str, Any]:
    """CATEGORY_PAYLOAD doesn't seed operations.GENERATE_WITH_CLEANUP by
    default -- only migration 0022 does, against the real DB. Build a
    payload with it enabled on top of it, same technique test_api_mix.py's
    own _mix_payload uses."""
    payload = dict(CATEGORY_PAYLOAD)
    payload["global"] = dict(payload["global"])
    payload["global"]["operations"] = {
        **payload["global"]["operations"],
        "GENERATE_WITH_CLEANUP": {
            "enabled": True,
            "prompt": "Remove the background, standardize on a clean e-commerce backdrop.",
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
        source_hash="generate-with-cleanup-test-hash",
        payload=_cleanup_payload(),
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    return cv


@pytest.fixture
async def api_client_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "generate-with-cleanup-test-client")
    await db_session.commit()
    return raw


def _real_jpeg_bytes(size: tuple[int, int] = (60, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 10, 200)).save(buf, format="JPEG")
    return buf.getvalue()


async def _presign_and_upload(client: AsyncClient, key: str) -> str:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": key},
        json={"operation": "GENERATE_WITH_CLEANUP"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation_upload"] is not None
    upload = body["operation_upload"]

    put = httpx.put(
        upload["upload_url"], content=_real_jpeg_bytes(), headers={"Content-Type": "image/jpeg"}
    )
    assert put.status_code == 200
    return str(upload["storage_path"])


async def test_presign_operation_mode_accepts_generate_with_cleanup(
    client: AsyncClient, api_client_key: str
) -> None:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": api_client_key},
        json={"operation": "GENERATE_WITH_CLEANUP"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["angles"] == []
    assert body["operation_upload"] is not None
    assert body["mask_upload"] is None
    assert body["secondary_upload"] is None


async def test_happy_path_creates_cleanup_sub_job_then_angle_sub_jobs_from_its_output(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """The test that proves the pipeline actually chains: every angle
    sub-job's input_asset_id must be the CLEANUP sub-job's output asset --
    NOT the client's original upload.
    """
    storage_path = await _presign_and_upload(client, api_client_key)

    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-happy-path"},
        json={
            "storage_path": storage_path,
            "category_code": "RING",
            "angles": ["FRONT", "SIDE", "DIAGONAL"],
        },
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    job = (await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))).scalar_one()
    assert job.status == JobStatus.COMPLETED
    assert job.operation == Operation.GENERATE_WITH_CLEANUP

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 4  # 1 cleanup + 3 angles

    cleanup_sub_job = next(sj for sj in sub_jobs if sj.angle is None)
    assert cleanup_sub_job.status == SubJobStatus.COMPLETED
    assert cleanup_sub_job.output_asset_id is not None

    angle_sub_jobs = [sj for sj in sub_jobs if sj.angle is not None]
    assert len(angle_sub_jobs) == 3
    assert {sj.angle.value for sj in angle_sub_jobs} == {"FRONT", "SIDE", "DIAGONAL"}
    for angle_sub_job in angle_sub_jobs:
        assert angle_sub_job.status == SubJobStatus.COMPLETED
        assert angle_sub_job.input_asset_id == cleanup_sub_job.output_asset_id


async def test_cleanup_failure_fails_the_job_with_zero_angle_sub_jobs(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors import ProviderError
    from app.db.models.enums import FailureClass

    def _refuse(self: object, prompt: str, reference_images: list[bytes], seed: int) -> None:
        raise ProviderError("refused.", failure_class=FailureClass.SAFETY_REFUSAL)

    monkeypatch.setattr(GeminiProvider, "generate", _refuse)

    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-fails"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    job = (await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))).scalar_one()
    assert job.status == JobStatus.FAILED

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 1  # the cleanup sub-job only -- no angle sub-jobs ever created
    assert sub_jobs[0].angle is None
    assert sub_jobs[0].status == SubJobStatus.REJECTED  # SAFETY_REFUSAL -> REJECTED, not FAILED


async def test_status_never_exposes_the_cleanup_sub_job(
    client: AsyncClient, api_client_key: str
) -> None:
    """The user's explicit choice: internal only, never in results, never
    in angles."""
    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-not-exposed"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    job_id = resp.json()["job_id"]

    status_resp = await client.get(
        f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key}
    )
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["operation"] == "GENERATE_WITH_CLEANUP"
    assert body["results"] == []
    assert len(body["angles"]) == 1
    assert body["angles"][0]["angle"] == "FRONT"
    assert body["angles"][0]["status"] == "COMPLETED"


async def test_empty_angles_list_returns_no_angles_requested(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-no-angles"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": []},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "NO_ANGLES_REQUESTED"


async def test_duplicate_angles_rejected(client: AsyncClient, api_client_key: str) -> None:
    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-dup-angles"},
        json={
            "storage_path": storage_path,
            "category_code": "RING",
            "angles": ["FRONT", "FRONT"],
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_idempotent_replay_returns_original_job_id(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload(client, api_client_key)
    payload = {"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]}
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-replay"}

    first = await client.post("/api/v2/generate-with-cleanup", headers=headers, json=payload)
    second = await client.post("/api/v2/generate-with-cleanup", headers=headers, json=payload)
    assert first.json()["job_id"] == second.json()["job_id"]


async def test_same_key_different_payload_409(client: AsyncClient, api_client_key: str) -> None:
    storage_path = await _presign_and_upload(client, api_client_key)
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-conflict"}
    await client.post(
        "/api/v2/generate-with-cleanup",
        headers=headers,
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers=headers,
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["SIDE"]},
    )
    assert resp.status_code == 409, resp.text


async def test_job_level_retry_retries_a_failed_cleanup_step(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the cleanup sub-job exists when cleanup fails -- job-level retry
    (§14's all-or-nothing logic, a set of size one) retries exactly it, and
    on success creates and dispatches the angle sub-jobs through the
    identical path the original request used.

    **Expected to fail until Task 9 wires GENERATE_WITH_CLEANUP into
    app/api/v2/retry.py's dispatch-task lookup** -- that route currently
    falls back to background_process_task for any operation it doesn't
    recognize, which is the wrong worker for a cleanup sub-job. Written
    anyway per the task brief; not touching retry.py to make it pass.
    """
    from app.core.errors import ProviderError
    from app.db.models.enums import FailureClass

    call_count = {"n": 0}
    original_generate = GeminiProvider.generate

    def _fail_once_then_succeed(
        self: GeminiProvider, prompt: str, reference_images: list[bytes], seed: int
    ):  # noqa: ANN201
        call_count["n"] += 1
        if call_count["n"] <= 3:  # exhaust all 3 in-process attempts
            raise ProviderError("blip.", failure_class=FailureClass.TRANSIENT_PROVIDER)
        return original_generate(self, prompt, reference_images, seed)

    monkeypatch.setattr(GeminiProvider, "generate", _fail_once_then_succeed)

    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-retry"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    job_id = resp.json()["job_id"]
    job = (await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))).scalar_one()
    assert job.status == JobStatus.FAILED

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 1
    assert sub_jobs[0].status == SubJobStatus.FAILED  # TRANSIENT_PROVIDER -> FAILED, retryable
    assert sub_jobs[0].attempt_count == 3  # exhausted the internal loop, as documented below

    # Same shared-counter situation tests/integration/test_retry_real.py's own
    # _create_failed_job documents and works around: cleanup_service.MAX_ATTEMPTS
    # and job_service.MAX_RETRY_ATTEMPTS are both 3 and share one column
    # (docs/schema.md, CLAUDE.md's Phase 8 entry), so a sub-job that fails via
    # the real internal retry loop is already AT the client-retry ceiling the
    # moment it lands on FAILED. Lower it by hand, exactly like that module
    # does, so this test can actually exercise the client-retry path rather
    # than universally 409ing on RETRY_LIMIT_EXCEEDED regardless of what
    # Task 9 does to retry.py's dispatch map.
    sub_jobs[0].attempt_count = 1
    await db_session.commit()

    retry_resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-retry-2"},
    )
    assert retry_resp.status_code == 202, retry_resp.text

    await db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 2  # cleanup (now COMPLETED) + the 1 angle it dispatched
    angle_sub_job = next(sj for sj in sub_jobs if sj.angle is not None)
    assert angle_sub_job.status == SubJobStatus.COMPLETED


async def test_job_level_retry_blocked_once_angle_sub_jobs_exist(
    client: AsyncClient, api_client_key: str
) -> None:
    """Once cleanup has succeeded and angle sub-jobs exist, job-level retry
    must reject with a clear redirect to the per-angle route -- exactly the
    same posture ANGLE_GENERATION jobs already have, conditional here on
    whether the pipeline has moved past its cleanup phase.

    **Expected to fail until Task 9** wires GENERATE_WITH_CLEANUP-specific
    handling into retry.py -- see the note on the retry test above.
    """
    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-past-phase-1"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    job_id = resp.json()["job_id"]

    retry_resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-blocked-retry"},
    )
    assert retry_resp.status_code == 409, retry_resp.text


async def test_per_angle_retry_works_once_cleanup_has_succeeded(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing per-angle route works completely unmodified once the
    angle sub-job's input_asset_id already points at the cleanup output."""
    from app.db.models.enums import FailureClass

    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-then-angle-fails"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    job_id = resp.json()["job_id"]
    job = (await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))).scalar_one()
    assert job.status == JobStatus.COMPLETED  # cleanup + angle both succeeded via fixture

    # Force the already-completed angle sub-job back to FAILED to exercise
    # the retry route in isolation, rather than re-running the whole
    # pipeline with a more elaborate call-counting monkeypatch.
    angle_sub_job = (
        (
            await db_session.execute(
                select(SubJob).where(SubJob.job_id == job.id, SubJob.angle.is_not(None))
            )
        )
        .scalars()
        .one()
    )
    angle_sub_job.status = SubJobStatus.FAILED
    angle_sub_job.failure_class = FailureClass.TRANSIENT_PROVIDER
    angle_sub_job.attempt_count = 1
    await db_session.commit()

    retry_resp = await client.post(
        f"/api/v2/jobs/{job_id}/angles/FRONT/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "angle-retry"},
    )
    assert retry_resp.status_code == 202, retry_resp.text

    await db_session.refresh(angle_sub_job)
    assert angle_sub_job.status == SubJobStatus.COMPLETED


async def test_cost_report_includes_cleanup_and_angle_calls(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    from app.db.models.cost_events import CostEvent

    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-cost"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT", "SIDE"]},
    )
    job_id = resp.json()["job_id"]

    events = (
        (await db_session.execute(select(CostEvent).where(CostEvent.job_id == uuid.UUID(job_id))))
        .scalars()
        .all()
    )
    assert len(events) == 3  # 1 cleanup call + 2 angle calls
