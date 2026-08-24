"""Runs a single MIX sub-job end-to-end. See phases/phase-20-mix.md Step 4.
Mirrors app/services/recolor_service.py's shape (rate-limit -> provider
call -> cost event -> success/fail -> recompute) closely, plus a genuinely
new step *before* the rate-limit loop even starts: a deterministic
rough-composite, no provider involved. Separate module, not a shared code
path — see app/workers/mix.py for the thin Celery wrapper (same split as
recolor.py/background_service.py, match.py/match_service.py). Reimplements
`_erode`/`_feather` as its own module-level functions rather than importing
recolor_service.py's — same "two independent constants/helpers that happen
to share a shape" precedent recolor_service.py's own docstring already
establishes relative to background_service.py/match_service.py; no service
module imports another service module's private helpers anywhere in this
codebase.

Two deliberate differences from recolor_service.py, on top of everything it
already differs from background_service.py/match_service.py:

1. **A deterministic rough-composite before any provider call.** RECOLOR's
   first real step is a provider call with a pre-built overlay. MIX's first
   real step is Pillow-only image assembly — crop region B via its mask's
   bounding box, scale it to fit region A's bounding box, paste it onto
   source A (`_build_rough_composite`). Placement is never asked of the
   model — only the seam is. This is the direct consequence of cross-image
   spatial reasoning being the weakest capability in play for any
   current-generation image model (see phases/phase-20-mix.md's own
   reality-check section).
2. **A seam-band mask, not a filled-region mask.** RECOLOR's compositing
   mask marks "the region to change." MIX's post-composite mask
   (`_seam_band_mask`) marks a *ring* around the graft boundary — the
   visible seam, not the graft's interior (already correct by construction
   from the rough-composite step) and not the untouched rest of image A.

**No QA gate — a fourth distinct reason, not a fourth posture.** See
docs/business-rules.md §7's MIX note: RECOLOR's output is provably
identical to *the original untouched source* outside a static mask; MIX's
output is provably identical to `rough_composite` — itself already a
deterministic merge of two different pieces' photos — outside the seam
band. Verified by a deterministic test, not a probabilistic model call.

**Both the rough-composite's non-aspect-preserving scale-to-fit and the
seam-only refinement strategy are unvalidated against a real model call** —
no real `GEMINI_API_KEY` exists in this environment, same gap every phase
since 6 has hit. If a future session with a real key finds either doesn't
hold up, that's a correction to docs/ai-integration.md's Mode F, not a
silent code change.

**Post-Phase-20 incident fix (2026-08-24):** a live RECOLOR request OOM-killed
`jewelry-api` on Render's free tier (512MB) — root-caused to full-resolution
PIL compositing with no size cap; see `app/config.py`'s `WORKING_MAX_EDGE`
note and `app/services/recolor_service.py`'s own matching fix. MIX's
exposure is worse in principle (four source images decoded at once in
`_build_rough_composite` vs. RECOLOR's two), but only `_build_seam_overlay`
could actually be downscaled here — see that function's own docstring for
why `_build_rough_composite` could not be, without a larger refactor than
this incident fix, and is flagged as MIX's own remaining memory hotspot
rather than silently left unbounded.
"""

import random
import uuid
from datetime import UTC, datetime
from io import BytesIO

from PIL import Image, ImageChops, ImageFilter
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
from app.services.job_service import resolve_mix_prompt, resolve_operation_unit_cost
from app.services.rate_limiter import acquire as acquire_rate_limit

# Mirrors recolor_service.py's/background_service.py's/match_service.py's
# own MAX_ATTEMPTS/_RETRYABLE_CLASSES rather than importing them — same
# "two independent constants that happen to share a value" precedent
# already set across every prior operation's service module.
MAX_ATTEMPTS = 3
_RETRYABLE_CLASSES = {
    FailureClass.RATE_LIMITED,
    FailureClass.TRANSIENT_PROVIDER,
    FailureClass.TRANSIENT_NETWORK,
}

_COST_OPERATION_LABEL = "mix"
_MAGENTA = (255, 0, 255)


