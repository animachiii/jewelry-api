"""Verifies scripts/seed_dev.py against docs/business-rules.md §3.

Uses testcontainers Postgres, not the local dev DB — no shared dev
database, per docs/conventions.md.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Asset, ConfigVersion, Job, SubJob
from app.db.models.enums import JobStatus, SubJobStatus
from scripts.seed_dev import main as seed_main

pytestmark = pytest.mark.integration


def compute_parent_status(requested: int, succeeded: int, failed: int) -> JobStatus:
    """docs/business-rules.md §3."""
    if succeeded + failed < requested:
        return JobStatus.PROCESSING
    if succeeded == requested:
        return JobStatus.COMPLETED
    if failed == requested:
        return JobStatus.FAILED
    return JobStatus.PARTIAL_SUCCESS


class _NoCloseSessionCM:
    """Wraps an already-open session so seed_dev's `async with` doesn't close it —
    tests keep querying the same session after calling seed_main()."""

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


@pytest.mark.asyncio
async def test_seed_produces_all_eight_scenarios(db_session: AsyncSession) -> None:
    await seed_main()

    result = await db_session.execute(select(Job).where(Job.idempotency_key.like("seed-%")))
    jobs = {j.idempotency_key: j for j in result.scalars().all()}
    assert len(jobs) == 8

    for job in jobs.values():
        sub_jobs = (
            (
                await db_session.execute(
                    select(SubJob).where(
                        SubJob.job_id == job.id, SubJob.status != SubJobStatus.SKIPPED
                    )
                )
            )
            .scalars()
            .all()
        )
        succeeded = sum(1 for sj in sub_jobs if sj.status == SubJobStatus.COMPLETED)
        failed = sum(
            1 for sj in sub_jobs if sj.status in (SubJobStatus.FAILED, SubJobStatus.REJECTED)
        )
        # R = non-skipped sub-jobs only (docs/business-rules.md §3) — `sub_jobs`
        # above is already filtered to exclude SKIPPED, so its length *is* R.
        requested = len(sub_jobs)
        expected = compute_parent_status(requested, succeeded, failed)
        assert job.status == expected, (
            f"{job.idempotency_key}: expected {expected}, got {job.status}"
        )


@pytest.mark.asyncio
async def test_single_angle_failure_is_failed_not_partial(db_session: AsyncSession) -> None:
    await seed_main()
    job = (
        await db_session.execute(
            select(Job).where(Job.idempotency_key == "seed-single-angle-failed")
        )
    ).scalar_one()
    assert job.status == JobStatus.FAILED
    assert job.requested_angles == 1


@pytest.mark.asyncio
async def test_two_skipped_job_is_completed_with_requested_angles_two(
    db_session: AsyncSession,
) -> None:
    await seed_main()
    job = (
        await db_session.execute(select(Job).where(Job.idempotency_key == "seed-two-skipped"))
    ).scalar_one()
    assert job.status == JobStatus.COMPLETED
    assert job.requested_angles == 2


@pytest.mark.asyncio
async def test_qa_review_job_stays_processing(db_session: AsyncSession) -> None:
    await seed_main()
    job = (
        await db_session.execute(select(Job).where(Job.idempotency_key == "seed-qa-review"))
    ).scalar_one()
    assert job.status == JobStatus.PROCESSING


@pytest.mark.asyncio
async def test_exactly_one_active_config_version(db_session: AsyncSession) -> None:
    await seed_main()
    active = (
        (await db_session.execute(select(ConfigVersion).where(ConfigVersion.is_active)))
        .scalars()
        .all()
    )
    assert len(active) == 1


@pytest.mark.asyncio
async def test_at_least_one_expired_asset(db_session: AsyncSession) -> None:
    from datetime import UTC, datetime

    await seed_main()
    assets = (await db_session.execute(select(Asset))).scalars().all()
    assert any(a.expires_at is not None and a.expires_at < datetime.now(UTC) for a in assets)
