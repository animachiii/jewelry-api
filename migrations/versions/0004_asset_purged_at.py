"""add assets.purged_at

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-07

Phase 4 (Storage & Ingest Pipeline) implements the retention/expiry
lifecycle from docs/business-rules.md §11: a Celery beat task finds assets
past `expires_at` and removes their bytes from Supabase Storage, but the
row itself is never deleted (CLAUDE.md Hard Rule 10). Without a purge
marker, the task would re-attempt (harmless but wasteful) storage deletes
for the same asset on every beat tick forever. `purged_at` records when
bytes were actually removed so the task can query only unpurged, expired
rows.

Originally authored as revision 0005 in a parallel worktree (alongside a
sibling Phase 3 agent that, in the end, needed no migration at all) and
renumbered to 0004 here at merge time now that the chain is known.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column("purged_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assets", "purged_at")
