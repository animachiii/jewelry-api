"""Phase 18 Steps 3-4 — POST /api/v2/match, operation-aware presign for
MATCH, and (Step 4) the real fan-out dispatch / worker / status / retry
machinery on top of it.

Same stack as tests/integration/test_background_operations.py:
testcontainers Postgres, real local Redis (idempotency, rate limiting), real
Supabase Storage (never mocked), fixture-driven Gemini (tests/conftest.py's
autouse `_fake_gemini_success_by_default`). Under `task_always_eager` (also
autouse), `POST /api/v2/match` now dispatches
`orchestration.fan_out_match_job` -> `match.process` per variant, all inline
during the request — a MATCH job created here runs to a real terminal status
before the 202 response's own DB reads happen, not "sits at PENDING" (that
was true only before Step 4 landed).
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
from app.db.models.cost_events import CostEvent
from app.db.models.enums import (
    FailureClass,
    JobStatus,
    Operation,
    QAStatus,
    SourceType,
    SubJobStatus,
    SyncStatus,
)
from app.db.models.jobs import Job, SubJob
from app.db.session import get_db
from app.main import app
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()
_GEMINI_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gemini"


def _load_gemini_fixture(name: str) -> dict:
    return json.loads((_GEMINI_FIXTURES / name).read_text())


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
    # Step 4: fan-out dispatch is now wired, and under task_always_eager +
    # the autouse fixture-driven Gemini success default, the job runs to a
    # real terminal COMPLETED before this request even returns — the 202
    # response body above still reflects the plan at accept time (PENDING),
    # same as background operations' identical pattern.
    assert job.status == JobStatus.COMPLETED
    assert job.succeeded_angles == 3

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 3
    assert sorted(sj.variant_index for sj in sub_jobs) == [0, 1, 2]
    assert all(sj.angle is None for sj in sub_jobs)
    # COMPLETED, never QA_REVIEW — MATCH has no QA gate (Step 4).
    assert all(sj.status == SubJobStatus.COMPLETED for sj in sub_jobs)
    assert all(sj.qa_status == QAStatus.NOT_APPLICABLE for sj in sub_jobs)
    assert all(sj.output_asset_id is not None for sj in sub_jobs)
    assert all(sj.source_type == SourceType.UPLOADED for sj in sub_jobs)
    # All variants share one input asset — same reference photo.
    input_asset_ids = {sj.input_asset_id for sj in sub_jobs}
    assert len(input_asset_ids) == 1
    assert None not in input_asset_ids

    cost_events = (
        (await db_session.execute(select(CostEvent).where(CostEvent.job_id == job.id)))
        .scalars()
        .all()
    )
    assert len(cost_events) == 3  # one call attempt per variant
    assert all(e.operation == "match" for e in cost_events)


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


# --- Step 4: fan-out, worker, mixed outcome, status, retry ----------------


async def test_match_mixed_outcome_rolls_up_to_partial_success(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forces the first provider call to refuse (SAFETY_REFUSAL — a single,
    non-retryable attempt, so it doesn't burn its whole internal retry
    budget and accidentally succeed on a later attempt within the same
    sub-job) and every subsequent call to succeed. Since fan_out_match_job
    dispatches each variant's match.process call synchronously, in order,
    under task_always_eager, the first sub-job's entire attempt loop runs to
    completion before the second sub-job's first call happens — so "first
    call fails, the rest succeed" reliably lands on exactly one FAILED and
    the rest COMPLETED, without depending on which variant_index is which.
    Confirms the job-level rollup uses the exact same
    generation_service.recompute_parent_status/compute_parent_status logic
    every other operation does — no MATCH-specific rollup code exists to
    write or to break.
    """
    import app.providers.gemini as gemini_module

    success_fixture = _load_gemini_fixture("success.json")
    refusal_fixture = _load_gemini_fixture("safety_refusal.json")
    calls = {"count": 0}

    def _first_call_refuses(self: object, *a: object, **k: object) -> dict:
        calls["count"] += 1
        return refusal_fixture if calls["count"] == 1 else success_fixture

    monkeypatch.setattr(gemini_module.GeminiProvider, "_call_api", _first_call_refuses)

    storage_path = await _presign_and_upload_match(client, api_client_key)
    resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "match-mixed-1"},
        json={"storage_path": storage_path, "target_category": "RING", "variant_count": 2},
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status == JobStatus.PARTIAL_SUCCESS
    assert job.succeeded_angles == 1
    assert job.failed_angles == 1

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalars().all()
    )
    # A safety refusal lands on REJECTED, not FAILED (same _fail shape
    # background_service.py uses) — recompute_parent_status counts both as
    # "failed" for the parent rollup (job.failed_angles above), but the
    # sub-job's own status is REJECTED specifically.
    statuses = sorted(sj.status for sj in sub_jobs)
    assert statuses == sorted([SubJobStatus.REJECTED, SubJobStatus.COMPLETED])
    failed_sub_job = next(sj for sj in sub_jobs if sj.status == SubJobStatus.REJECTED)
    assert failed_sub_job.failure_class == FailureClass.SAFETY_REFUSAL
    assert failed_sub_job.attempt_count == 1  # non-retryable, no internal retry burned


