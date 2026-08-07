"""POST /api/v2/generate. See docs/api-routes.md and docs/business-rules.md §1, §8.

Real job creation (Phase 2) — see phases/phase-2-data-model.md. Only the
retry endpoint is still MOCK_MODE; /generate is real regardless of that flag.
"""

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.generate import GenerateJobRequest, JobAcceptedResponse
from app.core.auth import require_client_scope
from app.core.idempotency import require_idempotency_key
from app.db.models.api_clients import ApiClient
from app.db.repositories import config_versions as config_versions_repo
from app.db.session import get_db
from app.services.config_service import ConfigUnavailableError
from app.services.job_service import create_job_for_request

router = APIRouter(tags=["generate"])


def _payload_hash(body: GenerateJobRequest) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


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
    config_version = await config_versions_repo.get_active(session)
    if config_version is None:
        raise ConfigUnavailableError("No active config version found.")

    return await create_job_for_request(
        session,
        client,
        config_version,
        body,
        idempotency_key,
        _payload_hash(body),
    )
