"""Runs a single sub-job's generation call end-to-end. See docs/ai-integration.md
Call Site 1 and phases/phase-6-generation-worker.md / phase-7-orchestration.md.
Testable directly against testcontainers Postgres + real local Redis, without
going through Celery — see app/workers/generation.py for the thin task wrapper
(same split as app/services/retention_service.py / app/workers/retention.py in
Phase 4).

Retry policy: up to MAX_ATTEMPTS calls to the provider, in-process — not
Celery's own task-level retry/backoff. `docs/business-rules.md` §4 asks for
"3 attempts, exponential + jitter" for transient classes; doing that as a
tight in-service loop (bounded, deterministic, no real sleep) is
observably equivalent for this phase's scope — attempt_count still reflects
every attempt, and the sub-job still lands on FAILED only once the budget is
exhausted — and it is far more testable under `task_always_eager` than
timing real Celery backoff. This is a deliberate simplification, called out
here and in the phase file's self-audit rather than left implicit.

Parent-status recompute (Phase 7): after writing its own sub-job's terminal
status, this function recomputes and persists the parent job's status in the
same transaction — see docs/conventions.md ("Parent status recomputation and
the sub-job transition that triggered it happen in the same transaction")
and phases/phase-7-orchestration.md's reality-check section for why this
replaced a chord-based rollup.
"""

import random
import uuid
from datetime import UTC, datetime

import httpx
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import ProviderError
from app.db.models.enums import AssetKind, FailureClass, SourceType, SubJobStatus
from app.db.models.jobs import Job, SubJob
from app.db.repositories import assets as assets_repo
from app.db.repositories import config_versions as config_versions_repo
from app.db.repositories import job_events as job_events_repo
from app.db.repositories import jobs as jobs_repo
from app.providers.base import GenerationResult
from app.providers.gemini import GeminiProvider
from app.services import cost_service, storage_service
from app.services.job_service import find_category
from app.services.rate_limiter import acquire as acquire_rate_limit
from app.services.status_rollup import TERMINAL_STATUSES, compute_parent_status

logger = structlog.get_logger()

MAX_ATTEMPTS = 3
_RETRYABLE_CLASSES = {
    FailureClass.RATE_LIMITED,
    FailureClass.TRANSIENT_PROVIDER,
    FailureClass.TRANSIENT_NETWORK,
}


def fetch_reference_images(urls: list[str]) -> list[bytes]:
    """Real HTTP fetch of a category's reference_image_urls (Mode B). A seam
    tests monkeypatch — the seeded config's reference URLs are placeholders
    (`https://example.com/...`) that don't resolve to anything real.
    """
    images = []
    for url in urls:
        try:
            resp = httpx.get(url, timeout=10.0)
            resp.raise_for_status()
            images.append(resp.content)
        except httpx.HTTPError:
            logger.warning("reference_image_fetch_failed", url=url)
    return images


class SubJobNotFoundError(Exception):
    pass


async def transform_photo(
    session: AsyncSession, redis_client: Redis, sub_job_id: uuid.UUID
) -> SubJob:
    sub_job = await jobs_repo.get_sub_job_by_id(session, sub_job_id)
    if sub_job is None:
        raise SubJobNotFoundError(f"SubJob {sub_job_id} not found.")

    job = await jobs_repo.get_by_id(session, sub_job.job_id)
    if job is None:
        raise SubJobNotFoundError(f"Job {sub_job.job_id} for sub-job {sub_job_id} not found.")

    # docs/business-rules.md §2: PENDING -> GENERATING -> terminal. Committed
    # immediately so a concurrent GET /status mid-call sees GENERATING, not
    # a stale PENDING — see phases/phase-7-orchestration.md Step 1.
    sub_job.status = SubJobStatus.GENERATING
    sub_job.started_at = datetime.now(UTC)
    await session.commit()

    config_version = await config_versions_repo.get_by_id(session, job.config_version_id)
    if config_version is None:
        raise SubJobNotFoundError(f"Pinned config version {job.config_version_id} not found.")

    category = find_category(config_version, job.category_code)
    if category is None:
        raise SubJobNotFoundError(f"Category {job.category_code} not found in pinned config.")

    angle_config = category["angles"][sub_job.angle.value]
    prompt = (
        f"{angle_config['prompt']} {config_version.payload['global']['default_negative_prompt']}"
    )
    model_version = config_version.payload["global"]["model_version"]
    unit_cost_usd = config_version.payload["global"]["unit_cost_usd"]

    if sub_job.source_type == SourceType.UPLOADED:
        input_asset = (
            await assets_repo.get_by_id(session, sub_job.input_asset_id)
            if sub_job.input_asset_id is not None
            else None
        )
        if input_asset is None:
            reference_images: list[bytes] = []
        else:
            local_path = storage_service.download_to_temp(
                input_asset.bucket, input_asset.storage_path
            )
            reference_images = [local_path.read_bytes()]
    else:
        reference_images = fetch_reference_images(angle_config.get("reference_image_urls", []))

    provider = GeminiProvider(model_version=model_version)
    seed = random.randint(0, 2**31 - 1)

    last_error: ProviderError | None = None
    while sub_job.attempt_count < MAX_ATTEMPTS:
        sub_job.attempt_count += 1

        allowed = await acquire_rate_limit(redis_client)
        if not allowed:
            # Blocked by our own local bucket before any request left this
            # process — no provider call happened, so no cost event either.
            last_error = ProviderError(
                "Gemini rate limit window exhausted.", failure_class=FailureClass.RATE_LIMITED
            )
            if last_error.failure_class not in _RETRYABLE_CLASSES:
                break
            continue

        try:
            result = provider.generate(prompt, reference_images, seed)
        except ProviderError as exc:
            last_error = exc
            # docs/business-rules.md §10: cost is recorded before the result
            # is evaluated further — including calls that end in refusal.
            cost_service.record_cost_event(
                session,
                job_id=job.id,
                sub_job_id=sub_job.id,
                provider="gemini",
                operation="image_generation",
                model_version=model_version,
                unit_cost_usd=unit_cost_usd,
            )
            if last_error.failure_class not in _RETRYABLE_CLASSES:
                break
            continue

        cost_service.record_cost_event(
            session,
            job_id=job.id,
            sub_job_id=sub_job.id,
            provider="gemini",
            operation="image_generation",
            model_version=result.model_version,
            unit_cost_usd=unit_cost_usd,
        )
        await _complete_success(session, job.id, sub_job, result, prompt, seed)
        await _recompute_parent_status(session, job)
        return sub_job

    assert last_error is not None
    _fail(sub_job, last_error, prompt, seed)
    await _recompute_parent_status(session, job)
    return sub_job


