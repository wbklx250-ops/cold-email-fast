"""
M365 Setup Service - Automated Domain Verification & DKIM Setup

WORKER QUEUE VERSION - N workers pick up domains as they finish. No pre-calculated delays.

ARCHITECTURE NOTE (Asyncio Event Loop Fix):
============================================
This module carefully separates synchronous Selenium automation from async database operations:

1. SYNC SELENIUM: Runs in thread pool workers via run_in_executor
   - _sync_setup_domain() - Pure synchronous function, NO async, NO database
   - Calls admin_portal.setup_domain_with_retry() which is synchronous
   
2. ASYNC DATABASE: Each domain saves to DB immediately after Selenium finishes
   - Uses BackgroundSessionLocal (NullPool) for fresh connections
   - No stale sessions — each save gets a brand new DB connection
   
This prevents "Task got Future attached to a different loop" errors and
avoids Neon killing idle connections during 5+ minute Selenium runs.
"""

import asyncio
import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.core.config import get_settings
from app.db.session import get_fresh_db_session, BackgroundSessionLocal
from app.models.tenant import Tenant, TenantStatus
from app.models.domain import Domain, DomainStatus
from app.services.cloudflare import cloudflare_service, CloudflareError

# Load settings from .env
_settings = get_settings()
MAX_PARALLEL_BROWSERS = _settings.max_parallel_browsers
STEP6_HEADLESS = _settings.step6_headless
STEP6_DOMAIN_TIMEOUT_SECONDS = _settings.step6_domain_timeout_seconds

logger = logging.getLogger(__name__)


@dataclass
class Step5Result:
    """Result of Step 5 automation for a single tenant."""
    tenant_id: str
    domain_name: str
    success: bool = False
    domain_added: bool = False
    verification_txt: Optional[str] = None
    txt_added_to_cloudflare: bool = False
    domain_verified: bool = False
    mail_dns_added: bool = False
    dkim_config_retrieved: bool = False
    dkim_cnames_added: bool = False
    dkim_enabled: bool = False
    error: Optional[str] = None
    error_step: Optional[str] = None
    steps: Dict[str, Dict] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "tenant_id": self.tenant_id, "domain_name": self.domain_name,
            "success": self.success, "domain_added": self.domain_added,
            "verification_txt": self.verification_txt,
            "txt_added_to_cloudflare": self.txt_added_to_cloudflare,
            "domain_verified": self.domain_verified,
            "mail_dns_added": self.mail_dns_added,
            "dkim_config_retrieved": self.dkim_config_retrieved,
            "dkim_cnames_added": self.dkim_cnames_added,
            "dkim_enabled": self.dkim_enabled,
            "error": self.error, "error_step": self.error_step, "steps": self.steps
        }


# ============================================================
# SYNCHRONOUS SELENIUM WRAPPER
# This function runs in thread pool - NO async, NO database!
# ============================================================

def _sync_setup_domain(tenant_data: dict) -> dict:
    """
    Synchronous wrapper for domain setup via Selenium.
    
    CRITICAL: This function runs in a ThreadPoolExecutor thread.
    - NO async/await allowed
    - NO database operations allowed
    - NO asyncio event loop usage allowed
    - NO stagger delays — semaphore controls parallelism
    
    Args:
        tenant_data: Dict containing:
            - domain: Domain name
            - zone_id: Cloudflare zone ID
            - admin_email: M365 admin email
            - admin_password: M365 admin password
            - totp_secret: Optional TOTP secret for MFA
            - tenant_id: Tenant UUID (for logging only)
    
    Returns:
        Dict with automation results (success, verified, dns_configured, error)
    """
    domain = tenant_data["domain"]
    
    logger.info(f"[{domain}] Starting synchronous Selenium automation")
    
    try:
        # Import here to avoid circular imports and ensure fresh import per thread
        from app.services.selenium.admin_portal import setup_domain_with_retry
        
        # Call the SYNCHRONOUS Selenium function WITH RETRY
        result = setup_domain_with_retry(
            domain=domain,
            zone_id=tenant_data["zone_id"],
            admin_email=tenant_data["admin_email"],
            admin_password=tenant_data["admin_password"],
            totp_secret=tenant_data["totp_secret"],
            headless=STEP6_HEADLESS,
        )
        
        logger.info(f"[{domain}] Selenium automation completed: success={result.get('success')}")
        return result
        
    except Exception as e:
        logger.exception(f"[{domain}] Selenium automation error: {e}")
        return {
            "success": False,
            "verified": False,
            "dns_configured": False,
            "error": str(e)
        }


