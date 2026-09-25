"""
Pipeline API — Collect-everything-upfront, then execute automatically.
"""
import asyncio
import logging
import os
import random
import time
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, BackgroundTasks
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, or_

from app.db.session import get_db_session as get_db, SessionLocal
from app.models.batch import SetupBatch, BatchStatus
from app.models.domain import Domain, DomainStatus
from app.models.tenant import Tenant, TenantStatus
from app.models.mailbox import Mailbox
from app.models.pipeline_log import PipelineLog
from app.services.pipeline_readiness import (
    PipelineBlocked, first_blocker, load_batch_state, refresh_nameservers,
    require_ready, sync_manual_m365_setup, reconciliation_complete, refresh_counters,
)
from app.services.validation_service import (
    parse_domains_csv_content,
    parse_tenants_csv_content,
    parse_credentials_txt_content,
    cross_validate,
)
from app.services.tenant_import import tenant_import_service
from app.services.cloudflare import cloudflare_service
from app.services.selenium.admin_portal import enable_org_smtp_auth
from app.services.selenium.browser import kill_all_browsers

router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])
logger = logging.getLogger(__name__)

# In-memory pipeline job tracking
pipeline_jobs = {}

MAX_PIPELINE_RETRIES = 4   # Max retries per tenant per step
STEP5_MAX_WORKERS = 2      # Max parallel browsers for first login (Railway memory limit)
# M365 admin-center pages are heavy enough that two parallel Chromium sessions
# can crash tabs or wedge ChromeDriver on Railway. Keep Step 6 serial and
# reclaim the browser after every domain.
STEP6_MAX_WORKERS = 1
STEP6_CHUNK_SIZE = 1

def _fmt_err(exc: Exception) -> str:
    """Format exception for logging — never returns empty string."""
    msg = str(exc)
    if msg:
        return f"{type(exc).__name__}: {msg}"
    return f"{type(exc).__name__} (no message)"


STEP_NAMES = {
    1: "Create Cloudflare Zones",
    2: "Update Nameservers",
    3: "Verify NS Propagation",
    4: "Cloudflare Redirects",
    5: "First Login Automation",
    6: "M365 Domain Setup & DKIM",
    7: "Create Mailboxes & Delegate",
    8: "Enable SMTP Auth",
    9: "Export Credentials",
    10: "Upload to Sequencer",
    11: "Reconciliation & Verification",
}


def _step5_incomplete_domain_filter():
    return or_(
        Domain.domain_verified_in_m365.is_not(True),
        Domain.dkim_enabled.is_not(True),
        Domain.step5_complete.is_not(True),
        Domain.dmarc_configured.is_not(True),
        Domain.dkim_cnames_added.is_not(True),
        Domain.mx_record_added.is_not(True),
        Domain.spf_record_added.is_not(True),
        Domain.autodiscover_added.is_not(True),
    )


def _step5_ready_domain_filters():
    return (
        Domain.step5_complete == True,
        Domain.domain_verified_in_m365 == True,
        Domain.dkim_enabled == True,
        Domain.dmarc_configured == True,
        Domain.dkim_cnames_added == True,
        Domain.mx_record_added == True,
        Domain.spf_record_added == True,
        Domain.autodiscover_added == True,
    )


@router.post("/validate")
async def validate_inputs(
    domains_csv: UploadFile = File(...),
    tenants_csv: UploadFile = File(...),
    credentials_txt: UploadFile = File(None),
    first_name: str = Form(""),
    last_name: str = Form(""),
    domains_per_tenant: int = Form(1),
    mailboxes_per_tenant: int = Form(50),
):
    """
    Validate all input files without creating anything.
    Returns preview counts and any errors/warnings.
    Call this on file upload for instant feedback.
    Credentials TXT is optional — batches can be created without credentials.
    """
    domains_content = (await domains_csv.read()).decode("utf-8-sig")
    tenants_content = (await tenants_csv.read()).decode("utf-8-sig")
    creds_content = (await credentials_txt.read()).decode("utf-8-sig") if credentials_txt else ""

    domains, domain_errors = parse_domains_csv_content(domains_content)
    tenants, tenant_errors = parse_tenants_csv_content(tenants_content)
    credentials, cred_errors = parse_credentials_txt_content(creds_content) if creds_content else ({}, [])

    # If parsing failed, return errors immediately
    all_parse_errors = domain_errors + tenant_errors + cred_errors
    if all_parse_errors:
        return {
            "valid": False,
            "errors": all_parse_errors,
            "warnings": [],
            "summary": {
                "domains_count": len(domains),
                "tenants_count": len(tenants),
                "credentials_matched": 0,
            }
        }

    # Cross-validate with user-selected mailboxes_per_tenant (25-100, default 50)
    result = cross_validate(domains, tenants, credentials, first_name, last_name, mailboxes_per_tenant, domains_per_tenant)
    return result


@router.post("/create-and-start")
async def create_and_start(
    batch_name: str = Form(...),
    domains_csv: UploadFile = File(...),
    tenants_csv: UploadFile = File(...),
    credentials_txt: UploadFile = File(None),
    first_name: str = Form(""),
    last_name: str = Form(""),
    sequencer_platform: str = Form(""),
    sequencer_account_id: str = Form(""),
    domains_per_tenant: int = Form(1),
    mailboxes_per_tenant: int = Form(50),
    sequencer_api_key: str = Form(""),
    profile_photo: UploadFile = File(None),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
):
    """
    Create batch with all data and start the automated pipeline.

    This is the MAIN entry point. It:
    1. Validates all inputs
    2. Creates the batch
    3. Imports domains
    4. Imports tenants + credentials
    5. Links domains to tenants (1:1 in order)
    6. Saves all configuration
    7. Starts the pipeline in background
    8. Returns batch_id for progress tracking
    """
    # Read file contents
    domains_content = (await domains_csv.read()).decode("utf-8-sig")
    tenants_content = (await tenants_csv.read()).decode("utf-8-sig")
    creds_content = (await credentials_txt.read()).decode("utf-8-sig") if credentials_txt else ""

    # Parse and validate
    domains, domain_errors = parse_domains_csv_content(domains_content)
    tenants, tenant_errors = parse_tenants_csv_content(tenants_content)
    credentials, cred_errors = parse_credentials_txt_content(creds_content) if creds_content else ({}, [])

    all_errors = domain_errors + tenant_errors + cred_errors
    if all_errors:
        raise HTTPException(400, detail={"errors": all_errors})

    validation = cross_validate(domains, tenants, credentials, first_name, last_name, mailboxes_per_tenant, domains_per_tenant)
    if not validation["valid"]:
        raise HTTPException(400, detail={"errors": validation["errors"]})

    # Save profile photo if provided
    photo_path = None
    if profile_photo and profile_photo.filename:
        photo_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "uploads", "batch_photos")
        os.makedirs(photo_dir, exist_ok=True)
        photo_path = os.path.join(photo_dir, f"{batch_name}_{profile_photo.filename}")
        with open(photo_path, "wb") as f:
            f.write(await profile_photo.read())

    # Create batch
    batch = SetupBatch(
        name=batch_name,
        status=BatchStatus.IN_PROGRESS,
        current_step=1,
        new_admin_password="#Sendemails1",  # Always hardcoded
        persona_first_name=first_name,
        persona_last_name=last_name,
        mailboxes_per_tenant=mailboxes_per_tenant,
        domains_per_tenant=domains_per_tenant,
        sequencer_platform=sequencer_platform or None,
        sequencer_login_email=None,  # No longer collected here
        sequencer_login_password=None,  # No longer collected here
        profile_photo_path=photo_path,
        pipeline_status="running",
        pipeline_step=1,
        pipeline_step_name=STEP_NAMES[1],
        pipeline_started_at=datetime.utcnow(),
        total_domains=len(domains),
        total_tenants=len(tenants),
    )
    db.add(batch)
    await db.flush()  # Get batch.id

    batch_id = batch.id

    # Import domains — handle duplicates by reusing existing domain records
    imported_domain_count = 0
    for d in domains:
        parts = d["name"].rsplit(".", 1)
        tld = parts[-1] if len(parts) > 1 else ""

        # Check if domain already exists
        existing = (await db.execute(
            select(Domain).where(Domain.name == d["name"])
        )).scalar_one_or_none()

        if existing:
            # Re-assign to this batch (domain may have been in a deleted/old batch)
            existing.batch_id = batch_id
            # CRITICAL: Clear old tenant linkage so import_tenants can assign new tenants
            existing.tenant_id = None
            existing.redirect_url = d.get("redirect_url", "") or existing.redirect_url
            # Always overwrite per-domain persona from current CSV so re-uploads
            # with updated persona take effect. None means "fall back to batch".
            existing.persona_first_name = d.get("first_name") or None
            existing.persona_last_name = d.get("last_name") or None
            existing.status = DomainStatus.PURCHASED
            existing.cloudflare_zone_status = existing.cloudflare_zone_status or "pending"

            # CRITICAL: Reset ALL M365/pipeline state so the new batch processes this domain fresh
            existing.domain_added_to_m365 = False
            existing.domain_verified_in_m365 = False
            existing.domain_verified_at = None
            existing.dkim_enabled = False
            existing.dkim_cnames_added = False
            existing.dkim_enabled_at = None
            existing.mx_record_added = False
            existing.spf_record_added = False
            existing.autodiscover_added = False
            existing.step5_complete = False
            existing.step5_retry_count = 0
            existing.step5_skipped = False
            existing.step6_complete = False
            existing.step6_mailboxes_created = 0
            existing.step6_skipped = False
            existing.error_message = None
            # CRITICAL: Also reset CF verification state. Zone IDs stay (pre-flight
            # in run_pipeline Step 1 validates them across all accounts), but the
            # "I verified these records exist" flags must clear so Step 4 re-checks.
            existing.phase1_cname_added = False
            existing.phase1_dmarc_added = False
            existing.dns_records_created = False
            existing.redirect_configured = False
            existing.cloudflare_zone_status = "pending"
            existing.ns_propagated_at = None
            existing.nameservers_updated = False
            existing.domain_index_in_tenant = 0  # Will be re-assigned by auto_link_domains

            imported_domain_count += 1
        else:
            domain = Domain(
                batch_id=batch_id,
                name=d["name"],
                tld=tld,
                redirect_url=d.get("redirect_url", ""),
                persona_first_name=d.get("first_name") or None,
                persona_last_name=d.get("last_name") or None,
                status=DomainStatus.PURCHASED,
                cloudflare_zone_status="pending",
                cloudflare_nameservers=[],
            )
            db.add(domain)
            imported_domain_count += 1

    await db.flush()  # Ensure domain inserts/updates are visible to import_tenants

    # Import tenants with credentials using the existing service
    try:
        logger.info(f"Calling import_tenants for batch {batch_id} with {len(domains)} domains")
        result = await tenant_import_service.import_tenants(
            db, batch_id, tenants_content, creds_content, provider="reseller"
        )
        logger.info(f"import_tenants returned: {result}")
    except Exception as e:
        logger.error(f"import_tenants FAILED: {e}", exc_info=True)
        raise

    # Pull explicit "Domain N to link tenant" assignments collected during import.
    # Empty dict -> Phase 1 is a no-op and the linker behaves exactly like before.
    explicit_map = result.get("explicit_domain_map", {}) or {}

    # Auto-link domains to tenants. Phase 1 honors the explicit map; Phase 2
    # legacy-fills any remaining tenants/domains.
    try:
        link_result = await tenant_import_service.auto_link_domains(
            db,
            batch_id,
            domains_per_tenant,
            explicit_map=explicit_map,
        )
        logger.info(f"auto_link_domains result: {link_result}")
    except Exception as e:
        logger.error(f"auto_link_domains FAILED: {e}", exc_info=True)
        raise

    await db.commit()

    # Surface explicit-link issues as warnings (validation already enforced
    # the hard constraints, but late race conditions / DB state can still
    # produce unmatched / conflicting / overflow entries here).
    extra_warnings: List[str] = []
    if link_result.get("unmatched_domains"):
        extra_warnings.append(
            f"Explicit assignment: {len(link_result['unmatched_domains'])} "
            f"domain name(s) not found in batch and skipped: "
            f"{', '.join(link_result['unmatched_domains'][:5])}"
            + ("…" if len(link_result["unmatched_domains"]) > 5 else "")
        )
    if link_result.get("conflicting_domains"):
        extra_warnings.append(
            f"Explicit assignment: {len(link_result['conflicting_domains'])} "
            f"domain(s) were already linked to a different tenant and skipped: "
            f"{', '.join(link_result['conflicting_domains'][:5])}"
            + ("…" if len(link_result["conflicting_domains"]) > 5 else "")
        )
    if link_result.get("overflow_domains"):
        extra_warnings.append(
            f"Explicit assignment: {len(link_result['overflow_domains'])} "
            f"domain(s) exceeded the per-tenant cap of {domains_per_tenant} "
            f"and were ignored."
        )


    # Initialize pipeline job tracking
    job_id = str(batch_id)
    pipeline_jobs[job_id] = {
        "status": "starting",
        "batch_id": job_id,
        "batch_name": batch_name,
        "started_at": datetime.utcnow().isoformat(),
        "current_step": 1,
        "current_step_name": STEP_NAMES[1],
        "message": "Starting pipeline...",
        "total_domains": len(domains),
        "total_tenants": validation["summary"]["credentials_matched"],
        "steps": {str(i): {"status": "pending", "completed": 0, "failed": 0, "total": 0} for i in range(1, 12)},
        "errors": [],
        "activity_log": [],
    }

    # Start pipeline in background
    background_tasks.add_task(run_pipeline, batch_id)

    return {
        "success": True,
        "batch_id": str(batch_id),
        "batch_name": batch_name,
        "domains_imported": imported_domain_count,
        "tenants_imported": result.get("imported", 0),
        "tenants_linked": link_result.get("linked", 0),
        "tenants_linked_explicit": link_result.get("linked_explicit", 0),
        "tenants_linked_auto": link_result.get("linked_auto", 0),
        "pipeline_started": True,
        "warnings": list(validation.get("warnings", [])) + extra_warnings,
        "explicit_link_summary": {
            "tenants_with_explicit": link_result.get("tenants_with_explicit", 0),
            "unmatched_domains": link_result.get("unmatched_domains", []),
            "conflicting_domains": link_result.get("conflicting_domains", []),
            "overflow_domains": link_result.get("overflow_domains", []),
        },
    }



