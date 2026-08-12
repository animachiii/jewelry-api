"""Output QA gate — real automatic scoring plus the human review/decision
path. See docs/business-rules.md §7, docs/ai-integration.md Call Site 2,
and phases/phase-9-qa-gate.md.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode, ProviderError
from app.db.models.enums import FailureClass, QAStatus, SubJobStatus
from app.db.models.jobs import Job, SubJob
from app.db.repositories import assets as assets_repo
from app.db.repositories import config_versions as config_versions_repo
from app.db.repositories import job_events as job_events_repo
from app.db.repositories import jobs as jobs_repo
from app.providers.gemini_qa import GeminiQaProvider
from app.services import storage_service
from app.services.generation_service import fetch_reference_images, recompute_parent_status
from app.services.job_service import find_category


class SubJobNotFoundError(AppError):
    code = ErrorCode.SUB_JOB_NOT_FOUND
    http_status = 404


class QaNotPendingError(AppError):
    code = ErrorCode.QA_NOT_PENDING
    http_status = 409


class SubJobNotFoundInternalError(Exception):
    """Raised for a worker-context lookup failure — mirrors
    generation_service.SubJobNotFoundError. Not an AppError: a Celery task
    has no HTTP response to shape.
    """


async def score_synthetic_angle(session: AsyncSession, sub_job_id: uuid.UUID) -> SubJob:
    """Scores one QA_REVIEW sub-job and either completes it (score >=
    threshold) or leaves it flagged for human review (score < threshold, or
    the QA provider call itself failed — see phases/phase-9-qa-gate.md's
    reality-check section: fail open to a human, never to an unscored pass).
    """
    sub_job = await jobs_repo.get_sub_job_by_id(session, sub_job_id)
    if sub_job is None:
        raise SubJobNotFoundInternalError(f"SubJob {sub_job_id} not found.")

    job = await jobs_repo.get_by_id(session, sub_job.job_id)
    if job is None:
        raise SubJobNotFoundInternalError(
            f"Job {sub_job.job_id} for sub-job {sub_job_id} not found."
        )

    config_version = await config_versions_repo.get_by_id(session, job.config_version_id)
    if config_version is None:
        raise SubJobNotFoundInternalError(
            f"Pinned config version {job.config_version_id} not found."
        )

    # SYNTHETIC-angle-only: the category reference matrix this function
    # scores against doesn't apply to Phase 15 background operations, which
    # get their own scoring path with the input image as reference instead
    # — see phases/phase-15-background-operations.md Step 5.
    assert job.category_code is not None
    assert sub_job.angle is not None

    category = find_category(config_version, job.category_code)
    if category is None:
        raise SubJobNotFoundInternalError(
            f"Category {job.category_code} not found in pinned config."
        )
    angle_config = category["angles"][sub_job.angle.value]
    threshold = config_version.payload["global"]["qa_similarity_threshold"]
    model_version = config_version.payload["global"]["model_version"]

    output_asset = (
        await assets_repo.get_by_id(session, sub_job.output_asset_id)
        if sub_job.output_asset_id is not None
        else None
    )
    if output_asset is None:
        raise SubJobNotFoundInternalError(f"No output asset for sub-job {sub_job_id}.")

    output_bytes = storage_service.download_to_temp(
        output_asset.bucket, output_asset.storage_path
    ).read_bytes()
    reference_images = fetch_reference_images(angle_config.get("reference_image_urls", []))

    return await _score_and_apply(
        session,
        job,
        sub_job,
        threshold=threshold,
        model_version=model_version,
        output_bytes=output_bytes,
        reference_images=reference_images,
    )


async def score_background_operation(session: AsyncSession, sub_job_id: uuid.UUID) -> SubJob:
    """Subject-preservation QA gate for Phase 15 background operations —
    reuses this module's scoring machinery with the **input image** as the
    reference instead of the category reference matrix: the subject is
    meant to be identical to the input, only the background changed, so
    "same object, new background" is what's being judged here, not "novel
    view of the same object" (score_synthetic_angle's job). Threshold is
    `global.background_qa_similarity_threshold` (migration 0010), not
    `qa_similarity_threshold` — calibrated and tunable independently. See
    phases/phase-15-background-operations.md Step 5.
    """
    sub_job = await jobs_repo.get_sub_job_by_id(session, sub_job_id)
    if sub_job is None:
        raise SubJobNotFoundInternalError(f"SubJob {sub_job_id} not found.")

    job = await jobs_repo.get_by_id(session, sub_job.job_id)
    if job is None:
        raise SubJobNotFoundInternalError(
            f"Job {sub_job.job_id} for sub-job {sub_job_id} not found."
        )

    config_version = await config_versions_repo.get_by_id(session, job.config_version_id)
    if config_version is None:
        raise SubJobNotFoundInternalError(
            f"Pinned config version {job.config_version_id} not found."
        )

    assert sub_job.angle is None  # background operations only

    threshold = config_version.payload["global"]["background_qa_similarity_threshold"]
    model_version = config_version.payload["global"]["model_version"]

    input_asset = (
        await assets_repo.get_by_id(session, sub_job.input_asset_id)
        if sub_job.input_asset_id is not None
        else None
    )
    if input_asset is None:
        raise SubJobNotFoundInternalError(f"No input asset for sub-job {sub_job_id}.")
    output_asset = (
        await assets_repo.get_by_id(session, sub_job.output_asset_id)
        if sub_job.output_asset_id is not None
        else None
    )
    if output_asset is None:
        raise SubJobNotFoundInternalError(f"No output asset for sub-job {sub_job_id}.")

    reference_images = [
        storage_service.download_to_temp(input_asset.bucket, input_asset.storage_path).read_bytes()
    ]
    output_bytes = storage_service.download_to_temp(
        output_asset.bucket, output_asset.storage_path
    ).read_bytes()

    return await _score_and_apply(
        session,
        job,
        sub_job,
        threshold=threshold,
        model_version=model_version,
        output_bytes=output_bytes,
        reference_images=reference_images,
    )


async def _score_and_apply(
    session: AsyncSession,
    job: Job,
    sub_job: SubJob,
    *,
    threshold: float,
    model_version: str,
    output_bytes: bytes,
    reference_images: list[bytes],
) -> SubJob:
    """Shared by score_synthetic_angle and score_background_operation — the
    provider call, pass/fail branching, and event recording are identical;
    only how threshold/model_version/reference_images get resolved differs.
    """
    provider = GeminiQaProvider(model_version=model_version)

    try:
        result = provider.score(output_bytes, reference_images)
    except ProviderError as exc:
        _flag_for_review(sub_job, score=None)
        _record_qa_scored_event(
            session, job, sub_job, score=None, threshold=threshold, outcome="provider_error"
        )
        _log_provider_error(exc)
        return sub_job

    if result.score >= threshold:
        sub_job.qa_score = result.score
        sub_job.qa_status = QAStatus.PASSED
        sub_job.status = SubJobStatus.COMPLETED
        _record_qa_scored_event(
            session, job, sub_job, score=result.score, threshold=threshold, outcome="passed"
        )
        await recompute_parent_status(session, job)
    else:
        _flag_for_review(sub_job, score=result.score)
        _record_qa_scored_event(
            session, job, sub_job, score=result.score, threshold=threshold, outcome="flagged"
        )

    return sub_job


def _flag_for_review(sub_job: SubJob, score: float | None) -> None:
    sub_job.qa_score = score
    sub_job.qa_status = QAStatus.FLAGGED
    # status stays QA_REVIEW — already there on entry to this function.


def _log_provider_error(exc: ProviderError) -> None:
    import structlog

    structlog.get_logger().warning(
        "qa_provider_error", failure_class=exc.failure_class, message=exc.message
    )


def _record_qa_scored_event(
    session: AsyncSession,
    job: Job,
    sub_job: SubJob,
    *,
    score: float | None,
    threshold: float,
    outcome: str,
) -> None:
    detail: dict[str, Any] = {
        "threshold": threshold,
        "outcome": outcome,
        "angle": sub_job.angle.value if sub_job.angle is not None else None,
    }
    if score is not None:
        detail["score"] = score
    job_events_repo.record_event(
        session,
        job.id,
        "QA_SCORED",
        sub_job_id=sub_job.id,
        to_status=sub_job.status.value,
        detail=detail,
    )


async def build_review_queue_items(session: AsyncSession) -> list[dict[str, Any]]:
    """Assembles the fields app/api/v2/schemas/qa.py::QaReviewItem needs —
    joins each flagged sub-job to its job and pinned config_version (never
    the currently-active one, same rule /retry follows for the same reason:
    visual consistency against what was actually generated). Kept out of the
    route per docs/conventions.md — routes validate/serialize, services own
    the joins.
    """
    sub_jobs = await jobs_repo.get_flagged_qa_review(session)
    items: list[dict[str, Any]] = []
    for sub_job in sub_jobs:
        job = await jobs_repo.get_by_id(session, sub_job.job_id)
        if job is None or sub_job.output_asset_id is None:
            continue
        output_asset = await assets_repo.get_by_id(session, sub_job.output_asset_id)
        if output_asset is None:
            continue
        config_version = await config_versions_repo.get_by_id(session, job.config_version_id)
        category = (
            find_category(config_version, job.category_code)
            if config_version and job.category_code is not None
            else None
        )
        angle_config = (
            category["angles"][sub_job.angle.value]
            if category and sub_job.angle is not None
            else {}
        )

        # A background item's "reference" is the input photo itself, not a
        # category matrix — without this the human review queue would show
        # a subject-preservation item with nothing to compare the output
        # against, defeating the point of a human review queue. See
        # phases/phase-15-background-operations.md Step 5.
        reference_image_urls = angle_config.get("reference_image_urls", [])
        if sub_job.angle is None and sub_job.input_asset_id is not None:
            input_asset = await assets_repo.get_by_id(session, sub_job.input_asset_id)
            if input_asset is not None:
                reference_image_urls = [
                    storage_service.generate_signed_url(
                        input_asset.bucket, input_asset.storage_path
                    )
                ]

        items.append(
            {
                "sub_job_id": str(sub_job.id),
                "job_id": str(job.id),
                "operation": job.operation,
                "angle": sub_job.angle,
                "category_code": job.category_code,
                "qa_score": float(sub_job.qa_score) if sub_job.qa_score is not None else None,
                "image_url": storage_service.generate_signed_url(
                    output_asset.bucket, output_asset.storage_path
                ),
                "reference_image_urls": reference_image_urls,
                "started_at": sub_job.started_at,
            }
        )
    return items


async def submit_qa_decision(session: AsyncSession, sub_job_id: uuid.UUID, decision: str) -> SubJob:
    sub_job = await jobs_repo.get_sub_job_by_id(session, sub_job_id)
    if sub_job is None:
        raise SubJobNotFoundError(
            f"Sub-job {sub_job_id} not found.", details={"sub_job_id": str(sub_job_id)}
        )
    if sub_job.status != SubJobStatus.QA_REVIEW:
        raise QaNotPendingError(
            f"Sub-job {sub_job_id} is {sub_job.status.value}, not QA_REVIEW.",
            details={"sub_job_id": str(sub_job_id), "status": sub_job.status.value},
        )

    job = await jobs_repo.get_by_id(session, sub_job.job_id)
    if job is None:
        raise SubJobNotFoundError(
            f"Job for sub-job {sub_job_id} not found.", details={"sub_job_id": str(sub_job_id)}
        )

    from_status = sub_job.status

    if decision == "approve":
        sub_job.qa_status = QAStatus.PASSED
        sub_job.status = SubJobStatus.COMPLETED
    else:
        sub_job.qa_status = QAStatus.FAILED
        sub_job.status = SubJobStatus.REJECTED
        sub_job.failure_class = FailureClass.QA_REJECTED
    # sub_job.completed_at is left NULL — generation_service's own terminal
    # writes (_complete_success/_fail) never set it either, a pre-existing
    # gap across the whole sub_job lifecycle, not something to fix
    # inconsistently from just this one code path.

    job_events_repo.record_event(
        session,
        job.id,
        "QA_DECISION",
        sub_job_id=sub_job.id,
        from_status=from_status.value,
        to_status=sub_job.status.value,
        detail={
            "decision": decision,
            "angle": sub_job.angle.value if sub_job.angle is not None else None,
        },
    )
    await recompute_parent_status(session, job)

    return sub_job
