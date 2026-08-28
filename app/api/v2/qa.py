"""GET /api/v2/qa/review-queue, POST /api/v2/qa/{sub_job_id}/decision,
POST /api/v2/internal/qa/rescore-flagged-background. Ops-only.

See docs/api-routes.md and docs/business-rules.md §7. Real as of Phase 9 —
see phases/phase-9-qa-gate.md. Not in CLAUDE.md's original Phase 0 folder
sketch — see app/api/v2/jobs.py for why ops routes get their own module.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.qa import (
    QaDecisionRequest,
    QaRescoreResponse,
    QaReviewItem,
    QaReviewQueueResponse,
)
from app.core.auth import require_ops_scope
from app.core.errors import NotFoundError
from app.db.models.api_clients import ApiClient
from app.db.session import get_db
from app.services.qa_service import (
    build_review_queue_items,
    rescore_flagged_background_operations,
    submit_qa_decision,
)

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


@router.post(
    "/internal/qa/rescore-flagged-background",
    status_code=202,
    response_model=QaRescoreResponse,
    responses={
        401: {"description": "Invalid API key"},
        403: {"description": "Insufficient scope"},
    },
)
async def rescore_flagged_background(
    client: Annotated[ApiClient, Depends(require_ops_scope)],
    session: Annotated[AsyncSession, Depends(get_db)],
    unscored_only: bool = True,
) -> QaRescoreResponse:
    """Re-runs the QA judge over background-operation sub-jobs flagged by a
    broken judge — see qa_service.rescore_flagged_background_operations for
    which defects left them there and why attempt_count is left alone.

    Lives here rather than in a `scripts/` one-off deliberately. The
    equivalent local script published its Celery tasks to whatever
    CELERY_BROKER_URL the operator's own .env happened to name, while
    writing its audit rows to production Postgres — on 2026-08-28 that
    silently sent 17 re-scores into a localhost broker and reported success.
    An operator action on live data belongs in the deployed app, where the
    broker, the Gemini key and the database cannot disagree with each other.
    `POST /internal/config/sync` is the established precedent for an
    ops-only internal route that kicks off background work.

    Idempotent in the only sense that matters: a second call re-dispatches
    whatever is *still* flagged, so anything the first call already cleared
    is simply not in the set.
    """
    sub_job_ids = await rescore_flagged_background_operations(session, unscored_only=unscored_only)
    await session.commit()

    # Dispatch after commit, same rule app/workers/background.py follows for
    # its own QA dispatch: a task must never read a sub-job row before the
    # transaction recording its request has landed.
    from app.workers.qa import score_background

    for sub_job_id in sub_job_ids:
        score_background.delay(str(sub_job_id))

    return QaRescoreResponse(
        dispatched=len(sub_job_ids),
        unscored_only=unscored_only,
        sub_job_ids=[str(s) for s in sub_job_ids],
    )