def _default_pipeline_steps(batch: SetupBatch) -> dict:
    """Build dashboard step state from persisted batch counters."""
    current_step = batch.pipeline_step or 0
    pipeline_status = batch.pipeline_status or "unknown"
    steps = {
        str(i): {"status": "pending", "completed": 0, "failed": 0, "total": 0}
        for i in range(1, 12)
    }

    for step in range(1, min(current_step, 12)):
        steps[str(step)]["status"] = "completed"

    if 1 <= current_step <= 11:
        if pipeline_status == "paused" and current_step == 2:
            current_status = "waiting_for_user"
        elif pipeline_status in ("running", "paused", "error", "completed"):
            current_status = pipeline_status
        else:
            current_status = "pending"
        steps[str(current_step)]["status"] = current_status

    total_domains = batch.total_domains or 0
    total_tenants = batch.total_tenants or 0
    steps["1"].update(completed=batch.zones_completed or 0, total=total_domains)
    steps["3"].update(completed=batch.ns_propagated_count or 0, total=total_domains)
    steps["4"].update(completed=batch.dns_completed or 0, total=total_domains)
    steps["5"].update(completed=batch.first_login_completed_count or 0, total=total_tenants)
    steps["6"].update(completed=batch.m365_completed or 0, total=total_domains)
    steps["7"].update(completed=batch.mailboxes_completed_count or 0, total=total_domains)
    steps["8"].update(completed=batch.smtp_completed or 0, total=total_tenants)
    steps["10"].update(completed=batch.sequencer_uploaded_count or 0, total=total_domains)

    for number in ("1", "3", "4", "5", "6", "7", "8"):
        stage = steps[number]
        if int(number) < current_step:
            stage["status"] = (
                "completed" if stage["total"] > 0 and stage["completed"] >= stage["total"]
                else "pending"
            )
    if current_step > 2:
        steps["2"]["status"] = steps["3"]["status"]

    if pipeline_status == "error" and 1 <= current_step <= 11:
        steps[str(current_step)]["failed"] = batch.errors_count or 0

    return steps


def _pipeline_message(batch: SetupBatch) -> str:
    current_step = batch.pipeline_step or 0
    step_name = batch.pipeline_step_name or STEP_NAMES.get(current_step, "Unknown")
    status = batch.pipeline_status or "unknown"

    if status == "completed":
        return "Pipeline complete!"
    if status == "paused" and current_step == 2:
        return "Waiting for nameserver update confirmation..."
    if status == "paused":
        return f"Pipeline paused at Step {current_step}: {step_name}"
    if status == "error":
        return f"Pipeline error at Step {current_step}: {step_name}"
    if status == "running":
        return f"{step_name}..."
    return step_name


