"""validate_operation_angle_consistency must accept BOTH shapes for
GENERATE_WITH_CLEANUP (angle-less cleanup sub-job, angled sub-jobs) while
every other operation keeps its existing single-shape rule. See
docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md section 3.
"""

import pytest

from app.db.models.enums import Angle, Operation
from app.services.job_service import validate_operation_angle_consistency


def test_generate_with_cleanup_accepts_angle_none() -> None:
    validate_operation_angle_consistency(Operation.GENERATE_WITH_CLEANUP, None)


def test_generate_with_cleanup_accepts_an_angle() -> None:
    validate_operation_angle_consistency(Operation.GENERATE_WITH_CLEANUP, Angle.FRONT)


def test_angle_generation_still_requires_an_angle() -> None:
    with pytest.raises(ValueError, match="must specify an angle"):
        validate_operation_angle_consistency(Operation.ANGLE_GENERATION, None)


def test_background_removal_still_rejects_an_angle() -> None:
    with pytest.raises(ValueError, match="must not specify an angle"):
        validate_operation_angle_consistency(Operation.BACKGROUND_REMOVAL, Angle.FRONT)


def test_mix_still_rejects_an_angle() -> None:
    with pytest.raises(ValueError, match="must not specify an angle"):
        validate_operation_angle_consistency(Operation.MIX, Angle.SIDE)
