"""POST /api/v2/uploads/presign. See docs/api-routes.md.

Real signed upload URLs against the live `jewelry-inputs` bucket. The path
convention in docs/schema.md is `{job_id}/{angle}/{kind}_{short_uuid}.{ext}`,
but no job exists yet at presign time — this endpoint runs before /generate.
We group this request's angles under `pending/{client_id}/{group_id}`; the
client_id segment is what lets /generate's "storage_path belongs to this
client" check (docs/api-routes.md validation rule 4) verify ownership without
a database row, since no Asset exists yet either. The client quotes the
resulting `storage_path` back verbatim on /generate and never parses it.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v2.schemas.uploads import (
    PresignedAngle,
    PresignedBackgroundUpload,
    PresignedOperationUpload,
    PresignUploadRequest,
    PresignUploadResponse,
)
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

    if body.operation is not None:
        storage_path = (
            f"pending/{client.id}/{group_id}/{body.operation.value}/"
            f"input_{uuid.uuid4().hex[:8]}.jpg"
        )
        result = storage_service.generate_upload_url(settings.BUCKET_INPUTS, storage_path)

        background_upload = None
        if body.include_background_upload:
            background_storage_path = (
                f"pending/{client.id}/{group_id}/{body.operation.value}/"
                f"background_{uuid.uuid4().hex[:8]}.jpg"
            )
            background_result = storage_service.generate_upload_url(
                settings.BUCKET_INPUTS, background_storage_path
            )
            background_upload = PresignedBackgroundUpload(
                upload_url=background_result["signedUrl"],
                storage_path=background_storage_path,
                expires_at=expires_at,
            )

        return PresignUploadResponse(
            angles=[],
            operation_upload=PresignedOperationUpload(
                operation=body.operation,
                upload_url=result["signedUrl"],
                storage_path=storage_path,
                expires_at=expires_at,
            ),
            background_upload=background_upload,
        )

    assert body.angles is not None  # enforced by the request schema's mode validator
    angles = []
    for angle in body.angles:
        storage_path = (
            f"pending/{client.id}/{group_id}/{angle.value}/input_{uuid.uuid4().hex[:8]}.jpg"
        )
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
