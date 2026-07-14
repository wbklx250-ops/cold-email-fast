"""
Background Jobs Service

Uses APScheduler to run periodic background tasks.

Current jobs:
- DKIM Enable Retry: Retries enabling DKIM for tenants where DKIM CNAMEs
  have been added but enable failed (Microsoft takes time to provision DKIM).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_engine
from app.models.tenant import Tenant, TenantStatus
from app.models.domain import Domain

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None

# Constants
DKIM_RETRY_INTERVAL_MINUTES = 10  # How often to check for pending DKIM
DKIM_RETRY_WINDOW_HOURS = 24  # Stop retrying after this many hours


async def retry_dkim_enable_job():
    """
    Background job that retries enabling DKIM for tenants where:
    - DKIM CNAMEs have been added to Cloudflare (dkim_cnames_added = True)
    - DKIM has not been enabled in Exchange yet (dkim_enabled = False)
    - Domain was verified less than 24 hours ago (don't retry forever)
    
    This handles the case where Microsoft takes time (minutes to hours) to
    provision DKIM for newly added domains.
    
    NOTE: This job skips if Step 5 automation is currently running to avoid
    connection pool exhaustion.
    """
    logger.info("=== DKIM Enable Retry Job Starting ===")
    logger.warning(
        "DKIM retry job disabled: Step 5 auth state must be repaired through "
        "the Admin Center setup wizard pipeline."
    )
    return {"status": "skipped", "message": "DKIM retry disabled; rerun Step 5 wizard"}
    
    try:
        # First, check if Step 5 automation is currently running
        # Import here to avoid circular imports
        from app.models.batch import SetupBatch
        
        async with AsyncSession(async_engine, expire_on_commit=False) as session:
            # Check for any batch with active automation (status contains 'running' or step 5 in progress)
            automation_check = await session.execute(
                select(SetupBatch).where(SetupBatch.status == 'automation_running')
            )
            active_batch = automation_check.scalar_one_or_none()
            
            if active_batch:
                logger.info(f"DKIM retry job SKIPPED - Step 5 automation is running for batch {active_batch.id}")
                logger.info("=== DKIM Enable Retry Job Complete (skipped) ===")
                return
        
        # Now proceed with the actual DKIM retry logic
        async with AsyncSession(async_engine, expire_on_commit=False) as session:
            # Calculate cutoff time (24 hours ago)
            cutoff_time = datetime.utcnow() - timedelta(hours=DKIM_RETRY_WINDOW_HOURS)
            
            # Find tenants needing DKIM enable
            # Conditions:
            # 1. dkim_cnames_added = True (CNAMEs are in Cloudflare)
            # 2. dkim_enabled != True (not yet enabled in Exchange)
            # 3. domain_verified_at is not null and > cutoff_time (within retry window)
            query = select(Tenant).where(
                and_(
                    Tenant.dkim_cnames_added == True,
                    Tenant.dkim_enabled != True,
                    Tenant.domain_verified_at.isnot(None),
                    Tenant.domain_verified_at > cutoff_time
                )
            )
            
            result = await session.execute(query)
            tenants = result.scalars().all()
            
            if not tenants:
                logger.info("No tenants pending DKIM enable")
                logger.info("=== DKIM Enable Retry Job Complete ===")
                return
            
            logger.info(f"Found {len(tenants)} tenants pending DKIM enable")
            
            # Import admin portal automation
            from app.services.selenium.admin_portal import AdminPortalAutomation
            
            for tenant in tenants:
                try:
                    # Get the domain
                    domain_query = select(Domain).where(Domain.id == tenant.domain_id)
                    domain_result = await session.execute(domain_query)
                    domain = domain_result.scalar_one_or_none()
                    
                    if not domain:
                        logger.warning(f"[{tenant.name}] No domain found, skipping")
                        continue
                    
                    # Check if credentials are available
                    if not tenant.admin_email or not tenant.admin_password or not tenant.totp_secret:
                        logger.warning(f"[{domain.name}] Missing credentials, skipping")
                        continue
                    
                    # Update retry tracking
                    tenant.dkim_retry_count += 1
                    tenant.dkim_last_retry_at = datetime.utcnow()
                    await session.commit()
                    
                    logger.info(f"[{domain.name}] Attempting DKIM enable (retry #{tenant.dkim_retry_count}, last retry: {tenant.dkim_last_retry_at})")
                    
                    # Try to enable DKIM via Exchange Admin Center UI
                    automation = AdminPortalAutomation(headless=True)  # Run headless for background job
                    result = await automation.enable_dkim_via_ui(
                        admin_email=tenant.admin_email,
                        admin_password=tenant.admin_password,
                        totp_secret=tenant.totp_secret,
                        domain_name=domain.name
                    )
                    
                    if result.success:
                        logger.info(f"[{domain.name}] DKIM enabled successfully on retry #{tenant.dkim_retry_count}!")
                        
                        # Update tenant
                        tenant.dkim_enabled = True
                        tenant.dkim_enabled_at = datetime.utcnow()
                        tenant.status = TenantStatus.DKIM_ENABLED
                        tenant.setup_error = None
                        
                        # Update domain
                        domain.dkim_enabled = True
                        domain.status = "active"
                        
                        await session.commit()
                        logger.info(f"[{domain.name}] Status updated to DKIM_ENABLED")
                        
                    else:
                        error = result.error or "Unknown error"
                        if "not found" in error.lower():
                            logger.info(f"[{domain.name}] Domain not yet in DKIM list (retry #{tenant.dkim_retry_count}), will retry in {DKIM_RETRY_INTERVAL_MINUTES} minutes")
                        else:
                            logger.warning(f"[{domain.name}] DKIM enable failed (retry #{tenant.dkim_retry_count}): {error}")
                        
                        # Don't update status - let it retry next cycle
                        # Just log the error but keep status as PENDING_DKIM
                    
                    # Wait between tenants to avoid rate limiting
                    await asyncio.sleep(5)
                    
                except Exception as e:
                    logger.error(f"[{tenant.name}] Error enabling DKIM: {e}")
                    # Continue with next tenant
                    continue
        
        logger.info("=== DKIM Enable Retry Job Complete ===")
        
    except Exception as e:
        logger.error(f"DKIM Enable Retry Job failed: {e}")
        import traceback
        logger.error(traceback.format_exc())


def start_background_scheduler():
    """Start the background job scheduler."""
    global scheduler
    
    if scheduler is not None:
        logger.warning("Background scheduler already running")
        return
    
    scheduler = AsyncIOScheduler()
    
    # Add DKIM retry job - runs every 10 minutes
    scheduler.add_job(
        retry_dkim_enable_job,
        trigger=IntervalTrigger(minutes=DKIM_RETRY_INTERVAL_MINUTES),
        id="dkim_enable_retry",
        name="Retry DKIM Enable",
        replace_existing=True,
        max_instances=1,  # Don't allow overlapping runs
        coalesce=True,  # Merge missed runs
    )
    
    scheduler.start()
    logger.info(f"Background scheduler started - DKIM retry job scheduled every {DKIM_RETRY_INTERVAL_MINUTES} minutes")


def stop_background_scheduler():
    """Stop the background job scheduler."""
    global scheduler
    
    if scheduler is None:
        logger.warning("Background scheduler not running")
        return
    
    scheduler.shutdown(wait=False)
    scheduler = None
    logger.info("Background scheduler stopped")


async def trigger_dkim_retry_now():
    """
    Manually trigger the DKIM retry job immediately.
    
    Returns the number of pending tenants found.
    """
    logger.info("Manual DKIM retry triggered")
    
    # Run the job directly
    await retry_dkim_enable_job()
    
    return {"status": "completed", "message": "DKIM retry job completed"}


async def get_pending_dkim_count() -> int:
    """Get count of tenants pending DKIM enable."""
    return 0
    try:
        async with AsyncSession(async_engine, expire_on_commit=False) as session:
            cutoff_time = datetime.utcnow() - timedelta(hours=DKIM_RETRY_WINDOW_HOURS)
            
            query = select(Tenant).where(
                and_(
                    Tenant.dkim_cnames_added == True,
                    Tenant.dkim_enabled != True,
                    Tenant.domain_verified_at.isnot(None),
                    Tenant.domain_verified_at > cutoff_time
                )
            )
            
            result = await session.execute(query)
            tenants = result.scalars().all()
            return len(tenants)
    except Exception as e:
        logger.error(f"Error getting pending DKIM count: {e}")
        return 0


async def get_dkim_retry_status():
    """Get status of pending DKIM retries with details."""
    return {
        "pending_count": 0,
        "pending": [],
        "disabled": True,
        "message": "DKIM retry is disabled; rerun Step 5 Admin Center wizard",
    }
    try:
        async with AsyncSession(async_engine, expire_on_commit=False) as session:
            cutoff_time = datetime.utcnow() - timedelta(hours=DKIM_RETRY_WINDOW_HOURS)
            
            query = select(Tenant).where(
                and_(
                    Tenant.dkim_cnames_added == True,
                    Tenant.dkim_enabled != True,
                    Tenant.domain_verified_at.isnot(None),
                    Tenant.domain_verified_at > cutoff_time
                )
            )
            
            result = await session.execute(query)
            tenants = result.scalars().all()
            
            pending = []
            for tenant in tenants:
                domain_query = select(Domain).where(Domain.id == tenant.domain_id)
                domain_result = await session.execute(domain_query)
                domain = domain_result.scalar_one_or_none()
                
                pending.append({
                    "tenant_id": str(tenant.id),
                    "tenant_name": tenant.name,
                    "domain": domain.name if domain else None,
                    "retry_count": tenant.dkim_retry_count,
                    "last_retry_at": tenant.dkim_last_retry_at.isoformat() if tenant.dkim_last_retry_at else None,
                    "domain_verified_at": tenant.domain_verified_at.isoformat() if tenant.domain_verified_at else None,
                    "status": tenant.status.value
                })
            
            return {
                "pending_count": len(pending),
                "retry_interval_minutes": DKIM_RETRY_INTERVAL_MINUTES,
                "retry_window_hours": DKIM_RETRY_WINDOW_HOURS,
                "tenants": pending
            }
    except Exception as e:
        logger.error(f"Error getting DKIM retry status: {e}")
        return {"error": str(e)}
