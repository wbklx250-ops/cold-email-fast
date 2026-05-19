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
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from typing import Optional, Dict, Any

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select, update

logger = logging.getLogger("smartlead")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SMARTLEAD_API_BASE = "https://server.smartlead.ai/api/v1"
DEFAULT_SMARTLEAD_WORKERS = 1
DEFAULT_SMARTLEAD_MAX_RETRIES = 1
DEFAULT_SMARTLEAD_COOLDOWN_SECONDS = 2.0
DEFAULT_SMARTLEAD_RESOURCE_FAILURE_LIMIT = 2
RESOURCE_FAILURE_MARKERS = (
    "resource temporarily unavailable",
    "failed to start a thread",
    "unable to obtain driver",
    "session not created",
    "chrome failed to start",
    "devtoolsactiveport file doesn't exist",
    "unsupported architecture",
)


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
    Uses Selenium — must be run outside the async event loop.
    """

    def __init__(self, headless: bool = True, worker_id: int = 0):
        self.headless = headless
        self.worker_id = worker_id
        self.last_error: Optional[str] = None

    def _build_driver(self):
        """Create a Chrome driver and isolated temp profile for one OAuth attempt."""
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        chrome_options = webdriver.ChromeOptions()
        chrome_binary = os.getenv("CHROME_PATH")
        if chrome_binary:
            chrome_options.binary_location = chrome_binary

        profile_dir = tempfile.mkdtemp(prefix=f"smartlead-w{self.worker_id}-")
        chrome_options.add_argument(f"--user-data-dir={profile_dir}")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-software-rasterizer")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-infobars")
        chrome_options.add_argument("--disable-notifications")
        chrome_options.add_argument("--disable-popup-blocking")
        chrome_options.add_argument("--no-first-run")
        chrome_options.add_argument("--no-default-browser-check")
        chrome_options.add_argument("--disable-background-networking")
        chrome_options.add_argument("--disable-sync")
        chrome_options.add_argument("--disable-default-apps")
        chrome_options.add_argument("--disable-component-update")
        chrome_options.add_argument("--disable-crash-reporter")
        chrome_options.add_argument("--disable-crashpad")
        chrome_options.add_argument("--disable-breakpad")
        chrome_options.add_argument("--no-zygote")
        chrome_options.add_argument("--remote-debugging-port=0")
        chrome_options.add_argument("--disable-features=ThirdPartyCookieBlocking")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)
        chrome_options.add_experimental_option(
            "prefs",
            {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
                "profile.password_manager_leak_detection": False,
                "profile.cookie_controls_mode": 0,
                "profile.block_third_party_cookies": False,
            },
        )

        if self.headless:
            chrome_options.add_argument("--headless=new")

        chromedriver_path = os.getenv("CHROMEDRIVER_PATH")
        if chromedriver_path and os.path.exists(chromedriver_path):
            driver = webdriver.Chrome(
                service=Service(executable_path=chromedriver_path),
                options=chrome_options,
            )
        else:
            logger.warning(
                "CHROMEDRIVER_PATH missing/unusable (%s); falling back to Selenium Manager",
                chromedriver_path,
            )
            driver = webdriver.Chrome(options=chrome_options)

        return driver, profile_dir

    def chrome_preflight(self) -> tuple[bool, Optional[str]]:
        """Start Chrome once before a large upload so infrastructure failures fail fast."""
        driver = None
        profile_dir = None
        try:
            driver, profile_dir = self._build_driver()
            driver.set_page_load_timeout(10)
            driver.get("data:text/html,<html><title>smartlead-preflight</title><body>ok</body></html>")
            return True, None
        except Exception as e:
            return False, str(e)
        finally:
            self._cleanup_driver(driver, profile_dir)

    def upload_account(self, email: str, password: str, oauth_url: str) -> bool:
        """Upload a single M365 account via OAuth. Returns True on success."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        driver = None
        profile_dir = None
        self.last_error = None
        try:
            driver, profile_dir = self._build_driver()
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
                self.last_error = f"Email field not found; {self._page_context(driver)}"
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
                self.last_error = f"Password field not found; {self._page_context(driver)}"
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
                self.last_error = f"OAuth incomplete; {self._page_context(driver)}"
                return False
            else:
                logger.warning(f"[Worker {self.worker_id}] Unclear result for {email}, URL: {driver.current_url}")
                self._screenshot(driver, f"unclear_{email.split('@')[0]}")
                self.last_error = f"OAuth ended on unexpected page; {self._page_context(driver)}"
                return False

        except Exception as e:
            logger.error(f"[Worker {self.worker_id}] OAuth failed for {email}: {e}")
            if not self.last_error:
                self.last_error = f"{e}; {self._page_context(driver)}" if driver else str(e)
            if driver:
                self._screenshot(driver, f"error_{email.split('@')[0]}")
            return False

        finally:
            self._cleanup_driver(driver, profile_dir)

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

    def _page_context(self, driver) -> str:
        if not driver:
            return "browser_not_started"

        parts = []
        try:
            parts.append(f"url={driver.current_url}")
        except Exception:
            pass
        try:
            parts.append(f"title={driver.title}")
        except Exception:
            pass
        try:
            from selenium.webdriver.common.by import By

            body = driver.find_element(By.TAG_NAME, "body").text
            body = " ".join(body.split())[:500]
            if body:
                parts.append(f"body={body}")
        except Exception:
            pass
        return "; ".join(parts) if parts else "page_context_unavailable"

    def _cleanup_driver(self, driver, profile_dir: Optional[str]) -> None:
        if driver:
            driver_pid = None
            try:
                service = getattr(driver, "service", None)
                process = getattr(service, "process", None)
                driver_pid = getattr(process, "pid", None)
            except Exception:
                driver_pid = None

            try:
                driver.quit()
            except Exception:
                pass

            if driver_pid:
                self._kill_child_processes(driver_pid)

        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)

    def _kill_child_processes(self, parent_pid: int) -> None:
        if os.name == "nt":
            return

        try:
            child_result = subprocess.run(
                ["pgrep", "-P", str(parent_pid)],
                capture_output=True,
                text=True,
                timeout=3,
            )
            child_pids = [
                pid.strip()
                for pid in child_result.stdout.splitlines()
                if pid.strip().isdigit()
            ]
            for pid in child_pids:
                subprocess.run(["kill", "-TERM", pid], capture_output=True, timeout=2)
            time.sleep(0.2)
            for pid in child_pids:
                subprocess.run(["kill", "-KILL", pid], capture_output=True, timeout=2)
        except Exception:
            pass

    def _screenshot(self, driver, label):
        try:
            screenshot_dir = os.getenv("SCREENSHOT_DIR", "screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)
            safe_label = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in label)
            driver.save_screenshot(
                os.path.join(
                    screenshot_dir,
                    f"sl_{self.worker_id}_{safe_label}_{datetime.now().strftime('%H%M%S')}.png",
                )
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
    max_retries: int = DEFAULT_SMARTLEAD_MAX_RETRIES
) -> Dict[str, Any]:
    """
    Synchronous function to process a single mailbox upload to Smartlead.
    Runs in a worker thread.
    """
    mailbox_id = mailbox_data["id"]
    email = mailbox_data["email"]
    password = mailbox_data["password"]
    last_error = None

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
                last_error = uploader.last_error or "OAuth upload returned false"
                if attempt < max_retries:
                    logger.warning(
                        f"[Worker {uploader.worker_id}] Attempt {attempt + 1} failed for {email}: {last_error}; retrying..."
                    )
                    time.sleep(3)
                else:
                    return {
                        "mailbox_id": mailbox_id,
                        "success": False,
                        "error": last_error or "OAuth upload failed after retries",
                        "retries": attempt
                    }
        except Exception as e:
            last_error = str(e)
            if attempt < max_retries:
                time.sleep(3)
            else:
                return {
                    "mailbox_id": mailbox_id,
                    "success": False,
                    "error": str(e),
                    "retries": attempt
                }
    
    return {
        "mailbox_id": mailbox_id,
        "success": False,
        "error": last_error or "Unknown error",
        "retries": max_retries,
    }


