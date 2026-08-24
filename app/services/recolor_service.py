"""Runs a single RECOLOR sub-job end-to-end. See phases/phase-19-recolor.md
Step 4. Mirrors app/services/background_service.py's shape (rate-limit ->
provider call -> cost event -> success/fail -> recompute) closely — same
one-photo-in/one-photo-out Gemini call shape, no new provider code needed
(app/providers/gemini.py's `_call_api` takes only image + text; there is no
mask parameter). Separate module, not a shared code path — see
app/workers/recolor.py for the thin Celery wrapper (same split as
background.py/background_service.py, match.py/match_service.py).

Three deliberate differences from background_service.py / match_service.py:

1. **Mask-to-Gemini conveyance.** Since the Gemini API has no mask
   parameter, the mask does its work in two places, neither of which is
   "passed as a mask": before the call, it's burned into a colour overlay
   (a solid magenta fill over the region to edit) that becomes the single
   image sent to the provider; after the call, it drives a compositing step
   that discards everything the provider changed outside the mask. See
   `_build_overlay` and `_composite_result` below.
2. **Generate-then-composite.** Unlike every other operation in this
   codebase, the client-facing OUTPUT asset is not the provider's raw
   response — it's `source` composited with the provider's response through
   a feathered mask, so everywhere the mask is 0 the output pixel is
   provably the original source's pixel. This is the one call site where
   "the provider call succeeded" and "the sub-job's artifact is correct"
   are two different claims; `_composite_result` is what bridges them.
3. **No QA gate, for a third, distinct reason from MATCH's.** MATCH ships
   straight to COMPLETED because its output is *supposed* to differ from
   its source (a similarity gate is the wrong tool for it). RECOLOR ships
   straight to COMPLETED for the opposite reason: its output is supposed to
   be *provably identical* to the source outside the mask, which is a
   pixel-exact compositing-correctness question a perceptual-similarity
   judge (`GeminiQaProvider`) isn't built to answer — verified by a
   deterministic test, not a probabilistic model call. See
   docs/business-rules.md §15 and §7's note distinguishing all three
   no-QA-gate cases.

**Mask-conveyance strategy is unvalidated against a real model call** — no
real `GEMINI_API_KEY` exists in this environment, same gap every phase
since 6 has hit. If a future session with a real key finds the magenta-
overlay approach doesn't reliably confine edits to the marked region on
real jewelry macro photography, that's a correction to
docs/ai-integration.md's Mode E, not a silent code change.

**Post-Phase-20 incident fix (2026-08-24):** `_build_overlay` now downscales
its working copies to `settings.WORKING_MAX_EDGE` before erosion/compositing
— see `app/config.py`'s own note on this setting for the live OOM this
fixes. This is safe because the overlay is a throwaway input to Gemini, not
the client-facing artifact — `_composite_result` still decodes the
*original* `source_bytes`/`mask_bytes` at their real, full resolution, so
the "byte-identical outside the mask" guarantee (point 2 above) is
unaffected. Only the pre-call path got cheaper.
"""

import random
import uuid
from datetime import UTC, datetime
from io import BytesIO

from PIL import Image, ImageFilter
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
from app.services.job_service import resolve_operation_unit_cost, resolve_recolor_prompt
from app.services.rate_limiter import acquire as acquire_rate_limit

# Mirrors background_service.py's/match_service.py's own
# MAX_ATTEMPTS/_RETRYABLE_CLASSES rather than importing them — same
# "two independent constants that happen to share a value" precedent
# already set across every prior operation's service module.
MAX_ATTEMPTS = 3
_RETRYABLE_CLASSES = {
    FailureClass.RATE_LIMITED,
    FailureClass.TRANSIENT_PROVIDER,
    FailureClass.TRANSIENT_NETWORK,
}

_COST_OPERATION_LABEL = "recolor"
_MAGENTA = (255, 0, 255)


class SubJobNotFoundError(Exception):
    pass


def _erode(mask: "Image.Image", px: int) -> "Image.Image":
    """Pulls the mask edge back off metal/prongs a hand-drawn mask routinely
    catches at its boundary — applied to the overlay sent to Gemini, never
    to the compositing mask (see _feather below). `ImageFilter.MinFilter`
    on a binary L-mode image is erosion: it replaces each pixel with the
    minimum in its neighborhood, which shrinks the white (255) region.
    """
    if px <= 0:
        return mask
    size = px * 2 + 1  # MinFilter requires an odd kernel size
    return mask.filter(ImageFilter.MinFilter(size))


