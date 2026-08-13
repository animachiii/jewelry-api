"""POST /api/v2/background/remove, POST /api/v2/background/replace.

See docs/api-routes.md and phases/phase-15-background-operations.md Step 4.
Two explicit request shapes rather than one `{operation: ...}` body — they
genuinely differ (`preset_code`/`background_storage_path` are meaningless
for remove) and a discriminated body would push that into runtime
validation where the OpenAPI spec can't express it. Both return the existing
`JobAcceptedResponse` (app/api/v2/schemas/generate.py).

`BackgroundReplaceRequest`'s `preset_code`/`background_storage_path` mutual
exclusivity (custom-background compositing) follows the same
`@model_validator(mode="after")` pattern
`app/api/v2/schemas/uploads.py::PresignUploadRequest._exactly_one_mode`
already uses — a bare `ValueError` there is converted to a clean 422
VALIDATION_ERROR by app/core/errors.py's jsonable_encoder fix, not a 500.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BackgroundRemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_path: str
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BackgroundReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_path: str
    preset_code: str | None = None
    background_storage_path: str | None = None
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_background_source(self) -> "BackgroundReplaceRequest":
        if (self.preset_code is None) == (self.background_storage_path is None):
            raise ValueError(
                "exactly one of preset_code or background_storage_path is required"
            )
        return self
