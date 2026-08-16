"""POST /api/v2/mix.

See docs/api-routes.md and phases/phase-20-mix.md Step 3. `primary`/
`secondary` naming, not `source`/`reference` — both images are equally real
uploaded photographs of physical pieces (unlike MATCH, where the source is
explicitly a style reference, not the transformed subject). "Primary" is
the image whose frame the final output keeps; "secondary" is where the
grafted content comes from.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_storage_path: str
    primary_mask_storage_path: str
    secondary_storage_path: str
    secondary_mask_storage_path: str
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
