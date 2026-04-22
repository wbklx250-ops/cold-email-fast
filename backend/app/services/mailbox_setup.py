"""
Step 6 Orchestrator: Mailbox Creation

Coordinates all Step 6 operations for a tenant:
1. Create licensed user (me1)
2. Generate email addresses
3. Create shared mailboxes
4. Fix display names
5. Fix UPNs
6. Enable accounts
7. Set passwords
8. Delegate to licensed user

Uses hybrid approach:
- PowerShell device-code auth for mailbox creation + delegation
- Selenium UI for user operations (create licensed user, enable accounts, set passwords, fix UPNs)
- NO Graph API needed
"""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from app.models.tenant import Tenant
from app.models.mailbox import Mailbox
from app.models.batch import SetupBatch
from app.db.session import SyncSessionLocal
from app.services.email_generator import generate_emails_for_domain
from app.services.powershell_exchange import PowerShellExchangeService
from app.services.selenium.user_ops import UserOpsSelenium

logger = logging.getLogger(__name__)

# Configuration
MAX_PARALLEL_TENANTS = 2
LICENSED_USER_PASSWORD = "#Sendemails1"
MAILBOX_PASSWORD = "#Sendemails1"  # Standard password for all mailboxes
SCREENSHOT_DIR = "C:/temp/screenshots/step6"

# Progress tracking (in-memory for real-time UI updates)
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


def clear_progress(tenant_id: str):
    """Clear progress for a tenant."""
    _progress_store.pop(tenant_id, None)


