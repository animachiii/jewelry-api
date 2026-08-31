"""Phase 9 Checkpoints 2-3 — real QA scoring wired into the pipeline, plus
real GET /qa/review-queue and POST /qa/{sub_job_id}/decision.

Real testcontainers Postgres, real local Redis, real Supabase Storage,
fixture-driven Gemini generation AND Gemini QA scoring — same stack as
tests/integration/test_orchestration.py. Under task_always_eager, a single
POST /generate for a synthetic angle cascades all the way through
generation -> QA scoring in the same request, so tests just assert on the
resulting DB state.
"""

import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
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
GEMINI_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gemini"
QA_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "qa"


def _load(base: Path, name: str) -> dict:
    return json.loads((base / name).read_text())


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


async def _make_key(db_session: AsyncSession, scope: str) -> str:
    import secrets

    raw = secrets.token_urlsafe(32)
    api_client = ApiClient(
        name=f"qa-test-{scope}-client",
        key_prefix=raw[:8],
        key_hash=_hasher.hash(raw),
        scope=scope,
        is_active=True,
    )
    db_session.add(api_client)
    await db_session.commit()
    return raw


@pytest.fixture
async def api_client_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    return await _make_key(db_session, "client")


@pytest.fixture
async def ops_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    return await _make_key(db_session, "ops")


def _fake_gemini_generation_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.providers.gemini as gemini_module

    fixture = _load(GEMINI_FIXTURES, "success.json")
    monkeypatch.setattr(gemini_module.GeminiProvider, "_call_api", lambda self, *a, **k: fixture)


def _fake_qa_score(monkeypatch: pytest.MonkeyPatch, fixture_name: str) -> None:
    import app.providers.gemini_qa as qa_module

    fixture = _load(QA_FIXTURES, fixture_name)
    monkeypatch.setattr(qa_module.GeminiQaProvider, "_call_api", lambda self, *a, **k: fixture)


async def _create_synthetic_job(
    client: AsyncClient, api_client_key: str, idem_key: str
) -> uuid.UUID:
    resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": idem_key},
        json={"category_code": "RING", "angles": {"DIAGONAL": {"synthetic": True}}},
    )
    assert resp.status_code == 202, resp.text
    return uuid.UUID(resp.json()["job_id"])


