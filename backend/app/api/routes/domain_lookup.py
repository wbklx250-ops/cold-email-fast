"""
Domain Lookup API Routes - Check domains against Microsoft's public endpoints.

No authentication with Microsoft is needed — these are publicly accessible
OpenID Connect discovery endpoints.

Tenant matching uses a multi-strategy cascade:
1. By microsoft_tenant_id (exact GUID match)
2. By custom_domain on tenant (tenant has this domain assigned)
3. By existing Domain→Tenant FK link in the domains table
4. By Tenant.domain_id FK pointing to this domain
5. By organization name fuzzy match
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import pyotp

from app.api.deps import get_db
from app.models.domain import Domain
from app.models.tenant import Tenant
from app.services.domain_lookup import DomainLookupService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/domain-lookup", tags=["domain-lookup"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class BulkLookupRequest(BaseModel):
    domains: list[str]  # List of domain names to check


class TenantLoginDetails(BaseModel):
    tenant_id: str
    tenant_name: str
    microsoft_tenant_id: str
    onmicrosoft_domain: str
    provider: str
    admin_email: str
    admin_password: str
    login_url: str = "https://admin.microsoft.com/"
    has_totp_secret: bool = False
    totp_code: Optional[str] = None
    totp_seconds_remaining: Optional[int] = None
    totp_error: Optional[str] = None


class LookupResultWithDB(BaseModel):
    domain: str
    is_connected: bool
    microsoft_tenant_id: Optional[str] = None
    organization_name: Optional[str] = None
    namespace_type: Optional[str] = None
    error: Optional[str] = None
    # Database matching info
    found_in_db: bool = False
    db_domain_id: Optional[str] = None
    db_tenant_id: Optional[str] = None
    db_tenant_name: Optional[str] = None
    match_method: Optional[str] = None  # How tenant was matched


class CredentialLookupResult(LookupResultWithDB):
    credentials: Optional[TenantLoginDetails] = None
    credential_error: Optional[str] = None


class BulkLookupResponse(BaseModel):
    total: int
    connected: int
    not_connected: int
    errors: int
    results: list[LookupResultWithDB]


class BulkCredentialLookupResponse(BaseModel):
    total: int
    matched: int
    credentials_found: int
    missing_credentials: int
    errors: int
    results: list[CredentialLookupResult]


# ---------------------------------------------------------------------------
# Multi-strategy tenant matching
# ---------------------------------------------------------------------------

BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def clean_domain_list(domains: list[str]) -> list[str]:
    """Normalize domains while preserving first-seen order."""
    clean_domains: list[str] = []
    seen: set[str] = set()

    for domain in domains:
        cleaned = domain.strip().lower().removeprefix("http://").removeprefix("https://")
        cleaned = cleaned.split("/", 1)[0].strip(".")

        if not cleaned or "." not in cleaned or cleaned in seen:
            continue

        seen.add(cleaned)
        clean_domains.append(cleaned)

    return clean_domains


async def get_domain_record(db: AsyncSession, domain_name: str) -> Optional[Domain]:
    result = await db.execute(select(Domain).where(Domain.name == domain_name))
    return result.scalar_one_or_none()


def maybe_decode_legacy_password(password: str) -> str:
    """
    Old create/update endpoints base64-encoded admin_password values.
    CSV imports store the actual working password. Decode only when the value
    has strong base64 markers to avoid corrupting normal passwords.
    """
    if not password or len(password) % 4 != 0 or not BASE64_RE.fullmatch(password):
        return password

    try:
        decoded_bytes = base64.b64decode(password, validate=True)
        decoded = decoded_bytes.decode("utf-8")
    except Exception:
        return password

    if not decoded or any(ord(char) < 32 for char in decoded):
        return password

    # Avoid turning plain passwords like "abcd" into binary-looking strings.
    if not password.endswith("=") and len(decoded) < 8:
        return password

    return decoded


def build_tenant_login_details(tenant: Tenant) -> TenantLoginDetails:
    totp_secret = "".join((tenant.totp_secret or "").split()).upper()
    totp_code: Optional[str] = None
    totp_seconds_remaining: Optional[int] = None
    totp_error: Optional[str] = None

    if totp_secret:
        try:
            totp = pyotp.TOTP(totp_secret)
            now = time.time()
            totp_code = totp.at(int(now))
            totp_seconds_remaining = max(0, int(totp.interval - (now % totp.interval)))
        except Exception as exc:
            totp_error = f"Could not generate TOTP code: {exc}"

    return TenantLoginDetails(
        tenant_id=str(tenant.id),
        tenant_name=tenant.name,
        microsoft_tenant_id=tenant.microsoft_tenant_id,
        onmicrosoft_domain=tenant.onmicrosoft_domain,
        provider=tenant.provider,
        admin_email=tenant.admin_email,
        admin_password=maybe_decode_legacy_password(tenant.admin_password),
        has_totp_secret=bool(totp_secret),
        totp_code=totp_code,
        totp_seconds_remaining=totp_seconds_remaining,
        totp_error=totp_error,
    )


def credential_error_for_tenant(tenant: Tenant) -> Optional[str]:
    missing = []
    if not tenant.admin_email:
        missing.append("admin_email")
    if not tenant.admin_password:
        missing.append("admin_password")

    if missing:
        return f"Tenant is missing {', '.join(missing)}"

    return None

async def find_tenant_multi_strategy(
    db: AsyncSession,
    domain_name: str,
    ms_tenant_id: Optional[str],
    org_name: Optional[str],
    db_domain: Optional[Domain],
) -> tuple[Optional[Tenant], str]:
    """
    Try multiple strategies to find the matching tenant.
    Returns (tenant, match_method) or (None, "").

    Strategies in priority order:
    1. microsoft_tenant_id exact match
    2. Tenant.custom_domain matches domain name
    3. Existing Domain.tenant_id FK
    4. Tenant.domain_id FK points to this domain
    5. Organization name match against Tenant.name
    """

    # Strategy 1: Match by microsoft_tenant_id (GUID)
    if ms_tenant_id:
        result = await db.execute(
            select(Tenant).where(Tenant.microsoft_tenant_id == ms_tenant_id)
        )
        tenant = result.scalar_one_or_none()
        if tenant:
            return tenant, "tenant_id"

    # Strategy 2: Match by Tenant.custom_domain == domain name
    result = await db.execute(
        select(Tenant).where(Tenant.custom_domain == domain_name)
    )
    tenant = result.scalar_one_or_none()
    if tenant:
        return tenant, "custom_domain"

    # Strategy 3: Existing Domain→Tenant FK link
    if db_domain and db_domain.tenant_id:
        result = await db.execute(
            select(Tenant).where(Tenant.id == db_domain.tenant_id)
        )
        tenant = result.scalar_one_or_none()
        if tenant:
            return tenant, "domain_fk"

    # Strategy 4: Tenant.domain_id FK points to this domain
    if db_domain:
        result = await db.execute(
            select(Tenant).where(Tenant.domain_id == db_domain.id)
        )
        tenant = result.scalar_one_or_none()
        if tenant:
            return tenant, "tenant_domain_fk"

    # Strategy 5: Organization name match (case-insensitive)
    if org_name and org_name.strip():
        result = await db.execute(
            select(Tenant).where(Tenant.name.ilike(org_name.strip()))
        )
        tenant = result.scalar_one_or_none()
        if tenant:
            return tenant, "org_name"

    return None, ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/check", response_model=BulkLookupResponse)
async def bulk_domain_lookup(
    request: BulkLookupRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Check a list of domains against Microsoft's public endpoints.
    Returns tenant connection status and matches against our database
    using multi-strategy tenant matching.
    """
    service = DomainLookupService()

    # Clean and deduplicate domains
    clean_domains = clean_domain_list(request.domains)

    if not clean_domains:
        raise HTTPException(status_code=400, detail="No valid domains provided")

    if len(clean_domains) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 domains per request")

    # Check against Microsoft
    ms_results = await service.check_domains_bulk(clean_domains)

    # Match against our database using multi-strategy
    enriched_results: list[LookupResultWithDB] = []
    for ms_result in ms_results:
        enriched = LookupResultWithDB(**ms_result.model_dump())

        # Check if domain exists in our DB
        db_domain = await get_domain_record(db, ms_result.domain)

        if db_domain:
            enriched.found_in_db = True
            enriched.db_domain_id = str(db_domain.id)

        # Multi-strategy tenant matching
        if ms_result.is_connected or (db_domain and db_domain.tenant_id):
            db_tenant, match_method = await find_tenant_multi_strategy(
                db=db,
                domain_name=ms_result.domain,
                ms_tenant_id=ms_result.microsoft_tenant_id,
                org_name=ms_result.organization_name,
                db_domain=db_domain,
            )
            if db_tenant:
                enriched.db_tenant_id = str(db_tenant.id)
                enriched.db_tenant_name = db_tenant.name
                enriched.match_method = match_method

        enriched_results.append(enriched)

    connected = sum(1 for r in enriched_results if r.is_connected)
    errors = sum(1 for r in enriched_results if r.error)

    return BulkLookupResponse(
        total=len(enriched_results),
        connected=connected,
        not_connected=len(enriched_results) - connected,
        errors=errors,
        results=enriched_results,
    )


