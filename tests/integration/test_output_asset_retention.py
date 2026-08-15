"""Phase 16 Step 4 regression test — a completed OUTPUT asset now gets a
real `expires_at`, matching `RETENTION_DAYS[AssetKind.OUTPUT]`. Before this
fix, `_complete_success` in both generation_service.py and
background_service.py never passed `expires_at` to `create_asset` at all,
so every OUTPUT asset was NULL regardless of the retention policy value —
found while defaulting that value away from indefinite (Step 4), not
anticipated by the phase file.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as real_redis
from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.api_clients import ApiClient
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import Angle, AssetKind, JobStatus, SourceType, SubJobStatus, SyncStatus
from app.db.models.jobs import Job, SubJob
from app.db.repositories import assets as assets_repo
from app.services import generation_service, storage_service
from app.services.retention_policy import RETENTION_DAYS
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()


@pytest.fixture
async def redis_client() -> real_redis.Redis:
    client = real_redis.from_url(settings.REDIS_URL, decode_responses=True)  # type: ignore[no-untyped-call]
    yield client
    await client.aclose()


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


async def test_completed_output_asset_gets_expires_at(
    db_session: AsyncSession, redis_client: real_redis.Redis, active_config: ConfigVersion
) -> None:
    client = ApiClient(
        name="retention-test-client",
        key_prefix=uuid.uuid4().hex[:8],
        key_hash=_hasher.hash("unused"),
        scope="client",
    )
    db_session.add(client)
    await db_session.flush()

    job = Job(
        client_id=client.id,
        idempotency_key=f"retention-test-{uuid.uuid4()}",
        payload_hash="h",
        category_code="RING",
        config_version_id=active_config.id,
        status=JobStatus.PROCESSING,
        requested_angles=1,
    )
    db_session.add(job)
    await db_session.flush()

    storage_path = storage_service.build_storage_path(job.id, "FRONT", AssetKind.INPUT, "jpg")
    storage_service.upload_bytes(
        settings.BUCKET_INPUTS, storage_path, b"\xff\xd8\xff\xd9", "image/jpeg"
    )
    input_asset = assets_repo.create_asset(
        db_session,
        job_id=job.id,
        kind=AssetKind.INPUT,
        bucket=settings.BUCKET_INPUTS,
        storage_path=storage_path,
        mime_type="image/jpeg",
    )
    await db_session.flush()

    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        input_asset_id=input_asset.id,
    )
    db_session.add(sub_job)
    await db_session.commit()

    before = datetime.now(UTC)
    result_sub_job = await generation_service.transform_photo(db_session, redis_client, sub_job.id)
    await db_session.commit()

    assert result_sub_job.output_asset_id is not None
    output_asset = await assets_repo.get_by_id(db_session, result_sub_job.output_asset_id)
    assert output_asset is not None
    assert output_asset.expires_at is not None

    expected_days = RETENTION_DAYS[AssetKind.OUTPUT]
    assert expected_days is not None
    expected = before + timedelta(days=expected_days)
    assert abs((output_asset.expires_at - expected).total_seconds()) < 60
