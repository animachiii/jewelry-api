"""add MATCH operation, sub_jobs.variant_index, narrow ux_sub_jobs_job_single

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-16

phases/phase-18-match.md Step 1: MATCH is a fourth `operation_t` value that
fans out into 1-4 independent, angle-less "variant" sub-jobs per job (one
companion piece per variant), instead of angle generation's one-sub-job-per-
angle shape or background operations' always-exactly-one-sub-job shape.

**Correction to phases/phase-18-match.md's Step 1 claim about precedent:**
the phase file asserts that "0006's own migration already establishes this
pattern for operation_t" (i.e. that 0006 did `ALTER TYPE ... ADD VALUE` in
its own migration, separate from other DDL). That's wrong — read
migrations/versions/0006_add_job_operation.py: it *creates* `operation_t`
fresh via `postgresql.ENUM(...).create(bind, checkfirst=True)` with all
three values baked in at creation time. It never runs `ALTER TYPE ... ADD
VALUE` at all, and `grep -rn "ADD VALUE" migrations/versions/` turns up
nothing anywhere in this project. There is no actual precedent to follow
here — this migration is the first one that adds a value to an
already-existing enum type.

Given that, this migration was designed from real Postgres semantics
instead of a (nonexistent) local precedent: as of Postgres 12,
`ALTER TYPE ... ADD VALUE` **can** run inside a transaction (the old
"must be its own transaction, full stop" restriction is gone). The only
remaining restriction is that the *newly added value* cannot be *used*
(compared, cast, or inserted as data) within that same transaction that
added it. This migration only adds the enum value and does plain column/
index DDL alongside it — it never inserts or compares against 'MATCH' as a
value — so combining everything into one migration transaction (Alembic
runs each migration in a single transaction by default) is safe on the
Postgres 15 this project runs (see docker-compose.yml /
tests/conftest.py's `PostgresContainer("postgres:15-alpine", ...)`).
`IF NOT EXISTS` makes the ADD VALUE idempotent/rerunnable on its own
merits, on top of Alembic's own version tracking.

Schema changes, all in this one migration:

1. `ALTER TYPE operation_t ADD VALUE IF NOT EXISTS 'MATCH'`.
2. `sub_jobs.variant_index INT NULL` — 0-based position within a MATCH job's
   requested variants. NULL for every other operation, mirroring how
   `angle` is NULL for non-ANGLE_GENERATION operations.
3. `ux_sub_jobs_job_single` is dropped and recreated with its `WHERE` clause
   narrowed from `angle IS NULL` to `angle IS NULL AND variant_index IS
   NULL` — background-operation sub-jobs have `variant_index` also NULL, so
   the "exactly one angle-less sub-job per job" invariant for
   BACKGROUND_REMOVAL/BACKGROUND_REPLACEMENT is preserved exactly, while a
   MATCH job's several angle-less-but-variant-indexed sub-jobs no longer
   collide with it. Same drop/recreate shape 0006 used for this same index.
4. New `ux_sub_jobs_job_variant` — `(job_id, variant_index)` unique where
   `variant_index IS NOT NULL` — the MATCH-side equivalent of
   `ux_sub_jobs_job_angle`.

`downgrade()` reverses the column and both indexes, but — matching this
project's own stated convention (docs/conventions.md "Enum values are added
with ALTER TYPE ... ADD VALUE. Never removed — existing rows reference
them.") and app/db/models/enums.py's module docstring — it deliberately does
NOT attempt to remove 'MATCH' from `operation_t`. Postgres has no
`ALTER TYPE ... DROP VALUE`; removing a value requires rebuilding the whole
type (rename, recreate, migrate every column that uses it, drop the old
one), which is exactly the disruptive operation this project's own
convention says to avoid. This mirrors 0006's downgrade only where 0006
itself owned the type outright (it dropped the type it had just created);
here the type predates this migration, so downgrade leaves the enum value
in place, same as any other shipped enum value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE operation_t ADD VALUE IF NOT EXISTS 'MATCH'")

    op.add_column("sub_jobs", sa.Column("variant_index", sa.Integer(), nullable=True))

    op.drop_index("ux_sub_jobs_job_single", table_name="sub_jobs")
    op.create_index(
        "ux_sub_jobs_job_single",
        "sub_jobs",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("angle IS NULL AND variant_index IS NULL"),
    )
    op.create_index(
        "ux_sub_jobs_job_variant",
        "sub_jobs",
        ["job_id", "variant_index"],
        unique=True,
        postgresql_where=sa.text("variant_index IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_sub_jobs_job_variant", table_name="sub_jobs")
    op.drop_index("ux_sub_jobs_job_single", table_name="sub_jobs")
    op.create_index(
        "ux_sub_jobs_job_single",
        "sub_jobs",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("angle IS NULL"),
    )

    # A downgrade after any MATCH job exists (variant_index NOT NULL rows)
    # would violate nothing here directly, but those rows lose their
    # variant_index — same "downgrading past data that depends on the new
    # shape is expected to be lossy" posture 0006's downgrade documents for
    # angle nullability.
    op.drop_column("sub_jobs", "variant_index")

    # Deliberately no attempt to remove 'MATCH' from operation_t — see the
    # module docstring above.
