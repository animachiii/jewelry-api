"""POST /api/v2/match.

See docs/api-routes.md and phases/phase-18-match.md Step 3. Same shape as
app/api/v2/schemas/background.py: one request body, no discriminated union
needed (MATCH has no mutually-exclusive fields the way
BackgroundReplaceRequest's preset_code/background_storage_path does).
`target_category` is validated against the active config's `payload.categories`
list — the same single source of truth /generate's `category_code` uses, not
a MATCH-specific list.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_path: str
    target_category: str
    variant_count: int = Field(default=1, ge=1, le=4)
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
