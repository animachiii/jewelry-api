"""add jobs.payload_hash

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-07

docs/business-rules.md §8 requires that replaying an Idempotency-Key with a
*different* request body returns 409, not a silent replay of the original
job. Phase 1's mock stored the payload hash in Redis only (24h TTL) — not
durable enough for the real check. Postgres needs to hold it permanently,
same as the idempotency key itself.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("payload_hash", sa.String, nullable=False, server_default=""),
    )
    op.alter_column("jobs", "payload_hash", server_default=None)


def downgrade() -> None:
    op.drop_column("jobs", "payload_hash")
