"""POST /api/v2/generate-with-cleanup.

See docs/api-routes.md and docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md
section 2. Unlike GenerateJobRequest's per-angle dict (each angle can carry
its own storage_path, or be synthetic, or be skipped), `angles` here is a
plain list of angle codes with no per-angle choice -- every angle in this
operation derives from the ONE cleaned photo, so there is nothing per-angle
to specify. Mixing in synthetic angles is deliberately not supported in v1
(a client wanting that already has /generate) -- see the design spec's
section 9.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import Angle


class GenerateWithCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_path: str
    category_code: str
    angles: list[Angle]
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
