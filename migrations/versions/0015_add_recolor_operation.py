"""add RECOLOR operation, MASK asset kind, sub_jobs.mask_asset_id/palette_code

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-16

phases/phase-19-recolor.md Step 1: RECOLOR is the first operation needing a
second uploaded file (a mask) alongside its source photo. This migration
adds `RECOLOR` to `operation_t`, `MASK` to `asset_kind_t`, and two new
`sub_jobs` columns to carry the mask reference and the requested palette
code.

Same "one migration, not two" reasoning `0013`'s own docstring already
established for `operation_t`: on Postgres 12+, `ALTER TYPE ... ADD VALUE`
can run inside a transaction as long as the newly added value is never
compared or inserted as data within that same transaction. This migration
adds two enum values and does plain column DDL alongside them, and never
uses either new value as data — so combining everything into one migration
is safe here too, on the same Postgres 15 this project runs
(tests/conftest.py's `PostgresContainer("postgres:15-alpine", ...)`).

Unlike migration `0013` (Phase 18), **this migration does not touch
`ux_sub_jobs_job_single` or add a new partial index.** A RECOLOR job always
has exactly one sub-job — the same shape background operations use, not
MATCH's 1-4 — so the existing `angle IS NULL AND variant_index IS NULL`
partial-unique index already covers it without modification. Do not assume
every new operation needs an index change just because MATCH did; MATCH's
Step 1 needed one specifically because it fans out to multiple angle-less
sub-jobs per job, which RECOLOR does not.

Schema changes, all in this one migration:

1. `ALTER TYPE operation_t ADD VALUE IF NOT EXISTS 'RECOLOR'`.
2. `ALTER TYPE asset_kind_t ADD VALUE IF NOT EXISTS 'MASK'` — a mask is
   neither an `INPUT` (it isn't sent to the provider as reference material
   the way an INPUT image is — it's consumed server-side to build the
   overlay and drive compositing) nor an `OUTPUT`. It needs its own kind so
   asset-kind semantics stay meaningful and so retention (Step 5) can give
   it its own lifetime.
3. `sub_jobs.mask_asset_id UUID NULL` FK -> `assets.id`, `use_alter=True`
   (same circular-FK pattern every other `sub_jobs.*_asset_id` column
   already uses — see `app/db/models/jobs.py`'s own comment on
   `input_asset_id`).
4. `sub_jobs.palette_code TEXT NULL` — the requested target color, NULL for
   every operation except RECOLOR.

`downgrade()` reverses the two new columns but, matching `0013`'s own
downgrade and this project's documented "enum values are never removed"
convention (`docs/conventions.md`, `app/db/models/enums.py`'s module
docstring), deliberately does NOT attempt to remove `'RECOLOR'` or `'MASK'`
from their enums. Postgres has no `ALTER TYPE ... DROP VALUE`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE operation_t ADD VALUE IF NOT EXISTS 'RECOLOR'")
    op.execute("ALTER TYPE asset_kind_t ADD VALUE IF NOT EXISTS 'MASK'")

    op.add_column(
        "sub_jobs", sa.Column("mask_asset_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("sub_jobs", sa.Column("palette_code", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_sub_jobs_mask_asset_id",
        "sub_jobs",
        "assets",
        ["mask_asset_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_sub_jobs_mask_asset_id", "sub_jobs", type_="foreignkey")
    op.drop_column("sub_jobs", "palette_code")
    op.drop_column("sub_jobs", "mask_asset_id")

    # Deliberately no attempt to remove 'RECOLOR'/'MASK' from their enums —
    # see the module docstring above.
