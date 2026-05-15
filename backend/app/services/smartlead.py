"""
Smartlead Integration Service
==============================
Backend service for the Cold Email Infrastructure Platform.

Provides:
  - SmartleadAPI: REST API client for sending/warmup settings
  - SmartleadOAuthUploader: Selenium-based OAuth upload for M365 accounts
  - High-level orchestration functions for batch uploads

This file contains everything: API client, Selenium uploader, and helper functions.
"""

import asyncio
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, Dict, Any, List

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("smartlead")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SMARTLEAD_API_BASE = "https://server.smartlead.ai/api/v1"


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------
class SmartleadUploadRequest(BaseModel):
    """Request to upload accounts to Smartlead via OAuth."""
    api_key: str
    oauth_url: str = Field(..., description="Smartlead's custom Microsoft OAuth login URL")
    accounts: list[dict] = Field(..., description="List of {email, password} dicts")
    headless: bool = True
    configure_settings: bool = True
    sending_settings: Optional[dict] = None
    warmup_settings: Optional[dict] = None


class SmartleadSettingsRequest(BaseModel):
    """Request to update sending/warmup settings for accounts."""
    api_key: str
    emails: list[str] = Field(..., description="Email addresses to configure")
    max_email_per_day: int = 6
    time_to_wait_in_mins: int = 60
    custom_tracking_url: str = ""


class SmartleadWarmupRequest(BaseModel):
    """Request to update warmup settings."""
    api_key: str
    emails: list[str]
    warmup_enabled: bool = True
    total_warmup_per_day: int = 40
    daily_rampup: int = 5
    reply_rate_percentage: int = 79


class AccountResult(BaseModel):
    email: str
    success: bool
    action: str  # "uploaded", "skipped_existing", "settings_updated", "failed"
    error: Optional[str] = None
    smartlead_id: Optional[int] = None


class UploadResponse(BaseModel):
    total: int
    uploaded: int
    skipped_existing: int
    settings_configured: int
    warmup_configured: int
    failed: int
    results: list[AccountResult]


