"""MOCK_MODE-only helpers for /generate and /retry. See phases/phase-1-api-contract.md
Step 3. Real job creation and retry execution land in Phases 2 and 8 — these functions
stand in for that logic against real seeded/existing rows, never inventing data.
"""

from datetime import UTC, datetime

from app.core.errors import AppError, ErrorCode
from app.db.models.assets import Asset
from app.db.models.enums import SourceType, SubJobStatus
from app.db.models.jobs import SubJob

MAX_RETRY_ATTEMPTS = 3


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
