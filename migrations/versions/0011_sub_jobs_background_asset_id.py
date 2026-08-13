"""add sub_jobs.background_asset_id

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-13

Custom-background compositing
(docs/superpowers/specs/2026-08-13-custom-background-compositing-design.md):
BACKGROUND_REPLACEMENT can now composite onto a client-uploaded background
photo instead of only a curated preset. The background photo is stored as
an ordinary INPUT-kind asset scoped to the sub-job (docs/schema.md) — this
column is what app/services/background_service.py reads to know a second
image exists and where to find it. NULL for every existing row (preset-based
replacement, removal, and angle generation all leave it unset) — same
nullable-FK pattern as matte_asset_id/output_asset_id (migration 0001).
Both tables already exist by this point, so no use_alter circular-FK dance
is needed the way migration 0001's initial create did.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sub_jobs",
        sa.Column("background_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_sub_jobs_background_asset_id", "sub_jobs", "assets", ["background_asset_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_sub_jobs_background_asset_id", "sub_jobs", type_="foreignkey")
    op.drop_column("sub_jobs", "background_asset_id")
