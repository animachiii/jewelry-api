"""POST /api/v2/uploads/presign. See docs/api-routes.md.

Real signed upload URLs against the live `jewelry-inputs` bucket. The path
convention in docs/schema.md is `{job_id}/{angle}/{kind}_{short_uuid}.{ext}`,
but no job exists yet at presign time — this endpoint runs before /generate.
We group this request's angles under a `pending/{uuid}` prefix; the client
quotes the resulting `storage_path` back verbatim on /generate.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v2.schemas.uploads import PresignedAngle, PresignUploadRequest, PresignUploadResponse
from app.config import settings
from app.core.auth import require_client_scope
from app.db.models.api_clients import ApiClient
from app.services import storage_service

router = APIRouter(tags=["uploads"])

_UPLOAD_URL_TTL_SECONDS = 600


@router.post(
    "/uploads/presign",
    response_model=PresignUploadResponse,
    responses={
        401: {"description": "Invalid API key"},
        422: {"description": "Bad category or angle"},
    },
)
async def presign_uploads(
    body: PresignUploadRequest,
    client: Annotated[ApiClient, Depends(require_client_scope)],
) -> PresignUploadResponse:
    group_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(seconds=_UPLOAD_URL_TTL_SECONDS)

    angles = []
    for angle in body.angles:
        storage_path = f"pending/{group_id}/{angle.value}/input_{uuid.uuid4().hex[:8]}.jpg"
        result = storage_service.generate_upload_url(settings.BUCKET_INPUTS, storage_path)
        angles.append(
            PresignedAngle(
                angle=angle,
                upload_url=result["signedUrl"],
                storage_path=storage_path,
                expires_at=expires_at,
            )
        )
    return PresignUploadResponse(angles=angles)
