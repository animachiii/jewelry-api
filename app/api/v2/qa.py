"""GET /api/v2/qa/review-queue, POST /api/v2/qa/{sub_job_id}/decision. Ops-only.

See docs/api-routes.md and docs/business-rules.md §7. Real as of Phase 9 —
see phases/phase-9-qa-gate.md. Not in CLAUDE.md's original Phase 0 folder
sketch — see app/api/v2/jobs.py for why ops routes get their own module.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.qa import QaDecisionRequest, QaReviewItem, QaReviewQueueResponse
from app.core.auth import require_ops_scope
from app.core.errors import NotFoundError
from app.db.models.api_clients import ApiClient
from app.db.session import get_db
from app.services.qa_service import build_review_queue_items, submit_qa_decision

router = APIRouter(tags=["qa"])


@router.get(
    "/qa/review-queue",
    response_model=QaReviewQueueResponse,
    responses={401: {"description": "Invalid API key"}, 403: {"description": "Insufficient scope"}},
)
async def get_review_queue(
    client: Annotated[ApiClient, Depends(require_ops_scope)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> QaReviewQueueResponse:
    items = await build_review_queue_items(session)
    return QaReviewQueueResponse(items=[QaReviewItem(**item) for item in items])


@router.post(
    "/qa/{sub_job_id}/decision",
    status_code=202,
    responses={
        401: {"description": "Invalid API key"},
        403: {"description": "Insufficient scope"},
        404: {"description": "Sub-job not found"},
        409: {"description": "Sub-job not in QA_REVIEW"},
    },
)
async def submit_qa_decision_route(
    sub_job_id: str,
    body: QaDecisionRequest,
    client: Annotated[ApiClient, Depends(require_ops_scope)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    try:
        sub_job_uuid = uuid.UUID(sub_job_id)
    except ValueError as exc:
        raise NotFoundError(
            "Sub-job not found.", details={"sub_job_id": sub_job_id}, code="SUB_JOB_NOT_FOUND"
        ) from exc

    await submit_qa_decision(session, sub_job_uuid, body.decision)
    await session.commit()
