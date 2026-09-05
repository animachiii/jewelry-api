"""add GENERATE_WITH_CLEANUP operation, jobs.requested_angle_codes

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-31

GENERATE_WITH_CLEANUP is a two-phase pipeline: one uploaded photo is
background-cleaned, then 1-4 catalogue angles are generated from that
cleaned image. See docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md.

Two schema changes, one migration — same "safe to combine" reasoning
0017's own docstring established: on Postgres 12+, `ALTER TYPE ... ADD
VALUE` can run inside a transaction as long as the new value is never
compared or inserted as data within that same transaction, and this
migration's column DDL does neither.

`jobs.requested_angle_codes` is NULL for every operation except
GENERATE_WITH_CLEANUP — mirrors `preset_code`'s own nullability story
(migration 0009). It records which angles were requested so the worker can
create their sub-jobs *after* the cleanup step succeeds, once the original
request body is gone. `jobs.requested_angles` keeps its existing meaning
(a count) for this operation too — this new column is the one place the
actual angle *codes* are durably recorded.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE operation_t ADD VALUE IF NOT EXISTS 'GENERATE_WITH_CLEANUP'")
    # Raw SQL with IF NOT EXISTS, not op.add_column -- this environment's
    # shared dev/prod Supabase instance already carries this column from
    # this migration's own earlier testing against it (found live during
    # this branch's final review, see CLAUDE.md's Render-outage note), so
    # a plain add_column would crash the next real deploy on "column
    # already exists" the same way the version-pointer mismatch just did.
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS requested_angle_codes JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS requested_angle_codes")
    # Deliberately no attempt to remove 'GENERATE_WITH_CLEANUP' from
    # operation_t — Postgres has no ALTER TYPE ... DROP VALUE, and enum
    # values are never removed in this project (see migration 0017's own
    # downgrade for the same note).
