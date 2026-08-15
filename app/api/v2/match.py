"""POST /api/v2/match.

See docs/api-routes.md and phases/phase-18-match.md Step 3. Validation order
mirrors /background/*: operation enabled -> target_category exists and is
active -> storage_path exists and belongs to this client -> image passes
image_validation.inspect_and_validate — all before any job row exists
(app/services/job_service.py::create_match_job_for_request).

Does not dispatch any Celery task — MATCH's real fan-out/dispatch logic is
Phase 18 Step 4's job, not built yet. See job_service.py's own comment at
the exact spot where dispatch will go.
"""

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.generate import JobAcceptedResponse
from app.api.v2.schemas.match import MatchRequest
from app.core.auth import require_client_scope
from app.core.idempotency import require_idempotency_key
from app.db.models.api_clients import ApiClient
from app.db.repositories import config_versions as config_versions_repo
from app.db.session import get_db
from app.services.config_service import ConfigUnavailableError
from app.services.job_service import create_match_job_for_request

router = APIRouter(tags=["match"])


def _payload_hash(body: BaseModel) -> str:
    """Same construction as generate.py's and background.py's own
    _payload_hash — see docs/business-rules.md §8.
    """
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@router.post(
    "/match",
    status_code=202,
    response_model=JobAcceptedResponse,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        409: {"description": "Idempotency key conflict"},
        422: {
            "description": (
                "Operation disabled, category not found/inactive, "
                "asset not found/owned, or invalid image"
            )
        },
        429: {"description": "Rate limit or quota exceeded"},
    },
)
async def create_match_job(
    body: MatchRequest,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobAcceptedResponse:
    config_version = await config_versions_repo.get_active(session)
    if config_version is None:
        raise ConfigUnavailableError("No active config version found.")

    return await create_match_job_for_request(
        session,
        client,
        config_version,
        body.storage_path,
        body.target_category,
        body.variant_count,
        body.sku_reference,
        body.metadata,
        idempotency_key,
        _payload_hash(body),
    )
