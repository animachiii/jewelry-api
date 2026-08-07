"""Step 1 checkpoint tests — GenerateJobRequest discriminated angle union."""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api.v2.schemas.generate import GenerateJobRequest


def test_valid_request_parses_each_angle_mode() -> None:
    req = GenerateJobRequest.model_validate(
        {
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": "abc/front.jpg"},
                "SIDE": {"synthetic": True},
                "DIAGONAL": {"skip": True},
            },
        }
    )
    assert req.angles["FRONT"].mode == "uploaded"
    assert req.angles["SIDE"].mode == "synthetic"
    assert req.angles["DIAGONAL"].mode == "skipped"


def test_angle_with_both_skip_and_storage_path_rejected_422() -> None:
    with pytest.raises(PydanticValidationError) as exc_info:
        GenerateJobRequest.model_validate(
            {
                "category_code": "RING",
                "angles": {"FRONT": {"skip": True, "storage_path": "abc/front.jpg"}},
            }
        )
    errors = exc_info.value.errors()
    assert any("FRONT" in str(e["loc"]) for e in errors)


def test_unknown_angle_key_rejected() -> None:
    with pytest.raises(PydanticValidationError) as exc_info:
        GenerateJobRequest.model_validate(
            {
                "category_code": "RING",
                "angles": {"BACK": {"synthetic": True}},
            }
        )
    errors = exc_info.value.errors()
    assert any("BACK" in str(e["loc"]) or "BACK" in str(e.get("input")) for e in errors)


def test_angle_with_no_mode_set_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        GenerateJobRequest.model_validate({"category_code": "RING", "angles": {"FRONT": {}}})


def test_angle_with_unknown_field_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        GenerateJobRequest.model_validate(
            {"category_code": "RING", "angles": {"FRONT": {"storage_path": "a.jpg", "extra": 1}}}
        )
