"""Runs a single MIX sub-job end-to-end. See phases/phase-20-mix.md Step 4,
and the 2026-08-31 rewrite note below, which supersedes most of what that
phase file describes.

Mirrors app/services/match_service.py's shape most closely of any operation:
rate-limit -> provider call -> cost event -> success/fail -> recompute, with
the provider's raw response stored as the client-facing OUTPUT asset. Separate
module, not a shared code path — see app/workers/mix.py for the thin Celery
wrapper (same split as recolor.py/recolor_service.py, match.py/match_service.py).

**2026-08-31 — MIX is now generative, not a deterministic graft. This is a
product decision, decided directly with the user, and it deletes the central
architectural claim Phase 20 was built around.**

What MIX used to be: a Pillow-only "rough composite" that cropped region B out
of photo B, scaled it to fit region A's bounding box, and pasted it into photo
A through the intersection of both painted silhouettes — followed by a Gemini
call scoped to a thin ring around the graft's seam, and a final compositing
step that discarded everything the provider changed outside that ring. The
output was guaranteed byte-identical to the rough composite outside the seam.

Why that is gone. The deterministic graft only produces a sensible result when
mask A and mask B are *similar shapes*, because it fits B's silhouette into A's
box. Three consecutive live client jobs proved they routinely are not:

- `fe7d6372` (2026-08-28): mask B was two curved bands whose shared bounding
  box was 39.5% painted, so 60% of the grafted content was unpainted mannequin
  — a beige blob in the middle of the client's pendant. Fixed by intersecting
  both silhouettes and preserving aspect ratio.
- `f9768456` (2026-08-30): with those fixes in, mask A was a compact bird
  pendant (481x441 at working resolution) and mask B two disjoint bands
  (1103x651). The aspect-preserving fit shrank B to 481x284 and centred it, so
  the empty gap between B's two blobs landed mid-pendant and the graft covered
  only 24.4% of what the client painted. Measured against the real stored
  assets, the output *did* differ from the primary photo (26,472 px changed,
  mean diff 42.3 inside the graft) — but visually it read as a small patch of
  smudged gold in one corner of the pendant, not a merge. The client's
  verdict, looking at the delivered image, was "nothing changed."

The lesson is not that the graft had another bug to fix. It is that
"deterministically relocate pixels from photo B into photo A" is the wrong
operation for what the client actually wants, which is: *here are two pieces,
design me one that combines them.* That is a generative request, and asking
Gemini to do it directly removes the entire class of geometry failures above —
there is no crop, no scale-to-fit, no silhouette intersection, and therefore
nothing for mismatched mask shapes to break.

**What MIX is now.** Both photos go to Gemini as reference images, each with
its client-painted region marked in a distinct colour, plus a prompt asking for
a single new piece combining the two marked elements. The provider's raw
response is the client-facing output. Consequences, stated plainly because they
are real losses, not free wins:

- **The byte-identical guarantee is gone entirely.** docs/business-rules.md §16
  no longer claims the output matches anything pixel-for-pixel; it can't, and
  pretending otherwise would be worse than dropping it. The output is a
  generated design concept.
- **The output may idealize.** Gemini can invent prong counts, facet geometry
  and chain links the physical pieces don't have — the same hallucination risk
  docs/ai-integration.md's Mode B has always carried. The client accepted this
  explicitly: a MIX output is a mockup of a piece that does not exist yet, not
  a photograph of one that does.
- **MIX's "no QA gate" reason changes.** It used to be "provably identical to
  the rough composite outside the seam, verified by a deterministic test." It
  is now MATCH's reason: the output is *supposed* to differ from both inputs,
  so a subject-preservation similarity judge asks the wrong question. See
  docs/business-rules.md §7/§16.

**Why the masks are still required, and still colour-marked.** They are the
only way the client says *which* element of each photo they mean — the bird
pendant rather than the whole necklace, this chain rather than that clasp.
What changed is that the mask now drives attention, not geometry: it is burned
into the photo as a translucent tint plus a hard contour, and the model is told
to read those marks as identifying an element rather than as part of the
design. Nothing is cropped out and nothing is relocated, so a mask whose shape
doesn't match the other mask's shape is no longer a problem at all.

This also degrades gracefully against a real, repeated user error. The /ui mask
tool is a brush, not a lasso, and clients have circled a region instead of
painting over it three separate times. Under the old graft that produced a
ring-shaped region and grafted garbage; under a highlight it produces a ring
drawn around the element, which still communicates "this one" perfectly well.

**Why a translucent tint and not RECOLOR's solid magenta fill.** Mode E burns
an *opaque* fill because it wants that region replaced — hiding what's under it
costs nothing. Here the marked region is exactly what Gemini must reproduce, so
covering it would defeat the purpose. The solid contour
(`_HIGHLIGHT_OUTLINE_PX`) is what actually identifies the region; the tint
(`_HIGHLIGHT_TINT_ALPHA`) only disambiguates inside from outside for concave
shapes, so it is kept faint enough not to shift the material's apparent colour
— see that constant's own note for the measurement behind its value.

**Colour choice is deliberate:** magenta for the primary and cyan for the
secondary, because no real jewelry is either — gold, silver, ruby, emerald,
sapphire and pearl are all far from both in hue, so neither mark can be
mistaken for the piece's own material.

**Memory.** All four inputs still decode at `settings.WORKING_MAX_EDGE` via
`_load_downscaled` (2026-08-27 OOM fix — a real 12.6 MP client upload needed
~187 MB against ~160 MB of headroom on the 512 MB instance and was SIGKILLed
before every Gemini attempt). This rewrite strictly improves on that: two
downscaled highlight images are held instead of a full compositing chain, and
`_composite_seam_result`'s extra decode of the rough composite alongside the
provider's output is gone. The output's resolution is now whatever Gemini
returns rather than being capped by our own canvas.

**Unvalidated against a real model call at the time of writing** — the two-image
highlight strategy has the same status Phase 20's seam-band strategy had, except
that a real `GEMINI_API_KEY` now exists, so this one can and should be checked
against job `f9768456`'s own stored inputs before it is trusted.
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

# Two marker colours, one per source photo, named in the prompt. Chosen to be
# unreachable by real jewelry: gold, silver, rose gold, ruby, emerald, sapphire
# and pearl are all far from pure magenta and pure cyan in hue, so a mark can
# never be mistaken for the piece's own material. Magenta matches the colour
# recolor_service already uses for the same "the model is being told about this
# region" purpose; cyan is its complement and is the new one.
_HIGHLIGHT_PRIMARY = (255, 0, 255)
_HIGHLIGHT_SECONDARY = (0, 255, 255)

# Deliberately translucent, unlike recolor_service._build_overlay's opaque
# fill. RECOLOR hides the marked region because it wants it replaced; MIX must
# keep it legible because it wants it reproduced. See this module's docstring.
#
# 0.10 was measured, not guessed. Rendered against job `f9768456`'s real
# secondary photo — two ornate gold bands set with rubies — at 0.30 the cyan
# mark turned the gold visibly green and washed the rubies out, which risks
# Gemini reproducing green enamel instead of gold. At 0.15 a cool cast
# remained. At 0.08 the gold read as unambiguously gold and the rubies as red,
# with the contour no less legible. 0.10 sits just above that, keeping a little
# more fill signal for large concave regions whose interior is far from any
# edge, while staying in material-preserving territory.
#
# The contour, not the tint, is what identifies the region — which is why the
# tint can afford to be this faint.
_HIGHLIGHT_TINT_ALPHA = 0.10
_HIGHLIGHT_OUTLINE_PX = 3


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
    `ImageFilter.MaxFilter` mechanism. Used only to build the contour ring
    (`_outline_mask`), never applied on its own to a compositing alpha.
    """
    if px <= 0:
        return mask
    size = px * 2 + 1
    return mask.filter(ImageFilter.MaxFilter(size))


