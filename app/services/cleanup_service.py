"""Runs the cleanup phase of a GENERATE_WITH_CLEANUP sub-job — the first of
its two phases. See docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md.

Mirrors app/services/background_service.py::process almost exactly (same
rate-limit -> provider call -> cost event -> success/fail shape, same
Gemini call, even the same prompt at rest — this migration 0022 seeds it
as BACKGROUND_REMOVAL's own prompt text, copied verbatim). Kept as a
separate module rather than importing background_service's private
helpers, following this codebase's own precedent (recolor_service.py/
mix_service.py/match_service.py each reimplement rather than share).

The one real difference, and the reason this can't just be
background_service with a parameter: success goes straight to COMPLETED,
never QA_REVIEW. A standalone BACKGROUND_REMOVAL sub-job's QA gate exists
because "the cutout *is* the product" (docs/business-rules.md §13) — here
the cleaned photo is never the product; it's consumed internally by the
angle-generation phase this sub-job's caller triggers next. Same posture
Mode A real-photo angles already have (no QA gate at all).

Never dispatches phase 2's Celery tasks itself. `app/workers/cleanup.py`
does that, after this function's caller commits — same "dispatch from the
worker layer, never the service" rule Phase 9 established for
qa.score_similarity, so the actual `.delay()` calls never race the
creating transaction. The angle sub-job *rows* themselves, however, ARE
created here, inside the same transaction as the cleanup sub-job's
COMPLETED write — see `_create_angle_sub_jobs` below for why: creating them
in a later, separate transaction (the original design) left a real window
where the job had only its cleanup sub-job, `recompute_parent_status` saw
`requested == succeeded == 1` and stamped the job terminal `COMPLETED`
before a single angle existed, client-visible during that window and
permanent if the worker crashed inside it.
"""

import random
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import ProviderError
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import (
    Angle,
    AssetKind,
    FailureClass,
    JobStatus,
    Operation,
    SourceType,
    SubJobStatus,
)
from app.db.models.jobs import Job, SubJob
from app.db.repositories import assets as assets_repo
from app.db.repositories import config_versions as config_versions_repo
from app.db.repositories import jobs as jobs_repo
from app.providers.base import GenerationResult
from app.providers.gemini import GeminiProvider
from app.services import cost_service, retention_policy, storage_service
from app.services.generation_service import recompute_parent_status
from app.services.job_service import (
    find_operation_config,
    resolve_operation_unit_cost,
    validate_operation_angle_consistency,
)
from app.services.rate_limiter import acquire as acquire_rate_limit

# Mirrors background_service.py's own MAX_ATTEMPTS/_RETRYABLE_CLASSES rather
# than importing them — same "two independent constants that happen to
# share a value" precedent this codebase already established.
MAX_ATTEMPTS = 3
_RETRYABLE_CLASSES = {
    FailureClass.RATE_LIMITED,
    FailureClass.TRANSIENT_PROVIDER,
    FailureClass.TRANSIENT_NETWORK,
}

_COST_OPERATION_LABEL = "generate_with_cleanup_cleanup_step"


class SubJobNotFoundError(Exception):
    pass


def _resolve_prompt(config_version: ConfigVersion) -> str:
    op_config = find_operation_config(config_version, Operation.GENERATE_WITH_CLEANUP) or {}
    return str(op_config.get("prompt", ""))


