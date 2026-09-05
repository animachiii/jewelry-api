"""Schema additions for GENERATE_WITH_CLEANUP: the operation_t enum value
and jobs.requested_angle_codes, added so the worker can learn which angles
to build after the cleanup sub-job commits (the request body is long gone
by then) — the same reason migration 0009 added jobs.preset_code. See
docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md section 3.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import Operation, SyncStatus
from app.db.models.jobs import Job
from app.db.repositories import jobs as jobs_repo


def test_generate_with_cleanup_is_a_valid_operation() -> None:
    assert Operation.GENERATE_WITH_CLEANUP == "GENERATE_WITH_CLEANUP"


def test_job_has_requested_angle_codes_column() -> None:
    assert "requested_angle_codes" in Job.__table__.columns
    column = Job.__table__.columns["requested_angle_codes"]
    assert column.nullable is True


@pytest.mark.asyncio
async def test_create_job_accepts_requested_angle_codes(db_session: AsyncSession) -> None:
    cv = ConfigVersion(
        version_number=999999,
        source_hash="test-hash-requested-angle-codes",
        payload={"global": {"model_version": "test"}},
        sync_status=SyncStatus.SUCCESS,
        is_active=False,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.flush()

    job = jobs_repo.create_job(
        db_session,
        client_id=uuid.uuid4(),
        idempotency_key="test-key",
        payload_hash="test-hash",
        config_version_id=cv.id,
        requested_angles=2,
        sku_reference=None,
        metadata={},
        operation=Operation.GENERATE_WITH_CLEANUP,
        requested_angle_codes=["FRONT", "SIDE"],
    )
    assert job.requested_angle_codes == ["FRONT", "SIDE"]


def test_generate_with_cleanup_request_rejects_empty_angles_list_at_service_layer() -> None:
    """Not a Pydantic-level constraint on purpose -- every existing /generate-
    family validation failure uses a specific ErrorCode via the standard
    error envelope (docs/conventions.md), not a generic Pydantic 422. See
    NoAnglesRequestedError, already used by /generate for the identical rule.
    """
    from app.api.v2.schemas.generate_with_cleanup import GenerateWithCleanupRequest

    # An empty list is valid Pydantic input -- the check belongs to the
    # service layer (job_service.create_generate_with_cleanup_job_for_request),
    # exercised in the integration tests (Task 8), not here.
    request = GenerateWithCleanupRequest(
        storage_path="pending/test/x/y.jpg", category_code="RING", angles=[]
    )
    assert request.angles == []
