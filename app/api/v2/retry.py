"""POST /api/v2/jobs/{job_id}/angles/{angle}/retry, POST /api/v2/jobs/{job_id}/retry.

See docs/api-routes.md and docs/business-rules.md §5. Real as of Phase 8 —
see phases/phase-8-failure-retry.md. check_retry_preconditions
(app/services/job_service.py) still owns every 409 precondition; this route
now actually mutates state and dispatches a fresh
generation.transform_photo, reusing Phase 7's dispatch primitive.

The job-level route (Phase 15 Step 4) covers every job-level-retryable
operation — background operations (always exactly one sub-job) and, as of
Phase 18 Step 4, MATCH (1-4 sub-jobs, retries only the FAILED ones,
all-or-nothing precondition check before any execution). retry.py stays
angle-specific for ANGLE_GENERATION jobs, which must keep naming their angle
(docs/api-routes.md).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import idempotency
from app.core.auth import require_client_scope
from app.core.errors import NotFoundError
from app.core.idempotency import IdempotencyKeyConflictError, require_idempotency_key
from app.db.models.api_clients import ApiClient
from app.db.models.enums import Angle, Operation, QAStatus, SubJobStatus
from app.db.repositories import assets as assets_repo
from app.db.repositories import jobs as jobs_repo
from app.db.session import get_db
from app.services.job_service import (
    AngleJobRetryNotAllowedError,
    check_qa_retry_preconditions,
    check_retry_preconditions,
)
from app.services.retry_service import execute_qa_retry, execute_retry
from app.workers.generation import transform_photo_task

router = APIRouter(tags=["retry"])


@router.post(
    "/jobs/{job_id}/angles/{angle}/retry",
    status_code=202,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        404: {"description": "Not found, or not owned by this client"},
        409: {"description": "Not FAILED, retry ceiling reached, or input expired"},
    },
)
async def retry_angle(
    job_id: str,
    angle: Angle,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise NotFoundError("Job not found.", details={"job_id": job_id}) from exc

    job = await jobs_repo.get_by_id_for_client(session, job_uuid, client.id)
    if job is None:
        raise NotFoundError("Job not found.", details={"job_id": job_id})

    sub_job = await jobs_repo.get_sub_job(session, job.id, angle)
    if sub_job is None:
        raise NotFoundError(
            f"Angle {angle.value} was not requested on this job.",
            details={"job_id": job_id, "angle": angle.value},
        )

    # docs/business-rules.md §8: Idempotency-Key required on /retry too.
    # Checked before preconditions so a replay of an already-accepted retry
    # is always a no-op 202, even if the sub-job has since moved past FAILED
    # (e.g. it already finished running by the time the client retries the
    # request). See phases/phase-8-failure-retry.md Step 2.
    target = f"{sub_job.id}"
    existing_target = await idempotency.get_retry_target(str(client.id), idempotency_key)
    if existing_target is not None:
        if existing_target != target:
            raise IdempotencyKeyConflictError(
                "This Idempotency-Key was already used for a different angle/job."
            )
        return

    input_asset = (
        await assets_repo.get_by_id(session, sub_job.input_asset_id)
        if sub_job.input_asset_id is not None
        else None
    )
    check_retry_preconditions(sub_job, input_asset)

    execute_retry(session, job, sub_job)
    await idempotency.store_retry_target(str(client.id), idempotency_key, target)
    await session.commit()

    transform_photo_task.delay(str(sub_job.id))


@router.post(
    "/jobs/{job_id}/retry",
    status_code=202,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        404: {"description": "Not found, or not owned by this client"},
        409: {
            "description": (
                "Angle job (use /angles/{angle}/retry instead), not FAILED, "
                "retry ceiling reached, or input expired"
            )
        },
    },
)
async def retry_job(
    job_id: str,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise NotFoundError("Job not found.", details={"job_id": job_id}) from exc

    job = await jobs_repo.get_by_id_for_client(session, job_uuid, client.id)
    if job is None:
        raise NotFoundError("Job not found.", details={"job_id": job_id})

    if job.operation == Operation.ANGLE_GENERATION:
        raise AngleJobRetryNotAllowedError(
            "Angle jobs must retry a specific angle via POST /jobs/{job_id}/angles/{angle}/retry.",
            details={"job_id": job_id},
        )

    sub_jobs = await jobs_repo.get_sub_jobs(session, job.id)
    if not sub_jobs:
        raise NotFoundError("No sub-job found on this job.", details={"job_id": job_id})

    # Keyed off the job, not a specific sub-job ID — see this route's own
    # 2026-08-16 note below on why. Same "check before preconditions" order
    # as the angle route above: a replay of an already-accepted retry is
    # always a no-op 202, even if every sub-job has since moved past FAILED.
    target = f"{job.id}:retry"
    existing_target = await idempotency.get_retry_target(str(client.id), idempotency_key)
    if existing_target is not None:
        if existing_target != target:
            raise IdempotencyKeyConflictError(
                "This Idempotency-Key was already used for a different job."
            )
        return

    # 2026-08-16 (Phase 18 Step 4): this route used to always retry
    # `sub_jobs[0]` — correct only because every operation with a job-level
    # retry route (BACKGROUND_REMOVAL/BACKGROUND_REPLACEMENT) has exactly
    # one sub-job. MATCH has 1-4 (sub_jobs.variant_index), so this route now
    # retries every FAILED sub-job on the job, all-or-nothing: every failed
    # sub-job's own preconditions (ceiling, expired input) are validated
    # before any of them is executed, rather than silently retrying some and
    # skipping others — this project's "fail loud, don't silently degrade"
    # posture (see e.g. background_service.py's own docstring for the same
    # principle applied elsewhere). The idempotency target above is a fixed
    # function of job.id alone (not "whichever sub-jobs are FAILED right
    # now") precisely so a replay can't recompute a different target than
    # what was stored the first time and incorrectly 409 instead of no-op.

    # QA_REVIEW+FLAGGED is structurally impossible for a MATCH job (MATCH
    # never enters QA_REVIEW — see phases/phase-18-match.md Step 4), so this
    # branch still only ever matches a single sub-job in practice
    # (background operations). Unaffected by the multi-sub-job change below.
    qa_flagged = [
        sj
        for sj in sub_jobs
        if sj.status == SubJobStatus.QA_REVIEW and sj.qa_status == QAStatus.FLAGGED
    ]
    if qa_flagged:
        sub_job = qa_flagged[0]
        check_qa_retry_preconditions(sub_job)

        execute_qa_retry(session, job, sub_job)
        await idempotency.store_retry_target(str(client.id), idempotency_key, target)
        await session.commit()

        from app.workers.qa import score_background

        score_background.delay(str(sub_job.id))
        return

    failed = [sj for sj in sub_jobs if sj.status == SubJobStatus.FAILED]
    if not failed:
        # Preserves the exact existing 409 (code/message/details) for the
        # single-sub-job background case; for a multi-sub-job MATCH job with
        # nothing FAILED, this raises against the first sub-job — still a
        # correct, clear 409 (check_retry_preconditions always raises here,
        # since every sub_job in `sub_jobs` is, by construction, not in
        # `failed`).
        input_asset = (
            await assets_repo.get_by_id(session, sub_jobs[0].input_asset_id)
            if sub_jobs[0].input_asset_id is not None
            else None
        )
        check_retry_preconditions(sub_jobs[0], input_asset)
        raise AssertionError("unreachable: check_retry_preconditions always raises here")

    # Validate every failed sub-job's own retry preconditions before
    # executing any of them (see the 2026-08-16 note above).
    for sub_job in failed:
        input_asset = (
            await assets_repo.get_by_id(session, sub_job.input_asset_id)
            if sub_job.input_asset_id is not None
            else None
        )
        check_retry_preconditions(sub_job, input_asset)

    for sub_job in failed:
        execute_retry(session, job, sub_job)
    await idempotency.store_retry_target(str(client.id), idempotency_key, target)
    await session.commit()

    from app.workers.background import process_task as background_process_task
    from app.workers.match import process_task as match_process_task

    dispatch_task = (
        match_process_task if job.operation == Operation.MATCH else background_process_task
    )
    for sub_job in failed:
        dispatch_task.delay(str(sub_job.id))
