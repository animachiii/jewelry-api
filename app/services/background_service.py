"""Runs a single background-operation sub-job end-to-end (BACKGROUND_REMOVAL
/ BACKGROUND_REPLACEMENT). See phases/phase-15-background-operations.md
Step 5. Mirrors app/services/generation_service.py::transform_photo's shape
(rate-limit -> provider call -> cost event -> success/fail) but is a
separate module, not a shared code path — see app/workers/background.py for
the thin Celery wrapper (same split as generation.py/generation_service.py).

Reuses GeminiProvider unmodified: docs/decisions/0002-background-removal-approach.md
already decided both operations are Gemini calls with a flat/solid
background, same one-photo-in/one-photo-out shape Mode A angle generation
already uses — no new provider code needed.

Unlike Mode A angle generation, a background operation's success **always**
enters QA_REVIEW, never straight COMPLETED — see docs/ai-integration.md
Call Site 1's accepted risk on Mode A and this phase's Step 5: "the cutout
*is* the product," so this is the one call site that adds the gate rather
than inheriting the omission.
"""

import random
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import ProviderError
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import AssetKind, FailureClass, JobStatus, Operation, SubJobStatus
from app.db.models.jobs import Job, SubJob
from app.db.repositories import assets as assets_repo
from app.db.repositories import config_versions as config_versions_repo
from app.db.repositories import jobs as jobs_repo
from app.providers.base import GenerationResult
from app.providers.gemini import GeminiProvider
from app.services import cost_service, storage_service
from app.services.generation_service import recompute_parent_status
from app.services.job_service import (
    find_operation_config,
    find_preset,
    resolve_operation_unit_cost,
)
from app.services.rate_limiter import acquire as acquire_rate_limit

# Mirrors generation_service.py's own MAX_ATTEMPTS/_RETRYABLE_CLASSES rather
# than importing them — same "two independent constants that happen to
# share a value" precedent job_service.MAX_RETRY_ATTEMPTS and
# generation_service.MAX_ATTEMPTS already set (see Phase 8's CLAUDE.md
# note), not a shared source of truth to import across modules.
MAX_ATTEMPTS = 3
_RETRYABLE_CLASSES = {
    FailureClass.RATE_LIMITED,
    FailureClass.TRANSIENT_PROVIDER,
    FailureClass.TRANSIENT_NETWORK,
}

_COST_OPERATION_LABEL = {
    Operation.BACKGROUND_REMOVAL: "background_removal",
    Operation.BACKGROUND_REPLACEMENT: "background_replacement",
}


class SubJobNotFoundError(Exception):
    pass


def _resolve_prompt(config_version: ConfigVersion, job: Job) -> str:
    """`operations.<OP>.prompt` is the base instruction. A replacement job
    appends either its pinned preset's own prompt, or — for custom-background
    compositing (docs/superpowers/specs/2026-08-13-custom-background-compositing-design.md)
    — the operation's `custom_background_prompt`. `job.preset_code` is NULL
    for the custom-background path (BackgroundReplaceRequest's mutual-
    exclusivity validator guarantees exactly one of preset_code /
    background_storage_path was given at request time), so checking it here
    is sufficient to pick the right branch without threading the sub-job
    through this function.
    """
    op_config = find_operation_config(config_version, job.operation) or {}
    prompt = str(op_config.get("prompt", ""))
    if job.operation == Operation.BACKGROUND_REPLACEMENT:
        if job.preset_code is not None:
            preset = find_preset(config_version, job.preset_code)
            assert preset is not None  # validated at job-creation time; config is pinned, immutable
            prompt = f"{prompt} {preset['prompt']}".strip()
        else:
            # Mirrors the preset branch's loud-failure posture above: a
            # missing custom_background_prompt (e.g. a job pinned to a
            # config version older than migration 0012) must not silently
            # degrade to "just the generic replacement prompt, no
            # compositing/shadow/lighting instructions" — that would still
            # return a 202/COMPLETED job with a plausible-looking but
            # not-actually-composited image, with nothing anywhere
            # signaling the degradation.
            custom_prompt = op_config.get("custom_background_prompt")
            assert custom_prompt, (
                "custom_background_prompt missing from pinned config "
                f"for job {job.id} — see migration 0012"
            )
            prompt = f"{prompt} {custom_prompt}".strip()
    return prompt


