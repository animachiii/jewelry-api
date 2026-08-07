"""Phase 6 Checkpoint 4 — generation.transform_photo end-to-end.

Real testcontainers Postgres, real local Redis (rate limiter), real
Supabase Storage (input download + output upload) — only the Gemini call
itself is faked, via GeminiProvider._call_api monkeypatching, per
docs/ai-integration.md's "never call the live Gemini API in CI."
"""

import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import redis.asyncio as real_redis
from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.api_clients import ApiClient
from app.db.models.assets import Asset
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import (
    Angle,
    AssetKind,
    FailureClass,
    JobStatus,
    SourceType,
    SubJobStatus,
    SyncStatus,
)
from app.db.models.jobs import Job, SubJob
from app.services import generation_service, storage_service
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gemini"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
async def redis_client() -> AsyncGenerator[real_redis.Redis, None]:
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


async def _make_job(db_session: AsyncSession, config_version: ConfigVersion) -> Job:
    client = ApiClient(
        name="gen-test-client",
        key_prefix=uuid.uuid4().hex[:8],
        key_hash=_hasher.hash("unused"),
        scope="client",
    )
    db_session.add(client)
    await db_session.flush()

    job = Job(
        client_id=client.id,
        idempotency_key=f"gen-test-{uuid.uuid4()}",
        payload_hash="h",
        category_code="RING",
        config_version_id=config_version.id,
        status=JobStatus.PROCESSING,
        requested_angles=1,
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def _upload_real_input(db_session: AsyncSession, job: Job, angle: str = "FRONT") -> Asset:
    storage_path = storage_service.build_storage_path(job.id, angle, AssetKind.INPUT, "jpg")
    storage_service.upload_bytes(
        settings.BUCKET_INPUTS, storage_path, b"\xff\xd8\xff\xd9", "image/jpeg"
    )
    asset = Asset(
        job_id=job.id,
        kind=AssetKind.INPUT,
        bucket=settings.BUCKET_INPUTS,
        storage_path=storage_path,
        mime_type="image/jpeg",
    )
    db_session.add(asset)
    await db_session.flush()
    return asset


async def test_mode_a_success_completes_sub_job(
    db_session: AsyncSession, redis_client: real_redis.Redis, active_config: ConfigVersion
) -> None:
    job = await _make_job(db_session, active_config)
    input_asset = await _upload_real_input(db_session, job, "FRONT")
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        input_asset_id=input_asset.id,
    )
    db_session.add(sub_job)
    await db_session.commit()

    fixture = _load("success.json")

    import app.providers.gemini as gemini_module

    def fake_call_api(self: object, prompt: str, reference_images: list, seed: int) -> dict:
        return fixture

    orig = gemini_module.GeminiProvider._call_api
    gemini_module.GeminiProvider._call_api = fake_call_api  # type: ignore[method-assign]
    try:
        result_sub_job = await generation_service.transform_photo(
            db_session, redis_client, sub_job.id
        )
        await db_session.commit()
    finally:
        gemini_module.GeminiProvider._call_api = orig  # type: ignore[method-assign]

    assert result_sub_job.status == SubJobStatus.COMPLETED
    assert result_sub_job.prompt_snapshot is not None
    assert result_sub_job.model_version == "gemini-2.5-flash-image-preview"
    assert result_sub_job.seed is not None
    assert result_sub_job.output_asset_id is not None

    output_asset = (
        await db_session.execute(select(Asset).where(Asset.id == result_sub_job.output_asset_id))
    ).scalar_one()
    resp = httpx.get(
        storage_service.generate_signed_url(output_asset.bucket, output_asset.storage_path)
    )
    assert resp.status_code == 200
    assert len(resp.content) > 0


