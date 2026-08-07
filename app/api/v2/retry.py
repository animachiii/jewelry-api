"""POST /api/v2/jobs/{job_id}/angles/{angle}/retry.

See docs/api-routes.md and docs/business-rules.md §5. MOCK_MODE checks
preconditions against real seeded rows and returns 202/409 accordingly, but
does not mutate state — real retry execution lands in Phase 8.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth import require_client_scope
from app.core.errors import NotFoundError
from app.core.idempotency import require_idempotency_key
from app.db.models.api_clients import ApiClient
from app.db.models.enums import Angle
from app.db.repositories import assets as assets_repo
from app.db.repositories import jobs as jobs_repo
from app.db.session import get_db
from app.services.job_service import check_retry_preconditions

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
    if not settings.MOCK_MODE:
        raise NotImplementedError("Real retry execution lands in Phase 8.")

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

    input_asset = (
        await assets_repo.get_by_id(session, sub_job.input_asset_id)
        if sub_job.input_asset_id is not None
        else None
    )
    check_retry_preconditions(sub_job, input_asset)