def _feather(mask: "Image.Image", px: int) -> "Image.Image":
    """Softens the mask edge for the post-generation composite alpha only —
    never applied to the overlay sent to Gemini, which needs a hard-edged
    instruction region. This is what keeps the seam between original and
    recolored pixels from showing a hard ring.
    """
    if px <= 0:
        return mask
    return mask.filter(ImageFilter.GaussianBlur(px))


def _downscale_pair(
    image: "Image.Image", mask: "Image.Image", max_edge: int
) -> tuple["Image.Image", "Image.Image"]:
    """Resizes `image` and `mask` to the same target size, capped at
    `max_edge` on the longer edge, preserving aspect ratio. Both are always
    resized to the identical `target_size` tuple (not independently scaled)
    so they stay pixel-aligned regardless of rounding — erosion/compositing
    downstream assumes `image.size == mask.size`. A no-op if `image` is
    already within the cap. See app/config.py's `WORKING_MAX_EDGE` note.
    """
    if max(image.size) <= max_edge:
        return image, mask
    scale = max_edge / max(image.size)
    target_size = (round(image.width * scale), round(image.height * scale))
    return image.resize(target_size, Image.Resampling.LANCZOS), mask.resize(
        target_size, Image.Resampling.LANCZOS
    )


def _build_overlay(source_bytes: bytes, mask_bytes: bytes) -> bytes:
    """Composites a solid magenta fill over the source through the eroded
    mask — this single image is what's sent to Gemini as the sole
    reference image, alongside the prompt's "region marked in magenta"
    instruction. See this module's own docstring point 1.

    Downscaled to `settings.WORKING_MAX_EDGE` first (2026-08-24 incident
    fix, see this module's own docstring and app/config.py) — this overlay
    is never the client-facing artifact, only Gemini's input, so shrinking
    it costs nothing correctness-wise and bounds peak memory for a large
    real-world photo.
    """
    source = Image.open(BytesIO(source_bytes)).convert("RGB")
    mask = Image.open(BytesIO(mask_bytes)).convert("L")
    source, mask = _downscale_pair(source, mask, settings.WORKING_MAX_EDGE)
    eroded = _erode(mask, settings.MASK_ERODE_PX)
    magenta_fill = Image.new("RGB", source.size, _MAGENTA)
    overlay = Image.composite(magenta_fill, source, eroded)
    buf = BytesIO()
    overlay.save(buf, format="PNG")
    return buf.getvalue()


def _composite_result(
    source_bytes: bytes, provider_output_bytes: bytes, mask_bytes: bytes
) -> bytes:
    """Generate-then-composite: everywhere the (feathered) mask is 0, the
    result is the original source's pixel, exactly. The provider's raw
    output is resized to the source's own dimensions first if Gemini
    returned a different resolution — compositing requires pixel-aligned
    inputs, and the source's dimensions are what the client's mask was
    drawn against. See this module's own docstring point 2.

    Deliberately **not** downscaled, unlike `_build_overlay` — the output
    of this function is the client-facing artifact, and the
    byte-identical-outside-the-mask guarantee is a full-original-resolution
    claim. `Image.composite` is already PIL's own copy+paste, i.e. the most
    memory-efficient primitive available for this; see app/config.py's
    `WORKING_MAX_EDGE` note for what *is* bounded.
    """
    source = Image.open(BytesIO(source_bytes)).convert("RGB")
    provider_output = Image.open(BytesIO(provider_output_bytes)).convert("RGB")
    if provider_output.size != source.size:
        provider_output = provider_output.resize(source.size)
    # No persistent `mask` local — the undilated mask has no further use
    # after feathering, so this must stay full resolution (see docstring
    # above) but shouldn't outlive its one use. See app/config.py's
    # WORKING_MAX_EDGE note.
    feathered = _feather(Image.open(BytesIO(mask_bytes)).convert("L"), settings.MASK_FEATHER_PX)
    composited = Image.composite(provider_output, source, feathered)
    buf = BytesIO()
    composited.save(buf, format="PNG")
    return buf.getvalue()


