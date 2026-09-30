"""
Domain Checker API routes.

Provides endpoints to:
1. Upload a CSV of tenants and check their domains
2. Check domains for tenants in an existing batch
3. Poll job progress
4. Download results as CSV
"""

from __future__ import annotations

import csv
import io
import uuid
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.session import SessionLocal
from app.models.tenant import Tenant
from app.models.tenant_audit import TenantAudit, TenantAuditCredential, TenantDisposition
from app.services.inventory_credentials import decrypt_inventory_value, encrypt_inventory_value
from app.services.selenium.domain_checker import (
    check_tenants_parallel,
    TenantCheckResult,
    CHECKER_PARALLEL_BROWSERS,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/domain-checker", tags=["domain-checker"])

# In-memory job storage (same pattern as existing step4_jobs, step8_jobs in wizard.py)
checker_jobs: dict[str, dict] = {}


class CheckerJobStatus(BaseModel):
    job_id: str
    status: str  # "running", "complete", "error"
    total: int
    processed: int
    results: list[dict] = []
    summary: dict = {}
    started_at: str = ""
    completed_at: Optional[str] = None


class TenantAuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    admin_email: str
    tenant_name: str
    disposition: str
    assigned_custom_domain: Optional[str] = None
    has_saved_credentials: bool = False
    login_success: bool
    domain_check_success: bool = False
    login_error: Optional[str] = None
    is_used: Optional[bool] = None
    verified_domains: list[dict] = []
    unverified_domains: list[dict] = []
    custom_domain_count: int
    last_checked_at: Optional[datetime] = None
    updated_at: datetime


class TenantAuditUpdate(BaseModel):
    disposition: Optional[TenantDisposition] = None
    assigned_custom_domain: Optional[str] = None


class TenantInventoryCreate(BaseModel):
    admin_email: str
    admin_password: str = Field(min_length=1)
    totp_secret: Optional[str] = None
    assigned_custom_domain: Optional[str] = None
    disposition: Optional[TenantDisposition] = None


def _normalize_assigned_domain(value: Optional[str]) -> Optional[str]:
    domain = (value or "").strip().lower().rstrip(".")
    if not domain:
        return None
    if domain.endswith(".onmicrosoft.com") or not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}", domain,
    ):
        raise HTTPException(422, "Enter a valid custom domain name")
    return domain


async def _set_assigned_domain(db: AsyncSession, audit: TenantAudit, value: Optional[str]) -> None:
    domain = _normalize_assigned_domain(value)
    if domain:
        result = await db.execute(
            select(TenantAudit.id).where(
                TenantAudit.assigned_custom_domain == domain,
                TenantAudit.id != audit.id,
            )
        )
        if result.scalar_one_or_none() is not None:
            raise HTTPException(409, "This domain is already assigned to another inventory tenant")
    audit.assigned_custom_domain = domain


async def _audit_read(db: AsyncSession, audit: TenantAudit) -> TenantAuditRead:
    result = await db.execute(
        select(TenantAuditCredential.id).where(TenantAuditCredential.audit_id == audit.id)
    )
    return TenantAuditRead.model_validate(audit).model_copy(
        update={"has_saved_credentials": result.scalar_one_or_none() is not None}
    )


# === ENDPOINTS ===


@router.get("/inventory", response_model=list[TenantAuditRead])
async def list_inventory(
    disposition: Optional[TenantDisposition] = None,
    db: AsyncSession = Depends(get_db),
):
    """List the latest persisted audit result for every checked tenant."""
    query = select(TenantAudit).order_by(TenantAudit.last_checked_at.desc().nullslast(), TenantAudit.created_at.desc())
    if disposition:
        query = query.where(TenantAudit.disposition == disposition.value)
    result = await db.execute(query)
    audits = list(result.scalars().all())
    saved = await db.execute(select(TenantAuditCredential.audit_id))
    saved_ids = set(saved.scalars().all())
    return [
        TenantAuditRead.model_validate(audit).model_copy(
            update={"has_saved_credentials": audit.id in saved_ids}
        )
        for audit in audits
    ]


