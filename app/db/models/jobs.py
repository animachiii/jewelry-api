"""jobs, sub_jobs — one job per POST /generate, one sub-job per angle.

See docs/schema.md and docs/business-rules.md §2-3 for the state machine
and parent-status rollup this schema supports.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.db.models.enums import (
    Angle,
    FailureClass,
    JobStatus,
    Operation,
    QAStatus,
    SourceType,
    SubJobStatus,
)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_jobs_client_idempotency_key"),
        Index("ix_jobs_client_created_at", "client_id", "created_at"),
        Index(
            "ix_jobs_status_active",
            "status",
            postgresql_where=text("status IN ('PENDING', 'PROCESSING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_clients.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    # NULL for a background-operation job (operation != ANGLE_GENERATION) —
    # there is no jewelry category to pin. See migration 0008 and
    # phases/phase-15-background-operations.md Step 4.
    category_code: Mapped[str | None] = mapped_column(String, nullable=True)
    # NULL unless operation == BACKGROUND_REPLACEMENT — the worker needs a
    # durable, queryable record of which preset was requested to resolve
    # its prompt. See migration 0009 and
    # phases/phase-15-background-operations.md Step 5.
    preset_code: Mapped[str | None] = mapped_column(String, nullable=True)
    config_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("config_versions.id"), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status_t"), nullable=False, server_default=JobStatus.PENDING.value
    )
    operation: Mapped[Operation] = mapped_column(
        Enum(Operation, name="operation_t"),
        nullable=False,
        server_default=Operation.ANGLE_GENERATION.value,
    )
    requested_angles: Mapped[int] = mapped_column(Integer, nullable=False)
    succeeded_angles: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_angles: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sku_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    job_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class SubJob(Base):
    __tablename__ = "sub_jobs"
    __table_args__ = (
        # No plain UniqueConstraint("job_id", "angle") — a background job's
        # sub-job has angle IS NULL, and Postgres unique constraints treat
        # NULLs as distinct, so it wouldn't stop a job accumulating more than
        # one angle-less sub-job. These two partial indexes replace it: one
        # enforces "no duplicate angle within a job" (angle jobs only), the
        # other "at most one angle-less sub-job per job" (background jobs
        # only). See docs/schema.md and phases/phase-15-background-operations.md
        # Step 2.
        Index(
            "ux_sub_jobs_job_angle",
            "job_id",
            "angle",
            unique=True,
            postgresql_where=text("angle IS NOT NULL"),
        ),
        Index(
            "ux_sub_jobs_job_single",
            "job_id",
            unique=True,
            postgresql_where=text("angle IS NULL"),
        ),
        Index("ix_sub_jobs_job_id", "job_id"),
        Index(
            "ix_sub_jobs_qa_review",
            "status",
            postgresql_where=text("status = 'QA_REVIEW'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    # NULL for a background-operation sub-job (operation != ANGLE_GENERATION)
    # — see the operation/angle cross-table invariant enforced in
    # app/services/job_service.py::validate_operation_angle_consistency,
    # which is the Postgres-CHECK-that-can't-be-expressed this schema
    # otherwise relies on.
    angle: Mapped[Angle | None] = mapped_column(Enum(Angle, name="angle_t"), nullable=True)
    status: Mapped[SubJobStatus] = mapped_column(
        Enum(SubJobStatus, name="sub_job_status_t"),
        nullable=False,
        server_default=SubJobStatus.PENDING.value,
    )
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type_t"), nullable=False
    )
    celery_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # use_alter=True + explicit names: assets <-> sub_jobs is a genuine circular FK
    # (see docs/schema.md relationships) — without this, SQLAlchemy can't sort
    # tables for create_all/drop_all and Alembic's `check` can't resolve it either.
    input_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", name="fk_sub_jobs_input_asset_id", use_alter=True),
        nullable=True,
    )
    matte_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", name="fk_sub_jobs_matte_asset_id", use_alter=True),
        nullable=True,
    )
    output_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", name="fk_sub_jobs_output_asset_id", use_alter=True),
        nullable=True,
    )
    # NULL unless this BACKGROUND_REPLACEMENT sub-job used an uploaded
    # background photo instead of a preset — see migration 0011 and
    # docs/superpowers/specs/2026-08-13-custom-background-compositing-design.md.
    # An ordinary INPUT-kind asset, distinguished from input_asset_id only by
    # which FK column points at it (same pattern matte_asset_id/output_asset_id
    # already use).
    background_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", name="fk_sub_jobs_background_asset_id", use_alter=True),
        nullable=True,
    )
    prompt_snapshot: Mapped[str | None] = mapped_column(String, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failure_class: Mapped[FailureClass | None] = mapped_column(
        Enum(FailureClass, name="failure_class_t"), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    qa_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    qa_status: Mapped[QAStatus] = mapped_column(
        Enum(QAStatus, name="qa_status_t"),
        nullable=False,
        server_default=QAStatus.NOT_APPLICABLE.value,
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