async def _recompute_parent_status(session: AsyncSession, job: Job) -> None:
    """Recomputes and persists the parent job's status from its sub-jobs'
    current state, in the same transaction as the sub-job write that
    triggered it — see docs/conventions.md and phases/phase-7-orchestration.md
    Step 2.
    """
    sub_jobs = await jobs_repo.get_sub_jobs(session, job.id)
    non_skipped = [sj for sj in sub_jobs if sj.status != SubJobStatus.SKIPPED]
    requested = len(non_skipped)
    succeeded = sum(1 for sj in non_skipped if sj.status == SubJobStatus.COMPLETED)
    failed = sum(
        1 for sj in non_skipped if sj.status in (SubJobStatus.FAILED, SubJobStatus.REJECTED)
    )

    from_status = job.status
    new_status = compute_parent_status(requested, succeeded, failed)

    job.status = new_status
    job.succeeded_angles = succeeded
    job.failed_angles = failed
    job.completed_at = datetime.now(UTC) if new_status in TERMINAL_STATUSES else None

    job_events_repo.record_event(
        session,
        job.id,
        "SUBJOB_STATUS_CHANGE",
        from_status=from_status.value if from_status != new_status else None,
        to_status=new_status.value,
        detail={"requested": requested, "succeeded": succeeded, "failed": failed},
    )


async def _complete_success(
    session: AsyncSession,
    job_id: uuid.UUID,
    sub_job: SubJob,
    result: GenerationResult,
    prompt: str,
    seed: int,
) -> None:
    ext = "png" if result.mime_type == "image/png" else "jpg"
    storage_path = storage_service.build_storage_path(
        job_id, sub_job.angle.value, AssetKind.OUTPUT, ext
    )
    storage_service.upload_bytes(
        settings.BUCKET_OUTPUTS, storage_path, result.image_bytes, result.mime_type
    )
    output_asset = assets_repo.create_asset(
        session,
        job_id=job_id,
        sub_job_id=sub_job.id,
        kind=AssetKind.OUTPUT,
        bucket=settings.BUCKET_OUTPUTS,
        storage_path=storage_path,
        mime_type=result.mime_type,
        bytes_=len(result.image_bytes),
    )
    await session.flush()

    sub_job.output_asset_id = output_asset.id
    sub_job.prompt_snapshot = prompt
    sub_job.model_version = result.model_version
    sub_job.seed = seed
    sub_job.status = (
        SubJobStatus.COMPLETED
        if sub_job.source_type == SourceType.UPLOADED
        else SubJobStatus.QA_REVIEW
    )


def _fail(sub_job: SubJob, error: ProviderError, prompt: str, seed: int) -> None:
    sub_job.prompt_snapshot = prompt
    sub_job.seed = seed
    sub_job.failure_class = FailureClass(error.failure_class)
    sub_job.error_message = error.message
    sub_job.status = (
        SubJobStatus.REJECTED
        if error.failure_class == FailureClass.SAFETY_REFUSAL
        else SubJobStatus.FAILED
    )
