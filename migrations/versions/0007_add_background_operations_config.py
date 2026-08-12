"""feat: seed operations and background_presets into config_versions.payload.global

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-12

phases/phase-15-background-operations.md Step 3: BACKGROUND_REMOVAL and
BACKGROUND_REPLACEMENT need a per-operation enabled flag, prompt, and cost,
plus a curated backdrop preset list for replacement. The real Google Sheet
has no Global tab (app/providers/sheets.py) — these keys can only ever be
seeded here and inherited forward through config_sync_service.normalize_sheet_rows's
`base_global` carry-forward, exactly like `unit_cost_usd` (Phase 6/13) and
`model_version` (migration 0005) already are. They live inside `global`,
not as top-level payload keys, for that same reason: normalize_sheet_rows
rebuilds `categories` from scratch every sync but only ever carries `global`
forward untouched.

Per CLAUDE.md Hard Rule 11, this inserts a new version rather than mutating
the active row's payload — mirrors 0005 exactly, including the no-op guards
(no active row, or the keys are already present) and best-effort cache
invalidation.

**Placeholder content, not a real business decision:** the prompts, costs,
and the single seeded preset (`STUDIO_WHITE`) are drawn directly from the
phase file's own example payload — nobody has confirmed real prompt
wording, real per-operation pricing, or the client's actual preset list yet
(phases/phase-roadmap.md open decision #11 is still open). Treat these the
same as `qa_similarity_threshold`'s `0.82` — a value that unblocks Steps 4/5
being built and tested, not a value to ship to real client traffic without
review.
"""

import hashlib
import json
import ssl
from collections.abc import Sequence
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

NEW_OPERATIONS = {
    "BACKGROUND_REMOVAL": {
        "enabled": True,
        "prompt": (
            "Replace the background with a clean, seamless white studio backdrop. "
            "Keep the product subject — its proportions, materials, textures, and "
            "every detail — exactly unchanged. Do not redesign, embellish, or "
            "invent any part of the product."
        ),
        "unit_cost_usd": 0.02,
    },
    "BACKGROUND_REPLACEMENT": {
        "enabled": True,
        "unit_cost_usd": 0.02,
    },
}
NEW_BACKGROUND_PRESETS = [
    {
        "code": "STUDIO_WHITE",
        "name": "Studio White",
        "prompt": (
            "Place the product on a clean, seamless white studio backdrop with soft, "
            "even lighting and a subtle natural drop shadow."
        ),
        "reference_image_urls": [],
        "is_active": True,
    }
]


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
    if "operations" in global_block and "background_presets" in global_block:
        return  # already extended

    global_block.setdefault("operations", NEW_OPERATIONS)
    global_block.setdefault("background_presets", NEW_BACKGROUND_PRESETS)
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
    if global_block.get("operations") != NEW_OPERATIONS:
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