@router.post("/inventory/tenants", response_model=TenantAuditRead, status_code=201)
async def add_inventory_tenant(
    tenant: TenantInventoryCreate,
    db: AsyncSession = Depends(get_db),
):
    """Save a tenant for future checks without touching Microsoft 365."""
    email = tenant.admin_email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.onmicrosoft\.com", email):
        raise HTTPException(422, "Use an admin email on the tenant's .onmicrosoft.com domain")
    password = tenant.admin_password
    if not password.strip():
        raise HTTPException(422, "Admin password is required")
    secret = "".join((tenant.totp_secret or "").split()).upper() or None
    if secret:
        import pyotp
        try:
            pyotp.TOTP(secret).now()
        except (ValueError, TypeError):
            raise HTTPException(422, "TOTP secret is not valid Base32")

    result = await db.execute(select(TenantAudit).where(func.lower(TenantAudit.admin_email) == email))
    audit = result.scalar_one_or_none()
    if audit is None:
        audit = TenantAudit(
            admin_email=email,
            tenant_name=email.split("@", 1)[1].removesuffix(".onmicrosoft.com"),
            disposition=(tenant.disposition or TenantDisposition.UNREVIEWED).value,
            is_used=None,
            verified_domains=[],
            unverified_domains=[],
            custom_domain_count=0,
        )
        db.add(audit)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "Tenant is already in the inventory")
    elif tenant.disposition not in (None, TenantDisposition.UNREVIEWED):
        audit.disposition = tenant.disposition.value
    if tenant.assigned_custom_domain:
        await _set_assigned_domain(db, audit, tenant.assigned_custom_domain)

    result = await db.execute(
        select(TenantAuditCredential).where(TenantAuditCredential.audit_id == audit.id)
    )
    credential = result.scalar_one_or_none()
    is_new_credential = credential is None
    if is_new_credential:
        credential = TenantAuditCredential(audit_id=audit.id)
        db.add(credential)
    credential.password_ciphertext = encrypt_inventory_value(password)
    if secret:
        credential.totp_ciphertext = encrypt_inventory_value(secret)
    elif is_new_credential:
        credential.totp_ciphertext = None
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Tenant or custom domain is already in the inventory")
    await db.refresh(audit)
    return await _audit_read(db, audit)