def _outline_mask(mask: "Image.Image", px: int) -> "Image.Image":
    """A ring `px` pixels wide straddling the mask's boundary —
    `dilate(mask, px) - erode(mask, px)`. Drawn in the marker colour on top of
    the tint so the edge of the client's painted region is unambiguous even
    where a 30% tint reads faintly over bright polished metal.

    Straddling the boundary rather than sitting strictly inside it is
    deliberate: half the stroke falls on the element and half on its
    surroundings, so the contour never eats a meaningful amount of the detail
    Gemini is being asked to reproduce.

    This is the same dilate-minus-erode shape the deleted `_seam_band_mask`
    used, kept because the geometry is genuinely the same; its role is not.
    That ring told the provider *where to blend two pasted images together*
    and drove a post-call compositing step. This one is purely a visual
    annotation — nothing downstream reads it, and no guarantee depends on it.
    """
    if px <= 0:
        return Image.new("L", mask.size, 0)
    return ImageChops.subtract(_dilate(mask, px), _erode(mask, px))


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
    guarantees a binary 0/255 mask on ingest and `_outline_mask`'s
    dilate-minus-erode assumes it, so a smooth filter would introduce
    intermediate values that quietly soften the contour into a gradient.
    Photos use LANCZOS, where smoothness is what you want.
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


