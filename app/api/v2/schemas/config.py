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


class ConfigResponse(BaseModel):
    config_version: int
    categories: list[CategoryConfig]
