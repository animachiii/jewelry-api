"""feat: seed GENERATE_WITH_CLEANUP into config_versions.payload.global.operations

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-31

GENERATE_WITH_CLEANUP needs the same per-operation enabled/prompt/unit_cost_usd
shape 0007/0014/0016/0018 already seeded for the other operations that use
it. This is the fifth migration to touch payload.global.operations, so it
reads whatever is already there and merges GENERATE_WITH_CLEANUP in — same
merge-not-replace reasoning every prior operations-touching migration's own
docstring already established.

The prompt text is BACKGROUND_REMOVAL's own prompt, copied verbatim — a
deliberate decision, not a placeholder oversight: the cleanup step performs
the exact same transformation standalone background removal does, just as
an internal pipeline stage rather than a client deliverable. See
docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md section 9.

Same uncalibrated-placeholder cost status as every other seeded operation
in this project.
"""

import hashlib
import json
import ssl
from collections.abc import Sequence
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

NEW_OPERATIONS = {
    "GENERATE_WITH_CLEANUP": {
        "enabled": True,
        # Copied verbatim from operations.BACKGROUND_REMOVAL.prompt as
        # superseded by migration 0019 (NOT 0007's original, which is
        # stale) -- see this migration's own docstring for why.
        "prompt": (
            "Isolate only the jewellery product and remove everything else from the "
            "frame — hands, fingers, mannequins, models, price tags, hangtags, "
            "stickers, packaging, props, and any other object. Replace the "
            "background with a clean, seamless pure white (#FFFFFF) studio backdrop. "
            "Keep the jewellery itself — its proportions, materials, textures, and "
            "every detail — exactly unchanged. Do not redesign, embellish, or "
            "invent any part of the product. The final image must contain only the "
            "jewellery product on a plain white background, ready to use directly "
            "as an e-commerce product photo."
        ),
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
    if "GENERATE_WITH_CLEANUP" in operations:
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
    if (
        global_block.get("operations", {}).get("GENERATE_WITH_CLEANUP")
        != NEW_OPERATIONS["GENERATE_WITH_CLEANUP"]
    ):
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
