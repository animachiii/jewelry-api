"""Google Sheets client — the only place that imports `googleapiclient`/`google-auth`
for config authoring. See docs/conventions.md: "app/providers/ is the only place that
imports a model SDK" — Sheets isn't a model, but the same isolation reasoning applies:
`app/services/config_sync_service.py` never imports this SDK directly, so tests can
substitute a fixture function instead of hitting a live spreadsheet (Hard Rule 5 in
CLAUDE.md: "Never call the live Gemini or Sheets API in tests").

**Sheet layout convention (assumed — roadmap open decision #2, "the 7 exact category
codes and per-category angle enablement," is still open):** one row per
category/angle pair on a tab named `Angles`, columns:

    category_code | category_name | category_is_active | angle | enabled
    | synthetic_allowed | prompt | negative_prompt | reference_image_urls

`reference_image_urls` is a comma-separated list. A second tab, `Global`, holds
`model_version` / `qa_similarity_threshold` / `default_negative_prompt` as
key/value rows. This is a reasonable, realistic tabular convention chosen so the
sync pipeline (fetch -> normalize -> hash -> version -> activate) is fully real and
testable now; the exact column layout is expected to be revisited once the client's
real sheet is seen, without touching `config_sync_service.py`'s normalization
contract (`SheetRows -> payload dict`).
"""

import json
from typing import NamedTuple

from app.config import settings


class SheetsUnavailableError(Exception):
    """Sheets could not be reached or is not configured.

    Callers (see `app/services/config_sync_service.py`) must treat this as a
    fallback trigger, never a hard failure — docs/business-rules.md §9: "A
    Sheets outage must never fail a job."
    """


class SheetRows(NamedTuple):
    angle_rows: list[list[str]]
    global_rows: list[list[str]]


def fetch_sheet_rows() -> SheetRows:
    """Real Sheets fetch. Never called from tests — see module docstring.

    Raises `SheetsUnavailableError` if the service account / sheet ID are not
    configured (true in every environment right now — no real Sheets project
    exists yet, see phases/phase-3-config-service.md) or if the API call fails
    for any reason (network, auth, quota).
    """
    if not settings.GOOGLE_SERVICE_ACCOUNT_JSON or not settings.CONFIG_SHEET_ID:
        raise SheetsUnavailableError(
            "Google Sheets not configured: GOOGLE_SERVICE_ACCOUNT_JSON / CONFIG_SHEET_ID empty."
        )

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        info = json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
        credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        service = build("sheets", "v4", credentials=credentials)
        values = service.spreadsheets().values()
        angle_result = values.get(
            spreadsheetId=settings.CONFIG_SHEET_ID, range="Angles!A2:I1000"
        ).execute()
        global_result = values.get(
            spreadsheetId=settings.CONFIG_SHEET_ID, range="Global!A2:B100"
        ).execute()
        return SheetRows(
            angle_rows=angle_result.get("values", []),
            global_rows=global_result.get("values", []),
        )
    except SheetsUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - any Sheets/auth/network failure is a fallback trigger
        raise SheetsUnavailableError(f"Google Sheets fetch failed: {exc}") from exc