async def process(session: AsyncSession, redis_client: Redis, sub_job_id: uuid.UUID) -> SubJob:
    sub_job = await jobs_repo.get_sub_job_by_id(session, sub_job_id)
    if sub_job is None:
        raise SubJobNotFoundError(f"SubJob {sub_job_id} not found.")

    job = await jobs_repo.get_by_id(session, sub_job.job_id)
    if job is None:
        raise SubJobNotFoundError(f"Job {sub_job.job_id} for sub-job {sub_job_id} not found.")

    # recolor.process only ever runs for RECOLOR sub-jobs.
    assert job.operation == Operation.RECOLOR
    assert sub_job.angle is None
    assert sub_job.mask_asset_id is not None
    assert sub_job.palette_code is not None

    # docs/business-rules.md §2: PENDING -> GENERATING -> terminal, same
    # immediate-commit-before-the-provider-call shape as every other
    # operation. Also marks the parent job PROCESSING here — a RECOLOR job
    # has no fan-out step, same as a background job: it's always exactly
    # one sub-job, dispatched directly by
    # job_service.create_recolor_job_for_request or the retry route.
    # Idempotent guard, so a retry that's already past PENDING doesn't
    # stomp started_at.
    if job.status == JobStatus.PENDING:
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(UTC)
    sub_job.status = SubJobStatus.GENERATING
    sub_job.started_at = datetime.now(UTC)
    await session.commit()

    config_version = await config_versions_repo.get_by_id(session, job.config_version_id)
    if config_version is None:
        raise SubJobNotFoundError(f"Pinned config version {job.config_version_id} not found.")

    prompt = resolve_recolor_prompt(config_version, sub_job.palette_code)
    model_version = config_version.payload["global"]["model_version"]
    unit_cost_usd = resolve_operation_unit_cost(config_version, job.operation)

    input_asset = (
        await assets_repo.get_by_id(session, sub_job.input_asset_id)
        if sub_job.input_asset_id is not None
        else None
    )
    if input_asset is None:
        raise SubJobNotFoundError(f"No input asset for sub-job {sub_job_id}.")
    mask_asset = await assets_repo.get_by_id(session, sub_job.mask_asset_id)
    if mask_asset is None:
        raise SubJobNotFoundError(f"No mask asset for sub-job {sub_job_id}.")

    source_bytes = storage_service.download_bytes(input_asset.bucket, input_asset.storage_path)
    mask_bytes = storage_service.download_bytes(mask_asset.bucket, mask_asset.storage_path)
    overlay_bytes = _build_overlay(source_bytes, mask_bytes)

    provider = GeminiProvider(model_version=model_version)
    seed = random.randint(0, 2**31 - 1)

    last_error: ProviderError | None = None
    while sub_job.attempt_count < MAX_ATTEMPTS:
        sub_job.attempt_count += 1

        # Shared budget with every other operation — the Gemini rate limit
        # is global. See phases/phase-19-recolor.md Step 4.
        allowed = await acquire_rate_limit(redis_client)
        if not allowed:
            last_error = ProviderError(
                "Gemini rate limit window exhausted.", failure_class=FailureClass.RATE_LIMITED
            )
            if last_error.failure_class not in _RETRYABLE_CLASSES:
                break
            continue

        try:
            result = provider.generate(prompt, [overlay_bytes], seed)
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
        await _complete_success(
            session, job.id, sub_job, result, source_bytes, mask_bytes, prompt, seed
        )
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
    source_bytes: bytes,
    mask_bytes: bytes,
    prompt: str,
    seed: int,
) -> None:
    composited_bytes = _composite_result(source_bytes, result.image_bytes, mask_bytes)
    assert sub_job.angle is None
    storage_path = storage_service.build_storage_path(job_id, "recolor", AssetKind.OUTPUT, "png")
    storage_service.upload_bytes(
        settings.BUCKET_OUTPUTS, storage_path, composited_bytes, "image/png"
    )
    output_asset = assets_repo.create_asset(
        session,
        job_id=job_id,
        sub_job_id=sub_job.id,
        kind=AssetKind.OUTPUT,
        bucket=settings.BUCKET_OUTPUTS,
        storage_path=storage_path,
        mime_type="image/png",
        bytes_=len(composited_bytes),
        expires_at=retention_policy.compute_expires_at(AssetKind.OUTPUT),
    )
    await session.flush()

    sub_job.output_asset_id = output_asset.id
    sub_job.prompt_snapshot = prompt
    sub_job.model_version = result.model_version
    sub_job.seed = seed
    # Straight to COMPLETED, never QA_REVIEW — see this module's docstring
    # point 3.
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
