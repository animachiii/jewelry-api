"""feat: replace MIX's prompt for the generative two-image rewrite

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-31

MIX stopped being a deterministic graft on 2026-08-31 — see
`app/services/mix_service.py`'s module docstring and `docs/business-rules.md`
§16 for the full accounting of why. The old prompt described a job that no
longer exists: it told Gemini it was looking at *one* image of two already-
merged pieces and asked it to blend "the seam marked in solid magenta"
without moving or reshaping anything. There is no seam and no rough composite
any more, and there are now *two* images rather than one.

**This migration replaces MIX's prompt rather than merging a new key in** —
the first of the five `operations`-touching migrations
(`0007`/`0014`/`0016`/`0018`) to overwrite rather than add. `0018`'s
merge-not-replace guard exists so a database that already ran `0007` doesn't
lose the background operations' config; that reasoning is about not clobbering
*sibling* keys, and it still holds here. Only `operations.MIX.prompt` is
rewritten; `enabled` and `unit_cost_usd` are carried through untouched, and
every other operation's block is left exactly as found.

The prompt names both marker colours explicitly, because
`mix_service._build_highlight` is what puts them there and the two must agree:
magenta marks the **primary** photo's element, cyan the **secondary**'s, and
`mix_service.process` passes the reference images in that order. Changing
either side without the other silently breaks the pairing — the call still
succeeds, it just describes the wrong image.

Two clauses in the prompt are defensive rather than descriptive, and both are
there for a specific known failure mode rather than as generic prompt padding:
telling the model the markings are annotations, because otherwise a magenta
tint is plausibly a design instruction ("make this part pink"); and forbidding
a collage, because a two-image prompt is a standard way to elicit a
side-by-side comparison instead of a synthesis.

Per CLAUDE.md Hard Rule 11 this inserts a new `config_versions` row rather
than mutating the active one, same as every prior config migration, with the
same no-op guards and best-effort Redis `config:active` invalidation.

**Placeholder content, not a client-reviewed decision** — same uncalibrated
status as every other seeded prompt in this project. Unlike its predecessors,
though, this one *can* be validated against a real Gemini call: a real
`GEMINI_API_KEY` now exists, and job `f9768456`'s four stored input assets are
the exact case that motivated the rewrite.
"""

import hashlib
import json
import ssl
from collections.abc import Sequence
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

NEW_MIX_PROMPT = (
    "You are given two photographs of jewelry.\n\n"
    "In the FIRST image, one element is marked with a magenta tint and a "
    "magenta outline. In the SECOND image, one element is marked with a cyan "
    "tint and a cyan outline.\n\n"
    "Design a single new piece of jewelry that combines these two marked "
    "elements into one coherent, wearable design. Keep the metal tone, the "
    "stone types and cuts, and the craftsmanship style of each marked element "
    "clearly recognisable in the result.\n\n"
    "Render the result as a clean, professional studio product photograph of "
    "that single new piece: plain uncluttered background, even lighting, a "
    "soft natural shadow, the whole piece in frame and in focus.\n\n"
    "The magenta and cyan markings are annotations identifying which elements "
    "to use. They are not part of the design — do not reproduce any magenta or "
    "cyan tint, outline, or colour cast anywhere in the output. Do not return "
    "a collage, a side-by-side comparison, or either original photograph."
)

# The prompt this migration expects to find and replace. Guarding on the exact
# old value (rather than just "MIX exists") means this is a no-op on a database
# whose MIX prompt has already been changed by hand or by a later migration,
# instead of silently reverting someone else's edit.
OLD_MIX_PROMPT = (
    "This image shows two jewelry pieces that have already been "
    "merged: one piece's element has been placed into the other. "
    "Blend only the seam marked in solid magenta so the graft looks "
    "like a single, naturally manufactured piece — smooth the "
    "transition in metal, lighting, and shadow exactly at that "
    "boundary. Do not alter anything else in the image, and do not "
    "move, resize, or reshape either piece."
)


def upgrade() -> None:
    _swap_mix_prompt(expected=OLD_MIX_PROMPT, replacement=NEW_MIX_PROMPT)


def downgrade() -> None:
    _swap_mix_prompt(expected=NEW_MIX_PROMPT, replacement=OLD_MIX_PROMPT)


def _swap_mix_prompt(expected: str, replacement: str) -> None:
    """Activates a new config version whose `operations.MIX.prompt` is
    `replacement`, but only if the currently active one is `expected`.

    Written as one function used by both directions rather than two near-
    identical ones: unlike `0018`, whose downgrade reactivates the previous
    row, this migration changes a value in place, so rolling it back is
    genuinely the same operation with the two strings swapped. Reactivating
    the previous row would be wrong here — that row may differ from the
    current one in ways later migrations introduced.
    """
    bind = op.get_bind()

    active = (
        bind.execute(
            sa.text(
                "SELECT id, payload::text AS payload_text "
                "FROM config_versions WHERE is_active = true"
            )
        )
        .mappings()
        .first()
    )

    if active is None:
        return  # fresh/CI database, no seeded config -- nothing to rewrite

    payload = json.loads(active["payload_text"])
    global_block = dict(payload.get("global", {}))
    operations = dict(global_block.get("operations", {}))
    mix = operations.get("MIX")

    if not isinstance(mix, dict) or mix.get("prompt") != expected:
        return  # not the state this migration knows how to change

    # enabled/unit_cost_usd carried through untouched -- only the prompt moves.
    operations["MIX"] = {**mix, "prompt": replacement}
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
