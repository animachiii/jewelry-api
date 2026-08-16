"""Job creation (real, Phase 2) and retry precondition checks
(check_retry_preconditions — real execution wired up in
app/services/retry_service.py and app/api/v2/retry.py, Phase 8).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.generate import (
    GenerateJobRequest,
    JobAcceptedResponse,
    ResolvedAnglePlan,
    ResolvedVariantPlan,
)
from app.config import settings
from app.core import ratelimit
from app.core.errors import AppError, ErrorCode, QuotaExceededError, RateLimitError
from app.core.idempotency import IdempotencyKeyConflictError
from app.db.models.api_clients import ApiClient
from app.db.models.assets import Asset
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import Angle, AssetKind, Operation, QAStatus, SourceType, SubJobStatus
from app.db.models.jobs import SubJob
from app.db.repositories import assets as assets_repo
from app.db.repositories import job_events as job_events_repo
from app.db.repositories import jobs as jobs_repo
from app.services import image_validation, mask_validation, retention_policy, storage_service
from app.services.image_validation import ImageMetadata

MAX_RETRY_ATTEMPTS = 3
POLL_AFTER_MS = 1500


class CategoryNotFoundError(AppError):
    code = ErrorCode.CATEGORY_NOT_FOUND
    http_status = 422


class CategoryInactiveError(AppError):
    code = ErrorCode.CATEGORY_INACTIVE
    http_status = 422


class AngleNotEnabledError(AppError):
    code = ErrorCode.ANGLE_NOT_ENABLED
    http_status = 422


class SyntheticNotAllowedError(AppError):
    code = ErrorCode.SYNTHETIC_NOT_ALLOWED
    http_status = 422


class AssetNotFoundError(AppError):
    code = ErrorCode.ASSET_NOT_FOUND
    http_status = 422


class AssetNotOwnedError(AppError):
    code = ErrorCode.ASSET_NOT_OWNED
    http_status = 422


class NoAnglesRequestedError(AppError):
    code = ErrorCode.NO_ANGLES_REQUESTED
    http_status = 422


class SubJobNotRetryableError(AppError):
    code = ErrorCode.SUBJOB_NOT_RETRYABLE
    http_status = 409


class RetryLimitExceededError(AppError):
    code = ErrorCode.RETRY_LIMIT_EXCEEDED
    http_status = 409


class InputAssetExpiredError(AppError):
    code = ErrorCode.INPUT_ASSET_EXPIRED
    http_status = 409


class OperationDisabledError(AppError):
    code = ErrorCode.OPERATION_DISABLED
    http_status = 422


class PresetNotFoundError(AppError):
    code = ErrorCode.PRESET_NOT_FOUND
    http_status = 422


class PresetInactiveError(AppError):
    code = ErrorCode.PRESET_INACTIVE
    http_status = 422


class PaletteNotFoundError(AppError):
    code = ErrorCode.PALETTE_NOT_FOUND
    http_status = 422


class PaletteInactiveError(AppError):
    code = ErrorCode.PALETTE_INACTIVE
    http_status = 422


class AngleJobRetryNotAllowedError(AppError):
    """An ANGLE_GENERATION job must retry a specific angle via
    /jobs/{job_id}/angles/{angle}/retry — the job-level route is for
    single-sub-job (background) jobs only. See
    phases/phase-15-background-operations.md Step 4.
    """

    code = ErrorCode.ANGLE_JOB_RETRY_NOT_ALLOWED
    http_status = 409


def validate_operation_angle_consistency(operation: Operation, angle: Angle | None) -> None:
    """The cross-table CHECK Postgres can't express (angle non-null iff the
    parent job is ANGLE_GENERATION — see docs/schema.md and
    phases/phase-15-background-operations.md Step 2). Every code path that
    builds a SubJob must call this before `jobs_repo.create_sub_job`.
    """
    if operation == Operation.ANGLE_GENERATION and angle is None:
        raise ValueError(f"{operation.value} sub-jobs must specify an angle.")
    if operation != Operation.ANGLE_GENERATION and angle is not None:
        raise ValueError(
            f"{operation.value} sub-jobs must not specify an angle (got {angle.value})."
        )


def _sub_job_label(sub_job: SubJob) -> str:
    """Angle jobs describe themselves by angle; a background job's single
    sub-job has no angle at all. Shared by both the per-angle retry route
    and the single-sub-job retry route Step 4 adds on top of this same
    function — see phases/phase-15-background-operations.md Step 4.
    """
    return f"angle {sub_job.angle.value}" if sub_job.angle is not None else "the sub-job"


def check_retry_preconditions(sub_job: SubJob, input_asset: Asset | None) -> None:
    """Raises the specific 409 for the first violated precondition.
    See docs/business-rules.md §5.
    """
    label = _sub_job_label(sub_job)
    angle_detail = sub_job.angle.value if sub_job.angle is not None else None
    if sub_job.status != SubJobStatus.FAILED:
        raise SubJobNotRetryableError(
            f"Sub-job for {label} is {sub_job.status.value}, not FAILED.",
            details={"angle": angle_detail, "status": sub_job.status.value},
        )
    if sub_job.attempt_count >= MAX_RETRY_ATTEMPTS:
        ceiling = f"({MAX_RETRY_ATTEMPTS} attempts)"
        subject = f"Angle {sub_job.angle.value}" if sub_job.angle is not None else "The sub-job"
        raise RetryLimitExceededError(
            f"{subject} has reached the retry ceiling {ceiling}.",
            details={"angle": angle_detail, "attempt_count": sub_job.attempt_count},
        )
    if sub_job.source_type == SourceType.UPLOADED:
        if input_asset is None:
            raise InputAssetExpiredError(
                f"No input asset found for {label}.",
                details={"angle": angle_detail},
            )
        if input_asset.expires_at is not None and input_asset.expires_at <= datetime.now(UTC):
            raise InputAssetExpiredError(
                f"Input asset for {label} has expired. "
                "Submit a new job — the image cannot be regenerated.",
                details={"angle": angle_detail},
            )


def check_qa_retry_preconditions(sub_job: SubJob) -> None:
    """Raises the specific 409 for the first violated precondition on a
    retry-QA-only request — see
    docs/superpowers/specs/2026-08-13-background-qa-preview-and-retry-design.md.
    Distinct from check_retry_preconditions: this accepts QA_REVIEW+FLAGGED
    (the judge call failed or scored too low) rather than FAILED, and never
    touches input_asset_id -- the existing output image is reused as-is, no
    input re-download needed. Shares MAX_RETRY_ATTEMPTS with the regenerate
    path (one counter, same precedent check_retry_preconditions already
    follows for generation retries).
    """
    label = _sub_job_label(sub_job)
    if sub_job.status != SubJobStatus.QA_REVIEW or sub_job.qa_status != QAStatus.FLAGGED:
        raise SubJobNotRetryableError(
            f"Sub-job for {label} is {sub_job.status.value} "
            f"(qa_status={sub_job.qa_status.value}), not QA_REVIEW+FLAGGED.",
            details={"status": sub_job.status.value, "qa_status": sub_job.qa_status.value},
        )
    if sub_job.attempt_count >= MAX_RETRY_ATTEMPTS:
        raise RetryLimitExceededError(
            f"The sub-job has reached the retry ceiling ({MAX_RETRY_ATTEMPTS} attempts).",
            details={"attempt_count": sub_job.attempt_count},
        )


def resolve_angle_plan(body: GenerateJobRequest) -> list[ResolvedAnglePlan]:
    plan = []
    for angle, spec in body.angles.items():
        if spec.mode == "skipped":
            plan.append(
                ResolvedAnglePlan(
                    angle=angle, source_type=SourceType.UPLOADED, status=SubJobStatus.SKIPPED
                )
            )
        elif spec.mode == "synthetic":
            plan.append(
                ResolvedAnglePlan(
                    angle=angle, source_type=SourceType.SYNTHETIC, status=SubJobStatus.PENDING
                )
            )
        else:
            plan.append(
                ResolvedAnglePlan(
                    angle=angle,
                    source_type=SourceType.UPLOADED,
                    status=SubJobStatus.PENDING,
                    storage_path=spec.storage_path,
                )
            )
    return plan


def find_category(config_version: ConfigVersion, category_code: str) -> dict[str, Any] | None:
    for cat in config_version.payload["categories"]:
        if cat["code"] == category_code:
            return dict(cat)
    return None


def find_operation_config(
    config_version: ConfigVersion, operation: Operation
) -> dict[str, Any] | None:
    """`operations.<OP>` lives inside `payload.global`, not as a top-level
    payload key — see phases/phase-15-background-operations.md Step 3: only
    the `global` block survives a Sheets sync untouched
    (config_sync_service.normalize_sheet_rows rebuilds `categories` fresh
    every time), so anything a sync doesn't model has to live there to
    persist.
    """
    operations: dict[str, Any] = config_version.payload.get("global", {}).get("operations", {})
    return operations.get(operation.value)


def resolve_match_prompt(config_version: ConfigVersion, target_category: str) -> str:
    """MATCH's `operations.MATCH.prompt` template (phases/phase-18-match.md
    Step 2, seeded by migration 0014) carries a genuine runtime
    `{target_category}` placeholder — the first operation prompt in this
    codebase that isn't a complete, final string at rest. `str.format` is
    the right tool, not a custom regex/replace: if the seeded template ever
    references some other unresolved placeholder (a typo, or a field this
    function doesn't supply), `.format` raises `KeyError` on its own — the
    fail-loud behavior the checkpoint wants, so a prompt with a literal
    `{...}` left in it never ships to Gemini. That KeyError is left to
    propagate uncaught; do not add a fallback that silently returns the
    unsubstituted template.
    """
    op_config = find_operation_config(config_version, Operation.MATCH) or {}
    template = str(op_config.get("prompt", ""))
    return template.format(target_category=target_category)


def resolve_recolor_prompt(config_version: ConfigVersion, palette_code: str) -> str:
    """RECOLOR's `operations.RECOLOR.prompt` template (phases/phase-19-recolor.md
    Step 2, seeded by migration 0016) carries a genuine runtime
    `{palette_prompt}` placeholder — same `str.format`/fail-loud-on-KeyError
    posture `resolve_match_prompt` already established for
    `{target_category}`: a prompt with a literal `{...}` left in it must
    never ship to Gemini, so an unresolvable placeholder in a future config
    edit surfaces as an uncaught KeyError here, not a silently degraded
    prompt.

    Callers must have already validated `palette_code` via `validate_palette`
    — this function assumes the palette entry exists.
    """
    op_config = find_operation_config(config_version, Operation.RECOLOR) or {}
    template = str(op_config.get("prompt", ""))
    palette_entry = find_palette_entry(config_version, palette_code)
    assert palette_entry is not None  # validated at job-creation time; config is pinned, immutable
    return template.format(palette_prompt=palette_entry["prompt_phrase"])


def resolve_mix_prompt(config_version: ConfigVersion) -> str:
    """MIX's `operations.MIX.prompt` template (phases/phase-20-mix.md
    Step 2, seeded by migration 0018) carries **no runtime template
    placeholder** — unlike `resolve_match_prompt`'s `{target_category}` or
    `resolve_recolor_prompt`'s `{palette_prompt}`, it's a complete, final
    string at rest. There is nothing per-request to substitute into it: the
    seam-band overlay itself (app/services/mix_service.py) carries the
    visual information, not a text parameter. Kept as its own function
    rather than inlined at the call site for the same reason
    `resolve_match_prompt`/`resolve_recolor_prompt` are — one place per
    operation to look up how its prompt is resolved.
    """
    op_config = find_operation_config(config_version, Operation.MIX) or {}
    return str(op_config.get("prompt", ""))


def find_palette_entry(config_version: ConfigVersion, palette_code: str) -> dict[str, Any] | None:
    for entry in config_version.payload.get("global", {}).get("palette", []):
        if entry["code"] == palette_code:
            return dict(entry)
    return None


def validate_palette(config_version: ConfigVersion, palette_code: str) -> dict[str, Any]:
    entry = find_palette_entry(config_version, palette_code)
    if entry is None:
        raise PaletteNotFoundError(
            f"Palette code {palette_code} not found.", details={"palette_code": palette_code}
        )
    if not entry.get("is_active", False):
        raise PaletteInactiveError(
            f"Palette code {palette_code} is not active.", details={"palette_code": palette_code}
        )
    return entry


def find_preset(config_version: ConfigVersion, preset_code: str) -> dict[str, Any] | None:
    for preset in config_version.payload.get("global", {}).get("background_presets", []):
        if preset["code"] == preset_code:
            return dict(preset)
    return None


def resolve_operation_unit_cost(config_version: ConfigVersion, operation: Operation) -> float:
    """docs/business-rules.md §10: unit_cost_usd always comes from
    configuration, never a hardcoded constant. Per-operation cost, falling
    back to `global.unit_cost_usd` when the operation doesn't set its own —
    same fallback shape `operations.BACKGROUND_REPLACEMENT` uses in the
    phase spec's own example payload (no per-operation cost given).
    """
    global_block = config_version.payload.get("global", {})
    op_config = find_operation_config(config_version, operation) or {}
    return float(op_config.get("unit_cost_usd", global_block.get("unit_cost_usd", 0.0)))


def validate_operation_enabled(
    config_version: ConfigVersion, operation: Operation
) -> dict[str, Any]:
    """Raises 422 OPERATION_DISABLED for both "no config entry at all" and
    "enabled: false" — an operation with no entry is not enabled, same
    treatment, no separate NOT_FOUND code needed.
    """
    op_config = find_operation_config(config_version, operation)
    if op_config is None or not op_config.get("enabled", False):
        raise OperationDisabledError(
            f"{operation.value} is not enabled.", details={"operation": operation.value}
        )
    return op_config


def validate_preset(config_version: ConfigVersion, preset_code: str) -> dict[str, Any]:
    preset = find_preset(config_version, preset_code)
    if preset is None:
        raise PresetNotFoundError(
            f"Preset {preset_code} not found.", details={"preset_code": preset_code}
        )
    if not preset.get("is_active", False):
        raise PresetInactiveError(
            f"Preset {preset_code} is not active.", details={"preset_code": preset_code}
        )
    return preset


def _validate_request(
    body: GenerateJobRequest, category: dict[str, Any], client_id: uuid.UUID
) -> dict[Angle, ImageMetadata]:
    """See docs/api-routes.md /generate validation rules 1-5 (rule 1, category
    lookup, already happened by the time this is called).

    Phase 4 adds a real content check to rule 4: existence in the bucket is
    no longer sufficient. The object is downloaded and structurally
    validated as a decodable image (see app/services/image_validation.py) —
    a client PUTting arbitrary bytes to a signed upload URL used to sail
    straight through to job creation. Returns the extracted image metadata
    per uploaded angle so the caller does not have to re-download.
    """
    if not category["is_active"]:
        raise CategoryInactiveError(
            f"Category {category['code']} is not active.",
            details={"category_code": category["code"]},
        )

    requested_count = 0
    image_metadata: dict[Angle, ImageMetadata] = {}
    for angle, spec in body.angles.items():
        angle_config = category["angles"].get(angle.value, {})
        if spec.mode == "skipped":
            continue
        requested_count += 1

        if not angle_config.get("enabled", False):
            raise AngleNotEnabledError(
                f"Angle {angle.value} is not enabled for category {category['code']}.",
                details={"category_code": category["code"], "angle": angle.value},
            )
        if spec.mode == "synthetic" and not angle_config.get("synthetic_allowed", False):
            raise SyntheticNotAllowedError(
                f"Synthetic generation is not allowed for angle {angle.value} "
                f"in category {category['code']}.",
                details={"category_code": category["code"], "angle": angle.value},
            )
        if spec.mode == "uploaded":
            assert spec.storage_path is not None
            if not storage_service.exists(settings.BUCKET_INPUTS, spec.storage_path):
                raise AssetNotFoundError(
                    f"No uploaded asset found at {spec.storage_path}.",
                    details={"storage_path": spec.storage_path},
                )
            if not spec.storage_path.startswith(f"pending/{client_id}/"):
                raise AssetNotOwnedError(
                    f"storage_path {spec.storage_path} does not belong to this client.",
                    details={"storage_path": spec.storage_path},
                )
            image_metadata[angle] = image_validation.inspect_and_validate(
                settings.BUCKET_INPUTS, spec.storage_path
            )

    if requested_count == 0:
        raise NoAnglesRequestedError("At least one angle must not be skipped.")

    return image_metadata


async def _build_accepted_response(
    job_id: uuid.UUID, body: GenerateJobRequest
) -> JobAcceptedResponse:
    return JobAcceptedResponse(
        job_id=str(job_id),
        status="PENDING",
        angles=resolve_angle_plan(body),
        poll_after_ms=POLL_AFTER_MS,
    )


async def _enforce_rate_limit_and_quota(session: AsyncSession, client: ApiClient) -> None:
    """Shared by both `/generate` and the Phase 15 background routes — same
    per-client budget, same docs/business-rules.md §8 reasoning: an
    idempotency replay never reaches this call, so it never consumes a
    token or quota slot.
    """
    if not await ratelimit.allow(str(client.id), client.rate_limit_per_min):
        raise RateLimitError(
            "Rate limit exceeded.",
            details={"limit_per_minute": client.rate_limit_per_min},
            retry_after_seconds=60,
        )
    if client.daily_job_quota is not None:
        created_today = await jobs_repo.count_created_today(session, client.id)
        if created_today >= client.daily_job_quota:
            now = datetime.now(UTC)
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            raise QuotaExceededError(
                "Daily job quota exceeded.",
                details={"daily_job_quota": client.daily_job_quota},
                retry_after_seconds=int((tomorrow - now).total_seconds()),
            )


async def create_job_for_request(
    session: AsyncSession,
    client: ApiClient,
    config_version: ConfigVersion,
    body: GenerateJobRequest,
    idempotency_key: str,
    payload_hash: str,
) -> JobAcceptedResponse:
    """Implements POST /generate for real. See phases/phase-2-data-model.md
    Step 4 and docs/business-rules.md §1, §8.
    """
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise IdempotencyKeyConflictError(
                "This Idempotency-Key was already used with a different request body."
            )
        return await _build_accepted_response(existing.id, body)

    # docs/api-routes.md: "Returns 429 when the client's rate limit or daily
    # quota is exceeded" — a replay above never reaches here, so it never
    # consumes a token or counts toward the quota (docs/business-rules.md
    # §8: a replay doesn't bill the provider either; same reasoning).
    await _enforce_rate_limit_and_quota(session, client)

    category = find_category(config_version, body.category_code)
    if category is None:
        raise CategoryNotFoundError(
            f"Category {body.category_code} not found.",
            details={"category_code": body.category_code},
        )
    image_metadata = _validate_request(body, category, client.id)

    requested_angles = sum(1 for spec in body.angles.values() if spec.mode != "skipped")

    job = jobs_repo.create_job(
        session,
        client_id=client.id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        category_code=body.category_code,
        config_version_id=config_version.id,
        requested_angles=requested_angles,
        sku_reference=body.sku_reference,
        metadata=body.metadata,
    )

    try:
        await session.flush()  # assigns job.id
    except IntegrityError:
        await session.rollback()
        return await _handle_replay_race(session, client, idempotency_key, payload_hash, body)

    for angle, spec in body.angles.items():
        validate_operation_angle_consistency(Operation.ANGLE_GENERATION, angle)
        if spec.mode == "skipped":
            jobs_repo.create_sub_job(
                session,
                job_id=job.id,
                angle=angle,
                status=SubJobStatus.SKIPPED,
                source_type=SourceType.UPLOADED,
            )
        elif spec.mode == "synthetic":
            jobs_repo.create_sub_job(
                session,
                job_id=job.id,
                angle=angle,
                status=SubJobStatus.PENDING,
                source_type=SourceType.SYNTHETIC,
            )
        else:
            assert spec.storage_path is not None
            meta = image_metadata[angle]
            asset = assets_repo.create_asset(
                session,
                job_id=job.id,
                kind=AssetKind.INPUT,
                bucket=settings.BUCKET_INPUTS,
                storage_path=spec.storage_path,
                mime_type=meta.mime_type,
                width_px=meta.width_px,
                height_px=meta.height_px,
                bytes_=meta.bytes,
                checksum_sha256=meta.checksum_sha256,
                expires_at=retention_policy.compute_expires_at(AssetKind.INPUT),
            )
            await session.flush()  # assigns asset.id
            jobs_repo.create_sub_job(
                session,
                job_id=job.id,
                angle=angle,
                status=SubJobStatus.PENDING,
                source_type=SourceType.UPLOADED,
                input_asset_id=asset.id,
            )

    job_events_repo.record_event(
        session,
        job.id,
        "JOB_CREATED",
        to_status=job.status.value,
        detail={"category_code": body.category_code, "requested_angles": requested_angles},
    )

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await _handle_replay_race(session, client, idempotency_key, payload_hash, body)

    # Phase 7: dispatch real work only for a genuinely new job — an
    # idempotency replay (both branches above) must never re-dispatch.
    from app.workers.orchestration import fan_out_job

    fan_out_job.delay(str(job.id))

    return await _build_accepted_response(job.id, body)


async def _handle_replay_race(
    session: AsyncSession,
    client: ApiClient,
    idempotency_key: str,
    payload_hash: str,
    body: GenerateJobRequest,
) -> JobAcceptedResponse:
    """A concurrent request created the job for this key between our
    idempotency check and our insert. Treat it exactly like a replay.
    """
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is None:
        raise AppError(
            "Idempotency key conflict could not be resolved.", code=ErrorCode.INTERNAL_ERROR
        )
    if existing.payload_hash != payload_hash:
        raise IdempotencyKeyConflictError(
            "This Idempotency-Key was already used with a different request body."
        )
    return await _build_accepted_response(existing.id, body)


async def _build_background_accepted_response(
    job_id: uuid.UUID, storage_path: str
) -> JobAcceptedResponse:
    return JobAcceptedResponse(
        job_id=str(job_id),
        status="PENDING",
        angles=[
            ResolvedAnglePlan(
                angle=None,
                source_type=SourceType.UPLOADED,
                status=SubJobStatus.PENDING,
                storage_path=storage_path,
            )
        ],
        poll_after_ms=POLL_AFTER_MS,
    )


async def create_background_job_for_request(
    session: AsyncSession,
    client: ApiClient,
    config_version: ConfigVersion,
    operation: Operation,
    storage_path: str,
    preset_code: str | None,
    sku_reference: str | None,
    metadata: dict[str, Any],
    idempotency_key: str,
    payload_hash: str,
    background_storage_path: str | None = None,
) -> JobAcceptedResponse:
    """Implements POST /background/remove and POST /background/replace —
    see phases/phase-15-background-operations.md Steps 4-5. Mirrors
    create_job_for_request's shape: idempotency -> rate limit/quota ->
    validate -> create -> commit -> dispatch `background.process`
    (Step 5), same "only dispatch a genuinely new job, never a replay"
    rule create_job_for_request follows for fan_out_job.

    `background_storage_path`, when given, is a client-uploaded custom
    background photo for BACKGROUND_REPLACEMENT — it is validated and
    linked as an INPUT asset via sub_jobs.background_asset_id.
    """
    assert operation != Operation.ANGLE_GENERATION

    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise IdempotencyKeyConflictError(
                "This Idempotency-Key was already used with a different request body."
            )
        return await _build_background_accepted_response(existing.id, storage_path)

    await _enforce_rate_limit_and_quota(session, client)

    validate_operation_enabled(config_version, operation)
    if operation == Operation.BACKGROUND_REPLACEMENT:
        # enforced by BackgroundReplaceRequest's mutual-exclusivity validator
        assert preset_code is not None or background_storage_path is not None
        if preset_code is not None:
            validate_preset(config_version, preset_code)

    if not storage_service.exists(settings.BUCKET_INPUTS, storage_path):
        raise AssetNotFoundError(
            f"No uploaded asset found at {storage_path}.",
            details={"storage_path": storage_path},
        )
    if not storage_path.startswith(f"pending/{client.id}/"):
        raise AssetNotOwnedError(
            f"storage_path {storage_path} does not belong to this client.",
            details={"storage_path": storage_path},
        )
    meta = image_validation.inspect_and_validate(settings.BUCKET_INPUTS, storage_path)

    background_meta: ImageMetadata | None = None
    if background_storage_path is not None:
        if not storage_service.exists(settings.BUCKET_INPUTS, background_storage_path):
            raise AssetNotFoundError(
                f"No uploaded asset found at {background_storage_path}.",
                details={"storage_path": background_storage_path},
            )
        if not background_storage_path.startswith(f"pending/{client.id}/"):
            raise AssetNotOwnedError(
                f"storage_path {background_storage_path} does not belong to this client.",
                details={"storage_path": background_storage_path},
            )
        background_meta = image_validation.inspect_and_validate(
            settings.BUCKET_INPUTS, background_storage_path
        )

    job = jobs_repo.create_job(
        session,
        client_id=client.id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        category_code=None,
        preset_code=preset_code,
        config_version_id=config_version.id,
        requested_angles=1,
        sku_reference=sku_reference,
        metadata=metadata,
        operation=operation,
    )

    try:
        await session.flush()  # assigns job.id
    except IntegrityError:
        await session.rollback()
        return await _handle_background_replay_race(
            session, client, idempotency_key, payload_hash, storage_path
        )

    validate_operation_angle_consistency(operation, None)
    asset = assets_repo.create_asset(
        session,
        job_id=job.id,
        kind=AssetKind.INPUT,
        bucket=settings.BUCKET_INPUTS,
        storage_path=storage_path,
        mime_type=meta.mime_type,
        width_px=meta.width_px,
        height_px=meta.height_px,
        bytes_=meta.bytes,
        checksum_sha256=meta.checksum_sha256,
        expires_at=retention_policy.compute_expires_at(AssetKind.INPUT),
    )
    await session.flush()  # assigns asset.id

    background_asset_id: uuid.UUID | None = None
    if background_storage_path is not None:
        assert background_meta is not None
        background_asset = assets_repo.create_asset(
            session,
            job_id=job.id,
            kind=AssetKind.INPUT,
            bucket=settings.BUCKET_INPUTS,
            storage_path=background_storage_path,
            mime_type=background_meta.mime_type,
            width_px=background_meta.width_px,
            height_px=background_meta.height_px,
            bytes_=background_meta.bytes,
            checksum_sha256=background_meta.checksum_sha256,
            expires_at=retention_policy.compute_expires_at(AssetKind.INPUT),
        )
        await session.flush()  # assigns background_asset.id
        background_asset_id = background_asset.id

    sub_job = jobs_repo.create_sub_job(
        session,
        job_id=job.id,
        angle=None,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        input_asset_id=asset.id,
        background_asset_id=background_asset_id,
    )
    await session.flush()  # assigns sub_job.id

    job_events_repo.record_event(
        session,
        job.id,
        "JOB_CREATED",
        to_status=job.status.value,
        detail={
            "operation": operation.value,
            "preset_code": preset_code,
            "background_storage_path": background_storage_path,
        },
    )

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await _handle_background_replay_race(
            session, client, idempotency_key, payload_hash, storage_path
        )

    # Phase 15 Step 5: dispatch real work only for a genuinely new job — an
    # idempotency replay (both branches above) must never re-dispatch, same
    # rule create_job_for_request follows for fan_out_job.
    from app.workers.background import process_task

    process_task.delay(str(sub_job.id))

    return await _build_background_accepted_response(job.id, storage_path)


async def _handle_background_replay_race(
    session: AsyncSession,
    client: ApiClient,
    idempotency_key: str,
    payload_hash: str,
    storage_path: str,
) -> JobAcceptedResponse:
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is None:
        raise AppError(
            "Idempotency key conflict could not be resolved.", code=ErrorCode.INTERNAL_ERROR
        )
    if existing.payload_hash != payload_hash:
        raise IdempotencyKeyConflictError(
            "This Idempotency-Key was already used with a different request body."
        )
    return await _build_background_accepted_response(existing.id, storage_path)


async def _build_match_accepted_response(
    job_id: uuid.UUID, variant_count: int
) -> JobAcceptedResponse:
    """Hardcodes a PENDING plan for each requested variant, same shape
    `_build_background_accepted_response` uses for its single-element
    `angles` list — no re-query of the sub_jobs table needed since the
    caller always just created (or is replaying) a job whose sub-jobs are
    all still PENDING at accept time.
    """
    return JobAcceptedResponse(
        job_id=str(job_id),
        status="PENDING",
        variants=[
            ResolvedVariantPlan(variant_index=i, status=SubJobStatus.PENDING)
            for i in range(variant_count)
        ],
        poll_after_ms=POLL_AFTER_MS,
    )


async def create_match_job_for_request(
    session: AsyncSession,
    client: ApiClient,
    config_version: ConfigVersion,
    storage_path: str,
    target_category: str,
    variant_count: int,
    sku_reference: str | None,
    metadata: dict[str, Any],
    idempotency_key: str,
    payload_hash: str,
) -> JobAcceptedResponse:
    """Implements POST /match — see phases/phase-18-match.md Step 3. Mirrors
    create_background_job_for_request's shape: idempotency -> rate
    limit/quota -> validate -> create -> commit. Unlike background
    operations (always exactly one angle-less sub-job), MATCH fans out to
    `variant_count` (1-4) angle-less sub-jobs, each with a distinct
    `variant_index`, all sharing one uploaded reference-photo asset — the
    same "record the same source per output" pattern angle generation uses,
    applied to variants instead of angles.

    Dispatches `orchestration.fan_out_match_job` after commit, for a
    genuinely new job only (Phase 18 Step 4) — see this function's own call
    site below.
    """
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise IdempotencyKeyConflictError(
                "This Idempotency-Key was already used with a different request body."
            )
        return await _build_match_accepted_response(existing.id, variant_count)

    await _enforce_rate_limit_and_quota(session, client)

    validate_operation_enabled(config_version, Operation.MATCH)

    # target_category is validated against payload.categories, the same
    # single source of truth /generate's category_code uses — not a
    # MATCH-specific list. operations.MATCH itself has no category list.
    category = find_category(config_version, target_category)
    if category is None:
        raise CategoryNotFoundError(
            f"Category {target_category} not found.",
            details={"category_code": target_category},
        )
    if not category["is_active"]:
        raise CategoryInactiveError(
            f"Category {target_category} is not active.",
            details={"category_code": target_category},
        )

    if not storage_service.exists(settings.BUCKET_INPUTS, storage_path):
        raise AssetNotFoundError(
            f"No uploaded asset found at {storage_path}.",
            details={"storage_path": storage_path},
        )
    if not storage_path.startswith(f"pending/{client.id}/"):
        raise AssetNotOwnedError(
            f"storage_path {storage_path} does not belong to this client.",
            details={"storage_path": storage_path},
        )
    meta = image_validation.inspect_and_validate(settings.BUCKET_INPUTS, storage_path)

    job = jobs_repo.create_job(
        session,
        client_id=client.id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        category_code=target_category,
        config_version_id=config_version.id,
        requested_angles=variant_count,
        sku_reference=sku_reference,
        metadata=metadata,
        operation=Operation.MATCH,
    )

    try:
        await session.flush()  # assigns job.id
    except IntegrityError:
        await session.rollback()
        return await _handle_match_replay_race(
            session, client, idempotency_key, payload_hash, variant_count
        )

    # One uploaded reference-photo asset, linked once per variant sub-job —
    # same pattern angle generation uses to record the same source per
    # angle, applied here to variants instead.
    asset = assets_repo.create_asset(
        session,
        job_id=job.id,
        kind=AssetKind.INPUT,
        bucket=settings.BUCKET_INPUTS,
        storage_path=storage_path,
        mime_type=meta.mime_type,
        width_px=meta.width_px,
        height_px=meta.height_px,
        bytes_=meta.bytes,
        checksum_sha256=meta.checksum_sha256,
        expires_at=retention_policy.compute_expires_at(AssetKind.INPUT),
    )
    await session.flush()  # assigns asset.id

    for variant_index in range(variant_count):
        validate_operation_angle_consistency(Operation.MATCH, None)
        jobs_repo.create_sub_job(
            session,
            job_id=job.id,
            angle=None,
            status=SubJobStatus.PENDING,
            source_type=SourceType.UPLOADED,
            input_asset_id=asset.id,
            variant_index=variant_index,
        )
    await session.flush()  # assigns sub_job ids

    job_events_repo.record_event(
        session,
        job.id,
        "JOB_CREATED",
        to_status=job.status.value,
        detail={
            "operation": Operation.MATCH.value,
            "target_category": target_category,
            "variant_count": variant_count,
        },
    )

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await _handle_match_replay_race(
            session, client, idempotency_key, payload_hash, variant_count
        )

    # Phase 18 Step 4: dispatch real work only for a genuinely new job — an
    # idempotency replay (both branches above) must never re-dispatch, same
    # rule create_job_for_request/create_background_job_for_request follow
    # for their own fan-out dispatch.
    from app.workers.orchestration import fan_out_match_job

    fan_out_match_job.delay(str(job.id))

    return await _build_match_accepted_response(job.id, variant_count)


async def _handle_match_replay_race(
    session: AsyncSession,
    client: ApiClient,
    idempotency_key: str,
    payload_hash: str,
    variant_count: int,
) -> JobAcceptedResponse:
    """A concurrent request created the MATCH job for this key between our
    idempotency check and our insert. Treat it exactly like a replay — same
    shape as _handle_background_replay_race.
    """
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is None:
        raise AppError(
            "Idempotency key conflict could not be resolved.", code=ErrorCode.INTERNAL_ERROR
        )
    if existing.payload_hash != payload_hash:
        raise IdempotencyKeyConflictError(
            "This Idempotency-Key was already used with a different request body."
        )
    return await _build_match_accepted_response(existing.id, variant_count)


async def _build_recolor_accepted_response(
    job_id: uuid.UUID, storage_path: str
) -> JobAcceptedResponse:
    """Same shape _build_background_accepted_response uses — a RECOLOR job
    has exactly one sub-job, same as a background job, not MATCH's 1-4. See
    phases/phase-19-recolor.md Step 3.
    """
    return JobAcceptedResponse(
        job_id=str(job_id),
        status="PENDING",
        angles=[
            ResolvedAnglePlan(
                angle=None,
                source_type=SourceType.UPLOADED,
                status=SubJobStatus.PENDING,
                storage_path=storage_path,
            )
        ],
        poll_after_ms=POLL_AFTER_MS,
    )


async def create_recolor_job_for_request(
    session: AsyncSession,
    client: ApiClient,
    config_version: ConfigVersion,
    storage_path: str,
    mask_storage_path: str,
    palette_code: str,
    sku_reference: str | None,
    metadata: dict[str, Any],
    idempotency_key: str,
    payload_hash: str,
) -> JobAcceptedResponse:
    """Implements POST /recolor — see phases/phase-19-recolor.md Step 3.
    Mirrors create_background_job_for_request's shape (idempotency -> rate
    limit/quota -> validate -> create -> commit -> dispatch), since a
    RECOLOR job has exactly one sub-job, same as a background job, not
    MATCH's 1-4 fan-out. The one genuinely new piece: a second uploaded
    file (the mask) is validated against the mask contract
    (app/services/mask_validation.py), dimension-checked against the
    source's own width/height, and stored as a MASK-kind asset linked via
    sub_jobs.mask_asset_id.

    Dispatches app.workers.recolor.process_task directly after commit, for
    a genuinely new job only — same "never re-dispatch on a replay" rule
    every other job-creating function here follows.
    """
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise IdempotencyKeyConflictError(
                "This Idempotency-Key was already used with a different request body."
            )
        return await _build_recolor_accepted_response(existing.id, storage_path)

    await _enforce_rate_limit_and_quota(session, client)

    validate_operation_enabled(config_version, Operation.RECOLOR)
    validate_palette(config_version, palette_code)

    if not storage_service.exists(settings.BUCKET_INPUTS, storage_path):
        raise AssetNotFoundError(
            f"No uploaded asset found at {storage_path}.",
            details={"storage_path": storage_path},
        )
    if not storage_path.startswith(f"pending/{client.id}/"):
        raise AssetNotOwnedError(
            f"storage_path {storage_path} does not belong to this client.",
            details={"storage_path": storage_path},
        )
    meta = image_validation.inspect_and_validate(settings.BUCKET_INPUTS, storage_path)

    if not storage_service.exists(settings.BUCKET_INPUTS, mask_storage_path):
        raise AssetNotFoundError(
            f"No uploaded asset found at {mask_storage_path}.",
            details={"storage_path": mask_storage_path},
        )
    if not mask_storage_path.startswith(f"pending/{client.id}/"):
        raise AssetNotOwnedError(
            f"storage_path {mask_storage_path} does not belong to this client.",
            details={"storage_path": mask_storage_path},
        )
    mask_meta = mask_validation.validate_mask(
        settings.BUCKET_INPUTS, mask_storage_path, meta.width_px, meta.height_px
    )

    job = jobs_repo.create_job(
        session,
        client_id=client.id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        category_code=None,
        config_version_id=config_version.id,
        requested_angles=1,
        sku_reference=sku_reference,
        metadata=metadata,
        operation=Operation.RECOLOR,
    )

    try:
        await session.flush()  # assigns job.id
    except IntegrityError:
        await session.rollback()
        return await _handle_recolor_replay_race(
            session, client, idempotency_key, payload_hash, storage_path
        )

    asset = assets_repo.create_asset(
        session,
        job_id=job.id,
        kind=AssetKind.INPUT,
        bucket=settings.BUCKET_INPUTS,
        storage_path=storage_path,
        mime_type=meta.mime_type,
        width_px=meta.width_px,
        height_px=meta.height_px,
        bytes_=meta.bytes,
        checksum_sha256=meta.checksum_sha256,
        expires_at=retention_policy.compute_expires_at(AssetKind.INPUT),
    )
    await session.flush()  # assigns asset.id

    mask_asset = assets_repo.create_asset(
        session,
        job_id=job.id,
        kind=AssetKind.MASK,
        bucket=settings.BUCKET_INPUTS,
        storage_path=mask_storage_path,
        mime_type="image/png",
        width_px=mask_meta.width_px,
        height_px=mask_meta.height_px,
        bytes_=mask_meta.bytes,
        checksum_sha256=mask_meta.checksum_sha256,
        expires_at=retention_policy.compute_expires_at(AssetKind.MASK),
    )
    await session.flush()  # assigns mask_asset.id

    validate_operation_angle_consistency(Operation.RECOLOR, None)
    sub_job = jobs_repo.create_sub_job(
        session,
        job_id=job.id,
        angle=None,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        input_asset_id=asset.id,
        mask_asset_id=mask_asset.id,
        palette_code=palette_code,
    )
    await session.flush()  # assigns sub_job.id

    job_events_repo.record_event(
        session,
        job.id,
        "JOB_CREATED",
        to_status=job.status.value,
        detail={"operation": Operation.RECOLOR.value, "palette_code": palette_code},
    )

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await _handle_recolor_replay_race(
            session, client, idempotency_key, payload_hash, storage_path
        )

    # dispatch real work only for a genuinely new job — an idempotency
    # replay (both branches above) must never re-dispatch, same rule every
    # other job-creating function here follows.
    from app.workers.recolor import process_task

    process_task.delay(str(sub_job.id))

    return await _build_recolor_accepted_response(job.id, storage_path)


async def _handle_recolor_replay_race(
    session: AsyncSession,
    client: ApiClient,
    idempotency_key: str,
    payload_hash: str,
    storage_path: str,
) -> JobAcceptedResponse:
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is None:
        raise AppError(
            "Idempotency key conflict could not be resolved.", code=ErrorCode.INTERNAL_ERROR
        )
    if existing.payload_hash != payload_hash:
        raise IdempotencyKeyConflictError(
            "This Idempotency-Key was already used with a different request body."
        )
    return await _build_recolor_accepted_response(existing.id, storage_path)


async def _build_mix_accepted_response(
    job_id: uuid.UUID, primary_storage_path: str
) -> JobAcceptedResponse:
    """Same shape _build_recolor_accepted_response/_build_background_accepted_response
    use — a MIX job has exactly one sub-job, same as RECOLOR and background
    operations, not MATCH's 1-4. See phases/phase-20-mix.md Step 3."""
    return JobAcceptedResponse(
        job_id=str(job_id),
        status="PENDING",
        angles=[
            ResolvedAnglePlan(
                angle=None,
                source_type=SourceType.UPLOADED,
                status=SubJobStatus.PENDING,
                storage_path=primary_storage_path,
            )
        ],
        poll_after_ms=POLL_AFTER_MS,
    )


