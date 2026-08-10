"""Google Sheets client — the only place that imports `googleapiclient`/`google-auth`
for config authoring. See docs/conventions.md: "app/providers/ is the only place that
imports a model SDK" — Sheets isn't a model, but the same isolation reasoning applies:
`app/services/config_sync_service.py` never imports this SDK directly, so tests can
substitute a fixture function instead of hitting a live spreadsheet (Hard Rule 5 in
CLAUDE.md: "Never call the live Gemini or Sheets API in tests").

**Real sheet layout (confirmed live 2026-08-10 against the client's actual
spreadsheet, resolving roadmap open decision #2).** The previous version of this
module assumed a flat `Angles` tab with one row per category/angle pair and a
separate `Global` key/value tab. Neither exists. The real sheet is a single tab
(`Sheet1`) holding a hand-authored pivot grid, and this module's job is to
transform it into the flat row shape `config_sync_service.normalize_sheet_rows`
already expects — exactly the "revisit the layout without touching the
normalization contract" seam the previous docstring anticipated.

Actual structure::

    row 0    (header)  | Anklets | Necklace | Earrings | Bangles | Bracelets
                       | Hipbelt | Ring | Matching jewellery
    rows 1-30          the V1-era shot-type grid (Female Model / Male Model /
                       Mannequin / Product styling x Traditional / Modern), each
                       cell a free-text prompt + Drive link. **Not read here** —
                       that axis has no representation in v2's data model, which
                       is category x angle only (docs/schema.md). It belongs to
                       v1 (`jewellery-gen-backend`), which still parses it.
    row 34   "Angles"  | per jewelry-type column, the comma-separated angle order
                       e.g. Ring -> "front, side, diagonal, top"
    rows 35+           one row per angle, positionally matching the declared order
                       above. Each cell is a JSON object plus one trailing Drive
                       reference-image URL.

Each angle cell's JSON carries `camera_angle`, `photography_summary`, and
`production_prompt`. **Only `production_prompt` becomes the `prompt`** — verified
against the real content: it is self-contained, already describing lens/framing,
key light, shadow direction, depth of field, and white balance. The other two
keys are the authoring notes it was distilled from; sending them too would
duplicate and potentially contradict it.

`Matching jewellery` (header column I) is deliberately **not** a v2 category: it
appears only in the shot-type grid above, never in the Angles section, and has no
angle data to generate from.

**Reference image URLs** are authored as Drive *share* links (`/file/d/<id>/view`),
which serve an HTML interstitial rather than image bytes. They are rewritten to
`uc?export=download&id=<id>` so `generation_service`'s fetch gets real bytes — a
share link would fail the reference fetch (observed as `reference_image_fetch_failed`).

**Global config** (`model_version`, `qa_similarity_threshold`, `unit_cost_usd`,
`default_negative_prompt`) has no home in this sheet at all. `fetch_sheet_rows`
returns no global rows, and `config_sync_service` inherits the whole `global`
block from the currently-active config version — see its `normalize_sheet_rows`
docstring for why that matters (it is what keeps `unit_cost_usd` alive).
"""

import json
import re
from typing import NamedTuple

from app.config import settings

# Sheet header label -> v2 `category_code`. Singular + uppercase per
# docs/conventions.md's enum-value convention; matches v1's own JewelryType
# codes so the two systems name the same product the same way.
HEADER_TO_CATEGORY: dict[str, tuple[str, str]] = {
    "anklets": ("ANKLET", "Anklets"),
    "necklace": ("NECKLACE", "Necklaces"),
    "earrings": ("EARRING", "Earrings"),
    "bangles": ("BANGLE", "Bangles"),
    "bracelets": ("BRACELET", "Bracelets"),
    "hipbelt": ("HIPBELT", "Hipbelts"),
    "ring": ("RING", "Rings"),
}

_ANGLES_LABEL = "angles"
_DRIVE_URL_RE = re.compile(r"https://drive\.google\.com/\S+")
_DRIVE_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)|[?&]id=([A-Za-z0-9_-]+)")


class SheetsUnavailableError(Exception):
    """Sheets could not be reached or is not configured.

    Callers (see `app/services/config_sync_service.py`) must treat this as a
    fallback trigger, never a hard failure — docs/business-rules.md §9: "A
    Sheets outage must never fail a job."
    """


class SheetRows(NamedTuple):
    angle_rows: list[list[str]]
    global_rows: list[list[str]]


