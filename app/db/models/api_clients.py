"""api_clients — machine credentials for the Flutter ERP and future consumers."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class ApiClient(Base):
    __tablename__ = "api_clients"
    __table_args__ = (
        UniqueConstraint("key_prefix", name="uq_api_clients_key_prefix"),
        Index("ix_api_clients_key_prefix", "key_prefix"),
        CheckConstraint("scope IN ('client', 'ops')", name="ck_api_clients_scope"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String, nullable=False)
    key_hash: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False, server_default="client")
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    daily_job_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