async def test_mode_b_success_lands_in_qa_review_not_completed(
    db_session: AsyncSession, redis_client: real_redis.Redis, active_config: ConfigVersion
) -> None:
    job = await _make_job(db_session, active_config)
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.DIAGONAL,
        status=SubJobStatus.PENDING,
        source_type=SourceType.SYNTHETIC,
    )
    db_session.add(sub_job)
    await db_session.commit()

    fixture = _load("success.json")

    import app.providers.gemini as gemini_module

    def fake_call_api(self: object, prompt: str, reference_images: list, seed: int) -> dict:
        return fixture

    def fake_fetch(urls: list[str]) -> list[bytes]:
        return []

    orig = gemini_module.GeminiProvider._call_api
    gemini_module.GeminiProvider._call_api = fake_call_api  # type: ignore[method-assign]
    orig_fetch = generation_service.fetch_reference_images
    generation_service.fetch_reference_images = fake_fetch  # type: ignore[assignment]
    try:
        result_sub_job = await generation_service.transform_photo(
            db_session, redis_client, sub_job.id
        )
        await db_session.commit()
    finally:
        gemini_module.GeminiProvider._call_api = orig  # type: ignore[method-assign]
        generation_service.fetch_reference_images = orig_fetch  # type: ignore[assignment]

    assert result_sub_job.status == SubJobStatus.QA_REVIEW
    assert result_sub_job.output_asset_id is not None


async def test_safety_refusal_rejected_no_retry(
    db_session: AsyncSession, redis_client: real_redis.Redis, active_config: ConfigVersion
) -> None:
    job = await _make_job(db_session, active_config)
    input_asset = await _upload_real_input(db_session, job, "FRONT")
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        input_asset_id=input_asset.id,
    )
    db_session.add(sub_job)
    await db_session.commit()

    fixture = _load("safety_refusal.json")

    import app.providers.gemini as gemini_module

    def fake_call_api(self: object, prompt: str, reference_images: list, seed: int) -> dict:
        return fixture

    orig = gemini_module.GeminiProvider._call_api
    gemini_module.GeminiProvider._call_api = fake_call_api  # type: ignore[method-assign]
    try:
        result_sub_job = await generation_service.transform_photo(
            db_session, redis_client, sub_job.id
        )
        await db_session.commit()
    finally:
        gemini_module.GeminiProvider._call_api = orig  # type: ignore[method-assign]

    assert result_sub_job.status == SubJobStatus.REJECTED
    assert result_sub_job.failure_class == FailureClass.SAFETY_REFUSAL
    assert result_sub_job.attempt_count == 1


async def test_transient_failure_exhausts_attempts_then_fails(
    db_session: AsyncSession, redis_client: real_redis.Redis, active_config: ConfigVersion
) -> None:
    job = await _make_job(db_session, active_config)
    input_asset = await _upload_real_input(db_session, job, "FRONT")
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        input_asset_id=input_asset.id,
    )
    db_session.add(sub_job)
    await db_session.commit()

    import app.providers.gemini as gemini_module

    def fake_call_api(self: object, prompt: str, reference_images: list, seed: int) -> dict:
        raise gemini_module.GeminiAPIError(503, "The model is overloaded.")

    orig = gemini_module.GeminiProvider._call_api
    gemini_module.GeminiProvider._call_api = fake_call_api  # type: ignore[method-assign]
    try:
        result_sub_job = await generation_service.transform_photo(
            db_session, redis_client, sub_job.id
        )
        await db_session.commit()
    finally:
        gemini_module.GeminiProvider._call_api = orig  # type: ignore[method-assign]

    assert result_sub_job.status == SubJobStatus.FAILED
    assert result_sub_job.failure_class == FailureClass.TRANSIENT_PROVIDER
    assert result_sub_job.attempt_count == generation_service.MAX_ATTEMPTS


async def test_rate_limit_exhaustion_routes_through_rate_limited_path(
    db_session: AsyncSession, redis_client: real_redis.Redis, active_config: ConfigVersion
) -> None:
    job = await _make_job(db_session, active_config)
    input_asset = await _upload_real_input(db_session, job, "FRONT")
    sub_job = SubJob(
        job_id=job.id,
        angle=Angle.FRONT,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        input_asset_id=input_asset.id,
    )
    db_session.add(sub_job)
    await db_session.commit()

    from app.services import rate_limiter

    window_key = rate_limiter._window_key()
    await redis_client.set(window_key, settings.GEMINI_RATE_LIMIT_PER_MINUTE)

    try:
        result_sub_job = await generation_service.transform_photo(
            db_session, redis_client, sub_job.id
        )
        await db_session.commit()
    finally:
        await redis_client.delete(window_key)

    assert result_sub_job.failure_class == FailureClass.RATE_LIMITED


def test_generation_task_never_imports_google_genai_at_module_level() -> None:
    import app.workers.generation as module

    assert not hasattr(module, "genai")