async def _validate_and_ingest_mix_asset(client_id: uuid.UUID, storage_path: str) -> ImageMetadata:
    """Shared existence/ownership/structural-validity check for MIX's two
    independent source photos — same three checks _validate_request runs
    per angle, applied here to a standalone photo the way
    create_recolor_job_for_request already applies it to RECOLOR's source.
    """
    if not storage_service.exists(settings.BUCKET_INPUTS, storage_path):
        raise AssetNotFoundError(
            f"No uploaded asset found at {storage_path}.",
            details={"storage_path": storage_path},
        )
    if not storage_path.startswith(f"pending/{client_id}/"):
        raise AssetNotOwnedError(
            f"storage_path {storage_path} does not belong to this client.",
            details={"storage_path": storage_path},
        )
    return image_validation.inspect_and_validate(settings.BUCKET_INPUTS, storage_path)


async def _validate_and_ingest_mix_mask(
    client_id: uuid.UUID, mask_storage_path: str, source_meta: ImageMetadata
) -> mask_validation.MaskMetadata:
    """Shared existence/ownership/mask-contract check for MIX's two
    independent masks — mask_validation.validate_mask is called once per
    pair, completely independently, same as RECOLOR's own single call; it
    has no knowledge of MIX or of a second image, and needs no change to
    serve this. See phases/phase-20-mix.md Step 3."""
    if not storage_service.exists(settings.BUCKET_INPUTS, mask_storage_path):
        raise AssetNotFoundError(
            f"No uploaded asset found at {mask_storage_path}.",
            details={"storage_path": mask_storage_path},
        )
    if not mask_storage_path.startswith(f"pending/{client_id}/"):
        raise AssetNotOwnedError(
            f"storage_path {mask_storage_path} does not belong to this client.",
            details={"storage_path": mask_storage_path},
        )
    return mask_validation.validate_mask(
        settings.BUCKET_INPUTS, mask_storage_path, source_meta.width_px, source_meta.height_px
    )


