"""Durable swap plans and exclusive resource reservations."""
from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampUUIDMixin


class DomainSwapJob(TimestampUUIDMixin, Base):
    __tablename__ = "domain_swap_jobs"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="preview")
    # Contains identifiers, configuration and progress only; never credentials.
    mappings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class DomainSwapReservation(Base):
    __tablename__ = "domain_swap_reservations"

    resource: Mapped[str] = mapped_column(String(300), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("domain_swap_jobs.id", ondelete="CASCADE"), nullable=False, index=True,
    )