async def process(
    session: AsyncSession, redis_client: Redis, sub_job_id: uuid.UUID
) -> tuple[SubJob, list[uuid.UUID]]:
    """Returns the cleanup sub-job and the IDs of any angle sub-jobs created
    as a side effect of its completion (empty unless it just succeeded).
    The caller (`app/workers/cleanup.py::_run`) still owns dispatching
    `generation.transform_photo_task` for each ID, from its own sync body
    after this coroutine has returned — see this module's docstring.
    """
    sub_job = await jobs_repo.get_sub_job_by_id(session, sub_job_id)
    if sub_job is None:
        raise SubJobNotFoundError(f"SubJob {sub_job_id} not found.")

    job = await jobs_repo.get_by_id(session, sub_job.job_id)
    if job is None:
        raise SubJobNotFoundError(f"Job {sub_job.job_id} for sub-job {sub_job_id} not found.")

    # cleanup.process only ever runs for the one angle-less cleanup sub-job
    # of a GENERATE_WITH_CLEANUP job -- the angle sub-jobs it later creates
    # stay on generation.transform_photo, same as any ordinary angle job.
    assert job.operation == Operation.GENERATE_WITH_CLEANUP
    assert sub_job.angle is None

    if job.status == JobStatus.PENDING:
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(UTC)
    sub_job.status = SubJobStatus.GENERATING
    sub_job.started_at = datetime.now(UTC)
    await session.commit()

    config_version = await config_versions_repo.get_by_id(session, job.config_version_id)
    if config_version is None:
        raise SubJobNotFoundError(f"Pinned config version {job.config_version_id} not found.")

    prompt = _resolve_prompt(config_version)
    model_version = config_version.payload["global"]["model_version"]
    unit_cost_usd = resolve_operation_unit_cost(config_version, job.operation)

    input_asset = (
        await assets_repo.get_by_id(session, sub_job.input_asset_id)
        if sub_job.input_asset_id is not None
        else None
    )
    if input_asset is None:
        raise SubJobNotFoundError(f"No input asset for sub-job {sub_job_id}.")
    reference_images = [
        storage_service.download_bytes(input_asset.bucket, input_asset.storage_path)
    ]

    provider = GeminiProvider(model_version=model_version)
    seed = random.randint(0, 2**31 - 1)

    last_error: ProviderError | None = None
    while sub_job.attempt_count < MAX_ATTEMPTS:
        sub_job.attempt_count += 1

        allowed = await acquire_rate_limit(redis_client)
        if not allowed:
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
            cost_service.record_cost_event(
                session,
                job_id=job.id,
                sub_job_id=sub_job.id,
                provider="gemini",
                operation=_COST_OPERATION_LABEL,
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
            operation=_COST_OPERATION_LABEL,
            model_version=result.model_version,
            unit_cost_usd=unit_cost_usd,
        )
        await _complete_success(session, job.id, sub_job, result, prompt, seed)
        angle_sub_job_ids = await _create_angle_sub_jobs(session, job, sub_job)
        await recompute_parent_status(session, job)
        return sub_job, angle_sub_job_ids

    assert last_error is not None
    _fail(sub_job, last_error, prompt, seed)
    await recompute_parent_status(session, job)
    return sub_job, []


async def _create_angle_sub_jobs(
    session: AsyncSession, job: Job, cleanup_sub_job: SubJob
) -> list[uuid.UUID]:
    """Creates the job's angle sub-jobs in the SAME transaction as the
    cleanup sub-job's own COMPLETED write, before `recompute_parent_status`
    runs — this is what keeps the rollup from ever seeing a job with only
    its cleanup sub-job (`requested == succeeded == 1`, which stamps the
    job terminal `COMPLETED` before a single angle exists). See this
    module's docstring for the incident this closes.

    Idempotent: if angle sub-jobs already exist for this job (a redelivered
    or re-dispatched `cleanup.process` call on an already-completed cleanup
    sub-job), returns an empty list rather than colliding with
    `ux_sub_jobs_job_angle` or double-billing a fresh set of angle calls.
    """
    existing = await jobs_repo.get_sub_jobs(session, job.id)
    if any(sj.angle is not None for sj in existing):
        return []

    assert job.requested_angle_codes, (
        f"Job {job.id} has no requested_angle_codes -- cannot create its angle sub-jobs"
    )
    assert cleanup_sub_job.output_asset_id is not None

    angle_sub_job_ids: list[uuid.UUID] = []
    for code in job.requested_angle_codes:
        angle = Angle(code)
        validate_operation_angle_consistency(job.operation, angle)
        angle_sub_job = jobs_repo.create_sub_job(
            session,
            job_id=job.id,
            angle=angle,
            status=SubJobStatus.PENDING,
            source_type=SourceType.UPLOADED,
            input_asset_id=cleanup_sub_job.output_asset_id,
        )
        await session.flush()  # assigns angle_sub_job.id
        angle_sub_job_ids.append(angle_sub_job.id)

    return angle_sub_job_ids


async def _complete_success(
    session: AsyncSession,
    job_id: uuid.UUID,
    sub_job: SubJob,
    result: GenerationResult,
    prompt: str,
    seed: int,
) -> None:
    ext = "png" if result.mime_type == "image/png" else "jpg"
    assert sub_job.angle is None
    storage_path = storage_service.build_storage_path(job_id, "cleanup", AssetKind.OUTPUT, ext)
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
        expires_at=retention_policy.compute_expires_at(AssetKind.OUTPUT),
    )
    await session.flush()

    sub_job.output_asset_id = output_asset.id
    sub_job.prompt_snapshot = prompt
    sub_job.model_version = result.model_version
    sub_job.seed = seed
    # Straight to COMPLETED, never QA_REVIEW -- see this module's docstring.
    sub_job.status = SubJobStatus.COMPLETED


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