class SubJobNotFoundError(Exception):
    pass


def _erode(mask: "Image.Image", px: int) -> "Image.Image":
    """Shrinks the white region — see recolor_service._erode's own
    docstring for the reasoning; reimplemented here rather than imported.
    """
    if px <= 0:
        return mask
    size = px * 2 + 1  # MinFilter requires an odd kernel size
    return mask.filter(ImageFilter.MinFilter(size))


def _dilate(mask: "Image.Image", px: int) -> "Image.Image":
    """Grows the white region — the opposite of `_erode`, same odd-kernel
    `ImageFilter.MaxFilter` mechanism. Used only to build the seam-band ring
    (`_seam_band_mask`), never applied on its own to a compositing alpha.
    """
    if px <= 0:
        return mask
    size = px * 2 + 1
    return mask.filter(ImageFilter.MaxFilter(size))


def _feather(mask: "Image.Image", px: int) -> "Image.Image":
    """Softens an edge for a post-generation composite alpha only — never
    applied to the overlay sent to Gemini. See recolor_service._feather.
    """
    if px <= 0:
        return mask
    return mask.filter(ImageFilter.GaussianBlur(px))


def _seam_band_mask(mask_a: "Image.Image", band_px: int) -> "Image.Image":
    """A ring `band_px` pixels wide straddling mask A's boundary —
    dilate(mask_a, band_px) minus erode(mask_a, band_px). Marks only the
    visible graft edge: not the graft's interior (already correct by
    construction from `_build_rough_composite`) and not the untouched rest
    of image A. See this module's own docstring point 2.

    **Known limitation, not fixed here:** for a mask A narrower than
    roughly `2 * (band_px + settings.MASK_FEATHER_PX)` in either dimension,
    the post-call Gaussian feather (`_composite_seam_result`) can bleed
    through the graft's interior "hole" from both sides of the ring at
    once, letting the provider's output reach pixels this module's own
    docstring claims are protected. No ingest-time minimum-region-size
    check exists yet for the same reason no cross-mask ratio check does —
    see phases/phase-20-mix.md Step 3's note on deferred validation. Tests
    covering the off-seam-band guarantee must use a region large enough to
    avoid this, not a minimal one.
    """
    if band_px <= 0:
        return Image.new("L", mask_a.size, 0)
    dilated = _dilate(mask_a, band_px)
    eroded = _erode(mask_a, band_px)
    return ImageChops.subtract(dilated, eroded)


def _build_rough_composite(
    source_a_bytes: bytes, mask_a_bytes: bytes, source_b_bytes: bytes, mask_b_bytes: bytes
) -> bytes:
    """Deterministic image assembly, no provider call involved — see this
    module's own docstring point 1. Crops region B to mask B's bounding
    box, scales it to exactly fit mask A's bounding box (aspect ratio not
    preserved — a deliberate simplification, see phases/phase-20-mix.md
    Step 3's own note on why no ingest-time ratio limit exists yet), and
    pastes it onto a copy of source A using mask A's own crop (already at
    the target box's exact size, since a bounding box is defined by it) as
    the paste alpha — so pixels inside the box but outside mask A's actual
    silhouette are left as source A's original pixels, not overwritten by
    the crop's rectangular corners.

    **Deliberately not downscaled** — unlike `_build_seam_overlay` below,
    this function's *output* (`rough_composite`) is not a throwaway input
    to Gemini; it's the base both the seam overlay is built from *and* the
    final compositing step composites back onto (`_composite_seam_result`),
    so it must stay at source A's real, full original resolution the same
    way `recolor_service._composite_result`'s inputs must. This means
    `_build_rough_composite` decodes **four** full-resolution images at
    once (source A, mask A, source B, mask B) — the single largest memory
    consumer in this codebase, larger than anything RECOLOR's own
    2026-08-24 incident fix addressed there. Bounding it would need
    restructuring this function to build the crop/scale/paste at a reduced
    working resolution and *separately* re-derive a full-resolution
    `rough_composite` for the final output — a real refactor, out of scope
    for this incident fix. Flagged here explicitly as MIX's own remaining
    memory hotspot, not silently left unbounded.
    """
    source_a = Image.open(BytesIO(source_a_bytes)).convert("RGB")
    mask_a = Image.open(BytesIO(mask_a_bytes)).convert("L")
    source_b = Image.open(BytesIO(source_b_bytes)).convert("RGB")
    mask_b = Image.open(BytesIO(mask_b_bytes)).convert("L")

    bbox_a = mask_a.getbbox()
    bbox_b = mask_b.getbbox()
    assert bbox_a is not None  # coverage validated > 0 at ingest (mask_validation)
    assert bbox_b is not None

    cropped_b = source_b.crop(bbox_b)
    target_size = (bbox_a[2] - bbox_a[0], bbox_a[3] - bbox_a[1])
    scaled_b = cropped_b.resize(target_size)
    paste_alpha = mask_a.crop(bbox_a)

    rough = source_a.copy()
    rough.paste(scaled_b, (bbox_a[0], bbox_a[1]), paste_alpha)
    buf = BytesIO()
    rough.save(buf, format="PNG")
    return buf.getvalue()


