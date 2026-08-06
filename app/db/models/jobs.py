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
from app.db.models.enums import Angle, FailureClass, JobStatus, QAStatus, SourceType, SubJobStatus


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
    category_code: Mapped[str] = mapped_column(String, nullable=False)
    config_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("config_versions.id"), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status_t"), nullable=False, server_default=JobStatus.PENDING.value
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
        UniqueConstraint("job_id", "angle", name="uq_sub_jobs_job_angle"),
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
    angle: Mapped[Angle] = mapped_column(Enum(Angle, name="angle_t"), nullable=False)
    status: Mapped[SubJobStatus] = mapped_column(
        Enum(SubJobStatus, name="sub_job_status_t"),
        nullable=False,
        server_default=SubJobStatus.PENDING.value,
    )
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type_t"), nullable=False
    )
    celery_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    input_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id"), nullable=True
    )
    matte_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id"), nullable=True
    )
    output_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id"), nullable=True
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
