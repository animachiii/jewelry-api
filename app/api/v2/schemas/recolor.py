"""POST /api/v2/recolor.

See docs/api-routes.md and phases/phase-19-recolor.md Step 3. Same shape as
app/api/v2/schemas/match.py: one request body, no discriminated union
needed — RECOLOR has no mutually-exclusive fields the way
BackgroundReplaceRequest's preset_code/background_storage_path does.
`palette_code` is validated against the active config's `payload.global.palette`
list, the RECOLOR-specific counterpart to preset validation.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RecolorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_path: str
    mask_storage_path: str
    palette_code: str
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
