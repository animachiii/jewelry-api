"""POST /api/v2/generate. See docs/api-routes.md and docs/business-rules.md §1, §8.

MOCK_MODE stands in for real job creation (Phase 2): instead of writing new
rows, it hands back an existing job belonging to this client — see
docs/decisions and phases/phase-1-api-contract.md Step 3. Idempotency replay
and the same-key-different-payload 409 are real, backed by Redis.
"""

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.generate import GenerateJobRequest, JobAcceptedResponse, ResolvedAnglePlan
from app.config import settings
from app.core.auth import require_client_scope
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import (
    IdempotencyKeyConflictError,
    get_replay,
    require_idempotency_key,
    store_replay,
)
from app.db.models.api_clients import ApiClient
from app.db.models.enums import SourceType, SubJobStatus
from app.db.repositories import jobs as jobs_repo
from app.db.session import get_db

router = APIRouter(tags=["generate"])

_POLL_AFTER_MS = 1500


def _payload_hash(body: GenerateJobRequest) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _resolve_plan(body: GenerateJobRequest) -> list[ResolvedAnglePlan]:
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


@router.post(
    "/generate",
    status_code=202,
    response_model=JobAcceptedResponse,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        409: {"description": "Idempotency key conflict"},
        422: {"description": "Bad category, disabled angle, or synthetic not allowed"},
        429: {"description": "Rate limit or quota exceeded"},
    },
)
async def create_job(
    body: GenerateJobRequest,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobAcceptedResponse:
    if not settings.MOCK_MODE:
        raise NotImplementedError("Real job creation lands in Phase 2.")

    payload_hash = _payload_hash(body)
    client_id = str(client.id)

    replay = await get_replay(client_id, idempotency_key)
    if replay is not None:
        job_id, stored_hash = replay
        if stored_hash != payload_hash:
            raise IdempotencyKeyConflictError(
                "This Idempotency-Key was already used with a different request body."
            )
        return JobAcceptedResponse(
            job_id=job_id,
            status="PENDING",
            angles=_resolve_plan(body),
            poll_after_ms=_POLL_AFTER_MS,
        )

    mock_job = await jobs_repo.get_any_for_client(session, client.id)
    if mock_job is None:
        raise AppError(
            "No demo job data available for this client in MOCK_MODE. Run "
            "scripts/seed_dev.py against this client first.",
            code=ErrorCode.INTERNAL_ERROR,
        )

    await store_replay(client_id, idempotency_key, str(mock_job.id), payload_hash)

    return JobAcceptedResponse(
        job_id=str(mock_job.id),
        status="PENDING",
        angles=_resolve_plan(body),
        poll_after_ms=_POLL_AFTER_MS,
    )
