"""POST /api/v2/uploads/presign — see docs/api-routes.md."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models.enums import Angle


class PresignUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_code: str
    angles: list[Angle]


class PresignedAngle(BaseModel):
    angle: Angle
    upload_url: str
    storage_path: str
    expires_at: datetime


class PresignUploadResponse(BaseModel):
    angles: list[PresignedAngle]