async def process(session: AsyncSession, redis_client: Redis, sub_job_id: uuid.UUID) -> SubJob:
    sub_job = await jobs_repo.get_sub_job_by_id(session, sub_job_id)
    if sub_job is None:
        raise SubJobNotFoundError(f"SubJob {sub_job_id} not found.")

    job = await jobs_repo.get_by_id(session, sub_job.job_id)
    if job is None:
        raise SubJobNotFoundError(f"Job {sub_job.job_id} for sub-job {sub_job_id} not found.")

    # background.process only ever runs for BACKGROUND_REMOVAL/
    # BACKGROUND_REPLACEMENT sub-jobs — ANGLE_GENERATION stays on
    # generation.transform_photo.
    assert job.operation != Operation.ANGLE_GENERATION
    assert sub_job.angle is None

    # docs/business-rules.md §2: PENDING -> GENERATING -> terminal, same
    # immediate-commit-before-the-provider-call shape as transform_photo —
    # see phases/phase-7-orchestration.md Step 1.
    #
    # Also marks the parent job PROCESSING here — angle jobs get this for
    # free from orchestration_service.dispatch_job (a separate task that
    # runs before any generation.transform_photo dispatch), but a
    # background job has no fan-out step: this is its one and only
    # sub-job, dispatched directly by job_service.create_background_job_for_request
    # / the retry route. Idempotent, same guard dispatch_job uses, so a
    # retry that's already past PENDING doesn't stomp started_at.
    if job.status == JobStatus.PENDING:
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(UTC)
    sub_job.status = SubJobStatus.GENERATING
    sub_job.started_at = datetime.now(UTC)
    await session.commit()

    config_version = await config_versions_repo.get_by_id(session, job.config_version_id)
    if config_version is None:
        raise SubJobNotFoundError(f"Pinned config version {job.config_version_id} not found.")

    prompt = _resolve_prompt(config_version, job)
    model_version = config_version.payload["global"]["model_version"]
    unit_cost_usd = resolve_operation_unit_cost(config_version, job.operation)
    cost_operation = _COST_OPERATION_LABEL[job.operation]

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
    if sub_job.background_asset_id is not None:
        background_asset = await assets_repo.get_by_id(session, sub_job.background_asset_id)
        if background_asset is None:
            raise SubJobNotFoundError(f"No background asset for sub-job {sub_job_id}.")
        # Product photo first, background photo second — matches the order
        # the prompt (see _resolve_prompt above) narrates them in. Uses
        # download_bytes (not download_to_temp().read_bytes()) for the same
        # reason the product-photo download above does — see
        # storage_service.download_bytes's docstring on the 2026-08-13 OOM
        # this avoids re-introducing.
        reference_images.append(
            storage_service.download_bytes(background_asset.bucket, background_asset.storage_path)
        )

    provider = GeminiProvider(model_version=model_version)
    seed = random.randint(0, 2**31 - 1)

    last_error: ProviderError | None = None
    while sub_job.attempt_count < MAX_ATTEMPTS:
        sub_job.attempt_count += 1

        # Shared budget with angle generation — the Gemini rate limit is
        # global, these calls compete with angle jobs for the same window.
        # See phases/phase-15-background-operations.md Step 5.
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
                operation=cost_operation,
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
            operation=cost_operation,
            model_version=result.model_version,
            unit_cost_usd=unit_cost_usd,
        )
        await _complete_success(session, job.id, sub_job, result, prompt, seed)
        # Unlike transform_photo: never straight to COMPLETED. The
        # subject-preservation gate is mandatory for every background
        # operation, not conditional on source_type — see this module's
        # docstring and phases/phase-15-background-operations.md Step 5.
        # Still recompute here even though QA_REVIEW isn't S(ucceeded) or
        # F(ailed) and a no-op for compute_parent_status's own math today:
        # it's what makes this function's contract "the parent status is
        # always correct on return" rather than "correct because the
        # PROCESSING write above happens to survive until qa_service's own
        # recompute runs later" — see this file's docstring on why there's
        # no fan-out step to rely on instead.
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
    storage_path = storage_service.build_storage_path(job_id, "background", AssetKind.OUTPUT, ext)
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
    sub_job.status = SubJobStatus.QA_REVIEW


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
