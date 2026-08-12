"""make jobs.category_code nullable

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-12

phases/phase-15-background-operations.md Step 4: `POST /background/remove`
and `POST /background/replace` create jobs with no category at all — the
request bodies the phase file specifies (`{storage_path, sku_reference?,
metadata?}` / `{storage_path, preset_code, sku_reference?, metadata?}`)
never include one, and there is no sensible category_code to invent for a
job that isn't about a jewelry category matrix in the first place. A gap
the phase file itself doesn't call out (it only addresses `sub_jobs.angle`
in Step 2), found while implementing Step 4's job-creation path — same
kind of gap Phase 2 found in presign path ownership scoping, fixed here
rather than papering over it with a fake category string that would
corrupt cost/ops reporting.

`jobs.config_version_id` stays NOT NULL — a background job still pins the
config version its operation/preset config was read from, same "pin at
creation" principle every job follows.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("jobs", "category_code", nullable=True)


def downgrade() -> None:
    op.alter_column("jobs", "category_code", nullable=False)
