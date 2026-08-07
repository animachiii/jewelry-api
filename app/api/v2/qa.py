"""GET /api/v2/qa/review-queue, POST /api/v2/qa/{sub_job_id}/decision. Ops-only.

See docs/api-routes.md and docs/business-rules.md §7. Not in CLAUDE.md's
original Phase 0 folder sketch — see app/api/v2/jobs.py for why ops routes
get their own module.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v2.schemas.qa import QaDecisionRequest, QaReviewQueueResponse
from app.core.auth import require_ops_scope
from app.db.models.api_clients import ApiClient

router = APIRouter(tags=["qa"])


@router.get(
    "/qa/review-queue",
    response_model=QaReviewQueueResponse,
    responses={401: {"description": "Invalid API key"}, 403: {"description": "Insufficient scope"}},
)
async def get_review_queue(
    client: Annotated[ApiClient, Depends(require_ops_scope)],
) -> QaReviewQueueResponse:
    raise NotImplementedError("Real review queue lands in Phase 9 / mock fixture in Step 3.")


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
async def submit_qa_decision(
    sub_job_id: str,
    body: QaDecisionRequest,
    client: Annotated[ApiClient, Depends(require_ops_scope)],
) -> None:
    raise NotImplementedError("Real QA decision lands in Phase 9 / mock fixture in Step 3.")