def _is_resource_failure(error: Optional[str]) -> bool:
    if not error:
        return False
    normalized = error.lower()
    return any(marker in normalized for marker in RESOURCE_FAILURE_MARKERS)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _mailbox_not_ready_reasons(mailbox) -> list[str]:
    reasons = []
    if not mailbox.created_in_exchange:
        reasons.append("created_in_exchange=false")
    if not mailbox.delegated:
        reasons.append("delegated=false")
    if not mailbox.password_set:
        reasons.append("password_set=false")
    if not mailbox.account_enabled:
        reasons.append("account_enabled=false")
    if not (mailbox.initial_password or mailbox.password):
        reasons.append("missing_password")
    return reasons


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

    requested_workers = num_workers
    configured_max_workers = _env_int("SMARTLEAD_MAX_WORKERS", DEFAULT_SMARTLEAD_WORKERS)
    num_workers = max(1, min(num_workers, configured_max_workers))
    max_retries = max(0, _env_int("SMARTLEAD_MAX_RETRIES", DEFAULT_SMARTLEAD_MAX_RETRIES))
    cooldown_seconds = max(
        0.0,
        _env_float("SMARTLEAD_ACCOUNT_COOLDOWN_SECONDS", DEFAULT_SMARTLEAD_COOLDOWN_SECONDS),
    )
    resource_failure_limit = max(
        1,
        _env_int("SMARTLEAD_RESOURCE_FAILURE_LIMIT", DEFAULT_SMARTLEAD_RESOURCE_FAILURE_LIMIT),
    )

    if requested_workers != num_workers:
        logger.warning(
            "Smartlead workers capped from %s to %s (SMARTLEAD_MAX_WORKERS=%s)",
            requested_workers,
            num_workers,
            configured_max_workers,
        )

    logger.info(
        "Starting Smartlead upload for batch %s with %s worker(s), max_retries=%s, cooldown=%ss",
        batch_id,
        num_workers,
        max_retries,
        cooldown_seconds,
    )
    
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
        
        # Get all candidate mailboxes for tenants in this batch.
        query = (
            select(Mailbox)
            .join(Tenant, Mailbox.tenant_id == Tenant.id)
            .where(Tenant.batch_id == batch_id)
        )
        
        if skip_uploaded:
            query = query.where(Mailbox.smartlead_uploaded == False)
            
        result = await session.execute(query)
        candidate_mailboxes = result.scalars().all()
        
        if not candidate_mailboxes:
            logger.info(f"No mailboxes to upload for batch {batch_id}")
            return {"total": 0, "uploaded": 0, "failed": 0, "skipped": 0, "errors": []}

        mailboxes = []
        ineligible_count = 0
        ineligible_errors = []
        for mb in candidate_mailboxes:
            not_ready_reasons = _mailbox_not_ready_reasons(mb)
            if not_ready_reasons:
                ineligible_count += 1
                error = f"Skipped - mailbox not upload-ready ({', '.join(not_ready_reasons)})"
                ineligible_errors.append(error)
                await session.execute(
                    update(Mailbox)
                    .where(Mailbox.id == mb.id)
                    .values(
                        smartlead_uploaded=False,
                        smartlead_upload_error=error,
                        uploaded_to_sequencer=False,
                        upload_error=error,
                    )
                )
            else:
                mailboxes.append(mb)

        if ineligible_count:
            await session.commit()
            logger.warning(
                "Skipped %s Smartlead candidate mailbox(es) because they were not upload-ready",
                ineligible_count,
            )
        
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
    total_count = len(mailbox_list) + ineligible_count
    
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
                        smartlead_upload_error=None,
                        uploaded_to_sequencer=True,
                        uploaded_at=datetime.utcnow(),
                        sequencer_name="smartlead",
                        upload_error=None,
                    )
                )
                await session.commit()
        else:
            to_upload.append(mb)
    
    if not to_upload:
        await api.close()
        return {
            "total": total_count,
            "uploaded": 0,
            "failed": ineligible_count,
            "skipped": skipped_count,
            "ineligible": ineligible_count,
            "errors": ineligible_errors[:10],
        }
    
    # Run uploads serially. Chrome/OAuth sessions are too expensive to fan out in Railway.
    uploaded_count = 0
    failed_count = 0
    errors = []
    settings_configured = 0
    warmup_configured = 0

    preflight_uploader = SmartleadOAuthUploader(headless=headless, worker_id="preflight")
    preflight_ok, preflight_error = preflight_uploader.chrome_preflight()
    if not preflight_ok:
        await api.close()
        raise RuntimeError(f"Smartlead Chrome preflight failed: {preflight_error}")

    uploader = SmartleadOAuthUploader(headless=headless, worker_id=0)
    consecutive_resource_failures = 0

    for mailbox_data in to_upload:
        result = await asyncio.to_thread(
            process_smartlead_mailbox_sync,
            uploader,
            mailbox_data,
            oauth_url,
            max_retries,
        )

        error = result.get("error")
        if result["success"]:
            consecutive_resource_failures = 0
        elif _is_resource_failure(error):
            consecutive_resource_failures += 1
            errors.append(error)
            logger.error(
                "Smartlead browser resource failure %s/%s for %s: %s",
                consecutive_resource_failures,
                resource_failure_limit,
                mailbox_data["email"],
                error,
            )
            if consecutive_resource_failures >= resource_failure_limit:
                await api.close()
                raise RuntimeError(
                    "Stopping Smartlead upload after "
                    f"{consecutive_resource_failures} browser resource failure(s): {error}"
                )
            await asyncio.sleep(max(cooldown_seconds, 5.0))
            continue
        else:
            consecutive_resource_failures = 0

        # Update database
        async with async_session_factory() as session:
            if result["success"]:
                await session.execute(
                    update(Mailbox)
                    .where(Mailbox.id == result["mailbox_id"])
                    .values(
                        smartlead_uploaded=True,
                        smartlead_uploaded_at=datetime.utcnow(),
                        smartlead_upload_error=None,
                        uploaded_to_sequencer=True,
                        uploaded_at=datetime.utcnow(),
                        sequencer_name="smartlead",
                        upload_error=None,
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
                        smartlead_upload_error=error,
                        uploaded_to_sequencer=False,
                        upload_error=error,
                    )
                )
                failed_count += 1
                errors.append(error)

            await session.commit()

        logger.info(f"Progress: {uploaded_count + failed_count}/{len(to_upload)} processed")
        if cooldown_seconds:
            await asyncio.sleep(cooldown_seconds)
    
    await api.close()
    
    logger.info(f"Smartlead upload complete: {uploaded_count} uploaded, {failed_count} failed, {skipped_count} skipped")
    
    return {
        "total": total_count,
        "uploaded": uploaded_count,
        "failed": failed_count + ineligible_count,
        "skipped": skipped_count,
        "ineligible": ineligible_count,
        "settings_configured": settings_configured,
        "warmup_configured": warmup_configured,
        "errors": (ineligible_errors + errors)[:10]  # Limit to first 10 errors
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