def drive_url_to_direct_download(url: str) -> str | None:
    """Rewrite a Drive share link to a direct-download link, or None if no id."""
    match = _DRIVE_ID_RE.search(url)
    if not match:
        return None
    file_id = match.group(1) or match.group(2)
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def parse_grid(grid: list[list[str]]) -> list[list[str]]:
    """Pivot grid -> the flat 9-column angle rows `normalize_sheet_rows` expects.

    Pure and SDK-free so it can be tested against a recorded copy of the real
    sheet without any network access.
    """
    if not grid:
        raise SheetsUnavailableError("Sheet is empty")

    def cell(row_index: int, col: int) -> str:
        if row_index >= len(grid):
            return ""
        row = grid[row_index]
        return row[col] if col < len(row) else ""

    header = grid[0]
    columns: dict[int, tuple[str, str]] = {}
    for col, label in enumerate(header):
        key = label.strip().lower()
        if key in HEADER_TO_CATEGORY:
            columns[col] = HEADER_TO_CATEGORY[key]
    if not columns:
        raise SheetsUnavailableError(f"No known jewelry-type columns in header row: {header!r}")

    angles_row = next(
        (i for i, row in enumerate(grid) if row and row[0].strip().lower() == _ANGLES_LABEL),
        None,
    )
    if angles_row is None:
        raise SheetsUnavailableError("No 'Angles' row found — cannot locate per-angle data")

    rows: list[list[str]] = []
    for col, (code, name) in columns.items():
        declared = [a.strip().upper() for a in cell(angles_row, col).split(",") if a.strip()]
        for offset, angle in enumerate(declared):
            raw = cell(angles_row + 1 + offset, col).strip()
            if not raw:
                # Declared but unauthored — emit a disabled row rather than
                # dropping it, so the gap is visible in the config version
                # instead of silently looking like the angle was never wanted.
                rows.append([code, name, "TRUE", angle, "FALSE", "FALSE", "", "", ""])
                continue

            urls = _DRIVE_URL_RE.findall(raw)
            body = _DRIVE_URL_RE.sub("", raw).strip()
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                # Not fatal at the provider layer: emit the cell verbatim as the
                # prompt. normalize_sheet_rows still validates the row shape, and
                # a human-authored non-JSON cell is a content question for the
                # client, not a reason to fail the whole sync.
                parsed = {"production_prompt": body}

            prompt = parsed.get("production_prompt") or ""
            direct = [d for d in (drive_url_to_direct_download(u) for u in urls) if d]

            rows.append(
                [
                    code,
                    name,
                    "TRUE",
                    angle,
                    "TRUE" if prompt else "FALSE",
                    # synthetic_allowed: the sheet does not express this, and
                    # docs/business-rules.md §6 makes synthetic the risky,
                    # QA-gated path. Defaulting to FALSE keeps the safe
                    # behaviour until the client explicitly opts an angle in.
                    "FALSE",
                    prompt,
                    "",
                    ",".join(direct),
                ]
            )

    if not rows:
        raise SheetsUnavailableError("Angles row present but no angle data found")
    return rows


def fetch_sheet_rows() -> SheetRows:
    """Real Sheets fetch. Never called from tests — see module docstring.

    Raises `SheetsUnavailableError` if the service account / sheet ID are not
    configured or if the API call fails for any reason (network, auth, quota).
    """
    if not settings.GOOGLE_SERVICE_ACCOUNT_JSON or not settings.CONFIG_SHEET_ID:
        raise SheetsUnavailableError(
            "Google Sheets not configured: GOOGLE_SERVICE_ACCOUNT_JSON / CONFIG_SHEET_ID empty."
        )

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        raw = settings.GOOGLE_SERVICE_ACCOUNT_JSON
        try:
            info = json.loads(raw)
        except json.JSONDecodeError:
            # v1 stores this base64-encoded; accept either form so the same
            # credential value works in both projects without re-encoding.
            import base64

            info = json.loads(base64.b64decode(raw))

        credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        service = build("sheets", "v4", credentials=credentials)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.CONFIG_SHEET_ID, range="Sheet1!A1:Z400")
            .execute()
        )
        grid: list[list[str]] = result.get("values", [])
        # No global rows: this sheet has no Global tab at all. config_sync_service
        # inherits the active version's `global` block instead.
        return SheetRows(angle_rows=parse_grid(grid), global_rows=[])
    except SheetsUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - any Sheets/auth/network failure is a fallback trigger
        raise SheetsUnavailableError(f"Google Sheets fetch failed: {exc}") from exc
