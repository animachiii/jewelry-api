"""Asset retention policy — docs/business-rules.md §11.

Single source of truth for how long each `AssetKind` lives before storage
lifecycle removes its bytes (the row itself is never deleted — Hard Rule 10
in CLAUDE.md). `OUTPUT` retention is `None` (indefinite) because the client
retention policy is still an open decision — see
phases/phase-roadmap.md "Open decisions" #5. When that policy lands, only
this dict needs to change; nothing else in the codebase hardcodes a number.
"""

from datetime import UTC, datetime, timedelta

from app.db.models.enums import AssetKind

RETENTION_DAYS: dict[AssetKind, int | None] = {
    AssetKind.INPUT: 90,
    AssetKind.MATTE: 30,
    AssetKind.OUTPUT: None,  # indefinite, pending client policy
}


def compute_expires_at(kind: AssetKind, now: datetime | None = None) -> datetime | None:
    """Returns the `expires_at` value to store on a new Asset row of this
    kind, or None for indefinite retention."""
    days = RETENTION_DAYS.get(kind)
    if days is None:
        return None
    base = now if now is not None else datetime.now(UTC)
    return base + timedelta(days=days)
