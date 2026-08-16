"""POST /api/v2/uploads/presign — see docs/api-routes.md.

Phase 15 Step 4 adds an operation-aware form so BACKGROUND_REMOVAL/
BACKGROUND_REPLACEMENT can presign one upload without inventing a fake
angle: `{"operation": "BACKGROUND_REMOVAL"}` instead of
`{"category_code": ..., "angles": [...]}`. Mutually exclusive — same
one-mode-only pattern app/api/v2/schemas/generate.py::AngleSpec uses.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.db.models.enums import Angle, Operation


class PresignUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_code: str | None = None
    angles: list[Angle] | None = None
    operation: Operation | None = None
    include_background_upload: bool = False

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "PresignUploadRequest":
        angle_mode = self.category_code is not None or self.angles is not None
        op_mode = self.operation is not None
        if angle_mode and op_mode:
            raise ValueError("category_code/angles and operation are mutually exclusive")
        if angle_mode and (self.category_code is None or not self.angles):
            raise ValueError("category_code and angles must both be set together")
        if op_mode and self.operation == Operation.ANGLE_GENERATION:
            raise ValueError(
                "operation must be BACKGROUND_REMOVAL, BACKGROUND_REPLACEMENT, MATCH, "
                "RECOLOR, or MIX (ANGLE_GENERATION uses category_code/angles instead)"
            )
        if not angle_mode and not op_mode:
            raise ValueError("either category_code+angles or operation is required")
        if self.include_background_upload and self.operation != Operation.BACKGROUND_REPLACEMENT:
            raise ValueError(
                "include_background_upload is only valid with operation=BACKGROUND_REPLACEMENT"
            )
        return self


class PresignedAngle(BaseModel):
    angle: Angle
    upload_url: str
    storage_path: str
    expires_at: datetime


class PresignedOperationUpload(BaseModel):
    operation: Operation
    upload_url: str
    storage_path: str
    expires_at: datetime


class PresignedBackgroundUpload(BaseModel):
    upload_url: str
    storage_path: str
    expires_at: datetime


class PresignedMaskUpload(BaseModel):
    """Second upload slot for operation=RECOLOR, or the primary-mask slot
    for operation=MIX (see PresignedSecondaryUpload/
    PresignedSecondaryMaskUpload below for MIX's other two) — a RECOLOR or
    MIX job is never valid without a mask (unlike a custom background photo
    for BACKGROUND_REPLACEMENT, which is optional), so this is always
    returned alongside operation_upload, no extra request flag needed. See
    phases/phase-19-recolor.md Step 3 and phases/phase-20-mix.md Step 3.
    """

    upload_url: str
    storage_path: str
    expires_at: datetime


class PresignedSecondaryUpload(BaseModel):
    """Third upload slot for operation=MIX — the second source photo
    (image B, the piece grafted from). Always returned alongside
    operation_upload/mask_upload for MIX, no extra request flag needed — a
    MIX job is never valid without all four slots. See
    phases/phase-20-mix.md Step 3.
    """

    upload_url: str
    storage_path: str
    expires_at: datetime


class PresignedSecondaryMaskUpload(BaseModel):
    """Fourth upload slot for operation=MIX — the mask on the second source
    photo (region B to cut). See PresignedSecondaryUpload above.
    """

    upload_url: str
    storage_path: str
    expires_at: datetime


class PresignUploadResponse(BaseModel):
    angles: list[PresignedAngle] = []
    operation_upload: PresignedOperationUpload | None = None
    background_upload: PresignedBackgroundUpload | None = None
    mask_upload: PresignedMaskUpload | None = None
    secondary_upload: PresignedSecondaryUpload | None = None
    secondary_mask_upload: PresignedSecondaryMaskUpload | None = None
