from __future__ import annotations

import asyncio
import base64
import csv
import io
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db
from app.models.domain import Domain, DomainStatus
from app.models.tenant import Tenant, TenantStatus
from app.schemas.tenant import TenantCreate, TenantRead, TenantUpdate
from app.services.cloudflare import CloudflareService, CloudflareError
from app.services.microsoft import MicrosoftGraphService, MicrosoftGraphError
from app.services.powershell import PowerShellService, PowerShellError
from app.services.tenant_import import (
    _is_totp_secret_column,
    _normalize_totp_secret,
    tenant_import_service,
)

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])


def encrypt_password(password: str) -> str:
    """Simple base64 encoding for MVP. Replace with proper encryption in production."""
    return base64.b64encode(password.encode()).decode()


async def get_tenant_or_404(tenant_id: UUID, db: AsyncSession) -> Tenant:
    """Get tenant by ID or raise 404."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@router.get("/", response_model=list[TenantRead])
async def list_tenants(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    status: TenantStatus | None = Query(default=None),
    provider: str | None = Query(default=None),
    batch_id: UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[Tenant]:
    """List all tenants with optional filters."""
    query = select(Tenant)
    if batch_id:
        query = query.where(Tenant.batch_id == batch_id)
    if status:
        query = query.where(Tenant.status == status)
    if provider:
        query = query.where(Tenant.provider == provider)
    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/", response_model=TenantRead, status_code=201)
async def create_tenant(
    tenant_in: TenantCreate,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Create/import a new tenant."""
    # Check if tenant already exists by microsoft_tenant_id
    existing = await db.execute(
        select(Tenant).where(Tenant.microsoft_tenant_id == tenant_in.microsoft_tenant_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Tenant with this Microsoft ID already exists")

    # Check if onmicrosoft_domain is unique
    existing_domain = await db.execute(
        select(Tenant).where(Tenant.onmicrosoft_domain == tenant_in.onmicrosoft_domain)
    )
    if existing_domain.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Tenant with this onmicrosoft domain already exists")

    tenant = Tenant(
        microsoft_tenant_id=tenant_in.microsoft_tenant_id,
        name=tenant_in.name,
        onmicrosoft_domain=tenant_in.onmicrosoft_domain,
        provider=tenant_in.provider,
        admin_email=tenant_in.admin_email,
        admin_password=encrypt_password(tenant_in.admin_password),
        licensed_user_upn=tenant_in.licensed_user_upn or "",
        status=TenantStatus.NEW,
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.get("/{tenant_id}", response_model=TenantRead)
async def get_tenant(
    tenant_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get a single tenant by ID, including all linked domains."""
    tenant = await get_tenant_or_404(tenant_id, db)

    # Fetch ALL domains linked to this tenant (multi-domain support)
    linked_domains_result = await db.execute(
        select(Domain)
        .where(Domain.tenant_id == tenant_id)
        .order_by(Domain.domain_index_in_tenant)
    )
    linked_domains = linked_domains_result.scalars().all()

    # Convert tenant ORM -> dict, then inject domains list
    from app.schemas.tenant import TenantRead as TenantReadSchema, DomainBrief
    tenant_data = TenantReadSchema.model_validate(tenant)
    tenant_data.domains = [DomainBrief.model_validate(d) for d in linked_domains]
    return tenant_data


@router.patch("/{tenant_id}", response_model=TenantRead)
async def update_tenant(
    tenant_id: UUID,
    tenant_in: TenantUpdate,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Update tenant fields."""
    tenant = await get_tenant_or_404(tenant_id, db)

    update_data = tenant_in.model_dump(exclude_unset=True)

    # Encrypt password if being updated
    if "admin_password" in update_data and update_data["admin_password"]:
        update_data["admin_password"] = encrypt_password(update_data["admin_password"])

    for field, value in update_data.items():
        setattr(tenant, field, value)

    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.delete("/{tenant_id}")
async def delete_tenant(
    tenant_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Soft delete tenant by setting status to retired."""
    tenant = await get_tenant_or_404(tenant_id, db)
    tenant.status = TenantStatus.RETIRED
    await db.commit()
    return {"message": "Tenant retired"}


@router.post("/bulk-import")
async def bulk_import_tenants(
    tenants_in: list[TenantCreate],
    db: AsyncSession = Depends(get_db),
) -> dict[str, int | list[dict[str, str]]]:
    """
    Bulk import multiple tenants.
    Returns count of created tenants and list of errors.
    """
    created = 0
    errors: list[dict[str, str]] = []

    for idx, tenant_data in enumerate(tenants_in):
        try:
            # Check for existing tenant
            existing = await db.execute(
                select(Tenant).where(Tenant.microsoft_tenant_id == tenant_data.microsoft_tenant_id)
            )
            if existing.scalar_one_or_none():
                errors.append({
                    "index": str(idx),
                    "microsoft_tenant_id": tenant_data.microsoft_tenant_id,
                    "error": "Tenant already exists",
                })
                continue

            # Check for existing onmicrosoft_domain
            existing_domain = await db.execute(
                select(Tenant).where(Tenant.onmicrosoft_domain == tenant_data.onmicrosoft_domain)
            )
            if existing_domain.scalar_one_or_none():
                errors.append({
                    "index": str(idx),
                    "microsoft_tenant_id": tenant_data.microsoft_tenant_id,
                    "error": "Onmicrosoft domain already exists",
                })
                continue

            tenant = Tenant(
                microsoft_tenant_id=tenant_data.microsoft_tenant_id,
                name=tenant_data.name,
                onmicrosoft_domain=tenant_data.onmicrosoft_domain,
                provider=tenant_data.provider,
                admin_email=tenant_data.admin_email,
                admin_password=encrypt_password(tenant_data.admin_password),
                licensed_user_upn=tenant_data.licensed_user_upn or "",
                status=TenantStatus.NEW,
            )
            db.add(tenant)
            created += 1

        except Exception as e:
            errors.append({
                "index": str(idx),
                "microsoft_tenant_id": tenant_data.microsoft_tenant_id,
                "error": str(e),
            })

    await db.commit()

    return {
        "created": created,
        "errors": errors,
    }


@router.post("/{tenant_id}/link-domain/{domain_id}", response_model=TenantRead)
async def link_domain_to_tenant(
    tenant_id: UUID,
    domain_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """
    Link a domain to a tenant.
    Updates both tenant.domain_id and domain.tenant_id.
    """
    tenant = await get_tenant_or_404(tenant_id, db)

    # Get domain
    result = await db.execute(select(Domain).where(Domain.id == domain_id))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    # Check if domain is already linked to another tenant
    if domain.tenant_id and domain.tenant_id != tenant_id:
        raise HTTPException(status_code=409, detail="Domain is already linked to another tenant")

    # Link both sides
    tenant.domain_id = domain_id
    domain.tenant_id = tenant_id

    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.post("/bulk-import-csv")
async def bulk_import_tenants_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Bulk import tenants from CSV file.
    
    CSV format (columns):
    tenant_name,microsoft_tenant_id,onmicrosoft_domain,admin_email,admin_password,provider,licensed_user_upn,domain_name
    
    Features:
    - Plain text admin_password storage (MVP)
    - Auto-links to domain if domain_name is provided
    - Skips duplicates (by microsoft_tenant_id or onmicrosoft_domain)
    """
    if not file.filename or not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV")
    
    # Read CSV content
    content = await file.read()
    try:
        text_content = content.decode('utf-8')
    except UnicodeDecodeError:
        text_content = content.decode('utf-8-sig')  # Handle BOM
    
    csv_reader = csv.DictReader(io.StringIO(text_content))
    headers = [h.strip().lower() for h in (csv_reader.fieldnames or []) if h]
    standard_required = {
        "tenant_name",
        "microsoft_tenant_id",
        "onmicrosoft_domain",
        "admin_email",
        "admin_password",
        "provider",
    }
    
    results: list[dict] = []
    created = 0
    skipped = 0
    failed = 0
    preconfigured_totp_count = 0

    if not standard_required.issubset(set(headers)):
        tenant_list = tenant_import_service.parse_tenant_csv(text_content)
        merged, _, _ = tenant_import_service.merge_data(tenant_list, {})

        for row_num, tenant_data in enumerate(merged, start=2):
            tenant_name = tenant_data["name"]
            microsoft_tenant_id = tenant_data["microsoft_tenant_id"]
            onmicrosoft_domain = tenant_data["onmicrosoft_domain"]
            admin_email = tenant_data["admin_email"]
            admin_password = tenant_data["admin_password"]
            provider = tenant_data.get("provider") or "Unknown"
            totp_secret = tenant_data.get("totp_secret") or ""
            has_preconfigured_mfa = bool(totp_secret)

            if not all([tenant_name, microsoft_tenant_id, onmicrosoft_domain, admin_email, admin_password, provider]):
                results.append({
                    "row": row_num,
                    "tenant": tenant_name or onmicrosoft_domain,
                    "status": "failed",
                    "reason": "Missing required tenant credentials",
                })
                failed += 1
                continue

            try:
                existing_tid = await db.execute(
                    select(Tenant).where(Tenant.microsoft_tenant_id == microsoft_tenant_id)
                )
                if existing_tid.scalar_one_or_none():
                    results.append({
                        "row": row_num,
                        "tenant": tenant_name,
                        "status": "skipped",
                        "reason": "Tenant with this microsoft_tenant_id already exists",
                    })
                    skipped += 1
                    continue

                existing_omd = await db.execute(
                    select(Tenant).where(Tenant.onmicrosoft_domain == onmicrosoft_domain)
                )
                if existing_omd.scalar_one_or_none():
                    results.append({
                        "row": row_num,
                        "tenant": tenant_name,
                        "status": "skipped",
                        "reason": "Tenant with this onmicrosoft_domain already exists",
                    })
                    skipped += 1
                    continue

                tenant = Tenant(
                    microsoft_tenant_id=microsoft_tenant_id,
                    name=tenant_name,
                    onmicrosoft_domain=onmicrosoft_domain,
                    provider=provider,
                    admin_email=admin_email,
                    admin_password=admin_password,
                    initial_password=admin_password,
                    totp_secret=totp_secret or None,
                    first_login_completed=has_preconfigured_mfa,
                    first_login_at=datetime.utcnow() if has_preconfigured_mfa else None,
                    password_changed=has_preconfigured_mfa,
                    status=(
                        TenantStatus.FIRST_LOGIN_COMPLETE
                        if has_preconfigured_mfa
                        else TenantStatus.NEW
                    ),
                )
                db.add(tenant)
                created += 1
                if has_preconfigured_mfa:
                    preconfigured_totp_count += 1
                results.append({
                    "row": row_num,
                    "tenant": tenant_name,
                    "status": "created",
                    "reason": "Tenant created with preconfigured TOTP" if has_preconfigured_mfa else "Tenant created successfully",
                })
            except Exception as e:
                results.append({
                    "row": row_num,
                    "tenant": tenant_name,
                    "status": "failed",
                    "reason": str(e),
                })
                failed += 1

        await db.commit()

        return {
            "total": created + skipped + failed,
            "created": created,
            "skipped": skipped,
            "failed": failed,
            "preconfigured_totp": preconfigured_totp_count,
            "results": results,
        }
    
    for row_num, row in enumerate(csv_reader, start=2):  # Start at 2 (header is row 1)
        tenant_name = row.get('tenant_name', '').strip()
        microsoft_tenant_id = row.get('microsoft_tenant_id', '').strip()
        onmicrosoft_domain = row.get('onmicrosoft_domain', '').strip()
        admin_email = row.get('admin_email', '').strip()
        admin_password = row.get('admin_password', '').strip()
        provider = row.get('provider', '').strip()
        licensed_user_upn = row.get('licensed_user_upn', '').strip() or row.get('licensed_user_email', '').strip()
        domain_name = row.get('domain_name', '').strip()
        totp_secret = row.get('totp_secret', '').strip()
        if not totp_secret:
            for key, value in row.items():
                if _is_totp_secret_column(key):
                    totp_secret = value.strip() if value else ""
                    break
        totp_secret = _normalize_totp_secret(totp_secret)
        has_preconfigured_mfa = bool(totp_secret)
        
        # Validate required fields
        if not all([tenant_name, microsoft_tenant_id, onmicrosoft_domain, admin_email, admin_password, provider]):
            results.append({
                "row": row_num,
                "tenant": tenant_name or microsoft_tenant_id,
                "status": "failed",
                "reason": "Missing required fields",
            })
            failed += 1
            continue
        
        try:
            # Check for existing tenant by microsoft_tenant_id
            existing = await db.execute(
                select(Tenant).where(Tenant.microsoft_tenant_id == microsoft_tenant_id)
            )
            if existing.scalar_one_or_none():
                results.append({
                    "row": row_num,
                    "tenant": tenant_name,
                    "status": "skipped",
                    "reason": "Tenant with this microsoft_tenant_id already exists",
                })
                skipped += 1
                continue
            
            # Check for existing tenant by onmicrosoft_domain
            existing_omd = await db.execute(
                select(Tenant).where(Tenant.onmicrosoft_domain == onmicrosoft_domain)
            )
            if existing_omd.scalar_one_or_none():
                results.append({
                    "row": row_num,
                    "tenant": tenant_name,
                    "status": "skipped",
                    "reason": "Tenant with this onmicrosoft_domain already exists",
                })
                skipped += 1
                continue
            
            # Create tenant with plain text password (MVP requirement)
            tenant = Tenant(
                microsoft_tenant_id=microsoft_tenant_id,
                name=tenant_name,
                onmicrosoft_domain=onmicrosoft_domain,
                provider=provider,
                admin_email=admin_email,
                admin_password=admin_password,  # Plain text for MVP
                initial_password=admin_password,
                totp_secret=totp_secret or None,
                first_login_completed=has_preconfigured_mfa,
                first_login_at=datetime.utcnow() if has_preconfigured_mfa else None,
                password_changed=has_preconfigured_mfa,
                licensed_user_upn=licensed_user_upn or "",
                status=(
                    TenantStatus.FIRST_LOGIN_COMPLETE
                    if has_preconfigured_mfa
                    else TenantStatus.NEW
                ),
            )
            db.add(tenant)
            await db.flush()  # Get tenant ID without committing
            if has_preconfigured_mfa:
                preconfigured_totp_count += 1
            
            # Auto-link to domain if domain_name is provided
            domain_linked = False
            if domain_name:
                domain_result = await db.execute(
                    select(Domain).where(Domain.name == domain_name)
                )
                domain = domain_result.scalar_one_or_none()
                
                if domain:
                    # Check if domain is already linked to another tenant
                    if domain.tenant_id and domain.tenant_id != tenant.id:
                        results.append({
                            "row": row_num,
                            "tenant": tenant_name,
                            "status": "created",
                            "reason": f"Tenant created but domain '{domain_name}' is already linked to another tenant",
                        })
                        created += 1
                        continue
                    
                    # Link both sides
                    tenant.domain_id = domain.id
                    domain.tenant_id = tenant.id
                    domain.status = DomainStatus.TENANT_LINKED
                    domain_linked = True
            
            if domain_name and not domain_linked:
                if domain_name:
                    results.append({
                        "row": row_num,
                        "tenant": tenant_name,
                        "status": "created",
                        "reason": f"Tenant created but domain '{domain_name}' not found in database",
                    })
                else:
                    results.append({
                        "row": row_num,
                        "tenant": tenant_name,
                        "status": "created",
                        "reason": "No domain specified",
                    })
            elif domain_linked:
                results.append({
                    "row": row_num,
                    "tenant": tenant_name,
                    "status": "created",
                    "reason": f"Tenant created and linked to domain '{domain_name}'",
                })
            else:
                results.append({
                    "row": row_num,
                    "tenant": tenant_name,
                    "status": "created",
                    "reason": "Tenant created successfully",
                })
            
            created += 1
            
        except Exception as e:
            results.append({
                "row": row_num,
                "tenant": tenant_name,
                "status": "failed",
                "reason": str(e),
            })
            failed += 1
    
    await db.commit()
    
    return {
        "total": created + skipped + failed,
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "preconfigured_totp": preconfigured_totp_count,
        "results": results,
    }


@router.post("/bulk-add-to-m365")
async def bulk_add_domains_to_m365(
    tenant_ids: Optional[List[UUID]] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Add custom domains to M365 tenants and verify them.
    
    If tenant_ids is None, process all tenants with status="tenant_linked" (or domain linked).
    
    For EACH tenant:
    1. Get admin credentials from tenant record
    2. Authenticate to M365 (get token)
    3. Add custom domain to M365
    4. Get verification TXT record (MS=msXXXXXX)
    5. Add verification TXT to Cloudflare
    6. Trigger domain verification in M365
    7. Get MX/SPF values from M365
    8. Store mx_value and spf_value in tenant record
    9. Update tenant status = "domain_verified"
    10. Update domain status = "m365_verified", set m365_verified_at
    
    Returns: Summary of results per tenant
    """
    # Initialize services
    try:
        ms_service = MicrosoftGraphService()
        cf_service = CloudflareService()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize services: {str(e)}")
    
    # Build query for tenants
    query = select(Tenant).options(selectinload(Tenant.domain))
    
    if tenant_ids:
        query = query.where(Tenant.id.in_(tenant_ids))
    else:
        # Process tenants with linked domains that haven't been M365 verified yet
        query = query.where(
            Tenant.status.in_([TenantStatus.DOMAIN_LINKED, TenantStatus.NEW, TenantStatus.IMPORTED])
        ).where(Tenant.domain_id.isnot(None))
    
    result = await db.execute(query)
    tenants = list(result.scalars().all())
    
    if not tenants:
        return {
            "total": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "message": "No tenants found to process"
        }
    
    results = []
    succeeded = 0
    failed = 0
    
    for tenant in tenants:
        tenant_result = {
            "tenant_id": str(tenant.id),
            "tenant_name": tenant.name,
            "domain": tenant.domain.name if tenant.domain else None,
            "success": False,
            "steps_completed": [],
            "error": None,
        }
        
        try:
            # Validate tenant has a linked domain
            if not tenant.domain:
                tenant_result["error"] = "No domain linked to tenant"
                tenant.error_message = tenant_result["error"]
                failed += 1
                results.append(tenant_result)
                continue
            
            domain = tenant.domain
            domain_name = domain.name
            
            # Validate domain has zone_id
            if not domain.cloudflare_zone_id:
                tenant_result["error"] = f"Domain {domain_name} has no Cloudflare zone_id"
                tenant.error_message = tenant_result["error"]
                failed += 1
                results.append(tenant_result)
                continue
            
            # Step 1: Get access token
            token = await ms_service.get_token(
                tenant_id=tenant.microsoft_tenant_id,
                admin_email=tenant.admin_email,
                admin_password=tenant.admin_password,
            )
            tenant_result["steps_completed"].append("got_token")
            
            # Step 2: Add domain to M365
            try:
                await ms_service.add_domain(token=token, domain=domain_name)
                tenant_result["steps_completed"].append("domain_added")
            except MicrosoftGraphError as e:
                # Domain might already be added - check if it's a "already exists" error
                if "already exists" in str(e).lower() or "domain already" in str(e).lower():
                    tenant_result["steps_completed"].append("domain_already_exists")
                else:
                    raise
            
            # Step 3: Get verification TXT record
            txt_value = await ms_service.get_domain_verification_records(
                token=token, domain=domain_name
            )
            tenant_result["steps_completed"].append("got_verification_txt")
            tenant_result["verification_txt"] = txt_value
            
            # Step 4: Add verification TXT to Cloudflare
            txt_added = await cf_service.create_verification_txt(
                zone_id=domain.cloudflare_zone_id,
                domain=domain_name,
                ms_value=txt_value,
            )
            if txt_added:
                domain.verification_txt_value = txt_value
                domain.verification_txt_added = True
                tenant_result["steps_completed"].append("verification_txt_added_to_cf")
            else:
                tenant_result["steps_completed"].append("verification_txt_may_exist")
            
            # Wait a moment for DNS propagation
            await asyncio.sleep(2)
            
            # Step 5: Trigger domain verification
            is_verified = await ms_service.verify_domain(token=token, domain=domain_name)
            if is_verified:
                tenant_result["steps_completed"].append("domain_verified")
            else:
                # Try again after a longer wait
                await asyncio.sleep(5)
                is_verified = await ms_service.verify_domain(token=token, domain=domain_name)
                if is_verified:
                    tenant_result["steps_completed"].append("domain_verified_retry")
                else:
                    tenant_result["error"] = "Domain verification failed - DNS may not have propagated yet"
                    tenant.error_message = tenant_result["error"]
                    tenant.status = TenantStatus.ERROR
                    failed += 1
                    results.append(tenant_result)
                    continue
            
            # Step 6: Get MX/SPF service configuration
            service_config = await ms_service.get_domain_service_config(
                token=token, domain=domain_name
            )
            tenant_result["steps_completed"].append("got_service_config")
            tenant_result["mx_value"] = service_config["mx_value"]
            tenant_result["spf_value"] = service_config["spf_value"]
            
            # Step 7: Update tenant with MX/SPF values
            tenant.mx_value = service_config["mx_value"]
            tenant.spf_value = service_config["spf_value"]
            tenant.status = TenantStatus.DOMAIN_VERIFIED
            tenant.error_message = None
            
            # Step 8: Update domain status
            domain.status = DomainStatus.M365_VERIFIED
            domain.m365_verified_at = datetime.now(timezone.utc)
            
            tenant_result["success"] = True
            succeeded += 1
            
        except MicrosoftGraphError as e:
            tenant_result["error"] = f"Microsoft Graph API error: {str(e)}"
            tenant.error_message = tenant_result["error"]
            tenant.status = TenantStatus.ERROR
            failed += 1
        except CloudflareError as e:
            tenant_result["error"] = f"Cloudflare API error: {str(e)}"
            tenant.error_message = tenant_result["error"]
            tenant.status = TenantStatus.ERROR
            failed += 1
        except Exception as e:
            tenant_result["error"] = f"Unexpected error: {str(e)}"
            tenant.error_message = tenant_result["error"]
            tenant.status = TenantStatus.ERROR
            failed += 1
        
        results.append(tenant_result)
        
        # Rate limiting between tenants
        await asyncio.sleep(0.5)
    
    await db.commit()
    
    return {
        "total": len(tenants),
        "processed": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


@router.post("/bulk-setup-dns")
async def bulk_setup_dns(
    tenant_ids: Optional[List[UUID]] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Add MX and SPF records to Cloudflare for verified tenants.
    
    If tenant_ids is None, process all tenants with status="domain_verified".
    
    For EACH tenant:
    1. Get mx_value and spf_value from tenant record
    2. Get zone_id from linked domain
    3. Add MX record to Cloudflare (NOT proxied)
    4. Add SPF TXT record to Cloudflare
    5. Update domain: mx_configured=True, spf_configured=True
    6. Update domain status = "dns_configured"
    
    Returns: Summary of results
    """
    # Initialize Cloudflare service
    try:
        cf_service = CloudflareService()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize Cloudflare service: {str(e)}")
    
    # Build query for tenants
    query = select(Tenant).options(selectinload(Tenant.domain))
    
    if tenant_ids:
        query = query.where(Tenant.id.in_(tenant_ids))
    else:
        # Process tenants with domain_verified status
        query = query.where(Tenant.status == TenantStatus.DOMAIN_VERIFIED)
    
    result = await db.execute(query)
    tenants = list(result.scalars().all())
    
    if not tenants:
        return {
            "total": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "message": "No tenants found to process"
        }
    
    results = []
    succeeded = 0
    failed = 0
    
    for tenant in tenants:
        tenant_result = {
            "tenant_id": str(tenant.id),
            "tenant_name": tenant.name,
            "domain": tenant.domain.name if tenant.domain else None,
            "success": False,
            "steps_completed": [],
            "error": None,
        }
        
        try:
            # Validate tenant has required data
            if not tenant.domain:
                tenant_result["error"] = "No domain linked to tenant"
                failed += 1
                results.append(tenant_result)
                continue
            
            domain = tenant.domain
            
            if not domain.cloudflare_zone_id:
                tenant_result["error"] = f"Domain {domain.name} has no Cloudflare zone_id"
                failed += 1
                results.append(tenant_result)
                continue
            
            if not tenant.mx_value or not tenant.spf_value:
                tenant_result["error"] = "Tenant missing mx_value or spf_value"
                failed += 1
                results.append(tenant_result)
                continue
            
            # Step 1: Add MX record
            try:
                await cf_service.create_dns_record(
                    zone_id=domain.cloudflare_zone_id,
                    record_type="MX",
                    name="@",
                    content=tenant.mx_value,
                    priority=0,
                    proxied=False,  # MX records must not be proxied
                )
                domain.mx_configured = True
                tenant_result["steps_completed"].append("mx_record_added")
            except CloudflareError as e:
                if "already exist" in str(e).lower():
                    domain.mx_configured = True
                    tenant_result["steps_completed"].append("mx_record_exists")
                else:
                    raise
            
            # Step 2: Add SPF TXT record
            try:
                await cf_service.create_dns_record(
                    zone_id=domain.cloudflare_zone_id,
                    record_type="TXT",
                    name="@",
                    content=tenant.spf_value,
                    proxied=False,
                )
                domain.spf_configured = True
                tenant_result["steps_completed"].append("spf_record_added")
            except CloudflareError as e:
                if "already exist" in str(e).lower():
                    domain.spf_configured = True
                    tenant_result["steps_completed"].append("spf_record_exists")
                else:
                    raise
            
            # Step 3: Update domain status
            domain.status = DomainStatus.DNS_CONFIGURING
            tenant.status = TenantStatus.DNS_CONFIGURING
            tenant.error_message = None
            
            tenant_result["success"] = True
            succeeded += 1
            
        except CloudflareError as e:
            tenant_result["error"] = f"Cloudflare API error: {str(e)}"
            tenant.error_message = tenant_result["error"]
            failed += 1
        except Exception as e:
            tenant_result["error"] = f"Unexpected error: {str(e)}"
            tenant.error_message = tenant_result["error"]
            failed += 1
        
        results.append(tenant_result)
        
        # Rate limiting for Cloudflare API
        await asyncio.sleep(0.25)
    
    await db.commit()
    
    return {
        "total": len(tenants),
        "processed": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


@router.post("/bulk-setup-dkim")
async def bulk_setup_dkim(
    tenant_ids: Optional[List[UUID]] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Setup DKIM for tenants.
    
    If tenant_ids is None, process all tenants with status="dns_configuring".
    
    For EACH tenant:
    1. Call PowerShell to get DKIM CNAME values from M365
    2. Store dkim_selector1_cname and dkim_selector2_cname in tenant
    3. Add DKIM CNAMEs to Cloudflare (NOT proxied!)
    4. Update domain: dkim_cnames_added=True
    5. Wait a moment for DNS propagation (or check)
    6. Call PowerShell to enable DKIM in M365
    7. Update domain: dkim_enabled=True
    8. Update tenant status = "dkim_enabled"
    9. Update domain status = "active" (fully configured!)
    
    Returns: Summary of results
    """
    # Initialize services
    try:
        ps_service = PowerShellService()
        cf_service = CloudflareService()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize services: {str(e)}")
    
    # Build query for tenants
    query = select(Tenant).options(selectinload(Tenant.domain))
    
    if tenant_ids:
        query = query.where(Tenant.id.in_(tenant_ids))
    else:
        # Process tenants with dns_configuring status
        query = query.where(Tenant.status == TenantStatus.DNS_CONFIGURING)
    
    result = await db.execute(query)
    tenants = list(result.scalars().all())
    
    if not tenants:
        return {
            "total": 0,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "results": [],
            "message": "No tenants found to process"
        }
    
    results = []
    succeeded = 0
    failed = 0
    
    for tenant in tenants:
        tenant_result = {
            "tenant_id": str(tenant.id),
            "tenant_name": tenant.name,
            "domain": tenant.domain.name if tenant.domain else None,
            "success": False,
            "steps_completed": [],
            "error": None,
        }
        
        try:
            # Validate tenant has required data
            if not tenant.domain:
                tenant_result["error"] = "No domain linked to tenant"
                failed += 1
                results.append(tenant_result)
                continue
            
            domain = tenant.domain
            
            if not domain.cloudflare_zone_id:
                tenant_result["error"] = f"Domain {domain.name} has no Cloudflare zone_id"
                failed += 1
                results.append(tenant_result)
                continue
            
            # Step 1: Get DKIM config from M365 via PowerShell
            dkim_config = await ps_service.get_dkim_config(
                admin_email=tenant.admin_email,
                admin_password=tenant.admin_password,
                domain=domain.name,
            )
            tenant_result["steps_completed"].append("got_dkim_config")
            
            # Extract CNAME values
            selector1_cname = dkim_config.get("selector1_cname", "")
            selector2_cname = dkim_config.get("selector2_cname", "")
            
            if not selector1_cname or not selector2_cname:
                tenant_result["error"] = "DKIM config missing selector CNAME values"
                failed += 1
                results.append(tenant_result)
                continue
            
            tenant_result["selector1_cname"] = selector1_cname
            tenant_result["selector2_cname"] = selector2_cname
            
            # Step 2: Store DKIM values in tenant
            tenant.dkim_selector1_cname = selector1_cname
            tenant.dkim_selector2_cname = selector2_cname
            
            # Also store in domain for reference
            domain.dkim_selector1_cname = selector1_cname
            domain.dkim_selector2_cname = selector2_cname
            
            # Step 3: Add DKIM CNAMEs to Cloudflare
            dkim_result = await cf_service.create_dkim_cnames(
                zone_id=domain.cloudflare_zone_id,
                domain=domain.name,
                selector1_value=selector1_cname,
                selector2_value=selector2_cname,
            )
            
            if dkim_result["selector1_created"] or "already exist" in str(dkim_result.get("errors", [])).lower():
                tenant_result["steps_completed"].append("selector1_cname_added")
            if dkim_result["selector2_created"] or "already exist" in str(dkim_result.get("errors", [])).lower():
                tenant_result["steps_completed"].append("selector2_cname_added")
            
            domain.dkim_cnames_added = True
            
            # Step 4: Wait for DNS propagation
            await asyncio.sleep(5)  # Brief wait for DNS
            
            # Step 5: Enable DKIM in M365 via PowerShell
            try:
                await ps_service.enable_dkim(
                    admin_email=tenant.admin_email,
                    admin_password=tenant.admin_password,
                    domain=domain.name,
                )
                tenant_result["steps_completed"].append("dkim_enabled_in_m365")
                domain.dkim_enabled = True
            except PowerShellError as e:
                # DKIM enable might fail if DNS hasn't propagated - still mark partial success
                tenant_result["steps_completed"].append("dkim_enable_attempted")
                tenant_result["dkim_enable_error"] = str(e)
                # Don't fail completely - DKIM can be retried later
            
            # Step 6: Update statuses
            if domain.dkim_enabled:
                domain.status = DomainStatus.ACTIVE
                tenant.status = TenantStatus.DKIM_ENABLED
            else:
                domain.status = DomainStatus.PENDING_DKIM
                tenant.status = TenantStatus.DKIM_CONFIGURING
            
            tenant.error_message = None
            tenant_result["success"] = True
            succeeded += 1
            
        except PowerShellError as e:
            tenant_result["error"] = f"PowerShell error: {str(e)}"
            tenant.error_message = tenant_result["error"]
            tenant.status = TenantStatus.ERROR
            failed += 1
        except CloudflareError as e:
            tenant_result["error"] = f"Cloudflare API error: {str(e)}"
            tenant.error_message = tenant_result["error"]
            failed += 1
        except Exception as e:
            tenant_result["error"] = f"Unexpected error: {str(e)}"
            tenant.error_message = tenant_result["error"]
            failed += 1
        
        results.append(tenant_result)
        
        # Rate limiting between tenants (PowerShell calls are slow)
        await asyncio.sleep(1)
    
    await db.commit()
    
    return {
        "total": len(tenants),
        "processed": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }
