"""Runs a single MATCH sub-job (companion-piece generation) end-to-end. See
phases/phase-18-match.md Step 4. Mirrors app/services/background_service.py's
shape (rate-limit -> provider call -> cost event -> success/fail ->
recompute) closely — same one-photo-in/one-photo-out Gemini call shape, no
new provider code needed. Separate module, not a shared code path — see
app/workers/match.py for the thin Celery wrapper (same split as
background.py/background_service.py).

Two deliberate differences from background_service.py:

1. Prompt resolution goes through app.services.job_service.resolve_match_prompt
   rather than a preset-branching `_resolve_prompt` — MATCH has no presets,
   just a single `operations.MATCH.prompt` template with a runtime
   `{target_category}` substitution (job.category_code holds the requested
   target_category — see job_service.create_match_job_for_request).

2. A successful provider call lands the sub-job straight on COMPLETED, never
   QA_REVIEW. Background operations always gate through QA_REVIEW because
   "the cutout *is* the product" (see background_service.py's own module
   docstring). MATCH's output is *supposed* to differ from its source — it's
   a different physical piece meant to match it stylistically, not preserve
   its subject — so a subject-preservation similarity gate is the wrong tool
   here. This is the same posture Mode A (app/services/generation_service.py
   ::transform_photo) already ships for a SourceType.UPLOADED sub-job: no QA
   gate at all, straight to COMPLETED. See phases/phase-18-match.md Step 4.
"""

import random
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import ProviderError
from app.db.models.enums import AssetKind, FailureClass, JobStatus, Operation, SubJobStatus
from app.db.models.jobs import SubJob
from app.db.repositories import assets as assets_repo
from app.db.repositories import config_versions as config_versions_repo
from app.db.repositories import jobs as jobs_repo
from app.providers.base import GenerationResult
from app.providers.gemini import GeminiProvider
from app.services import cost_service, retention_policy, storage_service
from app.services.generation_service import recompute_parent_status
from app.services.job_service import resolve_match_prompt, resolve_operation_unit_cost
from app.services.rate_limiter import acquire as acquire_rate_limit

# Mirrors background_service.py's own MAX_ATTEMPTS/_RETRYABLE_CLASSES rather
# than importing them — same "two independent constants that happen to share
# a value" precedent generation_service.MAX_ATTEMPTS / job_service.
# MAX_RETRY_ATTEMPTS / background_service.MAX_ATTEMPTS already set (Phase 8's
# CLAUDE.md note), not a shared source of truth to import across modules.
MAX_ATTEMPTS = 3
_RETRYABLE_CLASSES = {
    FailureClass.RATE_LIMITED,
    FailureClass.TRANSIENT_PROVIDER,
    FailureClass.TRANSIENT_NETWORK,
}

_COST_OPERATION_LABEL = "match"


class SubJobNotFoundError(Exception):
    pass


async def process(session: AsyncSession, redis_client: Redis, sub_job_id: uuid.UUID) -> SubJob:
    sub_job = await jobs_repo.get_sub_job_by_id(session, sub_job_id)
    if sub_job is None:
        raise SubJobNotFoundError(f"SubJob {sub_job_id} not found.")

    job = await jobs_repo.get_by_id(session, sub_job.job_id)
    if job is None:
        raise SubJobNotFoundError(f"Job {sub_job.job_id} for sub-job {sub_job_id} not found.")

    # match.process only ever runs for MATCH sub-jobs.
    assert job.operation == Operation.MATCH
    assert sub_job.angle is None
    assert sub_job.variant_index is not None

    # docs/business-rules.md §2: PENDING -> GENERATING -> terminal, same
    # immediate-commit-before-the-provider-call shape as transform_photo /
    # background.process. Also marks the parent job PROCESSING here, the
    # same way orchestration_service.dispatch_job does for angle jobs before
    # any generation.transform_photo dispatch — see
    # app/workers/orchestration.py::fan_out_match_job, which calls
    # dispatch_job (unmodified) before dispatching match.process per
    # variant. Idempotent guard, so a retry that's already past PENDING
    # doesn't stomp started_at.
    if job.status == JobStatus.PENDING:
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(UTC)
    sub_job.status = SubJobStatus.GENERATING
    sub_job.started_at = datetime.now(UTC)
    await session.commit()

    config_version = await config_versions_repo.get_by_id(session, job.config_version_id)
    if config_version is None:
        raise SubJobNotFoundError(f"Pinned config version {job.config_version_id} not found.")

    assert job.category_code is not None  # target_category, set at job creation
    prompt = resolve_match_prompt(config_version, job.category_code)
    model_version = config_version.payload["global"]["model_version"]
    unit_cost_usd = resolve_operation_unit_cost(config_version, job.operation)

    input_asset = (
        await assets_repo.get_by_id(session, sub_job.input_asset_id)
        if sub_job.input_asset_id is not None
        else None
    )
    if input_asset is None:
        raise SubJobNotFoundError(f"No input asset for sub-job {sub_job_id}.")
    # A single style-reference image — unlike background operations, MATCH
    # never has a second (background) image to append.
    reference_images = [
        storage_service.download_bytes(input_asset.bucket, input_asset.storage_path)
    ]

    provider = GeminiProvider(model_version=model_version)
    seed = random.randint(0, 2**31 - 1)

    last_error: ProviderError | None = None
    while sub_job.attempt_count < MAX_ATTEMPTS:
        sub_job.attempt_count += 1

        # Shared budget with angle generation and background operations —
        # the Gemini rate limit is global, these calls compete with them for
        # the same window. See phases/phase-18-match.md Step 4.
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
            # docs/business-rules.md §10: cost recorded before further
            # evaluation, including calls that end in refusal.
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
        await recompute_parent_status(session, job)
        return sub_job

    assert last_error is not None
    _fail(sub_job, last_error, prompt, seed)
    await recompute_parent_status(session, job)
    return sub_job


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
    storage_path = storage_service.build_storage_path(job_id, "match", AssetKind.OUTPUT, ext)
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
    # Straight to COMPLETED, never QA_REVIEW — see this module's docstring.
    # sub_job.completed_at is deliberately left unset here: it's never set by
    # any terminal write anywhere in this codebase (generation_service.py's
    # own _complete_success doesn't set it even for its SourceType.UPLOADED
    # -> COMPLETED case, and qa_service.py's approve/reject path explicitly
    # documents leaving it NULL) — a pre-existing, documented gap across the
    # whole sub_job lifecycle, not something to fix inconsistently from just
    # this one code path.
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
