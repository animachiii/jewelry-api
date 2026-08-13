"""POST /api/v2/background/remove, POST /api/v2/background/replace.

See docs/api-routes.md and phases/phase-15-background-operations.md Step 4.
Validation order mirrors /generate: operation enabled -> preset exists and
is active (replace only) -> storage_path exists and belongs to this client
-> image passes image_validation.inspect_and_validate — all before any job
row exists (app/services/job_service.py::create_background_job_for_request).
"""

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.background import BackgroundRemoveRequest, BackgroundReplaceRequest
from app.api.v2.schemas.generate import JobAcceptedResponse
from app.core.auth import require_client_scope
from app.core.idempotency import require_idempotency_key
from app.db.models.api_clients import ApiClient
from app.db.models.enums import Operation
from app.db.repositories import config_versions as config_versions_repo
from app.db.session import get_db
from app.services.config_service import ConfigUnavailableError
from app.services.job_service import create_background_job_for_request

router = APIRouter(tags=["background"])


def _payload_hash(body: BaseModel) -> str:
    """Same construction as generate.py's own _payload_hash — see
    docs/business-rules.md §8.
    """
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@router.post(
    "/background/remove",
    status_code=202,
    response_model=JobAcceptedResponse,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        409: {"description": "Idempotency key conflict"},
        422: {"description": "Operation disabled, asset not found/owned, or invalid image"},
        429: {"description": "Rate limit or quota exceeded"},
    },
)
async def remove_background(
    body: BackgroundRemoveRequest,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobAcceptedResponse:
    config_version = await config_versions_repo.get_active(session)
    if config_version is None:
        raise ConfigUnavailableError("No active config version found.")

    return await create_background_job_for_request(
        session,
        client,
        config_version,
        Operation.BACKGROUND_REMOVAL,
        body.storage_path,
        None,
        body.sku_reference,
        body.metadata,
        idempotency_key,
        _payload_hash(body),
    )


@router.post(
    "/background/replace",
    status_code=202,
    response_model=JobAcceptedResponse,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        409: {"description": "Idempotency key conflict"},
        422: {
            "description": (
                "Operation disabled, preset not found/inactive, "
                "asset not found/owned, or invalid image"
            )
        },
        429: {"description": "Rate limit or quota exceeded"},
    },
)
async def replace_background(
    body: BackgroundReplaceRequest,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobAcceptedResponse:
    config_version = await config_versions_repo.get_active(session)
    if config_version is None:
        raise ConfigUnavailableError("No active config version found.")

    return await create_background_job_for_request(
        session,
        client,
        config_version,
        Operation.BACKGROUND_REPLACEMENT,
        body.storage_path,
        body.preset_code,
        body.sku_reference,
        body.metadata,
        idempotency_key,
        _payload_hash(body),
        background_storage_path=body.background_storage_path,
    )