class Step6Orchestrator:
    """Orchestrates the complete Step 6 mailbox creation process."""

    def __init__(self, tenant_data: Dict[str, Any], display_name: str, domain_data: Dict[str, Any] = None, domain_index: int = 0):
        """
        Initialize orchestrator for a single domain within a tenant.

        Args:
            tenant_data: Dict with tenant info (from database)
            display_name: Display name for mailboxes (e.g., "Jack Zuvelek")
            domain_data: Dict with domain info {"id": ..., "name": ...} (if None, falls back to tenant custom_domain)
            domain_index: Position of this domain within its tenant (0, 1, 2...) for mailbox numbering
        """
        self.tenant_data = tenant_data
        self.tenant_id = tenant_data["id"]
        self.display_name = display_name
        
        # Domain-specific info
        if domain_data:
            self.domain = domain_data["name"]  # The specific domain being processed
            self.domain_id = domain_data["id"]
        else:
            self.domain = tenant_data["custom_domain"]
            self.domain_id = tenant_data.get("domain_id")
        
        self.domain_index = domain_index
        # Mailbox count per domain (configurable, 25-100, defaults to 50 for backward compat)
        self.mailboxes_per_tenant = tenant_data.get("mailboxes_per_tenant") or 50
        # Mailbox numbers for THIS domain: e.g., count=50 → 1-50, 51-100, 101-150...
        self.mailbox_start_index = domain_index * self.mailboxes_per_tenant + 1
        
        self.onmicrosoft_domain = tenant_data["onmicrosoft_domain"]
        self.admin_email = tenant_data["admin_email"]
        self.admin_password = tenant_data["admin_password"]
        self.totp_secret = tenant_data.get("totp_secret")
        
        # Licensed user info (for re-run idempotency) — per domain
        if domain_data:
            self.licensed_user_created = domain_data.get("licensed_user_created", False)
            self.licensed_user_upn = domain_data.get("licensed_user_upn")
            self.licensed_user_password = domain_data.get("licensed_user_password")
        else:
            self.licensed_user_created = tenant_data.get("licensed_user_created", False)
            self.licensed_user_upn = tenant_data.get("licensed_user_upn")
            self.licensed_user_password = tenant_data.get("licensed_user_password")

        self.driver: Optional[webdriver.Chrome] = None
        self.user_ops: Optional[UserOpsSelenium] = None

        # Results tracking
        self.results = {
            "success": False,
            "licensed_user": None,
            "mailboxes_created": 0,
            "display_names_fixed": 0,
            "accounts_enabled": 0,
            "passwords_set": 0,
            "upns_fixed": 0,
            "delegations_done": 0,
            "errors": [],
            "mailboxes": [],
        }

    def _create_driver(self, headless: bool = False) -> webdriver.Chrome:
        """Create a Chrome driver."""
        opts = Options()
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-software-rasterizer")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-blink-features=AutomationControlled")

        if headless:
            opts.add_argument("--headless=new")

        return webdriver.Chrome(options=opts)

    def _screenshot(self, name: str):
        """Save screenshot for debugging."""
        if self.driver:
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            path = f"{SCREENSHOT_DIR}/{self.domain}_{name}_{int(time.time())}.png"
            try:
                self.driver.save_screenshot(path)
            except Exception:
                pass

    def _create_mailboxes_with_powershell(
        self,
        mailbox_data: List[Dict[str, Any]],
        delegate_to: str,
    ) -> Dict[str, Any]:
        """Create and delegate mailboxes using PowerShell device-code auth."""

        results: Dict[str, Any] = {
            "created": [],
            "failed": [],
            "delegated": [],
            "display_names_fixed": [],
            "display_names_failed": [],
        }

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ps_service = PowerShellExchangeService(
            driver=self.driver,
            admin_email=self.admin_email,
            admin_password=self.admin_password,
            totp_secret=self.totp_secret,
        )

        try:
            connected = loop.run_until_complete(ps_service.connect())
            if not connected:
                raise Exception("Failed to connect to Exchange Online via PowerShell")

            mailbox_payload = [
                {
                    "email": mb["email"],
                    "display_name": f"{mb['display_name']} {mb['index']}",
                    "delegate_to": delegate_to,
                }
                for mb in mailbox_data
            ]

            results = loop.run_until_complete(ps_service.create_shared_mailboxes(mailbox_payload))

            fix_payload = [
                {"email": mb["email"], "display_name": mb["display_name"]}
                for mb in mailbox_data
            ]
            fix_results = loop.run_until_complete(ps_service.fix_display_names(fix_payload))
            results["display_names_fixed"] = fix_results.get("updated", [])
            results["display_names_failed"] = fix_results.get("failed", [])

        except Exception as exc:
            logger.error("[%s] PowerShell mailbox creation failed: %s", self.domain, exc)
            results["failed"].append({"error": str(exc)})
        finally:
            try:
                loop.run_until_complete(ps_service.disconnect())
            except Exception as exc:
                logger.warning("[%s] PowerShell disconnect failed: %s", self.domain, exc)
            loop.close()

        return results

    def run(self) -> Dict[str, Any]:
        """
        Run the complete Step 6 process for this tenant.

        This is a SYNCHRONOUS method designed to run in a thread pool.
        Returns results dict.
        """
        try:
            update_progress(self.tenant_id, "starting", "in_progress", "Initializing...")

            # Step 1: Login to M365 Admin Portal
            update_progress(
                self.tenant_id,
                "login",
                "in_progress",
                "Logging into M365 Admin Portal",
            )
            self.driver = self._create_driver(headless=False)

            success = self._login()
            if not success:
                raise Exception("Failed to login to M365 Admin Portal")

            update_progress(self.tenant_id, "login", "complete", "Logged in successfully")

            # Step 2: Initialize Selenium UI operations (no Graph API needed)
            update_progress(self.tenant_id, "token", "in_progress", "Preparing admin session")
            self.user_ops = UserOpsSelenium(self.driver, self.domain)
            logger.info("[%s]  UserOpsSelenium initialized (using UI instead of Graph API)", self.domain)
            
            update_progress(
                self.tenant_id,
                "token",
                "complete",
                "Admin session ready",
            )

            # Step 3: Create licensed user (me1)
            update_progress(
                self.tenant_id,
                "licensed_user",
                "in_progress",
                "Creating licensed user (me1)",
            )
            
            # Check database if licensed user already exists
            logger.info(f"[{self.domain}] Checking if licensed user already exists...")
            logger.info(f"[{self.domain}] licensed_user_created = {self.licensed_user_created}")
            logger.info(f"[{self.domain}] licensed_user_upn = {self.licensed_user_upn}")
            
            if self.licensed_user_created and self.licensed_user_upn:
                logger.info(f"[{self.domain}]  Licensed user already exists: {self.licensed_user_upn} - SKIPPING creation")
                licensed_user = {
                    "success": True,
                    "email": self.licensed_user_upn,
                    "password": self.licensed_user_password or LICENSED_USER_PASSWORD,
                    "skipped": True,
                }
                update_progress(
                    self.tenant_id,
                    "licensed_user",
                    "complete",
                    f"Already exists: {self.licensed_user_upn}",
                )
            else:
                logger.info(f"[{self.domain}] Creating licensed user...")
                # Create new licensed user
                licensed_user = self._create_licensed_user()
                if not licensed_user.get("success"):
                    raise Exception(
                        f"Failed to create licensed user: {licensed_user.get('error')}"
                    )
                
                # IMPORTANT: Save to database immediately after successful creation
                try:
                    licensed_upn = f"me1@{self.domain}"
                    with SyncSessionLocal() as db:
                        db.execute(
                            update(Tenant)
                            .where(Tenant.id == self.tenant_id)
                            .values(
                                licensed_user_created=True,
                                licensed_user_upn=licensed_upn,
                                licensed_user_password=LICENSED_USER_PASSWORD
                            )
                        )
                        db.commit()
                    
                    # Update local state as well
                    self.licensed_user_created = True
                    self.licensed_user_upn = licensed_upn
                    self.licensed_user_password = LICENSED_USER_PASSWORD
                    
                    logger.info(f"[{self.domain}]  Licensed user created and saved to DB: {licensed_upn}")
                except Exception as db_err:
                    logger.error(f"[{self.domain}] Failed to save licensed user to DB: {db_err}")
                    # Don't raise - the user was created, just DB save failed
                
                update_progress(
                    self.tenant_id,
                    "licensed_user",
                    "complete",
                    f"Created {licensed_user['email']}",
                )
            
            self.results["licensed_user"] = licensed_user

            # Step 4: Generate email addresses
            update_progress(
                self.tenant_id,
                "generate_emails",
                "in_progress",
                f"Generating {self.mailboxes_per_tenant} email addresses",
            )
            mailbox_data = generate_emails_for_domain(self.display_name, self.domain, count=self.mailboxes_per_tenant)

            # Add index for numbered display names — offset by domain position within tenant
            # e.g., domain_index=0 → 1-50, domain_index=1 → 51-100, domain_index=2 → 101-150
            for i, mb in enumerate(mailbox_data, self.mailbox_start_index):
                mb["index"] = i

            self.results["mailboxes"] = mailbox_data
            update_progress(
                self.tenant_id,
                "generate_emails",
                "complete",
                f"Generated {len(mailbox_data)} emails",
            )

            # Step 5: Create shared mailboxes
            update_progress(
                self.tenant_id,
                "create_mailboxes",
                "in_progress",
                f"Creating shared mailboxes (0/{self.mailboxes_per_tenant})",
            )

            ps_results = self._create_mailboxes_with_powershell(
                mailbox_data,
                licensed_user["email"],
            )

            created = len(ps_results.get("created", []))
            delegated = len(ps_results.get("delegated", []))
            fixed = len(ps_results.get("display_names_fixed", []))

            self.results["mailboxes_created"] = created
            self.results["display_names_fixed"] = fixed
            self.results["delegations_done"] = delegated

            update_progress(
                self.tenant_id,
                "create_mailboxes",
                "complete",
                f"Created {created} mailboxes",
            )

            # Step 6: Fix display names (remove numbers)
            update_progress(
                self.tenant_id,
                "fix_display_names",
                "in_progress",
                f"Fixing display names (0/{self.mailboxes_per_tenant})",
            )
            update_progress(
                self.tenant_id,
                "fix_display_names",
                "complete",
                f"Fixed {fixed} display names",
            )

            # Step 7: Fix UPNs
            update_progress(
                self.tenant_id,
                "fix_upns",
                "in_progress",
                f"Fixing UPNs (0/{self.mailboxes_per_tenant})",
            )
            upns_fixed = self._fix_upns(mailbox_data)
            self.results["upns_fixed"] = upns_fixed
            update_progress(
                self.tenant_id,
                "fix_upns",
                "complete",
                f"Fixed {upns_fixed} UPNs",
            )

            # Step 8: Enable accounts
            update_progress(
                self.tenant_id,
                "enable_accounts",
                "in_progress",
                f"Enabling accounts (0/{self.mailboxes_per_tenant})",
            )
            enabled = self._enable_accounts(mailbox_data)
            self.results["accounts_enabled"] = enabled
            update_progress(
                self.tenant_id,
                "enable_accounts",
                "complete",
                f"Enabled {enabled} accounts",
            )

            # Step 9: Set passwords
            update_progress(
                self.tenant_id,
                "set_passwords",
                "in_progress",
                f"Setting passwords (0/{self.mailboxes_per_tenant})",
            )
            pwd_set = self._set_passwords(mailbox_data)
            self.results["passwords_set"] = pwd_set
            update_progress(
                self.tenant_id,
                "set_passwords",
                "complete",
                f"Set {pwd_set} passwords",
            )

            # Step 10: Delegate to licensed user
            update_progress(
                self.tenant_id,
                "delegation",
                "in_progress",
                f"Adding delegation (0/{self.mailboxes_per_tenant})",
            )
            update_progress(
                self.tenant_id,
                "delegation",
                "complete",
                f"Delegated {delegated} mailboxes",
            )

            if ps_results.get("failed"):
                for failure in ps_results["failed"]:
                    self.results["errors"].append(
                        failure.get("error")
                        or f"{failure.get('email', 'unknown')} failed"
                    )
            if ps_results.get("display_names_failed"):
                for failure in ps_results["display_names_failed"]:
                    self.results["errors"].append(
                        failure.get("error")
                        or f"Display name failed for {failure.get('email', 'unknown')}"
                    )

            # Success!
            self.results["success"] = not self.results["errors"]
            update_progress(self.tenant_id, "complete", "complete", "Step 6 complete!")

            return self.results

        except Exception as e:
            logger.error("[%s] Step 6 failed: %s", self.domain, e)
            self.results["errors"].append(str(e))
            update_progress(self.tenant_id, "error", "failed", str(e)[:100])
            return self.results

        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass

    def _login(self) -> bool:
        """Login using the proven Step 5 login code."""
        try:
            from app.services.selenium.admin_portal import _login_with_mfa

            _login_with_mfa(
                driver=self.driver,
                admin_email=self.admin_email,
                admin_password=self.admin_password,
                totp_secret=self.totp_secret,
                domain=self.domain,
            )

            logger.info("[%s] Login successful", self.domain)
            return True
        except Exception as e:
            logger.error("[%s] Login failed: %s", self.domain, e)
            self._screenshot("login_failed")
            return False

    def _create_licensed_user(self) -> Dict[str, Any]:
        """Create the licensed user (me1) via Selenium UI."""
        if not self.user_ops:
            return {"success": False, "error": "UserOpsSelenium not initialized"}

        # Use Selenium UI to create licensed user (no Graph API needed)
        result = self.user_ops.create_licensed_user(
            username="me1",
            display_name=self.display_name,
            password=LICENSED_USER_PASSWORD,
            custom_domain=self.domain,
        )
        return result


    def _enable_accounts(self, mailbox_data: List[Dict[str, Any]]) -> int:
        """Enable user accounts via Selenium UI."""
        if not self.user_ops:
            logger.error("UserOpsSelenium not initialized - skipping account enable")
            return 0

        # Build list of UPNs (using onmicrosoft domain since UPNs not fixed yet)
        upns = [f"{mb['local_part']}@{self.onmicrosoft_domain}" for mb in mailbox_data]

        # Use Selenium UI to enable accounts (no Graph API needed)
        result = self.user_ops.enable_users_bulk(upns)
        return len(result.get("enabled", []))

    def _set_passwords(self, mailbox_data: List[Dict[str, Any]]) -> int:
        """Set passwords via Selenium UI."""
        if not self.user_ops:
            logger.error("UserOpsSelenium not initialized - skipping password set")
            return 0

        # Build list of users with UPNs and passwords
        # Note: After UPN fix, emails should be using custom domain
        # All mailboxes use the standard mailbox password
        users = [
            {
                "upn": mb["email"],  # After UPN fix, use custom domain email
                "password": MAILBOX_PASSWORD,  # Use standard password
            }
            for mb in mailbox_data
        ]

        # Use Selenium UI to set passwords (no Graph API needed)
        result = self.user_ops.set_passwords_bulk(users)
        return len(result.get("set", []))

    def _fix_upns(self, mailbox_data: List[Dict[str, Any]]) -> int:
        """Fix UPNs via Selenium UI."""
        if not self.user_ops:
            logger.error("UserOpsSelenium not initialized - skipping UPN fix")
            return 0

        # Build list of users with current and new UPNs
        users = [
            {
                "current_upn": f"{mb['local_part']}@{self.onmicrosoft_domain}",
                "new_upn": mb["email"],  # Custom domain email
            }
            for mb in mailbox_data
        ]

        # Use Selenium UI to fix UPNs (no Graph API needed)
        result = self.user_ops.update_upns_bulk(users)
        return len(result.get("updated", []))



