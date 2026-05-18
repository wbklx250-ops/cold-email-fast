from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.domain import DomainStatus


class DomainBase(BaseModel):
    name: str


class DomainCreate(DomainBase):
    redirect_url: str | None = None


class DomainUpdate(BaseModel):
    name: str | None = None
    tld: str | None = None
    status: DomainStatus | None = None
    cloudflare_zone_id: str | None = None
    cloudflare_nameservers: list[str] | None = None
    cloudflare_zone_status: str | None = None
    nameservers_updated: bool | None = None
    nameservers_updated_at: datetime | None = None
    dns_records_created: bool | None = None
    mx_configured: bool | None = None
    spf_configured: bool | None = None
    dmarc_configured: bool | None = None
    dkim_cnames_added: bool | None = None
    dkim_enabled: bool | None = None
    dkim_selector1_cname: str | None = None
    dkim_selector2_cname: str | None = None
    # Phase 1 tracking
    phase1_cname_added: bool | None = None
    phase1_dmarc_added: bool | None = None
    # M365 verification
    verification_txt_value: str | None = None
    verification_txt_added: bool | None = None
    # Error tracking
    error_message: str | None = None
    # Redirect URL
    redirect_url: str | None = None
    redirect_configured: bool | None = None
    # Milestone timestamps
    ns_propagated_at: datetime | None = None
    m365_verified_at: datetime | None = None
    tenant_id: UUID | None = None


class DomainRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    tld: str
    status: DomainStatus
    cloudflare_zone_id: str | None
    cloudflare_nameservers: list[str]
    cloudflare_zone_status: str
    nameservers_updated: bool
    nameservers_updated_at: datetime | None
    dns_records_created: bool
    mx_configured: bool
    spf_configured: bool
    dmarc_configured: bool
    dkim_cnames_added: bool
    dkim_enabled: bool
    dkim_selector1_cname: str | None
    dkim_selector2_cname: str | None
    # Phase 1 tracking
    phase1_cname_added: bool
    phase1_dmarc_added: bool
    # M365 verification
    verification_txt_value: str | None
    verification_txt_added: bool
    # Error tracking
    error_message: str | None
    # Redirect URL
    redirect_url: str | None
    redirect_configured: bool
    # Milestone timestamps
    ns_propagated_at: datetime | None
    m365_verified_at: datetime | None
    tenant_id: UUID | None
    created_at: datetime
    updated_at: datetime


class DomainList(BaseModel):
    items: list[DomainRead]
    total: int
    page: int
    per_page: int


class DomainWithNameservers(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: DomainStatus
    cloudflare_nameservers: list[str]
    cloudflare_zone_status: str
    nameservers_updated: bool


# ============================================================
# Bulk Import/Export Schemas
# ============================================================

class NameserverGroup(BaseModel):
    """Group of domains sharing the same Cloudflare nameservers."""
    nameservers: List[str]  # e.g. ["anna.ns.cloudflare.com", "bob.ns.cloudflare.com"]
    domain_count: int
    domains: List[str]  # List of domain names in this group


class BulkImportResult(BaseModel):
    """Result of bulk domain import operation."""
    total: int
    created: int
    skipped: int
    failed: int
    results: List[Dict[str, Any]]  # [{"domain": "x.com", "status": "created|skipped|failed", "reason": "..."}]


class BulkZoneResult(BaseModel):
    """Result of bulk Cloudflare zone creation."""
    total: int
    success: int
    failed: int
    results: List[Dict[str, Any]]
    nameserver_groups: List[NameserverGroup]


class CloudflareZoneSetupRequest(BaseModel):
    """Standalone request for creating or finding Cloudflare zones."""
    domains: List[str]
    redirect_url: str | None = None


class CloudflareZoneSetupResult(BaseModel):
    """Result of standalone Cloudflare zone setup."""
    total: int
    success: int
    failed: int
    results: List[Dict[str, Any]]
    nameserver_groups: List[NameserverGroup]
