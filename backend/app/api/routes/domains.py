from __future__ import annotations

import asyncio
import csv
import io
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_cloudflare_service, get_db
from app.models.domain import Domain, DomainStatus
from app.schemas.domain import (
    BulkImportResult,
    BulkZoneResult,
    CloudflareZoneSetupRequest,
    CloudflareZoneSetupResult,
    DomainCreate,
    DomainRead,
    DomainUpdate,
    NameserverGroup,
)
from app.services.cloudflare import CloudflareError, CloudflareService

router = APIRouter(prefix="/api/v1/domains", tags=["domains"])

DOMAIN_REGEX = re.compile(r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$")


def validate_domain_name(name: str) -> None:
    """Validate domain name format."""
    if not DOMAIN_REGEX.match(name):
        raise HTTPException(status_code=400, detail="Invalid domain name format")


def extract_tld(domain_name: str) -> str:
    """Extract TLD from domain name."""
    parts = domain_name.rsplit(".", 1)
    return f".{parts[-1]}" if len(parts) > 1 else ""


def normalize_domain_input(value: str) -> str:
    """Normalize pasted domain or URL input to a bare domain."""
    domain = value.strip().lower().rstrip(".")
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/", 1)[0]
    domain = domain.split("?", 1)[0]
    domain = domain.rsplit("@", 1)[-1]
    domain = domain.split(":", 1)[0]
    return domain


def normalize_redirect_url(value: str | None) -> str | None:
    """Normalize a redirect target URL."""
    if not value:
        return None

    redirect_url = value.strip()
    if not redirect_url:
        return None

    if not re.match(r"^https?://", redirect_url, flags=re.IGNORECASE):
        redirect_url = f"https://{redirect_url}"

    return redirect_url


async def get_domain_or_404(domain_id: UUID, db: AsyncSession) -> Domain:
    """Get domain by ID or raise 404."""
    result = await db.execute(select(Domain).where(Domain.id == domain_id))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    return domain


@router.get("/", response_model=list[DomainRead])
async def list_domains(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    status: DomainStatus | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[Domain]:
    """List all domains with optional status filter."""
    query = select(Domain)
    if status:
        query = query.where(Domain.status == status)
    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/nameserver-groups")
async def get_nameserver_groups(
    status: Optional[str] = Query(default="zone_created"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Get all domains grouped by their assigned nameservers.
    Used for display so user can easily bulk-update at Porkbun.
    
    NOTE: Nameservers are stored in cloudflare_nameservers as a JSONB array.
    
    Query approach:
    1. Get all domains with the specified status
    2. Group by cloudflare_nameservers (convert to tuple/string for grouping)
    3. Return grouped results
    
    Returns:
    {
        "groups": [
            {
                "nameservers": ["anna.ns.cloudflare.com", "bob.ns.cloudflare.com"],
                "domain_count": 288,
                "domains": ["domain1.com", "domain2.com", "domain3.net", ...]
            },
            {
                "nameservers": ["carl.ns.cloudflare.com", "dana.ns.cloudflare.com"],
                "domain_count": 12,
                "domains": ["domain4.com", "domain5.io", ...]
            }
        ],
        "total_domains": 300,
        "status_filter": "zone_created"
    }
    """
    # Build query with optional status filter
    query = select(Domain)
    
    if status:
        # Convert string status to DomainStatus enum
        try:
            status_enum = DomainStatus(status)
            query = query.where(Domain.status == status_enum)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status: {status}. Valid values: {[s.value for s in DomainStatus]}"
            )
    
    result = await db.execute(query)
    domains = list(result.scalars().all())
    
    # Group domains by nameservers using defaultdict
    ns_groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    
    for domain in domains:
        # Only include domains that have nameservers assigned
        if domain.cloudflare_nameservers:
            # Sort nameservers to ensure consistent grouping key
            ns_key = tuple(sorted(domain.cloudflare_nameservers))
            ns_groups[ns_key].append(domain.name)
    
    # Build response groups
    groups: List[NameserverGroup] = []
    for ns_tuple, domain_list in ns_groups.items():
        groups.append(
            NameserverGroup(
                nameservers=list(ns_tuple),
                domain_count=len(domain_list),
                domains=sorted(domain_list),  # Sort domains alphabetically
            )
        )
    
    # Sort groups by domain count descending (most common NS pairs first)
    groups.sort(key=lambda g: g.domain_count, reverse=True)
    
    # Calculate total domains with nameservers assigned
    total_with_ns = sum(g.domain_count for g in groups)
    
    return {
        "groups": groups,
        "total_domains": total_with_ns,
        "status_filter": status,
    }


@router.post("/cloudflare-zone-setup", response_model=CloudflareZoneSetupResult)
async def setup_cloudflare_zones_only(
    request: CloudflareZoneSetupRequest,
    db: AsyncSession = Depends(get_db),
    cf_service: CloudflareService = Depends(get_cloudflare_service),
) -> CloudflareZoneSetupResult:
    """
    Standalone Cloudflare zone setup.

    Creates or reuses Cloudflare zones, optionally configures redirect rules,
    and returns registrar nameservers.
    """
    seen: set[str] = set()
    domain_names: list[str] = []
    results: list[dict[str, Any]] = []
    redirect_url = normalize_redirect_url(request.redirect_url)

    for raw_domain in request.domains:
        domain_name = normalize_domain_input(raw_domain)
        if not domain_name or domain_name in seen:
            continue

        seen.add(domain_name)

        if not DOMAIN_REGEX.match(domain_name):
            results.append(
                {
                    "domain": domain_name or raw_domain,
                    "success": False,
                    "zone_id": None,
                    "nameservers": [],
                    "zone_status": None,
                    "already_existed": False,
                    "account_label": None,
                    "database_action": None,
                    "redirect_url": redirect_url,
                    "redirect_configured": False,
                    "redirect_error": None,
                    "phase1_dns": None,
                    "error": "Invalid domain name format",
                }
            )
            continue

        domain_names.append(domain_name)

    if len(domain_names) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 domains per request")

    if domain_names:
        existing_query = await db.execute(select(Domain).where(Domain.name.in_(domain_names)))
        existing_domains = {domain.name: domain for domain in existing_query.scalars().all()}

        cf_results = await cf_service.bulk_setup_zones_only(domain_names)
        early_statuses = {
            DomainStatus.PURCHASED,
            DomainStatus.CF_ZONE_PENDING,
            DomainStatus.CF_ZONE_ACTIVE,
            DomainStatus.ERROR,
            DomainStatus.PROBLEM,
        }

        for cf_result in cf_results:
            domain_name = cf_result["domain"]
            domain_obj = existing_domains.get(domain_name)
            cf_result["database_action"] = None
            cf_result["redirect_url"] = redirect_url
            cf_result["redirect_configured"] = False
            cf_result["redirect_error"] = None
            cf_result["phase1_dns"] = None
            cf_result["redirect_result"] = None

            if cf_result.get("success"):
                zone_status = cf_result.get("zone_status") or "pending"
                nameservers = cf_result.get("nameservers") or []
                next_status = (
                    DomainStatus.CF_ZONE_ACTIVE
                    if zone_status == "active"
                    else DomainStatus.CF_ZONE_PENDING
                )

                if domain_obj:
                    domain_obj.cloudflare_zone_id = cf_result.get("zone_id")
                    domain_obj.cloudflare_nameservers = nameservers
                    domain_obj.cloudflare_zone_status = zone_status
                    domain_obj.error_message = None
                    if redirect_url:
                        domain_obj.redirect_url = redirect_url
                        domain_obj.redirect_configured = False
                    if domain_obj.status in early_statuses:
                        domain_obj.status = next_status
                        cf_result["database_action"] = "updated"
                    else:
                        cf_result["database_action"] = "updated_status_preserved"
                else:
                    domain_obj = Domain(
                        name=domain_name,
                        tld=extract_tld(domain_name),
                        status=next_status,
                        cloudflare_zone_id=cf_result.get("zone_id"),
                        cloudflare_nameservers=nameservers,
                        cloudflare_zone_status=zone_status,
                        redirect_url=redirect_url,
                        redirect_configured=False,
                    )
                    db.add(domain_obj)
                    cf_result["database_action"] = "created"

                if redirect_url and cf_result.get("zone_id"):
                    try:
                        phase1_result = await cf_service.create_phase1_dns(
                            cf_result["zone_id"],
                            domain_name,
                        )
                        cf_result["phase1_dns"] = phase1_result

                        redirect_result = await cf_service.create_redirect_rule(
                            cf_result["zone_id"],
                            domain_name,
                            redirect_url,
                        )
                        cf_result["redirect_result"] = redirect_result
                        cf_result["redirect_configured"] = redirect_result.get("success", False)
                        if domain_obj:
                            domain_obj.redirect_configured = cf_result["redirect_configured"]
                    except Exception as e:
                        cf_result["success"] = False
                        cf_result["redirect_error"] = str(e)
                        cf_result["error"] = f"Redirect setup failed: {e}"
                        if domain_obj:
                            domain_obj.redirect_configured = False
                            domain_obj.error_message = cf_result["error"]
            elif domain_obj and domain_obj.status in early_statuses:
                domain_obj.status = DomainStatus.ERROR
                domain_obj.error_message = cf_result.get("error", "Cloudflare zone setup failed")
                cf_result["database_action"] = "error_recorded"

            results.append(cf_result)

        await db.commit()

    ns_groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for result in results:
        nameservers = result.get("nameservers") or []
        if result.get("zone_id") and nameservers:
            ns_groups[tuple(sorted(nameservers))].append(result["domain"])

    nameserver_groups = [
        NameserverGroup(
            nameservers=list(ns_tuple),
            domain_count=len(domain_list),
            domains=sorted(domain_list),
        )
        for ns_tuple, domain_list in ns_groups.items()
    ]
    nameserver_groups.sort(key=lambda group: group.domain_count, reverse=True)

    success_count = sum(1 for result in results if result.get("success"))

    return CloudflareZoneSetupResult(
        total=len(results),
        success=success_count,
        failed=len(results) - success_count,
        results=results,
        nameserver_groups=nameserver_groups,
    )


@router.post("/bulk-import", response_model=BulkImportResult)
async def bulk_import_domains(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> BulkImportResult:
    """
    Import domains from CSV file.
    
    CSV Format (header required):
    domain_name,registrar,registration_date,redirect_url
    example1.com,Porkbun,2025-01-15,https://mainbusiness.com
    example2.com,Porkbun,2025-01-15,https://mainbusiness.com
    example3.net,Porkbun,2025-01-15,https://anotherbusiness.com
    
    Note: redirect_url column is optional. If not provided, defaults to None.
    
    Processing:
    1. Parse CSV file (handle encoding issues)
    2. Validate each domain format using regex
    3. Check for duplicates already in database (skip them)
    4. Extract TLD from domain name
    5. Bulk insert valid domains with status="purchased"
    6. Return detailed results
    """
    results: list[dict[str, Any]] = []
    domains_to_create: list[Domain] = []
    created_count = 0
    skipped_count = 0
    failed_count = 0
    
    # Read and decode file content
    try:
        content = await file.read()
        # Try UTF-8 first, fall back to latin-1
        try:
            decoded_content = content.decode("utf-8")
        except UnicodeDecodeError:
            decoded_content = content.decode("latin-1")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read file: {e}")
    
    # Parse CSV
    try:
        csv_reader = csv.DictReader(io.StringIO(decoded_content))
        rows = list(csv_reader)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse CSV: {e}")
    
    if not rows:
        raise HTTPException(status_code=400, detail="CSV file is empty or has no data rows")
    
    # Get all domain names from CSV for bulk duplicate check
    domain_names_from_csv: list[str] = []
    for row in rows:
        domain_name = row.get("domain_name", "").strip().lower()
        if domain_name:
            domain_names_from_csv.append(domain_name)
    
    # Bulk check for existing domains in database
    existing_query = await db.execute(
        select(Domain.name).where(Domain.name.in_(domain_names_from_csv))
    )
    existing_domains = set(existing_query.scalars().all())
    
    # Process each row
    for row in rows:
        domain_name = row.get("domain_name", "").strip().lower()
        
        if not domain_name:
            continue  # Skip empty rows
        
        # Validate domain format
        if not DOMAIN_REGEX.match(domain_name):
            results.append({
                "domain": domain_name,
                "status": "failed",
                "reason": "Invalid domain format"
            })
            failed_count += 1
            continue
        
        # Check if already exists in database
        if domain_name in existing_domains:
            results.append({
                "domain": domain_name,
                "status": "skipped",
                "reason": "Already exists"
            })
            skipped_count += 1
            continue
        
        # Check for duplicates within the CSV file
        if any(d.name == domain_name for d in domains_to_create):
            results.append({
                "domain": domain_name,
                "status": "skipped",
                "reason": "Duplicate in CSV"
            })
            skipped_count += 1
            continue
        
        # Get redirect_url from CSV (optional column)
        redirect_url = row.get("redirect_url", "").strip() or None
        
        # Create domain object
        domain = Domain(
            name=domain_name,
            tld=extract_tld(domain_name),
            status=DomainStatus.PURCHASED,
            cloudflare_nameservers=[],
            cloudflare_zone_status="pending",
            redirect_url=redirect_url,
        )
        domains_to_create.append(domain)
        results.append({
            "domain": domain_name,
            "status": "created",
            "reason": None
        })
        created_count += 1
    
    # Bulk insert all valid domains
    if domains_to_create:
        try:
            db.add_all(domains_to_create)
            await db.commit()
        except Exception as e:
            await db.rollback()
            # Mark all 'created' as failed
            for result in results:
                if result["status"] == "created":
                    result["status"] = "failed"
                    result["reason"] = f"Database error: {e}"
                    created_count -= 1
                    failed_count += 1
    
    return BulkImportResult(
        total=len(rows),
        created=created_count,
        skipped=skipped_count,
        failed=failed_count,
        results=results
    )


@router.post("/", response_model=DomainRead, status_code=201)
async def create_domain(
    domain_in: DomainCreate,
    db: AsyncSession = Depends(get_db),
    cf_service: CloudflareService = Depends(get_cloudflare_service),
) -> Domain:
    """
    Add a new domain.
    Creates domain in DB and Cloudflare zone.
    Returns domain with nameservers.
    """
    validate_domain_name(domain_in.name)

    # Check if domain already exists
    existing = await db.execute(select(Domain).where(Domain.name == domain_in.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Domain already exists")

    # Create domain in DB with initial status
    domain = Domain(
        name=domain_in.name,
        tld=extract_tld(domain_in.name),
        status=DomainStatus.PURCHASED,
        cloudflare_nameservers=[],
        cloudflare_zone_status="pending",
    )
    db.add(domain)
    await db.commit()
    await db.refresh(domain)

    # Create Cloudflare zone
    try:
        cf_result = await cf_service.create_zone(domain_in.name)
        domain.cloudflare_zone_id = cf_result["zone_id"]
        domain.cloudflare_nameservers = cf_result["nameservers"]
        domain.cloudflare_zone_status = cf_result["status"]
        domain.status = DomainStatus.CF_ZONE_PENDING
        await db.commit()
        await db.refresh(domain)
    except CloudflareError as e:
        # Log the full error for debugging
        print(f"[CLOUDFLARE ERROR] {e}")
        print(f"[CLOUDFLARE ERROR] Status: {e.status_code}")
        print(f"[CLOUDFLARE ERROR] Response: {e.response_body}")
        # Rollback domain creation on CF failure
        await db.delete(domain)
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Cloudflare error: {e}") from e

    return domain


@router.get("/{domain_id}", response_model=DomainRead)
async def get_domain(
    domain_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> Domain:
    """Get a single domain by ID."""
    return await get_domain_or_404(domain_id, db)


@router.patch("/{domain_id}", response_model=DomainRead)
async def update_domain(
    domain_id: UUID,
    domain_in: DomainUpdate,
    db: AsyncSession = Depends(get_db),
) -> Domain:
    """Update domain fields."""
    domain = await get_domain_or_404(domain_id, db)

    update_data = domain_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(domain, field, value)

    await db.commit()
    await db.refresh(domain)
    return domain


@router.delete("/{domain_id}")
async def delete_domain(
    domain_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Soft delete domain by setting status to retired."""
    domain = await get_domain_or_404(domain_id, db)
    domain.status = DomainStatus.RETIRED
    await db.commit()
    return {"message": "Domain retired"}


@router.post("/bulk-delete")
async def bulk_delete_domains(
    domain_ids: list[UUID],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | int]:
    """Soft delete multiple domains by setting status to retired."""
    deleted_count = 0
    for domain_id in domain_ids:
        result = await db.execute(select(Domain).where(Domain.id == domain_id))
        domain = result.scalar_one_or_none()
        if domain:
            domain.status = DomainStatus.RETIRED
            deleted_count += 1
    await db.commit()
    return {"message": f"{deleted_count} domains retired", "count": deleted_count}


@router.post("/{domain_id}/confirm-ns", response_model=DomainRead)
async def confirm_nameservers(
    domain_id: UUID,
    db: AsyncSession = Depends(get_db),
    cf_service: CloudflareService = Depends(get_cloudflare_service),
) -> Domain:
    """
    User confirms nameserver update at registrar.
    Checks Cloudflare zone status.
    """
    domain = await get_domain_or_404(domain_id, db)

    if not domain.cloudflare_zone_id:
        raise HTTPException(status_code=400, detail="No Cloudflare zone associated")

    try:
        zone_status = await cf_service.get_zone_status(domain.cloudflare_zone_id)
    except CloudflareError as e:
        raise HTTPException(status_code=502, detail=f"Cloudflare error: {e}") from e

    domain.cloudflare_zone_status = zone_status

    if zone_status == "active":
        domain.nameservers_updated = True
        from datetime import datetime, timezone
        domain.nameservers_updated_at = datetime.now(timezone.utc)
        domain.status = DomainStatus.CF_ZONE_ACTIVE
    else:
        # Still pending
        domain.status = DomainStatus.NS_PROPAGATING

    await db.commit()
    await db.refresh(domain)
    return domain


@router.post("/{domain_id}/create-dns", response_model=DomainRead)
async def create_dns_records(
    domain_id: UUID,
    db: AsyncSession = Depends(get_db),
    cf_service: CloudflareService = Depends(get_cloudflare_service),
) -> Domain:
    """
    Create MX, SPF, DMARC DNS records.
    Prerequisite: Zone must be active.
    """
    domain = await get_domain_or_404(domain_id, db)

    if not domain.cloudflare_zone_id:
        raise HTTPException(status_code=400, detail="No Cloudflare zone associated")

    if domain.cloudflare_zone_status != "active":
        raise HTTPException(
            status_code=409,
            detail="Zone is not active. Please confirm nameservers first.",
        )

    domain.status = DomainStatus.DNS_CONFIGURING
    await db.commit()

    try:
        await cf_service.create_all_dns_records(domain.cloudflare_zone_id, domain.name)
        domain.dns_records_created = True
        domain.mx_configured = True
        domain.spf_configured = True
        domain.dmarc_configured = True
        domain.status = DomainStatus.PENDING_M365
    except CloudflareError as e:
        domain.status = DomainStatus.PROBLEM
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Cloudflare error: {e}") from e

    await db.commit()
    await db.refresh(domain)
    return domain


@router.get("/{domain_id}/status")
async def check_domain_status(
    domain_id: UUID,
    db: AsyncSession = Depends(get_db),
    cf_service: CloudflareService = Depends(get_cloudflare_service),
) -> dict[str, str | bool]:
    """
    Check current domain status including Cloudflare zone status.
    """
    domain = await get_domain_or_404(domain_id, db)

    zone_status = "unknown"
    if domain.cloudflare_zone_id:
        try:
            zone_status = await cf_service.get_zone_status(domain.cloudflare_zone_id)
            domain.cloudflare_zone_status = zone_status
            await db.commit()
        except CloudflareError:
            zone_status = "error"

    return {
        "status": domain.status.value,
        "zone_status": zone_status,
        "dns_configured": domain.dns_records_created,
    }


@router.post("/bulk-create-zones", response_model=BulkZoneResult)
async def bulk_create_zones(
    domain_ids: Optional[List[UUID]] = Body(default=None),
    db: AsyncSession = Depends(get_db),
    cf_service: CloudflareService = Depends(get_cloudflare_service),
) -> BulkZoneResult:
    """
    Create Cloudflare zones for multiple domains and add Phase 1 DNS records.
    
    If domain_ids is None, process ALL domains with status="purchased".
    
    For EACH domain:
    1. Create Cloudflare zone (get zone_id and nameservers)
    2. Immediately create Phase 1 DNS records:
       - CNAME @ -> www.{domain} (PROXIED for redirect)
       - TXT _dmarc -> "v=DMARC1; p=none;"
    3. Update domain record:
       - cloudflare_zone_id = zone_id
       - cloudflare_nameservers = [ns1, ns2]
       - cloudflare_zone_status = "pending"
       - phase1_cname_added = True
       - phase1_dmarc_added = True
       - status = "zone_created"
    4. Rate limit: wait 0.25s between domains (Cloudflare limit)
    
    After processing all domains, GROUP results by nameserver.
    
    Returns:
    {
        "total": 100,
        "success": 98,
        "failed": 2,
        "results": [
            {"domain": "example1.com", "success": true, "zone_id": "abc", "nameservers": ["anna.ns...", "bob.ns..."]},
            {"domain": "example2.com", "success": false, "error": "Rate limited"}
        ],
        "nameserver_groups": [
            {
                "nameservers": ["anna.ns.cloudflare.com", "bob.ns.cloudflare.com"],
                "domain_count": 95,
                "domains": ["example1.com", "example2.com", ...]
            },
            {
                "nameservers": ["carl.ns.cloudflare.com", "dana.ns.cloudflare.com"],
                "domain_count": 3,
                "domains": ["example99.com", ...]
            }
        ]
    }
    
    The nameserver_groups are crucial - user will use these to bulk-update NS at Porkbun.
    """
    # Step 1: Query domains to process
    if domain_ids is None:
        # Get ALL domains with status="purchased"
        query = select(Domain).where(Domain.status == DomainStatus.PURCHASED)
        result = await db.execute(query)
        domains = list(result.scalars().all())
    else:
        # Get specific domains by ID
        query = select(Domain).where(Domain.id.in_(domain_ids))
        result = await db.execute(query)
        domains = list(result.scalars().all())
        
        # Validate all requested domains exist
        found_ids = {d.id for d in domains}
        missing_ids = set(domain_ids) - found_ids
        if missing_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Domains not found: {[str(id) for id in missing_ids]}"
            )
    
    if not domains:
        return BulkZoneResult(
            total=0,
            success=0,
            failed=0,
            results=[],
            nameserver_groups=[]
        )
    
    # Build a mapping from domain name to Domain object for DB updates
    domain_map: dict[str, Domain] = {d.name: d for d in domains}
    domain_names = list(domain_map.keys())
    
    # Step 2: Call Cloudflare service for bulk zone creation
    # This handles rate limiting (0.25s between calls) and Phase 1 DNS
    cf_results = await cf_service.bulk_create_zones(domain_names)
    
    # Step 3: Process results and update database
    results: List[dict[str, Any]] = []
    success_count = 0
    failed_count = 0
    
    # For nameserver grouping
    ns_groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    
    for cf_result in cf_results:
        domain_name = cf_result["domain"]
        domain_obj = domain_map.get(domain_name)
        
        if cf_result["success"] and domain_obj:
            # Update database record
            domain_obj.cloudflare_zone_id = cf_result["zone_id"]
            domain_obj.cloudflare_nameservers = cf_result["nameservers"]
            domain_obj.cloudflare_zone_status = "pending"
            domain_obj.status = DomainStatus.ZONE_CREATED
            
            # Track Phase 1 DNS status
            if cf_result.get("phase1_dns"):
                domain_obj.phase1_cname_added = cf_result["phase1_dns"].get("cname_created", False)
                domain_obj.phase1_dmarc_added = cf_result["phase1_dns"].get("dmarc_created", False)
            
            # Clear any previous error
            domain_obj.error_message = None
            
            success_count += 1
            
            # Group by nameservers (sort to ensure consistent tuple key)
            ns_key = tuple(sorted(cf_result["nameservers"]))
            ns_groups[ns_key].append(domain_name)
            
            results.append({
                "domain": domain_name,
                "success": True,
                "zone_id": cf_result["zone_id"],
                "nameservers": cf_result["nameservers"],
                "phase1_dns": cf_result.get("phase1_dns"),
            })
        else:
            # Failed - update error status
            if domain_obj:
                domain_obj.status = DomainStatus.ERROR
                domain_obj.error_message = cf_result.get("error", "Unknown error")
            
            failed_count += 1
            results.append({
                "domain": domain_name,
                "success": False,
                "error": cf_result.get("error", "Unknown error"),
            })
    
    # Commit all database changes
    await db.commit()
    
    # Step 4: Build nameserver groups for response
    nameserver_groups: List[NameserverGroup] = []
    for ns_tuple, domain_list in ns_groups.items():
        nameserver_groups.append(
            NameserverGroup(
                nameservers=list(ns_tuple),
                domain_count=len(domain_list),
                domains=sorted(domain_list),
            )
        )
    
    # Sort groups by domain count descending (most common NS pairs first)
    nameserver_groups.sort(key=lambda g: g.domain_count, reverse=True)
    
    return BulkZoneResult(
        total=len(domains),
        success=success_count,
        failed=failed_count,
        results=results,
        nameserver_groups=nameserver_groups,
    )


@router.post("/check-propagation")
async def check_ns_propagation(
    domain_ids: Optional[List[UUID]] = Body(default=None),
    db: AsyncSession = Depends(get_db),
    cf_service: CloudflareService = Depends(get_cloudflare_service),
) -> dict[str, Any]:
    """
    Check if nameservers have propagated for domains.
    
    If domain_ids is None, check ALL domains with status="zone_created".
    
    For EACH domain:
    1. Get expected nameservers from cloudflare_nameservers (JSONB array)
    2. Do DNS lookup using cloudflare_service.check_ns_propagation()
    3. If propagated:
       - Update status = "ns_propagated"
       - Set ns_propagated_at = now
       - Also check Cloudflare zone status via API (should be "active")
    4. If not propagated: leave status unchanged
    
    Returns:
    {
        "total_checked": 300,
        "propagated": 285,
        "pending": 15,
        "propagated_domains": ["domain1.com", "domain2.com", ...],
        "pending_domains": ["domain5.com", "domain8.com", ...]
    }
    """
    # Step 1: Query domains to check
    if domain_ids is None:
        # Get ALL domains with status="zone_created"
        query = select(Domain).where(Domain.status == DomainStatus.ZONE_CREATED)
        result = await db.execute(query)
        domains = list(result.scalars().all())
    else:
        # Get specific domains by ID
        query = select(Domain).where(Domain.id.in_(domain_ids))
        result = await db.execute(query)
        domains = list(result.scalars().all())
        
        # Validate all requested domains exist
        found_ids = {d.id for d in domains}
        missing_ids = set(domain_ids) - found_ids
        if missing_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Domains not found: {[str(id) for id in missing_ids]}"
            )
    
    if not domains:
        return {
            "total_checked": 0,
            "propagated": 0,
            "pending": 0,
            "propagated_domains": [],
            "pending_domains": [],
        }
    
    # Step 2: Check propagation for each domain
    propagated_domains: List[str] = []
    pending_domains: List[str] = []
    
    for i, domain in enumerate(domains):
        # Skip domains without nameservers assigned
        if not domain.cloudflare_nameservers:
            pending_domains.append(domain.name)
            continue
        
        # Check if NS have propagated via DNS lookup
        is_propagated = await cf_service.check_ns_propagation(
            domain.name, 
            domain.cloudflare_nameservers
        )
        
        if is_propagated:
            # Also verify Cloudflare zone status if we have a zone_id
            zone_active = False
            if domain.cloudflare_zone_id:
                try:
                    zone_status = await cf_service.get_zone_status(domain.cloudflare_zone_id)
                    zone_active = zone_status == "active"
                    domain.cloudflare_zone_status = zone_status
                except CloudflareError:
                    # If we can't check zone status, just rely on DNS check
                    zone_active = True  # Assume active if DNS propagated
            else:
                zone_active = True  # No zone to check
            
            if zone_active:
                # Update domain status
                domain.status = DomainStatus.NS_PROPAGATED
                domain.ns_propagated_at = datetime.now(timezone.utc)
                domain.nameservers_updated = True
                propagated_domains.append(domain.name)
            else:
                # DNS propagated but zone not active yet
                pending_domains.append(domain.name)
        else:
            pending_domains.append(domain.name)
        
        # Rate limit DNS queries - wait 0.1s between domains
        if i < len(domains) - 1:
            await asyncio.sleep(0.1)
    
    # Commit all database changes
    await db.commit()
    
    return {
        "total_checked": len(domains),
        "propagated": len(propagated_domains),
        "pending": len(pending_domains),
        "propagated_domains": sorted(propagated_domains),
        "pending_domains": sorted(pending_domains),
    }


@router.post("/bulk-set-redirect")
async def bulk_set_redirect(
    redirect_url: str = Body(..., embed=True),
    domain_ids: Optional[List[UUID]] = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Set redirect URL for multiple domains at once.
    
    If domain_ids is None, apply to ALL domains that don't have a redirect_url set.
    
    Useful when all cold email domains should redirect to the same main site.
    
    Request body:
    {
        "redirect_url": "https://mainbusiness.com",
        "domain_ids": ["uuid1", "uuid2", ...] // optional
    }
    
    Returns:
    {
        "updated_count": 150,
        "redirect_url": "https://mainbusiness.com",
        "domains": ["domain1.com", "domain2.com", ...]
    }
    """
    # Step 1: Query domains to update
    if domain_ids is None:
        # Update ALL domains that don't have a redirect_url set
        query = select(Domain).where(Domain.redirect_url.is_(None))
        result = await db.execute(query)
        domains = list(result.scalars().all())
    else:
        # Update specific domains by ID
        query = select(Domain).where(Domain.id.in_(domain_ids))
        result = await db.execute(query)
        domains = list(result.scalars().all())
        
        # Validate all requested domains exist
        found_ids = {d.id for d in domains}
        missing_ids = set(domain_ids) - found_ids
        if missing_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Domains not found: {[str(id) for id in missing_ids]}"
            )
    
    if not domains:
        return {
            "updated_count": 0,
            "redirect_url": redirect_url,
            "domains": [],
        }
    
    # Step 2: Update redirect_url for all matched domains
    updated_domain_names: List[str] = []
    for domain in domains:
        domain.redirect_url = redirect_url
        updated_domain_names.append(domain.name)
    
    # Commit all database changes
    await db.commit()
    
    return {
        "updated_count": len(updated_domain_names),
        "redirect_url": redirect_url,
        "domains": sorted(updated_domain_names),
    }


@router.post("/bulk-setup-redirects")
async def bulk_setup_redirects(
    domain_ids: Optional[List[UUID]] = Body(default=None),
    db: AsyncSession = Depends(get_db),
    cf_service: CloudflareService = Depends(get_cloudflare_service),
) -> dict[str, Any]:
    """
    Setup Cloudflare redirect rules for domains.
    
    If domain_ids is None, process all domains where:
    - cloudflare_zone_id is set (zone exists)
    - redirect_url is set
    - redirect_configured = False (not yet configured)
    
    For EACH domain:
    1. Get zone_id and redirect_url from domain record
    2. Create Cloudflare redirect rule
    3. Update domain: redirect_configured = True
    
    Returns: {
        "total": 100,
        "success": 98,
        "failed": 2,
        "results": [
            {"domain": "example1.com", "success": true, "redirect_url": "https://main.com"},
            {"domain": "example2.com", "success": false, "error": "Rate limited"}
        ]
    }
    """
    # Step 1: Query domains to process
    if domain_ids is None:
        # Get all domains that have zone_id, redirect_url, but not yet configured
        query = select(Domain).where(
            Domain.cloudflare_zone_id.isnot(None),
            Domain.redirect_url.isnot(None),
            Domain.redirect_configured == False,
        )
        result = await db.execute(query)
        domains = list(result.scalars().all())
    else:
        # Get specific domains by ID
        query = select(Domain).where(Domain.id.in_(domain_ids))
        result = await db.execute(query)
        domains = list(result.scalars().all())
        
        # Validate all requested domains exist
        found_ids = {d.id for d in domains}
        missing_ids = set(domain_ids) - found_ids
        if missing_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Domains not found: {[str(id) for id in missing_ids]}"
            )
        
        # Filter to only process domains with zone_id and redirect_url
        domains = [d for d in domains if d.cloudflare_zone_id and d.redirect_url]
    
    if not domains:
        return {
            "total": 0,
            "success": 0,
            "failed": 0,
            "results": [],
        }
    
    # Step 2: Build list for bulk redirect creation
    domains_to_process = [
        {
            "zone_id": d.cloudflare_zone_id,
            "domain": d.name,
            "redirect_url": d.redirect_url,
        }
        for d in domains
    ]
    
    # Create mapping for database updates
    domain_map: dict[str, Domain] = {d.name: d for d in domains}
    
    # Step 3: Call Cloudflare service for bulk redirect rule creation
    cf_results = await cf_service.bulk_create_redirect_rules(domains_to_process)
    
    # Step 4: Process results and update database
    results: List[dict[str, Any]] = []
    success_count = 0
    failed_count = 0
    
    for cf_result in cf_results:
        domain_name = cf_result["domain"]
        domain_obj = domain_map.get(domain_name)
        
        if cf_result["success"] and domain_obj:
            # Update database record
            domain_obj.redirect_configured = True
            domain_obj.error_message = None
            success_count += 1
            
            results.append({
                "domain": domain_name,
                "success": True,
                "redirect_url": cf_result["redirect_url"],
                "already_exists": cf_result.get("already_exists", False),
            })
        else:
            # Failed - record error
            if domain_obj:
                domain_obj.error_message = cf_result.get("error", "Unknown error")
            
            failed_count += 1
            results.append({
                "domain": domain_name,
                "success": False,
                "redirect_url": cf_result.get("redirect_url"),
                "error": cf_result.get("error", "Unknown error"),
            })
    
    # Commit all database changes
    await db.commit()
    
    return {
        "total": len(domains),
        "success": success_count,
        "failed": failed_count,
        "results": results,
    }
