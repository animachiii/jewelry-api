"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-06

Creates every enum, table, index, and constraint in docs/schema.md as a
single migration — the schema is designed as a whole and partial migration
produces contradictory foreign keys.

Row Level Security stays disabled on all tables: the backend connects with
the Supabase service role and is the only writer. Do not enable RLS here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

angle_t = postgresql.ENUM("FRONT", "SIDE", "DIAGONAL", "TOP", name="angle_t", create_type=False)
job_status_t = postgresql.ENUM(
    "PENDING",
    "PROCESSING",
    "COMPLETED",
    "PARTIAL_SUCCESS",
    "FAILED",
    name="job_status_t",
    create_type=False,
)
sub_job_status_t = postgresql.ENUM(
    "PENDING",
    "MATTING",
    "GENERATING",
    "QA_REVIEW",
    "COMPLETED",
    "FAILED",
    "REJECTED",
    "SKIPPED",
    name="sub_job_status_t",
    create_type=False,
)
source_type_t = postgresql.ENUM("UPLOADED", "SYNTHETIC", name="source_type_t", create_type=False)
asset_kind_t = postgresql.ENUM("INPUT", "MATTE", "OUTPUT", name="asset_kind_t", create_type=False)
failure_class_t = postgresql.ENUM(
    "TRANSIENT_PROVIDER",
    "TRANSIENT_NETWORK",
    "RATE_LIMITED",
    "INVALID_INPUT",
    "SAFETY_REFUSAL",
    "QA_REJECTED",
    "INTERNAL",
    name="failure_class_t",
    create_type=False,
)
qa_status_t = postgresql.ENUM(
    "NOT_APPLICABLE", "PASSED", "FLAGGED", "FAILED", name="qa_status_t", create_type=False
)
sync_status_t = postgresql.ENUM("SUCCESS", "FAILED", name="sync_status_t", create_type=False)