async def test_status_for_match_job_returns_variants(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload_match(client, api_client_key)
    create_resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "match-status-1"},
        json={"storage_path": storage_path, "target_category": "RING", "variant_count": 2},
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.get(f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "MATCH"
    assert body["angles"] == []
    assert body["results"] == []
    assert len(body["variants"]) == 2
    assert sorted(v["variant_index"] for v in body["variants"]) == [0, 1]
    # Ordered by variant_index — see app/api/v2/status.py's MATCH branch.
    assert [v["variant_index"] for v in body["variants"]] == [0, 1]
    for variant in body["variants"]:
        assert variant["status"] == "COMPLETED"
        assert variant["image_url"] is not None
        assert variant["qa_status"] == "NOT_APPLICABLE"
        assert "angle" not in variant


async def test_retry_job_retries_only_failed_match_variants(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """Checkpoint 4: `POST /jobs/{job_id}/retry` against a PARTIAL_SUCCESS
    MATCH job retries only the failed variant(s). variant_count: 3, with
    variants 0 and 2 forced to FAILED after a real successful creation
    (same "force via hand-edit" technique
    test_retry_job_202_on_failed_background_job_runs_to_completion uses) —
    confirms only the forced-FAILED sub-jobs get re-dispatched (attempt_count
    increases, status moves back through PENDING and lands COMPLETED again
    under the default success fixture) while variant 1's original COMPLETED
    state, output asset, and attempt_count are left untouched.
    """
    storage_path = await _presign_and_upload_match(client, api_client_key)
    create_resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "match-retry-create-1"},
        json={"storage_path": storage_path, "target_category": "RING", "variant_count": 3},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalars().all()
    )
    assert len(sub_jobs) == 3
    assert all(sj.status == SubJobStatus.COMPLETED for sj in sub_jobs)  # real completion

    by_variant = {sj.variant_index: sj for sj in sub_jobs}
    untouched_output_asset_id = by_variant[1].output_asset_id
    untouched_attempt_count = by_variant[1].attempt_count

    for idx in (0, 2):
        by_variant[idx].status = SubJobStatus.FAILED
        by_variant[idx].failure_class = FailureClass.TRANSIENT_NETWORK
    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    job.status = JobStatus.PARTIAL_SUCCESS
    await db_session.commit()

    attempt_counts_before = {idx: by_variant[idx].attempt_count for idx in (0, 2)}

    resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "match-retry-attempt-1"},
    )
    assert resp.status_code == 202, resp.text

    for idx in (0, 2):
        await db_session.refresh(by_variant[idx])
    await db_session.refresh(by_variant[1])
    await db_session.refresh(job)

    for idx in (0, 2):
        sub_job = by_variant[idx]
        assert sub_job.status == SubJobStatus.COMPLETED  # re-ran, succeeded again
        assert sub_job.attempt_count > attempt_counts_before[idx]

    # Variant 1 was never FAILED — untouched by the retry.
    assert by_variant[1].status == SubJobStatus.COMPLETED
    assert by_variant[1].output_asset_id == untouched_output_asset_id
    assert by_variant[1].attempt_count == untouched_attempt_count

    assert job.status == JobStatus.COMPLETED
    assert job.succeeded_angles == 3
    assert job.failed_angles == 0


async def test_retry_job_replay_same_key_is_noop_for_match(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """Regression for the retry-idempotency-target redesign (app/api/v2/retry.py):
    a replay of the same Idempotency-Key against a MATCH job-level retry must
    stay a no-op 202, not re-dispatch a second time.
    """
    storage_path = await _presign_and_upload_match(client, api_client_key)
    create_resp = await client.post(
        "/api/v2/match",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "match-retry-replay-create-1"},
        json={"storage_path": storage_path, "target_category": "RING", "variant_count": 2},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalars().all()
    )
    target_sub_job = sub_jobs[0]
    target_sub_job.status = SubJobStatus.FAILED
    target_sub_job.failure_class = FailureClass.TRANSIENT_NETWORK
    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    job.status = JobStatus.PARTIAL_SUCCESS
    await db_session.commit()

    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "match-retry-replay-attempt-1"}
    first = await client.post(f"/api/v2/jobs/{job_id}/retry", headers=headers)
    assert first.status_code == 202, first.text

    await db_session.refresh(target_sub_job)
    attempt_count_after_first = target_sub_job.attempt_count

    second = await client.post(f"/api/v2/jobs/{job_id}/retry", headers=headers)
    assert second.status_code == 202, second.text

    await db_session.refresh(target_sub_job)
    assert target_sub_job.attempt_count == attempt_count_after_first  # no second dispatch