def _build_seam_overlay(rough_composite_bytes: bytes, seam_band_mask: "Image.Image") -> bytes:
    """Composites a solid magenta fill over `rough_composite` through the
    seam-band mask (hard edge, same reasoning recolor_service._build_overlay
    already established for why the overlay sent to Gemini needs a hard
    boundary, not a feathered one). This single image is what's sent to
    Gemini as the sole reference image.

    Downscaled to `settings.WORKING_MAX_EDGE` first (2026-08-24 incident
    fix, see this module's own docstring and app/config.py) — same
    reasoning recolor_service._build_overlay already established: this
    overlay is Gemini's input only, never the client-facing artifact.
    `rough_composite_bytes` itself is untouched by this function (the
    caller's own copy stays full resolution for
    `_composite_seam_result`'s later use), so the byte-identical-outside-
    the-seam-band guarantee is unaffected — only this one throwaway
    working copy gets smaller.
    """
    rough = Image.open(BytesIO(rough_composite_bytes)).convert("RGB")
    mask = seam_band_mask
    if max(rough.size) > settings.WORKING_MAX_EDGE:
        scale = settings.WORKING_MAX_EDGE / max(rough.size)
        target_size = (round(rough.width * scale), round(rough.height * scale))
        rough = rough.resize(target_size, Image.Resampling.LANCZOS)
        mask = mask.resize(target_size, Image.Resampling.LANCZOS)
    magenta_fill = Image.new("RGB", rough.size, _MAGENTA)
    overlay = Image.composite(magenta_fill, rough, mask)
    buf = BytesIO()
    overlay.save(buf, format="PNG")
    return buf.getvalue()