# ============================================================
# FRESH DB SAVE — Opens a NEW session per domain
# ============================================================

TENANT_CONFLICT_ERROR_MARKERS = (
    "already added to",
    "different microsoft 365 organization",
)


def _is_tenant_conflict_error(error: str | None) -> bool:
    if not error:
        return False
    error_lower = error.lower()
    return any(marker in error_lower for marker in TENANT_CONFLICT_ERROR_MARKERS)


async def _save_step6_result(domain_data: dict, selenium_result: dict):
    """
    Save Selenium result using a FRESH database session (BackgroundSessionLocal).
    
    This avoids the stale session problem where Neon kills idle connections
    during the 5+ minute Selenium run. Each domain gets its own brand-new
    DB connection that is opened AFTER Selenium returns.
    """
    domain_name = domain_data["domain"]
    tenant_id = domain_data["tenant_id"]
    domain_id = domain_data.get("domain_id")
    
    DB_RETRY_COUNT = 3
    
    for db_attempt in range(DB_RETRY_COUNT):
        try:
            async with BackgroundSessionLocal() as session:
                domain_obj = await session.get(Domain, UUID(domain_id)) if domain_id else None
                tenant_obj = await session.get(Tenant, UUID(tenant_id))
                
                if not domain_obj:
                    logger.error(f"[{domain_name}] Domain not found for DB save!")
                    return
                
                # Handle exceptions from gather(return_exceptions=True)
                if isinstance(selenium_result, Exception):
                    selenium_result = {
                        "success": False,
                        "verified": False,
                        "dns_configured": False,
                        "error": str(selenium_result)
                    }
                error_msg = selenium_result.get("error", "Unknown error")
                
                if selenium_result.get("success"):
                    # Full success — domain verified AND DNS configured
                    domain_obj.domain_added_to_m365 = True
                    domain_obj.domain_verified_in_m365 = True
                    domain_obj.domain_verified_at = datetime.utcnow()
                    domain_obj.mx_record_added = True
                    domain_obj.spf_record_added = True
                    domain_obj.autodiscover_added = True
                    domain_obj.dkim_cnames_added = True
                    domain_obj.dkim_enabled = True
                    domain_obj.dkim_enabled_at = datetime.utcnow()
                    domain_obj.step5_complete = True
                    domain_obj.status = DomainStatus.ACTIVE
                    domain_obj.m365_verified_at = datetime.utcnow()
                    domain_obj.mx_configured = True
                    domain_obj.spf_configured = True
                    domain_obj.dns_records_created = True
                    domain_obj.error_message = None
                    
                    # Store DNS values from Selenium result
                    if selenium_result.get("mx_value"):
                        domain_obj.mx_value = selenium_result["mx_value"]
                    if selenium_result.get("spf_value"):
                        domain_obj.spf_value = selenium_result["spf_value"]
                    if selenium_result.get("dkim_selector1_cname"):
                        domain_obj.dkim_selector1 = selenium_result["dkim_selector1_cname"]
                        domain_obj.dkim_selector1_cname = selenium_result["dkim_selector1_cname"]
                    if selenium_result.get("dkim_selector2_cname"):
                        domain_obj.dkim_selector2 = selenium_result["dkim_selector2_cname"]
                        domain_obj.dkim_selector2_cname = selenium_result["dkim_selector2_cname"]
                    if selenium_result.get("verification_txt"):
                        domain_obj.m365_verification_txt = selenium_result["verification_txt"]
                        domain_obj.verification_txt_value = selenium_result["verification_txt"]
                        domain_obj.verification_txt_added = True
                    
                    # Persist corrected zone_id if resolve_zone_id found a mismatch
                    if selenium_result.get("corrected_zone_id"):
                        old_zone_id = domain_obj.cloudflare_zone_id
                        domain_obj.cloudflare_zone_id = selenium_result["corrected_zone_id"]
                        logger.info(f"[{domain_name}] Updated cloudflare_zone_id: {old_zone_id} -> {selenium_result['corrected_zone_id']}")
                    
                    # Clear tenant error
                    if tenant_obj:
                        tenant_obj.setup_error = None
                    
                    await session.commit()
                    
                    # Verify
                    await session.refresh(domain_obj)
                    logger.info(f"[{domain_name}] ✓ DB SAVED: verified={domain_obj.domain_verified_in_m365}, dkim={domain_obj.dkim_enabled}")
                
                elif _is_tenant_conflict_error(error_msg):
                    # Microsoft confirmed ownership but refused to add this
                    # domain because it belongs to another M365 tenant. This is
                    # terminal for the current tenant and must not be stored as
                    # a generic partial success.
                    domain_obj.domain_added_to_m365 = False
                    domain_obj.domain_verified_in_m365 = False
                    domain_obj.dkim_enabled = False
                    domain_obj.status = DomainStatus.ERROR
                    domain_obj.error_message = error_msg
                    if tenant_obj:
                        tenant_obj.setup_error = error_msg
                    await session.commit()
                    logger.error(f"[{domain_name}] ✗ DB SAVED tenant conflict: {error_msg}")

                elif selenium_result.get("verified"):
                    # Partial success — domain verified but DNS may not be complete
                    domain_obj.domain_added_to_m365 = True
                    domain_obj.domain_verified_in_m365 = True
                    domain_obj.domain_verified_at = datetime.utcnow()
                    domain_obj.status = DomainStatus.M365_VERIFIED
                    domain_obj.m365_verified_at = datetime.utcnow()
                    domain_obj.error_message = "Domain verified but DNS setup incomplete"
                    
                    if selenium_result.get("verification_txt"):
                        domain_obj.m365_verification_txt = selenium_result["verification_txt"]
                        domain_obj.verification_txt_value = selenium_result["verification_txt"]
                        domain_obj.verification_txt_added = True
                    
                    await session.commit()
                    logger.info(f"[{domain_name}] ✓ DB SAVED: PARTIAL (verified only)")
                
                else:
                    # Complete failure
                    domain_obj.error_message = error_msg
                    if tenant_obj:
                        tenant_obj.setup_error = error_msg
                    await session.commit()
                    logger.error(f"[{domain_name}] ✗ DB SAVED error: {error_msg}")
                
                return  # Success — exit retry loop
                
        except Exception as e:
            logger.error(f"[{domain_name}] DB save attempt {db_attempt + 1}/{DB_RETRY_COUNT} FAILED: {e}")
            if db_attempt < DB_RETRY_COUNT - 1:
                logger.info(f"[{domain_name}] Retrying DB save in 5 seconds...")
                await asyncio.sleep(5)
            else:
                logger.error(f"[{domain_name}] ✗✗ FAILED TO SAVE TO DB after {DB_RETRY_COUNT} attempts!")
                logger.error(traceback.format_exc())


