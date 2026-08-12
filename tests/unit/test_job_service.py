"""Phase 15 Step 2 Checkpoint 2 — the operation/angle cross-table invariant
that can't be a Postgres CHECK (it spans jobs and sub_jobs): angle is set
iff the job is ANGLE_GENERATION. See docs/schema.md and
phases/phase-15-background-operations.md Step 2.

Also Step 3 Checkpoint 3 — the operation/preset lookup and validation
helpers `POST /background/remove|replace` will call in Step 4.
"""

import pytest

from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import Angle, Operation, SyncStatus
from app.services.job_service import (
    OperationDisabledError,
    PresetInactiveError,
    PresetNotFoundError,
    find_operation_config,
    find_preset,
    resolve_operation_unit_cost,
    validate_operation_angle_consistency,
    validate_operation_enabled,
    validate_preset,
)


def test_angle_generation_without_angle_is_rejected() -> None:
    with pytest.raises(ValueError, match="must specify an angle"):
        validate_operation_angle_consistency(Operation.ANGLE_GENERATION, None)


def test_angle_generation_with_angle_is_accepted() -> None:
    validate_operation_angle_consistency(Operation.ANGLE_GENERATION, Angle.FRONT)


_BACKGROUND_OPS = [Operation.BACKGROUND_REMOVAL, Operation.BACKGROUND_REPLACEMENT]


@pytest.mark.parametrize("operation", _BACKGROUND_OPS)
def test_background_operation_with_angle_is_rejected(operation: Operation) -> None:
    """An angle sub-job on a BACKGROUND_REMOVAL/BACKGROUND_REPLACEMENT job
    is rejected by job_service — the exact case Checkpoint 2 asks for."""
    with pytest.raises(ValueError, match="must not specify an angle"):
        validate_operation_angle_consistency(operation, Angle.FRONT)


@pytest.mark.parametrize("operation", _BACKGROUND_OPS)
def test_background_operation_without_angle_is_accepted(operation: Operation) -> None:
    validate_operation_angle_consistency(operation, None)


def _config_version(global_block: dict[str, object]) -> ConfigVersion:
    return ConfigVersion(
        version_number=1,
        source_hash="h",
        payload={"categories": [], "global": global_block},
        sync_status=SyncStatus.SUCCESS,
    )


def test_find_operation_config_returns_the_operation_block() -> None:
    cv = _config_version(
        {"operations": {"BACKGROUND_REMOVAL": {"enabled": True, "unit_cost_usd": 0.05}}}
    )
    assert find_operation_config(cv, Operation.BACKGROUND_REMOVAL) == {
        "enabled": True,
        "unit_cost_usd": 0.05,
    }
    assert find_operation_config(cv, Operation.BACKGROUND_REPLACEMENT) is None


def test_validate_operation_enabled_rejects_missing_operation() -> None:
    cv = _config_version({"operations": {}})
    with pytest.raises(OperationDisabledError):
        validate_operation_enabled(cv, Operation.BACKGROUND_REMOVAL)


def test_validate_operation_enabled_rejects_disabled_operation() -> None:
    cv = _config_version({"operations": {"BACKGROUND_REMOVAL": {"enabled": False}}})
    with pytest.raises(OperationDisabledError):
        validate_operation_enabled(cv, Operation.BACKGROUND_REMOVAL)


def test_validate_operation_enabled_accepts_enabled_operation() -> None:
    cv = _config_version({"operations": {"BACKGROUND_REMOVAL": {"enabled": True}}})
    assert validate_operation_enabled(cv, Operation.BACKGROUND_REMOVAL) == {"enabled": True}


def test_find_preset_looks_up_by_code() -> None:
    cv = _config_version({"background_presets": [{"code": "STUDIO_WHITE", "name": "Studio White"}]})
    assert find_preset(cv, "STUDIO_WHITE") == {"code": "STUDIO_WHITE", "name": "Studio White"}
    assert find_preset(cv, "NOPE") is None


def test_validate_preset_rejects_absent_preset_code() -> None:
    cv = _config_version({"background_presets": []})
    with pytest.raises(PresetNotFoundError):
        validate_preset(cv, "STUDIO_WHITE")


def test_validate_preset_rejects_inactive_preset() -> None:
    cv = _config_version({"background_presets": [{"code": "STUDIO_WHITE", "is_active": False}]})
    with pytest.raises(PresetInactiveError):
        validate_preset(cv, "STUDIO_WHITE")


def test_validate_preset_accepts_active_preset() -> None:
    cv = _config_version(
        {"background_presets": [{"code": "STUDIO_WHITE", "is_active": True, "name": "White"}]}
    )
    preset = validate_preset(cv, "STUDIO_WHITE")
    assert preset["name"] == "White"


def test_resolve_operation_unit_cost_uses_per_operation_value() -> None:
    cv = _config_version(
        {
            "unit_cost_usd": 0.02,
            "operations": {"BACKGROUND_REMOVAL": {"unit_cost_usd": 0.05}},
        }
    )
    assert resolve_operation_unit_cost(cv, Operation.BACKGROUND_REMOVAL) == 0.05


def test_resolve_operation_unit_cost_falls_back_to_global() -> None:
    """docs/business-rules.md §10: a job must never bill at a hardcoded rate."""
    cv = _config_version({"unit_cost_usd": 0.02, "operations": {"BACKGROUND_REMOVAL": {}}})
    assert resolve_operation_unit_cost(cv, Operation.BACKGROUND_REMOVAL) == 0.02


def test_resolve_operation_unit_cost_falls_back_to_zero_when_nothing_configured() -> None:
    cv = _config_version({})
    assert resolve_operation_unit_cost(cv, Operation.BACKGROUND_REMOVAL) == 0.0
