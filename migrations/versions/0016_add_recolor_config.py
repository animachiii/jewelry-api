"""feat: seed RECOLOR + palette into config_versions.payload.global

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-16

phases/phase-19-recolor.md Step 2: RECOLOR needs the same per-operation
`enabled`/`prompt`/`unit_cost_usd` shape `0007`/`0014` already seeded for
the other three operations, plus a genuinely new top-level `global.palette`
list — a fixed set of client-selectable colors, since raw hex input is
deliberately not supported (produces flat, unrealistic fills and gives the
client an unbounded surface to reject; see this phase's own reality-check
section).

Same merge-not-replace reasoning `0014`'s own docstring established for
being the *second* migration to touch `payload.global.operations`: this is
the *third*, so it reads whatever is already in `operations` (which may
already include BACKGROUND_REMOVAL/BACKGROUND_REPLACEMENT/MATCH) and merges
`RECOLOR` in alongside it, rather than replacing the whole block.
`global.palette` is a brand-new key with no prior migration to collide
with, so it's set directly — same as `0007` could get away with
`setdefault` for `operations` the first time.

Per CLAUDE.md Hard Rule 11, this inserts a new config_versions row rather
than mutating the active row's payload — mirrors `0007`/`0014` exactly,
including the no-op guards (no active row, or `RECOLOR` already present)
and best-effort Redis `config:active` cache invalidation.

**Placeholder content, not a real business decision:** the prompt wording,
the palette entries, and the `0.02` unit cost are all placeholders — nobody
has confirmed real prompt wording, a real client-approved palette, or real
per-operation pricing yet. Treat this the same as every other seeded
prompt/cost in this project (see `0007`'s own docstring): a value that
unblocks Steps 3/4 being built and tested, not a value to ship to real
client traffic without review.

**`{palette_prompt}` is a genuine runtime template placeholder**, resolved
per-request by `app/services/job_service.py::resolve_recolor_prompt`, which
substitutes the requested palette entry's `prompt_phrase` via
`str.format(palette_prompt=...)` at prompt-build time — same mechanism
`resolve_match_prompt` already established for `{target_category}`.
"""

import hashlib
import json
import ssl
from collections.abc import Sequence
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

NEW_OPERATIONS = {
    "RECOLOR": {
        "enabled": True,
        "prompt": (
            "Recolor only the gemstone inside the region marked in solid magenta to "
            "{palette_prompt}. Do not alter any metal, prong, setting, or any part of "
            "the image outside the marked region. Preserve lighting, reflections, "
            "shadows, and composition exactly as they are."
        ),
        # placeholder, same uncalibrated status as every other seeded
        # operation cost in this project (see 0007's own docstring) — not
        # reviewed by the client yet
        "unit_cost_usd": 0.02,
    }
}

PALETTE = [
    {
        "code": "EMERALD_GREEN",
        "label": "Emerald",
        "prompt_phrase": "a deep emerald green emerald",
        "is_active": True,
    },
    {
        "code": "RUBY_RED",
        "label": "Ruby",
        "prompt_phrase": "a rich pigeon-blood red ruby",
        "is_active": True,
    },
    {
        "code": "SAPPHIRE_BLUE",
        "label": "Sapphire",
        "prompt_phrase": "a vivid cornflower blue sapphire",
        "is_active": True,
    },
    {
        "code": "AMETHYST_PURPLE",
        "label": "Amethyst",
        "prompt_phrase": "a rich violet-purple amethyst",
        "is_active": True,
    },
    {
        "code": "CITRINE_YELLOW",
        "label": "Citrine",
        "prompt_phrase": "a warm golden-yellow citrine",
        "is_active": True,
    },
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
    operations = dict(global_block.get("operations", {}))
    if "RECOLOR" in operations:
        return  # already extended

    operations.update(NEW_OPERATIONS)
    global_block["operations"] = operations
    global_block["palette"] = PALETTE
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
    if global_block.get("operations", {}).get("RECOLOR") != NEW_OPERATIONS["RECOLOR"]:
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
