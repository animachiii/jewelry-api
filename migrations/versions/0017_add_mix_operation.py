"""add MIX operation, sub_jobs.secondary_input_asset_id/secondary_mask_asset_id

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-16

phases/phase-20-mix.md Step 1: MIX is the first operation needing two
independent source images and two independent masks on the same sub-job.
This migration adds `MIX` to `operation_t` and two new `sub_jobs` columns to
carry the second source photo and its mask.

Same "one migration, not two" reasoning `0015`'s own docstring already
established for `operation_t`: on Postgres 12+, `ALTER TYPE ... ADD VALUE`
can run inside a transaction as long as the newly added value is never
compared or inserted as data within that same transaction. This migration
adds one enum value and does plain column DDL alongside it, and never uses
the new value as data — so combining everything into one migration is safe
here too.

Unlike migration `0015` (which added `asset_kind_t.MASK`), **this migration
adds no new `asset_kind_t` value at all** — `secondary_input_asset_id`
reuses `AssetKind.INPUT` (same precedent `sub_jobs.background_asset_id`,
migration 0011, already established for a second uploaded photo) and
`secondary_mask_asset_id` reuses `AssetKind.MASK` (added by 0015). Both are
ordinary assets of an existing kind, distinguished only by which FK column
on `sub_jobs` points at them.

Unlike migration `0013` (Phase 18), **this migration does not touch
`ux_sub_jobs_job_single` or add a new partial index.** A MIX job always has
exactly one sub-job — the same shape RECOLOR and background operations use,
not MATCH's 1-4 — so the existing `angle IS NULL AND variant_index IS NULL`
partial-unique index already covers it without modification. Do not assume
every new operation needs an index change just because MATCH did.

Schema changes, all in this one migration:

1. `ALTER TYPE operation_t ADD VALUE IF NOT EXISTS 'MIX'`.
2. `sub_jobs.secondary_input_asset_id UUID NULL` FK -> `assets.id`,
   `use_alter=True` (same circular-FK pattern every other
   `sub_jobs.*_asset_id` column already uses).
3. `sub_jobs.secondary_mask_asset_id UUID NULL` FK -> `assets.id`, same
   pattern.

`downgrade()` reverses the two new columns but, matching `0013`'s/`0015`'s
own downgrades and this project's documented "enum values are never
removed" convention, deliberately does NOT attempt to remove `'MIX'` from
`operation_t`. Postgres has no `ALTER TYPE ... DROP VALUE`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE operation_t ADD VALUE IF NOT EXISTS 'MIX'")

    op.add_column(
        "sub_jobs",
        sa.Column("secondary_input_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "sub_jobs",
        sa.Column("secondary_mask_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_sub_jobs_secondary_input_asset_id",
        "sub_jobs",
        "assets",
        ["secondary_input_asset_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_sub_jobs_secondary_mask_asset_id",
        "sub_jobs",
        "assets",
        ["secondary_mask_asset_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_sub_jobs_secondary_mask_asset_id", "sub_jobs", type_="foreignkey")
    op.drop_constraint("fk_sub_jobs_secondary_input_asset_id", "sub_jobs", type_="foreignkey")
    op.drop_column("sub_jobs", "secondary_mask_asset_id")
    op.drop_column("sub_jobs", "secondary_input_asset_id")

    # Deliberately no attempt to remove 'MIX' from operation_t — see the
    # module docstring above.