@router.patch("/inventory/{audit_id}", response_model=TenantAuditRead)
async def update_inventory_disposition(
    audit_id: UUID,
    update: TenantAuditUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Set the operator-managed state without changing pipeline tenant status."""
    result = await db.execute(select(TenantAudit).where(TenantAudit.id == audit_id))
    audit = result.scalar_one_or_none()
    if not audit:
        raise HTTPException(404, "Audited tenant not found")
    if "disposition" in update.model_fields_set:
        if update.disposition is None:
            raise HTTPException(422, "Status is required")
        audit.disposition = update.disposition.value
    if "assigned_custom_domain" in update.model_fields_set:
        await _set_assigned_domain(db, audit, update.assigned_custom_domain)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This domain is already assigned to another inventory tenant")
    await db.refresh(audit)
    return await _audit_read(db, audit)


@router.post("/inventory/{audit_id}/check")
async def check_saved_inventory_tenant(
    audit_id: UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    audit = (await db.execute(select(TenantAudit).where(TenantAudit.id == audit_id))).scalar_one_or_none()
    if audit is None:
        raise HTTPException(404, "Audited tenant not found")
    credential = (await db.execute(
        select(TenantAuditCredential).where(TenantAuditCredential.audit_id == audit_id)
    )).scalar_one_or_none()
    if credential is None:
        raise HTTPException(409, "Save credentials for this tenant before checking it")
    try:
        password = decrypt_inventory_value(credential.password_ciphertext)
        secret = decrypt_inventory_value(credential.totp_ciphertext) if credential.totp_ciphertext else None
    except Exception:
        logger.exception("Saved checker credentials could not be decrypted for audit %s", audit_id)
        raise HTTPException(500, "Saved credentials could not be decrypted")

    job_id = str(uuid.uuid4())
    checker_jobs[job_id] = {
        "status": "running", "total": 1, "processed": 0, "results": [],
        "started_at": datetime.utcnow().isoformat(), "completed_at": None,
    }
    background_tasks.add_task(
        _run_checker_job, job_id,
        [{"admin_email": audit.admin_email, "admin_password": password, "totp_secret": secret}],
        True, 1,
    )
    return {"job_id": job_id, "total_tenants": 1}


@router.post("/check-csv")
async def check_from_csv(
    background_tasks: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
    credentials_text: Optional[str] = Form(None),
    totp_secret: Optional[str] = Form(None),  # Shared TOTP if all tenants use the same
    headless: bool = Form(True),
    max_workers: int = Form(3),
):
    """
    Upload a CSV of tenants and check which domains are set up.

    CSV must have columns matching (case-insensitive, flexible):
    - Email/Username/Admin column containing admin@xxx.onmicrosoft.com
    - Password column
    - Optional: TOTP Secret column

    Returns a job_id to poll for progress.
    """
    if file:
        content = await file.read()
        text = content.decode("utf-8-sig")
    else:
        text = credentials_text or ""
    tenants = _parse_credentials(text, totp_secret)

    if not tenants:
        raise HTTPException(
            400,
            "No valid tenants found. Supply email + password columns or paste email,password rows.",
        )

    # Clamp max_workers to safe range
    max_workers = max(1, min(max_workers, 10))

    # Create job
    job_id = str(uuid.uuid4())
    checker_jobs[job_id] = {
        "status": "running",
        "total": len(tenants),
        "processed": 0,
        "results": [],
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": None,
    }

    # Run in background
    background_tasks.add_task(_run_checker_job, job_id, tenants, headless, max_workers)

    return {"job_id": job_id, "total_tenants": len(tenants)}


def _parse_credentials(text: str, shared_totp: Optional[str] = None) -> list[dict]:
    """Parse either a headed CSV or pasted comma/tab/pipe-separated rows."""
    text = text.strip()
    if not text:
        return []

    tenants: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [str(name or "").strip().lower() for name in (reader.fieldnames or [])]
    has_header = any(
        "@" not in name
        and ("email" in name or "username" in name or "user name" in name or "admin" in name)
        for name in fieldnames
    )

    if has_header:
        rows = reader
    else:
        # Pasted rows can be: email,password[,totp], using comma, tab, or pipe.
        lines = [line for line in text.splitlines() if line.strip()]
        delimiter = "\t" if any("\t" in line for line in lines) else "|" if any("|" in line for line in lines) else ","
        raw_rows = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
        rows = (
            {"email": values[0], "password": values[1], "totp": values[2] if len(values) > 2 else ""}
            for values in raw_rows
            if len(values) >= 2
        )

    for row in rows:
        email = None
        password = None
        row_totp = None

        for k, v in row.items():
            kl = k.strip().lower()
            if any(x in kl for x in ["email", "user name", "username", "admin"]):
                if v and "@" in v.strip():
                    email = v.strip()
            if any(x in kl for x in ["password", "pass", "pwd"]):
                if v:
                    password = v.strip()
            if any(x in kl for x in ["totp", "mfa", "secret"]):
                if v:
                    row_totp = v.strip()

        if email and password:
            tenants.append({
                "admin_email": email.lower(),
                "admin_password": password,
                "totp_secret": row_totp or shared_totp,  # Row-level overrides shared
            })
    return tenants


@router.post("/check-batch/{batch_id}")
async def check_from_batch(
    batch_id: UUID,
    background_tasks: BackgroundTasks,
    headless: bool = Form(True),
    max_workers: int = Form(3),
    db: AsyncSession = Depends(get_db),
):
    """
    Check domains for all tenants in an existing batch.
    Uses stored credentials and TOTP secrets from the database.
    """
    result = await db.execute(
        select(Tenant).where(Tenant.batch_id == batch_id)
    )
    tenants_db = result.scalars().all()

    if not tenants_db:
        raise HTTPException(404, f"No tenants found in batch {batch_id}")

    tenants = []
    for t in tenants_db:
        tenants.append({
            "admin_email": t.admin_email,
            "admin_password": t.admin_password,
            "totp_secret": t.totp_secret,
        })

    job_id = str(uuid.uuid4())
    checker_jobs[job_id] = {
        "status": "running",
        "total": len(tenants),
        "processed": 0,
        "results": [],
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": None,
    }

    # Clamp max_workers to safe range
    max_workers = max(1, min(max_workers, 10))

    background_tasks.add_task(_run_checker_job, job_id, tenants, headless, max_workers)

    return {"job_id": job_id, "total_tenants": len(tenants)}


@router.get("/jobs/{job_id}", response_model=CheckerJobStatus)
async def get_job_status(job_id: str):
    """Poll job progress."""
    job = checker_jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")

    # Build summary
    results = job["results"]
    summary = {}
    if results:
        auth_ok = sum(1 for r in results if r.get("login_success"))
        auth_fail = len(results) - auth_ok
        checked = [r for r in results if r.get("login_success") and r.get("domain_check_success") and not r.get("login_error")]
        has_verified = sum(1 for r in checked if r.get("verified_count", 0) > 0)
        has_unverified = sum(1 for r in checked if r.get("unverified_count", 0) > 0)
        no_domains = sum(1 for r in checked if r.get("custom_domain_count", 0) == 0)
        total_verified = sum(r.get("verified_count", 0) for r in results)
        total_unverified = sum(r.get("unverified_count", 0) for r in results)

        summary = {
            "auth_success": auth_ok,
            "auth_failed": auth_fail,
            "domain_checks_failed": len(results) - len(checked),
            "tenants_with_verified_domains": has_verified,
            "tenants_with_unverified_domains": has_unverified,
            "tenants_no_domains": no_domains,
            "total_verified_domains": total_verified,
            "total_unverified_domains": total_unverified,
        }

    return CheckerJobStatus(
        job_id=job_id,
        status=job["status"],
        total=job["total"],
        processed=job["processed"],
        results=results,
        summary=summary,
        started_at=job["started_at"],
        completed_at=job.get("completed_at"),
    )


@router.get("/jobs/{job_id}/csv")
async def download_results_csv(job_id: str):
    """Download results as CSV."""
    job = checker_jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    if job["status"] != "complete":
        raise HTTPException(400, "Job not yet complete")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Tenant", "Admin Email", "Login Success", "Check Error", "Domain Check Success",
        "Verified Domain Count", "Unverified Domain Count",
        "Verified Domains", "Unverified Domains",
    ])

    for r in job["results"]:
        verified_names = "; ".join(d["name"] for d in r.get("verified_domains", []))
        unverified_names = "; ".join(d["name"] for d in r.get("unverified_domains", []))

        writer.writerow([
            r.get("tenant_name", ""),
            r.get("admin_email", ""),
            r.get("login_success", False),
            r.get("login_error", ""),
            r.get("domain_check_success", False),
            r.get("verified_count", 0) if r.get("domain_check_success") else "",
            r.get("unverified_count", 0) if r.get("domain_check_success") else "",
            verified_names,
            unverified_names,
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=domain_check_{job_id[:8]}.csv"
        },
    )


# === BACKGROUND TASK ===


async def _enrich_totp_from_db(tenants: list[dict]) -> None:
    """
    For tenants missing a TOTP secret, try to look it up from the database.
    This allows CSV uploads (which often lack TOTP columns) to still work
    if the tenants already exist in the DB with stored TOTP secrets.
    """
    emails_needing_totp = [t["admin_email"] for t in tenants if not t.get("totp_secret")]
    if not emails_needing_totp:
        return

    logger.info(f"Looking up TOTP secrets from DB for {len(emails_needing_totp)} tenants...")
    try:
        async with SessionLocal() as db:
            result = await db.execute(
                select(Tenant.admin_email, Tenant.totp_secret).where(
                    Tenant.admin_email.in_(emails_needing_totp),
                    Tenant.totp_secret.isnot(None),
                )
            )
            db_secrets = {row.admin_email: row.totp_secret for row in result}

        enriched = 0
        for tenant in tenants:
            if not tenant.get("totp_secret") and tenant["admin_email"] in db_secrets:
                tenant["totp_secret"] = db_secrets[tenant["admin_email"]]
                enriched += 1

        if enriched:
            logger.info(f"Enriched {enriched} tenants with TOTP secrets from database")
    except Exception as e:
        logger.warning(f"Could not look up TOTP secrets from DB: {e}")


async def _run_checker_job(job_id: str, tenants: list[dict], headless: bool, max_workers: int = 3):
    """
    Process tenants with chunked parallel processing.
    Uses max_workers for concurrency (user-selected, default: 3).
    """
    job = checker_jobs[job_id]
    total = job["total"]

    # Enrich missing TOTP secrets from database (for CSV uploads)
    await _enrich_totp_from_db(tenants)

    def on_progress(processed, total, latest_result):
        """Update job state as each tenant completes."""
        job["processed"] = processed
        if latest_result:
            job["results"].append(
                latest_result.to_dict() if hasattr(latest_result, 'to_dict') else latest_result
            )

    try:
        logger.info(f"[Job {job_id[:8]}] Starting parallel check: {total} tenants, {max_workers} workers")

        results = await check_tenants_parallel(
            tenants=tenants,
            headless=headless,
            max_workers=max_workers,
            progress_callback=on_progress,
        )

        # Ensure all results are in job (callback may have missed some on exceptions)
        job["results"] = [r.to_dict() if hasattr(r, 'to_dict') else r for r in results]
        job["processed"] = len(results)
        job["status"] = "complete"
        job["completed_at"] = datetime.utcnow().isoformat()

        # Persist the audit output, but never the submitted password or TOTP.
        try:
            await _persist_audit_results(job_id, results)
        except Exception as persist_error:
            logger.exception(
                "[Job %s] Audit completed but inventory persistence failed: %s",
                job_id[:8],
                persist_error,
            )

        # Log summary
        auth_ok = sum(1 for r in results if (r.login_success if hasattr(r, 'login_success') else r.get('login_success')))
        logger.info(f"[Job {job_id[:8]}] Complete — {auth_ok}/{total} auth success")

    except Exception as e:
        logger.error(f"[Job {job_id[:8]}] Job failed: {e}")
        job["status"] = "error"
        job["completed_at"] = datetime.utcnow().isoformat()


async def _persist_audit_results(
    job_id: str,
    results: list[TenantCheckResult | dict],
    session_factory=None,
) -> None:
    """Upsert latest checker results while preserving manual dispositions."""
    checked_at = datetime.now(timezone.utc)
    session_factory = session_factory or SessionLocal
    async with session_factory() as db:
        for result in results:
            data = result.to_dict() if hasattr(result, "to_dict") else result
            email = str(data.get("admin_email", "")).strip().lower()
            if not email:
                continue

            existing_result = await db.execute(
                select(TenantAudit).where(TenantAudit.admin_email == email)
            )
            audit = existing_result.scalar_one_or_none()
            if audit is None:
                audit = TenantAudit(
                    admin_email=email,
                    tenant_name=data.get("tenant_name") or email.split("@")[-1],
                    disposition=TenantDisposition.UNREVIEWED.value,
                    last_checked_at=checked_at,
                )
                db.add(audit)

            login_success = bool(data.get("login_success"))
            domain_check_success = bool(login_success and data.get("domain_check_success") and not data.get("login_error"))
            custom_domain_count = int(data.get("custom_domain_count") or 0)
            audit.tenant_name = data.get("tenant_name") or audit.tenant_name
            audit.login_success = login_success
            audit.domain_check_success = domain_check_success
            audit.login_error = data.get("login_error") or None
            # Authentication alone does not prove the domain list was read.
            audit.is_used = custom_domain_count > 0 if domain_check_success else None
            audit.verified_domains = data.get("verified_domains") or []
            audit.unverified_domains = data.get("unverified_domains") or []
            audit.custom_domain_count = custom_domain_count
            audit.last_checked_at = checked_at
            audit.last_job_id = job_id

        await db.commit()
