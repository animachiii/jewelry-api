"""POST /api/v2/background/remove, POST /api/v2/background/replace.

See docs/api-routes.md and phases/phase-15-background-operations.md Step 4.
Two explicit request shapes rather than one `{operation: ...}` body — they
genuinely differ (`preset_code` is required for replace, meaningless for
remove) and a discriminated body would push that into runtime validation
where the OpenAPI spec can't express it. Both return the existing
`JobAcceptedResponse` (app/api/v2/schemas/generate.py).
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BackgroundRemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_path: str
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BackgroundReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_path: str
    preset_code: str
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
