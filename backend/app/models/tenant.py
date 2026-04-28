from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampUUIDMixin

if TYPE_CHECKING:
    from app.models.batch import SetupBatch
    from app.models.domain import Domain
    from app.models.mailbox import Mailbox


class TenantStatus(str, Enum):
    """Status tracking for tenant automation workflow - synced with database enum."""
    # Core states
    NEW = "new"
    CONFIGURING = "configuring"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    RETIRED = "retired"
    ERROR = "error"
    
    # Import/setup states
    IMPORTED = "imported"
    FIRST_LOGIN_PENDING = "first_login_pending"
    FIRST_LOGIN_COMPLETE = "first_login_complete"
    
    # Domain states
    DOMAIN_LINKED = "domain_linked"
    DOMAIN_ADDED = "domain_added"
    M365_CONNECTED = "m365_connected"
    DOMAIN_VERIFIED = "domain_verified"
    
    # DNS/DKIM states
    DNS_CONFIGURING = "dns_configuring"
    DNS_CONFIGURED = "dns_configured"
    DKIM_CONFIGURING = "dkim_configuring"
    PENDING_DKIM = "pending_dkim"  # DKIM CNAMEs added, waiting for enable
    DKIM_ENABLED = "dkim_enabled"
    
    # Mailbox states
    MAILBOXES_CREATING = "mailboxes_creating"
    MAILBOXES_CONFIGURING = "mailboxes_configuring"
    MAILBOXES_CREATED = "mailboxes_created"
    
    # Final state
    READY = "ready"


class Tenant(TimestampUUIDMixin, Base):
    __tablename__ = "tenants"

    # === FROM RESELLER CSV ===
    microsoft_tenant_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # Company Name
    onmicrosoft_domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)  # Reseller name
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # === ADMIN CREDENTIALS ===
    admin_email: Mapped[str] = mapped_column(String(255), nullable=False)  # admin@xxx.onmicrosoft.com
    admin_password: Mapped[str] = mapped_column(String(255), nullable=False)  # Current working password (updated after change)
    initial_password: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Original password from reseller (never changes)
    totp_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)  # MFA backup - ENCRYPT IN PROD!

    # === FIRST LOGIN TRACKING ===
    first_login_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    security_defaults_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    security_defaults_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    security_defaults_disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # === CONDITIONAL ACCESS TRACKING ===
    # Some tenants ship with Microsoft-managed CA policies that block our automation
    # by enforcing MFA. When detected, step8 disables the targeted MFA policies via
    # Graph PATCH and records the outcome here.
    conditional_access_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conditional_access_disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    conditional_access_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    conditional_access_policies_disabled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # === OAUTH TOKENS ===
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)  # ENCRYPT IN PROD!
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)  # ENCRYPT IN PROD!
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # === LICENSED USER ===
    licensed_user_upn: Mapped[str | None] = mapped_column(String(255), nullable=True)  # user@customdomain.com
    licensed_user_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    licensed_user_created: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    licensed_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    license_assigned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # === STEP 6 PROGRESS TRACKING ===
    step6_started: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    step6_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    step6_mailboxes_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step6_display_names_fixed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step6_accounts_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step6_passwords_set: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step6_upns_fixed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step6_delegations_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step6_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    step6_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    step6_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # === STEP 7 PROGRESS TRACKING ===
    step7_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    step7_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    step7_smtp_auth_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    step7_app_consent_granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    step7_app_consent_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    step7_app_consent_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    step7_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # === STEP 5 PROGRESS TRACKING ===
    step5_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    step5_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # === AUTO-RETRY TRACKING (max 4 retries per step) ===
    step4_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step5_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step6_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step7_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # === DOMAIN LINKING ===
    custom_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    domain_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL"), nullable=True
    )

    # === M365 DOMAIN SETUP ===
    domain_added_to_m365: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    m365_verification_txt: Mapped[str | None] = mapped_column(String(255), nullable=True)
    domain_verified_in_m365: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    domain_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # === DNS TRACKING ===
    mx_record_added: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    spf_record_added: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    autodiscover_added: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    
    # DNS values from M365
    mx_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    spf_value: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # === DKIM TRACKING ===
    dkim_selector1: Mapped[str | None] = mapped_column(String(500), nullable=True)
    dkim_selector2: Mapped[str | None] = mapped_column(String(500), nullable=True)
    dkim_selector1_cname: Mapped[str | None] = mapped_column(String(500), nullable=True)
    dkim_selector2_cname: Mapped[str | None] = mapped_column(String(500), nullable=True)
    dkim_cnames_added: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dkim_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dkim_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dkim_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Background job retry counter
    dkim_last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # Last retry timestamp

    # === MAILBOX TRACKING ===
    target_mailbox_count: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    mailbox_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Actual count
    mailboxes_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # Credentials ready
    mailboxes_created: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # Exist in M365
    mailboxes_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mailboxes_configured: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delegation_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # === STATUS ===
    status: Mapped[TenantStatus] = mapped_column(
        SqlEnum(
            TenantStatus,
            name="tenant_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    setup_step: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Current automation step for debugging
    setup_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # === RESELLER TRACKING ===
    provider_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # === BATCH RELATIONSHIP ===
    batch_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("setup_batches.id", ondelete="SET NULL"), nullable=True
    )

    # === RELATIONSHIPS ===
    domain: Mapped[Domain | None] = relationship(
        "Domain",
        uselist=False,
        primaryjoin="Tenant.domain_id == Domain.id",
        foreign_keys="[Tenant.domain_id]",
        overlaps="tenant"
    )
    
    batch: Mapped[Optional[SetupBatch]] = relationship(
        "SetupBatch",
        back_populates="tenants",
    )
    
    mailboxes: Mapped[list[Mailbox]] = relationship(
        "Mailbox", back_populates="tenant", cascade="all, delete-orphan"
    )