@router.post("/credentials", response_model=BulkCredentialLookupResponse)
async def bulk_domain_credentials_lookup(
    request: BulkLookupRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Find the tenant for each domain and return the login details needed to
    access that tenant, including a live TOTP code when a TOTP secret is stored.
    Supports single-domain and batch inputs using the same request format.
    """
    service = DomainLookupService()
    clean_domains = clean_domain_list(request.domains)

    if not clean_domains:
        raise HTTPException(status_code=400, detail="No valid domains provided")

    if len(clean_domains) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 domains per request")

    ms_results = await service.check_domains_bulk(clean_domains)

    enriched_results: list[CredentialLookupResult] = []
    for ms_result in ms_results:
        enriched = CredentialLookupResult(**ms_result.model_dump())
        db_domain = await get_domain_record(db, ms_result.domain)

        if db_domain:
            enriched.found_in_db = True
            enriched.db_domain_id = str(db_domain.id)

        db_tenant, match_method = await find_tenant_multi_strategy(
            db=db,
            domain_name=ms_result.domain,
            ms_tenant_id=ms_result.microsoft_tenant_id,
            org_name=ms_result.organization_name,
            db_domain=db_domain,
        )

        if db_tenant:
            enriched.db_tenant_id = str(db_tenant.id)
            enriched.db_tenant_name = db_tenant.name
            enriched.match_method = match_method

            credential_error = credential_error_for_tenant(db_tenant)
            if credential_error:
                enriched.credential_error = credential_error
            else:
                enriched.credentials = build_tenant_login_details(db_tenant)
        elif db_domain:
            enriched.credential_error = "Domain exists in DB but is not linked to a tenant"
        elif ms_result.is_connected:
            enriched.credential_error = "Domain is connected to Microsoft but no local tenant match was found"
        else:
            enriched.credential_error = "Domain is not connected to a Microsoft tenant"

        enriched_results.append(enriched)

    matched = sum(1 for r in enriched_results if r.db_tenant_id)
    credentials_found = sum(1 for r in enriched_results if r.credentials is not None)
    errors = sum(1 for r in enriched_results if r.error)

    return BulkCredentialLookupResponse(
        total=len(enriched_results),
        matched=matched,
        credentials_found=credentials_found,
        missing_credentials=len(enriched_results) - credentials_found,
        errors=errors,
        results=enriched_results,
    )


@router.post("/sync-to-db")
async def sync_lookup_to_database(
    request: BulkLookupRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Run domain lookup AND update our database with the tenant associations.
    Uses multi-strategy matching and auto-updates microsoft_tenant_id when
    a match is found by other means.
    """
    service = DomainLookupService()
    clean_domains = clean_domain_list(request.domains)

    if not clean_domains:
        raise HTTPException(status_code=400, detail="No valid domains provided")

    if len(clean_domains) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 domains per request")

    ms_results = await service.check_domains_bulk(clean_domains)

    updated = []
    already_linked = []
    tenant_id_updated = []
    no_tenant_match = []
    not_in_db = []
    not_connected = []

    for ms_result in ms_results:
        if not ms_result.is_connected:
            not_connected.append(ms_result.domain)
            continue

        # Find domain in our DB
        db_domain = await get_domain_record(db, ms_result.domain)
        if not db_domain:
            not_in_db.append(ms_result.domain)
            logger.info(f"[sync] {ms_result.domain}: domain not in our DB")
            continue

        # Multi-strategy tenant matching
        db_tenant, match_method = await find_tenant_multi_strategy(
            db=db,
            domain_name=ms_result.domain,
            ms_tenant_id=ms_result.microsoft_tenant_id,
            org_name=ms_result.organization_name,
            db_domain=db_domain,
        )

        if not db_tenant:
            no_tenant_match.append({
                "domain": ms_result.domain,
                "microsoft_tenant_id": ms_result.microsoft_tenant_id,
                "organization_name": ms_result.organization_name,
            })
            logger.info(
                f"[sync] {ms_result.domain}: no tenant match found "
                f"(ms_id={ms_result.microsoft_tenant_id}, org={ms_result.organization_name})"
            )
            continue

        # Auto-update microsoft_tenant_id on the tenant if we matched by other means
        # and the stored ID differs or is missing
        if (
            ms_result.microsoft_tenant_id
            and match_method != "tenant_id"
            and db_tenant.microsoft_tenant_id != ms_result.microsoft_tenant_id
        ):
            old_ms_id = db_tenant.microsoft_tenant_id
            db_tenant.microsoft_tenant_id = ms_result.microsoft_tenant_id
            tenant_id_updated.append({
                "domain": ms_result.domain,
                "tenant_name": db_tenant.name,
                "old_microsoft_tenant_id": old_ms_id,
                "new_microsoft_tenant_id": ms_result.microsoft_tenant_id,
                "match_method": match_method,
            })
            logger.info(
                f"[sync] {ms_result.domain}: updated tenant {db_tenant.name} "
                f"microsoft_tenant_id: {old_ms_id} → {ms_result.microsoft_tenant_id} "
                f"(matched via {match_method})"
            )

        # Check if domain→tenant link needs updating
        if db_domain.tenant_id == db_tenant.id:
            already_linked.append({
                "domain": ms_result.domain,
                "tenant_id": str(db_tenant.id),
                "tenant_name": db_tenant.name,
                "match_method": match_method,
            })
            logger.info(
                f"[sync] {ms_result.domain}: already linked to {db_tenant.name} "
                f"(matched via {match_method})"
            )
            continue

        # Update the domain → tenant link
        old_tenant_id = str(db_domain.tenant_id) if db_domain.tenant_id else None
        db_domain.tenant_id = db_tenant.id
        updated.append({
            "domain": ms_result.domain,
            "tenant_id": str(db_tenant.id),
            "tenant_name": db_tenant.name,
            "microsoft_tenant_id": ms_result.microsoft_tenant_id,
            "match_method": match_method,
            "previous_tenant_id": old_tenant_id,
        })
        logger.info(
            f"[sync] {ms_result.domain}: linked to {db_tenant.name} "
            f"(matched via {match_method}), was {old_tenant_id}"
        )

    await db.commit()

    return {
        "total_checked": len(ms_results),
        "updated_links": len(updated),
        "already_linked": len(already_linked),
        "tenant_ids_updated": len(tenant_id_updated),
        "no_tenant_match": len(no_tenant_match),
        "not_in_db": len(not_in_db),
        "not_connected": len(not_connected),
        "updates": updated,
        "already_linked_details": already_linked,
        "tenant_id_updated_details": tenant_id_updated,
        "no_tenant_match_details": no_tenant_match,
    }