def _composite_seam_result(
    rough_composite_bytes: bytes, provider_output_bytes: bytes, seam_band_mask: "Image.Image"
) -> bytes:
    """Generate-then-composite, scoped to the seam only: everywhere the
    (feathered) seam-band mask is 0 — including both the untouched rest of
    image A and the already-correct interior of the graft — the result is
    `rough_composite`'s own pixel, exactly. The provider's raw output is
    resized to `rough_composite`'s own dimensions first if Gemini returned a
    different resolution, same reasoning recolor_service._composite_result
    already established.
    """
    rough = Image.open(BytesIO(rough_composite_bytes)).convert("RGB")
    provider_output = Image.open(BytesIO(provider_output_bytes)).convert("RGB")
    if provider_output.size != rough.size:
        provider_output = provider_output.resize(rough.size)
    feathered = _feather(seam_band_mask, settings.MASK_FEATHER_PX)
    composited = Image.composite(provider_output, rough, feathered)
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

    # mix.process only ever runs for MIX sub-jobs.
    assert job.operation == Operation.MIX
    assert sub_job.angle is None
    assert sub_job.mask_asset_id is not None
    assert sub_job.secondary_input_asset_id is not None
    assert sub_job.secondary_mask_asset_id is not None

    # docs/business-rules.md §2: PENDING -> GENERATING -> terminal, same
    # immediate-commit-before-the-provider-call shape as every other
    # operation. Also marks the parent job PROCESSING here — a MIX job has
    # no fan-out step, same as RECOLOR/background: it's always exactly one
    # sub-job, dispatched directly by job_service.create_mix_job_for_request
    # or the retry route. Idempotent guard, so a retry that's already past
    # PENDING doesn't stomp started_at.
    if job.status == JobStatus.PENDING:
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(UTC)
    sub_job.status = SubJobStatus.GENERATING
    sub_job.started_at = datetime.now(UTC)
    await session.commit()

    config_version = await config_versions_repo.get_by_id(session, job.config_version_id)
    if config_version is None:
        raise SubJobNotFoundError(f"Pinned config version {job.config_version_id} not found.")

    prompt = resolve_mix_prompt(config_version)
    model_version = config_version.payload["global"]["model_version"]
    unit_cost_usd = resolve_operation_unit_cost(config_version, job.operation)

    primary_asset = (
        await assets_repo.get_by_id(session, sub_job.input_asset_id)
        if sub_job.input_asset_id is not None
        else None
    )
    if primary_asset is None:
        raise SubJobNotFoundError(f"No primary input asset for sub-job {sub_job_id}.")
    primary_mask_asset = await assets_repo.get_by_id(session, sub_job.mask_asset_id)
    if primary_mask_asset is None:
        raise SubJobNotFoundError(f"No primary mask asset for sub-job {sub_job_id}.")
    secondary_asset = await assets_repo.get_by_id(session, sub_job.secondary_input_asset_id)
    if secondary_asset is None:
        raise SubJobNotFoundError(f"No secondary input asset for sub-job {sub_job_id}.")
    secondary_mask_asset = await assets_repo.get_by_id(session, sub_job.secondary_mask_asset_id)
    if secondary_mask_asset is None:
        raise SubJobNotFoundError(f"No secondary mask asset for sub-job {sub_job_id}.")

    primary_bytes = storage_service.download_bytes(primary_asset.bucket, primary_asset.storage_path)
    primary_mask_bytes = storage_service.download_bytes(
        primary_mask_asset.bucket, primary_mask_asset.storage_path
    )
    secondary_bytes = storage_service.download_bytes(
        secondary_asset.bucket, secondary_asset.storage_path
    )
    secondary_mask_bytes = storage_service.download_bytes(
        secondary_mask_asset.bucket, secondary_mask_asset.storage_path
    )

    rough_composite_bytes = _build_rough_composite(
        primary_bytes, primary_mask_bytes, secondary_bytes, secondary_mask_bytes
    )
    primary_mask_image = Image.open(BytesIO(primary_mask_bytes)).convert("L")
    seam_band = _seam_band_mask(primary_mask_image, settings.MIX_SEAM_BAND_PX)
    overlay_bytes = _build_seam_overlay(rough_composite_bytes, seam_band)

    provider = GeminiProvider(model_version=model_version)
    seed = random.randint(0, 2**31 - 1)

    last_error: ProviderError | None = None
    while sub_job.attempt_count < MAX_ATTEMPTS:
        sub_job.attempt_count += 1

        # Shared budget with every other operation — the Gemini rate limit
        # is global. See phases/phase-20-mix.md Step 4.
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
            session, job.id, sub_job, result, rough_composite_bytes, seam_band, prompt, seed
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
    rough_composite_bytes: bytes,
    seam_band: "Image.Image",
    prompt: str,
    seed: int,
) -> None:
    composited_bytes = _composite_seam_result(rough_composite_bytes, result.image_bytes, seam_band)
    assert sub_job.angle is None
    storage_path = storage_service.build_storage_path(job_id, "mix", AssetKind.OUTPUT, "png")
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
    # Straight to COMPLETED, never QA_REVIEW — see this module's docstring's
    # "No QA gate" section.
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
