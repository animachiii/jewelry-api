"""POST /api/v2/mix.

See docs/api-routes.md and phases/phase-20-mix.md Step 3. Validation order:
operation enabled -> primary source exists/owned/valid -> primary mask
exists/owned/valid (against the primary source's own dimensions) ->
secondary source exists/owned/valid -> secondary mask exists/owned/valid
(against the secondary source's own dimensions) — all before any job row
exists (app/services/job_service.py::create_mix_job_for_request).
"""

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.generate import JobAcceptedResponse
from app.api.v2.schemas.mix import MixRequest
from app.core.auth import require_client_scope
from app.core.idempotency import require_idempotency_key
from app.db.models.api_clients import ApiClient
from app.db.repositories import config_versions as config_versions_repo
from app.db.session import get_db
from app.services.config_service import ConfigUnavailableError
from app.services.job_service import create_mix_job_for_request

router = APIRouter(tags=["mix"])


def _payload_hash(body: BaseModel) -> str:
    """Same construction as generate.py's/background.py's/match.py's/
    recolor.py's own _payload_hash — see docs/business-rules.md §8."""
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@router.post(
    "/mix",
    status_code=202,
    response_model=JobAcceptedResponse,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        409: {"description": "Idempotency key conflict"},
        422: {
            "description": (
                "Operation disabled, asset not found/owned, invalid image, "
                "or mask contract violation (primary or secondary pair)"
            )
        },
        429: {"description": "Rate limit or quota exceeded"},
    },
)
async def create_mix_job(
    body: MixRequest,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobAcceptedResponse:
    config_version = await config_versions_repo.get_active(session)
    if config_version is None:
        raise ConfigUnavailableError("No active config version found.")

    return await create_mix_job_for_request(
        session,
        client,
        config_version,
        body.primary_storage_path,
        body.primary_mask_storage_path,
        body.secondary_storage_path,
        body.secondary_mask_storage_path,
        body.sku_reference,
        body.metadata,
        idempotency_key,
        _payload_hash(body),
    )
