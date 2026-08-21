"""feat: rewrite BACKGROUND_REMOVAL/STUDIO_WHITE prompts for pure product photos

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-18

The prompts seeded by 0007 only ever instructed Gemini to keep "the product
subject... exactly unchanged" and swap the backdrop for white. Neither told
it to strip out anything else in frame — a hand, a mannequin, a price tag,
packaging — so a hand-modeled or tagged source photo came back with the
hand/tag still in it. Client requirement: output must be usable directly as
an e-commerce product photo — jewellery only, pure white background, no
hands/tags/props.

Rewrites two prompt strings in payload.global:
- operations.BACKGROUND_REMOVAL.prompt
- background_presets[code=STUDIO_WHITE].prompt

Same seed-forward pattern as 0005/0007/0012/etc. (CLAUDE.md Hard Rule 11):
never mutates the active row, inserts a new version, best-effort cache
invalidation. Idempotent: no-ops if the active row's prompts already match
the new text (e.g. a re-run after this migration already applied).

**Still placeholder-quality in the sense that 0007 flagged** — real prompt
wording tuned against actual test images should replace this if results
aren't good enough, but this specific change (isolate-only-the-jewellery
instruction) is a direct, confirmed client requirement, not a guess.
"""

import hashlib
import json
import ssl
from collections.abc import Sequence
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

OLD_REMOVAL_PROMPT = (
    "Replace the background with a clean, seamless white studio backdrop. "
    "Keep the product subject — its proportions, materials, textures, and "
    "every detail — exactly unchanged. Do not redesign, embellish, or "
    "invent any part of the product."
)
NEW_REMOVAL_PROMPT = (
    "Isolate only the jewellery product and remove everything else from the "
    "frame — hands, fingers, mannequins, models, price tags, hangtags, "
    "stickers, packaging, props, and any other object. Replace the "
    "background with a clean, seamless pure white (#FFFFFF) studio backdrop. "
    "Keep the jewellery itself — its proportions, materials, textures, and "
    "every detail — exactly unchanged. Do not redesign, embellish, or "
    "invent any part of the product. The final image must contain only the "
    "jewellery product on a plain white background, ready to use directly "
    "as an e-commerce product photo."
)

OLD_STUDIO_WHITE_PROMPT = (
    "Place the product on a clean, seamless white studio backdrop with soft, "
    "even lighting and a subtle natural drop shadow."
)
NEW_STUDIO_WHITE_PROMPT = (
    "Isolate only the jewellery product and remove everything else — hands, "
    "fingers, mannequins, models, price tags, hangtags, stickers, packaging, "
    "and props. Place the jewellery on a clean, seamless pure white "
    "(#FFFFFF) studio backdrop with soft, even lighting and a subtle "
    "natural drop shadow. The final image must contain only the jewellery "
    "product on a plain white background, ready to use directly as an "
    "e-commerce product photo."
)


def _load_active(bind: sa.engine.Connection) -> sa.engine.Row | None:
    return (
        bind.execute(
            sa.text(
                "SELECT id, version_number, payload::text AS payload_text "
                "FROM config_versions WHERE is_active = true"
            )
        )
        .mappings()
        .first()
    )


def upgrade() -> None:
    bind = op.get_bind()

    active = _load_active(bind)
    if active is None:
        return  # fresh/CI database, no seeded config -- nothing to extend

    payload = json.loads(active["payload_text"])
    global_block = dict(payload.get("global", {}))
    operations = dict(global_block.get("operations", {}))
    removal = dict(operations.get("BACKGROUND_REMOVAL", {}))
    presets = [dict(p) for p in global_block.get("background_presets", [])]

    already_updated = removal.get("prompt") == NEW_REMOVAL_PROMPT and all(
        p.get("prompt") == NEW_STUDIO_WHITE_PROMPT if p.get("code") == "STUDIO_WHITE" else True
        for p in presets
    )
    if already_updated:
        return

    if "prompt" in removal:
        removal["prompt"] = NEW_REMOVAL_PROMPT
        operations["BACKGROUND_REMOVAL"] = removal
    for preset in presets:
        if preset.get("code") == "STUDIO_WHITE":
            preset["prompt"] = NEW_STUDIO_WHITE_PROMPT

    global_block["operations"] = operations
    global_block["background_presets"] = presets
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

    active = _load_active(bind)
    if active is None:
        return

    payload = json.loads(active["payload_text"])
    global_block = payload.get("global", {})
    removal_prompt = global_block.get("operations", {}).get("BACKGROUND_REMOVAL", {}).get("prompt")
    if removal_prompt != NEW_REMOVAL_PROMPT:
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
