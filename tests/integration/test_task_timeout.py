"""Phase 16 Step 1 Checkpoint — a task that exceeds its worker timeout lands
the sub-job on FAILED/INTERNAL with a job_events row naming the timeout, and
the parent job status recomputes correctly, instead of hanging forever or
raising an uncaught exception. Real testcontainers Postgres — see
tests/integration/test_generation_worker.py for the sibling "real provider
failure" coverage this mirrors.

Two layers, matching app/services/generation_service.py::mark_sub_job_timed_out
and the worker task wrappers that call it:
1. Service-level: mark_sub_job_timed_out directly.
2. Task-wrapper-level: transform_photo_task/process_task actually catch the
   timeout and route to it — `_run` is monkeypatched to raise immediately
   rather than actually waiting out WORKER_TASK_TIMEOUT_SECONDS (180s),
   matching this repo's own "short override" testing convention (see
   phases/phase-16-stability-closeout.md Step 1).
"""

import uuid
from datetime import UTC, datetime

import pytest
from argon2 import PasswordHasher
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import (
    Angle,
    FailureClass,
    JobStatus,
    SourceType,
    SubJobStatus,
    SyncStatus,
)
from app.db.models.job_events import JobEvent
from app.db.models.jobs import Job, SubJob
from app.services import generation_service
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()


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
    db_session: AsyncSession, config_version: ConfigVersion, requested_angles: int = 1
) -> Job:
    client = ApiClient(
        name="timeout-test-client",
        key_prefix=uuid.uuid4().hex[:8],
        key_hash=_hasher.hash("unused"),
        scope="client",
    )
    db_session.add(client)
    await db_session.flush()

    job = Job(
        client_id=client.id,
        idempotency_key=f"timeout-test-{uuid.uuid4()}",
        payload_hash="h",
        category_code="RING",
        config_version_id=config_version.id,
        status=JobStatus.PROCESSING,
        requested_angles=requested_angles,
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def test_mark_sub_job_timed_out_fails_generating_sub_job(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    job = await _make_job(db_session, active_config)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.GENERATING,
        source_type=SourceType.UPLOADED,
        started_at=datetime.now(UTC),
    )
    db_session.add(sub_job)
    await db_session.commit()

    result = await generation_service.mark_sub_job_timed_out(db_session, sub_job.id)
    await db_session.commit()

    assert result.status == SubJobStatus.FAILED
    assert result.failure_class == FailureClass.INTERNAL
    assert result.error_message is not None
    assert "timeout" in result.error_message.lower()

    events = (
        (
            await db_session.execute(
                select(JobEvent).where(
                    JobEvent.sub_job_id == sub_job.id, JobEvent.event_type == "TASK_TIMEOUT"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].to_status == "FAILED"

    await db_session.refresh(job)
    assert job.status == JobStatus.FAILED


async def test_mark_sub_job_timed_out_fails_pending_sub_job(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    """A never-dispatched PENDING sub-job (worker OOM-killed before it even
    started, or lost between accept and dispatch) is exactly what this
    guards, not only a mid-GENERATING hang.
    """
    job = await _make_job(db_session, active_config)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
    )
    db_session.add(sub_job)
    await db_session.commit()

    result = await generation_service.mark_sub_job_timed_out(db_session, sub_job.id)
    await db_session.commit()

    assert result.status == SubJobStatus.FAILED
    assert result.failure_class == FailureClass.INTERNAL


async def test_mark_sub_job_timed_out_is_noop_for_terminal_status(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    """Defensive guard against the narrow race where the real call actually
    finished and committed right as the timeout fired — must not clobber a
    real COMPLETED result.
    """
    job = await _make_job(db_session, active_config)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.COMPLETED,
        source_type=SourceType.UPLOADED,
    )
    db_session.add(sub_job)
    await db_session.commit()

    result = await generation_service.mark_sub_job_timed_out(db_session, sub_job.id)
    await db_session.commit()

    assert result.status == SubJobStatus.COMPLETED
    assert result.failure_class is None


async def test_partial_success_when_one_angle_times_out_and_rest_complete(
    db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    """docs/business-rules.md §3's rollup rule, exercised via a timeout
    instead of a provider failure — a job with one timed-out sub-job and the
    rest COMPLETED lands on PARTIAL_SUCCESS, matching Checkpoint 1's ask.
    """
    job = await _make_job(db_session, active_config, requested_angles=2)
    timed_out = SubJob(
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
    db_session.add_all([timed_out, completed])
    await db_session.commit()

    await generation_service.mark_sub_job_timed_out(db_session, timed_out.id)
    await db_session.commit()
    await db_session.refresh(job)

    assert job.status == JobStatus.PARTIAL_SUCCESS


async def test_generation_task_wrapper_routes_timeout_error_to_failure(
    db_session: AsyncSession, active_config: ConfigVersion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual enforcement mechanism under `--pool=solo` — asyncio.wait_for
    raising TimeoutError — reaches the same failure path as a caught
    SoftTimeLimitExceeded would under prefork. `_run` is monkeypatched to
    raise immediately rather than this test actually waiting out
    WORKER_TASK_TIMEOUT_SECONDS.
    """
    job = await _make_job(db_session, active_config)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.GENERATING,
        source_type=SourceType.UPLOADED,
        started_at=datetime.now(UTC),
    )
    db_session.add(sub_job)
    await db_session.commit()

    import app.workers.generation as generation_worker

    async def _hung(sub_job_id: str) -> str:
        raise TimeoutError

    monkeypatch.setattr(generation_worker, "_run", _hung)

    status = generation_worker.transform_photo_task(str(sub_job.id))

    assert status == SubJobStatus.FAILED.value

    await db_session.refresh(sub_job)
    assert sub_job.status == SubJobStatus.FAILED
    assert sub_job.failure_class == FailureClass.INTERNAL


async def test_generation_task_wrapper_routes_soft_time_limit_exceeded_to_failure(
    db_session: AsyncSession, active_config: ConfigVersion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as above but for Celery's own SoftTimeLimitExceeded — caught for
    free so this keeps working unmodified if the pool is ever switched back
    to prefork, where it would actually fire.
    """
    job = await _make_job(db_session, active_config)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.GENERATING,
        source_type=SourceType.UPLOADED,
        started_at=datetime.now(UTC),
    )
    db_session.add(sub_job)
    await db_session.commit()

    import app.workers.generation as generation_worker

    async def _hung(sub_job_id: str) -> str:
        raise SoftTimeLimitExceeded

    monkeypatch.setattr(generation_worker, "_run", _hung)

    status = generation_worker.transform_photo_task(str(sub_job.id))

    assert status == SubJobStatus.FAILED.value


async def test_background_task_wrapper_routes_timeout_error_to_failure(
    db_session: AsyncSession, active_config: ConfigVersion, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models.enums import Operation

    job = await _make_job(db_session, active_config)
    job.operation = Operation.BACKGROUND_REMOVAL
    job.category_code = None
    sub_job = SubJob(
        job_id=job.id,
        angle=None,
        status=SubJobStatus.GENERATING,
        source_type=SourceType.UPLOADED,
        started_at=datetime.now(UTC),
    )
    db_session.add(sub_job)
    await db_session.commit()

    import app.workers.background as background_worker

    async def _hung(sub_job_id: str) -> str:
        raise TimeoutError

    monkeypatch.setattr(background_worker, "_run", _hung)

    status = background_worker.process_task(str(sub_job.id))

    assert status == SubJobStatus.FAILED.value

    await db_session.refresh(sub_job)
    assert sub_job.failure_class == FailureClass.INTERNAL
