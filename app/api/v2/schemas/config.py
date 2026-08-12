"""GET /api/v2/config — angle matrix only, no prompts/reference images. See docs/api-routes.md."""

from pydantic import BaseModel

from app.db.models.enums import Angle


class AngleAvailability(BaseModel):
    enabled: bool
    synthetic_allowed: bool


class CategoryConfig(BaseModel):
    code: str
    name: str
    is_active: bool
    angles: dict[Angle, AngleAvailability]


class PresetSummary(BaseModel):
    """code + name only — prompts and reference_image_urls stay internal,
    same rule angle prompts already follow. Only active presets are listed;
    see phases/phase-15-background-operations.md Step 3.
    """

    code: str
    name: str


class ConfigResponse(BaseModel):
    config_version: int
    categories: list[CategoryConfig]
    background_presets: list[PresetSummary] = []


class ConfigSyncResponse(BaseModel):
    """POST /internal/config/sync response — see docs/api-routes.md."""

    config_version: int
    sync_status: str
    activated: bool