ALL_ENUMS = [
    angle_t,
    job_status_t,
    sub_job_status_t,
    source_type_t,
    asset_kind_t,
    failure_class_t,
    qa_status_t,
    sync_status_t,
]


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in ALL_ENUMS:
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "api_clients",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("key_prefix", sa.String, nullable=False),
        sa.Column("key_hash", sa.String, nullable=False),
        sa.Column("rate_limit_per_min", sa.Integer, nullable=False, server_default="60"),
        sa.Column("daily_job_quota", sa.Integer, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_api_clients_key_prefix", "api_clients", ["key_prefix"])
    op.create_index("ix_api_clients_key_prefix", "api_clients", ["key_prefix"])

    op.create_table(
        "config_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("version_number", sa.BigInteger, nullable=False),
        sa.Column("source_hash", sa.String, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("sync_status", sync_status_t, nullable=False),
        sa.Column("error_message", sa.String, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "synced_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("activated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_config_versions_version_number", "config_versions", ["version_number"]
    )
    op.create_index(
        "ix_config_versions_active_unique",
        "config_versions",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_clients.id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String, nullable=False),
        sa.Column("category_code", sa.String, nullable=False),
        sa.Column(
            "config_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("config_versions.id"),
            nullable=False,
        ),
        sa.Column("status", job_status_t, nullable=False, server_default="PENDING"),
        sa.Column("requested_angles", sa.Integer, nullable=False),
        sa.Column("succeeded_angles", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed_angles", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sku_reference", sa.String, nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_jobs_client_idempotency_key", "jobs", ["client_id", "idempotency_key"]
    )
    op.create_index("ix_jobs_client_created_at", "jobs", ["client_id", "created_at"])
    op.create_index(
        "ix_jobs_status_active",
        "jobs",
        ["status"],
        postgresql_where=sa.text("status IN ('PENDING', 'PROCESSING')"),
    )

    op.create_table(
        "sub_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("angle", angle_t, nullable=False),
        sa.Column("status", sub_job_status_t, nullable=False, server_default="PENDING"),
        sa.Column("source_type", source_type_t, nullable=False),
        sa.Column("celery_task_id", sa.String, nullable=True),
        # input/matte/output_asset_id FKs added after `assets` exists (circular dependency)
        sa.Column("input_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("matte_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("output_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("prompt_snapshot", sa.String, nullable=True),
        sa.Column("model_version", sa.String, nullable=True),
        sa.Column("seed", sa.BigInteger, nullable=True),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failure_class", failure_class_t, nullable=True),
        sa.Column("error_message", sa.String, nullable=True),
        sa.Column("qa_score", sa.Numeric(4, 3), nullable=True),
        sa.Column("qa_status", qa_status_t, nullable=False, server_default="NOT_APPLICABLE"),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_sub_jobs_job_angle", "sub_jobs", ["job_id", "angle"])
    op.create_index("ix_sub_jobs_job_id", "sub_jobs", ["job_id"])
    op.create_index(
        "ix_sub_jobs_qa_review",
        "sub_jobs",
        ["status"],
        postgresql_where=sa.text("status = 'QA_REVIEW'"),
    )

    op.create_table(
        "assets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=False
        ),
        sa.Column(
            "sub_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sub_jobs.id"),
            nullable=True,
        ),
        sa.Column("kind", asset_kind_t, nullable=False),
        sa.Column("bucket", sa.String, nullable=False),
        sa.Column("storage_path", sa.String, nullable=False),
        sa.Column("mime_type", sa.String, nullable=False),
        sa.Column("width_px", sa.Integer, nullable=True),
        sa.Column("height_px", sa.Integer, nullable=True),
        sa.Column("bytes", sa.BigInteger, nullable=True),
        sa.Column("checksum_sha256", sa.String, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_assets_bucket_storage_path", "assets", ["bucket", "storage_path"]
    )
    op.create_index("ix_assets_job_kind", "assets", ["job_id", "kind"])
    op.create_index(
        "ix_assets_expires_at",
        "assets",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )

    # Now that `assets` exists, wire up the three sub_jobs -> assets FKs.
    op.create_foreign_key(
        "fk_sub_jobs_input_asset_id", "sub_jobs", "assets", ["input_asset_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_sub_jobs_matte_asset_id", "sub_jobs", "assets", ["matte_asset_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_sub_jobs_output_asset_id", "sub_jobs", "assets", ["output_asset_id"], ["id"]
    )

    op.create_table(
        "cost_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=False
        ),
        sa.Column(
            "sub_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sub_jobs.id"),
            nullable=True,
        ),
        sa.Column("provider", sa.String, nullable=False),
        sa.Column("operation", sa.String, nullable=False),
        sa.Column("model_version", sa.String, nullable=False),
        sa.Column("units", sa.Integer, nullable=False, server_default="1"),
        sa.Column("unit_cost_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("total_cost_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_cost_events_job_id", "cost_events", ["job_id"])
    op.create_index("ix_cost_events_created_at", "cost_events", ["created_at"])

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=False
        ),
        sa.Column(
            "sub_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sub_jobs.id"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String, nullable=False),
        sa.Column("from_status", sa.String, nullable=True),
        sa.Column("to_status", sa.String, nullable=True),
        sa.Column(
            "detail", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_job_events_job_id_created_at", "job_events", ["job_id", "created_at"])


def downgrade() -> None:
    op.drop_table("job_events")
    op.drop_table("cost_events")
    op.drop_constraint("fk_sub_jobs_output_asset_id", "sub_jobs", type_="foreignkey")
    op.drop_constraint("fk_sub_jobs_matte_asset_id", "sub_jobs", type_="foreignkey")
    op.drop_constraint("fk_sub_jobs_input_asset_id", "sub_jobs", type_="foreignkey")
    op.drop_table("assets")
    op.drop_table("sub_jobs")
    op.drop_table("jobs")
    op.drop_table("config_versions")
    op.drop_table("api_clients")

    bind = op.get_bind()
    for enum_type in reversed(ALL_ENUMS):
        enum_type.drop(bind, checkfirst=True)
