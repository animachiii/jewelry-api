"""Phase 3 — pure normalization + hashing logic for the Sheets sync pipeline.
See docs/business-rules.md §9 and app/services/config_sync_service.py.
"""

import pytest

from app.providers.sheets import SheetRows
from app.services.config_sync_service import (
    ConfigValidationError,
    compute_source_hash,
    normalize_sheet_rows,
)

_VALID_ANGLE_ROWS = [
    ["RING", "Rings", "TRUE", "FRONT", "TRUE", "FALSE", "Front view", "", "https://x/ring-f.jpg"],
    ["RING", "Rings", "TRUE", "SIDE", "TRUE", "FALSE", "Side view", "", ""],
    ["RING", "Rings", "TRUE", "DIAGONAL", "TRUE", "TRUE", "Diagonal view", "", "https://x/d.jpg"],
    ["RING", "Rings", "TRUE", "TOP", "FALSE", "FALSE", "", "", ""],
]
_VALID_GLOBAL_ROWS = [
    ["model_version", "gemini-2.5-flash-image-preview"],
    ["qa_similarity_threshold", "0.82"],
    ["default_negative_prompt", "blurry, distorted"],
]


def test_normalize_sheet_rows_builds_expected_payload_shape() -> None:
    payload = normalize_sheet_rows(SheetRows(_VALID_ANGLE_ROWS, _VALID_GLOBAL_ROWS))

    assert payload["global"]["model_version"] == "gemini-2.5-flash-image-preview"
    assert payload["global"]["qa_similarity_threshold"] == 0.82
    assert len(payload["categories"]) == 1
    ring = payload["categories"][0]
    assert ring["code"] == "RING"
    assert ring["is_active"] is True
    assert ring["angles"]["FRONT"] == {
        "enabled": True,
        "synthetic_allowed": False,
        "prompt": "Front view",
        "reference_image_urls": ["https://x/ring-f.jpg"],
    }
    assert ring["angles"]["DIAGONAL"]["synthetic_allowed"] is True
    assert ring["angles"]["TOP"]["enabled"] is False


def test_normalize_sheet_rows_fills_missing_angles_as_disabled() -> None:
    rows = [["NECKLACE", "Necklaces", "TRUE", "FRONT", "TRUE", "FALSE", "Front", "", ""]]
    payload = normalize_sheet_rows(SheetRows(rows, _VALID_GLOBAL_ROWS))

    necklace = payload["categories"][0]
    assert set(necklace["angles"].keys()) == {"FRONT", "SIDE", "DIAGONAL", "TOP"}
    assert necklace["angles"]["SIDE"]["enabled"] is False


def test_normalize_sheet_rows_rejects_unknown_angle() -> None:
    rows = [["RING", "Rings", "TRUE", "BOTTOM", "TRUE", "FALSE", "x", "", ""]]
    with pytest.raises(ConfigValidationError, match="Unknown angle"):
        normalize_sheet_rows(SheetRows(rows, _VALID_GLOBAL_ROWS))


def test_normalize_sheet_rows_rejects_duplicate_angle_for_category() -> None:
    rows = [
        ["RING", "Rings", "TRUE", "FRONT", "TRUE", "FALSE", "a", "", ""],
        ["RING", "Rings", "TRUE", "FRONT", "TRUE", "FALSE", "b", "", ""],
    ]
    with pytest.raises(ConfigValidationError, match="Duplicate angle"):
        normalize_sheet_rows(SheetRows(rows, _VALID_GLOBAL_ROWS))


def test_normalize_sheet_rows_rejects_missing_model_version() -> None:
    with pytest.raises(ConfigValidationError, match="model_version"):
        normalize_sheet_rows(SheetRows(_VALID_ANGLE_ROWS, []))


def test_normalize_sheet_rows_rejects_out_of_range_threshold() -> None:
    bad_global = [*_VALID_GLOBAL_ROWS[:1], ["qa_similarity_threshold", "1.5"]]
    with pytest.raises(ConfigValidationError, match="between 0 and 1"):
        normalize_sheet_rows(SheetRows(_VALID_ANGLE_ROWS, bad_global))


def test_normalize_sheet_rows_rejects_no_rows() -> None:
    with pytest.raises(ConfigValidationError, match="No category rows"):
        normalize_sheet_rows(SheetRows([], _VALID_GLOBAL_ROWS))


def test_compute_source_hash_is_stable_regardless_of_dict_key_order() -> None:
    payload_a = {"categories": [], "global": {"model_version": "v1"}}
    payload_b = {"global": {"model_version": "v1"}, "categories": []}
    assert compute_source_hash(payload_a) == compute_source_hash(payload_b)


def test_compute_source_hash_changes_when_payload_changes() -> None:
    hash_a = compute_source_hash({"global": {"model_version": "v1"}})
    hash_b = compute_source_hash({"global": {"model_version": "v2"}})
    assert hash_a != hash_b


def test_normalize_sheet_rows_is_stable_under_row_reordering() -> None:
    """An ops author reordering sheet rows without changing content must not
    produce a different hash — docs/business-rules.md §9.
    """
    reordered = list(reversed(_VALID_ANGLE_ROWS))
    payload_a = normalize_sheet_rows(SheetRows(_VALID_ANGLE_ROWS, _VALID_GLOBAL_ROWS))
    payload_b = normalize_sheet_rows(SheetRows(reordered, _VALID_GLOBAL_ROWS))
    assert compute_source_hash(payload_a) == compute_source_hash(payload_b)