async def create_mix_job_for_request(
    session: AsyncSession,
    client: ApiClient,
    config_version: ConfigVersion,
    primary_storage_path: str,
    primary_mask_storage_path: str,
    secondary_storage_path: str,
    secondary_mask_storage_path: str,
    sku_reference: str | None,
    metadata: dict[str, Any],
    idempotency_key: str,
    payload_hash: str,
) -> JobAcceptedResponse:
    """Implements POST /mix — see phases/phase-20-mix.md Step 3. Mirrors
    create_recolor_job_for_request's shape (idempotency -> rate limit/quota
    -> validate -> create -> commit -> dispatch), since a MIX job has
    exactly one sub-job, same as RECOLOR and background jobs, not MATCH's
    1-4 fan-out. The one genuinely new piece: **two** independent uploaded
    photo/mask pairs are validated, each via the same
    image_validation.inspect_and_validate / mask_validation.validate_mask
    calls RECOLOR already uses for its one pair, called here twice,
    completely independently — no cross-mask validation at ingest time (see
    this phase file's own Step 3 note on why).

    Dispatches app.workers.mix.process_task directly after commit, for a
    genuinely new job only — same "never re-dispatch on a replay" rule
    every other job-creating function here follows.
    """
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise IdempotencyKeyConflictError(
                "This Idempotency-Key was already used with a different request body."
            )
        return await _build_mix_accepted_response(existing.id, primary_storage_path)

    await _enforce_rate_limit_and_quota(session, client)

    validate_operation_enabled(config_version, Operation.MIX)

    primary_meta = await _validate_and_ingest_mix_asset(client.id, primary_storage_path)
    primary_mask_meta = await _validate_and_ingest_mix_mask(
        client.id, primary_mask_storage_path, primary_meta
    )
    secondary_meta = await _validate_and_ingest_mix_asset(client.id, secondary_storage_path)
    secondary_mask_meta = await _validate_and_ingest_mix_mask(
        client.id, secondary_mask_storage_path, secondary_meta
    )

    job = jobs_repo.create_job(
        session,
        client_id=client.id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        category_code=None,
        config_version_id=config_version.id,
        requested_angles=1,
        sku_reference=sku_reference,
        metadata=metadata,
        operation=Operation.MIX,
    )

    try:
        await session.flush()  # assigns job.id
    except IntegrityError:
        await session.rollback()
        return await _handle_mix_replay_race(
            session, client, idempotency_key, payload_hash, primary_storage_path
        )

    primary_asset = assets_repo.create_asset(
        session,
        job_id=job.id,
        kind=AssetKind.INPUT,
        bucket=settings.BUCKET_INPUTS,
        storage_path=primary_storage_path,
        mime_type=primary_meta.mime_type,
        width_px=primary_meta.width_px,
        height_px=primary_meta.height_px,
        bytes_=primary_meta.bytes,
        checksum_sha256=primary_meta.checksum_sha256,
        expires_at=retention_policy.compute_expires_at(AssetKind.INPUT),
    )
    await session.flush()  # assigns asset.id

    primary_mask_asset = assets_repo.create_asset(
        session,
        job_id=job.id,
        kind=AssetKind.MASK,
        bucket=settings.BUCKET_INPUTS,
        storage_path=primary_mask_storage_path,
        mime_type="image/png",
        width_px=primary_mask_meta.width_px,
        height_px=primary_mask_meta.height_px,
        bytes_=primary_mask_meta.bytes,
        checksum_sha256=primary_mask_meta.checksum_sha256,
        expires_at=retention_policy.compute_expires_at(AssetKind.MASK),
    )
    await session.flush()

    secondary_asset = assets_repo.create_asset(
        session,
        job_id=job.id,
        kind=AssetKind.INPUT,
        bucket=settings.BUCKET_INPUTS,
        storage_path=secondary_storage_path,
        mime_type=secondary_meta.mime_type,
        width_px=secondary_meta.width_px,
        height_px=secondary_meta.height_px,
        bytes_=secondary_meta.bytes,
        checksum_sha256=secondary_meta.checksum_sha256,
        expires_at=retention_policy.compute_expires_at(AssetKind.INPUT),
    )
    await session.flush()

    secondary_mask_asset = assets_repo.create_asset(
        session,
        job_id=job.id,
        kind=AssetKind.MASK,
        bucket=settings.BUCKET_INPUTS,
        storage_path=secondary_mask_storage_path,
        mime_type="image/png",
        width_px=secondary_mask_meta.width_px,
        height_px=secondary_mask_meta.height_px,
        bytes_=secondary_mask_meta.bytes,
        checksum_sha256=secondary_mask_meta.checksum_sha256,
        expires_at=retention_policy.compute_expires_at(AssetKind.MASK),
    )
    await session.flush()

    validate_operation_angle_consistency(Operation.MIX, None)
    sub_job = jobs_repo.create_sub_job(
        session,
        job_id=job.id,
        angle=None,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        input_asset_id=primary_asset.id,
        mask_asset_id=primary_mask_asset.id,
        secondary_input_asset_id=secondary_asset.id,
        secondary_mask_asset_id=secondary_mask_asset.id,
    )
    await session.flush()  # assigns sub_job.id

    job_events_repo.record_event(
        session,
        job.id,
        "JOB_CREATED",
        to_status=job.status.value,
        detail={"operation": Operation.MIX.value},
    )

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await _handle_mix_replay_race(
            session, client, idempotency_key, payload_hash, primary_storage_path
        )

    from app.workers.mix import process_task

    process_task.delay(str(sub_job.id))

    return await _build_mix_accepted_response(job.id, primary_storage_path)


async def _handle_mix_replay_race(
    session: AsyncSession,
    client: ApiClient,
    idempotency_key: str,
    payload_hash: str,
    primary_storage_path: str,
) -> JobAcceptedResponse:
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is None:
        raise AppError(
            "Idempotency key conflict could not be resolved.", code=ErrorCode.INTERNAL_ERROR
        )
    if existing.payload_hash != payload_hash:
        raise IdempotencyKeyConflictError(
            "This Idempotency-Key was already used with a different request body."
        )
    return await _build_mix_accepted_response(existing.id, primary_storage_path)
