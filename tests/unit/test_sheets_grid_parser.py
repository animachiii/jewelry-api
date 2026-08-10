"""Parsing the client's real pivot-grid sheet layout.

Confirmed live 2026-08-10 against the real spreadsheet (roadmap open decision
#2). The fixture below mirrors that structure exactly — header row, the v1-era
shot-type grid that v2 must ignore, the `Angles` row declaring per-type angle
order, and the per-angle JSON+Drive-URL cells beneath it — with prompts
shortened. Never hits the live Sheets API (CLAUDE.md Hard Rule 5).
"""

import json

import pytest

from app.providers.sheets import (
    SheetsUnavailableError,
    drive_url_to_direct_download,
    parse_grid,
)


def _angle_cell(prompt: str, file_id: str = "FILEID123") -> str:
    payload = {
        "camera_angle": "Front view at a low camera height.",
        "photography_summary": "Luxury commercial product photography.",
        "production_prompt": prompt,
    }
    return json.dumps(payload) + f" https://drive.google.com/file/d/{file_id}/view?usp=drive_link"


REAL_SHAPE_GRID: list[list[str]] = [
    [
        "",
        "Anklets",
        "Necklace",
        "Earrings",
        "Bangles",
        "Bracelets",
        "Hipbelt",
        "Ring",
        "Matching jewellery ",
    ],
    ["Female Model"],
    ["Traditional", "shot-type prompt", "shot-type prompt", "", "", "", "", "", "ignored"],
    ["Product styling"],
    ["Modern", "shot-type prompt"],
    [],
    # The blue "Angles" row: per-column comma-separated angle order.
    [
        "Angles",
        "Top, diagonal ",
        "Front, diagonal ",
        "front, diagonal ",
        "front, side, diagonal",
        "front, side, diagonal",
        "front, diagonal, ",
        "front, side, diagonal, top",
    ],
    # Leading "" is the label column A — data starts at column B, same as the
    # real sheet.
    [
        "",
        _angle_cell("anklet top"),
        _angle_cell("necklace front"),
        _angle_cell("earring front"),
        _angle_cell("bangle front"),
        _angle_cell("bracelet front"),
        _angle_cell("hipbelt front"),
        _angle_cell("ring front"),
    ],
    [
        "",
        _angle_cell("anklet diagonal"),
        _angle_cell("necklace diagonal"),
        _angle_cell("earring diagonal"),
        _angle_cell("bangle side"),
        _angle_cell("bracelet side"),
        _angle_cell("hipbelt diagonal"),
        _angle_cell("ring side"),
    ],
    [
        "",
        "",
        "",
        "",
        _angle_cell("bangle diagonal"),
        _angle_cell("bracelet diagonal"),
        "",
        _angle_cell("ring diagonal"),
    ],
    ["", "", "", "", "", "", "", _angle_cell("ring top")],
]


def _rows_for(rows: list[list[str]], code: str) -> list[list[str]]:
    return [r for r in rows if r[0] == code]


def test_parses_all_seven_categories_from_the_real_layout() -> None:
    rows = parse_grid(REAL_SHAPE_GRID)
    codes = {r[0] for r in rows}
    assert codes == {"ANKLET", "NECKLACE", "EARRING", "BANGLE", "BRACELET", "HIPBELT", "RING"}


def test_angle_order_is_positional_per_the_declared_order() -> None:
    """Row N under the Angles row is the Nth angle in that column's list —
    this positional coupling is the whole reason the sheet is readable at all.
    """
    rows = parse_grid(REAL_SHAPE_GRID)
    ring = {r[3]: r for r in _rows_for(rows, "RING")}
    assert set(ring) == {"FRONT", "SIDE", "DIAGONAL", "TOP"}
    assert ring["FRONT"][6] == "ring front"
    assert ring["SIDE"][6] == "ring side"
    assert ring["DIAGONAL"][6] == "ring diagonal"
    assert ring["TOP"][6] == "ring top"

    # Anklets declare "Top, diagonal" — TOP first, so the first data row is TOP.
    anklet = {r[3]: r for r in _rows_for(rows, "ANKLET")}
    assert set(anklet) == {"TOP", "DIAGONAL"}
    assert anklet["TOP"][6] == "anklet top"
    assert anklet["DIAGONAL"][6] == "anklet diagonal"


