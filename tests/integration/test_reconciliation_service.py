"""Phase 16 Step 2 Checkpoint — reconcile_stuck_sub_jobs against real
testcontainers Postgres. Mirrors tests/integration/test_generation_worker.py's
style for job/sub-job fixtures.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import (
    Angle,
    FailureClass,
    JobStatus,
    QAStatus,
    SourceType,
    SubJobStatus,
    SyncStatus,
)
from app.db.models.job_events import JobEvent
from app.db.models.jobs import Job, SubJob
from app.services import reconciliation_service
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()
STALE_AFTER = 600


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
    await db_session.flush()
    return cv


async def _make_job(
    db_session: AsyncSession,
    config_version: ConfigVersion,
    *,
    created_at: datetime,
    requested_angles: int = 1,
) -> Job:
    client = ApiClient(
        name="reconciliation-test-client",
        key_prefix=uuid.uuid4().hex[:8],
        key_hash=_hasher.hash("unused"),
        scope="client",
    )
    db_session.add(client)
    await db_session.flush()

    job = Job(
        client_id=client.id,
        idempotency_key=f"reconciliation-test-{uuid.uuid4()}",
        payload_hash="h",
        category_code="RING",
        config_version_id=config_version.id,
        status=JobStatus.PROCESSING,
        requested_angles=requested_angles,
        created_at=created_at,
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def test_stale_generating_sub_job_is_reconciled(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    old = datetime.now(UTC) - timedelta(seconds=STALE_AFTER * 2)
    job = await _make_job(db_session, active_config, created_at=old)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.GENERATING,
        source_type=SourceType.UPLOADED,
    )
    db_session.add(sub_job)
    await db_session.commit()

    reconciled = await reconciliation_service.reconcile_stuck_sub_jobs(
        db_session, stale_after_seconds=STALE_AFTER
    )
    await db_session.commit()

    assert reconciled == 1
    await db_session.refresh(sub_job)
    assert sub_job.status == SubJobStatus.FAILED
    assert sub_job.failure_class == FailureClass.INTERNAL

    events = (
        (
            await db_session.execute(
                select(JobEvent).where(
                    JobEvent.sub_job_id == sub_job.id, JobEvent.event_type == "RECONCILIATION_SWEEP"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1

    await db_session.refresh(job)
    assert job.status == JobStatus.FAILED


async def test_fresh_generating_sub_job_is_left_untouched(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    job = await _make_job(db_session, active_config, created_at=datetime.now(UTC))
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.GENERATING,
        source_type=SourceType.UPLOADED,
    )
    db_session.add(sub_job)
    await db_session.commit()

    reconciled = await reconciliation_service.reconcile_stuck_sub_jobs(
        db_session, stale_after_seconds=STALE_AFTER
    )
    await db_session.commit()

    assert reconciled == 0
    await db_session.refresh(sub_job)
    assert sub_job.status == SubJobStatus.GENERATING


async def test_stale_pending_sub_job_is_reconciled(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    old = datetime.now(UTC) - timedelta(seconds=STALE_AFTER * 2)
    job = await _make_job(db_session, active_config, created_at=old)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
    )
    db_session.add(sub_job)
    await db_session.commit()

    reconciled = await reconciliation_service.reconcile_stuck_sub_jobs(
        db_session, stale_after_seconds=STALE_AFTER
    )
    await db_session.commit()

    assert reconciled == 1


async def test_stale_qa_review_sub_job_is_never_touched(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    """The core correctness guard: QA_REVIEW is a legitimate, unbounded-
    duration human-review wait (docs/business-rules.md §7), not a stuck
    state. A sweep with STUCK_STATUSES wrong-scoped to include it would
    silently fail a job correctly sitting in a human's review queue.
    """
    ancient = datetime.now(UTC) - timedelta(days=30)
    job = await _make_job(db_session, active_config, created_at=ancient)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.DIAGONAL,
        status=SubJobStatus.QA_REVIEW,
        source_type=SourceType.SYNTHETIC,
        qa_status=QAStatus.FLAGGED,
    )
    db_session.add(sub_job)
    await db_session.commit()

    reconciled = await reconciliation_service.reconcile_stuck_sub_jobs(
        db_session, stale_after_seconds=STALE_AFTER
    )
    await db_session.commit()

    assert reconciled == 0
    await db_session.refresh(sub_job)
    assert sub_job.status == SubJobStatus.QA_REVIEW
    assert sub_job.failure_class is None


async def test_partial_success_when_one_stuck_sub_job_reconciled_and_rest_complete(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    old = datetime.now(UTC) - timedelta(seconds=STALE_AFTER * 2)
    job = await _make_job(db_session, active_config, created_at=old, requested_angles=2)
    stuck = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.GENERATING,
        source_type=SourceType.UPLOADED,
    )
    completed = SubJob(
        job_id=job.id,
        angle=Angle.SIDE,
        status=SubJobStatus.COMPLETED,
        source_type=SourceType.UPLOADED,
    )
    db_session.add_all([stuck, completed])
    await db_session.commit()

    await reconciliation_service.reconcile_stuck_sub_jobs(
        db_session, stale_after_seconds=STALE_AFTER
    )
    await db_session.commit()
    await db_session.refresh(job)

    assert job.status == JobStatus.PARTIAL_SUCCESS


async def test_before_cutoff_excludes_jobs_created_after_it(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    """scripts/reconcile_legacy_orphans.py's safety rail: even a stale
    sub-job must not be touched by the one-time cleanup if its job was
    created after the cutoff — it could be a legitimately long-running job,
    not a pre-fix orphan.
    """
    old = datetime.now(UTC) - timedelta(seconds=STALE_AFTER * 2)
    cutoff = datetime.now(UTC) - timedelta(seconds=STALE_AFTER)
    job = await _make_job(db_session, active_config, created_at=old)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.GENERATING,
        source_type=SourceType.UPLOADED,
    )
    db_session.add(sub_job)
    await db_session.commit()

    # job.created_at (old) < cutoff — should be reconciled.
    reconciled = await reconciliation_service.reconcile_stuck_sub_jobs(
        db_session, stale_after_seconds=0, before=cutoff
    )
    await db_session.commit()
    assert reconciled == 1


async def test_before_cutoff_leaves_recent_job_untouched(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    ancient_enough_to_be_stale = datetime.now(UTC) - timedelta(seconds=STALE_AFTER * 2)
    job = await _make_job(db_session, active_config, created_at=ancient_enough_to_be_stale)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.GENERATING,
        source_type=SourceType.UPLOADED,
    )
    db_session.add(sub_job)
    await db_session.commit()

    # cutoff is before this job's created_at -> must be excluded even though stale.
    cutoff = job.created_at - timedelta(seconds=1)
    reconciled = await reconciliation_service.reconcile_stuck_sub_jobs(
        db_session, stale_after_seconds=0, before=cutoff
    )
    await db_session.commit()

    assert reconciled == 0
    await db_session.refresh(sub_job)
    assert sub_job.status == SubJobStatus.GENERATING