# =============================================================================
# BATCH PROCESSING
# =============================================================================


def run_step6_for_tenant_sync(tenant_data: Dict[str, Any], display_name: str) -> Dict[str, Any]:
    """
    Synchronous wrapper to run Step 6 for a single tenant.
    Designed to run in a thread pool.
    """
    orchestrator = Step6Orchestrator(tenant_data, display_name)
    return orchestrator.run()


async def run_step6_for_batch(
    batch_id: UUID,
    display_name: str,
    db: AsyncSession,
    max_parallel: int = MAX_PARALLEL_TENANTS,
) -> Dict[str, Any]:
    """
    Run Step 6 for all tenants in a batch.

    Args:
        batch_id: The batch UUID
        display_name: Display name for mailboxes (e.g., "Jack Zuvelek")
        db: Database session
        max_parallel: Max concurrent tenants to process

    Returns:
        Summary of results
    """
    logger.info("Starting Step 6 for batch %s with display name: %s", batch_id, display_name)

    # Update batch with display name
    first_name, last_name = (
        display_name.strip().split(" ", 1) if " " in display_name else (display_name, "")
    )
    await db.execute(
        update(SetupBatch)
        .where(SetupBatch.id == batch_id)
        .values(
            persona_first_name=first_name,
            persona_last_name=last_name,
            step6_emails_generated=False,
        )
    )
    await db.commit()

    # Get all tenants that have completed Step 5
    result = await db.execute(
        select(Tenant).where(
            Tenant.batch_id == batch_id,
            Tenant.domain_verified_in_m365 == True,
            Tenant.step6_complete == False,
        )
    )
    tenants = result.scalars().all()

    if not tenants:
        logger.warning("No eligible tenants found for batch %s", batch_id)
        return {"success": False, "error": "No eligible tenants", "processed": 0}

    logger.info("Found %s tenants to process", len(tenants))

    # Prepare tenant data for thread pool
    tenant_data_list = []
    for tenant in tenants:
        tenant_data_list.append(
            {
                "id": str(tenant.id),
                "name": tenant.name,
                "custom_domain": tenant.custom_domain,
                "onmicrosoft_domain": tenant.onmicrosoft_domain,
                "admin_email": tenant.admin_email,
                "admin_password": tenant.admin_password,
                "totp_secret": tenant.totp_secret,
                # Licensed user info (for re-run idempotency)
                "licensed_user_created": tenant.licensed_user_created,
                "licensed_user_upn": tenant.licensed_user_upn,
                "licensed_user_password": tenant.licensed_user_password,
            }
        )

    # Process tenants in parallel using thread pool
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = []
        for tenant_data in tenant_data_list:
            future = executor.submit(run_step6_for_tenant_sync, tenant_data, display_name)
            futures.append((tenant_data["id"], future))
            # Stagger starts
            await asyncio.sleep(10)

        # Collect results
        for tenant_id, future in futures:
            try:
                result = future.result(timeout=3600)
                results.append({"tenant_id": tenant_id, "result": result})

                # Update database with result
                await _update_tenant_step6_status(db, tenant_id, result)

            except Exception as e:
                logger.error("Tenant %s failed: %s", tenant_id, e)
                results.append({"tenant_id": tenant_id, "error": str(e)})

    # Update batch status
    success_count = sum(1 for r in results if r.get("result", {}).get("success"))

    await db.execute(
        update(SetupBatch)
        .where(SetupBatch.id == batch_id)
        .values(
            step6_emails_generated=True,
            step6_emails_generated_at=datetime.utcnow(),
        )
    )
    await db.commit()

    return {
        "success": success_count == len(tenants),
        "total": len(tenants),
        "succeeded": success_count,
        "failed": len(tenants) - success_count,
        "results": results,
    }


