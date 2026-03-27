"""
Step 6 Orchestrator (Selenium -> PowerShell -> Selenium)

Uses Selenium to create the licensed user, PowerShell device code auth
to create shared mailboxes and delegation, then Selenium Admin UI to
set passwords and enable accounts.
"""

import asyncio
try:
    current_loop = asyncio.get_running_loop()
except RuntimeError:
    current_loop = None

if current_loop is None or type(current_loop).__module__ != "uvloop":
    import nest_asyncio

    nest_asyncio.apply()
import logging
import time
from datetime import datetime
from typing import Dict, Any, List
from uuid import UUID

from sqlalchemy import select, update, func

from app.core.config import get_settings
from app.db.session import SessionLocal, async_session_factory, BackgroundSessionLocal
from app.models.batch import SetupBatch, BatchStatus
from app.models.domain import Domain
from app.models.mailbox import Mailbox, MailboxStatus
from app.models.tenant import Tenant, TenantStatus
from app.services.email_generator import generate_emails_for_domain
from app.services.powershell_exchange import PowerShellExchangeService
from app.services.selenium.admin_portal import _login_with_mfa
from app.services.selenium.browser import create_driver, cleanup_driver
from app.services.selenium.user_ops import UserOpsSelenium

logger = logging.getLogger(__name__)


def _format_error(exc: Exception) -> str:
    """Format exception for logging — never returns empty string."""
    msg = str(exc)
    if msg:
        return f"{type(exc).__name__}: {msg}"
    return f"{type(exc).__name__} (no message)"


_progress_store: Dict[str, Dict[str, Any]] = {}


def update_progress(tenant_id: str, step: str, status: str, detail: str = ""):
    """Update progress for real-time UI tracking."""
    _progress_store[tenant_id] = {
        "step": step,
        "status": status,
        "detail": detail,
        "timestamp": time.time(),
    }
    logger.info("[%s] %s: %s - %s", tenant_id[:8], step, status, detail)


def get_progress(tenant_id: str) -> Dict[str, Any]:
    """Get current progress for a tenant."""
    return _progress_store.get(tenant_id, {})


def get_all_progress() -> Dict[str, Dict[str, Any]]:
    """Get progress for all tenants."""
    return dict(_progress_store)


def is_browser_alive(driver) -> bool:
    """Check if the Selenium browser session is still responsive."""
    if driver is None:
        return False
    try:
        # Try to get the current URL - this will fail if browser is dead
        _ = driver.current_url
        return True
    except Exception:
        return False


async def save_to_db_with_retry(operation, max_retries=3, description="DB operation"):
    """
    Execute a database write operation with retry on connection errors.
    Creates a completely fresh session (via NullPool) for each attempt,
    guaranteeing no stale connections from Neon idle timeout.
    """
    for attempt in range(1, max_retries + 1):
        try:
            async with BackgroundSessionLocal() as db:
                await operation(db)
                await db.commit()
                return True
        except Exception as e:
            error_str = str(e)
            is_conn_error = any(phrase in error_str for phrase in [
                "ConnectionDoesNotExistError",
                "connection was closed",
                "connection refused",
                "SSL connection has been closed",
                "server closed the connection",
                "ConnectionResetError",
                "InterfaceError",
                "connection does not exist",
            ])

            if is_conn_error and attempt < max_retries:
                logger.warning(
                    "[%s] DB connection error on attempt %s/%s: %s - retrying in %ss",
                    description, attempt, max_retries, error_str[:100], attempt * 2
                )
                await asyncio.sleep(attempt * 2)  # Exponential backoff
            else:
                logger.error("[%s] DB operation failed after %s attempts: %s", description, attempt, e)
                raise
    return False


