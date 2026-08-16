"""GET /api/v2/status/{job_id}. See docs/api-routes.md and docs/business-rules.md §3."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.status import JobStatusResponse
from app.core.auth import require_client_scope
from app.core.errors import NotFoundError
from app.db.models.api_clients import ApiClient
from app.db.models.enums import Operation
from app.db.repositories import assets as assets_repo
from app.db.repositories import jobs as jobs_repo
from app.db.session import get_db
from app.services import status_service

router = APIRouter(tags=["status"])


@router.get(
    "/status/{job_id}",
    response_model=JobStatusResponse,
    responses={
        401: {"description": "Invalid API key"},
        404: {"description": "Not found, or not owned by this client"},
    },
)
async def get_status(
    job_id: str,
    response: Response,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobStatusResponse:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise NotFoundError("Job not found.", details={"job_id": job_id}) from exc

    job = await jobs_repo.get_by_id_for_client(session, job_uuid, client.id)
    if job is None:
        raise NotFoundError("Job not found.", details={"job_id": job_id})

    sub_jobs = await jobs_repo.get_sub_jobs(session, job.id)

    if status_service.is_non_terminal(job):
        response.headers["Retry-After"] = "3"

    bucket_and_paths: dict[uuid.UUID, tuple[str, str] | None] = {}
    for sub_job in sub_jobs:
        bucket_and_path = None
        if sub_job.output_asset_id is not None:
            asset = await assets_repo.get_by_id(session, sub_job.output_asset_id)
            if asset is not None:
                bucket_and_path = (asset.bucket, asset.storage_path)
        bucket_and_paths[sub_job.id] = bucket_and_path

    # Additive per phases/phase-15-background-operations.md Step 4 and
    # phases/phase-18-match.md Step 4: `angles` stays exactly as it was
    # before those phases for ANGLE_GENERATION jobs; a background job
    # populates `results` instead, a MATCH job populates `variants` —
    # exactly one of the three is ever non-empty for a given job.
    if job.operation == Operation.ANGLE_GENERATION:
        angle_statuses = [
            status_service.build_angle_status(job, sub_job, bucket_and_paths[sub_job.id])
            for sub_job in sub_jobs
        ]
        return status_service.build_job_status_response(job, angle_statuses)

    if job.operation == Operation.MATCH:
        # get_sub_jobs orders by `angle`, which is NULL for every MATCH
        # sub-job — no natural ordering from that column, so sort by
        # variant_index here for a stable, readable response (mirrors how
        # angle jobs get their order for free from the angle_t enum).
        match_sub_jobs = sorted(sub_jobs, key=lambda sj: sj.variant_index or 0)
        variant_statuses = [
            status_service.build_variant_status(job, sub_job, bucket_and_paths[sub_job.id])
            for sub_job in match_sub_jobs
        ]
        return status_service.build_job_status_response(job, [], [], variant_statuses)

    # BACKGROUND_REMOVAL, BACKGROUND_REPLACEMENT, RECOLOR, and MIX all fall
    # through to here — each has exactly one angle-less, variant-less
    # sub-job, so `build_background_result_status` (which only asserts
    # `angle is None`) applies unmodified to RECOLOR and MIX too. See
    # phases/phase-19-recolor.md Step 4 and phases/phase-20-mix.md Step 4.
    result_statuses = [
        status_service.build_background_result_status(job, sub_job, bucket_and_paths[sub_job.id])
        for sub_job in sub_jobs
    ]
    return status_service.build_job_status_response(job, [], result_statuses)
