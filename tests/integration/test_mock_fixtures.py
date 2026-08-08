"""Phase 1 Checkpoint 3 — mock fixtures for all 8 job states.

Seeds the real 8 scenarios (scripts/seed_dev.py) into the testcontainers
Postgres, then drives GET /status/{job_id} etc. against them for real. Signed
URLs are generated against the real configured Supabase project (see
app/services/storage_service.py) — Supabase's sign endpoint 404s for a path
with no object, so `seeded_jobs` also backfills placeholder bytes for every
COMPLETED output asset (scripts/upload_seed_assets.upload_placeholder_bytes)
before any test runs. This is a real signed-URL + real-bytes round trip, not
a stub. TTL expiry was verified manually: Supabase returns 400, not the 403
phases/phase-1-api-contract.md originally assumed — see the Step 3 self-audit
in docs/integration-guide.md for that discrepancy.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.api_clients import ApiClient
from app.db.models.jobs import Job
from app.db.session import get_db
from app.main import app
from scripts.seed_dev import main as seed_main
from scripts.upload_seed_assets import upload_placeholder_bytes

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()


class _NoCloseSessionCM:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _patch_session_factory(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.seed_dev as seed_module

    monkeypatch.setattr(seed_module, "async_session_factory", lambda: _NoCloseSessionCM(db_session))


@pytest.fixture(autouse=True)
def _mock_mode_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MOCK_MODE", True)


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
async def seeded_jobs(db_session: AsyncSession) -> dict[str, Job]:
    await seed_main()
    await upload_placeholder_bytes(db_session)
    result = await db_session.execute(select(Job).where(Job.idempotency_key.like("seed-%")))
    return {j.idempotency_key: j for j in result.scalars().all()}


@pytest.fixture
async def dev_client_key(db_session: AsyncSession, seeded_jobs: dict[str, Job]) -> str:
    """The dev-client raw key isn't retrievable after seeding (only the hash is
    stored) — seed a fresh key for the same client row the jobs already belong to
    isn't possible either (unique key_prefix), so we mint a *new* client owning
    the same jobs by repointing client_id. This keeps the test self-contained
    without needing seed_dev's printed raw key.
    """
    import secrets

    raw = secrets.token_urlsafe(32)
    new_client = ApiClient(
        name="test dev client",
        key_prefix=raw[:8],
        key_hash=_hasher.hash(raw),
        scope="client",
        is_active=True,
    )
    db_session.add(new_client)
    await db_session.flush()

    for job in seeded_jobs.values():
        job.client_id = new_client.id
    await db_session.commit()
    return raw


@pytest.fixture
async def other_client_key(db_session: AsyncSession) -> str:
    import secrets

    raw = secrets.token_urlsafe(32)
    client = ApiClient(
        name="test other client",
        key_prefix=raw[:8],
        key_hash=_hasher.hash(raw),
        scope="client",
        is_active=True,
    )
    db_session.add(client)
    await db_session.commit()
    return raw


async def test_all_eight_scenarios_reachable_by_job_id(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    expected_status = {
        "seed-completed": "COMPLETED",
        "seed-partial-retryable": "PARTIAL_SUCCESS",
        "seed-partial-rejected": "PARTIAL_SUCCESS",
        "seed-all-failed": "FAILED",
        "seed-single-angle-failed": "FAILED",
        "seed-two-skipped": "COMPLETED",
        "seed-qa-review": "PROCESSING",
        "seed-in-flight": "PROCESSING",
    }
    for key, job in seeded_jobs.items():
        resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": dev_client_key})
        assert resp.status_code == 200, (key, resp.text)
        assert resp.json()["status"] == expected_status[key], key


async def test_partial_success_failed_angle_is_retryable_with_url(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    job = seeded_jobs["seed-partial-retryable"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": dev_client_key})
    body = resp.json()
    failed = [a for a in body["angles"] if a["status"] == "FAILED"]
    assert len(failed) == 1
    assert failed[0]["retryable"] is True
    assert failed[0]["retry_url"] == f"/api/v2/jobs/{job.id}/angles/{failed[0]['angle']}/retry"
    others = [a for a in body["angles"] if a["status"] != "FAILED"]
    assert all(a["retryable"] is False and a["retry_url"] is None for a in others)


async def test_partial_success_rejected_angle_not_retryable_no_url(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    job = seeded_jobs["seed-partial-rejected"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": dev_client_key})
    body = resp.json()
    rejected = [a for a in body["angles"] if a["status"] == "REJECTED"]
    assert len(rejected) == 1
    assert rejected[0]["retryable"] is False
    assert rejected[0]["retry_url"] is None


async def test_single_angle_failure_is_failed_not_partial(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    job = seeded_jobs["seed-single-angle-failed"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": dev_client_key})
    assert resp.json()["status"] == "FAILED"


async def test_skipped_angles_excluded_from_requested_count(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    job = seeded_jobs["seed-two-skipped"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": dev_client_key})
    body = resp.json()
    assert body["requested_angles"] == 2
    assert body["status"] == "COMPLETED"


async def test_qa_review_fixture_parent_processing_no_image_url(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    job = seeded_jobs["seed-qa-review"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": dev_client_key})
    body = resp.json()
    assert body["status"] == "PROCESSING"
    qa = [a for a in body["angles"] if a["status"] == "QA_REVIEW"]
    assert len(qa) == 1
    assert qa[0]["image_url"] is None
    assert qa[0]["synthetic"] is True


async def test_completed_angle_has_real_signed_url(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    import httpx

    job = seeded_jobs["seed-completed"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": dev_client_key})
    body = resp.json()
    for angle in body["angles"]:
        assert angle["status"] == "COMPLETED"
        assert angle["image_url"] is not None
        assert angle["image_url"].startswith(f"https://{settings.SUPABASE_URL.split('://')[-1]}")

    # Real signed URL that actually returns image bytes on GET — not just a
    # plausible-looking string. See phases/phase-1-api-contract.md Checkpoint 3.
    first_url = body["angles"][0]["image_url"]
    download = httpx.get(first_url)
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("image/")
    assert len(download.content) > 0


async def test_non_terminal_status_has_retry_after_terminal_does_not(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    in_flight = seeded_jobs["seed-in-flight"]
    resp = await client.get(f"/api/v2/status/{in_flight.id}", headers={"X-API-Key": dev_client_key})
    assert "Retry-After" in resp.headers

    completed = seeded_jobs["seed-completed"]
    resp2 = await client.get(
        f"/api/v2/status/{completed.id}", headers={"X-API-Key": dev_client_key}
    )
    assert "Retry-After" not in resp2.headers


async def test_other_clients_job_id_returns_404_not_403(
    client: AsyncClient, other_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    job = seeded_jobs["seed-completed"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": other_client_key})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"


async def test_other_clients_job_id_404_body_leaks_nothing(
    client: AsyncClient, other_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    """Phase 10 Step 3 — URL scoping regression: a client can never be
    handed a signed URL, storage path, or any other asset detail for a job
    it doesn't own. The 404 response body is exactly the standard error
    envelope and nothing else — no image_url, no angles array, no
    storage-shaped fields could leak through it.
    """
    job = seeded_jobs["seed-completed"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": other_client_key})
    assert resp.status_code == 404
    body = resp.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message", "details", "request_id"}
    assert "image_url" not in resp.text
    assert "storage_path" not in resp.text
    assert "supabase" not in resp.text.lower()


async def test_presign_returns_url_accepting_real_put(
    client: AsyncClient, other_client_key: str
) -> None:
    import httpx

    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": other_client_key},
        json={"category_code": "RING", "angles": ["FRONT"]},
    )
    assert resp.status_code == 200
    angle = resp.json()["angles"][0]
    assert angle["storage_path"]
    put_resp = httpx.put(
        angle["upload_url"], content=b"test-bytes", headers={"Content-Type": "image/jpeg"}
    )
    assert put_resp.status_code == 200


async def test_generate_creates_a_real_new_job_not_a_seeded_one(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    """/generate became real in Phase 2 (phases/phase-2-data-model.md) — it no
    longer stands in with an existing seeded job the way Phase 1's mock did.
    Full validation/idempotency coverage lives in test_generate_real.py; this
    just confirms the seeded-job-scoped fixtures in this file don't
    accidentally still exercise mock behavior.
    """
    body = {"category_code": "RING", "angles": {"DIAGONAL": {"synthetic": True}}}
    headers = {"X-API-Key": dev_client_key, "Idempotency-Key": "test-key-1"}
    resp = await client.post("/api/v2/generate", headers=headers, json=body)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert uuid.UUID(job_id)
    assert job_id not in {str(j.id) for j in seeded_jobs.values()}


async def test_generate_missing_idempotency_key_400(
    client: AsyncClient, dev_client_key: str
) -> None:
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": dev_client_key},
        json={"category_code": "RING", "angles": {"FRONT": {"synthetic": True}}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_retry_on_failed_angle_returns_202(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    job = seeded_jobs["seed-partial-retryable"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": dev_client_key})
    failed_angle = next(a["angle"] for a in resp.json()["angles"] if a["status"] == "FAILED")

    retry_resp = await client.post(
        f"/api/v2/jobs/{job.id}/angles/{failed_angle}/retry",
        headers={"X-API-Key": dev_client_key, "Idempotency-Key": "retry-key-1"},
    )
    assert retry_resp.status_code == 202


async def test_retry_on_rejected_angle_returns_409_not_retryable(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    job = seeded_jobs["seed-partial-rejected"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": dev_client_key})
    rejected_angle = next(a["angle"] for a in resp.json()["angles"] if a["status"] == "REJECTED")

    retry_resp = await client.post(
        f"/api/v2/jobs/{job.id}/angles/{rejected_angle}/retry",
        headers={"X-API-Key": dev_client_key, "Idempotency-Key": "retry-key-2"},
    )
    assert retry_resp.status_code == 409
    assert retry_resp.json()["error"]["code"] == "SUBJOB_NOT_RETRYABLE"


async def test_retry_on_completed_angle_returns_409(
    client: AsyncClient, dev_client_key: str, seeded_jobs: dict[str, Job]
) -> None:
    job = seeded_jobs["seed-completed"]
    retry_resp = await client.post(
        f"/api/v2/jobs/{job.id}/angles/FRONT/retry",
        headers={"X-API-Key": dev_client_key, "Idempotency-Key": "retry-key-3"},
    )
    assert retry_resp.status_code == 409


async def test_retry_is_real_regardless_of_mock_mode(
    client: AsyncClient,
    dev_client_key: str,
    seeded_jobs: dict[str, Job],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/retry became real in Phase 8 — settings.MOCK_MODE no longer gates it
    (nor /generate, real since Phase 2). Full real-retry coverage
    (idempotency, ceiling, dispatch) lives in test_retry_real.py; this just
    confirms the flag has no effect either way, in either direction, on this
    fixture-seeded file's own scenarios.
    """
    monkeypatch.setattr(settings, "MOCK_MODE", False)

    job = seeded_jobs["seed-partial-rejected"]
    resp = await client.get(f"/api/v2/status/{job.id}", headers={"X-API-Key": dev_client_key})
    rejected_angle = next(a["angle"] for a in resp.json()["angles"] if a["status"] == "REJECTED")
    retry_resp = await client.post(
        f"/api/v2/jobs/{job.id}/angles/{rejected_angle}/retry",
        headers={"X-API-Key": dev_client_key, "Idempotency-Key": "mock-mode-off-still-real"},
    )
    assert retry_resp.status_code == 409
    assert retry_resp.json()["error"]["code"] == "SUBJOB_NOT_RETRYABLE"
