from __future__ import annotations

import asyncio
import csv
import re
from io import StringIO
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.domain import Domain
from app.models.mailbox import Mailbox, MailboxStatus, WarmupStage
from app.models.tenant import Tenant, TenantStatus
from app.schemas.mailbox import MailboxCreate, MailboxRead, MailboxUpdate
from app.services.email_generator import generate_email_addresses

router = APIRouter(prefix="/api/v1/mailboxes", tags=["mailboxes"])

MAILBOX_PASSWORD = "#Sendemails1"
_reset_passwords_progress = {
    "status": "idle",
    "total": 0,
    "completed": 0,
    "failed": 0,
    "current_tenant": None,
    "errors": [],
}


class MailboxGenerateRequest(BaseModel):
    tenant_id: UUID
    first_name: str
    last_name: str
    count: int = 50


class PersonaBase(BaseModel):
    first_name: str
    last_name: str


class ResetPasswordsStatus(BaseModel):
    status: str
    total: int
    completed: int
    failed: int
    current_tenant: str | None
    errors: list[str]


class DomainMailboxExportRequest(BaseModel):
    domains: list[str] | str


_DOMAIN_TOKEN_SPLIT_RE = re.compile(r"[\s,;]+")
_DOMAIN_VALUE_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9][a-z0-9-]{1,62}$"
)


def _normalize_export_domain_token(token: str) -> str | None:
    domain = token.strip().lower()
    if not domain:
        return None

    domain = re.sub(r"^[a-z][a-z0-9+.-]*://", "", domain)
    domain = domain.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    domain = domain.rsplit("@", 1)[-1].strip().strip(".")
    if domain.startswith("*."):
        domain = domain[2:]

    return domain or None


def _parse_export_domains(raw_domains: list[str] | str) -> tuple[list[str], list[str]]:
    raw_values = raw_domains if isinstance(raw_domains, list) else [raw_domains]
    domains: list[str] = []
    invalid_tokens: list[str] = []
    seen: set[str] = set()

    for value in raw_values:
        for token in _DOMAIN_TOKEN_SPLIT_RE.split(str(value)):
            if not token.strip():
                continue

            domain = _normalize_export_domain_token(token)
            if not domain:
                continue

            if not _DOMAIN_VALUE_RE.match(domain):
                invalid_tokens.append(token.strip())
                continue

            if domain not in seen:
                domains.append(domain)
                seen.add(domain)

    return domains, invalid_tokens


def _mailbox_domain(email: str) -> str:
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].lower()


def _export_filename_for_domains(domains: list[str]) -> str:
    if len(domains) == 1:
        safe_domain = re.sub(r"[^a-z0-9.-]+", "_", domains[0]).replace(".", "_")
        return f"mailboxes_{safe_domain}.csv"
    return f"mailboxes_{len(domains)}_domains.csv"


def _build_domain_mailbox_csv(rows: list[tuple[Mailbox, Tenant]]) -> str:
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["DisplayName", "EmailAddress", "Password", "Domain", "TenantName"])

    for mailbox, tenant in rows:
        domain = _mailbox_domain(mailbox.email) or (tenant.custom_domain or "")
        writer.writerow(
            [
                mailbox.display_name,
                mailbox.email,
                mailbox.initial_password or mailbox.password or MAILBOX_PASSWORD,
                domain,
                tenant.name if tenant else "",
            ]
        )

    return output.getvalue()


