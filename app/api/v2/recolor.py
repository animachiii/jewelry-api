"""POST /api/v2/recolor.

See docs/api-routes.md and phases/phase-19-recolor.md Step 3. Validation
order mirrors /match: operation enabled -> palette_code exists and is
active -> source storage_path exists and belongs to this client -> source
image passes image_validation.inspect_and_validate -> mask storage_path
exists and belongs to this client -> mask passes
mask_validation.validate_mask against the source's own dimensions — all
before any job row exists
(app/services/job_service.py::create_recolor_job_for_request).
"""

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.generate import JobAcceptedResponse
from app.api.v2.schemas.recolor import RecolorRequest
from app.core.auth import require_client_scope
from app.core.idempotency import require_idempotency_key
from app.db.models.api_clients import ApiClient
from app.db.repositories import config_versions as config_versions_repo
from app.db.session import get_db
from app.services.config_service import ConfigUnavailableError
from app.services.job_service import create_recolor_job_for_request

router = APIRouter(tags=["recolor"])


def _payload_hash(body: BaseModel) -> str:
    """Same construction as generate.py's/background.py's/match.py's own
    _payload_hash — see docs/business-rules.md §8.
    """
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@router.post(
    "/recolor",
    status_code=202,
    response_model=JobAcceptedResponse,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        409: {"description": "Idempotency key conflict"},
        422: {
            "description": (
                "Operation disabled, palette not found/inactive, "
                "asset not found/owned, invalid image, or mask contract violation"
            )
        },
        429: {"description": "Rate limit or quota exceeded"},
    },
)
async def create_recolor_job(
    body: RecolorRequest,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobAcceptedResponse:
    config_version = await config_versions_repo.get_active(session)
    if config_version is None:
        raise ConfigUnavailableError("No active config version found.")

    return await create_recolor_job_for_request(
        session,
        client,
        config_version,
        body.storage_path,
        body.mask_storage_path,
        body.palette_code,
        body.sku_reference,
        body.metadata,
        idempotency_key,
        _payload_hash(body),
    )
