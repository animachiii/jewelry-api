"""feat: seed custom_background_prompt into operations.BACKGROUND_REPLACEMENT

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-13

Custom-background compositing needs its own base instruction, separate from
each curated preset's own prompt (config.global.background_presets[].prompt)
— see docs/superpowers/specs/2026-08-13-custom-background-compositing-design.md.
Same seed-forward pattern as migrations 0005/0007/0010: never mutates the
active row (CLAUDE.md Hard Rule 11), inserts a new version, best-effort
cache invalidation.

**Placeholder content, not a calibrated business decision** — same status as
every other prompt/threshold seeded so far (qa_similarity_threshold,
background_qa_similarity_threshold). Unblocks the feature being built and
tested, not a value to ship to real client traffic without review.
"""

import hashlib
import json
import ssl
from collections.abc import Sequence
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

CUSTOM_BACKGROUND_PROMPT = (
    "Place the product naturally into the supplied background photo, as if it "
    "were physically photographed in that scene. Match the background's light "
    "direction, color temperature, and perspective. Render realistic contact "
    "shadows and any relevant surface reflections. Do not composite a visibly "
    "pasted cutout — the result must read as one photograph, not two images "
    "layered together."
)


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
    replacement = dict(operations.get("BACKGROUND_REPLACEMENT", {}))
    if "custom_background_prompt" in replacement:
        return  # already extended

    replacement["custom_background_prompt"] = CUSTOM_BACKGROUND_PROMPT
    operations["BACKGROUND_REPLACEMENT"] = replacement
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
    replacement = payload.get("global", {}).get("operations", {}).get("BACKGROUND_REPLACEMENT", {})
    if replacement.get("custom_background_prompt") != CUSTOM_BACKGROUND_PROMPT:
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
