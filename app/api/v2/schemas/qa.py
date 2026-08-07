"""GET /qa/review-queue, POST /qa/{sub_job_id}/decision — ops-only.

See docs/business-rules.md §7."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.db.models.enums import Angle


class QaDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]


class QaReviewItem(BaseModel):
    sub_job_id: str
    job_id: str
    angle: Angle
    category_code: str
    qa_score: float | None
    image_url: str
    reference_image_urls: list[str]
    created_at: datetime


class QaReviewQueueResponse(BaseModel):
    items: list[QaReviewItem]
