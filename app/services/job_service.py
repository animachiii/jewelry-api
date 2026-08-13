"""Job creation (real, Phase 2) and retry precondition checks
(check_retry_preconditions — real execution wired up in
app/services/retry_service.py and app/api/v2/retry.py, Phase 8).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.generate import GenerateJobRequest, JobAcceptedResponse, ResolvedAnglePlan
from app.config import settings
from app.core import ratelimit
from app.core.errors import AppError, ErrorCode, QuotaExceededError, RateLimitError
from app.core.idempotency import IdempotencyKeyConflictError
from app.db.models.api_clients import ApiClient
from app.db.models.assets import Asset
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import Angle, AssetKind, Operation, SourceType, SubJobStatus
from app.db.models.jobs import SubJob
from app.db.repositories import assets as assets_repo
from app.db.repositories import job_events as job_events_repo
from app.db.repositories import jobs as jobs_repo
from app.services import image_validation, retention_policy, storage_service
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
) -> JobAcceptedResponse:
    """Implements POST /background/remove and POST /background/replace —
    see phases/phase-15-background-operations.md Steps 4-5. Mirrors
    create_job_for_request's shape: idempotency -> rate limit/quota ->
    validate -> create -> commit -> dispatch `background.process`
    (Step 5), same "only dispatch a genuinely new job, never a replay"
    rule create_job_for_request follows for fan_out_job.
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
        if preset_code is None:
            # `background_storage_path` is now accepted by the request schema
            # (BackgroundReplaceRequest's mutual-exclusivity validator,
            # docs/superpowers/specs/2026-08-13-custom-background-compositing-design.md)
            # but this function isn't wired to create/link a background asset
            # from it yet — that lands in a follow-up task. Fail clean with a
            # clear 422 rather than let the client's genuinely valid request
            # crash with an unhandled 500 in the meantime.
            raise OperationDisabledError(
                "Custom background compositing (background_storage_path) is not yet "
                "available; use preset_code.",
                details={"operation": operation.value},
            )
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
    sub_job = jobs_repo.create_sub_job(
        session,
        job_id=job.id,
        angle=None,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        input_asset_id=asset.id,
    )
    await session.flush()  # assigns sub_job.id

    job_events_repo.record_event(
        session,
        job.id,
        "JOB_CREATED",
        to_status=job.status.value,
        detail={"operation": operation.value, "preset_code": preset_code},
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
