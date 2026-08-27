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
note and `app/services/recolor_service.py`'s own matching fix. That first
pass only covered `_build_seam_overlay` here — `_build_rough_composite`
still decoded all four source images (source A, mask A, source B, mask B)
at native resolution, flagged explicitly as MIX's own remaining hotspot
rather than silently left unbounded.

**2026-08-25 follow-up:** `_build_rough_composite` now downscales source B
and mask B to `WORKING_MAX_EDGE` before the crop — they're always
cropped-then-resized into region A's bounding box regardless, so decoding
them at native resolution bought nothing. Source A and mask A still decode
at full resolution (required — see `_build_rough_composite`'s own
docstring), and the function no longer holds a redundant `.copy()` of
source A alongside the original. Net effect: two of the four decodes are
now bounded and the third full-resolution buffer (the old `.copy()`) is
gone, down from four uncapped full-resolution images held at once to two.
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


def _working_size(width: int, height: int) -> tuple[int, int] | None:
    """The size this pipeline actually operates at, or None when the image is
    already small enough to use as-is. See `_load_downscaled`.
    """
    longest = max(width, height)
    if longest <= settings.WORKING_MAX_EDGE:
        return None
    scale = settings.WORKING_MAX_EDGE / longest
    return (round(width * scale), round(height * scale))


def _load_downscaled(data: bytes, mode: str) -> "Image.Image":
    """Decode an upload straight into MIX's working resolution.

    `img.draft()` is the part that actually saves memory: for a JPEG it
    decodes at a reduced DCT scale (1/2, 1/4, 1/8) rather than decoding all
    12.6 MP and throwing most of it away on the next line. It is a no-op for
    PNG (the masks), which still decode in full and are resized after -- the
    masks are the cheap buffers here (1 byte/px, not 3), so that is fine.

    Masks resample with NEAREST, deliberately, not LANCZOS: `mask_validation`
    guarantees a binary 0/255 mask on ingest, `_seam_band_mask`'s
    dilate-minus-erode assumes it, and a smooth filter would introduce
    intermediate values that quietly blur the seam ring. Photos use LANCZOS,
    where smoothness is what you want.
    """
    # `handle` stays separate from `img`: Image.open returns an ImageFile,
    # and draft() is only available there -- it has to be called on the
    # undecoded handle, before convert() forces the decode.
    handle = Image.open(BytesIO(data))
    target = _working_size(handle.width, handle.height)
    if target is not None:
        handle.draft(mode, target)  # JPEG fast path; no-op otherwise
    img: Image.Image = handle.convert(mode)
    if target is not None and img.size != target:
        resample = Image.Resampling.NEAREST if mode == "L" else Image.Resampling.LANCZOS
        img = img.resize(target, resample)
    return img


