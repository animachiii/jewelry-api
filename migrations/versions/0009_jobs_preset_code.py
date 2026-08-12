"""add jobs.preset_code

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-12

phases/phase-15-background-operations.md Step 5: `background.process`
(the worker) needs to know which backdrop preset a BACKGROUND_REPLACEMENT
job requested in order to resolve the right prompt — but nothing durable
recorded that choice. `create_background_job_for_request` (Step 4) only
ever put `preset_code` in the `JOB_CREATED` job_events detail, an audit
log, not a queryable business field a worker should read from. A gap the
phase file doesn't call out (same kind as Step 4's `jobs.category_code`
gap), found while wiring the worker to actually resolve a prompt.

NULL for BACKGROUND_REMOVAL and ANGLE_GENERATION jobs, which have no
preset. Not reusing `jobs.metadata` — that's documented as opaque client
passthrough (docs/schema.md); a preset selection is this system's own
business data, not the client's, and conflating the two risks a client's
own `metadata.preset_code` key colliding with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("preset_code", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "preset_code")
