"""Asset retention policy — docs/business-rules.md §11.

Single source of truth for how long each `AssetKind` lives before storage
lifecycle removes its bytes (the row itself is never deleted — Hard Rule 10
in CLAUDE.md). When the client sets a real policy, only this dict needs to
change; nothing else in the codebase hardcodes a number.

`OUTPUT` was `None` (indefinite) pending the client's decision — see
phases/phase-roadmap.md "Open decisions" #5. Phase 16 Step 4 defaults it to
180 days instead of leaving it unbounded: a live storage audit
(docs/storage-audit-2026-08.md) found this Supabase project at 484MB of its
500MB free-tier ceiling, and real-photo/production `OUTPUT` assets are the
one category of stored bytes with no expiry mechanism at all otherwise.
This is a default, not a resolution of the open decision — the client can
change it to any value at any time, including back to indefinite.
"""

from datetime import UTC, datetime, timedelta

from app.db.models.enums import AssetKind

RETENTION_DAYS: dict[AssetKind, int | None] = {
    AssetKind.INPUT: 90,
    AssetKind.MATTE: 30,
    AssetKind.OUTPUT: 180,  # defaulted 2026-08-15 (Phase 16), not resolved — see module docstring
    # A client-drawn artifact, not a byproduct the system could recreate
    # (unlike MATTE, which is regenerable from input) — but it also has no
    # purpose once its one RECOLOR job is terminal, so it doesn't need
    # INPUT's 90-day retry-window justification either. See migration 0015
    # and phases/phase-19-recolor.md Step 5.
    AssetKind.MASK: 7,
}


def compute_expires_at(kind: AssetKind, now: datetime | None = None) -> datetime | None:
    """Returns the `expires_at` value to store on a new Asset row of this
    kind, or None for indefinite retention."""
    days = RETENTION_DAYS.get(kind)
    if days is None:
        return None
    base = now if now is not None else datetime.now(UTC)
    return base + timedelta(days=days)
