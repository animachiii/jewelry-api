"""add api_clients.scope

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-06

docs/api-routes.md states "Scope lives on the api_clients row" (client | ops)
but docs/schema.md's api_clients table never defined the column — a gap
caught while writing Phase 0 Step 6 seed data (an ops-scope client is one of
the required seed rows). Fixing the schema and docs together rather than
carrying the drift forward.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_clients",
        sa.Column("scope", sa.String, nullable=False, server_default="client"),
    )
    op.create_check_constraint("ck_api_clients_scope", "api_clients", "scope IN ('client', 'ops')")


def downgrade() -> None:
    op.drop_constraint("ck_api_clients_scope", "api_clients", type_="check")
    op.drop_column("api_clients", "scope")