async def _export_mailbox_credentials_for_domains(
    raw_domains: list[str] | str,
    db: AsyncSession,
) -> StreamingResponse:
    domains, invalid_tokens = _parse_export_domains(raw_domains)

    if invalid_tokens:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Invalid domain values",
                "invalid_domains": invalid_tokens,
            },
        )

    if not domains:
        raise HTTPException(status_code=400, detail="Provide at least one domain")

    domain_filters = [Mailbox.email.ilike(f"%@{domain}") for domain in domains]
    result = await db.execute(
        select(Mailbox, Tenant)
        .join(Tenant, Mailbox.tenant_id == Tenant.id)
        .where(or_(*domain_filters))
    )
    rows = [(mailbox, tenant) for mailbox, tenant in result.all()]

    requested_order = {domain: index for index, domain in enumerate(domains)}
    rows.sort(
        key=lambda row: (
            requested_order.get(_mailbox_domain(row[0].email), len(requested_order)),
            _mailbox_domain(row[0].email),
            row[0].email.lower(),
        )
    )

    if not rows:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "No mailboxes found for the requested domains",
                "domains": domains,
            },
        )

    found_domains = {_mailbox_domain(mailbox.email) for mailbox, _tenant in rows}
    missing_domains = [domain for domain in domains if domain not in found_domains]
    csv_content = _build_domain_mailbox_csv(rows)

    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={_export_filename_for_domains(domains)}",
            "Access-Control-Expose-Headers": "Content-Disposition, X-Mailbox-Count, X-Domain-Count, X-Missing-Domains",
            "X-Mailbox-Count": str(len(rows)),
            "X-Domain-Count": str(len(domains)),
            "X-Missing-Domains": ",".join(missing_domains),
        },
    )


def _reset_progress_snapshot() -> dict:
    return dict(_reset_passwords_progress)


async def _run_reset_passwords_job(tenant_id: UUID | None = None) -> None:
    from app.db.session import async_session_factory
    from app.services.selenium.user_ops import UserOpsSelenium
    from app.services.selenium.admin_portal import _login_with_mfa
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    _reset_passwords_progress.update(
        {
            "status": "running",
            "total": 0,
            "completed": 0,
            "failed": 0,
            "current_tenant": None,
            "errors": [],
        }
    )

    def build_driver() -> webdriver.Chrome:
        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--headless=new")
        return webdriver.Chrome(options=options)

    try:
        async with async_session_factory() as session:
            tenant_query = select(Tenant)
            if tenant_id:
                tenant_query = tenant_query.where(Tenant.id == tenant_id)
            tenant_result = await session.execute(tenant_query)
            tenants = list(tenant_result.scalars().all())

        if not tenants:
            _reset_passwords_progress.update(
                {"status": "completed", "errors": ["No tenants found"]}
            )
            return

        async with async_session_factory() as session:
            mailbox_query = select(Mailbox)
            if tenant_id:
                mailbox_query = mailbox_query.where(Mailbox.tenant_id == tenant_id)
            mailbox_result = await session.execute(mailbox_query)
            all_mailboxes = list(mailbox_result.scalars().all())
            _reset_passwords_progress["total"] = len(all_mailboxes)

        for tenant in tenants:
            _reset_passwords_progress["current_tenant"] = tenant.custom_domain
            driver = build_driver()
            user_ops = UserOpsSelenium(driver, tenant.custom_domain)

            try:
                _login_with_mfa(
                    driver=driver,
                    admin_email=tenant.admin_email,
                    admin_password=tenant.admin_password,
                    totp_secret=tenant.totp_secret,
                    domain=tenant.custom_domain,
                )

                async with async_session_factory() as session:
                    mb_result = await session.execute(
                        select(Mailbox).where(Mailbox.tenant_id == tenant.id)
                    )
                    mailboxes = list(mb_result.scalars().all())

                if not mailboxes:
                    continue

                expected_count = len(mailboxes)
                bulk_results = user_ops.set_passwords_and_enable_via_admin_ui(
                    password=MAILBOX_PASSWORD,
                    exclude_users=["me1"],
                    expected_count=expected_count,
                )

                reset_count = int(bulk_results.get("passwords_set", 0))
                error_list = bulk_results.get("errors", [])

                _reset_passwords_progress["completed"] += reset_count
                if reset_count < expected_count:
                    _reset_passwords_progress["failed"] += expected_count - reset_count

                if error_list:
                    _reset_passwords_progress["errors"].extend(
                        [f"Tenant {tenant.custom_domain}: {err}" for err in error_list]
                    )

                if reset_count >= expected_count and not error_list:
                    async with async_session_factory() as session:
                        await session.execute(
                            update(Mailbox)
                            .where(Mailbox.tenant_id == tenant.id)
                            .values(
                                password=MAILBOX_PASSWORD,
                                initial_password=MAILBOX_PASSWORD,
                            )
                        )
                        await session.commit()
                else:
                    _reset_passwords_progress["errors"].append(
                        f"Tenant {tenant.custom_domain}: password reset incomplete; database not updated"
                    )

            except Exception as exc:
                _reset_passwords_progress["errors"].append(
                    f"Tenant {tenant.custom_domain} failed: {exc}"
                )
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass

        _reset_passwords_progress["status"] = "completed"
    except Exception as exc:
        _reset_passwords_progress["status"] = "failed"
        _reset_passwords_progress["errors"].append(str(exc))


