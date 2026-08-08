"""Phase 8 Checkpoint 2-3 — real POST /jobs/{job_id}/angles/{angle}/retry.

Real testcontainers Postgres, real local Redis (retry-scoped idempotency,
app/core/idempotency.py), real Supabase Storage, fixture-driven Gemini
(tests/conftest.py's autouse `_fake_gemini_success_by_default`, overridden
per-test where a different outcome is needed) — same stack as
tests/integration/test_orchestration.py.

Every test here starts from a real /generate + a real forced provider
failure (not a hand-built row) so the retried angle's input asset actually
exists in Supabase Storage and its config_version is genuinely pinned —
exactly what a client hits in production. The one thing tests must still do
by hand: generation_service.MAX_ATTEMPTS and job_service.MAX_RETRY_ATTEMPTS
are both 3 and share one column (docs/schema.md — "attempt_count increments
on manual retry and internal backoff"), so a sub-job that fails via the real
internal retry loop is already AT the client-retry ceiling the moment it
lands on FAILED (found and documented, not fixed, in
phases/phase-8-failure-retry.md's self-audit — this is the shared-budget
design working as literally specified in docs/business-rules.md §5, not a
bug this phase silently patches). Tests that need a FAILED-but-still-under-
ceiling sub-job lower attempt_count by hand afterward, same shape of
adjustment seed_dev.py already makes for its own retryable fixture.
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
from app.db.models.job_events import JobEvent
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
        name="retry-test-client",
        key_prefix=raw[:8],
        key_hash=_hasher.hash(raw),
        scope="client",
        is_active=True,
    )
    db_session.add(api_client)
    await db_session.commit()
    return raw


def _real_jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color=(10, 200, 10)).save(buf, format="JPEG")
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


async def _create_failed_job(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    idempotency_key: str,
) -> tuple[uuid.UUID, SubJob]:
    """Real /generate + a real forced TRANSIENT_PROVIDER failure -> FAILED,
    then attempt_count is lowered by hand so the sub-job is under
    MAX_RETRY_ATTEMPTS — see module docstring for why that's needed to test
    the client-retryable path at all.
    """
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
        headers={"X-API-Key": api_client_key, "Idempotency-Key": idempotency_key},
        json={"category_code": "RING", "angles": {"FRONT": {"storage_path": front_path}}},
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status.value == "FAILED"
    assert sub_job.attempt_count == 3  # exhausted the internal loop, as documented above

    sub_job.attempt_count = 1
    await db_session.commit()

    monkeypatch.setattr(
        gemini_module.GeminiProvider, "_call_api", lambda *a, **k: _load("success.json")
    )

    return job_id, sub_job


async def test_retry_resets_and_completes_attempt_count_not_reset_to_zero(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, sub_job = await _create_failed_job(
        client, api_client_key, db_session, monkeypatch, "retry-happy-path"
    )

    resp = await client.post(
        f"/api/v2/jobs/{job_id}/angles/FRONT/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-req-1"},
    )
    assert resp.status_code == 202, resp.text

    await db_session.refresh(sub_job)
    assert sub_job.status.value == "COMPLETED"
    assert sub_job.attempt_count == 2  # continued from 1, not reset to 0 by the retry

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status.value == "COMPLETED"


async def test_retry_moves_terminal_job_back_to_processing_immediately(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same scenario, but the dispatched task itself is what finishes the
    job — this asserts the eager PROCESSING write execute_retry makes before
    dispatch, not the post-completion state test_retry_resets_... checks.
    Since task_always_eager makes the dispatch run inline before the route
    even returns, we can't observe the mid-flight PROCESSING state directly
    without stopping the world — instead this confirms execute_retry's own
    write is exercised (job.completed_at cleared then reset by the real
    recompute), by checking the JobEvent trail contains RETRY_REQUESTED
    ahead of the terminal SUBJOB_STATUS_CHANGE recompute event.
    """
    job_id, _ = await _create_failed_job(
        client, api_client_key, db_session, monkeypatch, "retry-event-order"
    )

    resp = await client.post(
        f"/api/v2/jobs/{job_id}/angles/FRONT/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-req-2"},
    )
    assert resp.status_code == 202, resp.text

    events = (
        (
            await db_session.execute(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
            )
        )
        .scalars()
        .all()
    )
    event_types = [e.event_type for e in events]
    assert "RETRY_REQUESTED" in event_types
    retry_index = event_types.index("RETRY_REQUESTED")
    assert "SUBJOB_STATUS_CHANGE" in event_types[retry_index + 1 :]

    retry_event = events[retry_index]
    assert retry_event.from_status == "FAILED"
    assert retry_event.to_status == "PENDING"


async def test_retry_ceiling_reached_returns_409(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, sub_job = await _create_failed_job(
        client, api_client_key, db_session, monkeypatch, "retry-ceiling"
    )
    sub_job.attempt_count = 3
    sub_job.status = sub_job.status.__class__.FAILED
    await db_session.commit()

    resp = await client.post(
        f"/api/v2/jobs/{job_id}/angles/FRONT/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-req-3"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "RETRY_LIMIT_EXCEEDED"


async def test_retry_on_completed_angle_returns_409(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    front_path = await _presign_and_upload(client, api_client_key, "FRONT")
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-not-failed"},
        json={"category_code": "RING", "angles": {"FRONT": {"storage_path": front_path}}},
    )
    job_id = resp.json()["job_id"]

    retry_resp = await client.post(
        f"/api/v2/jobs/{job_id}/angles/FRONT/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-req-4"},
    )
    assert retry_resp.status_code == 409
    assert retry_resp.json()["error"]["code"] == "SUBJOB_NOT_RETRYABLE"


async def test_retry_replay_same_key_is_noop_no_second_dispatch(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, sub_job = await _create_failed_job(
        client, api_client_key, db_session, monkeypatch, "retry-replay"
    )

    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "retry-req-5"}
    resp1 = await client.post(f"/api/v2/jobs/{job_id}/angles/FRONT/retry", headers=headers)
    assert resp1.status_code == 202

    await db_session.refresh(sub_job)
    attempt_count_after_first = sub_job.attempt_count

    resp2 = await client.post(f"/api/v2/jobs/{job_id}/angles/FRONT/retry", headers=headers)
    assert resp2.status_code == 202

    await db_session.refresh(sub_job)
    assert sub_job.attempt_count == attempt_count_after_first  # no second dispatch happened


async def test_retry_same_key_different_target_returns_409_conflict(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id_1, _ = await _create_failed_job(
        client, api_client_key, db_session, monkeypatch, "retry-conflict-1"
    )
    job_id_2, _ = await _create_failed_job(
        client, api_client_key, db_session, monkeypatch, "retry-conflict-2"
    )

    shared_key = "retry-req-shared"
    resp1 = await client.post(
        f"/api/v2/jobs/{job_id_1}/angles/FRONT/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": shared_key},
    )
    assert resp1.status_code == 202

    resp2 = await client.post(
        f"/api/v2/jobs/{job_id_2}/angles/FRONT/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": shared_key},
    )
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


async def test_status_retryable_false_at_ceiling(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, sub_job = await _create_failed_job(
        client, api_client_key, db_session, monkeypatch, "retry-status-ceiling"
    )
    sub_job.attempt_count = 3
    await db_session.commit()

    resp = await client.get(f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key})
    angle = next(a for a in resp.json()["angles"] if a["angle"] == "FRONT")
    assert angle["status"] == "FAILED"
    assert angle["retryable"] is False
    assert angle["retry_url"] is None