def test_only_production_prompt_becomes_the_prompt() -> None:
    """camera_angle/photography_summary are the authoring notes production_prompt
    was distilled from — sending them too would duplicate and contradict it.
    """
    rows = parse_grid(REAL_SHAPE_GRID)
    front = _rows_for(rows, "NECKLACE")[0]
    assert front[6] == "necklace front"
    assert "camera_angle" not in front[6]
    assert "photography_summary" not in front[6]


def test_drive_share_links_are_rewritten_to_direct_download() -> None:
    """A /view share link serves an HTML interstitial, not image bytes —
    generation_service's reference fetch needs the uc?export=download form.
    """
    rows = parse_grid(REAL_SHAPE_GRID)
    urls = _rows_for(rows, "RING")[0][8]
    assert urls == "https://drive.google.com/uc?export=download&id=FILEID123"
    assert "/view" not in urls


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://drive.google.com/file/d/ABC_123-x/view?usp=drive_link",
            "https://drive.google.com/uc?export=download&id=ABC_123-x",
        ),
        (
            "https://drive.google.com/open?id=XYZ789",
            "https://drive.google.com/uc?export=download&id=XYZ789",
        ),
        ("https://drive.google.com/nonsense", None),
    ],
)
def test_drive_url_rewrite_forms(url: str, expected: str | None) -> None:
    assert drive_url_to_direct_download(url) == expected


def test_matching_jewellery_column_is_not_a_category() -> None:
    """It appears only in the shot-type grid, never in the Angles section, so it
    has no angle data to generate from.
    """
    rows = parse_grid(REAL_SHAPE_GRID)
    assert all("MATCHING" not in r[0] for r in rows)


def test_shot_type_grid_rows_are_ignored() -> None:
    """Female Model / Product styling x Traditional / Modern has no
    representation in v2's category x angle model — it belongs to v1.
    """
    rows = parse_grid(REAL_SHAPE_GRID)
    assert all(r[6] != "shot-type prompt" for r in rows)


def test_declared_but_unauthored_angle_is_emitted_disabled_not_dropped() -> None:
    """A visible gap in the config version beats silently looking like the angle
    was never wanted.
    """
    grid = [row[:] for row in REAL_SHAPE_GRID]
    grid[10] = ["", "", "", "", "", "", "", ""]  # blank out RING's TOP cell
    rows = parse_grid(grid)
    ring_top = [r for r in _rows_for(rows, "RING") if r[3] == "TOP"][0]
    assert ring_top[4] == "FALSE"  # enabled
    assert ring_top[6] == ""  # prompt


def test_missing_angles_row_is_a_sheets_error() -> None:
    grid = [r for r in REAL_SHAPE_GRID if not (r and r[0] == "Angles")]
    with pytest.raises(SheetsUnavailableError, match="Angles"):
        parse_grid(grid)


def test_unrecognised_header_is_a_sheets_error() -> None:
    with pytest.raises(SheetsUnavailableError, match="header"):
        parse_grid([["", "Widgets", "Gadgets"], ["Angles", "front", "front"]])


def test_non_json_cell_falls_back_to_verbatim_prompt() -> None:
    """A human-authored plain-text cell is a content question for the client,
    not a reason to fail the whole sync.
    """
    grid = [row[:] for row in REAL_SHAPE_GRID]
    grid[7] = ["", "", "just plain text https://drive.google.com/file/d/PLAIN1/view"]
    rows = parse_grid(grid)
    necklace_front = [r for r in _rows_for(rows, "NECKLACE") if r[3] == "FRONT"][0]
    assert necklace_front[6] == "just plain text"
    assert necklace_front[8] == "https://drive.google.com/uc?export=download&id=PLAIN1"