async def run_step6_for_batch(batch_id: UUID, display_name: str) -> Dict[str, Any]:
    """Run Step 6 (mailbox creation) for all eligible domains in a batch.
    
    Iterates over DOMAINS (not tenants) so each domain within a multi-domain
    tenant gets its own mailbox range (1-50, 51-100, 101-150, etc.).
    """
    logger.info(
        "Starting Step 6 for batch %s with display name: %s",
        batch_id,
        display_name,
    )

    # --- Phase 1: Use a DB session to set up batch and collect domain info ---
    domain_work_items: List[Dict[str, Any]] = []  # List of {domain_id, tenant_id, domain_index, domain_name}
    total_domains = 0

    async with async_session_factory() as db:
        batch = await db.get(SetupBatch, batch_id)
        if not batch:
            logger.error("Batch %s not found", batch_id)
            return {"success": False, "error": "Batch not found"}

        first_name, last_name = (
            display_name.strip().split(" ", 1)
            if " " in display_name
            else (display_name, "")
        )
        await db.execute(
            update(SetupBatch)
            .where(SetupBatch.id == batch_id)
            .values(
                persona_first_name=first_name,
                persona_last_name=last_name,
            )
        )
        await db.commit()

        # Get all DOMAINS needing mailbox creation for this batch
        # (domain-based iteration instead of tenant-based)
        result = await db.execute(
            select(Domain)
            .join(Tenant, Domain.tenant_id == Tenant.id)
            .where(
                Tenant.batch_id == batch_id,
                Domain.domain_verified_in_m365 == True,
                Domain.dkim_enabled == True,
                Domain.step6_complete.is_not(True),
            )
            .order_by(Tenant.created_at, Domain.domain_index_in_tenant)
        )
        domains = result.scalars().all()

        if not domains:
            logger.warning("No eligible domains found for batch %s", batch_id)
            return {"success": False, "error": "No eligible domains"}

        # Build work items from domains
        for d in domains:
            domain_work_items.append({
                "domain_id": d.id,
                "domain_name": d.name,
                "tenant_id": d.tenant_id,
                "domain_index": d.domain_index_in_tenant,
            })

        total_domains = len(domain_work_items)
        logger.info("Step 6: %s eligible domains for mailbox creation", total_domains)
    # --- Parent session is now CLOSED before parallel work begins ---

    results: List[Dict[str, Any]] = []
    successful = 0
    failed = 0

    # Parallel processing with semaphore (default 5 concurrent browsers)
    settings = get_settings()
    max_parallel = int(settings.max_parallel_browsers) if hasattr(settings, "max_parallel_browsers") else 5
    max_parallel = max(1, min(max_parallel, 10))  # clamp 1-10
    semaphore = asyncio.Semaphore(max_parallel)

    logger.info(
        "Processing %s domains with max_parallel=%s",
        total_domains,
        max_parallel,
    )

    async def _process_one(idx: int, work_item: Dict[str, Any]):
        """Each parallel task processes one domain and gets its own DB session."""
        async with semaphore:
            domain_name = work_item["domain_name"]
            domain_id = work_item["domain_id"]
            tenant_id = work_item["tenant_id"]
            domain_index = work_item["domain_index"]

            logger.info("=" * 80)
            logger.info(
                "BATCH PROGRESS: Domain %s/%s - %s (tenant domain_index=%s)",
                idx,
                total_domains,
                domain_name,
                domain_index,
            )
            logger.info("=" * 80)

            try:
                # Pass domain_id and domain_index to the per-tenant processor
                domain_result = await run_step6_for_tenant(
                    tenant_id,
                    domain_id=domain_id,
                    domain_index=domain_index,
                )
                return {
                    "domain_id": str(domain_id),
                    "tenant_id": str(tenant_id),
                    "domain_name": domain_name,
                    "result": domain_result,
                }
            except Exception as e:
                logger.error(
                    "[%s] Domain exception: %s",
                    domain_name,
                    _format_error(e),
                )
                return {
                    "domain_id": str(domain_id),
                    "tenant_id": str(tenant_id),
                    "domain_name": domain_name,
                    "result": {"success": False, "error": _format_error(e)},
                }

    tasks = [
        _process_one(i, item)
        for i, item in enumerate(domain_work_items, 1)
    ]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    for item in gathered:
        if isinstance(item, Exception):
            failed += 1
            results.append(
                {"domain_id": "unknown", "tenant_id": "unknown", "result": {"success": False, "error": _format_error(item)}}
            )
        else:
            results.append(item)
            if item["result"].get("success"):
                successful += 1
            else:
                failed += 1
                logger.error(
                    "Domain %s failed: %s",
                    item.get("domain_name", "unknown"),
                    item["result"].get("error"),
                )
    
    logger.info("=" * 80)
    logger.info("BATCH COMPLETE: %s/%s domains successful, %s failed", successful, total_domains, failed)
    logger.info("=" * 80)

    # --- Phase 3: Update batch status in a fresh session ---
    async with async_session_factory() as db:
        batch = await db.get(SetupBatch, batch_id)
        if batch:
            completed_steps = batch.completed_steps or []
            if 6 not in completed_steps:
                completed_steps.append(6)

            batch.completed_steps = sorted(completed_steps)

            if failed == 0:
                # All domains succeeded - advance to Step 7
                batch.current_step = 7
                await db.commit()
                logger.info("Batch %s advanced to Step 7 (step 6 done, all domains succeeded)", batch_id)
            else:
                # Some domains failed - stay on Step 6 for retry
                batch.current_step = 6
                await db.commit()
                logger.info("Batch %s step 6 finished with %s failures - batch NOT marked complete", batch_id, failed)

    return {
        "success": failed == 0,
        "total": total_domains,
        "successful": successful,
        "failed": failed,
        "results": results,
    }


