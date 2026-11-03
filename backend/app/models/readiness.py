"""Durable outbound-mail checks and their encrypted restart checkpoints."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampUUIDMixin


class ReadinessCheck(TimestampUUIDMixin, Base):
    __tablename__ = 'readiness_checks'

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True, index=True)
    admin_email: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(120), default='')
    status: Mapped[str] = mapped_column(String(40), index=True, default='queued')
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    credentials_ciphertext: Mapped[str] = mapped_column(Text)
    artifacts_ciphertext: Mapped[str] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