async def _update_tenant_step6_status(db: AsyncSession, tenant_id: str, result: Dict[str, Any]):
    """Update tenant record with Step 6 results."""
    try:
        values = {
            "step6_started": True,
            "step6_mailboxes_created": result.get("mailboxes_created", 0),
            "step6_display_names_fixed": result.get("display_names_fixed", 0),
            "step6_accounts_enabled": result.get("accounts_enabled", 0),
            "step6_passwords_set": result.get("passwords_set", 0),
            "step6_upns_fixed": result.get("upns_fixed", 0),
            "step6_delegations_done": result.get("delegations_done", 0),
        }

        if result.get("success"):
            values["step6_complete"] = True
            values["step6_completed_at"] = datetime.utcnow()
            values["step6_error"] = None
        else:
            values["step6_error"] = "; ".join(result.get("errors", ["Unknown error"]))

        # Update licensed user info
        if result.get("licensed_user", {}).get("success"):
            values["licensed_user_created"] = True
            values["licensed_user_upn"] = result["licensed_user"]["email"]
            values["licensed_user_password"] = result["licensed_user"].get(
                "password", LICENSED_USER_PASSWORD
            )

        await db.execute(update(Tenant).where(Tenant.id == tenant_id).values(**values))

        # Save mailboxes to database
        if result.get("mailboxes"):
            for mb in result["mailboxes"]:
                mailbox = Mailbox(
                    tenant_id=tenant_id,
                    email=mb["email"],
                    local_part=mb["local_part"],
                    display_name=mb["display_name"],
                    password=mb["password"],
                    initial_password=MAILBOX_PASSWORD,
                    created_in_exchange=result.get("success", False),
                    setup_complete=result.get("success", False),
                    setup_completed_at=datetime.utcnow() if result.get("success") else None,
                )
                db.add(mailbox)

        await db.commit()

    except Exception as e:
        logger.error("Failed to update tenant %s status: %s", tenant_id, e)
        await db.rollback()