"""Phase 11 (scoped) — GET /api/v2/jobs and GET /api/v2/jobs/{job_id}/cost,
real for the first time. Both were stubbed with `raise NotImplementedError`
since Phase 1. See phases/phase-11-observability-cost-tracking.md.

Same stack as every other integration test in this project: testcontainers
Postgres, real local Redis. Jobs/sub-jobs/cost-events are created directly
via the real repository/service functions those layers already expose, not
via raw SQL — same "reuse what's real" posture the RECOLOR/MIX test files
use for fixture setup.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import (
    JobStatus,
    Operation,
    SourceType,
    SubJobStatus,
    SyncStatus,
)
from app.db.repositories import jobs as jobs_repo
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
        source_hash="jobs-test-hash",
        payload=CATEGORY_PAYLOAD,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    return cv


@pytest.fixture
async def ops_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "jobs-ops-client", scope="ops")
    await db_session.commit()
    return raw


@pytest.fixture
async def client_scope_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "jobs-client-scope-client", scope="client")
    await db_session.commit()
    return raw


async def _create_job(
    db_session: AsyncSession,
    api_client: ApiClient,
    config: ConfigVersion,
    *,
    operation: Operation = Operation.ANGLE_GENERATION,
    category_code: str | None = "RING",
    status: JobStatus = JobStatus.PENDING,
    created_at: datetime | None = None,
) -> uuid.UUID:
    job = jobs_repo.create_job(
        db_session,
        client_id=api_client.id,
        idempotency_key=f"idem-{uuid.uuid4()}",
        payload_hash="hash",
        category_code=category_code,
        config_version_id=config.id,
        requested_angles=1,
        sku_reference=None,
        metadata={},
        operation=operation,
    )
    await db_session.flush()
    job.status = status
    if created_at is not None:
        job.created_at = created_at
    await db_session.commit()
    return job.id


# --- GET /jobs ---------------------------------------------------------


async def test_list_jobs_requires_ops_scope(client: AsyncClient, client_scope_key: str) -> None:
    resp = await client.get("/api/v2/jobs", headers={"X-API-Key": client_scope_key})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "INSUFFICIENT_SCOPE"


async def test_list_jobs_returns_jobs_across_clients_newest_first(
    client: AsyncClient, ops_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    other_client, _ = await _make_client(db_session, "some-other-client")
    ops_client_row, _ = await _make_client(db_session, "irrelevant-owner")
    await db_session.commit()

    older_id = await _create_job(
        db_session,
        other_client,
        active_config,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    newer_id = await _create_job(
        db_session,
        ops_client_row,
        active_config,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    resp = await client.get("/api/v2/jobs", headers={"X-API-Key": ops_key})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [j["job_id"] for j in body["jobs"]]
    assert str(newer_id) in ids
    assert str(older_id) in ids
    assert ids.index(str(newer_id)) < ids.index(str(older_id))  # newest first


async def test_list_jobs_filters_by_status(
    client: AsyncClient, ops_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    owner, _ = await _make_client(db_session, "status-filter-client")
    await db_session.commit()
    completed_id = await _create_job(db_session, owner, active_config, status=JobStatus.COMPLETED)
    await _create_job(db_session, owner, active_config, status=JobStatus.FAILED)

    resp = await client.get(
        "/api/v2/jobs", headers={"X-API-Key": ops_key}, params={"status": "COMPLETED"}
    )
    assert resp.status_code == 200, resp.text
    ids = [j["job_id"] for j in resp.json()["jobs"]]
    assert str(completed_id) in ids
    assert all(j["status"] == "COMPLETED" for j in resp.json()["jobs"])


async def test_list_jobs_filters_by_category_code(
    client: AsyncClient, ops_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    owner, _ = await _make_client(db_session, "category-filter-client")
    await db_session.commit()
    ring_id = await _create_job(db_session, owner, active_config, category_code="RING")
    await _create_job(db_session, owner, active_config, category_code="EARRING")

    resp = await client.get(
        "/api/v2/jobs", headers={"X-API-Key": ops_key}, params={"category_code": "RING"}
    )
    assert resp.status_code == 200, resp.text
    ids = [j["job_id"] for j in resp.json()["jobs"]]
    assert str(ring_id) in ids
    assert all(j["category_code"] == "RING" for j in resp.json()["jobs"])


async def test_list_jobs_filters_by_created_date_range(
    client: AsyncClient, ops_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    owner, _ = await _make_client(db_session, "date-filter-client")
    await db_session.commit()
    now = datetime.now(UTC)
    in_range_id = await _create_job(
        db_session, owner, active_config, created_at=now - timedelta(hours=1)
    )
    await _create_job(db_session, owner, active_config, created_at=now - timedelta(days=10))

    resp = await client.get(
        "/api/v2/jobs",
        headers={"X-API-Key": ops_key},
        params={
            "created_after": (now - timedelta(hours=2)).isoformat(),
            "created_before": now.isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    ids = [j["job_id"] for j in resp.json()["jobs"]]
    assert str(in_range_id) in ids
    assert len(ids) == 1


async def test_list_jobs_total_reflects_full_filtered_count_not_page_size(
    client: AsyncClient, ops_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    owner, _ = await _make_client(db_session, "pagination-client")
    await db_session.commit()
    for _ in range(3):
        await _create_job(db_session, owner, active_config, category_code="PAGINATION_TEST")

    resp = await client.get(
        "/api/v2/jobs",
        headers={"X-API-Key": ops_key},
        params={"category_code": "PAGINATION_TEST", "page_size": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["jobs"]) == 2
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 2


async def test_list_jobs_includes_job_with_null_category_code(
    client: AsyncClient, ops_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    """The specific bug this phase's own reality check found — a background/
    RECOLOR/MIX job has category_code: NULL, and JobSummary used to be typed
    non-optional for it."""
    owner, _ = await _make_client(db_session, "null-category-client")
    await db_session.commit()
    job_id = await _create_job(
        db_session,
        owner,
        active_config,
        operation=Operation.RECOLOR,
        category_code=None,
    )

    resp = await client.get("/api/v2/jobs", headers={"X-API-Key": ops_key})
    assert resp.status_code == 200, resp.text
    matching = [j for j in resp.json()["jobs"] if j["job_id"] == str(job_id)]
    assert len(matching) == 1
    assert matching[0]["category_code"] is None
    assert matching[0]["operation"] == "RECOLOR"


# --- GET /jobs/{job_id}/cost --------------------------------------------


async def test_get_job_cost_requires_ops_scope(client: AsyncClient, client_scope_key: str) -> None:
    resp = await client.get(
        f"/api/v2/jobs/{uuid.uuid4()}/cost", headers={"X-API-Key": client_scope_key}
    )
    assert resp.status_code == 403, resp.text


async def test_get_job_cost_unknown_job_404(client: AsyncClient, ops_key: str) -> None:
    resp = await client.get(f"/api/v2/jobs/{uuid.uuid4()}/cost", headers={"X-API-Key": ops_key})
    assert resp.status_code == 404, resp.text


async def test_get_job_cost_with_no_events_returns_zero(
    client: AsyncClient, ops_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    owner, _ = await _make_client(db_session, "no-cost-events-client")
    await db_session.commit()
    job_id = await _create_job(db_session, owner, active_config)

    resp = await client.get(f"/api/v2/jobs/{job_id}/cost", headers={"X-API-Key": ops_key})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_cost_usd"] == 0
    assert body["events"] == []


async def test_get_job_cost_sums_events_and_includes_failed_calls(
    client: AsyncClient, ops_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    owner, _ = await _make_client(db_session, "cost-sum-client")
    await db_session.commit()
    job_id = await _create_job(db_session, owner, active_config)
    sub_job = jobs_repo.create_sub_job(
        db_session,
        job_id=job_id,
        angle=None,
        status=SubJobStatus.FAILED,
        source_type=SourceType.UPLOADED,
    )
    await db_session.flush()

    from app.services import cost_service

    # First attempt: a failed/refused call — still billed, must still count.
    cost_service.record_cost_event(
        db_session,
        job_id=job_id,
        sub_job_id=sub_job.id,
        provider="gemini",
        operation="image_generation",
        model_version="gemini-3.1-flash-image",
        unit_cost_usd=0.02,
    )
    # Second attempt on the same sub-job: succeeded.
    cost_service.record_cost_event(
        db_session,
        job_id=job_id,
        sub_job_id=sub_job.id,
        provider="gemini",
        operation="image_generation",
        model_version="gemini-3.1-flash-image",
        unit_cost_usd=0.02,
    )
    await db_session.commit()

    resp = await client.get(f"/api/v2/jobs/{job_id}/cost", headers={"X-API-Key": ops_key})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == str(job_id)
    assert body["total_cost_usd"] == pytest.approx(0.04)
    assert len(body["events"]) == 2
    # Derived attempt_count, in call order — not a stored column.
    attempt_counts = [e["attempt_count"] for e in body["events"]]
    assert attempt_counts == [1, 2]


async def test_get_job_cost_attributes_events_to_the_right_angle(
    client: AsyncClient, ops_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    from app.db.models.enums import Angle

    owner, _ = await _make_client(db_session, "multi-angle-cost-client")
    await db_session.commit()
    job_id = await _create_job(db_session, owner, active_config)
    front_sub_job = jobs_repo.create_sub_job(
        db_session,
        job_id=job_id,
        angle=Angle.FRONT,
        status=SubJobStatus.COMPLETED,
        source_type=SourceType.UPLOADED,
    )
    side_sub_job = jobs_repo.create_sub_job(
        db_session,
        job_id=job_id,
        angle=Angle.SIDE,
        status=SubJobStatus.COMPLETED,
        source_type=SourceType.UPLOADED,
    )
    await db_session.flush()

    from app.services import cost_service

    cost_service.record_cost_event(
        db_session,
        job_id=job_id,
        sub_job_id=front_sub_job.id,
        provider="gemini",
        operation="image_generation",
        model_version="gemini-3.1-flash-image",
        unit_cost_usd=0.02,
    )
    cost_service.record_cost_event(
        db_session,
        job_id=job_id,
        sub_job_id=side_sub_job.id,
        provider="gemini",
        operation="image_generation",
        model_version="gemini-3.1-flash-image",
        unit_cost_usd=0.02,
    )
    await db_session.commit()

    resp = await client.get(f"/api/v2/jobs/{job_id}/cost", headers={"X-API-Key": ops_key})
    assert resp.status_code == 200, resp.text
    by_angle = {e["angle"]: e for e in resp.json()["events"]}
    assert set(by_angle.keys()) == {"FRONT", "SIDE"}
