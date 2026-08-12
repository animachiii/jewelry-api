"""add jobs.operation, make sub_jobs.angle nullable

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-12

phases/phase-15-background-operations.md Step 2: BACKGROUND_REMOVAL and
BACKGROUND_REPLACEMENT jobs have no angle — a job's operation must be
explicit and a background job's single sub-job must be able to hold
angle IS NULL.

`jobs.operation` defaults to 'ANGLE_GENERATION' so every existing row (and
every existing INSERT that doesn't yet know this column exists) keeps
meaning exactly what it always meant — see docs/conventions.md's migration
rules and CLAUDE.md Hard Rule 11's sibling reasoning for config_versions
(never silently reinterpret existing rows).

The old `UniqueConstraint(job_id, angle)` is replaced, not just loosened:
Postgres unique constraints treat NULL as distinct from every other NULL,
so it would silently allow a job to accumulate more than one angle-less
sub-job. Two partial unique indexes express the real invariant instead —
"no duplicate angle within a job" and "at most one angle-less sub-job per
job" are different rules once angle can be NULL, and the database should
enforce both, not just the first as a byproduct.

The angle-non-null-iff-ANGLE_GENERATION cross-table rule itself can't be a
Postgres CHECK (it spans jobs and sub_jobs) — that's enforced in
app/services/job_service.py::validate_operation_angle_consistency instead,
pinned by tests/unit/test_job_service.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

operation_t = postgresql.ENUM(
    "ANGLE_GENERATION",
    "BACKGROUND_REMOVAL",
    "BACKGROUND_REPLACEMENT",
    name="operation_t",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    operation_t.create(bind, checkfirst=True)

    op.add_column(
        "jobs",
        sa.Column(
            "operation",
            operation_t,
            nullable=False,
            server_default="ANGLE_GENERATION",
        ),
    )

    op.drop_constraint("uq_sub_jobs_job_angle", "sub_jobs", type_="unique")
    op.alter_column("sub_jobs", "angle", nullable=True)

    op.create_index(
        "ux_sub_jobs_job_angle",
        "sub_jobs",
        ["job_id", "angle"],
        unique=True,
        postgresql_where=sa.text("angle IS NOT NULL"),
    )
    op.create_index(
        "ux_sub_jobs_job_single",
        "sub_jobs",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("angle IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_sub_jobs_job_single", table_name="sub_jobs")
    op.drop_index("ux_sub_jobs_job_angle", table_name="sub_jobs")

    # A downgrade after any background job exists would violate the
    # restored NOT NULL / unique constraint — that's correct: those rows
    # are only meaningful under the schema this migration adds, same as any
    # other Alembic downgrade past data that depends on the new shape.
    op.alter_column("sub_jobs", "angle", nullable=False)
    op.create_unique_constraint("uq_sub_jobs_job_angle", "sub_jobs", ["job_id", "angle"])

    op.drop_column("jobs", "operation")

    bind = op.get_bind()
    operation_t.drop(bind, checkfirst=True)
