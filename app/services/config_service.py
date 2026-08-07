"""GET /api/v2/config — active angle matrix, no prompts/reference images exposed.

See docs/api-routes.md and docs/schema.md (config_versions.payload shape).
Real read against the active `config_versions` row — Redis caching (the
`config:active` key in docs/schema.md) lands in Phase 3; reading straight
from Postgres is a correct, just not yet cached, implementation.
"""

from app.api.v2.schemas.config import AngleAvailability, CategoryConfig, ConfigResponse
from app.core.errors import AppError, ErrorCode
from app.db.models.config_versions import ConfigVersion


class ConfigUnavailableError(AppError):
    code = ErrorCode.CONFIG_UNAVAILABLE
    http_status = 503


def build_config_response(config_version: ConfigVersion) -> ConfigResponse:
    categories = []
    for cat in config_version.payload["categories"]:
        angles = {
            angle: AngleAvailability(
                enabled=spec["enabled"], synthetic_allowed=spec["synthetic_allowed"]
            )
            for angle, spec in cat["angles"].items()
        }
        categories.append(
            CategoryConfig(
                code=cat["code"], name=cat["name"], is_active=cat["is_active"], angles=angles
            )
        )
    return ConfigResponse(
        config_version=config_version.version_number,
        categories=categories,
    )