# ============================================================
# MAIN ASYNC BATCH PROCESSOR — WORKER QUEUE (no stagger)
# ============================================================

async def run_step5_for_batch(
    batch_id: UUID, on_progress=None,
    max_workers: int = None, chunk_size: int = None,
) -> Dict[str, Any]:
    """
    WORKER QUEUE for Step 6 — N workers pick up domains as they finish.
    
    Architecture:
    1. ASYNC: Gather all domain data from database
    2. SEMAPHORE: Controls parallelism — N workers at a time
    3. PER-DOMAIN: Each domain runs Selenium → immediately saves to DB
    
    No pre-calculated stagger delays. When a worker finishes domain X,
    it immediately picks up domain X+N. This is ~10x faster than
    the old cumulative delay approach.
    """
    from app.models.batch import SetupBatch
    
    MAX_PIPELINE_RETRIES = 4  # Match pipeline.py constant
    
    # Use caller-specified limits, or fall back to settings
    effective_workers = max_workers if max_workers is not None else MAX_PARALLEL_BROWSERS
    effective_chunk_size = chunk_size  # None means "no chunking, old behaviour"
    
    # ============ WORKER QUEUE INDICATOR ============
    logger.info("=" * 60)
    logger.info(f"=== WORKER QUEUE: {effective_workers} workers, chunk_size={effective_chunk_size} ===")
    logger.info("=" * 60)
    
    # ============================================================
    # PHASE 1: GATHER DOMAIN DATA (Async, main event loop)
    # ============================================================
    
    logger.info("Phase 1: Gathering domain data from database...")
    
    async with get_fresh_db_session() as db:
        # Find all domains needing M365 setup for this batch
        domains_result = await db.execute(
            select(Domain)
            .join(Tenant, Domain.tenant_id == Tenant.id)
            .where(
                Tenant.batch_id == batch_id,
                Tenant.first_login_completed == True,
                Domain.domain_verified_in_m365.is_not(True),
                (Domain.step5_retry_count <= MAX_PIPELINE_RETRIES) | Domain.step5_retry_count.is_(None),
            )
            .order_by(Domain.domain_index_in_tenant)  # Process domain 0 before 1 before 2
        )
        domains = list(domains_result.scalars().all())
    
    summary = {"batch_id": str(batch_id), "total": len(domains),
               "successful": 0, "failed": 0, "processed": 0, "results": []}
    
    if not domains:
        logger.info("No domains to process")
        return summary
    
    # Prepare data for threads (extract all needed info, no SQLAlchemy objects)
    domains_data = []
    
    for idx, domain in enumerate(domains):
        domain_name = domain.name
        
        # Get tenant info for credentials
        async with get_fresh_db_session() as db:
            tenant = await db.get(Tenant, domain.tenant_id)
        if not tenant:
            logger.warning(f"[{domain_name}] Tenant record not found for domain, skipping")
            summary["results"].append({
                "domain_id": str(domain.id),
                "domain_name": domain_name,
                "success": False,
                "error": "Tenant not found for domain"
            })
            summary["failed"] += 1
            continue
        
        # Validate credentials (from tenant). TOTP is optional here: some
        # tenants have security defaults disabled or otherwise do not prompt
        # for MFA, so Step 6 can still complete with email/password only.
        if not tenant.admin_email or not tenant.admin_password:
            logger.warning(f"[{domain_name}] Missing tenant credentials, skipping")
            summary["results"].append({
                "domain_id": str(domain.id),
                "tenant_id": str(tenant.id),
                "domain_name": domain_name,
                "success": False,
                "error": "Missing credentials (admin_email or admin_password)"
            })
            summary["failed"] += 1
            continue
        
        domain_data = {
            "tenant_id": str(tenant.id),
            "domain_id": str(domain.id),
            "domain": domain_name,
            "zone_id": domain.cloudflare_zone_id,
            "admin_email": tenant.admin_email,
            "admin_password": tenant.admin_password,
            "totp_secret": tenant.totp_secret,
            "already_verified": domain.domain_verified_in_m365,
        }
        
        domains_data.append(domain_data)
        
        logger.info(f"Queued domain {idx+1}/{len(domains)}: {domain_name} (tenant={tenant.name})")
    
    if not domains_data:
        logger.info("No valid domains to process after validation")
        return summary
    
    total = len(domains_data)
    logger.info(f"Phase 1 complete: {total} domains ready for processing")
    logger.info(f"============================================================")
    logger.info(f"=== WORKER QUEUE: {effective_workers} workers, chunk_size={effective_chunk_size}, {total} domains ===")
    logger.info(f"============================================================")
    
    # ============================================================
    # PHASE 2: CHUNKED WORKER QUEUE — Process N domains at a time,
    # kill all browsers between chunks to prevent memory exhaustion.
    # Matches Step 5's proven pattern in pipeline.py.
    # ============================================================
    
    logger.info(f"Phase 2: Starting worker queue with {effective_workers} workers, chunk_size={effective_chunk_size}...")
    
    semaphore = asyncio.Semaphore(effective_workers)
    processed = 0
    failed = 0
    successful = 0
    lock = asyncio.Lock()
    
    async def process_one(domain_data, index):
        nonlocal processed, failed, successful
        domain_name = domain_data["domain"]
        
        async with semaphore:
            logger.info(f"[{domain_name}] Worker started ({index + 1}/{total})")
            
            try:
                # Run Selenium in thread pool (synchronous)
                selenium_future = asyncio.get_event_loop().run_in_executor(
                    None,
                    _sync_setup_domain,
                    domain_data
                )
                try:
                    selenium_result = await asyncio.wait_for(
                        selenium_future,
                        timeout=STEP6_DOMAIN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    error_msg = (
                        f"Selenium automation timed out after "
                        f"{STEP6_DOMAIN_TIMEOUT_SECONDS}s; ChromeDriver likely stopped responding"
                    )
                    logger.error(f"[{domain_name}] {error_msg}")
                    selenium_future.cancel()
                    try:
                        from app.services.selenium.browser import kill_all_browsers
                        logger.info(f"[{domain_name}] Cleaning up browsers after Step 6 timeout")
                        kill_all_browsers()
                    except Exception as cleanup_error:
                        logger.warning(f"[{domain_name}] Browser cleanup after timeout failed: {cleanup_error}")
                    selenium_result = {
                        "success": False,
                        "verified": False,
                        "dns_configured": False,
                        "error": error_msg,
                    }
                
                # Save to DB with FRESH session immediately
                await _save_step6_result(domain_data, selenium_result)
                
                # Update counters
                async with lock:
                    if isinstance(selenium_result, Exception):
                        failed += 1
                    elif selenium_result.get("success"):
                        successful += 1
                    else:
                        failed += 1
                    processed += 1
                    remaining = total - processed
                    logger.info(f"Progress: {successful} done, {failed} failed, {remaining} remaining")
                    
                    # Build result entry
                    step_result = Step5Result(
                        tenant_id=domain_data["tenant_id"],
                        domain_name=domain_name
                    )
                    if not isinstance(selenium_result, Exception) and selenium_result.get("success"):
                        step_result.success = True
                        step_result.domain_added = True
                        step_result.domain_verified = True
                        step_result.mail_dns_added = True
                        step_result.dkim_cnames_added = True
                        step_result.dkim_enabled = True
                    else:
                        error_msg = str(selenium_result) if isinstance(selenium_result, Exception) else selenium_result.get("error", "Unknown")
                        step_result.error = error_msg
                        step_result.error_step = "selenium_automation"
                    
                    summary["results"].append(step_result.to_dict())
                
                # Progress callback
                if on_progress:
                    is_success = not isinstance(selenium_result, Exception) and selenium_result.get("success")
                    on_progress(domain_data.get("domain_id"), "complete", "success" if is_success else "failed")
                    
            except Exception as e:
                logger.error(f"[{domain_name}] Exception: {e}")
                logger.error(traceback.format_exc())
                async with lock:
                    failed += 1
                    processed += 1
    
    if effective_chunk_size is not None and effective_chunk_size > 0:
        # === CHUNKED MODE: Process N domains → kill all browsers → next N ===
        # This prevents Chrome memory exhaustion on Railway (the whole point of this fix).
        from app.services.selenium.browser import kill_all_browsers
        
        total_chunks = (total + effective_chunk_size - 1) // effective_chunk_size
        
        for chunk_idx in range(0, total, effective_chunk_size):
            chunk = domains_data[chunk_idx:chunk_idx + effective_chunk_size]
            chunk_num = (chunk_idx // effective_chunk_size) + 1
            
            logger.info(f"Step 6: Processing chunk {chunk_num}/{total_chunks} ({len(chunk)} domains)")
            
            tasks = []
            for i, domain_data in enumerate(chunk):
                tasks.append(asyncio.create_task(process_one(domain_data, chunk_idx + i)))
            
            await asyncio.gather(*tasks, return_exceptions=True)
            
            # === CRITICAL: Kill ALL Chrome between chunks to reclaim memory ===
            logger.info(f"Step 6: Cleaning up browsers after chunk {chunk_num}/{total_chunks}...")
            kill_all_browsers()
            await asyncio.sleep(5)  # Let OS reclaim memory
    else:
        # === LEGACY MODE: Launch all with semaphore (no chunking) ===
        tasks = []
        for i, domain_data in enumerate(domains_data):
            # Small stagger between initial launches to avoid login detection
            if i > 0 and i % effective_workers == 0:
                await asyncio.sleep(3)
            tasks.append(asyncio.create_task(process_one(domain_data, i)))
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    summary["successful"] = successful
    summary["failed"] = failed
    summary["processed"] = processed
    
    logger.info(f"Phase 2 complete: All Selenium tasks finished")
    
    # ============================================================
    # PHASE 3: UPDATE BATCH STATUS
    # ============================================================
    
    logger.info("Phase 3: Updating batch status...")
    
    async with get_fresh_db_session() as db:
        batch = await db.get(SetupBatch, batch_id)
        if batch and summary["failed"] == 0 and summary["successful"] > 0:
            if batch.current_step == 5:
                batch.current_step = 6
                if batch.completed_steps is None:
                    batch.completed_steps = []
                if 5 not in batch.completed_steps:
                    batch.completed_steps = batch.completed_steps + [5]
                await db.commit()
                logger.info("Batch advanced to step 6")
    
    logger.info("=" * 60)
    logger.info(f"=== STEP 6 COMPLETE: {summary['successful']}/{summary['total']} succeeded, {summary['failed']} failed ===")
    logger.info("=" * 60)
    
    return summary


# ============================================================
# SINGLE TENANT PROCESSOR (for individual runs)
# ============================================================

async def run_step5_for_tenant(db: AsyncSession, tenant_id: UUID, on_progress=None) -> Step5Result:
    """
    Run Step 5 for a single tenant.
    
    Uses the same async-safe pattern as batch processing.
    """
    result = Step5Result(tenant_id=str(tenant_id), domain_name="unknown")
    
    logger.info(f"[SINGLE TENANT] Starting Step 5 for tenant_id={tenant_id}")
    
    try:
        # PHASE 1: Gather data
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            result.error = "Tenant not found"
            result.error_step = "lookup"
            logger.error(f"[SINGLE TENANT] Tenant {tenant_id} not found")
            return result
        
        result.domain_name = tenant.custom_domain or tenant.name
        logger.info(f"[{result.domain_name}] Found tenant: {tenant.name}")
        
        if not tenant.domain_id:
            result.error = "No domain linked"
            result.error_step = "lookup"
            logger.error(f"[{result.domain_name}] No domain linked")
            return result
        
        domain = await db.get(Domain, tenant.domain_id)
        if not domain:
            result.error = "Domain not found"
            result.error_step = "lookup"
            logger.error(f"[{result.domain_name}] Domain {tenant.domain_id} not found")
            return result
        
        # Validate credentials. TOTP is optional unless Microsoft prompts for
        # MFA during the browser login.
        if not tenant.admin_email or not tenant.admin_password:
            result.error = "Missing credentials"
            result.error_step = "credential_check"
            tenant.setup_error = result.error
            await db.commit()
            logger.error(f"[{result.domain_name}] Missing credentials")
            return result
        
        tenant_data = {
            "tenant_id": str(tenant.id),
            "domain_id": str(domain.id),
            "domain": result.domain_name,
            "zone_id": domain.cloudflare_zone_id,
            "admin_email": tenant.admin_email,
            "admin_password": tenant.admin_password,
            "totp_secret": tenant.totp_secret,
        }
        
        # PHASE 2: Run Selenium in thread
        logger.info(f"[{result.domain_name}] Starting Selenium automation...")
        loop = asyncio.get_event_loop()
        selenium_future = loop.run_in_executor(None, _sync_setup_domain, tenant_data)
        try:
            selenium_result = await asyncio.wait_for(
                selenium_future,
                timeout=STEP6_DOMAIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            error_msg = (
                f"Selenium automation timed out after "
                f"{STEP6_DOMAIN_TIMEOUT_SECONDS}s; ChromeDriver likely stopped responding"
            )
            logger.error(f"[{result.domain_name}] {error_msg}")
            selenium_future.cancel()
            try:
                from app.services.selenium.browser import kill_all_browsers
                logger.info(f"[{result.domain_name}] Cleaning up browsers after Step 6 timeout")
                kill_all_browsers()
            except Exception as cleanup_error:
                logger.warning(f"[{result.domain_name}] Browser cleanup after timeout failed: {cleanup_error}")
            selenium_result = {
                "success": False,
                "verified": False,
                "dns_configured": False,
                "error": error_msg,
            }
        logger.info(f"[{result.domain_name}] Selenium result: {selenium_result}")
        
        # PHASE 3: Save to DB using fresh session
        tenant_data["domain_id"] = str(domain.id)
        await _save_step6_result(tenant_data, selenium_result)
        
        if selenium_result.get("success"):
            result.success = True
            result.domain_added = True
            result.domain_verified = True
            result.txt_added_to_cloudflare = True
            result.mail_dns_added = True
            result.dkim_cnames_added = True
            result.dkim_enabled = True
        elif selenium_result.get("verified"):
            result.domain_added = True
            result.domain_verified = True
            result.error = "Domain verified but DNS setup incomplete"
            result.error_step = "dns_setup"
        else:
            result.error = selenium_result.get("error", "Unknown error")
            result.error_step = "selenium_automation"
        
    except Exception as e:
        logger.exception(f"[{result.domain_name}] CRITICAL ERROR in run_step5_for_tenant: {e}")
        result.error = f"Unhandled exception: {str(e)}"
        result.error_step = "unknown"
    
    return result


# ============================================================
# LEGACY M365SetupService CLASS (kept for compatibility)
# ============================================================

class M365SetupService:
    """
    Legacy service class - kept for API compatibility.
    
    New code should use run_step5_for_batch() or run_step5_for_tenant() directly.
    """
    
    def __init__(self, db: AsyncSession):
        self.db = db
        self.cloudflare = cloudflare_service
    
    def validate_credentials(self, tenant: Tenant) -> Optional[str]:
        if not tenant.admin_email:
            return "No admin_email"
        if not tenant.admin_password:
            return "No admin_password"
        return None
    
    async def setup_tenant_domain(self, tenant: Tenant, domain: Domain, on_progress=None) -> Step5Result:
        """Setup a single tenant domain using the new async-safe pattern."""
        return await run_step5_for_tenant(self.db, tenant.id, on_progress)
