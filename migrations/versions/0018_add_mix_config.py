"""feat: seed MIX into config_versions.payload.global.operations

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-16

phases/phase-20-mix.md Step 2: MIX needs the same per-operation
`enabled`/`prompt`/`unit_cost_usd` shape `0007`/`0014`/`0016` already seeded
for the other four operations. Unlike `0016`, this migration adds **no new
top-level `global` key** — MIX has no palette, no preset list, and no other
config substructure of its own. This is the smallest config addition of any
v3 phase.

Same merge-not-replace reasoning `0014`'s/`0016`'s own docstrings
established for being the *second*/*third* migration to touch
`payload.global.operations`: this is the *fourth*, so it reads whatever is
already in `operations` (which may already include
BACKGROUND_REMOVAL/BACKGROUND_REPLACEMENT/MATCH/RECOLOR) and merges `MIX`
in alongside it, rather than replacing the whole block.

Per CLAUDE.md Hard Rule 11, this inserts a new config_versions row rather
than mutating the active row's payload — mirrors `0007`/`0014`/`0016`
exactly, including the no-op guards (no active row, or `MIX` already
present) and best-effort Redis `config:active` cache invalidation.

**Placeholder content, not a real business decision:** the prompt wording
and the `0.02` unit cost are both placeholders — nobody has confirmed real
prompt wording or real per-operation pricing yet. Same uncalibrated status
as every other seeded prompt/cost in this project.

**No runtime template placeholder in this prompt** — unlike
`resolve_match_prompt`'s `{target_category}` or `resolve_recolor_prompt`'s
`{palette_prompt}`, MIX's prompt is a complete, final string at rest; there
is nothing per-request to substitute into it (the seam-band overlay itself
carries the visual information, not a text parameter).
"""

import hashlib
import json
import ssl
from collections.abc import Sequence
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

NEW_OPERATIONS = {
    "MIX": {
        "enabled": True,
        "prompt": (
            "This image shows two jewelry pieces that have already been "
            "merged: one piece's element has been placed into the other. "
            "Blend only the seam marked in solid magenta so the graft looks "
            "like a single, naturally manufactured piece — smooth the "
            "transition in metal, lighting, and shadow exactly at that "
            "boundary. Do not alter anything else in the image, and do not "
            "move, resize, or reshape either piece."
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
    if "MIX" in operations:
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
    if global_block.get("operations", {}).get("MIX") != NEW_OPERATIONS["MIX"]:
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
