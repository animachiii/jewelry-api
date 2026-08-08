"""Phase 6 Checkpoint 3 — cost event recording."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.cost_events import CostEvent
from app.db.models.enums import JobStatus, SyncStatus
from app.db.models.jobs import Job
from app.services.cost_service import record_cost_event

pytestmark = pytest.mark.integration


async def _make_job(session: AsyncSession) -> Job:
    client = ApiClient(name="cost-test-client", key_prefix="costtest", key_hash="x", scope="client")
    session.add(client)
    cv = ConfigVersion(
        version_number=1,
        source_hash="h",
        payload={"categories": [], "global": {}},
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
    )
    session.add(cv)
    await session.flush()

    job = Job(
        client_id=client.id,
        idempotency_key="cost-test",
        payload_hash="h",
        category_code="RING",
        config_version_id=cv.id,
        status=JobStatus.PROCESSING,
        requested_angles=1,
    )
    session.add(job)
    await session.flush()
    return job


async def test_record_cost_event_computes_total(db_session: AsyncSession) -> None:
    job = await _make_job(db_session)

    record_cost_event(
        db_session,
        job_id=job.id,
        sub_job_id=None,
        provider="gemini",
        operation="image_generation",
        model_version="gemini-2.5-flash-image-preview",
        unit_cost_usd=0.02,
        units=1,
    )
    await db_session.commit()

    events = (
        (await db_session.execute(select(CostEvent).where(CostEvent.job_id == job.id)))
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert float(events[0].total_cost_usd) == 0.02


async def test_cost_event_recorded_even_for_refused_call(db_session: AsyncSession) -> None:
    """docs/business-rules.md §10: a refused generation is still billed."""
    job = await _make_job(db_session)

    record_cost_event(
        db_session,
        job_id=job.id,
        sub_job_id=None,
        provider="gemini",
        operation="image_generation",
        model_version="gemini-2.5-flash-image-preview",
        unit_cost_usd=0.02,
    )
    await db_session.commit()

    events = (
        (await db_session.execute(select(CostEvent).where(CostEvent.job_id == job.id)))
        .scalars()
        .all()
    )
    assert len(events) == 1