def _build_rough_composite(
    source_a_bytes: bytes, mask_a_bytes: bytes, source_b_bytes: bytes, mask_b_bytes: bytes
) -> tuple[bytes, "Image.Image"]:
    """Deterministic image assembly, no provider call involved — see this
    module's own docstring point 1. Crops region B to mask B's bounding box,
    scales it **preserving aspect ratio** to fit inside mask A's bounding
    box, centres it there, and pastes it through the intersection of **both**
    silhouettes. Returns the composite plus the graft mask — the region that
    actually changed, which the caller needs for the seam band.

    **2026-08-28 — two real defects found on the first genuine client run
    (job `fe7d6372`), both fixed here.**

    *Defect 1: mask B's shape was discarded.* The old code used `mask_b` only
    to compute `bbox_b`, then grafted the raw rectangular crop. Measured on
    that job, mask B was two curved bands on opposite sides of the piece
    whose shared bounding box was only **39.5% painted** — so **60% of what
    got grafted was unpainted mannequin and background**, squashed into the
    pendant silhouette. The output was a beige pendant-shaped blob with gold
    fringing the edges. `mask_b` is now a paste alpha in its own right, so
    only pixels the client actually painted travel across.

    *Defect 2: the scale-to-fit was non-aspect-preserving.* Two long thin
    bands stretched into a compact bird-shaped box distort badly no matter
    how clean the masking is. `phases/phase-20-mix.md` Step 3 called this a
    "deliberate simplification... unvalidated against real client pieces";
    this was the validation, and it failed. The scale is now
    `min(w_ratio, h_ratio)` with the result centred in A's box, so B keeps
    its proportions and simply fits inside.

    **Consequence the caller must respect:** the graft no longer necessarily
    fills mask A. The real seam is the boundary of the returned graft mask,
    NOT of mask A, so the seam band must be built from the returned mask —
    building it from mask A would ask the provider to blend an edge that
    isn't there while leaving the actual graft edge untouched.

    **All four inputs are decoded at `settings.WORKING_MAX_EDGE`, so
    `rough_composite` — and therefore MIX's final client-facing output — is
    capped at that edge rather than source A's native resolution**
    (2026-08-27; decided directly with the user). Two earlier passes tried
    to keep full resolution and both were insufficient: 2026-08-24 (PR #32)
    capped only the throwaway Gemini overlay, and 2026-08-25 (PR #33)
    additionally capped source/mask B while keeping A native. Measured
    against the real client upload that broke it — 3072x4096, 12.6 MP, two
    photos plus two masks — the pipeline still needed **~187 MB** of working
    memory, against ~160 MB of headroom on the 512 MB free instance (the
    baseline was already 353 MB). The container was SIGKILLed roughly three
    seconds into the task, every time, before a single Gemini attempt: the
    live sub-job sat at `GENERATING` with `attempt_count` still 0.

    Capping the whole working canvas takes 12.6 MP to ~3.1 MP, roughly a 4x
    cut in every buffer, which is what makes MIX run at all on this instance.
    The byte-identical-outside-the-seam-band guarantee is unchanged in
    substance — it was always stated relative to `rough_composite`, not to
    either original photo (docs/business-rules.md §16), and `rough_composite`
    is simply now a smaller image. **What genuinely changed is the output
    resolution**, so this is a real product tradeoff, not a free win.

    Note this does NOT alter `recolor_service`, whose own guarantee *is*
    stated against the untouched original source and which therefore still
    composites at full resolution — it has the same exposure on a 12.6 MP
    upload and has not been addressed here.

    `settings.MIX_SEAM_BAND_PX` stays an absolute pixel count in working
    space rather than being scaled down with the canvas: the band exists so
    the model can see a seam to blend, and its usefulness is measured in the
    pixels of the image actually sent. The consequence is that the
    small-region limitation in `_seam_band_mask`'s docstring now applies in
    working-space pixels, so it bites at a larger fraction of the piece than
    it used to.
    """
    source_a = _load_downscaled(source_a_bytes, "RGB")
    mask_a = _load_downscaled(mask_a_bytes, "L")
    source_b = _load_downscaled(source_b_bytes, "RGB")
    mask_b = _load_downscaled(mask_b_bytes, "L")

    bbox_a = mask_a.getbbox()
    bbox_b = mask_b.getbbox()
    assert bbox_a is not None  # coverage validated > 0 at ingest (mask_validation)
    assert bbox_b is not None

    cropped_b = source_b.crop(bbox_b)
    cropped_mask_b = mask_b.crop(bbox_b)

    box_w = bbox_a[2] - bbox_a[0]
    box_h = bbox_a[3] - bbox_a[1]
    src_w, src_h = cropped_b.size

    # Aspect-preserving fit-inside, not stretch-to-fill (defect 2 above).
    scale = min(box_w / src_w, box_h / src_h)
    fit_w = max(1, round(src_w * scale))
    fit_h = max(1, round(src_h * scale))
    scaled_b = cropped_b.resize((fit_w, fit_h), Image.Resampling.LANCZOS)
    # NEAREST keeps the mask binary, same reason _load_downscaled uses it.
    scaled_mask_b = cropped_mask_b.resize((fit_w, fit_h), Image.Resampling.NEAREST)

    # Centre the fitted region inside A's box. fit_* <= box_*, so this window
    # is always inside bbox_a and therefore inside the image.
    off_x = bbox_a[0] + (box_w - fit_w) // 2
    off_y = bbox_a[1] + (box_h - fit_h) // 2

    # The graft is the INTERSECTION of the two silhouettes: inside A's region
    # (don't spill past where the client said the graft lands) and inside B's
    # painted shape (don't drag along the unpainted rest of B's crop —
    # defect 1 above). Both are binary, so multiply is a logical AND.
    window = (off_x, off_y, off_x + fit_w, off_y + fit_h)
    paste_alpha = ImageChops.multiply(mask_a.crop(window), scaled_mask_b)
    source_a.paste(scaled_b, (off_x, off_y), paste_alpha)

    # Full-frame record of what actually changed, for the seam band.
    graft_mask = Image.new("L", source_a.size, 0)
    graft_mask.paste(paste_alpha, (off_x, off_y))

    buf = BytesIO()
    source_a.save(buf, format="PNG")
    return buf.getvalue(), graft_mask


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
    # Both must have come from _load_downscaled. Image.composite would raise
    # on its own, but only with "images do not match" -- name the real cause,
    # since reaching here means the billed provider call has already happened.
    assert seam_band_mask.size == rough.size, (
        f"seam band {seam_band_mask.size} != rough composite {rough.size} "
        "-- both must be built at the same working resolution"
    )
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

    # The graft mask comes back from the composite step rather than being
    # rebuilt from the primary mask here. Two reasons, both load-bearing:
    # it is already at the composite's own working resolution (so
    # _composite_seam_result can never fail on a size mismatch after the
    # billed provider call), and since 2026-08-28 the graft is the
    # INTERSECTION of both silhouettes, which can be strictly smaller than
    # the primary mask. Building the band from the primary mask would mark
    # an edge that does not exist and miss the one that does.
    rough_composite_bytes, graft_mask = _build_rough_composite(
        primary_bytes, primary_mask_bytes, secondary_bytes, secondary_mask_bytes
    )
    seam_band = _seam_band_mask(graft_mask, settings.MIX_SEAM_BAND_PX)
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
