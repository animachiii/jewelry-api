"""GET /qa/review-queue, POST /qa/{sub_job_id}/decision — ops-only.

See docs/business-rules.md §7."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.db.models.enums import Angle, Operation


class QaDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]


class QaReviewItem(BaseModel):
    sub_job_id: str
    job_id: str
    # angle/category_code are None for a Phase 15 background-operation item
    # — the QA gate is shared across both, see
    # phases/phase-15-background-operations.md Step 5.
    operation: Operation
    angle: Angle | None
    category_code: str | None
    qa_score: float | None
    image_url: str
    reference_image_urls: list[str]
    # Renamed from the Phase 1 scaffold's `created_at` (Phase 9) — sub_jobs
    # has no created_at column (docs/schema.md), only started_at/
    # completed_at. started_at is set when the sub-job entered GENERATING
    # (app/services/generation_service.py), the closest real timestamp to
    # "when this item's generation began."
    started_at: datetime | None


class QaReviewQueueResponse(BaseModel):
    items: list[QaReviewItem]