async def _get_nameserver_groups(db: AsyncSession, batch_id: UUID) -> list[dict]:
    result = await db.execute(
        select(Domain.name, Domain.cloudflare_nameservers)
        .where(Domain.batch_id == batch_id)
        .order_by(Domain.name)
    )

    grouped: dict[tuple[str, ...], list[str]] = {}
    for domain_name, nameservers in result.all():
        clean_nameservers = tuple(sorted(ns for ns in (nameservers or []) if ns))
        if not clean_nameservers:
            continue
        grouped.setdefault(clean_nameservers, []).append(domain_name)

    return [
        {"nameservers": list(nameservers), "domains": domains, "count": len(domains)}
        for nameservers, domains in sorted(
            grouped.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
    ]


async def _get_pipeline_errors(db: AsyncSession, batch_id: UUID, limit: int = 20) -> list[dict]:
    result = await db.execute(
        select(PipelineLog)
        .where(
            PipelineLog.batch_id == batch_id,
            PipelineLog.status.in_(("failed", "error")),
        )
        .order_by(PipelineLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        {
            "step": log.step,
            "error": log.error_detail or log.message or f"{log.step_name} failed",
        }
        for log in logs
    ]


async def _get_pipeline_activity(db: AsyncSession, batch_id: UUID, limit: int = 50) -> list[dict]:
    result = await db.execute(
        select(PipelineLog)
        .where(PipelineLog.batch_id == batch_id)
        .order_by(PipelineLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        {
            "step": log.step,
            "step_name": log.step_name,
            "item_name": log.item_name,
            "status": log.status,
            "message": log.message,
            "timestamp": log.created_at.isoformat(),
        }
        for log in logs
    ]


async def _build_db_pipeline_status(
    db: AsyncSession,
    batch: SetupBatch,
    batch_id: UUID,
) -> dict:
    return {
        "status": batch.pipeline_status or "unknown",
        "batch_id": str(batch_id),
        "batch_name": batch.name,
        "current_step": batch.pipeline_step or 0,
        "current_step_name": batch.pipeline_step_name or STEP_NAMES.get(batch.pipeline_step, "Unknown"),
        "message": _pipeline_message(batch),
        "total_domains": batch.total_domains or 0,
        "total_tenants": batch.total_tenants or 0,
        "domains_per_tenant": batch.domains_per_tenant or 1,
        "nameserver_groups": await _get_nameserver_groups(db, batch_id),
        "steps": _default_pipeline_steps(batch),
        "errors": await _get_pipeline_errors(db, batch_id),
        "activity_log": await _get_pipeline_activity(db, batch_id),
        "completed_at": batch.pipeline_completed_at.isoformat() if batch.pipeline_completed_at else None,
    }


@router.get("/{batch_id}/status")
async def get_pipeline_status(batch_id: UUID, db: AsyncSession = Depends(get_db)):
    """Get real-time pipeline status for the progress dashboard."""
    job_id = str(batch_id)
    batch = await db.get(SetupBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    if job_id in pipeline_jobs:
        status = pipeline_jobs[job_id]
        status.setdefault("message", _pipeline_message(batch))
        status.setdefault("domains_per_tenant", batch.domains_per_tenant or 1)
        status.setdefault("steps", _default_pipeline_steps(batch))
        if "errors" not in status:
            status["errors"] = await _get_pipeline_errors(db, batch_id)
        if "activity_log" not in status:
            status["activity_log"] = await _get_pipeline_activity(db, batch_id)
        if not status.get("nameserver_groups"):
            status["nameserver_groups"] = await _get_nameserver_groups(db, batch_id)
        return status

    return await _build_db_pipeline_status(db, batch, batch_id)


@router.post("/{batch_id}/confirm-nameservers")
async def confirm_nameservers(
    batch_id: UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """User confirms they've updated nameservers at Porkbun. Resumes pipeline."""
    batch = await db.get(SetupBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    batch.ns_confirmed_at = datetime.utcnow()
    await db.commit()

    job_id = str(batch_id)

    # Check if background task is still alive
    task_alive = (job_id in pipeline_jobs and
                  pipeline_jobs[job_id].get("status") == "running")

    if task_alive:
        # Task is alive and polling — just set the flag
        pipeline_jobs[job_id]["ns_confirmed"] = True
        pipeline_jobs[job_id]["message"] = "Nameservers confirmed — checking propagation..."
        logger.info(f"NS confirmed for {batch_id} — pipeline task is alive, will pick up flag")
    else:
        # Task is DEAD — re-launch pipeline from Step 3 (NS propagation)
        logger.warning(f"NS confirmed for {batch_id} but pipeline task is DEAD — re-launching from Step 3")
        pipeline_jobs.pop(job_id, None)  # Clear stale state
        batch.pipeline_status = "running"
        await db.commit()
        background_tasks.add_task(run_pipeline, batch_id, 3)  # Skip Steps 1-2

    return {"success": True, "message": "Nameservers confirmed. Pipeline resuming."}


@router.post("/{batch_id}/skip-failed-domains")
async def skip_failed_domains(
    batch_id: UUID,
    step: int = 6,
    db: AsyncSession = Depends(get_db),
):
    """
    Mark all failed/stuck domains for a given step as 'skipped' so the pipeline can continue.

    This is for domains that CANNOT complete a step (e.g., domain stuck in old tenant)
    and need to be excluded from further processing.
    """
    batch = await db.get(SetupBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    skipped_count = 0

    if step == 6:
        # Step 6 = M365 Domain Setup & DKIM
        # Find domains that DON'T have domain_verified_in_m365=True AND aren't already skipped
        result = await db.execute(
            select(Domain).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                _step5_incomplete_domain_filter(),
                Domain.step5_skipped.is_not(True),
            )
        )
        failed_domains = result.scalars().all()

        for d in failed_domains:
            d.step5_skipped = True
            d.error_message = "MANUALLY SKIPPED - domain cannot be released from old tenant"
            skipped_count += 1

    elif step == 7:
        # Step 7 = Mailbox Creation
        result = await db.execute(
            select(Domain).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                *_step5_ready_domain_filters(),
                Domain.step6_complete.is_not(True),
                Domain.step6_skipped.is_not(True),
            )
        )
        failed_domains = result.scalars().all()

        for d in failed_domains:
            d.step6_skipped = True
            d.error_message = "MANUALLY SKIPPED"
            skipped_count += 1

    await db.commit()

    return {
        "success": True,
        "message": f"Skipped {skipped_count} failed domains at step {step}",
        "skipped_count": skipped_count,
    }


# --- Pydantic model for skip-domains request ---
class SkipDomainsRequest(BaseModel):
    domain_names: Optional[List[str]] = None  # Specific domains to skip by name
    skip_all_failed: bool = False  # Or skip ALL failed domains for this step
    reason: str = "Cannot be released from old tenant"


@router.post("/{batch_id}/skip-domains")
async def skip_domains(
    batch_id: UUID,
    request: SkipDomainsRequest,
    step: int = 6,
    db: AsyncSession = Depends(get_db),
):
    """
    Skip specific domains (or all failed) at a given pipeline step.

    Two modes:
    1. Provide domain_names list -> skip exactly those domains
    2. Set skip_all_failed=True -> skip every domain that hasn't completed this step

    After skipping, the pipeline can be resumed and will proceed with only the successful domains.
    """
    batch = await db.get(SetupBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    skipped = []
    not_found = []
    already_done = []

    if step == 6:
        # Step 6 = M365 Domain Setup & DKIM
        if request.skip_all_failed:
            # Find ALL domains that haven't completed M365 setup and aren't already skipped
            result = await db.execute(
                select(Domain).where(
                    Domain.batch_id == batch_id,
                    Domain.tenant_id.isnot(None),
                    _step5_incomplete_domain_filter(),
                    Domain.step5_skipped.is_not(True),
                )
            )
            domains_to_skip = result.scalars().all()
            for d in domains_to_skip:
                d.step5_skipped = True
                d.error_message = f"SKIPPED: {request.reason}"
                skipped.append(d.name)

        elif request.domain_names:
            # Skip specific domains by name
            for domain_name in request.domain_names:
                clean_name = domain_name.strip().lower()
                if not clean_name:
                    continue

                result = await db.execute(
                    select(Domain).where(
                        Domain.batch_id == batch_id,
                        Domain.name == clean_name,
                    )
                )
                domain = result.scalar_one_or_none()

                if not domain:
                    not_found.append(clean_name)
                elif (
                    domain.step5_complete
                    and domain.domain_verified_in_m365
                    and domain.dkim_enabled
                    and domain.dmarc_configured
                ):
                    already_done.append(clean_name)
                elif domain.step5_skipped:
                    already_done.append(clean_name)
                else:
                    domain.step5_skipped = True
                    domain.error_message = f"SKIPPED: {request.reason}"
                    skipped.append(clean_name)
        else:
            raise HTTPException(400, "Provide domain_names list or set skip_all_failed=True")

    elif step == 7:
        # Step 7 = Mailbox Creation
        if request.skip_all_failed:
            result = await db.execute(
                select(Domain).where(
                    Domain.batch_id == batch_id,
                    Domain.tenant_id.isnot(None),
                    *_step5_ready_domain_filters(),
                    Domain.step6_complete.is_not(True),
                    Domain.step6_skipped.is_not(True),
                )
            )
            domains_to_skip = result.scalars().all()
            for d in domains_to_skip:
                d.step6_skipped = True
                d.error_message = f"SKIPPED: {request.reason}"
                skipped.append(d.name)

        elif request.domain_names:
            for domain_name in request.domain_names:
                clean_name = domain_name.strip().lower()
                if not clean_name:
                    continue
                result = await db.execute(
                    select(Domain).where(
                        Domain.batch_id == batch_id,
                        Domain.name == clean_name,
                    )
                )
                domain = result.scalar_one_or_none()
                if not domain:
                    not_found.append(clean_name)
                elif domain.step6_complete:
                    already_done.append(clean_name)
                elif domain.step6_skipped:
                    already_done.append(clean_name)
                else:
                    domain.step6_skipped = True
                    domain.error_message = f"SKIPPED: {request.reason}"
                    skipped.append(clean_name)
        else:
            raise HTTPException(400, "Provide domain_names list or set skip_all_failed=True")
    else:
        raise HTTPException(400, f"Skip not supported for step {step}")

    await db.commit()

    await log_activity(
        batch_id, step, STEP_NAMES.get(step, f"Step {step}"),
        status="skipped",
        message=f"Skipped {len(skipped)} domains: {request.reason}"
    )

    return {
        "success": True,
        "skipped": skipped,
        "skipped_count": len(skipped),
        "not_found": not_found,
        "already_done": already_done,
        "message": f"Skipped {len(skipped)} domains. {len(not_found)} not found, {len(already_done)} already completed/skipped."
    }


@router.get("/{batch_id}/failed-domains")
async def get_failed_domains(
    batch_id: UUID,
    step: int = 6,
    db: AsyncSession = Depends(get_db),
):
    """
    Get all domains that are stuck/failed at a given step.
    Returns domain names, their error messages, and current status flags.
    """
    batch = await db.get(SetupBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    if step == 6:
        # Domains that haven't completed M365 setup
        failed_result = await db.execute(
            select(Domain).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                _step5_incomplete_domain_filter(),
                Domain.step5_skipped.is_not(True),
            ).order_by(Domain.name)
        )
        succeeded_result = await db.execute(
            select(Domain).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                *_step5_ready_domain_filters(),
            ).order_by(Domain.name)
        )
        skipped_result = await db.execute(
            select(Domain).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                Domain.step5_skipped == True,
            ).order_by(Domain.name)
        )
    elif step == 7:
        failed_result = await db.execute(
            select(Domain).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                *_step5_ready_domain_filters(),
                Domain.step6_complete.is_not(True),
                Domain.step6_skipped.is_not(True),
            ).order_by(Domain.name)
        )
        succeeded_result = await db.execute(
            select(Domain).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                Domain.step6_complete == True,
            ).order_by(Domain.name)
        )
        skipped_result = await db.execute(
            select(Domain).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                Domain.step6_skipped == True,
            ).order_by(Domain.name)
        )
    else:
        raise HTTPException(400, f"Step {step} not supported")

    failed = failed_result.scalars().all()
    succeeded = succeeded_result.scalars().all()
    skipped_list = skipped_result.scalars().all()

    return {
        "step": step,
        "step_name": STEP_NAMES.get(step, f"Step {step}"),
        "failed": [
            {
                "id": str(d.id),
                "name": d.name,
                "error": d.error_message,
                "retry_count": d.step5_retry_count if step == 6 else 0,
                "domain_added": d.domain_added_to_m365 or False,
                "domain_verified": d.domain_verified_in_m365 or False,
                "dkim_enabled": d.dkim_enabled or False,
            }
            for d in failed
        ],
        "succeeded": [{"id": str(d.id), "name": d.name} for d in succeeded],
        "skipped": [{"id": str(d.id), "name": d.name, "error": d.error_message} for d in skipped_list],
        "summary": {
            "failed_count": len(failed),
            "succeeded_count": len(succeeded),
            "skipped_count": len(skipped_list),
            "total": len(failed) + len(succeeded) + len(skipped_list),
        }
    }


@router.post("/{batch_id}/retry-failed")
async def retry_failed(
    batch_id: UUID,
    step: int = None,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
):
    """Retry failed items from a specific step or current step."""
    batch = await db.get(SetupBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    # === GUARD: Reject if pipeline is already running ===
    if batch.pipeline_status == "running":
        job_id = str(batch_id)
        if job_id in pipeline_jobs and pipeline_jobs[job_id].get("status") == "running":
            raise HTTPException(409, "Pipeline is already running. Pause first before retrying.")

    # Reset retry counts for the target step(s) using bulk update
    from sqlalchemy import update as sql_update

    if step == 5 or step is None:
        await db.execute(
            sql_update(Tenant).where(
                Tenant.batch_id == batch_id,
                Tenant.first_login_completed.is_not(True),
            ).values(step4_retry_count=0, setup_error=None)
        )
    if step == 6 or step is None:
        await db.execute(
            sql_update(Domain).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                _step5_incomplete_domain_filter(),
            ).values(step5_retry_count=0, step5_skipped=False, error_message=None)
        )
    if step == 7 or step is None:
        # Reset Domain-level step6 completion (domain-based iteration)
        failed_tenant_ids = select(Domain.tenant_id).where(
            Domain.batch_id == batch_id,
            Domain.tenant_id.isnot(None),
            Domain.step6_complete.is_not(True),
        )
        await db.execute(
            sql_update(Domain).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                Domain.step6_complete.is_not(True),
            ).values(step6_complete=False, step6_skipped=False, step6_mailboxes_created=0, error_message=None)
        )
        # Also reset Tenant-level for backward compatibility
        await db.execute(
            sql_update(Tenant).where(
                Tenant.id.in_(failed_tenant_ids),
            ).values(step6_complete=False, step6_retry_count=0, step6_error=None)
        )
    if step == 8 or step is None:
        await db.execute(
            sql_update(Tenant).where(
                Tenant.batch_id == batch_id,
                Tenant.step7_smtp_auth_enabled.is_not(True),
            ).values(step7_retry_count=0, step7_error=None)
        )

    # Determine start step
    start_step = step or batch.pipeline_step or 1

    batch.pipeline_status = "running"
    batch.status = BatchStatus.IN_PROGRESS
    batch.completed_at = None
    batch.pipeline_completed_at = None
    await db.commit()

    job_id = str(batch_id)
    pipeline_jobs.pop(job_id, None)  # Clear stale state

    background_tasks.add_task(run_pipeline, batch_id, start_step)

    return {
        "success": True,
        "message": f"Retrying from step {start_step} ({STEP_NAMES.get(start_step, 'Unknown')})",
    }


@router.post("/{batch_id}/pause")
async def pause_pipeline(batch_id: UUID, db: AsyncSession = Depends(get_db)):
    """Pause the pipeline. In-progress operations will complete."""
    job_id = str(batch_id)
    if job_id in pipeline_jobs:
        pipeline_jobs[job_id]["status"] = "paused"
        pipeline_jobs[job_id]["message"] = "Pipeline paused by user"

    batch = await db.get(SetupBatch, batch_id)
    if batch:
        batch.pipeline_status = "paused"
        batch.pipeline_paused_at = datetime.utcnow()
        await db.commit()

    return {"success": True}