def _build_highlight(source_bytes: bytes, mask_bytes: bytes, color: tuple[int, int, int]) -> bytes:
    """Mark the client's painted region on one source photo, without altering
    what is inside it beyond a readable tint. Returns a full-frame PNG — this
    is one of the two reference images sent to Gemini.

    Two marks, both in `color`: a `_HIGHLIGHT_TINT_ALPHA` blend across the
    whole painted region, and a solid `_HIGHLIGHT_OUTLINE_PX` contour on its
    boundary. See this module's docstring for why this is translucent where
    RECOLOR's equivalent overlay is opaque, and why these two colours.

    Everything outside the painted region is the downscaled source photo,
    untouched — the model needs the surrounding piece for context (that this
    pendant hangs from a chain, at that scale, in that metal), so masking it
    away would lose information the design depends on.
    """
    source = _load_downscaled(source_bytes, "RGB")
    mask = _load_downscaled(mask_bytes, "L")
    # mask_validation guarantees mask dims == source dims at ingest, and both
    # go through the same _working_size, so this holds by construction.
    # Assert rather than let Image.composite raise its generic "images do not
    # match" — this runs before the billed provider call, so a loud, named
    # failure here is strictly better than a confusing one.
    assert mask.size == source.size, (
        f"mask {mask.size} != source {source.size} — mask_validation should "
        "have rejected this pair at ingest"
    )

    fill = Image.new("RGB", source.size, color)
    tinted = Image.blend(source, fill, _HIGHLIGHT_TINT_ALPHA)
    highlighted = Image.composite(tinted, source, mask)
    highlighted = Image.composite(fill, highlighted, _outline_mask(mask, _HIGHLIGHT_OUTLINE_PX))

    buf = BytesIO()
    highlighted.save(buf, format="PNG")
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

    # Two reference images, one per piece, each marked in its own colour. The
    # prompt names both colours, so the order they're passed in is part of the
    # contract: primary (magenta) first, secondary (cyan) second.
    reference_images = [
        _build_highlight(primary_bytes, primary_mask_bytes, _HIGHLIGHT_PRIMARY),
        _build_highlight(secondary_bytes, secondary_mask_bytes, _HIGHLIGHT_SECONDARY),
    ]

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
    """Stores the provider's raw response as the OUTPUT asset.

    No compositing step, unlike RECOLOR (Mode E) and unlike MIX itself before
    2026-08-31. There is nothing to composite back onto: the output is a newly
    generated piece, not an edit of either uploaded photo. Same shape as
    match_service._complete_success, for the same reason — see this module's
    docstring.
    """
    assert sub_job.angle is None
    storage_path = storage_service.build_storage_path(job_id, "mix", AssetKind.OUTPUT, "png")
    storage_service.upload_bytes(
        settings.BUCKET_OUTPUTS, storage_path, result.image_bytes, "image/png"
    )
    output_asset = assets_repo.create_asset(
        session,
        job_id=job_id,
        sub_job_id=sub_job.id,
        kind=AssetKind.OUTPUT,
        bucket=settings.BUCKET_OUTPUTS,
        storage_path=storage_path,
        mime_type="image/png",
        bytes_=len(result.image_bytes),
        expires_at=retention_policy.compute_expires_at(AssetKind.OUTPUT),
    )
    await session.flush()

    sub_job.output_asset_id = output_asset.id
    sub_job.prompt_snapshot = prompt
    sub_job.model_version = result.model_version
    sub_job.seed = seed
    # Straight to COMPLETED, never QA_REVIEW — see this module's docstring's
    # note on why MIX's no-QA-gate reason is now MATCH's.
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