async def test_score_above_threshold_completes_and_recomputes_parent(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gemini_generation_success(monkeypatch)
    _fake_qa_score(monkeypatch, "high_similarity.json")

    job_id = await _create_synthetic_job(client, api_client_key, "qa-pass")

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status.value == "COMPLETED"
    assert sub_job.qa_status.value == "PASSED"
    assert float(sub_job.qa_score) == pytest.approx(0.94)

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status.value == "COMPLETED"


async def test_score_below_threshold_stays_flagged_parent_processing(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gemini_generation_success(monkeypatch)
    _fake_qa_score(monkeypatch, "low_similarity.json")

    job_id = await _create_synthetic_job(client, api_client_key, "qa-flag")

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status.value == "QA_REVIEW"
    assert sub_job.qa_status.value == "FLAGGED"
    assert float(sub_job.qa_score) == pytest.approx(0.41)

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status.value == "PROCESSING"


async def test_qa_provider_failure_completes_by_default_since_2026_08_30(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_score_and_apply's provider-error branch is shared by
    score_synthetic_angle and score_background_operation, so the
    2026-08-30 QA_PASS_ON_PROVIDER_ERROR policy applies here too -- an
    unevaluated synthetic angle now completes rather than entering the
    human queue. See tests/integration/test_background_operations.py's
    test_unevaluated_output_completes_instead_of_flagging for the full
    rationale.
    """
    _fake_gemini_generation_success(monkeypatch)
    _fake_qa_score(monkeypatch, "malformed.json")

    job_id = await _create_synthetic_job(client, api_client_key, "qa-provider-error")

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status.value == "COMPLETED"
    assert sub_job.qa_status.value == "NOT_APPLICABLE"
    assert sub_job.qa_score is None


async def test_review_queue_lists_flagged_item_with_real_signed_url(
    client: AsyncClient,
    api_client_key: str,
    ops_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    _fake_gemini_generation_success(monkeypatch)
    _fake_qa_score(monkeypatch, "low_similarity.json")
    job_id = await _create_synthetic_job(client, api_client_key, "qa-queue")

    resp = await client.get("/api/v2/qa/review-queue", headers={"X-API-Key": ops_key})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    matching = [i for i in items if i["job_id"] == str(job_id)]
    assert len(matching) == 1
    item = matching[0]
    assert item["qa_score"] == pytest.approx(0.41)
    assert item["angle"] == "DIAGONAL"

    download = httpx.get(item["image_url"])
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("image/")


async def test_review_queue_client_scope_403(client: AsyncClient, api_client_key: str) -> None:
    resp = await client.get("/api/v2/qa/review-queue", headers={"X-API-Key": api_client_key})
    assert resp.status_code == 403


async def test_decision_approve_completes_and_recomputes_parent(
    client: AsyncClient,
    api_client_key: str,
    ops_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gemini_generation_success(monkeypatch)
    _fake_qa_score(monkeypatch, "low_similarity.json")
    job_id = await _create_synthetic_job(client, api_client_key, "qa-approve")

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()

    resp = await client.post(
        f"/api/v2/qa/{sub_job.id}/decision",
        headers={"X-API-Key": ops_key},
        json={"decision": "approve"},
    )
    assert resp.status_code == 202, resp.text

    await db_session.refresh(sub_job)
    assert sub_job.status.value == "COMPLETED"
    assert sub_job.qa_status.value == "PASSED"

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status.value == "COMPLETED"


async def test_decision_reject_sets_rejected_qa_rejected_failure_class(
    client: AsyncClient,
    api_client_key: str,
    ops_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gemini_generation_success(monkeypatch)
    _fake_qa_score(monkeypatch, "low_similarity.json")
    job_id = await _create_synthetic_job(client, api_client_key, "qa-reject")

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()

    resp = await client.post(
        f"/api/v2/qa/{sub_job.id}/decision",
        headers={"X-API-Key": ops_key},
        json={"decision": "reject"},
    )
    assert resp.status_code == 202, resp.text

    await db_session.refresh(sub_job)
    assert sub_job.status.value == "REJECTED"
    assert sub_job.qa_status.value == "FAILED"
    assert sub_job.failure_class.value == "QA_REJECTED"

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status.value == "FAILED"


async def test_decision_on_non_pending_sub_job_409(
    client: AsyncClient,
    api_client_key: str,
    ops_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gemini_generation_success(monkeypatch)
    _fake_qa_score(monkeypatch, "high_similarity.json")
    job_id = await _create_synthetic_job(client, api_client_key, "qa-not-pending")

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status.value == "COMPLETED"  # already scored above threshold

    resp = await client.post(
        f"/api/v2/qa/{sub_job.id}/decision",
        headers={"X-API-Key": ops_key},
        json={"decision": "approve"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "QA_NOT_PENDING"


async def test_decision_on_nonexistent_sub_job_404(client: AsyncClient, ops_key: str) -> None:
    resp = await client.post(
        f"/api/v2/qa/{uuid.uuid4()}/decision",
        headers={"X-API-Key": ops_key},
        json={"decision": "approve"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SUB_JOB_NOT_FOUND"


async def test_qa_rejected_sub_job_cannot_be_retried(
    client: AsyncClient,
    api_client_key: str,
    ops_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gemini_generation_success(monkeypatch)
    _fake_qa_score(monkeypatch, "low_similarity.json")
    job_id = await _create_synthetic_job(client, api_client_key, "qa-reject-then-retry")

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    await client.post(
        f"/api/v2/qa/{sub_job.id}/decision",
        headers={"X-API-Key": ops_key},
        json={"decision": "reject"},
    )

    retry_resp = await client.post(
        f"/api/v2/jobs/{job_id}/angles/DIAGONAL/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-after-qa-reject"},
    )
    assert retry_resp.status_code == 409
    assert retry_resp.json()["error"]["code"] == "SUBJOB_NOT_RETRYABLE"