@router.post("/{batch_id}/resume")
async def resume_pipeline(
    batch_id: UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Resume a paused/crashed pipeline from where it left off."""
    batch = await db.get(SetupBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    # === GUARD: Reject if pipeline is already running ===
    job_id = str(batch_id)
    if batch.pipeline_status == "running":
        if job_id in pipeline_jobs and pipeline_jobs[job_id].get("status") == "running":
            logger.warning(f"Resume rejected for batch {batch_id} — pipeline already running")
            raise HTTPException(409, "Pipeline is already running. Pause first before resuming.")
        # DB says running but in-memory doesn't — stale DB state from crash, allow resume
        logger.warning(f"DB says running but no in-memory job for batch {batch_id} — allowing resume")

    # Determine which step to resume from
    resume_step = batch.pipeline_step or 1

    # If we were on Step 2 (NS wait) and NS is already confirmed, skip to 3
    if resume_step == 2 and batch.ns_confirmed_at:
        resume_step = 3

    batch.pipeline_status = "running"
    batch.pipeline_paused_at = None
    await db.commit()

    job_id = str(batch_id)
    pipeline_jobs.pop(job_id, None)  # Clear stale in-memory state

    logger.info(f"Resuming pipeline for batch {batch_id} from step {resume_step}")
    background_tasks.add_task(run_pipeline, batch_id, resume_step)

    return {
        "success": True,
        "message": f"Pipeline resumed from step {resume_step} ({STEP_NAMES.get(resume_step, 'Unknown')})",
    }


class RestartFromStepRequest(BaseModel):
    step: int
    force: bool = True


@router.post("/{batch_id}/restart-from-step")
async def restart_from_step(
    batch_id: UUID,
    request: RestartFromStepRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Restart the pipeline from any step (1..11), resetting all progress for that step
    and every step after it. Steps before the chosen step keep their progress.

    Behavior per step (cascading: choosing step N also resets N+1..11 flags):
      - 1: clear CF zone state (zone_id, nameservers, phase1 DNS flags)
      - 2/3: clear ns_confirmed_at, ns_propagated_at, nameservers_updated
      - 4: clear dns_records_created, redirect_configured
      - 5: clear tenant first_login_completed, step4_retry_count, setup_error
      - 6: clear domain m365/dkim flags + tenant step5_complete
      - 7: clear domain step6_* + tenant step6_* (this is what mailbox creation needs)
      - 8: clear tenant step7_smtp_auth_enabled / step7_retry_count / step7_error
      - 9-11: just rerun (no resets, idempotent)

    Sets pipeline_step=N, pipeline_status='running', clears pipeline_paused_at,
    then schedules run_pipeline(batch_id, start_from_step=N) in the background.
    """
    from sqlalchemy import update as sql_update

    step = request.step
    if step < 1 or step > 11:
        raise HTTPException(400, "step must be between 1 and 11")

    batch = await db.get(SetupBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    # Refuse if pipeline is actively running (must Pause first)
    job_id = str(batch_id)
    if batch.pipeline_status == "running":
        if job_id in pipeline_jobs and pipeline_jobs[job_id].get("status") == "running":
            raise HTTPException(
                409,
                "Pipeline is currently running. Pause it first, then restart from a step.",
            )

    reset_counts = {"domains": 0, "tenants": 0, "mailboxes": 0}

    # ---- STEP 1: Cloudflare zone creation ----
    if step <= 1:
        res = await db.execute(
            sql_update(Domain)
            .where(Domain.batch_id == batch_id)
            .values(
                cloudflare_zone_id=None,
                cloudflare_nameservers=[],
                cloudflare_zone_status="pending",
                phase1_cname_added=False,
                phase1_dmarc_added=False,
            )
        )
        reset_counts["domains"] = max(reset_counts["domains"], res.rowcount or 0)

    # ---- STEP 2-3: Nameserver update + propagation ----
    if step <= 3:
        await db.execute(
            sql_update(Domain)
            .where(Domain.batch_id == batch_id)
            .values(
                ns_propagated_at=None,
                nameservers_updated=False,
            )
        )
        batch.ns_confirmed_at = None
        batch.ns_propagated_count = 0

    # ---- STEP 4: DNS records + redirects ----
    if step <= 4:
        await db.execute(
            sql_update(Domain)
            .where(Domain.batch_id == batch_id)
            .values(
                dns_records_created=False,
                redirect_configured=False,
            )
        )
        batch.dns_completed = 0

    # ---- STEP 5: First login automation ----
    if step <= 5:
        res = await db.execute(
            sql_update(Tenant)
            .where(Tenant.batch_id == batch_id)
            .values(
                first_login_completed=False,
                first_login_at=None,
                password_changed=False,
                step4_retry_count=0,
                setup_error=None,
            )
        )
        reset_counts["tenants"] = max(reset_counts["tenants"], res.rowcount or 0)
        batch.first_login_completed_count = 0

    # ---- STEP 6: M365 domain setup + DKIM ----
    if step <= 6:
        res = await db.execute(
            sql_update(Domain)
            .where(Domain.batch_id == batch_id)
            .values(
                domain_added_to_m365=False,
                domain_verified_in_m365=False,
                domain_verified_at=None,
                step5_complete=False,
                step5_retry_count=0,
                step5_skipped=False,
                dkim_enabled=False,
                dkim_cnames_added=False,
                dkim_enabled_at=None,
                mx_record_added=False,
                spf_record_added=False,
                autodiscover_added=False,
                error_message=None,
            )
        )
        reset_counts["domains"] = max(reset_counts["domains"], res.rowcount or 0)
        await db.execute(
            sql_update(Tenant)
            .where(Tenant.batch_id == batch_id)
            .values(
                step5_complete=False,
                step5_retry_count=0,
                domain_verified_in_m365=False,
                dkim_enabled=False,
                dkim_cnames_added=False,
            )
        )
        batch.m365_completed = 0

    # ---- STEP 7: Mailbox creation & delegation ----
    if step <= 7:
        res = await db.execute(
            sql_update(Domain)
            .where(Domain.batch_id == batch_id)
            .values(
                step6_complete=False,
                step6_skipped=False,
                step6_mailboxes_created=0,
                licensed_user_created=False,
                error_message=None,
            )
        )
        reset_counts["domains"] = max(reset_counts["domains"], res.rowcount or 0)
        res2 = await db.execute(
            sql_update(Tenant)
            .where(Tenant.batch_id == batch_id)
            .values(
                step6_started=False,
                step6_started_at=None,
                step6_complete=False,
                step6_completed_at=None,
                step6_mailboxes_created=0,
                step6_display_names_fixed=0,
                step6_accounts_enabled=0,
                step6_passwords_set=0,
                step6_upns_fixed=0,
                step6_delegations_done=0,
                step6_retry_count=0,
                step6_error=None,
                mailboxes_created=False,
                mailboxes_configured=0,
                delegation_completed=False,
            )
        )
        reset_counts["tenants"] = max(reset_counts["tenants"], res2.rowcount or 0)
        batch.mailboxes_completed_count = 0

    # ---- STEP 8: SMTP Auth ----
    if step <= 8:
        await db.execute(
            sql_update(Tenant)
            .where(Tenant.batch_id == batch_id)
            .values(
                step7_complete=False,
                step7_completed_at=None,
                step7_smtp_auth_enabled=False,
                step7_retry_count=0,
                step7_error=None,
            )
        )
        batch.smtp_completed = 0

    # ---- STEP 9: Export Credentials (no reset needed, idempotent) ----
    # ---- STEP 10: Upload to Sequencer ----
    if step <= 10:
        batch.uploaded_to_sequencer = False
        batch.uploaded_at = None
        batch.sequencer_uploaded_count = 0

    # ---- STEP 11: Reconciliation (idempotent, no reset needed) ----

    # Update batch state and schedule run_pipeline
    batch.pipeline_step = step
    batch.pipeline_step_name = STEP_NAMES.get(step, f"Step {step}")
    batch.pipeline_status = "running"
    batch.pipeline_paused_at = None
    batch.pipeline_completed_at = None
    batch.completed_at = None
    batch.status = BatchStatus.IN_PROGRESS
    batch.errors_count = 0
    await db.commit()

    # Clear stale in-memory job state so run_pipeline initializes fresh
    pipeline_jobs.pop(job_id, None)

    logger.info(
        f"Restart-from-step: batch {batch_id} -> step {step} "
        f"(reset domains={reset_counts['domains']}, tenants={reset_counts['tenants']})"
    )

    background_tasks.add_task(run_pipeline, batch_id, step)

    return {
        "success": True,
        "restarted_from_step": step,
        "step_name": STEP_NAMES.get(step, f"Step {step}"),
        "reset_counts": reset_counts,
        "message": f"Pipeline restarting from Step {step}: {STEP_NAMES.get(step, 'Unknown')}",
    }


@router.post("/{batch_id}/reset-progress")
async def reset_batch_progress(batch_id: UUID, db: AsyncSession = Depends(get_db)):
    """Reset all step progress for tenants in a batch so the pipeline re-processes them."""
    batch = await db.get(SetupBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found")

    tenant_result = await db.execute(
        update(Tenant).where(Tenant.batch_id == batch_id).values(
            first_login_completed=False,
            first_login_at=None,
            password_changed=False,
            domain_verified_in_m365=False,
            step5_complete=False,
            step6_complete=False,
            step6_started=False,
            step7_complete=False,
            step7_smtp_auth_enabled=False,
            setup_error=None,
            step4_retry_count=0,
            step5_retry_count=0,
            step6_retry_count=0,
            step7_retry_count=0,
            step6_error=None,
            step7_error=None,
        )
    )

    # Also reset Domain-level tracking flags
    domain_result = await db.execute(
        update(Domain).where(Domain.batch_id == batch_id).values(
            domain_added_to_m365=False,
            domain_verified_in_m365=False,
            domain_verified_at=None,
            step5_complete=False,
            step5_retry_count=0,
            step6_complete=False,
            step6_mailboxes_created=0,
            dkim_enabled=False,
            dkim_cnames_added=False,
            dkim_enabled_at=None,
            mx_record_added=False,
            spf_record_added=False,
            autodiscover_added=False,
            licensed_user_created=False,
            error_message=None,
            step5_skipped=False,
            step6_skipped=False,
        )
    )

    # Also reset batch-level counters
    batch.first_login_completed_count = 0
    batch.m365_completed = 0
    batch.mailboxes_completed_count = 0
    batch.smtp_completed = 0
    batch.pipeline_status = "paused"
    batch.pipeline_step = 5  # Resume from Step 5 (Cloudflare steps already done)

    await db.commit()

    return {
        "success": True,
        "message": f"Reset progress for all tenants and domains in batch. Use Resume to restart from Step 5.",
        "tenants_reset": tenant_result.rowcount,
        "domains_reset": domain_result.rowcount,
    }


@router.get("/{batch_id}/activity-log")
async def get_activity_log(batch_id: UUID, limit: int = 50, db: AsyncSession = Depends(get_db)):
    """Get recent activity log entries."""
    result = await db.execute(
        select(PipelineLog)
        .where(PipelineLog.batch_id == batch_id)
        .order_by(PipelineLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()

    return {
        "logs": [
            {
                "step": log.step,
                "step_name": log.step_name,
                "item_type": log.item_type,
                "item_name": log.item_name,
                "status": log.status,
                "message": log.message,
                "error": log.error_detail,
                "timestamp": log.created_at.isoformat(),
            }
            for log in logs
        ]
    }


@router.get("/{batch_id}/credentials-export")
async def export_credentials(batch_id: UUID, db: AsyncSession = Depends(get_db)):
    """Export all mailbox credentials as CSV."""
    import io
    import csv

    result = await db.execute(
        select(Mailbox).where(Mailbox.batch_id == batch_id)
    )
    mailboxes = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["DisplayName", "EmailAddress", "Password", "Domain", "TenantName"])

    for mb in mailboxes:
        # Get tenant for domain info
        tenant = await db.get(Tenant, mb.tenant_id)
        writer.writerow([
            mb.display_name,
            mb.email,
            mb.password,
            tenant.custom_domain if tenant else "",
            tenant.name if tenant else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=credentials_batch_{batch_id}.csv"}
    )


async def _update_pipeline(batch_id: UUID, step: int, status: str, message: str):
    """Update both in-memory job tracker and database."""
    job_id = str(batch_id)

    if job_id in pipeline_jobs:
        pipeline_jobs[job_id]["current_step"] = step
        pipeline_jobs[job_id]["current_step_name"] = STEP_NAMES.get(step, "Unknown")
        pipeline_jobs[job_id]["status"] = status
        pipeline_jobs[job_id]["message"] = message

    try:
        async with SessionLocal() as db:
            batch = await db.get(SetupBatch, batch_id)
            if batch:
                batch.pipeline_step = step
                batch.pipeline_step_name = STEP_NAMES.get(step, "Unknown")
                batch.pipeline_status = status
                if status != "completed":
                    batch.status = BatchStatus.IN_PROGRESS
                    batch.completed_at = None
                    batch.pipeline_completed_at = None
                await db.commit()
    except Exception as e:
        logger.error(f"Failed to update pipeline status in DB: {e}")


async def _check_paused_or_stopped(batch_id: UUID) -> bool:
    """Check if pipeline was paused or stopped by user."""
    job_id = str(batch_id)
    if job_id in pipeline_jobs:
        return pipeline_jobs[job_id].get("status") in ("paused", "stopped")
    return False


async def run_pipeline(batch_id: UUID, start_from_step: int = 1):
    """
    MAIN PIPELINE ORCHESTRATOR.

    Runs Steps 1-10 sequentially, pausing only at Step 2 (NS update).
    Each step calls existing service functions.
    Unfinished prerequisites block dependent work and are never counted as success.
    Supports resuming from any step via start_from_step parameter.
    """
    job_id = str(batch_id)

    # Guard: if another instance is already running for this batch, exit immediately
    if job_id in pipeline_jobs and pipeline_jobs[job_id].get("status") == "running":
        logger.warning(f"Pipeline already running for batch {batch_id} — duplicate task exiting")
        return

    logger.info(f"🚀 Pipeline started for batch {batch_id} from step {start_from_step}")

    # Initialize in-memory job tracker if not exists
    if job_id not in pipeline_jobs:
        async with SessionLocal() as db:
            batch = await db.get(SetupBatch, batch_id)
            if not batch:
                logger.error(f"Batch {batch_id} not found")
                return
            pipeline_jobs[job_id] = {
                "status": "running",
                "batch_id": job_id,
                "batch_name": batch.name or "",
                "started_at": datetime.utcnow().isoformat(),
                "current_step": start_from_step,
                "current_step_name": STEP_NAMES.get(start_from_step, "Unknown"),
                "message": f"Resuming from step {start_from_step}...",
                "total_domains": batch.total_domains or 0,
                "total_tenants": batch.total_tenants or 0,
                "steps": {str(i): {"status": "pending", "completed": 0, "failed": 0, "total": 0} for i in range(1, 12)},
                "errors": [],
                "activity_log": [],
            }

    pipeline_jobs[job_id]["status"] = "running"

    try:
        await _update_pipeline(batch_id, start_from_step, "running", "Checking batch prerequisites...")
        if start_from_step > 3:
            await refresh_nameservers(batch_id)
        if start_from_step > 6:
            await _update_pipeline(batch_id, start_from_step, "running", "Verifying manually completed M365 setup...")
            try:
                await sync_manual_m365_setup(batch_id)
            except Exception as exc:
                logger.warning("Could not verify manual setup: %s", _fmt_err(exc))
        await refresh_counters(batch_id)
        domains, tenants = await load_batch_state(batch_id)
        blocker = first_blocker(domains, tenants, before_step=start_from_step)
        if blocker:
            if blocker.step == 1:
                raise blocker
            requested_step = start_from_step
            start_from_step = min(start_from_step, blocker.step)
            await log_activity(batch_id, start_from_step, STEP_NAMES[start_from_step],
                status="info", message=f"Resume requested at step {requested_step}; recovering from step {start_from_step}: {blocker}")
            # A deliberate recovery gets a fresh retry budget without claiming success.
            async with SessionLocal() as db:
                await db.execute(update(Domain).where(Domain.batch_id == batch_id).values(step5_retry_count=0, step5_skipped=False))
                await db.execute(update(Tenant).where(Tenant.batch_id == batch_id).values(
                    step4_retry_count=0, step6_retry_count=0, step7_retry_count=0))
                await db.commit()

        # Reset retry counts only for fresh pipeline runs (step 1)
        if start_from_step <= 1:
            async with SessionLocal() as db:
                await db.execute(
                    update(Tenant).where(Tenant.batch_id == batch_id).values(
                        step4_retry_count=0,
                        step5_retry_count=0,
                        step6_retry_count=0,
                        step7_retry_count=0,
                    )
                )
                await db.commit()
            logger.info(f"Reset retry counts for batch {batch_id}")

        # ================================================================
        # STEP 1: Create Cloudflare Zones
        # ================================================================
        if start_from_step <= 1:
          try:
            await _update_pipeline(batch_id, 1, "running", "Creating Cloudflare zones...")
            await log_activity(batch_id, 1, STEP_NAMES[1], status="started", message="Starting zone creation")

            # ============================================================
            # PRE-FLIGHT: Validate/refresh cloudflare_zone_id across ALL CF accounts.
            # Domains re-assigned from deleted batches carry stale zone_ids that may
            # be in a different CF account or no longer exist. get_or_create_zone()
            # searches all accounts and prefers active zones. If zone changes, clear
            # downstream flags so DNS/redirect/phase1 loops re-verify.
            # ============================================================
            async with SessionLocal() as db:
                all_batch_domains = (await db.execute(
                    select(Domain).where(Domain.batch_id == batch_id)
                )).scalars().all()

                logger.info(
                    f"Step 1 pre-flight: validating CF state for {len(all_batch_domains)} "
                    f"domains across all accounts"
                )

                preflight_refreshed = 0
                preflight_unchanged = 0
                preflight_errors = 0

                for domain in all_batch_domains:
                    if await _check_paused_or_stopped(batch_id):
                        await _update_pipeline(batch_id, 1, "paused", "Paused by user")
                        return

                    try:
                        zone_data = await cloudflare_service.get_or_create_zone(domain.name)
                        new_zone_id = zone_data.get("zone_id")
                        new_ns = zone_data.get("nameservers") or []
                        new_status = zone_data.get("status", "pending")
                        account_label = zone_data.get("account_label", "?")
                        already_existed = zone_data.get("already_existed", False)

                        zone_changed = domain.cloudflare_zone_id != new_zone_id
                        ns_changed = (domain.cloudflare_nameservers or []) != new_ns

                        if zone_changed or ns_changed or not domain.cloudflare_zone_id:
                            logger.info(
                                f"[{domain.name}] CF pre-flight refresh: "
                                f"zone_id {domain.cloudflare_zone_id} -> {new_zone_id} "
                                f"(account={account_label}, status={new_status}, "
                                f"already_existed={already_existed})"
                            )
                            domain.cloudflare_zone_id = new_zone_id
                            domain.cloudflare_nameservers = new_ns
                            domain.cloudflare_zone_status = new_status
                            if zone_changed:
                                domain.phase1_cname_added = False
                                domain.phase1_dmarc_added = False
                                domain.dns_records_created = False
                                domain.redirect_configured = False
                            preflight_refreshed += 1
                        else:
                            preflight_unchanged += 1

                    except Exception as e:
                        preflight_errors += 1
                        logger.warning(f"[{domain.name}] CF pre-flight failed: {e}")
                        # Don't fail the pipeline — existing Step 1 loop will catch any
                        # domains that still have a null zone_id after this.

                    await db.commit()

                logger.info(
                    f"Step 1 pre-flight complete: refreshed={preflight_refreshed}, "
                    f"unchanged={preflight_unchanged}, errors={preflight_errors}"
                )
                await log_activity(
                    batch_id, 1, STEP_NAMES[1],
                    status="info",
                    message=(
                        f"Pre-flight: {preflight_refreshed} refreshed, "
                        f"{preflight_unchanged} unchanged, {preflight_errors} errors"
                    ),
                )

            async with SessionLocal() as db:
                domains = (await db.execute(
                    select(Domain).where(
                        Domain.batch_id == batch_id,
                        Domain.cloudflare_zone_id == None,  # No zone yet
                    )
                )).scalars().all()

                zones_created = 0
                zones_failed = 0
                ns_groups = {}

                for domain in domains:
                    if await _check_paused_or_stopped(batch_id):
                        await _update_pipeline(batch_id, 1, "paused", "Paused by user")
                        return

                    try:
                        zone_result = await cloudflare_service.create_zone(domain.name)
                        if zone_result.get("zone_id"):
                            domain.cloudflare_zone_id = zone_result["zone_id"]
                            domain.cloudflare_nameservers = zone_result.get("nameservers", [])
                            domain.status = DomainStatus.CF_ZONE_ACTIVE
                            zones_created += 1

                            # Phase 1 DNS: CNAME proxy + DMARC (before NS propagation)
                            try:
                                await cloudflare_service.create_phase1_dns(zone_result["zone_id"], domain.name)
                                domain.phase1_cname_added = True
                                domain.phase1_dmarc_added = True
                                domain.dmarc_configured = True
                            except Exception as dns_e:
                                if "already exists" in str(dns_e).lower():
                                    logger.info(f"Phase 1 DNS already exists for {domain.name} — skipping")
                                    domain.phase1_cname_added = True
                                    domain.phase1_dmarc_added = True
                                    domain.dmarc_configured = True
                                else:
                                    logger.warning(f"Phase 1 DNS failed for {domain.name}: {dns_e}")

                            # Track NS groups
                            ns_key = ",".join(sorted(domain.cloudflare_nameservers or []))
                            if ns_key not in ns_groups:
                                ns_groups[ns_key] = []
                            ns_groups[ns_key].append(domain.name)

                            await log_activity(batch_id, 1, STEP_NAMES[1], "domain", str(domain.id), domain.name, "completed", "Zone created")
                        else:
                            zones_failed += 1
                            domain.error_message = zone_result.get("error", "Zone creation failed")
                            await log_activity(batch_id, 1, STEP_NAMES[1], "domain", str(domain.id), domain.name, "failed", domain.error_message)

                    except Exception as e:
                        zones_failed += 1
                        domain.error_message = str(e)
                        await log_activity(batch_id, 1, STEP_NAMES[1], "domain", str(domain.id), domain.name, "failed", str(e))

                    await db.commit()

                # Update batch counters — count ALL domains with zones (including re-used)
                total_with_zones = await db.scalar(
                    select(func.count(Domain.id)).where(
                        Domain.batch_id == batch_id,
                        Domain.cloudflare_zone_id.isnot(None),
                    )
                ) or 0
                batch = await db.get(SetupBatch, batch_id)
                if batch:
                    batch.zones_completed = total_with_zones
                    await db.commit()

            # Handle re-used domains that already have zone_id but no Phase 1 DNS flags
            async with SessionLocal() as db:
                reused_domains = (await db.execute(
                    select(Domain).where(
                        Domain.batch_id == batch_id,
                        Domain.cloudflare_zone_id.isnot(None),
                        Domain.phase1_cname_added.is_not(True),
                    )
                )).scalars().all()

                for domain in reused_domains:
                    try:
                        await cloudflare_service.create_phase1_dns(domain.cloudflare_zone_id, domain.name)
                        domain.phase1_cname_added = True
                        domain.phase1_dmarc_added = True
                        domain.dmarc_configured = True
                    except Exception as e:
                        if "already exists" in str(e).lower():
                            domain.phase1_cname_added = True
                            domain.phase1_dmarc_added = True
                            domain.dmarc_configured = True
                        else:
                            logger.warning(f"Phase 1 DNS for re-used domain {domain.name}: {e}")
                    await db.commit()

            # Store NS groups in job for frontend display
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["nameserver_groups"] = [
                    {"nameservers": ns.split(","), "domains": doms, "count": len(doms)}
                    for ns, doms in ns_groups.items()
                ]
                pipeline_jobs[job_id]["steps"]["1"]["status"] = "completed"
                pipeline_jobs[job_id]["steps"]["1"]["completed"] = total_with_zones
                pipeline_jobs[job_id]["steps"]["1"]["failed"] = zones_failed

            logger.info(f"Step 1: {zones_created} new zones created, {total_with_zones} total zones ready")

          except PipelineBlocked:
            raise
          except Exception as step_error:
            logger.exception("Step 1 failed")
            raise PipelineBlocked(1, f"Step 1 failed: {_fmt_err(step_error)}") from step_error
        else:
            logger.info(f"Skipping Step 1 (starting from step {start_from_step})")
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["1"]["status"] = "completed"

        await require_ready(batch_id, 2)

        # ================================================================
        # STEP 2-3: Confirm nameservers, then verify EVERY current zone.
        # ================================================================
        if start_from_step <= 3:
            readiness = await refresh_nameservers(batch_id)
            if not readiness:
                raise PipelineBlocked(1, "Batch has no domains")
            active = sum(r["active"] for r in readiness)
            async with SessionLocal() as db:
                batch = await db.get(SetupBatch, batch_id)
                confirmed = bool(batch and batch.ns_confirmed_at)
            if active < len(readiness) and start_from_step <= 2 and not confirmed:
                await _update_pipeline(batch_id, 2, "paused", "Update nameservers, then confirm to check propagation")
                pipeline_jobs[job_id]["steps"]["2"]["status"] = "waiting_for_user"
                await log_activity(batch_id, 2, STEP_NAMES[2], status="started",
                    message=f"{active}/{len(readiness)} zones active; waiting for nameserver confirmation")
                return

            pipeline_jobs[job_id]["steps"]["2"]["status"] = "completed"
            await _update_pipeline(batch_id, 3, "running", "Checking current Cloudflare activation...")
            await log_activity(batch_id, 3, STEP_NAMES[3], status="started")
            deadline = time.monotonic() + 4 * 3600
            while True:
                if await _check_paused_or_stopped(batch_id):
                    return
                active = sum(r["active"] for r in readiness)
                pipeline_jobs[job_id]["steps"]["3"].update(completed=active, total=len(readiness), status="running")
                pipeline_jobs[job_id]["message"] = f"NS propagation: {active}/{len(readiness)} active"
                pipeline_jobs[job_id]["last_heartbeat"] = datetime.utcnow().isoformat()
                if readiness and active == len(readiness):
                    break
                if time.monotonic() >= deadline:
                    pending = ", ".join(r["domain"] for r in readiness if not r["active"])
                    raise PipelineBlocked(3, f"Nameserver propagation timed out; inactive/unverified domains: {pending}")
                await asyncio.sleep(30)
                readiness = await refresh_nameservers(batch_id)
            pipeline_jobs[job_id]["steps"]["3"]["status"] = "completed"
            await log_activity(batch_id, 3, STEP_NAMES[3], status="completed",
                message=f"Live verification: {active}/{len(readiness)} Cloudflare zones active")
        else:
            for step in (2, 3):
                pipeline_jobs[job_id]["steps"][str(step)]["status"] = "completed"

        await require_ready(batch_id, 4)

        # ================================================================
        # STEP 4: Cloudflare redirects only
        # ================================================================
        if start_from_step <= 4:
          try:
            await _update_pipeline(batch_id, 4, "running", "Creating redirects; email DNS is deferred to Admin Center wizard...")
            await log_activity(batch_id, 4, STEP_NAMES[4], status="started")

            async with SessionLocal() as db:
                # Email-auth DNS is created by the Microsoft Admin Center wizard
                # in Step 6. Step 4 may only configure non-mail redirects.
                domains = (await db.execute(
                    select(Domain).where(
                        Domain.batch_id == batch_id,
                        Domain.cloudflare_zone_id.isnot(None),
                    )
                )).scalars().all()

                dns_done = 0
                for domain in domains:
                    if await _check_paused_or_stopped(batch_id):
                        return

                    try:
                        zone_id = domain.cloudflare_zone_id
                        if not zone_id:
                            continue

                        if domain.redirect_url and not getattr(domain, 'redirect_configured', False):
                            try:
                                await cloudflare_service.create_redirect_rule(zone_id, domain.name, domain.redirect_url)
                                domain.redirect_configured = True
                                dns_done += 1
                                await log_activity(batch_id, 4, STEP_NAMES[4], "domain", str(domain.id), domain.name, "completed", "Redirect configured; email DNS deferred to Admin Center wizard")
                            except Exception as re:
                                logger.warning(f"Redirect failed for {domain.name}: {re}")

                    except Exception as e:
                        domain.error_message = str(e)
                        await log_activity(batch_id, 4, STEP_NAMES[4], "domain", str(domain.id), domain.name, "failed", str(e))

                    await db.commit()

                total_dns_done = await db.scalar(
                    select(func.count(Domain.id)).where(
                        Domain.batch_id == batch_id,
                        Domain.dns_records_created == True,
                    )
                ) or 0

                batch = await db.get(SetupBatch, batch_id)
                if batch:
                    batch.dns_completed = total_dns_done
                    await db.commit()

            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["4"]["status"] = "completed"
                pipeline_jobs[job_id]["steps"]["4"]["completed"] = total_dns_done

            logger.info(f"Step 4: {dns_done} redirects configured, {total_dns_done} domains already have wizard DNS")

          except PipelineBlocked:
            raise
          except Exception as step_error:
            logger.exception("Step 4 failed")
            raise PipelineBlocked(4, f"Step 4 failed: {_fmt_err(step_error)}") from step_error
        else:
            logger.info(f"Skipping Step 4 (starting from step {start_from_step})")
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["4"]["status"] = "completed"

        # ================================================================
        # STEP 5: First Login Automation (BATCHED WITH CLEANUP)
        # ================================================================
        if start_from_step <= 5:
          try:
            await _update_pipeline(batch_id, 5, "running", "Running first login automation...")
            await log_activity(batch_id, 5, STEP_NAMES[5], status="started")

            async with SessionLocal() as db:
                batch = await db.get(SetupBatch, batch_id)
                new_password = batch.new_admin_password if batch else "#Sendemails1"

            from app.services.tenant_automation import process_tenants_parallel

            CHUNK_SIZE = 20  # Process 20 tenants at a time, then full cleanup

            for attempt in range(MAX_PIPELINE_RETRIES + 1):
                if await _check_paused_or_stopped(batch_id):
                    return

                # Get all tenants still needing first login
                async with SessionLocal() as db:
                    tenants = (await db.execute(
                        select(Tenant).where(
                            Tenant.batch_id == batch_id,
                            Tenant.first_login_completed.is_not(True),
                            (Tenant.step4_retry_count <= MAX_PIPELINE_RETRIES) | Tenant.step4_retry_count.is_(None),
                        )
                    )).scalars().all()

                    if not tenants:
                        logger.info(f"Step 5: All tenants completed first login")
                        break

                    remaining = len(tenants)
                    logger.info(f"Step 5: Attempt {attempt + 1}/{MAX_PIPELINE_RETRIES + 1} — {remaining} tenants remaining")
                    await _update_pipeline(batch_id, 5, "running",
                        f"First login attempt {attempt + 1} — {remaining} tenants remaining...")

                    all_tenant_data = [
                        {
                            "tenant_id": str(t.id),
                            "admin_email": t.admin_email,
                            "initial_password": t.initial_password or t.admin_password,
                        }
                        for t in tenants
                    ]

                # Process in chunks with cleanup between each
                for chunk_idx in range(0, len(all_tenant_data), CHUNK_SIZE):
                    if await _check_paused_or_stopped(batch_id):
                        return

                    chunk = all_tenant_data[chunk_idx:chunk_idx + CHUNK_SIZE]
                    chunk_num = (chunk_idx // CHUNK_SIZE) + 1
                    total_chunks = (len(all_tenant_data) + CHUNK_SIZE - 1) // CHUNK_SIZE

                    logger.info(f"Step 5: Processing chunk {chunk_num}/{total_chunks} ({len(chunk)} tenants)")
                    await _update_pipeline(batch_id, 5, "running",
                        f"First login — chunk {chunk_num}/{total_chunks} ({len(chunk)} tenants)...")

                    try:
                        results = await process_tenants_parallel(chunk, new_password, max_workers=STEP5_MAX_WORKERS)

                        # Save results immediately after each chunk
                        async with SessionLocal() as db:
                            for r in results:
                                try:
                                    t = await db.get(Tenant, UUID(r["tenant_id"]))
                                    if not t:
                                        continue
                                    if r.get("success"):
                                        if r.get("password_changed"):
                                            t.admin_password = r.get("new_password", new_password)
                                            t.password_changed = True
                                            logger.info(f"[Step 5] Password was CHANGED for {t.admin_email}")
                                        else:
                                            t.password_changed = False
                                            logger.info(f"[Step 5] Password was NOT changed for {t.admin_email}, keeping original")
                                        t.first_login_completed = True
                                        t.first_login_at = datetime.utcnow()
                                        t.setup_error = None
                                        if r.get("totp_secret") and not t.totp_secret:
                                            t.totp_secret = r["totp_secret"]
                                        await log_activity(batch_id, 5, STEP_NAMES[5], "tenant", str(t.id),
                                            t.custom_domain or t.name, "completed")
                                    else:
                                        t.step4_retry_count = (t.step4_retry_count or 0) + 1
                                        t.setup_error = r.get("error", "Unknown")
                                        if t.step4_retry_count > MAX_PIPELINE_RETRIES:
                                            t.first_login_completed = True
                                            t.setup_error = f"SKIPPED after {MAX_PIPELINE_RETRIES} retries: {r.get('error')}"
                                            await log_activity(batch_id, 5, STEP_NAMES[5], "tenant", str(t.id),
                                                t.custom_domain or t.name, "skipped", t.setup_error)
                                        else:
                                            await log_activity(batch_id, 5, STEP_NAMES[5], "tenant", str(t.id),
                                                t.custom_domain or t.name, "failed", r.get("error"))
                                    await db.commit()
                                except Exception as e:
                                    logger.error(f"Failed to save Step 5 result for {r.get('tenant_id')}: {e}")

                    except Exception as e:
                        logger.error(f"Step 5 chunk {chunk_num} crashed: {e}")
                        import traceback
                        logger.error(traceback.format_exc())

                    # === CRITICAL: Kill ALL Chrome between chunks ===
                    logger.info(f"Step 5: Cleaning up browsers after chunk {chunk_num}/{total_chunks}...")
                    kill_all_browsers()
                    await asyncio.sleep(5)

                # After all chunks, cleanup + wait before potential retry
                kill_all_browsers()
                if attempt < MAX_PIPELINE_RETRIES:
                    await asyncio.sleep(10)

            # Final count
            async with SessionLocal() as db:
                login_ok = await db.scalar(
                    select(func.count(Tenant.id)).where(
                        Tenant.batch_id == batch_id, Tenant.first_login_completed == True
                    )
                ) or 0
                batch = await db.get(SetupBatch, batch_id)
                if batch:
                    batch.first_login_completed_count = login_ok
                    await db.commit()

            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["5"]["completed"] = login_ok
                pipeline_jobs[job_id]["steps"]["5"]["status"] = "completed"
            logger.info(f"Step 5 complete: {login_ok} tenants logged in successfully")

            # === FINAL CLEANUP before Step 6 ===
            logger.info("Step 5 done — full browser cleanup before Step 6...")
            kill_all_browsers()
            await asyncio.sleep(10)

          except PipelineBlocked:
            raise
          except Exception as step_error:
            logger.exception("Step 5 failed")
            raise PipelineBlocked(5, f"Step 5 failed: {_fmt_err(step_error)}") from step_error
        else:
            logger.info(f"Skipping Step 5 (starting from step {start_from_step})")
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["5"]["status"] = "completed"

        await refresh_nameservers(batch_id)
        await require_ready(batch_id, 6)

        # ================================================================
        # STEP 6: M365 Domain Setup + DKIM (WITH AUTO-RETRY, CHUNKED)
        # Processes domains in chunks of STEP6_CHUNK_SIZE with full browser
        # cleanup between chunks to prevent Chrome memory exhaustion (OOM).
        # Matches Step 5's proven chunking pattern.
        # ================================================================
        if start_from_step <= 6:
          try:
            # === THOROUGH PRE-STEP-6 CLEANUP ===
            # Step 5 may leave zombie Chrome processes that eat into Step 6's memory budget
            logger.info("Step 6: Pre-flight browser cleanup (killing any zombie Chrome from Step 5)...")
            kill_all_browsers()
            await asyncio.sleep(5)  # Extra time for OS to reclaim memory
            await _update_pipeline(batch_id, 6, "running", "Adding domains to M365 and configuring DKIM...")
            await log_activity(batch_id, 6, STEP_NAMES[6], status="started")

            from app.services.m365_setup import run_step5_for_batch as run_m365_setup

            for attempt in range(MAX_PIPELINE_RETRIES + 1):
                if await _check_paused_or_stopped(batch_id):
                    return

                # Count domains still needing M365 setup
                async with SessionLocal() as db:
                    pending_m365 = await db.scalar(
                        select(func.count(Domain.id)).where(
                            Domain.batch_id == batch_id,
                            Domain.tenant_id.isnot(None),
                            _step5_incomplete_domain_filter(),
                            Domain.step5_skipped.is_not(True),
                            (Domain.step5_retry_count <= MAX_PIPELINE_RETRIES) | Domain.step5_retry_count.is_(None),
                        )
                    ) or 0

                if pending_m365 == 0:
                    logger.info("Step 6: All domains have M365 setup configured")
                    break

                logger.info(f"Step 6: Attempt {attempt + 1}/{MAX_PIPELINE_RETRIES + 1} — {pending_m365} domains remaining")
                await _update_pipeline(batch_id, 6, "running",
                    f"M365 setup attempt {attempt + 1} — {pending_m365} domains remaining...")

                try:
                    m365_result = await run_m365_setup(
                        batch_id,
                        max_workers=STEP6_MAX_WORKERS,
                        chunk_size=STEP6_CHUNK_SIZE,
                    )
                    logger.info(f"Step 6 attempt {attempt + 1} result: {m365_result.get('processed', 0)} processed, {m365_result.get('failed', 0)} failed")
                except Exception as e:
                    logger.error(f"Step 6 attempt {attempt + 1} failed: {e}")

                # Increment retry counts on failed domains and skip if exceeded
                async with SessionLocal() as db:
                    failed_domains = (await db.execute(
                        select(Domain).where(
                            Domain.batch_id == batch_id,
                            Domain.tenant_id.isnot(None),
                            _step5_incomplete_domain_filter(),
                            Domain.step5_skipped.is_not(True),
                        )
                    )).scalars().all()
                    for d in failed_domains:
                        d.step5_retry_count = (d.step5_retry_count or 0) + 1
                        if d.step5_retry_count > MAX_PIPELINE_RETRIES:
                            # Use skip flag instead of lying about verification status
                            d.error_message = f"M365 setup failed after {MAX_PIPELINE_RETRIES + 1} attempts"
                            await log_activity(batch_id, 6, STEP_NAMES[6], "domain", str(d.id),
                                d.name, "failed", d.error_message)
                    await db.commit()

                if attempt < MAX_PIPELINE_RETRIES:
                    logger.info(f"Step 6: Cleaning up browsers before retry attempt {attempt + 2}...")
                    kill_all_browsers()
                    await asyncio.sleep(15)  # Extra time for memory recovery

            await require_ready(batch_id, 7)

            # Only verified domains contribute to successful completion.
            async with SessionLocal() as db:
                m365_ok = await db.scalar(
                    select(func.count(Domain.id)).where(
                        Domain.batch_id == batch_id,
                        Domain.tenant_id.isnot(None),
                        *_step5_ready_domain_filters(),
                    )
                ) or 0
                m365_skipped = await db.scalar(
                    select(func.count(Domain.id)).where(
                        Domain.batch_id == batch_id,
                        Domain.tenant_id.isnot(None),
                        Domain.step5_skipped == True,
                    )
                ) or 0
                batch = await db.get(SetupBatch, batch_id)
                if batch:
                    batch.m365_completed = m365_ok
                    await db.commit()

            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["6"]["completed"] = m365_ok
                pipeline_jobs[job_id]["steps"]["6"]["status"] = "completed"
            logger.info(f"Step 6 complete: {m365_ok} domains M365 configured, {m365_skipped} skipped")

            # === Clean up all Chrome processes before Security Defaults ===
            logger.info("Cleaning up browser processes between Step 6 and Security Defaults...")
            kill_all_browsers()
            await asyncio.sleep(5)

          except PipelineBlocked:
            raise
          except Exception as step_error:
            logger.exception("Step 6 failed")
            raise PipelineBlocked(6, f"Step 6 failed: {_fmt_err(step_error)}") from step_error
        else:
            logger.info(f"Skipping Step 6 (starting from step {start_from_step})")
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["6"]["status"] = "completed"

        await require_ready(batch_id, 7)

        # ================================================================
        # STEP 6.5: Disable Security Defaults (before mailbox creation)
        # ================================================================
        if start_from_step <= 7:
          try:
            kill_all_browsers()
            await asyncio.sleep(3)
            await _update_pipeline(batch_id, 7, "running", "Disabling Security Defaults in Entra ID...")
            logger.info("Step 6.5: Disabling Security Defaults for all tenants...")

            from app.services.step8_security_defaults import SecurityDefaultsDisabler, TenantCredentials as SDTenantCredentials

            async with SessionLocal() as db:
                sd_tenants = (await db.execute(
                    select(Tenant)
                    .join(Domain, Domain.tenant_id == Tenant.id)
                    .where(
                        Tenant.batch_id == batch_id,
                        Domain.batch_id == batch_id,
                        *_step5_ready_domain_filters(),
                        Tenant.security_defaults_disabled.is_not(True),
                        Tenant.totp_secret.isnot(None),
                    )
                    .distinct()
                )).scalars().all()

            sd_ok = 0
            sd_fail = 0
            sd_total = len(sd_tenants)
            if os.getenv("PIPELINE_SKIP_SD_SELENIUM", "0") == "1" and sd_tenants:
                logger.warning(
                    "Step 6.5: Skipping Selenium Security Defaults pass for %s tenants "
                    "because PIPELINE_SKIP_SD_SELENIUM=1",
                    sd_total,
                )
                async with SessionLocal() as db:
                    for t in sd_tenants:
                        tenant = await db.get(Tenant, t.id)
                        if tenant:
                            tenant.security_defaults_error = (
                                "Selenium Security Defaults pass skipped for this run; "
                                "ROPC auth will be verified by Step 7/8"
                            )
                    await db.commit()
                sd_tenants = []

            for t in sd_tenants:
                if await _check_paused_or_stopped(batch_id):
                    return
                domain = t.custom_domain or t.name
                try:
                    worker_id = int(str(t.id).replace("-", "")[:6], 16) % 10000
                    disabler = SecurityDefaultsDisabler(headless=True, worker_id=worker_id)
                    creds = SDTenantCredentials(
                        tenant_id=str(t.id),
                        domain=domain,
                        admin_email=t.admin_email,
                        admin_password=t.admin_password,
                        totp_secret=t.totp_secret,
                    )
                    sd_result = await asyncio.to_thread(disabler.disable_for_tenant, creds)
                    async with SessionLocal() as db:
                        tenant = await db.get(Tenant, t.id)
                        if tenant:
                            if sd_result.get("success"):
                                tenant.security_defaults_disabled = True
                                tenant.security_defaults_error = None
                                tenant.security_defaults_disabled_at = datetime.utcnow()
                                sd_ok += 1
                                logger.info(f"[{domain}] Security Defaults disabled")
                            else:
                                tenant.security_defaults_error = sd_result.get("error") or "Unknown"
                                sd_fail += 1
                                logger.warning(f"[{domain}] Security Defaults failed: {tenant.security_defaults_error}")
                            await db.commit()
                except Exception as e:
                    sd_fail += 1
                    logger.error(f"[{domain}] Security Defaults exception: {e}")
                    async with SessionLocal() as db:
                        tenant = await db.get(Tenant, t.id)
                        if tenant:
                            tenant.security_defaults_error = str(e)
                            await db.commit()

            logger.info(f"Step 6.5 complete: {sd_ok} disabled, {sd_fail} failed out of {sd_total} tenants")

            kill_all_browsers()
            await asyncio.sleep(5)

          except PipelineBlocked:
            raise
          except Exception as step_error:
            logger.exception("Step 7 failed")
            raise PipelineBlocked(7, f"Step 7 failed: {_fmt_err(step_error)}") from step_error

        # ================================================================
        # STEP 7: Create Mailboxes + Delegate (WITH AUTO-RETRY, DOMAIN-BASED)
        # ================================================================
        if start_from_step <= 7:
          try:
            kill_all_browsers()
            await asyncio.sleep(3)
            await _update_pipeline(batch_id, 7, "running", "Creating mailboxes and delegation...")
            await log_activity(batch_id, 7, STEP_NAMES[7], status="started")

            async with SessionLocal() as db:
                batch = await db.get(SetupBatch, batch_id)
                display_name = f"{batch.persona_first_name or ''} {batch.persona_last_name or ''}".strip() if batch else ""

            # Use fast mode (no Chrome, ROPC auth) — falls back to Selenium mode if ROPC fails
            try:
                from app.services.step7_fast import run_step7_fast as run_mailbox_creation
                logger.info("Step 7: Using FAST MODE (no Chrome)")
            except ImportError:
                from app.services.azure_step6 import run_step6_for_batch as run_mailbox_creation
                logger.info("Step 7: Using standard mode (Selenium)")

            for attempt in range(MAX_PIPELINE_RETRIES + 1):
                if await _check_paused_or_stopped(batch_id):
                    return

                # Count DOMAINS still needing mailbox creation (domain-based iteration)
                async with SessionLocal() as db:
                    pending_mb = await db.scalar(
                        select(func.count(Domain.id)).where(
                            Domain.batch_id == batch_id,
                            Domain.tenant_id.isnot(None),
                            *_step5_ready_domain_filters(),
                            Domain.step6_complete.is_not(True),
                            Domain.step6_skipped.is_not(True),
                        )
                    ) or 0

                if pending_mb == 0:
                    logger.info("Step 7: All domains have mailboxes created")
                    break

                logger.info(f"Step 7: Attempt {attempt + 1}/{MAX_PIPELINE_RETRIES + 1} — {pending_mb} domains remaining")
                await _update_pipeline(batch_id, 7, "running",
                    f"Mailbox creation attempt {attempt + 1} — {pending_mb} domains remaining...")

                try:
                    await run_mailbox_creation(batch_id, display_name)
                except Exception as e:
                    logger.error(f"Step 7 attempt {attempt + 1} failed: {e}")

                # On the final attempt, preserve failures for a later retry.
                if attempt >= MAX_PIPELINE_RETRIES:
                    async with SessionLocal() as db:
                        failed_domains = (await db.execute(
                            select(Domain).where(
                                Domain.batch_id == batch_id,
                                Domain.tenant_id.isnot(None),
                                *_step5_ready_domain_filters(),
                                Domain.step6_complete.is_not(True),
                                Domain.step6_skipped.is_not(True),
                            )
                        )).scalars().all()
                        for d in failed_domains:
                            d.step6_skipped = False
                            d.error_message = (
                                d.error_message
                                or f"Mailbox creation failed after {MAX_PIPELINE_RETRIES + 1} attempts"
                            )
                            await log_activity(batch_id, 7, STEP_NAMES[7], "domain", str(d.id),
                                d.name, "failed", d.error_message)
                            if d.tenant_id:
                                t = await db.get(Tenant, d.tenant_id)
                                if t:
                                    t.step6_complete = False
                                    t.step6_error = d.error_message
                        await db.commit()

                if attempt < MAX_PIPELINE_RETRIES:
                    kill_all_browsers()
                    await asyncio.sleep(15)

            # Count completed domains for progress tracking
            async with SessionLocal() as db:
                mb_complete = await db.scalar(
                    select(func.count(Domain.id)).where(
                        Domain.batch_id == batch_id,
                        Domain.tenant_id.isnot(None),
                        Domain.step6_complete == True,
                    )
                ) or 0
                batch = await db.get(SetupBatch, batch_id)
                if batch:
                    batch.mailboxes_completed_count = mb_complete
                    await db.commit()

            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["7"]["completed"] = mb_complete
            async with SessionLocal() as db:
                mb_incomplete = await db.scalar(
                    select(func.count(Domain.id)).where(
                        Domain.batch_id == batch_id,
                        Domain.tenant_id.isnot(None),
                        *_step5_ready_domain_filters(),
                        Domain.step6_complete.is_not(True),
                        Domain.step6_skipped.is_not(True),
                    )
                ) or 0
                batch = await db.get(SetupBatch, batch_id)
                if batch:
                    batch.errors_count = mb_incomplete
                    await db.commit()

            if mb_incomplete:
                message = (
                    f"Step 7 incomplete: {mb_complete} domains completed, "
                    f"{mb_incomplete} domains still require mailbox creation"
                )
                await _update_pipeline(batch_id, 7, "error", message)
                await log_activity(
                    batch_id, 7, STEP_NAMES[7],
                    status="failed",
                    message=message,
                )
                if job_id in pipeline_jobs:
                    pipeline_jobs[job_id]["steps"]["7"]["failed"] = mb_incomplete
                    pipeline_jobs[job_id]["steps"]["7"]["status"] = "error"
                    pipeline_jobs[job_id]["status"] = "error"
                    pipeline_jobs[job_id]["errors"].append({"step": 7, "error": message})
                logger.error(message)
                return

            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["7"]["status"] = "completed"
            logger.info(f"Step 7 complete: {mb_complete} domains mailboxes created")

            # === Clean up all Chrome processes before Step 8 ===
            logger.info("Cleaning up browser processes between Step 7 and Step 8...")
            kill_all_browsers()
            await asyncio.sleep(5)

          except PipelineBlocked:
            raise
          except Exception as step_error:
            logger.exception("Step 7 failed")
            raise PipelineBlocked(7, f"Step 7 failed: {_fmt_err(step_error)}") from step_error
        else:
            logger.info(f"Skipping Step 7 (starting from step {start_from_step})")
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["7"]["status"] = "completed"

        await require_ready(batch_id, 8)

        # ================================================================
        # STEP 8: Enable SMTP Auth (WITH AUTO-RETRY)
        # ================================================================
        if start_from_step <= 8:
          try:
            kill_all_browsers()
            await asyncio.sleep(3)
            await _update_pipeline(batch_id, 8, "running", "Enabling SMTP authentication...")
            await log_activity(batch_id, 8, STEP_NAMES[8], status="started")

            for attempt in range(MAX_PIPELINE_RETRIES + 1):
                if await _check_paused_or_stopped(batch_id):
                    return

                tenant_list = []
                async with SessionLocal() as db:
                    tenants = (await db.execute(
                        select(Tenant).where(
                            Tenant.batch_id == batch_id,
                            Tenant.step6_complete == True,
                            Tenant.step7_smtp_auth_enabled.is_not(True),
                            (Tenant.step7_retry_count <= MAX_PIPELINE_RETRIES) | Tenant.step7_retry_count.is_(None),
                        )
                    )).scalars().all()

                    if not tenants:
                        logger.info("Step 8: All tenants have SMTP auth enabled")
                        break

                    for t in tenants:
                        tenant_list.append({
                            "id": t.id,
                            "admin_email": t.admin_email,
                            "admin_password": t.admin_password,
                            "totp_secret": t.totp_secret,
                            "domain": t.custom_domain or t.name,
                        })

                remaining = len(tenant_list)
                logger.info(f"Step 8: Attempt {attempt + 1}/{MAX_PIPELINE_RETRIES + 1} — {remaining} tenants remaining")
                await _update_pipeline(batch_id, 8, "running",
                    f"SMTP auth attempt {attempt + 1} — {remaining} tenants remaining...")

                for td in tenant_list:
                    if await _check_paused_or_stopped(batch_id):
                        return

                    try:
                        result = await enable_org_smtp_auth(
                            admin_email=td["admin_email"],
                            admin_password=td["admin_password"],
                            totp_secret=td["totp_secret"],
                            domain=td["domain"],
                        )
                    except Exception as e:
                        result = {"success": False, "error": str(e)}

                    async with SessionLocal() as db:
                        tenant = await db.get(Tenant, td["id"])
                        if not tenant:
                            continue
                        if result.get("success"):
                            tenant.step7_complete = True
                            tenant.step7_smtp_auth_enabled = True
                            tenant.step7_error = None
                            await log_activity(batch_id, 8, STEP_NAMES[8], "tenant", str(tenant.id),
                                td["domain"], "completed")
                        else:
                            tenant.step7_retry_count = (tenant.step7_retry_count or 0) + 1
                            tenant.step7_error = result.get("error")
                            if tenant.step7_retry_count > MAX_PIPELINE_RETRIES:
                                tenant.step7_complete = False
                                tenant.step7_smtp_auth_enabled = False
                                tenant.step7_error = (
                                    f"SMTP auth failed after {MAX_PIPELINE_RETRIES + 1} attempts: "
                                    f"{result.get('error')}"
                                )
                                await log_activity(batch_id, 8, STEP_NAMES[8], "tenant", str(tenant.id),
                                    td["domain"], "failed", tenant.step7_error)
                            else:
                                await log_activity(batch_id, 8, STEP_NAMES[8], "tenant", str(tenant.id),
                                    td["domain"], "failed", result.get("error"))
                        await db.commit()

                if attempt < MAX_PIPELINE_RETRIES:
                    kill_all_browsers()
                    await asyncio.sleep(15)

            async with SessionLocal() as db:
                smtp_ok = await db.scalar(
                    select(func.count(Tenant.id)).where(
                        Tenant.batch_id == batch_id,
                        Tenant.step7_smtp_auth_enabled == True,
                    )
                ) or 0
                smtp_incomplete = await db.scalar(
                    select(func.count(Tenant.id)).where(
                        Tenant.batch_id == batch_id,
                        Tenant.step6_complete == True,
                        Tenant.step7_smtp_auth_enabled.is_not(True),
                    )
                ) or 0
                batch = await db.get(SetupBatch, batch_id)
                if batch:
                    batch.smtp_completed = smtp_ok
                    batch.errors_count = smtp_incomplete
                    await db.commit()

            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["8"]["completed"] = smtp_ok
                pipeline_jobs[job_id]["steps"]["8"]["failed"] = smtp_incomplete

            if smtp_incomplete:
                message = (
                    f"Step 8 incomplete: {smtp_ok} tenants have SMTP auth enabled, "
                    f"{smtp_incomplete} still require repair"
                )
                await _update_pipeline(batch_id, 8, "error", message)
                await log_activity(
                    batch_id,
                    8,
                    STEP_NAMES[8],
                    status="failed",
                    message=message,
                )
                if job_id in pipeline_jobs:
                    pipeline_jobs[job_id]["steps"]["8"]["status"] = "error"
                    pipeline_jobs[job_id]["status"] = "error"
                    pipeline_jobs[job_id]["errors"].append(
                        {"step": 8, "error": message}
                    )
                logger.error(message)
                return

            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["8"]["status"] = "completed"
            logger.info(f"Step 8 complete: {smtp_ok} tenants SMTP auth enabled")

            kill_all_browsers()
            await asyncio.sleep(5)

          except PipelineBlocked:
            raise
          except Exception as step_error:
            logger.exception("Step 8 failed")
            raise PipelineBlocked(8, f"Step 8 failed: {_fmt_err(step_error)}") from step_error
        else:
            logger.info(f"Skipping Step 8 (starting from step {start_from_step})")
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["8"]["status"] = "completed"

        await require_ready(batch_id, 9)

        # ================================================================
        # STEP 9: Export Credentials (auto-generated)
        # ================================================================
        if start_from_step <= 9:
          try:
            await _update_pipeline(batch_id, 9, "running", "Generating credentials export...")
            await log_activity(batch_id, 9, STEP_NAMES[9], status="started")
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["9"]["status"] = "completed"
            await log_activity(batch_id, 9, STEP_NAMES[9], status="completed", message="Credentials available for download")
          except PipelineBlocked:
            raise
          except Exception as step_error:
            logger.exception("Step 9 failed")
            raise PipelineBlocked(9, f"Step 9 failed: {_fmt_err(step_error)}") from step_error
        else:
            logger.info(f"Skipping Step 9 (starting from step {start_from_step})")
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["9"]["status"] = "completed"

        # ================================================================
        # STEP 10: Upload to Sequencer (OAuth)
        # ================================================================
        if start_from_step <= 10:
          try:
            async with SessionLocal() as db:
                batch = await db.get(SetupBatch, batch_id)
                has_sequencer = batch and batch.sequencer_platform and batch.sequencer_login_email

            if has_sequencer:
                await _update_pipeline(batch_id, 10, "running", "Uploading to sequencer...")
                await log_activity(batch_id, 10, STEP_NAMES[10], status="started")
                logger.info("Step 10: Sequencer upload not yet implemented — skipping")
                await log_activity(batch_id, 10, STEP_NAMES[10], status="skipped", message="Sequencer upload not yet implemented")
                if job_id in pipeline_jobs:
                    pipeline_jobs[job_id]["steps"]["10"]["status"] = "skipped"
            else:
                logger.info("Step 10: No sequencer configured — skipping")
                if job_id in pipeline_jobs:
                    pipeline_jobs[job_id]["steps"]["10"]["status"] = "skipped"
          except PipelineBlocked:
            raise
          except Exception as step_error:
            logger.exception("Step 10 failed")
            raise PipelineBlocked(10, f"Step 10 failed: {_fmt_err(step_error)}") from step_error
        else:
            logger.info(f"Skipping Step 10 (starting from step {start_from_step})")
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["10"]["status"] = "skipped"

        # ================================================================
        # STEP 11: Reconciliation & Verification (Graph-first, Selenium fallback)
        # ================================================================
        if start_from_step <= 11:
          try:
            await _update_pipeline(batch_id, 11, "running", "Reconciling SD + SMTP state for all tenants...")
            await log_activity(batch_id, 11, STEP_NAMES[11], status="started")

            from app.services.batch_reconciliation import reconcile_batch

            recon_summary = await reconcile_batch(batch_id, auto_fix=True)
            _, expected_tenants = await load_batch_state(batch_id)
            recon_ok = reconciliation_complete(recon_summary, len(expected_tenants))

            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["reconciliation"] = recon_summary
                pipeline_jobs[job_id]["steps"]["11"]["completed"] = (
                    recon_summary.get("sd_ok", 0)
                    + recon_summary.get("sd_drift_fixed", 0)
                )
                pipeline_jobs[job_id]["steps"]["11"]["failed"] = (
                    recon_summary.get("sd_drift_unfixable", 0)
                    + recon_summary.get("smtp_drift_unfixable", 0)
                )
                pipeline_jobs[job_id]["steps"]["11"]["total"] = recon_summary.get("total_tenants", 0)
                pipeline_jobs[job_id]["steps"]["11"]["status"] = (
                    "completed" if recon_ok else "error"
                )

            await log_activity(
                batch_id, 11, STEP_NAMES[11],
                status="completed" if recon_ok else "failed",
                message=(
                    f"SD ok={recon_summary.get('sd_ok', 0)} "
                    f"fixed={recon_summary.get('sd_drift_fixed', 0)} "
                    f"unfixable={recon_summary.get('sd_drift_unfixable', 0)} | "
                    f"SMTP ok={recon_summary.get('smtp_ok', 0)} "
                    f"fixed={recon_summary.get('smtp_drift_fixed', 0)} "
                    f"unfixable={recon_summary.get('smtp_drift_unfixable', 0)} | "
                    f"errors={len(recon_summary.get('errors', []))}"
                ),
            )

            reconciliation_unfixable = (
                recon_summary.get("sd_drift_unfixable", 0)
                + recon_summary.get("smtp_drift_unfixable", 0)
            )
            if not recon_ok:
                message = (
                    "Reconciliation incomplete: "
                    f"{reconciliation_unfixable} failed checks; all {len(expected_tenants)} tenants must be verified without errors"
                )
                raise PipelineBlocked(11, message)
          except PipelineBlocked:
            raise
          except Exception as step_error:
            logger.exception("Step 11 failed")
            raise PipelineBlocked(11, f"Step 11 failed: {_fmt_err(step_error)}") from step_error
        else:
            logger.info(f"Skipping Step 11 (starting from step {start_from_step})")
            if job_id in pipeline_jobs:
                pipeline_jobs[job_id]["steps"]["11"]["status"] = "skipped"

        # ================================================================
        # PIPELINE COMPLETE
        # ================================================================
        await require_ready(batch_id, 12)
        await refresh_counters(batch_id)

        await _update_pipeline(batch_id, 11, "completed", "Pipeline complete!")

        async with SessionLocal() as db:
            batch = await db.get(SetupBatch, batch_id)
            if batch:
                batch.pipeline_status = "completed"
                batch.pipeline_completed_at = datetime.utcnow()
                batch.status = BatchStatus.COMPLETED
                await db.commit()

        if job_id in pipeline_jobs:
            pipeline_jobs[job_id]["status"] = "completed"
            pipeline_jobs[job_id]["completed_at"] = datetime.utcnow().isoformat()

        logger.info(f"✅ Pipeline COMPLETE for batch {batch_id}")
        await log_activity(batch_id, 11, "Pipeline Complete", status="completed", message="All steps finished")

    except PipelineBlocked as exc:
        await _update_pipeline(batch_id, exc.step, "error", str(exc))
        if job_id in pipeline_jobs:
            pipeline_jobs[job_id]["steps"][str(exc.step)]["status"] = "error"
            pipeline_jobs[job_id]["steps"][str(exc.step)]["failed"] = 1
            pipeline_jobs[job_id]["errors"].append({"step": exc.step, "error": str(exc)})
        async with SessionLocal() as db:
            batch = await db.get(SetupBatch, batch_id)
            if batch:
                batch.errors_count = max(batch.errors_count or 0, 1)
                await db.commit()
        await log_activity(batch_id, exc.step, STEP_NAMES[exc.step], status="failed", message=str(exc))
        logger.error("Pipeline blocked: %s", exc)

    except Exception as e:
        logger.error(f"💥 Pipeline CRASHED: {_fmt_err(e)}")
        import traceback
        logger.error(traceback.format_exc())

        await _update_pipeline(batch_id, 0, "error", f"Pipeline error: {_fmt_err(e)}")

        if job_id in pipeline_jobs:
            pipeline_jobs[job_id]["status"] = "error"
            pipeline_jobs[job_id]["error"] = _fmt_err(e)

        async with SessionLocal() as db:
            batch = await db.get(SetupBatch, batch_id)
            if batch:
                batch.pipeline_status = "error"
                await db.commit()


# Helper to log pipeline activity
async def log_activity(
    batch_id,
    step,
    step_name,
    item_type=None,
    item_id=None,
    item_name=None,
    status="started",
    message=None,
    error=None,
):
    """Write to both PipelineLog table and in-memory job."""
    try:
        async with SessionLocal() as db:
            log = PipelineLog(
                batch_id=batch_id,
                step=step,
                step_name=step_name,
                item_type=item_type,
                item_id=item_id,
                item_name=item_name,
                status=status,
                message=message,
                error_detail=error,
            )
            db.add(log)
            await db.commit()
    except Exception as e:
        logger.error(f"Failed to write pipeline log: {e}")

    # Also update in-memory
    job_id = str(batch_id)
    if job_id in pipeline_jobs:
        pipeline_jobs[job_id]["activity_log"].insert(0, {
            "step": step,
            "step_name": step_name,
            "item_name": item_name,
            "status": status,
            "message": message,
            "timestamp": datetime.utcnow().isoformat(),
        })
        # Keep only last 50 entries in memory
        pipeline_jobs[job_id]["activity_log"] = pipeline_jobs[job_id]["activity_log"][:50]


async def resume_interrupted_pipelines():
    """Resume pipelines that were running when the container restarted."""
    try:
        async with SessionLocal() as db:
            running_batches = (await db.execute(
                select(SetupBatch).where(
                    SetupBatch.pipeline_status == "running"
                )
            )).scalars().all()

            for batch in running_batches:
                logger.warning(f"Found interrupted pipeline for batch {batch.id} (was on step {batch.pipeline_step})")
                # Don't auto-resume — mark as paused so user can manually resume
                batch.pipeline_status = "paused"
                batch.pipeline_step_name = f"Interrupted at: {batch.pipeline_step_name or 'Unknown'}"
                await db.commit()
                logger.info(f"Marked batch {batch.id} as paused — user can resume from dashboard")
    except Exception as e:
        logger.error(f"Failed to check for interrupted pipelines: {e}")