async def run_step6_for_tenant(tenant_id: UUID, domain_id: UUID = None, domain_index: int = 0) -> Dict[str, Any]:
    """Run Step 6 for a single tenant/domain using Selenium + PowerShell + Selenium UI.
    
    When domain_id is provided, processes that specific domain within the tenant.
    The domain_index controls mailbox numbering offset (0→1-50, 1→51-100, etc.).
    
    BUG 6.1 FIX: Restructured to load→close→work→reopen→save pattern.
    DB sessions are opened/closed around each phase to prevent Neon idle timeouts.
    """
    # ================================================================
    # PHASE A: Load everything from DB into plain dicts (quick, ~1 second)
    # ================================================================
    tenant_data = None
    domain_data = None  # Domain-specific data (when domain_id provided)
    batch_data = None
    mailbox_list = []
    needs_powershell = True
    needs_admin_ui = True

    # Compute mailbox start index from domain_index (0→1, 1→51, 2→101, etc.)
    mailbox_start_index = domain_index * 50 + 1

    async with async_session_factory() as db:
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            logger.error("Tenant %s not found", tenant_id)
            return {"success": False, "error": "Tenant not found"}

        # Load specific Domain record when domain_id is provided
        domain_record = None
        working_domain = tenant.custom_domain  # fallback
        if domain_id:
            domain_record = await db.get(Domain, domain_id)
            if not domain_record:
                logger.error("Domain %s not found", domain_id)
                return {"success": False, "error": "Domain not found"}
            working_domain = domain_record.name
            logger.info("[%s] Processing domain (index=%s, start_index=%s)", working_domain, domain_index, mailbox_start_index)
        
        logger.info("[%s] Checking current state...", working_domain)

        # Check domain-level completion when domain_id is provided
        if domain_record:
            logger.info("  - domain.step6_complete: %s", domain_record.step6_complete)
            logger.info("  - domain.step6_mailboxes_created: %s", domain_record.step6_mailboxes_created)
            if domain_record.step6_complete:
                logger.info("[%s] SKIPPING - domain already complete", working_domain)
                update_progress(str(tenant.id), "complete", "complete", f"Step 6 already complete for {working_domain}")
                return {"success": True, "skipped": True}
        else:
            # Legacy: check tenant-level completion
            logger.info("  - mailboxes_created: %s", tenant.step6_mailboxes_created)
            logger.info("  - delegations_done: %s", tenant.step6_delegations_done)
            logger.info("  - passwords_set: %s", tenant.step6_passwords_set)
            logger.info("  - complete: %s", tenant.step6_complete)
            if tenant.step6_complete:
                logger.info("[%s] SKIPPING - already complete", working_domain)
                update_progress(str(tenant.id), "complete", "complete", "Step 6 already complete")
                return {"success": True, "skipped": True}

        # RETRY FIX: Clear previous error on retry start
        if tenant.step6_error:
            logger.info("[%s] Clearing previous error for retry: %s", working_domain, tenant.step6_error)
            tenant.step6_error = None
            await db.commit()

        # BUG 6.4 FIX: Check ACTUAL mailbox state in DB, not just counter fields
        # Filter by email domain suffix when processing a specific domain
        base_filter = [Mailbox.tenant_id == tenant.id]
        if domain_id:
            base_filter.append(Mailbox.email.like(f"%@{working_domain}"))

        total_mailbox_count = await db.scalar(
            select(func.count(Mailbox.id)).where(*base_filter)
        ) or 0
        actual_created_count = await db.scalar(
            select(func.count(Mailbox.id)).where(
                *base_filter,
                Mailbox.created_in_exchange == True,
            )
        ) or 0
        actual_delegated_count = await db.scalar(
            select(func.count(Mailbox.id)).where(
                *base_filter,
                Mailbox.delegated == True,
            )
        ) or 0
        actual_password_set_count = await db.scalar(
            select(func.count(Mailbox.id)).where(
                *base_filter,
                Mailbox.password_set == True,
            )
        ) or 0

        needs_powershell = (total_mailbox_count == 0) or (actual_created_count < total_mailbox_count) or (actual_delegated_count < total_mailbox_count)
        needs_admin_ui = (total_mailbox_count == 0) or (actual_password_set_count < total_mailbox_count)

        # DB inconsistency check
        if actual_delegated_count > actual_created_count:
            logger.warning("[%s] DB inconsistency: delegated=%s > created=%s - forcing PowerShell",
                          working_domain, actual_delegated_count, actual_created_count)
            needs_powershell = True

        logger.info("[%s] Actual DB state: total=%s, created=%s, delegated=%s, passwords_set=%s",
                    working_domain, total_mailbox_count, actual_created_count,
                    actual_delegated_count, actual_password_set_count)
        logger.info("[%s] needs_powershell=%s, needs_admin_ui=%s",
                    working_domain, needs_powershell, needs_admin_ui)

        if needs_powershell:
            logger.info("[%s] RUNNING PowerShell...", working_domain)
        else:
            logger.info("[%s] SKIPPING PowerShell - already done", working_domain)

        if needs_admin_ui:
            logger.info("[%s] RUNNING Admin UI password reset...", working_domain)
        else:
            logger.info("[%s] SKIPPING Admin UI password reset - already done", working_domain)

        if not tenant.admin_email or not tenant.admin_password:
            message = "Missing admin credentials"
            logger.error("[%s] %s", working_domain, message)
            tenant.step6_error = message
            await db.commit()
            update_progress(str(tenant.id), "error", "failed", message)
            return {"success": False, "error": message}

        if not tenant.onmicrosoft_domain:
            message = "Missing onmicrosoft domain"
            logger.error("[%s] %s", working_domain, message)
            tenant.step6_error = message
            await db.commit()
            update_progress(str(tenant.id), "error", "failed", message)
            return {"success": False, "error": message}

        # Serialize EVERYTHING we need into plain dicts/values
        tenant_data = {
            "id": tenant.id,
            "batch_id": tenant.batch_id,
            "name": tenant.name,
            "custom_domain": tenant.custom_domain,
            "admin_email": tenant.admin_email,
            "admin_password": tenant.admin_password,
            "totp_secret": tenant.totp_secret,
            "microsoft_tenant_id": tenant.microsoft_tenant_id,
            "onmicrosoft_domain": tenant.onmicrosoft_domain,
            "licensed_user_upn": tenant.licensed_user_upn,
            "licensed_user_password": tenant.licensed_user_password,
            "licensed_user_created": tenant.licensed_user_created,
            "step6_mailboxes_created": tenant.step6_mailboxes_created or 0,
            "step6_passwords_set": tenant.step6_passwords_set or 0,
            "step6_delegations_done": tenant.step6_delegations_done or 0,
        }

        # Serialize domain-specific data when processing a specific domain
        if domain_record:
            domain_data = {
                "id": domain_record.id,
                "name": domain_record.name,
                "domain_index": domain_record.domain_index_in_tenant,
                "licensed_user_upn": domain_record.licensed_user_upn,
                "licensed_user_password": domain_record.licensed_user_password,
                "licensed_user_created": domain_record.licensed_user_created,
            }
            # Use domain-level licensed user info if available (overrides tenant-level)
            if domain_record.licensed_user_created and domain_record.licensed_user_upn:
                tenant_data["licensed_user_upn"] = domain_record.licensed_user_upn
                tenant_data["licensed_user_password"] = domain_record.licensed_user_password
                tenant_data["licensed_user_created"] = domain_record.licensed_user_created

        batch = None
        if tenant.batch_id:
            batch = await db.get(SetupBatch, tenant.batch_id)
        if batch:
            batch_data = {
                "persona_first_name": batch.persona_first_name,
                "persona_last_name": batch.persona_last_name,
                "custom_mailbox_map": batch.custom_mailbox_map,
            }

        # Load existing mailboxes into plain dicts
        # When domain_id is provided, only load mailboxes for this specific domain
        mb_load_filters = [Mailbox.tenant_id == tenant.id]
        if domain_id:
            mb_load_filters.append(Mailbox.email.like(f"%@{working_domain}"))

        existing_mailbox_result = await db.execute(
            select(Mailbox).where(*mb_load_filters)
        )
        existing_mailboxes = existing_mailbox_result.scalars().all()
        mailbox_list = [
            {
                "id": mb.id,
                "email": mb.email,
                "local_part": mb.local_part,
                "display_name": mb.display_name,
                "password": mb.password,
                "created_in_exchange": mb.created_in_exchange,
                "password_set": mb.password_set,
                "delegated": mb.delegated,
                "upn_fixed": getattr(mb, 'upn_fixed', False),
            }
            for mb in existing_mailboxes
        ]
    # ================================================================
    # DB session CLOSED here — safe for long Selenium/PowerShell operations
    # ================================================================

    tid_str = str(tenant_data["id"])
    # Use working_domain (specific domain name) when domain_id provided, else tenant custom_domain
    domain = working_domain if domain_id else tenant_data["custom_domain"]
    update_progress(tid_str, "starting", "in_progress", "Initializing...")

    driver = None
    ps_service = None
    settings = get_settings()
    try:

        # ================================================================
        # PHASE B: Selenium login + Licensed user creation
        # ================================================================
        MAX_BROWSER_RETRIES = 2
        browser_retry_count = 0
        login_successful = False
        
        while browser_retry_count < MAX_BROWSER_RETRIES and not login_successful:
            try:
                browser_retry_count += 1
                update_progress(tid_str, "login", "in_progress",
                    f"Logging into M365 Admin Portal... (attempt {browser_retry_count}/{MAX_BROWSER_RETRIES})")
                
                if driver:
                    try:
                        cleanup_driver(driver)
                    except Exception:
                        pass
                    driver = None
                
                driver = create_driver(headless=settings.step6_headless)
                _login_with_mfa(
                    driver=driver,
                    admin_email=tenant_data["admin_email"],
                    admin_password=tenant_data["admin_password"],
                    totp_secret=tenant_data["totp_secret"],
                    domain=domain,
                )
                login_successful = True
                update_progress(tid_str, "login", "complete", "Logged in successfully")
                
            except Exception as browser_error:
                error_str = str(browser_error)
                is_connection_error = (
                    "HTTPConnectionPool" in error_str or
                    "NewConnectionError" in error_str or
                    "target machine actively refused" in error_str or
                    "session" in error_str.lower() and "delete" in error_str.lower()
                )
                
                if is_connection_error and browser_retry_count < MAX_BROWSER_RETRIES:
                    logger.warning(
                        "[%s] Browser connection error (attempt %s/%s): %s - retrying in 5 seconds...",
                        domain, browser_retry_count, MAX_BROWSER_RETRIES, error_str[:100]
                    )
                    if driver:
                        try:
                            cleanup_driver(driver)
                        except Exception:
                            pass
                        driver = None
                    await asyncio.sleep(5)
                else:
                    raise
        
        if not login_successful:
            raise Exception(f"Failed to login after {MAX_BROWSER_RETRIES} attempts")

        # Step 2 + 3: Selenium create licensed user + assign license
        update_progress(tid_str, "licensed_user", "in_progress", "Creating licensed user in M365 Admin UI")
        user_ops = UserOpsSelenium(driver, domain)

        licensed_user = None
        if tenant_data["licensed_user_created"] and tenant_data["licensed_user_upn"]:
            licensed_user = {
                "success": True,
                "email": tenant_data["licensed_user_upn"],
                "password": tenant_data["licensed_user_password"],
                "skipped": True,
            }
        else:
            licensed_user = user_ops.create_licensed_user(
                username="me1",
                display_name="me1",
                password=tenant_data["licensed_user_password"] or "#Sendemails1",
                custom_domain=domain,
            )
            if not licensed_user.get("success"):
                raise Exception(licensed_user.get("error") or "Failed to create licensed user")

            # Save licensed user immediately with fresh session (to both Tenant and Domain)
            async with BackgroundSessionLocal() as save_db:
                t = await save_db.get(Tenant, tenant_data["id"])
                if t:
                    t.licensed_user_created = True
                    t.licensed_user_upn = f"me1@{domain}"
                    t.licensed_user_password = licensed_user.get("password")
                # Also save to Domain record when domain_id is provided
                if domain_data:
                    d = await save_db.get(Domain, domain_data["id"])
                    if d:
                        d.licensed_user_created = True
                        d.licensed_user_upn = f"me1@{domain}"
                        d.licensed_user_password = licensed_user.get("password")
                await save_db.commit()
            # Update local data
            tenant_data["licensed_user_created"] = True
            tenant_data["licensed_user_upn"] = f"me1@{domain}"
            tenant_data["licensed_user_password"] = licensed_user.get("password")

        update_progress(tid_str, "licensed_user", "complete", f"Licensed user ready: {licensed_user.get('email')}")

        delegate_to = f"me1@{domain}"
        tenant_data["licensed_user_upn"] = delegate_to
        if licensed_user.get("password"):
            tenant_data["licensed_user_password"] = licensed_user.get("password")
        
        # Save delegate_to with fresh session (to both Tenant and Domain)
        async with BackgroundSessionLocal() as save_db:
            t = await save_db.get(Tenant, tenant_data["id"])
            if t:
                t.licensed_user_upn = delegate_to
                if licensed_user.get("password"):
                    t.licensed_user_password = licensed_user.get("password")
            if domain_data:
                d = await save_db.get(Domain, domain_data["id"])
                if d:
                    d.licensed_user_upn = delegate_to
                    if licensed_user.get("password"):
                        d.licensed_user_password = licensed_user.get("password")
            await save_db.commit()

        # ================================================================
        # PHASE C: Generate mailboxes in DB if needed (quick fresh session)
        # ================================================================
        update_progress(tid_str, "generate_emails", "in_progress", "Generating email variations")

        persona_display_name = None
        if batch_data:
            persona_display_name = f"{batch_data['persona_first_name'] or ''} {batch_data['persona_last_name'] or ''}".strip()
        if not persona_display_name:
            raise Exception("Missing persona display name for mailbox generation")

        if not mailbox_list:
            # Need to generate mailboxes
            # CHECK FOR CUSTOM MAILBOX MAP (CSV-imported emails for this domain)
            custom_emails_for_domain = None
            if batch_data and batch_data.get("custom_mailbox_map"):
                domain_key = domain.lower()
                custom_emails_for_domain = batch_data["custom_mailbox_map"].get(domain_key)

            if custom_emails_for_domain:
                logger.info("[%s] Using %d custom email addresses from CSV", domain, len(custom_emails_for_domain))
                from app.services.email_generator import MAILBOX_PASSWORD
                mailbox_data = []
                for entry in custom_emails_for_domain:
                    email = entry.get("email", "").strip().lower()
                    if not email or "@" not in email:
                        continue
                    local_part = email.split("@")[0]
                    display_name_val = entry.get("display_name", "").strip() or persona_display_name
                    password_val = entry.get("password", "").strip() or MAILBOX_PASSWORD
                    mailbox_data.append({
                        "email": email, "local_part": local_part,
                        "display_name": display_name_val, "password": password_val,
                    })
                if not mailbox_data:
                    logger.warning("[%s] Custom mailbox map had no valid entries - falling back to auto-generate", domain)
                    mailbox_data = generate_emails_for_domain(
                        display_name=persona_display_name, domain=domain, count=50,
                    )
            else:
                mailbox_data = generate_emails_for_domain(
                    display_name=persona_display_name, domain=domain, count=50,
                )

            # Save new mailboxes with fresh session
            async with BackgroundSessionLocal() as gen_db:
                for mb in mailbox_data:
                    mailbox = Mailbox(
                        email=mb["email"], local_part=mb["local_part"],
                        display_name=mb["display_name"], password=mb["password"],
                        tenant_id=tenant_data["id"], batch_id=tenant_data["batch_id"],
                        status=MailboxStatus.PENDING, warmup_stage="none",
                    )
                    gen_db.add(mailbox)
                await gen_db.commit()

                # Reload to get IDs
                result = await gen_db.execute(
                    select(Mailbox).where(Mailbox.tenant_id == tenant_data["id"])
                )
                existing = result.scalars().all()
                mailbox_list = [
                    {
                        "id": mb.id, "email": mb.email, "local_part": mb.local_part,
                        "display_name": mb.display_name, "password": mb.password,
                        "created_in_exchange": mb.created_in_exchange,
                        "password_set": mb.password_set, "delegated": mb.delegated,
                        "upn_fixed": getattr(mb, 'upn_fixed', False),
                    }
                    for mb in existing
                ]

        update_progress(tid_str, "generate_emails", "complete", f"Generated {len(mailbox_list)} mailboxes")

        # Build payload for PowerShell from plain dicts
        mailbox_data_payload = [
            {"email": mb["email"], "display_name": mb["display_name"], "password": mb["password"]}
            for mb in mailbox_list
        ]

        async def connect_powershell_with_retry(service: PowerShellExchangeService, max_attempts: int = 2) -> bool:
            for attempt in range(1, max_attempts + 1):
                try:
                    logger.info("[%s] PowerShell connect attempt %s/%s", domain, attempt, max_attempts)
                    connected = await service.connect()
                    if connected:
                        return True
                except Exception as exc:
                    logger.warning("[%s] PowerShell connect attempt %s failed: %s", domain, attempt, exc)
                try:
                    await service.disconnect()
                except Exception:
                    pass
                if attempt < max_attempts:
                    await asyncio.sleep(5)
            return False

        async def run_with_powershell_retry(operation_name: str, operation):
            last_error = None
            for attempt in range(1, 3):
                try:
                    logger.info("[%s] %s attempt %s/2", domain, operation_name, attempt)
                    return await operation()
                except Exception as exc:
                    last_error = exc
                    logger.warning("[%s] %s attempt %s failed: %s", domain, operation_name, attempt, exc)
                    if ps_service:
                        try:
                            await ps_service.disconnect()
                        except Exception:
                            pass
                    if attempt < 2 and ps_service:
                        connected = await connect_powershell_with_retry(ps_service)
                        if not connected:
                            break
            if last_error:
                raise last_error

        # ================================================================
        # PHASE D: PowerShell mailbox creation + delegation (LONG, 10-30 min, NO DB)
        # ================================================================
        if needs_powershell:
            if not is_browser_alive(driver):
                logger.warning("[%s] Browser died - creating fresh Chrome instance before PowerShell", domain)
                try:
                    cleanup_driver(driver)
                except Exception:
                    pass
                driver = create_driver(headless=settings.step6_headless)
                if driver is None:
                    raise Exception("Failed to create fresh Chrome browser for PowerShell auth")
                _login_with_mfa(
                    driver=driver,
                    admin_email=tenant_data["admin_email"],
                    admin_password=tenant_data["admin_password"],
                    totp_secret=tenant_data["totp_secret"],
                    domain=domain,
                )
                user_ops = UserOpsSelenium(driver, domain)
                logger.info("[%s] Fresh Chrome instance created successfully for PowerShell", domain)

            ps_service = PowerShellExchangeService(
                driver=driver,
                admin_email=tenant_data["admin_email"],
                admin_password=tenant_data["admin_password"],
                totp_secret=tenant_data["totp_secret"],
            )
            connected = await connect_powershell_with_retry(ps_service)
            if not connected:
                raise Exception("Failed to connect to Exchange Online")
            logger.info("[%s] PowerShell connected - stabilizing browser session before continuing...", domain)
            await asyncio.sleep(5)

        # ========================================
        # PHASE 1: PowerShell - Create, fix names, delegate
        # ========================================
        powershell_succeeded = False
        tenant_id_val = tenant_data["id"]

        if not needs_powershell:
            logger.info("[%s] Mailboxes already created & delegated, skipping PowerShell", domain)
            update_progress(tid_str, "create_mailboxes", "complete", "Mailboxes already created & delegated")
            powershell_succeeded = True
        else:
            try:
                logger.info("[%s] === PHASE 1: PowerShell STARTING ===", domain)
                logger.info("[%s] Phase 1: PowerShell operations", tid_str[:8])
                update_progress(tid_str, "create_mailboxes", "in_progress", "Creating shared mailboxes via PowerShell")

                mailboxes_to_process = mailbox_data_payload
                logger.info("[%s] Processing %s mailboxes (PowerShell will check existence first)",
                            domain, len(mailboxes_to_process))

                try:
                    logger.info("[%s] PowerShell create_shared_mailboxes with start_index=%s",
                                domain, mailbox_start_index)
                    ps_results = await run_with_powershell_retry(
                        "PowerShell mailbox creation",
                        lambda: ps_service.create_shared_mailboxes(
                            mailboxes=mailboxes_to_process,
                            delegate_to=delegate_to,
                            start_index=mailbox_start_index,
                        ),
                    )

                    # BUG 6.4 FIX: Count from mailbox_list dicts, not ORM objects
                    created_count = sum(
                        1 for mb in mailbox_list if (mb["created_in_exchange"] or mb["email"] in ps_results["created"])
                    )
                    delegated_count = sum(
                        1 for mb in mailbox_list if (mb["delegated"] or mb["email"] in ps_results["delegated"])
                    )
                    upn_fixed_count = sum(
                        1 for mb in mailbox_list
                        if (mb.get("upn_fixed", False) or mb["email"] in ps_results.get("upns_fixed", []))
                    )

                    logger.info("[%s] Saving PowerShell progress to database...", domain)
                    logger.info("[%s] PowerShell flags: mailboxes=%s delegations=%s upns=%s",
                                domain, created_count, delegated_count, upn_fixed_count)

                    async def _save_powershell_progress(fresh_db):
                        tenant_to_update = await fresh_db.get(Tenant, tenant_id_val)
                        if tenant_to_update:
                            tenant_to_update.step6_mailboxes_created = created_count
                            tenant_to_update.step6_display_names_fixed = created_count
                            tenant_to_update.step6_delegations_done = delegated_count
                            tenant_to_update.step6_upns_fixed = upn_fixed_count

                        for email in ps_results["created"]:
                            await fresh_db.execute(
                                update(Mailbox).where(Mailbox.email == email)
                                .values(created_in_exchange=True, display_name_fixed=True)
                            )
                        for email in ps_results["delegated"]:
                            await fresh_db.execute(
                                update(Mailbox).where(Mailbox.email == email)
                                .values(delegated=True)
                            )
                        for email in ps_results.get("upns_fixed", []):
                            await fresh_db.execute(
                                update(Mailbox).where(Mailbox.email == email)
                                .values(upn_fixed=True)
                            )

                    await save_to_db_with_retry(
                        _save_powershell_progress,
                        description=f"{domain} powershell progress",
                    )
                    logger.info("[%s] CHECKPOINT: PowerShell progress SAVED: mailboxes=%s, delegations=%s, upns=%s",
                                domain, created_count, delegated_count, upn_fixed_count)

                    # BUG 6.4 FIX: Check actual DB state instead of in-memory counters
                    async with BackgroundSessionLocal() as check_db:
                        ps_created_count = await check_db.scalar(
                            select(func.count(Mailbox.id)).where(
                                Mailbox.tenant_id == tenant_id_val,
                                Mailbox.created_in_exchange == True,
                            )
                        ) or 0
                        ps_delegated_count = await check_db.scalar(
                            select(func.count(Mailbox.id)).where(
                                Mailbox.tenant_id == tenant_id_val,
                                Mailbox.delegated == True,
                            )
                        ) or 0

                        ps_threshold = int(len(mailbox_list) * 0.9)
                        powershell_succeeded = (
                            ps_created_count >= ps_threshold and
                            ps_delegated_count >= ps_threshold
                        )
                        if not powershell_succeeded:
                            logger.warning("[%s] PowerShell below threshold: created=%s/%s, delegated=%s/%s (threshold=%s)",
                                           domain, ps_created_count, len(mailbox_list),
                                           ps_delegated_count, len(mailbox_list), ps_threshold)
                        logger.info("[%s] PowerShell phase check: created=%s, delegated=%s, threshold=%s, succeeded=%s",
                                    domain, ps_created_count, ps_delegated_count, ps_threshold, powershell_succeeded)

                    logger.info("[%s] === PHASE 1: PowerShell DONE (success=%s) ===", domain, powershell_succeeded)
                    update_progress(tid_str, "create_mailboxes", "complete",
                                    f"Created {len(ps_results['created'])} mailboxes")
                finally:
                    pass
            except Exception as e:
                logger.error("[%s] PHASE 1 FAILED: %s", domain, _format_error(e))
                import traceback
                logger.error(traceback.format_exc())
                raise

        # ========================================
        # PHASE 2: Set passwords via Graph PowerShell (replaces slow Admin UI)
        # Falls back to Admin UI if Graph fails
        # ========================================
        if not powershell_succeeded:
            logger.info("[%s] Phase 2: Skipped password setting - PowerShell did not succeed", domain)
            update_progress(tid_str, "set_passwords", "complete", "Skipped - PowerShell not successful")
        else:
            try:
                if not needs_admin_ui:
                    logger.info("[%s] Passwords already set, skipping Phase 2", domain)
                    update_progress(tid_str, "set_passwords", "complete", "Passwords already set")
                else:
                    graph_password_success = False

                    # --- PRIMARY: Graph PowerShell (fast, ~1 min) ---
                    if ps_service and ps_service.connected:
                        logger.info("[%s] === PHASE 2: Graph PowerShell passwords STARTING ===", domain)
                        update_progress(tid_str, "set_passwords", "in_progress", "Setting passwords via Graph API (fast)")

                        try:
                            graph_pw_results = await ps_service.set_mailbox_passwords(
                                mailboxes=mailbox_data_payload,
                                admin_email=tenant_data["admin_email"],
                                admin_password=tenant_data["admin_password"],
                            )

                            graph_updated = graph_pw_results.get("updated", [])
                            graph_failed = graph_pw_results.get("failed", [])
                            password_set_count = len(graph_updated)
                            accounts_enabled_count = password_set_count  # Graph enables accounts too

                            logger.info("[%s] Graph password results: set=%s, failed=%s",
                                        domain, password_set_count, len(graph_failed))

                            if graph_failed:
                                failed_preview = "; ".join(
                                    f"{item.get('email')}: {item.get('error')}" for item in graph_failed[:5]
                                )
                                logger.warning("[%s] Graph password failures (first 5): %s", domain, failed_preview)

                            # Consider success if we set at least 90% of passwords
                            threshold = int(len(mailbox_data_payload) * 0.9)
                            if password_set_count >= threshold:
                                graph_password_success = True
                                logger.info("[%s] Graph passwords succeeded: %s/%s (threshold=%s)",
                                            domain, password_set_count, len(mailbox_data_payload), threshold)
                            else:
                                logger.warning("[%s] Graph passwords below threshold: %s/%s (threshold=%s) - will fallback to Admin UI",
                                               domain, password_set_count, len(mailbox_data_payload), threshold)

                            # Save Graph results to DB
                            async def _save_graph_password_progress(fresh_db):
                                tenant_to_update = await fresh_db.get(Tenant, tenant_id_val)
                                if tenant_to_update:
                                    tenant_to_update.step6_passwords_set = password_set_count
                                    tenant_to_update.step6_accounts_enabled = accounts_enabled_count

                                if password_set_count > 0:
                                    # Mark individual mailboxes that were updated
                                    for email in graph_updated:
                                        await fresh_db.execute(
                                            update(Mailbox).where(Mailbox.email == email)
                                            .values(password_set=True, account_enabled=True, password="#Sendemails1")
                                        )

                            await save_to_db_with_retry(
                                _save_graph_password_progress, description=f"{domain} graph passwords",
                            )
                            logger.info("[%s] CHECKPOINT: Graph password progress saved: passwords_set=%s, accounts_enabled=%s",
                                        domain, password_set_count, accounts_enabled_count)
                            if graph_password_success:
                                logger.info("[%s] === PHASE 2: Graph PowerShell passwords DONE ===", domain)

                        except Exception as graph_err:
                            logger.warning("[%s] Graph password setting failed: %s - falling back to Admin UI",
                                           domain, _format_error(graph_err))
                    else:
                        logger.warning("[%s] PowerShell not connected for Graph passwords - falling back to Admin UI", domain)

                    # --- FALLBACK: Admin UI (slow, ~5 min) ---
                    if not graph_password_success:
                        logger.info("[%s] === PHASE 2 FALLBACK: Admin UI STARTING ===", domain)
                        update_progress(tid_str, "set_passwords", "in_progress", "Setting passwords via Admin UI (fallback)")

                        # PROACTIVE BROWSER HEALTH CHECK before Admin UI fallback
                        if not is_browser_alive(driver):
                            logger.warning("[%s] Browser died - creating fresh Chrome instance for Admin UI fallback", domain)
                            try:
                                cleanup_driver(driver)
                            except Exception:
                                pass
                            driver = create_driver(headless=settings.step6_headless)
                            if driver is None:
                                raise Exception("Failed to create fresh Chrome browser for Admin UI fallback")
                            _login_with_mfa(
                                driver=driver,
                                admin_email=tenant_data["admin_email"],
                                admin_password=tenant_data["admin_password"],
                                totp_secret=tenant_data["totp_secret"],
                                domain=domain,
                            )
                            user_ops = UserOpsSelenium(driver, domain)
                            logger.info("[%s] Fresh Chrome instance created for Admin UI fallback", domain)

                        admin_ui_results = None
                        admin_ui_retry_count = 0
                        MAX_ADMIN_UI_RETRIES = 2

                        while admin_ui_retry_count < MAX_ADMIN_UI_RETRIES and admin_ui_results is None:
                            try:
                                admin_ui_retry_count += 1
                                if admin_ui_retry_count > 1:
                                    update_progress(tid_str, "set_passwords", "in_progress",
                                        f"Retrying Admin UI (attempt {admin_ui_retry_count}/{MAX_ADMIN_UI_RETRIES})")
                                    if driver:
                                        try:
                                            cleanup_driver(driver)
                                        except Exception:
                                            pass
                                        driver = None
                                    driver = create_driver(headless=settings.step6_headless)
                                    _login_with_mfa(
                                        driver=driver,
                                        admin_email=tenant_data["admin_email"],
                                        admin_password=tenant_data["admin_password"],
                                        totp_secret=tenant_data["totp_secret"],
                                        domain=domain,
                                    )
                                    user_ops = UserOpsSelenium(driver, domain)

                                admin_ui_results = user_ops.set_passwords_and_enable_via_admin_ui(
                                    password="#Sendemails1",
                                    exclude_users=[f"me1@{domain}"],
                                    expected_count=len(mailbox_list),
                                )
                            except Exception as admin_ui_error:
                                error_str = str(admin_ui_error)
                                is_connection_error = (
                                    "HTTPConnectionPool" in error_str or
                                    "NewConnectionError" in error_str or
                                    "target machine actively refused" in error_str or
                                    "session" in error_str.lower() and "delete" in error_str.lower()
                                )
                                if is_connection_error and admin_ui_retry_count < MAX_ADMIN_UI_RETRIES:
                                    logger.warning("[%s] Admin UI browser error (attempt %s/%s): %s - retrying...",
                                                   domain, admin_ui_retry_count, MAX_ADMIN_UI_RETRIES, error_str[:100])
                                    await asyncio.sleep(5)
                                else:
                                    raise

                        if admin_ui_results is None:
                            raise Exception(f"Admin UI fallback failed after {MAX_ADMIN_UI_RETRIES} attempts")

                        if admin_ui_results.get("errors"):
                            logger.warning("[%s] Admin UI errors: %s", domain, "; ".join(admin_ui_results["errors"]))

                        password_set_count = admin_ui_results.get("passwords_set", 0)
                        accounts_enabled_count = admin_ui_results.get("accounts_enabled", 0)

                        logger.info("[%s] Saving Admin UI fallback progress to database...", domain)

                        async def _save_admin_ui_progress(fresh_db):
                            tenant_to_update = await fresh_db.get(Tenant, tenant_id_val)
                            if tenant_to_update:
                                tenant_to_update.step6_passwords_set = password_set_count
                                tenant_to_update.step6_accounts_enabled = accounts_enabled_count

                            total_mb_count = len(mailbox_list)
                            if password_set_count > 0:
                                logger.info("[%s] Marking all %s mailboxes as password_set=True (Admin UI reported %s successes)",
                                            domain, total_mb_count, password_set_count)
                                await fresh_db.execute(
                                    update(Mailbox).where(Mailbox.tenant_id == tenant_id_val)
                                    .values(password_set=True, account_enabled=True, password="#Sendemails1")
                                )
                            else:
                                logger.warning("[%s] Admin UI reported 0 passwords set", domain)

                        await save_to_db_with_retry(
                            _save_admin_ui_progress, description=f"{domain} admin UI progress",
                        )
                        logger.info("[%s] CHECKPOINT: Admin UI progress saved: passwords_set=%s, accounts_enabled=%s",
                                    domain, password_set_count, accounts_enabled_count)
                        logger.info("[%s] === PHASE 2 FALLBACK: Admin UI DONE ===", domain)
            except Exception as e:
                logger.error("[%s] PHASE 2 FAILED: %s", domain, _format_error(e))
                import traceback
                logger.error(traceback.format_exc())
                # Don't fail the whole step — mailboxes are created, passwords can be retried
                logger.warning("[%s] Continuing to completion check despite Phase 2 failure", domain)

        # ========================================
        # PHASE G: Final completion check (BUG 6.4 FIX: count from actual Mailbox table)
        # ========================================
        step6_actually_complete = False
        missing_items = []

        # Capture domain_data id for use inside closure
        _domain_data_id = domain_data["id"] if domain_data else None

        async def _save_completion_check(fresh_db):
            nonlocal step6_actually_complete, missing_items
            # BUG 6.4 FIX: Always count from source of truth (Mailbox table)
            # When domain_id is provided, filter by email domain suffix
            completion_filters = [Mailbox.tenant_id == tenant_id_val]
            if domain_id:
                completion_filters.append(Mailbox.email.like(f"%@{domain}"))

            total_mailboxes = await fresh_db.scalar(
                select(func.count(Mailbox.id)).where(*completion_filters)
            ) or 0
            created_count = await fresh_db.scalar(
                select(func.count(Mailbox.id)).where(
                    *completion_filters, Mailbox.created_in_exchange == True,
                )
            ) or 0
            delegated_count = await fresh_db.scalar(
                select(func.count(Mailbox.id)).where(
                    *completion_filters, Mailbox.delegated == True,
                )
            ) or 0
            passwords_set_count = await fresh_db.scalar(
                select(func.count(Mailbox.id)).where(
                    *completion_filters, Mailbox.password_set == True,
                )
            ) or 0

            tenant_to_update = await fresh_db.get(Tenant, tenant_id_val)
            if tenant_to_update:
                # Update counters from actual DB counts
                tenant_to_update.step6_mailboxes_created = created_count
                tenant_to_update.step6_delegations_done = delegated_count
                tenant_to_update.step6_passwords_set = passwords_set_count

                completion_threshold = 0.9
                if total_mailboxes > 0:
                    threshold = total_mailboxes * completion_threshold
                    has_mailboxes = created_count >= threshold
                    has_delegation = delegated_count >= threshold
                    has_passwords = passwords_set_count >= threshold

                    logger.info(
                        "[%s] Completion check: mailboxes=%s/%s, delegated=%s/%s, passwords=%s/%s (threshold=%.0f)",
                        domain, created_count, total_mailboxes,
                        delegated_count, total_mailboxes,
                        passwords_set_count, total_mailboxes, threshold,
                    )

                    if has_mailboxes and has_delegation and has_passwords:
                        # Mark tenant-level completion (backward compatible)
                        tenant_to_update.step6_complete = True
                        tenant_to_update.step6_completed_at = datetime.utcnow()
                        tenant_to_update.status = TenantStatus.READY
                        tenant_to_update.step6_error = None
                        step6_actually_complete = True
                        logger.info("[%s] All criteria met - marking Step 6 COMPLETE", domain)

                        # Also mark Domain-level completion when domain_id is provided
                        if _domain_data_id:
                            domain_to_update = await fresh_db.get(Domain, _domain_data_id)
                            if domain_to_update:
                                domain_to_update.step6_complete = True
                                domain_to_update.step6_mailboxes_created = created_count
                                logger.info("[%s] Domain record marked step6_complete=True, mailboxes_created=%s",
                                            domain, created_count)
                    else:
                        if not has_mailboxes:
                            missing_items.append(f"mailboxes ({created_count}/{total_mailboxes})")
                        if not has_delegation:
                            missing_items.append(f"delegation ({delegated_count}/{total_mailboxes})")
                        if not has_passwords:
                            missing_items.append(f"passwords ({passwords_set_count}/{total_mailboxes})")
                        error_msg = f"Incomplete - missing: {', '.join(missing_items)}"
                        tenant_to_update.step6_error = error_msg
                        logger.warning("[%s] NOT marking complete - missing: %s", domain, ", ".join(missing_items))

            if tenant_to_update and tenant_to_update.step6_complete:
                logger.info("[%s] Step 6 COMPLETE for %s", tid_str[:8], domain)

        await save_to_db_with_retry(
            _save_completion_check, description=f"{domain} completion check",
        )

        logger.info("[%s] FINAL: complete=%s", domain, step6_actually_complete)

        if step6_actually_complete:
            update_progress(tid_str, "complete", "complete", "Step 6 complete!")
            return {"success": True}
        else:
            error_detail = f"Step 6 incomplete - missing: {', '.join(missing_items)}" if missing_items else "Step 6 incomplete"
            update_progress(tid_str, "incomplete", "failed", error_detail)
            return {"success": False, "error": error_detail}
    except Exception as exc:
        message = _format_error(exc)
        logger.error("[%s] Step 6 failed: %s", domain, message)
        try:
            async def _save_error(err_db):
                tenant_to_update = await err_db.get(Tenant, tenant_data["id"])
                if tenant_to_update:
                    tenant_to_update.step6_error = message
            await save_to_db_with_retry(
                _save_error, description=f"{domain} error save",
            )
        except Exception as db_err:
            logger.error("[%s] CRITICAL: Could not save error to DB either: %s", domain, _format_error(db_err))
        update_progress(tid_str, "error", "failed", message)
        return {"success": False, "error": message}
    finally:
        if ps_service:
            try:
                await ps_service.disconnect()
            except Exception:
                pass
        if driver:
            cleanup_driver(driver)

