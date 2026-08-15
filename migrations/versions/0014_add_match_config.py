"""feat: seed MATCH into config_versions.payload.global.operations

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-16

phases/phase-18-match.md Step 2: MATCH needs the same per-operation
`enabled`/`prompt`/`unit_cost_usd` shape `0007` already seeded for
BACKGROUND_REMOVAL/BACKGROUND_REPLACEMENT. The real Google Sheet has no
Global tab (app/providers/sheets.py) and never will for this key — it can
only ever be seeded here and carried forward untouched through
config_sync_service.normalize_sheet_rows's `base_global` carry-forward,
same reasoning `0007`'s own docstring gives.

This is the *second* migration to touch `payload.global.operations`, not
the first, so unlike `0007` (which could get away with
`global_block.setdefault("operations", NEW_OPERATIONS)` because no prior
migration had ever populated that key) this one reads whatever is already
in `operations` and merges `MATCH` in alongside it — `setdefault("operations",
NEW_OPERATIONS)` here would be a no-op on any database that already ran
`0007`, silently dropping BACKGROUND_REMOVAL/BACKGROUND_REPLACEMENT's own
config on the new row. Idempotent at the `MATCH`-key level specifically
(re-running this migration against a database that already has `MATCH`
seeded is a no-op), not just at the "operations block exists" level.

Per CLAUDE.md Hard Rule 11, this inserts a new config_versions row rather
than mutating the active row's payload — mirrors `0007` exactly, including
the no-op guards (no active row, or `MATCH` already present) and
best-effort Redis `config:active` cache invalidation.

**Placeholder content, not a real business decision:** the prompt wording
and the `0.02` unit cost are drawn directly from the phase file's own
example payload — nobody has confirmed real prompt wording or real
per-operation pricing yet (phases/phase-roadmap.md open decision #11 is
still open). Treat this the same as `BACKGROUND_REMOVAL`'s own seeded
prompt/cost in `0007`: a value that unblocks Steps 3/4 being built and
tested, not a value to ship to real client traffic without review.

**`{target_category}` is a genuine runtime template placeholder**, not
literal text meant to ship as-is to Gemini. Unlike every other operation's
prompt seeded so far (which are complete, final strings), this one is
resolved per-request by `app/services/job_service.py::resolve_match_prompt`,
which substitutes the actual requested companion-piece category (e.g.
"EARRING") via `str.format(target_category=...)` at prompt-build time,
after the request's `target_category` is known. The unsubstituted template
with the literal `{target_category}` placeholder is what lives in this
migration and in `payload.global.operations.MATCH.prompt` at rest.
"""

import hashlib
import json
import ssl
from collections.abc import Sequence
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

NEW_OPERATIONS = {
    "MATCH": {
        "enabled": True,
        "prompt": (
            "Using the provided jewelry piece as a style reference, design a "
            "matching {target_category} intended to be worn as part of the same "
            "set. Match the metal tone, stone type and cut, and overall design "
            "language exactly. Render as a standalone studio product photograph "
            "on a clean white background, camera angle and lighting consistent "
            "with a real product catalog shot. Do not reproduce the reference "
            "piece itself — generate a new, different item that belongs with it."
        ),
        # placeholder, same uncalibrated status as every other seeded
        # operation cost in this project (see 0007's own docstring) — not
        # reviewed by the client yet
        "unit_cost_usd": 0.02,
    }
}


def upgrade() -> None:
    bind = op.get_bind()

    active = (
        bind.execute(
            sa.text(
                "SELECT id, version_number, payload::text AS payload_text "
                "FROM config_versions WHERE is_active = true"
            )
        )
        .mappings()
        .first()
    )

    if active is None:
        return  # fresh/CI database, no seeded config -- nothing to extend

    payload = json.loads(active["payload_text"])
    global_block = dict(payload.get("global", {}))
    operations = dict(global_block.get("operations", {}))
    if "MATCH" in operations:
        return  # already extended

    operations.update(NEW_OPERATIONS)
    global_block["operations"] = operations
    new_payload = dict(payload)
    new_payload["global"] = global_block

    new_hash = hashlib.sha256(json.dumps(new_payload, sort_keys=True).encode()).hexdigest()

    next_version = bind.execute(
        sa.text("SELECT COALESCE(MAX(version_number), 0) + 1 FROM config_versions")
    ).scalar_one()

    bind.execute(
        sa.text("UPDATE config_versions SET is_active = false WHERE id = :id"),
        {"id": active["id"]},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO config_versions
                (id, version_number, source_hash, payload, sync_status,
                 is_active, synced_at, activated_at)
            VALUES (gen_random_uuid(), :version_number, :source_hash,
                    CAST(:payload AS jsonb), 'SUCCESS', true, now(), now())
            """
        ),
        {
            "version_number": next_version,
            "source_hash": new_hash,
            "payload": json.dumps(new_payload),
        },
    )

    _invalidate_config_cache()


def downgrade() -> None:
    bind = op.get_bind()

    active = (
        bind.execute(
            sa.text(
                "SELECT id, version_number, payload::text AS payload_text "
                "FROM config_versions WHERE is_active = true"
            )
        )
        .mappings()
        .first()
    )

    if active is None:
        return

    payload = json.loads(active["payload_text"])
    global_block = payload.get("global", {})
    if global_block.get("operations", {}).get("MATCH") != NEW_OPERATIONS["MATCH"]:
        return  # active row wasn't the one this migration activated

    previous = (
        bind.execute(
            sa.text("SELECT id FROM config_versions WHERE version_number = :v"),
            {"v": active["version_number"] - 1},
        )
        .mappings()
        .first()
    )

    if previous is None:
        return

    bind.execute(
        sa.text("UPDATE config_versions SET is_active = false WHERE id = :id"),
        {"id": active["id"]},
    )
    bind.execute(
        sa.text("UPDATE config_versions SET is_active = true WHERE id = :id"),
        {"id": previous["id"]},
    )

    _invalidate_config_cache()


def _invalidate_config_cache() -> None:
    """Best-effort: a stale cache self-heals within the 15 min TTL
    (app/services/config_service.py), so a Redis hiccup here must not fail
    the migration step of a deploy."""
    try:
        import redis

        ssl_kwargs = (
            {"ssl_cert_reqs": ssl.CERT_REQUIRED}
            if urlparse(settings.REDIS_URL).scheme == "rediss"
            else {}
        )
        client = redis.from_url(settings.REDIS_URL, **ssl_kwargs)
        try:
            client.delete("config:active")
        finally:
            client.close()
    except Exception:  # noqa: BLE001 - never fail a migration over cache invalidation
        pass