async def get_mailbox_or_404(mailbox_id: UUID, db: AsyncSession) -> Mailbox:
    """Get mailbox by ID or raise 404."""
    result = await db.execute(select(Mailbox).where(Mailbox.id == mailbox_id))
    mailbox = result.scalar_one_or_none()
    if not mailbox:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    return mailbox


@router.get("/", response_model=list[MailboxRead])
async def list_mailboxes(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    tenant_id: UUID | None = Query(default=None),
    status: MailboxStatus | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[Mailbox]:
    """List all mailboxes with optional filters."""
    query = select(Mailbox)
    if tenant_id:
        query = query.where(Mailbox.tenant_id == tenant_id)
    if status:
        query = query.where(Mailbox.status == status)
    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/reset-all-passwords")
async def reset_all_passwords(
    tenant_id: UUID | None = Query(default=None),
) -> dict:
    """Trigger a background job to reset mailbox passwords to #Sendemails1."""
    if _reset_passwords_progress["status"] == "running":
        raise HTTPException(status_code=409, detail="Password reset already running")

    asyncio.create_task(_run_reset_passwords_job(tenant_id))
    return {"status": "started", "message": "Password reset job running in background"}


@router.get("/reset-passwords-status", response_model=ResetPasswordsStatus)
async def reset_passwords_status() -> dict:
    """Get current password reset job status."""
    return _reset_progress_snapshot()


@router.post("/", response_model=MailboxRead, status_code=201)
async def create_mailbox(
    mailbox_in: MailboxCreate,
    db: AsyncSession = Depends(get_db),
) -> Mailbox:
    """Create a single mailbox."""
    # Check if email already exists
    existing = await db.execute(select(Mailbox).where(Mailbox.email == mailbox_in.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already exists")

    # Verify tenant exists
    tenant_result = await db.execute(select(Tenant).where(Tenant.id == mailbox_in.tenant_id))
    if not tenant_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Tenant not found")

    mailbox = Mailbox(
        email=mailbox_in.email,
        display_name=mailbox_in.display_name,
        password=mailbox_in.password,
        tenant_id=mailbox_in.tenant_id,
        status=MailboxStatus.PENDING,
        warmup_stage=WarmupStage.NONE,
    )
    db.add(mailbox)
    await db.commit()
    await db.refresh(mailbox)
    return mailbox


@router.post("/generate")
async def generate_mailboxes(
    request: MailboxGenerateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate mailbox records for a tenant.
    Creates records in DATABASE only - not in M365 yet.
    
    Steps:
    1. Validate tenant exists and has a linked domain
    2. Get domain name from linked domain
    3. Generate email variations using email_generator
       - NO NUMBERS in emails
       - Display name is the same for all (e.g., "Jack Zuvelek")
       - Each gets a unique secure password
    4. Create mailbox records in DB with status="pending"
    5. Return generated list
    
    Returns: {
        "tenant_id": "...",
        "domain": "example.com",
        "mailboxes_generated": 50,
        "mailboxes": [
            {"email": "jack.zuvelek@example.com", "display_name": "Jack Zuvelek", "password": "xK9#mP2$"},
            ...
        ]
    }
    """
    # Verify tenant exists and get associated domain
    tenant_result = await db.execute(select(Tenant).where(Tenant.id == request.tenant_id))
    tenant = tenant_result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Get domain for this tenant
    if not tenant.domain_id:
        raise HTTPException(status_code=400, detail="Tenant has no linked domain")

    domain_result = await db.execute(select(Domain).where(Domain.id == tenant.domain_id))
    domain = domain_result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    # Generate email variations using the new generator (NO NUMBERS!)
    variations = generate_email_addresses(
        first_name=request.first_name,
        last_name=request.last_name,
        domain=domain.name,
        count=request.count,
    )

    created_mailboxes: list[dict] = []

    for var in variations:
        # Check if email already exists
        existing = await db.execute(select(Mailbox).where(Mailbox.email == var["email"]))
        if existing.scalar_one_or_none():
            continue

        mailbox = Mailbox(
            email=var["email"],
            display_name=var["display_name"],
            password=var["password"],
            tenant_id=request.tenant_id,
            status=MailboxStatus.PENDING,
            warmup_stage=WarmupStage.NONE,
        )
        db.add(mailbox)
        created_mailboxes.append({
            "email": var["email"],
            "display_name": var["display_name"],
            "password": var["password"],
        })

    await db.commit()

    return {
        "tenant_id": str(request.tenant_id),
        "domain": domain.name,
        "mailboxes_generated": len(created_mailboxes),
        "mailboxes": created_mailboxes,
    }


@router.post("/bulk-create-in-m365")
async def bulk_create_mailboxes_in_m365(
    tenant_ids: Optional[List[UUID]] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Create pending mailboxes in M365.
    
    If tenant_ids is None, process all tenants with pending mailboxes and dkim_enabled.
    
    For EACH tenant:
    1. Get all mailboxes with status="pending"
    2. Get admin credentials from tenant
    3. Call PowerShell to create shared mailboxes in M365
    4. Update each mailbox with microsoft_object_id
    5. Update status="created"
    6. Update tenant.mailboxes_created count
    
    Returns: Summary of results
    """
    from app.services.powershell import powershell_service
    
    results = {
        "tenants_processed": 0,
        "mailboxes_created": 0,
        "errors": [],
    }
    
    # Build query for tenants
    tenant_query = select(Tenant)
    if tenant_ids:
        tenant_query = tenant_query.where(Tenant.id.in_(tenant_ids))
    else:
        # Only process tenants with DKIM enabled
        tenant_query = tenant_query.where(Tenant.status == TenantStatus.DKIM_ENABLED)
    
    tenant_result = await db.execute(tenant_query)
    tenants = list(tenant_result.scalars().all())
    
    for tenant in tenants:
        # Get pending mailboxes for this tenant
        mailbox_query = select(Mailbox).where(
            Mailbox.tenant_id == tenant.id,
            Mailbox.status == MailboxStatus.PENDING,
        )
        mailbox_result = await db.execute(mailbox_query)
        mailboxes = list(mailbox_result.scalars().all())
        
        if not mailboxes:
            continue
        
        try:
            # Build mailbox list for PowerShell script
            mailbox_list = [
                {"email": mb.email, "display_name": mb.display_name}
                for mb in mailboxes
            ]
            
            # Call PowerShell to create mailboxes
            ps_results = await powershell_service.create_shared_mailboxes(
                admin_email=tenant.admin_email,
                admin_password=tenant.admin_password,
                mailboxes=mailbox_list,
            )
            
            # Update mailboxes with results
            for created in ps_results:
                email = created.get("email")
                object_id = created.get("object_id")
                success = created.get("success", False)
                
                # Find matching mailbox
                for mb in mailboxes:
                    if mb.email.lower() == email.lower():
                        if success:
                            mb.microsoft_object_id = object_id
                            mb.status = MailboxStatus.CREATED
                            results["mailboxes_created"] += 1
                        else:
                            mb.status = MailboxStatus.ERROR
                            mb.error_message = created.get("error", "Unknown error")
                        break
            
            # Update tenant count
            tenant.mailboxes_created += results["mailboxes_created"]
            tenant.status = TenantStatus.MAILBOXES_CREATING
            results["tenants_processed"] += 1
                
        except Exception as e:
            results["errors"].append({
                "tenant_id": str(tenant.id),
                "error": str(e),
            })
    
    await db.commit()
    return results


@router.post("/bulk-configure")
async def bulk_configure_mailboxes(
    tenant_ids: Optional[List[UUID]] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Configure created mailboxes (enable, password, UPN, delegation).
    
    For EACH tenant:
    1. Get all mailboxes with status="created"
    2. Get admin credentials and licensed_user_upn from tenant
    3. For each mailbox:
       a. Enable account
       b. Set password (from mailbox.password field)
       c. Fix UPN to match email
       d. Setup delegation to licensed user
    4. Update mailbox status="ready"
    5. Update tenant.mailboxes_configured count
    
    Returns: Summary of results
    """
    from app.services.powershell import powershell_service
    
    results = {
        "tenants_processed": 0,
        "mailboxes_configured": 0,
        "errors": [],
    }
    
    # Build query for tenants
    tenant_query = select(Tenant)
    if tenant_ids:
        tenant_query = tenant_query.where(Tenant.id.in_(tenant_ids))
    else:
        # Only process tenants in mailboxes_creating state
        tenant_query = tenant_query.where(
            Tenant.status.in_([TenantStatus.MAILBOXES_CREATING, TenantStatus.DKIM_ENABLED])
        )
    
    tenant_result = await db.execute(tenant_query)
    tenants = list(tenant_result.scalars().all())
    
    for tenant in tenants:
        # Get created mailboxes for this tenant
        mailbox_query = select(Mailbox).where(
            Mailbox.tenant_id == tenant.id,
            Mailbox.status == MailboxStatus.CREATED,
        )
        mailbox_result = await db.execute(mailbox_query)
        mailboxes = list(mailbox_result.scalars().all())
        
        if not mailboxes:
            continue
        
        configured_count = 0
        
        for mailbox in mailboxes:
            try:
                # Call PowerShell to configure mailbox
                ps_result = await powershell_service.configure_mailbox(
                    admin_email=tenant.admin_email,
                    admin_password=tenant.admin_password,
                    email=mailbox.email,
                    password=mailbox.password,
                    licensed_user_upn=tenant.licensed_user_upn,
                )
                
                if ps_result.get("success"):
                    # Update mailbox state
                    # Note: configure_mailbox enables the account as part of its flow
                    mailbox.account_enabled = True
                    mailbox.password_set = ps_result.get("password_set", False)
                    mailbox.upn_fixed = ps_result.get("upn_fixed", False)
                    mailbox.delegated = ps_result.get("delegation_configured", False)
                    
                    # If all steps complete, mark as ready
                    if all([
                        mailbox.account_enabled,
                        mailbox.password_set,
                        mailbox.upn_fixed,
                        mailbox.delegated,
                    ]):
                        mailbox.status = MailboxStatus.READY
                        configured_count += 1
                        results["mailboxes_configured"] += 1
                    else:
                        # Partial configuration
                        mailbox.status = MailboxStatus.CONFIGURED
                else:
                    error_msg = ps_result.get("error", "Unknown error")
                    mailbox.status = MailboxStatus.ERROR
                    mailbox.error_message = error_msg
                    results["errors"].append({
                        "mailbox_email": mailbox.email,
                        "error": error_msg,
                    })
                    
            except Exception as e:
                mailbox.status = MailboxStatus.ERROR
                mailbox.error_message = str(e)
                results["errors"].append({
                    "mailbox_email": mailbox.email,
                    "error": str(e),
                })
        
        # Update tenant counts
        tenant.mailboxes_configured += configured_count
        if configured_count > 0:
            tenant.status = TenantStatus.MAILBOXES_CONFIGURING
        if tenant.mailboxes_configured >= tenant.mailboxes_created:
            tenant.status = TenantStatus.ACTIVE
        results["tenants_processed"] += 1
    
    await db.commit()
    return results


@router.get("/export-credentials")
async def export_credentials(
    tenant_id: Optional[UUID] = None,
    format: str = "csv",
    db: AsyncSession = Depends(get_db),
):
    """
    Export mailbox credentials as CSV download.
    
    CSV format (matches your existing format):
    DisplayName,EmailAddress,Password
    Jack Zuvelek,jack@example.com,xK9#mP2$vL
    Jack Zuvelek,j.zuvelek@example.com,Abc123!@#
    
    If tenant_id provided, export only that tenant's mailboxes.
    Otherwise export all ready mailboxes.
    """
    # Query mailboxes
    query = select(Mailbox).where(Mailbox.status == MailboxStatus.READY)
    if tenant_id:
        query = query.where(Mailbox.tenant_id == tenant_id)
    
    result = await db.execute(query)
    mailboxes = list(result.scalars().all())
    mailboxes.sort(key=lambda mb: (mb.email.split('@')[1] if '@' in mb.email else '', mb.email))
    
    # Generate CSV
    output = StringIO()
    output.write("DisplayName,EmailAddress,Password\n")
    for mb in mailboxes:
        output.write(f"{mb.display_name},{mb.email},{mb.password or '#Sendemails1'}\n")
    
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=mailbox_credentials.csv"}
    )


@router.get("/export")
async def export_mailbox_credentials(
    tenant_id: UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """
    Export mailbox credentials as CSV (legacy endpoint).
    Returns email,password columns.
    """
    query = select(Mailbox)
    if tenant_id:
        query = query.where(Mailbox.tenant_id == tenant_id)
    result = await db.execute(query)
    mailboxes = list(result.scalars().all())
    mailboxes.sort(key=lambda mb: (mb.email.split('@')[1] if '@' in mb.email else '', mb.email))

    # Build CSV
    output = StringIO()
    output.write("email,password\n")
    for mailbox in mailboxes:
        output.write(f"{mailbox.email},{mailbox.password or '#Sendemails1'}\n")

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=mailbox_credentials.csv"},
    )


@router.post("/export-by-domains")
async def export_mailbox_credentials_by_domains(
    request: DomainMailboxExportRequest,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Export upload-ready mailbox credentials for one or more email domains."""
    return await _export_mailbox_credentials_for_domains(request.domains, db)


@router.get("/export-by-domains")
async def export_mailbox_credentials_by_domains_get(
    domains: list[str] = Query(..., description="Domain, repeated domains, or comma/newline separated domains"),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """GET variant for direct downloads and simple API clients."""
    return await _export_mailbox_credentials_for_domains(domains, db)


@router.get("/{mailbox_id}", response_model=MailboxRead)
async def get_mailbox(
    mailbox_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> Mailbox:
    """Get a single mailbox by ID."""
    return await get_mailbox_or_404(mailbox_id, db)


@router.patch("/{mailbox_id}", response_model=MailboxRead)
async def update_mailbox(
    mailbox_id: UUID,
    mailbox_in: MailboxUpdate,
    db: AsyncSession = Depends(get_db),
) -> Mailbox:
    """Update mailbox fields."""
    mailbox = await get_mailbox_or_404(mailbox_id, db)

    update_data = mailbox_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(mailbox, field, value)

    await db.commit()
    await db.refresh(mailbox)
    return mailbox


@router.delete("/{mailbox_id}")
async def delete_mailbox(
    mailbox_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Soft delete mailbox by setting status to suspended."""
    mailbox = await get_mailbox_or_404(mailbox_id, db)
    mailbox.status = MailboxStatus.SUSPENDED
    await db.commit()
    return {"message": "Mailbox suspended"}