# ---------------------------------------------------------------------------
# Smartlead REST API Client (async with httpx)
# ---------------------------------------------------------------------------
class SmartleadAPI:
    """Async REST client for Smartlead API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=30)
        self._email_cache: dict[str, int] = {}  # email -> account_id

    async def close(self):
        await self._client.aclose()

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{SMARTLEAD_API_BASE}{path}{sep}api_key={self.api_key}"

    # ---- List / Search ----

    async def get_all_accounts(self) -> list[dict]:
        """Fetch all email accounts, paginated. Builds internal cache."""
        all_accounts = []
        offset = 0
        while True:
            resp = await self._client.get(
                self._url(f"/email-accounts/?offset={offset}&limit=100")
            )
            if resp.status_code != 200:
                logger.warning(f"Smartlead API returned {resp.status_code}")
                break
            data = resp.json()
            if not data:
                break
            for acc in data:
                email = acc.get("from_email", "").lower()
                aid = acc.get("id")
                if email and aid:
                    self._email_cache[email] = aid
            all_accounts.extend(data)
            if len(data) < 100:
                break
            offset += 100
            await asyncio.sleep(0.2)  # Rate limit
        return all_accounts

    async def get_existing_emails(self) -> set[str]:
        """Get set of all existing email addresses."""
        accounts = await self.get_all_accounts()
        return {acc.get("from_email", "").lower() for acc in accounts if acc.get("from_email")}

    async def find_account_id(self, email: str) -> Optional[int]:
        """Find account ID by email. Uses cache, falls back to API search."""
        key = email.lower()
        if key in self._email_cache:
            return self._email_cache[key]

        # Refresh cache
        await self.get_all_accounts()
        return self._email_cache.get(key)

    # ---- Sending Settings ----

    async def update_sending_settings(
        self,
        account_id: int,
        max_per_day: int = 6,
        wait_mins: int = 60,
        tracking_url: str = "",
    ) -> bool:
        """POST /email-accounts/{id} — update sending settings."""
        try:
            resp = await self._client.post(
                self._url(f"/email-accounts/{account_id}"),
                json={
                    "max_email_per_day": max_per_day,
                    "time_to_wait_in_mins": wait_mins,
                    "custom_tracking_url": tracking_url,
                },
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Sending settings update failed for {account_id}: {e}")
            return False

    # ---- Warmup Settings ----

    async def update_warmup_settings(
        self,
        account_id: int,
        enabled: bool = True,
        per_day: int = 40,
        rampup: int = 5,
        reply_rate: int = 79,
    ) -> bool:
        """POST /email-accounts/{id}/warmup — update warmup settings."""
        try:
            resp = await self._client.post(
                self._url(f"/email-accounts/{account_id}/warmup"),
                json={
                    "warmup_enabled": enabled,
                    "total_warmup_per_day": per_day,
                    "daily_rampup": rampup,
                    "reply_rate_percentage": reply_rate,
                },
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Warmup settings update failed for {account_id}: {e}")
            return False


# ---------------------------------------------------------------------------
# Selenium OAuth Uploader (sync — runs in thread pool from async context)
# ---------------------------------------------------------------------------
class SmartleadOAuthUploader:
    """
    Uploads M365 accounts to Smartlead via their custom OAuth URL.
    Uses Selenium — must be run in a thread pool executor from async code.
    """

    def __init__(self, headless: bool = True, worker_id: int = 0):
        self.headless = headless
        self.worker_id = worker_id

    def upload_account(self, email: str, password: str, oauth_url: str) -> bool:
        """Upload a single M365 account via OAuth. Returns True on success."""
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException

        driver = None
        try:
            chrome_options = webdriver.ChromeOptions()
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option("useAutomationExtension", False)

            if self.headless:
                chrome_options.add_argument("--headless=new")

            driver = webdriver.Chrome(options=chrome_options)
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            driver.set_page_load_timeout(25)
            wait = WebDriverWait(driver, 12)

            # Navigate to OAuth URL
            logger.info(f"[Worker {self.worker_id}] Starting OAuth for {email}")
            driver.get(oauth_url)
            time.sleep(3 + random.uniform(0, 1))

            # Enter email
            email_field = self._find_element(
                wait, [(By.NAME, "loginfmt"), (By.ID, "i0116")]
            )
            if not email_field:
                self._screenshot(driver, f"no_email_{email.split('@')[0]}")
                raise Exception("Email field not found")

            self._human_type(email_field, email)
            time.sleep(0.5)

            next_btn = self._find_element(
                wait, [(By.CSS_SELECTOR, 'input[type="submit"]'), (By.ID, "idSIButton9")]
            )
            if next_btn:
                self._safe_click(driver, next_btn)
            time.sleep(3 + random.uniform(0, 1))

            # Enter password
            pass_field = self._find_element(
                wait, [(By.NAME, "passwd"), (By.ID, "i0118")]
            )
            if not pass_field:
                self._screenshot(driver, f"no_pass_{email.split('@')[0]}")
                raise Exception("Password field not found")

            self._human_type(pass_field, password)
            time.sleep(0.5)

            signin_btn = self._find_element(
                wait, [(By.CSS_SELECTOR, 'input[type="submit"]'), (By.ID, "idSIButton9")]
            )
            if signin_btn:
                self._safe_click(driver, signin_btn)
            time.sleep(4 + random.uniform(0, 1))

            # Handle post-login prompts
            self._handle_post_login(driver)

            # Accept permissions consent
            self._handle_consent(driver)

            # Verify
            time.sleep(3)
            current_url = driver.current_url.lower()
            if "smartlead" in current_url:
                logger.info(f"[Worker {self.worker_id}] OAuth success for {email}")
                return True
            elif "login.microsoftonline.com" in current_url:
                logger.warning(f"[Worker {self.worker_id}] OAuth incomplete for {email}, URL: {driver.current_url}")
                self._screenshot(driver, f"incomplete_{email.split('@')[0]}")
                return False
            else:
                logger.warning(f"[Worker {self.worker_id}] Unclear result for {email}, URL: {driver.current_url}")
                self._screenshot(driver, f"unclear_{email.split('@')[0]}")
                return True  # Completed all steps without error

        except Exception as e:
            logger.error(f"[Worker {self.worker_id}] OAuth failed for {email}: {e}")
            if driver:
                self._screenshot(driver, f"error_{email.split('@')[0]}")
            return False

        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    def _find_element(self, wait, locators):
        """Try multiple locator strategies."""
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.support import expected_conditions as EC
        for locator in locators:
            try:
                el = wait.until(EC.visibility_of_element_located(locator))
                return el
            except TimeoutException:
                continue
        return None

    def _safe_click(self, driver, element):
        """Try JS click first, then regular."""
        try:
            driver.execute_script("arguments[0].click();", element)
        except Exception:
            try:
                element.click()
            except Exception:
                pass

    def _human_type(self, element, text):
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.03, 0.10))

    def _handle_post_login(self, driver):
        """Handle Stay signed in / Don't show again / Ask later."""
        from selenium.webdriver.common.by import By

        # Stay signed in
        try:
            btns = driver.find_elements(By.ID, "idSIButton9")
            if btns and btns[0].is_displayed():
                self._safe_click(driver, btns[0])
                time.sleep(2)
        except Exception:
            pass

        # Don't show again + No
        try:
            cb = driver.find_elements(By.ID, "KmsiCheckboxField")
            if cb and cb[0].is_displayed():
                self._safe_click(driver, cb[0])
                time.sleep(0.5)
            no = driver.find_elements(By.ID, "idBtn_Back")
            if no and no[0].is_displayed():
                self._safe_click(driver, no[0])
                time.sleep(2)
        except Exception:
            pass

        # Ask later
        try:
            al = driver.find_elements(By.ID, "btnAskLater")
            if al and al[0].is_displayed():
                self._safe_click(driver, al[0])
                time.sleep(2)
        except Exception:
            pass

    def _handle_consent(self, driver):
        """Accept OAuth permissions consent."""
        from selenium.webdriver.common.by import By

        # <input type="submit"> style
        try:
            for btn in driver.find_elements(By.CSS_SELECTOR, 'input[type="submit"]'):
                if btn.is_displayed():
                    self._safe_click(driver, btn)
                    time.sleep(3)
                    break
        except Exception:
            pass

        # <button> style
        try:
            for btn in driver.find_elements(By.TAG_NAME, "button"):
                if btn.is_displayed():
                    txt = btn.text.lower()
                    if any(kw in txt for kw in ["accept", "continue", "allow", "yes"]):
                        self._safe_click(driver, btn)
                        time.sleep(3)
                        break
        except Exception:
            pass

    def _screenshot(self, driver, label):
        try:
            os.makedirs("screenshots", exist_ok=True)
            driver.save_screenshot(
                f"screenshots/sl_{self.worker_id}_{label}_{datetime.now().strftime('%H%M%S')}.png"
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# High-Level Orchestration (async, for use by API routes)
# ---------------------------------------------------------------------------
def process_smartlead_mailbox_sync(
    uploader: SmartleadOAuthUploader,
    mailbox_data: Dict[str, Any],
    oauth_url: str,
    max_retries: int = 2
) -> Dict[str, Any]:
    """
    Synchronous function to process a single mailbox upload to Smartlead.
    Runs in ThreadPoolExecutor.
    """
    mailbox_id = mailbox_data["id"]
    email = mailbox_data["email"]
    password = mailbox_data["password"]

    for attempt in range(max_retries + 1):
        try:
            success = uploader.upload_account(email, password, oauth_url)
            if success:
                return {
                    "mailbox_id": mailbox_id,
                    "success": True,
                    "error": None,
                    "retries": attempt
                }
            else:
                if attempt < max_retries:
                    logger.warning(f"[Worker {uploader.worker_id}] Attempt {attempt + 1} failed for {email}, retrying...")
                    time.sleep(3)
                else:
                    return {
                        "mailbox_id": mailbox_id,
                        "success": False,
                        "error": "OAuth upload failed after retries",
                        "retries": attempt
                    }
        except Exception as e:
            if attempt < max_retries:
                time.sleep(3)
            else:
                return {
                    "mailbox_id": mailbox_id,
                    "success": False,
                    "error": str(e),
                    "retries": attempt
                }
    
    return {"mailbox_id": mailbox_id, "success": False, "error": "Unknown error", "retries": max_retries}


async def run_smartlead_upload_for_batch(
    batch_id: str,
    api_key: str,
    oauth_url: str,
    num_workers: int = 3,
    headless: bool = True,
    skip_uploaded: bool = True,
    configure_settings: bool = True,
    sending_settings: Optional[dict] = None,
    warmup_settings: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Main async function to upload all mailboxes in a batch to Smartlead.
    Uses parallel browser workers for faster processing.
    
    Args:
        batch_id: SetupBatch UUID
        api_key: Smartlead API key
        oauth_url: Smartlead's custom Microsoft OAuth URL
        num_workers: Number of parallel browser workers (1-5)
        headless: Run browsers in headless mode
        skip_uploaded: Skip mailboxes already uploaded
        configure_settings: Whether to configure sending/warmup settings after upload
        sending_settings: Dict with max_per_day, wait_mins, tracking_url
        warmup_settings: Dict with per_day, rampup, reply_rate
        
    Returns:
        Dict with summary: total, uploaded, failed, skipped, errors
    """
    from app.models.mailbox import Mailbox
    from app.models.tenant import Tenant
    from app.models.batch import SetupBatch
    from app.db.session import async_session_factory

    logger.info(f"Starting Smartlead upload for batch {batch_id} with {num_workers} workers")
    
    # Default settings
    sending = sending_settings or {"max_per_day": 6, "wait_mins": 60, "tracking_url": ""}
    warmup = warmup_settings or {"per_day": 40, "rampup": 5, "reply_rate": 79}

    # Fetch mailboxes from database
    async with async_session_factory() as session:
        # Get batch to verify it exists
        batch_result = await session.execute(
            select(SetupBatch).where(SetupBatch.id == batch_id)
        )
        batch = batch_result.scalar_one_or_none()
        if not batch:
            return {"error": "Batch not found", "total": 0, "uploaded": 0, "failed": 0, "skipped": 0}
        
        # Get all mailboxes for tenants in this batch
        query = (
            select(Mailbox)
            .join(Tenant, Mailbox.tenant_id == Tenant.id)
            .where(Tenant.batch_id == batch_id)
        )
        
        if skip_uploaded:
            query = query.where(Mailbox.smartlead_uploaded == False)
            
        result = await session.execute(query)
        mailboxes = result.scalars().all()
        
        if not mailboxes:
            logger.info(f"No mailboxes to upload for batch {batch_id}")
            return {"total": 0, "uploaded": 0, "failed": 0, "skipped": 0, "errors": []}
        
        # Prepare mailbox data for workers
        mailbox_list = [
            {
                "id": str(mb.id),
                "email": mb.email,
                "password": mb.initial_password or mb.password or "#Sendemails1"
            }
            for mb in mailboxes
        ]
    
    logger.info(f"Found {len(mailbox_list)} mailboxes to upload to Smartlead")
    
    # Check for existing accounts in Smartlead (deduplication)
    api = SmartleadAPI(api_key)
    existing_emails = set()
    try:
        existing_emails = await api.get_existing_emails()
        logger.info(f"Found {len(existing_emails)} existing accounts in Smartlead")
    except Exception as e:
        logger.warning(f"Could not fetch existing Smartlead accounts: {e}")
    
    # Filter out already existing accounts
    to_upload = []
    skipped_count = 0
    for mb in mailbox_list:
        if mb["email"].lower() in existing_emails:
            skipped_count += 1
            logger.info(f"Skipping {mb['email']} - already exists in Smartlead")
            # Mark as uploaded in DB
            async with async_session_factory() as session:
                await session.execute(
                    update(Mailbox)
                    .where(Mailbox.id == mb["id"])
                    .values(
                        smartlead_uploaded=True,
                        smartlead_uploaded_at=datetime.utcnow(),
                        smartlead_upload_error="Skipped - already exists"
                    )
                )
                await session.commit()
        else:
            to_upload.append(mb)
    
    if not to_upload:
        await api.close()
        return {
            "total": len(mailbox_list),
            "uploaded": 0,
            "failed": 0,
            "skipped": skipped_count,
            "errors": []
        }
    
    # Run parallel upload using ThreadPoolExecutor
    uploaded_count = 0
    failed_count = 0
    errors = []
    settings_configured = 0
    warmup_configured = 0
    
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        # Create one uploader per worker
        uploaders = []
        for i in range(num_workers):
            uploader = SmartleadOAuthUploader(headless=headless, worker_id=i)
            uploaders.append(uploader)
        
        try:
            # Submit tasks to executor
            futures = []
            for i, mailbox_data in enumerate(to_upload):
                uploader = uploaders[i % len(uploaders)]
                future = executor.submit(
                    process_smartlead_mailbox_sync,
                    uploader,
                    mailbox_data,
                    oauth_url
                )
                futures.append(future)
            
            # Process results as they complete
            for future in as_completed(futures):
                result = future.result()
                
                # Update database
                async with async_session_factory() as session:
                    if result["success"]:
                        await session.execute(
                            update(Mailbox)
                            .where(Mailbox.id == result["mailbox_id"])
                            .values(
                                smartlead_uploaded=True,
                                smartlead_uploaded_at=datetime.utcnow(),
                                smartlead_upload_error=None
                            )
                        )
                        uploaded_count += 1
                        
                        # Configure settings if enabled
                        if configure_settings:
                            mb_result = await session.execute(
                                select(Mailbox).where(Mailbox.id == result["mailbox_id"])
                            )
                            mb = mb_result.scalar_one_or_none()
                            if mb:
                                await asyncio.sleep(3)  # Wait for Smartlead to register
                                account_id = await api.find_account_id(mb.email)
                                if account_id:
                                    if await api.update_sending_settings(account_id, **sending):
                                        settings_configured += 1
                                    if await api.update_warmup_settings(account_id, **warmup):
                                        warmup_configured += 1
                    else:
                        await session.execute(
                            update(Mailbox)
                            .where(Mailbox.id == result["mailbox_id"])
                            .values(
                                smartlead_uploaded=False,
                                smartlead_upload_error=result["error"]
                            )
                        )
                        failed_count += 1
                        errors.append(result["error"])
                    
                    await session.commit()
                
                logger.info(f"Progress: {uploaded_count + failed_count}/{len(to_upload)} processed")
        
        finally:
            pass  # Uploaders clean up automatically (no login state to maintain)
    
    await api.close()
    
    logger.info(f"Smartlead upload complete: {uploaded_count} uploaded, {failed_count} failed, {skipped_count} skipped")
    
    return {
        "total": len(mailbox_list),
        "uploaded": uploaded_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "settings_configured": settings_configured,
        "warmup_configured": warmup_configured,
        "errors": errors[:10]  # Limit to first 10 errors
    }


async def bulk_configure_smartlead_settings(
    api_key: str,
    emails: list[str],
    sending: dict | None = None,
    warmup: dict | None = None,
) -> list[AccountResult]:
    """Bulk update sending + warmup settings for existing Smartlead accounts (no upload)."""
    api = SmartleadAPI(api_key)
    results = []

    try:
        await api.get_all_accounts()  # Build cache

        for email in emails:
            aid = await api.find_account_id(email)
            if not aid:
                results.append(AccountResult(
                    email=email, success=False, action="failed",
                    error="Account not found in Smartlead",
                ))
                continue

            s_ok = True
            w_ok = True
            if sending:
                s_ok = await api.update_sending_settings(aid, **sending)
            if warmup:
                w_ok = await api.update_warmup_settings(aid, **warmup)

            results.append(AccountResult(
                email=email,
                success=s_ok and w_ok,
                action="settings_updated",
                smartlead_id=aid,
                error=None if (s_ok and w_ok) else "Partial settings update failure",
            ))

            await asyncio.sleep(0.2)  # Rate limit

    finally:
        await api.close()

    return results
