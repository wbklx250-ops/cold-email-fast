from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampUUIDMixin


class TenantDisposition(str, Enum):
    """Operator-managed lifecycle state for an audited tenant."""

    UNREVIEWED = "unreviewed"
    AVAILABLE = "available"
    ACTIVE = "active"
    BURNED = "burned"


class TenantAudit(TimestampUUIDMixin, Base):
    """Persistent results from the tenant domain checker.

    Login passwords and TOTP secrets are deliberately never stored here. They
    exist only for the lifetime of the checker job.
    """

    __tablename__ = "tenant_audits"

    admin_email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    tenant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    disposition: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TenantDisposition.UNREVIEWED.value, index=True
    )
    login_success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    login_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    verified_domains: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    unverified_domains: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    custom_domain_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
