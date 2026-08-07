"""Job creation (real, Phase 2) and retry precondition checks (still
MOCK_MODE — see phases/phase-1-api-contract.md Step 3; real retry execution
lands in Phase 8).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.generate import GenerateJobRequest, JobAcceptedResponse, ResolvedAnglePlan
from app.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import IdempotencyKeyConflictError
from app.db.models.api_clients import ApiClient
from app.db.models.assets import Asset
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import Angle, AssetKind, SourceType, SubJobStatus
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


def check_retry_preconditions(sub_job: SubJob, input_asset: Asset | None) -> None:
    """Raises the specific 409 for the first violated precondition.
    See docs/business-rules.md §5.
    """
    if sub_job.status != SubJobStatus.FAILED:
        raise SubJobNotRetryableError(
            f"Sub-job for angle {sub_job.angle.value} is {sub_job.status.value}, not FAILED.",
            details={"angle": sub_job.angle.value, "status": sub_job.status.value},
        )
    if sub_job.attempt_count >= MAX_RETRY_ATTEMPTS:
        raise RetryLimitExceededError(
            f"Angle {sub_job.angle.value} has reached the retry ceiling "
            f"({MAX_RETRY_ATTEMPTS} attempts).",
            details={"angle": sub_job.angle.value, "attempt_count": sub_job.attempt_count},
        )
    if sub_job.source_type == SourceType.UPLOADED:
        if input_asset is None:
            raise InputAssetExpiredError(
                f"No input asset found for angle {sub_job.angle.value}.",
                details={"angle": sub_job.angle.value},
            )
        if input_asset.expires_at is not None and input_asset.expires_at <= datetime.now(UTC):
            raise InputAssetExpiredError(
                f"Input asset for angle {sub_job.angle.value} has expired. "
                "Submit a new job — the image cannot be regenerated.",
                details={"angle": sub_job.angle.value},
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


def _find_category(config_version: ConfigVersion, category_code: str) -> dict[str, Any] | None:
    for cat in config_version.payload["categories"]:
        if cat["code"] == category_code:
            return dict(cat)
    return None


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

    category = _find_category(config_version, body.category_code)
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
