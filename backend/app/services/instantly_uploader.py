"""
Instantly.ai Uploader Service
Automates uploading mailboxes to Instantly.ai via Selenium OAuth flow

Ported from the hardened standalone script that is proven to work.
Uses native clicks with scrollIntoView, image blocking to prevent
overlay widgets from loading, and API-verified success.
"""
import asyncio
import logging
import time
import random
import os
import shutil
import signal
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Dict, Optional, Any

import requests as http_requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException, WebDriverException, NoSuchElementException,
    NoSuchWindowException, InvalidSessionIdException,
    ElementClickInterceptedException, StaleElementReferenceException
)
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mailbox import Mailbox
from app.models.tenant import Tenant
from app.models.domain import Domain
from app.models.batch import SetupBatch
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)


MAILBOX_HARD_TIMEOUT_SECONDS = int(os.getenv("INSTANTLY_MAILBOX_TIMEOUT_SECONDS", "420"))
INSTANTLY_USE_OAUTH_API = os.getenv("INSTANTLY_USE_OAUTH_API", "true").lower() not in {
    "0", "false", "no"
}


# ---------------------------------------------------------------------------
# Instantly API Client
# ---------------------------------------------------------------------------

class InstantlyAPI:
    """Thin wrapper around Instantly API V2 for account verification."""

    BASE = "https://api.instantly.ai/api/v2"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = http_requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        # Cache of known accounts (populated on first load)
        self._known_emails: set = set()
        self._cache_loaded = False

    def test_connection(self) -> bool:
        """Test if API key works."""
        try:
            r = self.session.get(f"{self.BASE}/accounts", params={"limit": 1}, timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def load_all_accounts(self) -> set:
        """Fetch ALL account emails from Instantly (paginated).
        Returns a set of lowercase email addresses."""
        emails = set()
        starting_after = None

        while True:
            params = {"limit": 100}
            if starting_after:
                params["starting_after"] = starting_after

            try:
                r = self.session.get(f"{self.BASE}/accounts", params=params, timeout=15)
                if r.status_code != 200:
                    break
                data = r.json()
                items = data.get("items", [])
                if not items:
                    break

                for item in items:
                    email = item.get("email", "").strip().lower()
                    if email:
                        emails.add(email)

                next_after = data.get("next_starting_after")
                if not next_after or next_after == starting_after:
                    break
                starting_after = next_after
            except Exception:
                break

        self._known_emails = emails
        self._cache_loaded = True
        return emails

    def account_exists(self, email: str) -> bool:
        """Check if a specific account exists in Instantly.
        Uses cache first, then does a targeted search if not cached."""
        email_lower = email.strip().lower()

        if self._cache_loaded and email_lower in self._known_emails:
            return True

        try:
            r = self.session.get(
                f"{self.BASE}/accounts/{email}",
                timeout=10,
            )
            if r.status_code == 200:
                self._known_emails.add(email_lower)
                return True
        except Exception:
            pass

        return False

    def verify_account(self, email: str, max_wait=15, poll_interval=3) -> bool:
        """Wait and poll for account to appear in Instantly after OAuth.
        Returns True if confirmed, False if not found after max_wait seconds."""
        email_lower = email.strip().lower()

        elapsed = 0
        while elapsed < max_wait:
            try:
                r = self.session.get(
                    f"{self.BASE}/accounts/{email}",
                    timeout=10,
                )
                if r.status_code == 200:
                    self._known_emails.add(email_lower)
                    return True
            except Exception:
                pass

            time.sleep(poll_interval)
            elapsed += poll_interval

        return False

    def init_microsoft_oauth(self, email: str) -> Dict[str, Any]:
        """Create an Instantly Microsoft OAuth session and return its auth URL."""
        try:
            r = self.session.post(
                f"{self.BASE}/oauth/microsoft/init",
                json={"email": email},
                timeout=15,
            )
            if r.status_code >= 400:
                return {
                    "success": False,
                    "error": f"init status {r.status_code}: {r.text[:300]}",
                }
            data = r.json()
            auth_url = data.get("auth_url") or data.get("authUrl") or data.get("url")
            session_id = (
                data.get("session_id")
                or data.get("sessionId")
                or data.get("id")
                or data.get("state")
            )
            if not auth_url:
                return {
                    "success": False,
                    "error": f"init response missing auth_url: {data}",
                }
            return {
                "success": True,
                "auth_url": auth_url,
                "session_id": session_id,
                "raw": data,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def oauth_status(self, session_id: Optional[str], email: str) -> Dict[str, Any]:
        """Check Instantly OAuth session status, falling back to account lookup."""
        if session_id:
            for path in (
                f"{self.BASE}/oauth/status/{session_id}",
                f"{self.BASE}/oauth/{session_id}/status",
            ):
                try:
                    r = self.session.get(path, timeout=10)
                    if r.status_code == 200:
                        data = r.json()
                        status = str(data.get("status") or "").lower()
                        if status in {"success", "completed", "connected", "done"}:
                            self._known_emails.add(email.strip().lower())
                            return {"success": True, "status": status, "raw": data}
                        if status in {"failed", "error", "expired", "cancelled", "canceled"}:
                            return {
                                "success": False,
                                "done": True,
                                "status": status,
                                "error": data.get("error") or data.get("message") or str(data),
                            }
                        return {"success": False, "done": False, "status": status, "raw": data}
                except Exception:
                    pass

        if self.account_exists(email):
            return {"success": True, "status": "account_exists"}

        return {"success": False, "done": False, "status": "pending"}

    def wait_for_oauth_success(
        self,
        session_id: Optional[str],
        email: str,
        max_wait: int = 45,
        poll_interval: int = 3,
    ) -> bool:
        elapsed = 0
        while elapsed < max_wait:
            status = self.oauth_status(session_id, email)
            if status.get("success"):
                return True
            if status.get("done"):
                return False
            time.sleep(poll_interval)
            elapsed += poll_interval
        return False


# ---------------------------------------------------------------------------
# Core Uploader — ported from working standalone script
# ---------------------------------------------------------------------------

class InstantlyUploader:
    """Handles Selenium automation for uploading mailboxes to Instantly.ai.
    
    Ported from the proven standalone instantly_automation_working.py script.
    Uses native clicks with scrollIntoView, image blocking, and no
    selenium_stealth to avoid overlay/widget interference.
    """

    RESTART_EVERY_N = 30  # Preventive browser restart after N accounts

    def __init__(
        self,
        instantly_email: str,
        instantly_password: str,
        api: Optional[InstantlyAPI] = None,
        worker_id: int = 0
    ):
        self.instantly_email = instantly_email
        self.instantly_password = instantly_password
        self.api = api
        self.worker_id = worker_id
        self.driver: Optional[webdriver.Chrome] = None
        self._accounts_since_restart = 0
        self.retired = False
        self._profile_dir: Optional[str] = None
        self._driver_pid: Optional[int] = None

    # ---- Browser Setup ----

    def setup_driver(self) -> bool:
        """Initialize Chrome WebDriver — matches working standalone script."""
        try:
            opts = Options()
            if self._profile_dir:
                shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = tempfile.mkdtemp(prefix=f"instantly-w{self.worker_id}-")

            # Use Railway chromium path if set, otherwise system default
            chrome_path = os.getenv("CHROME_PATH")
            if chrome_path:
                opts.binary_location = chrome_path

            opts.add_argument("--disable-popup-blocking")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
            opts.add_argument("--disable-extensions")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--disable-software-rasterizer")
            opts.add_argument("--disable-background-timer-throttling")
            opts.add_argument("--disable-backgrounding-occluded-windows")
            opts.add_argument("--disable-renderer-backgrounding")
            opts.add_argument("--disable-background-networking")
            opts.add_argument("--disable-sync")
            opts.add_argument("--disable-default-apps")
            opts.add_argument("--no-first-run")
            opts.add_argument("--no-default-browser-check")
            opts.add_argument("--disable-crash-reporter")
            opts.add_argument("--disk-cache-size=1")
            opts.add_argument("--media-cache-size=1")
            opts.add_argument("--remote-debugging-port=0")
            opts.add_argument("--disable-site-isolation-trials")
            opts.add_argument("--renderer-process-limit=2")
            opts.add_argument(f"--user-data-dir={self._profile_dir}")
            opts.add_argument("--window-size=1920,1080")
            opts.add_argument("--disable-features=ThirdPartyCookieBlocking")

            # Block images — prevents Featurebase/Intercom overlay widgets from loading
            opts.add_experimental_option("prefs", {
                "profile.managed_default_content_settings.images": 2,
                "profile.cookie_controls_mode": 0,
                "profile.block_third_party_cookies": False,
            })

            # Always headless on Railway
            opts.add_argument("--headless=new")

            if self.worker_id and isinstance(self.worker_id, int):
                offset = self.worker_id * 50
                opts.add_argument(f"--window-position={offset},{offset}")

            self.driver = webdriver.Chrome(options=opts)
            service_process = getattr(getattr(self.driver, "service", None), "process", None)
            self._driver_pid = getattr(service_process, "pid", None)
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            self.driver.set_page_load_timeout(60)
            self._accounts_since_restart = 0

            logger.info(f"[Worker {self.worker_id}] Chrome initialized (headless, images blocked)")
            return True
        except Exception as e:
            logger.error(f"[Worker {self.worker_id}] Chrome setup failed: {e}")
            return False

    def is_driver_alive(self) -> bool:
        """Check if browser driver is still responsive."""
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            return False

    def restart_browser(self) -> bool:
        """Restart browser and re-login to Instantly."""
        if self.retired:
            logger.warning(f"[Worker {self.worker_id}] Restart skipped because worker is retired")
            return False

        logger.info(f"[Worker {self.worker_id}] Restarting browser...")
        driver = self.driver
        driver_pid = self._driver_pid
        profile_dir = self._profile_dir
        self.driver = None
        self._driver_pid = None
        self._profile_dir = None
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
        self._kill_driver_process_tree(driver_pid)
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)

        if not self.setup_driver():
            return False
        if not self.login_to_instantly():
            return False

        logger.info(f"[Worker {self.worker_id}] Browser restarted successfully")
        return True

    def cleanup(self):
        """Clean up WebDriver resources."""
        self.retired = True
        driver = self.driver
        driver_pid = self._driver_pid
        profile_dir = self._profile_dir
        self.driver = None
        self._driver_pid = None
        self._profile_dir = None

        if driver:
            try:
                driver.quit()
                logger.info(f"[Worker {self.worker_id}] Chrome driver closed")
            except Exception as e:
                logger.error(f"[Worker {self.worker_id}] Error closing driver: {e}")
        self._kill_driver_process_tree(driver_pid)
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)

    def _kill_driver_process_tree(self, pid: Optional[int]) -> None:
        """Best-effort cleanup for orphaned ChromeDriver/Chrome children on Railway."""
        if not pid or os.name != "posix":
            return
        for pkill_arg, sig in (("-TERM", signal.SIGTERM), ("-KILL", signal.SIGKILL)):
            try:
                subprocess.run(
                    ["pkill", pkill_arg, "-P", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
            except Exception:
                pass
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            except Exception:
                pass
            time.sleep(0.2)

    # ---- Helpers (from working script) ----

    def delay(self, lo: float = 0.5, hi: float = 2.0):
        """Random delay."""
        time.sleep(random.uniform(lo, hi))

    def _find_and_click(self, selectors, timeout=5, label="element") -> bool:
        """Try multiple selectors, scrollIntoView, then native click.
        This is the proven click pattern from the working standalone script."""
        for sel_type, sel in selectors:
            el = None
            try:
                el = WebDriverWait(self.driver, timeout).until(
                    EC.element_to_be_clickable((sel_type, sel))
                )
                self.driver.execute_script("arguments[0].scrollIntoView(true);", el)
                self.delay(0.3, 0.6)
                el.click()
                return True
            except ElementClickInterceptedException:
                logger.warning(f"[Worker {self.worker_id}] Click intercepted by overlay — dismissing and retrying")
                self._dismiss_overlays()
                time.sleep(0.5)
                try:
                    if el is not None:
                        self.driver.execute_script("arguments[0].click()", el)
                        return True
                except Exception:
                    continue
            except (TimeoutException, NoSuchElementException, StaleElementReferenceException, AttributeError):
                continue
            except WebDriverException as e:
                logger.debug(f"[Worker {self.worker_id}] Click helper skipped selector {sel}: {e}")
                continue
        return False

    def _find_input(self, selectors, timeout=10):
        """Try multiple selectors to find an input field."""
        for sel_type, sel in selectors:
            try:
                return WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((sel_type, sel))
                )
            except (TimeoutException, NoSuchElementException):
                continue
        return None

    # ---- Cookies ----

    def clear_microsoft_cookies(self):
        """Clear ONLY Microsoft cookies, preserving Instantly session."""
        try:
            cookies = self.driver.get_cookies()
            ms_domains = [
                "login.microsoftonline.com", "login.live.com", "microsoft.com",
                "microsoftonline.com", "office.com", "outlook.com", "live.com",
            ]
            deleted = 0
            for cookie in cookies:
                domain = cookie.get("domain", "").lstrip(".")
                if any(ms in domain for ms in ms_domains):
                    try:
                        self.driver.delete_cookie(cookie["name"])
                        deleted += 1
                    except Exception:
                        pass
            if deleted > 0:
                logger.info(f"[Worker {self.worker_id}] Cleared {deleted} MS cookies")
        except Exception:
            pass

        # Close leftover popups
        try:
            windows = self.driver.window_handles
            if len(windows) > 1:
                main = windows[0]
                for w in windows[1:]:
                    try:
                        self.driver.switch_to.window(w)
                        self.driver.close()
                    except Exception:
                        pass
                self.driver.switch_to.window(main)
        except Exception:
            pass

    # ---- Instantly Login (from working script) ----

    def login_to_instantly(self) -> bool:
        """Log in to Instantly.ai — matches working standalone script."""
        try:
            logger.info(f"[Worker {self.worker_id}] Logging into Instantly...")
            self.driver.get("https://app.instantly.ai/auth/login")
            self.delay(2, 3)

            ef = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//input[@placeholder='Email']"))
            )
            ef.clear()
            ef.send_keys(self.instantly_email)
            self.delay(0.5, 1)

            pf = self.driver.find_element(By.XPATH, "//input[@placeholder='Password']")
            pf.clear()
            pf.send_keys(self.instantly_password)
            self.delay(0.5, 1)

            self.driver.find_element(By.XPATH, "//button[@type='submit']").click()
            WebDriverWait(self.driver, 15).until(lambda d: "auth/login" not in d.current_url)

            # Navigate to accounts page and wait (important for subsequent operations)
            self.driver.get("https://app.instantly.ai/app/accounts")
            WebDriverWait(self.driver, 10).until(EC.url_contains("app.instantly.ai/app/accounts"))
            self.delay(2, 3)

            logger.info(f"[Worker {self.worker_id}] Logged into Instantly")
            self._dismiss_overlays()
            return True
        except Exception as e:
            logger.error(f"[Worker {self.worker_id}] Login failed: {e}")
            return False

    # ---- Add Account via OAuth (from working script) ----

    def add_microsoft_account(
        self,
        email: str,
        password: str,
        mailbox_id: str
    ) -> Dict[str, Any]:
        """Add a Microsoft account to Instantly.ai via OAuth.
        Ported from the working standalone script's add_account method."""
        try:
            logger.info(f"[Worker {self.worker_id}] Starting upload for {email} (ID: {mailbox_id})")

            # Ensure we're on accounts page (not connect page from previous run)
            current = self.driver.current_url
            if "accounts" not in current or "connect" in current:
                self.driver.get("https://app.instantly.ai/app/accounts")
                self.delay(2, 3)

            # Dismiss any Featurebase/changelog overlays before clicking
            self._dismiss_overlays()

            # Click Add New
            if not self._find_and_click([
                (By.XPATH, "//button[contains(text(), 'Add New')]"),
                (By.XPATH, "//button[contains(text(), 'Add new')]"),
                (By.XPATH, "//*[contains(text(), 'Add') and contains(text(), 'New')]"),
            ], timeout=10):
                self.driver.get("https://app.instantly.ai/app/account/connect")

            WebDriverWait(self.driver, 10).until(
                EC.url_contains("app.instantly.ai/app/account/connect")
            )
            self.delay(1, 2)

            # Select Microsoft
            if not self._find_and_click([
                (By.XPATH, "//p[text()='Microsoft']"),
                (By.XPATH, "//div[contains(text(), 'Microsoft')]"),
                (By.XPATH, "//*[contains(text(), 'Office 365')]"),
            ], timeout=8):
                return {"success": False, "error": "Microsoft option not found"}
            self.delay(1, 2)

            # SMTP confirm
            self._find_and_click([
                (By.XPATH, "//button[contains(text(), 'Yes, SMTP has been enabled')]"),
                (By.XPATH, "//button[contains(text(), 'SMTP has been enabled')]"),
                (By.XPATH, "//button[contains(text(), 'Continue')]"),
                (By.CSS_SELECTOR, "button[type='submit']"),
            ], timeout=8)
            self.delay(2, 4)

            # Handle OAuth popup
            ok, err = self._handle_oauth_flow(email, password)
            if ok:
                return {"success": True, "error": None}
            else:
                return {"success": False, "error": err}

        except InvalidSessionIdException:
            return {"success": False, "error": "Browser session died"}
        except Exception as e:
            logger.error(f"[Worker {self.worker_id}] add_account error: {e}")
            return {"success": False, "error": str(e)}

    def _handle_oauth_flow(self, email: str, password: str) -> tuple:
        """Handle Microsoft OAuth popup — ported from working standalone script.
        Returns (success: bool, error: str)."""
        try:
            self.delay(2, 4)
            windows = self.driver.window_handles
            if len(windows) < 2:
                self.delay(3, 5)
                windows = self.driver.window_handles
            if len(windows) < 2:
                return False, "OAuth popup did not open"

            main_window = windows[0]
            self.driver.switch_to.window(windows[-1])

            # Account picker — click "Use another account" if present
            self._find_and_click([
                (By.XPATH, "//div[contains(text(), 'Use another account')]"),
                (By.XPATH, "//*[contains(text(), 'Use another account')]"),
            ], timeout=3)
            self.delay(1, 2)

            # Email
            email_input = self._find_input([
                (By.ID, "i0116"),
                (By.NAME, "loginfmt"),
                (By.XPATH, "//input[@type='email']"),
                (By.XPATH, "//input[contains(@placeholder,'email')]"),
            ], timeout=10)
            if not email_input:
                self._close_popups(main_window)
                return False, "Email field not found"

            email_input.clear()
            email_input.send_keys(email)
            self.delay(0.5, 1)

            self._find_and_click([
                (By.ID, "idSIButton9"),
                (By.XPATH, "//input[@value='Next']"),
            ], timeout=5)
            self.delay(2, 3)

            # Check for "doesn't exist"
            try:
                err = self.driver.find_element(
                    By.XPATH, "//*[contains(text(), \"doesn't exist\") or contains(text(), 'account doesn')]"
                )
                if err:
                    self._close_popups(main_window)
                    return False, "Account doesn't exist in Microsoft"
            except NoSuchElementException:
                pass

            # Password
            pw_input = self._find_input([
                (By.ID, "i0118"),
                (By.NAME, "passwd"),
                (By.XPATH, "//input[@type='password']"),
            ], timeout=10)
            if not pw_input:
                self._close_popups(main_window)
                return False, "Password field not found"

            pw_input.clear()
            pw_input.send_keys(password)
            self.delay(0.5, 1)

            self._find_and_click([
                (By.ID, "idSIButton9"),
                (By.XPATH, "//input[@value='Sign in']"),
            ], timeout=5)
            self.delay(3, 5)

            # Check wrong password
            try:
                err = self.driver.find_element(
                    By.XPATH, "//*[contains(text(), 'password is incorrect') or contains(text(), 'sign-in was blocked')]"
                )
                if err:
                    self._close_popups(main_window)
                    return False, "Wrong password or blocked"
            except NoSuchElementException:
                pass

            # Stay signed in? → No
            try:
                for st, sv in [
                    (By.XPATH, "//*[contains(text(), 'Stay signed in?')]"),
                    (By.ID, "KmsiCheckboxField"),
                ]:
                    try:
                        WebDriverWait(self.driver, 4).until(
                            EC.presence_of_element_located((st, sv))
                        )
                        self._find_and_click([
                            (By.ID, "idBtn_Back"),
                            (By.XPATH, "//input[@value='No']"),
                        ], timeout=5)
                        self.delay(2, 3)
                        break
                    except TimeoutException:
                        continue
            except Exception:
                pass

            # Accept permissions
            self._find_and_click([
                (By.XPATH, "//input[@value='Accept']"),
                (By.XPATH, "//button[contains(text(), 'Accept')]"),
                (By.ID, "idSIButton9"),
            ], timeout=8)

            # Wait for popup to close (up to 20 seconds)
            for _ in range(20):
                time.sleep(1)
                try:
                    cur = self.driver.window_handles
                    if len(cur) == 1:
                        self.driver.switch_to.window(cur[0])
                        return True, ""
                    if "instantly.ai" in self.driver.current_url:
                        self.driver.switch_to.window(main_window)
                        return True, ""
                except NoSuchWindowException:
                    try:
                        self.driver.switch_to.window(self.driver.window_handles[0])
                    except Exception:
                        pass
                    return True, ""
                except Exception:
                    pass

            self._close_popups(main_window)
            return True, ""  # Likely succeeded

        except NoSuchWindowException:
            try:
                self.driver.switch_to.window(self.driver.window_handles[0])
            except Exception:
                pass
            return True, ""
        except InvalidSessionIdException:
            return False, "Browser session died"
        except Exception as e:
            logger.error(f"[Worker {self.worker_id}] OAuth error: {e}")
            self._close_popups_safe()
            return False, str(e)

    def _close_popups(self, main_window):
        """Close all popup windows and switch back to main."""
        try:
            for w in self.driver.window_handles:
                if w != main_window:
                    self.driver.switch_to.window(w)
                    self.driver.close()
            self.driver.switch_to.window(main_window)
        except Exception:
            pass

    def _close_popups_safe(self):
        """Safely close popup windows and return to first window."""
        try:
            windows = self.driver.window_handles
            if len(windows) > 1:
                main = windows[0]
                for w in windows[1:]:
                    try:
                        self.driver.switch_to.window(w)
                        self.driver.close()
                    except Exception:
                        pass
                self.driver.switch_to.window(main)
            elif windows:
                self.driver.switch_to.window(windows[0])
        except Exception:
            pass

    def _dismiss_overlays(self):
        """Dismiss Featurebase changelog popups and any other overlays that block clicks."""
        try:
            # Remove Featurebase changelog overlay (most common blocker)
            self.driver.execute_script("""
                document.querySelectorAll(
                    '.fb-changelog-popup-overlay, [data-featurebase-widget], [class*="featurebase"]'
                ).forEach(el => el.remove());
            """)

            # Also try clicking any visible close/dismiss buttons on popups
            dismiss_selectors = [
                "//button[contains(@aria-label, 'Close')]",
                "//button[contains(text(), 'Got it')]",
                "//button[contains(text(), 'Dismiss')]",
                "//button[contains(text(), 'Not now')]",
            ]
            self.driver.implicitly_wait(0)
            for selector in dismiss_selectors:
                try:
                    btns = self.driver.find_elements(By.XPATH, selector)
                    for btn in btns:
                        try:
                            if btn is not None and btn.is_displayed():
                                btn.click()
                                time.sleep(0.3)
                        except Exception:
                            pass
                except Exception:
                    pass
            self.driver.implicitly_wait(10)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Synchronous mailbox processor (runs in ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def _is_browser_infra_error(error: Any) -> bool:
    """Return true for Chrome/Selenium transport failures that should not fail a mailbox."""
    text = str(error or "").lower()
    markers = [
        "timed out receiving message from renderer",
        "httpconnectionpool(host='localhost'",
        "read timed out",
        "invalid session id",
        "chrome not reachable",
        "session deleted because of page crash",
        "disconnected",
        "target window already closed",
        "browser restart failed",
        "browser crashed",
    ]
    return any(marker in text for marker in markers)


def process_mailbox_sync(
    uploader: InstantlyUploader,
    mailbox_data: Dict[str, Any],
    max_retries: int = 1
) -> Dict[str, Any]:
    """
    Synchronous function to process a single mailbox upload.
    Runs in ThreadPoolExecutor. Includes cookie clearing after each
    account (matching the working standalone script).
    """
    mailbox_id = mailbox_data["id"]
    email = mailbox_data["email"]
    password = mailbox_data["password"]

    # Pre-check: Skip if account already exists (API)
    if uploader.api and uploader.api.account_exists(email):
        logger.info(f"[Worker {uploader.worker_id}] ⊘ {email} — already in Instantly (API), skipping")
        return {
            "mailbox_id": mailbox_id,
            "success": True,
            "error": None,
            "retries": 0,
            "verified": True,
            "skipped": True,
        }

    # Preventive browser restart after every N accounts
    uploader._accounts_since_restart += 1
    if uploader._accounts_since_restart >= uploader.RESTART_EVERY_N:
        logger.info(
            f"[Worker {uploader.worker_id}] Preventive restart after "
            f"{uploader._accounts_since_restart} accounts"
        )
        if not uploader.restart_browser():
            logger.error(f"[Worker {uploader.worker_id}] Preventive restart failed — retiring worker")
            return {
                "mailbox_id": mailbox_id,
                "success": False,
                "error": "Browser restart failed",
                "retries": 0,
                "verified": False,
                "worker_dead": True,
            }

    # Check driver health
    if not uploader.is_driver_alive():
        logger.warning(f"[Worker {uploader.worker_id}] Driver not alive — restarting")
        if not uploader.restart_browser():
            return {
                "mailbox_id": mailbox_id,
                "success": False,
                "error": "Browser crashed and restart failed",
                "retries": 0,
                "verified": False,
                "worker_dead": True,
            }

    # Try OAuth upload with retries
    oauth_passed = False
    last_error = ""

    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                logger.info(f"[Worker {uploader.worker_id}] Retry {attempt} for {email}")
                if not uploader.is_driver_alive():
                    if not uploader.restart_browser():
                        return {
                            "mailbox_id": mailbox_id,
                            "success": False,
                            "error": "Browser restart failed",
                            "retries": attempt,
                            "verified": False,
                            "worker_dead": True,
                        }
                try:
                    uploader.driver.get("https://app.instantly.ai/app/accounts")
                    uploader.delay(2, 3)
                except Exception:
                    if not uploader.restart_browser():
                        return {
                            "mailbox_id": mailbox_id,
                            "success": False,
                            "error": "Browser restart failed",
                            "retries": attempt,
                            "verified": False,
                            "worker_dead": True,
                        }

            result = uploader.add_microsoft_account(email, password, mailbox_id)
            if result["success"]:
                oauth_passed = True
                break
            last_error = result["error"] or "Unknown error"
            if _is_browser_infra_error(last_error):
                logger.warning(
                    f"[Worker {uploader.worker_id}] Browser infrastructure error for {email}: {last_error}"
                )
                if not uploader.restart_browser():
                    return {
                        "mailbox_id": mailbox_id,
                        "success": False,
                        "error": last_error,
                        "retries": attempt,
                        "verified": False,
                        "worker_dead": True,
                    }
                if attempt >= max_retries:
                    return {
                        "mailbox_id": mailbox_id,
                        "success": False,
                        "error": last_error,
                        "retries": attempt,
                        "verified": False,
                        "worker_dead": True,
                    }
                continue

            # Clear MS cookies between retries
            if uploader.is_driver_alive():
                uploader.clear_microsoft_cookies()

        except Exception as e:
            last_error = f"Exception during upload: {str(e)}"
            logger.error(f"[Worker {uploader.worker_id}] {last_error}")
            if _is_browser_infra_error(last_error):
                if not uploader.restart_browser() or attempt >= max_retries:
                    return {
                        "mailbox_id": mailbox_id,
                        "success": False,
                        "error": last_error,
                        "retries": attempt,
                        "verified": False,
                        "worker_dead": True,
                    }
            if attempt < max_retries:
                time.sleep(3)

    # Always clear MS cookies after each account (matching working script)
    if uploader.is_driver_alive():
        uploader.clear_microsoft_cookies()

    # API Verification
    if oauth_passed:
        verified = True
        if uploader.api:
            logger.info(f"[Worker {uploader.worker_id}] OAuth done for {email} — verifying via API...")
            verified = uploader.api.verify_account(email, max_wait=15, poll_interval=3)

            if verified:
                logger.info(f"[Worker {uploader.worker_id}] ✓ {email} — API CONFIRMED")
            else:
                logger.warning(f"[Worker {uploader.worker_id}] ⚠ {email} — OAuth passed but NOT found in API")
                return {
                    "mailbox_id": mailbox_id,
                    "success": False,
                    "error": "OAuth completed but account not found in Instantly API",
                    "retries": attempt,
                    "verified": False,
                }

        return {
            "mailbox_id": mailbox_id,
            "success": True,
            "error": None,
            "retries": attempt,
            "verified": verified,
        }
    else:
        logger.error(f"[Worker {uploader.worker_id}] All attempts failed for {email}: {last_error}")
        if _is_browser_infra_error(last_error):
            return {
                "mailbox_id": mailbox_id,
                "success": False,
                "error": last_error,
                "retries": max_retries,
                "verified": False,
                "worker_dead": True,
            }
        return {
            "mailbox_id": mailbox_id,
            "success": False,
            "error": last_error,
            "retries": max_retries,
            "verified": False,
        }


# ---------------------------------------------------------------------------
# Async batch orchestrator
# ---------------------------------------------------------------------------

async def run_instantly_upload_for_batch(
    batch_id: str,
    instantly_email: str,
    instantly_password: str,
    instantly_api_key: Optional[str] = None,
    num_workers: int = 2,
    skip_uploaded: bool = True,
    batch_retry_rounds: int = 3
) -> Dict[str, Any]:
    """
    Main async function to upload all mailboxes in a batch to Instantly.ai
    Uses parallel browser workers for faster processing.

    After the main upload pass, automatically retries failed mailboxes
    up to batch_retry_rounds times to handle transient failures.
    """
    # Cap workers at 5 for Railway stability
    num_workers = min(num_workers, 5)
    logger.info(f"Starting Instantly upload for batch {batch_id} with {num_workers} workers")

    # Initialize API client if key provided
    api = None
    if instantly_api_key:
        api = InstantlyAPI(instantly_api_key)
        if await asyncio.to_thread(api.test_connection):
            logger.info("Instantly API connected - verification enabled")
            existing = await asyncio.to_thread(api.load_all_accounts)
            logger.info(f"Loaded {len(existing)} existing accounts from Instantly")
        else:
            logger.warning("Instantly API test failed - proceeding without verification")
            api = None

    # Fetch mailboxes from database
    async with async_session_factory() as session:
        batch_result = await session.execute(
            select(SetupBatch).where(SetupBatch.id == batch_id)
        )
        batch = batch_result.scalar_one_or_none()
        if not batch:
            return {"error": "Batch not found", "total": 0, "uploaded": 0, "failed": 0, "skipped": 0}

        query = (
            select(Mailbox)
            .join(Tenant, Mailbox.tenant_id == Tenant.id)
            .join(
                Domain,
                and_(
                    Domain.batch_id == Tenant.batch_id,
                    func.lower(Domain.name)
                    == func.lower(func.split_part(Mailbox.email, "@", 2)),
                ),
            )
            .where(
                Tenant.batch_id == batch_id,
                Mailbox.setup_complete == True,
                Domain.step5_complete == True,
                Domain.domain_verified_in_m365 == True,
                Domain.dkim_enabled == True,
                Domain.dmarc_configured == True,
            )
        )

        if skip_uploaded:
            query = query.where(Mailbox.instantly_uploaded == False)

        # Move previously failed/problem mailboxes to the end so clean pending
        # accounts keep flowing before slow retries are revisited.
        query = query.order_by(Mailbox.instantly_upload_error.isnot(None), Mailbox.email)

        result = await session.execute(query)
        mailboxes = result.scalars().all()

        if not mailboxes:
            logger.info(f"No mailboxes to upload for batch {batch_id}")
            return {"total": 0, "uploaded": 0, "failed": 0, "skipped": 0, "errors": []}

        mailbox_list = [
            {
                "id": str(mb.id),
                "email": mb.email,
                "password": mb.initial_password or mb.password or "#Sendemails1",
                "tenant_id": str(mb.tenant_id),
            }
            for mb in mailboxes
        ]

        # Pre-filter against Instantly API cache
        if api and api._cache_loaded:
            existing_emails = api._known_emails
            if existing_emails:
                already_uploaded = [
                    mb for mb in mailbox_list
                    if mb["email"].strip().lower() in existing_emails
                ]
                if already_uploaded:
                    logger.info(
                        f"Pre-filter: {len(already_uploaded)} accounts already in Instantly (API cache)"
                    )
                    already_uploaded_ids = [mb["id"] for mb in already_uploaded]
                    now = datetime.utcnow()
                    await session.execute(
                        update(Mailbox)
                        .where(Mailbox.id.in_(already_uploaded_ids))
                        .values(
                            instantly_uploaded=True,
                            instantly_uploaded_at=now,
                            instantly_upload_error=None,
                            uploaded_to_sequencer=True,
                            uploaded_at=now,
                            sequencer_name="instantly",
                            upload_error=None,
                        )
                    )
                    await session.commit()
                    logger.info(
                        f"Pre-filter: marked {len(already_uploaded_ids)} existing accounts uploaded in DB"
                    )

                    mailbox_list = [
                        mb for mb in mailbox_list
                        if mb["email"].strip().lower() not in existing_emails
                    ]

    logger.info(f"Found {len(mailbox_list)} mailboxes to upload")

    # ---------- helper: single upload pass ----------
    async def _run_upload_pass(
        mb_list: List[Dict[str, Any]],
        pass_label: str = "main",
    ) -> Dict[str, Any]:
        uploaded = 0
        failed = 0
        errs: List[str] = []
        failed_mbs: List[Dict[str, Any]] = []

        def setup_worker(worker_id: int, attempts: int = 3) -> Optional[InstantlyUploader]:
            for attempt in range(1, attempts + 1):
                upl = InstantlyUploader(
                    instantly_email=instantly_email,
                    instantly_password=instantly_password,
                    api=api,
                    worker_id=worker_id,
                )
                if upl.setup_driver() and upl.login_to_instantly():
                    return upl

                logger.error(
                    f"[{pass_label}] Worker {worker_id} setup/login attempt "
                    f"{attempt}/{attempts} failed"
                )
                upl.cleanup()
                if attempt < attempts:
                    time.sleep(5 * attempt)

            logger.error(f"[{pass_label}] Worker {worker_id} failed to start after {attempts} attempts")
            return None

        setup_results = []
        for i in range(num_workers):
            setup_results.append(await asyncio.to_thread(setup_worker, i))
            await asyncio.sleep(1)
        uploaders = [upl for upl in setup_results if upl is not None]

        # Keep spare threads available so watchdog replacements can continue even
        # if ChromeDriver leaves a timed-out Selenium call blocked.
        executor_workers = max((len(uploaders) or 1) * 2, (len(uploaders) or 1) + 2)
        executor = ThreadPoolExecutor(max_workers=executor_workers)
        try:
            if not uploaders:
                return {
                    "uploaded": 0,
                    "failed": 0,
                    "errors": ["All workers failed to login"],
                    "failed_mailboxes": [],
                    "fatal": True,
                }

            try:
                pending = list(mb_list)
                active_tenants: set[str] = set()
                active: Dict[Any, Dict[str, Any]] = {}

                def tenant_key(mb_data: Dict[str, Any]) -> str:
                    return str(mb_data.get("tenant_id") or mb_data["email"].split("@")[-1]).lower()

                def pop_next_mailbox() -> Optional[Dict[str, Any]]:
                    if not pending:
                        return None
                    for idx, candidate in enumerate(pending):
                        if tenant_key(candidate) not in active_tenants:
                            return pending.pop(idx)
                    return pending.pop(0)

                def submit_next(upl: InstantlyUploader) -> bool:
                    mb_data = pop_next_mailbox()
                    if not mb_data:
                        return False
                    key = tenant_key(mb_data)
                    active_tenants.add(key)
                    fut = executor.submit(process_mailbox_sync, upl, mb_data)
                    active[asyncio.wrap_future(fut)] = {
                        "uploader": upl,
                        "mailbox": mb_data,
                        "tenant_key": key,
                        "started_at": time.monotonic(),
                    }
                    return True

                async def replace_timed_out_worker(
                    fut: Any,
                    ctx: Dict[str, Any],
                    elapsed: float,
                ) -> None:
                    upl = ctx["uploader"]
                    mb_data = ctx["mailbox"]
                    key = ctx["tenant_key"]
                    active.pop(fut, None)
                    active_tenants.discard(key)
                    pending.append(mb_data)
                    upl.retired = True
                    fut.cancel()

                    logger.error(
                        f"[{pass_label}] Hard timeout after {elapsed:.0f}s for "
                        f"{mb_data['email']}; retiring worker {upl.worker_id} and re-queueing mailbox at end"
                    )

                    if upl in uploaders:
                        uploaders.remove(upl)
                    await asyncio.to_thread(upl.cleanup)

                    replacement = await asyncio.to_thread(setup_worker, upl.worker_id)
                    if replacement:
                        uploaders.append(replacement)
                        submit_next(replacement)
                    else:
                        logger.error(
                            f"[{pass_label}] Worker {upl.worker_id} replacement setup failed"
                        )

                for upl in uploaders:
                    submit_next(upl)

                while active:
                    done, _ = await asyncio.wait(
                        active.keys(),
                        timeout=5,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if not done:
                        now = time.monotonic()
                        timed_out = [
                            (fut, ctx, now - ctx["started_at"])
                            for fut, ctx in list(active.items())
                            if now - ctx["started_at"] >= MAILBOX_HARD_TIMEOUT_SECONDS
                        ]
                        for fut, ctx, elapsed in timed_out:
                            await replace_timed_out_worker(fut, ctx, elapsed)
                        continue

                    for fut in done:
                        ctx = active.pop(fut, None)
                        if not ctx:
                            continue
                        upl = ctx["uploader"]
                        mb_data = ctx["mailbox"]
                        key = ctx["tenant_key"]
                        active_tenants.discard(key)
                        try:
                            res = fut.result()
                        except Exception as exc:
                            res = {
                                "mailbox_id": mb_data["id"],
                                "success": False,
                                "error": f"Worker exception: {exc}",
                                "retries": 0,
                                "verified": False,
                                "worker_dead": _is_browser_infra_error(exc),
                            }

                        if res.get("worker_dead"):
                            logger.error(
                                f"[{pass_label}] Retiring worker {upl.worker_id}; "
                                f"re-queueing {mb_data['email']} at end after browser restart failure"
                            )
                            pending.append(mb_data)
                            upl.retired = True
                            if upl in uploaders:
                                uploaders.remove(upl)
                            await asyncio.to_thread(upl.cleanup)
                            replacement = await asyncio.to_thread(setup_worker, upl.worker_id)
                            if replacement:
                                uploaders.append(replacement)
                                submit_next(replacement)
                            continue

                        async with async_session_factory() as session:
                            if res["success"]:
                                await session.execute(
                                    update(Mailbox)
                                    .where(Mailbox.id == res["mailbox_id"])
                                    .values(
                                        instantly_uploaded=True,
                                        instantly_uploaded_at=datetime.utcnow(),
                                        instantly_upload_error=None,
                                        uploaded_to_sequencer=True,
                                        uploaded_at=datetime.utcnow(),
                                        sequencer_name="instantly",
                                        upload_error=None,
                                    )
                                )
                                uploaded += 1
                            else:
                                await session.execute(
                                    update(Mailbox)
                                    .where(Mailbox.id == res["mailbox_id"])
                                    .values(
                                        instantly_uploaded=False,
                                        instantly_upload_error=res["error"],
                                        uploaded_to_sequencer=False,
                                        upload_error=res["error"],
                                    )
                                )
                                failed += 1
                                errs.append(res["error"])
                                failed_mbs.append(mb_data)

                            await session.commit()

                        logger.info(
                            f"[{pass_label}] Progress: {uploaded + failed}/{len(mb_list)} processed"
                        )

                        submit_next(upl)

                if pending:
                    error = "No active Instantly upload workers available"
                    logger.error(
                        f"[{pass_label}] Upload pass stopped with {len(pending)} pending "
                        f"mailboxes: {error}"
                    )
                    errs.append(error)
                    return {
                        "uploaded": uploaded,
                        "failed": failed,
                        "errors": errs,
                        "failed_mailboxes": failed_mbs,
                        "fatal": True,
                    }
            finally:
                await asyncio.gather(
                    *(asyncio.to_thread(upl.cleanup) for upl in uploaders),
                    return_exceptions=True,
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return {
            "uploaded": uploaded,
            "failed": failed,
            "errors": errs,
            "failed_mailboxes": failed_mbs,
        }

    # ---------- Main upload pass ----------
    logger.info(f"=== Main upload pass: {len(mailbox_list)} mailboxes ===")
    pass_result = await _run_upload_pass(mailbox_list, pass_label="main")
    if pass_result.get("fatal"):
        raise RuntimeError("; ".join(pass_result.get("errors") or ["Instantly upload workers unavailable"]))

    total_uploaded = pass_result["uploaded"]
    total_failed = pass_result["failed"]
    all_errors = list(pass_result["errors"])
    remaining_failed = list(pass_result["failed_mailboxes"])

    # ---------- Batch retry rounds ----------
    for retry_round in range(1, batch_retry_rounds + 1):
        if not remaining_failed:
            break

        logger.info(
            f"=== Retry round {retry_round}/{batch_retry_rounds}: "
            f"{len(remaining_failed)} failed mailboxes ==="
        )

        await asyncio.sleep(5)

        async with async_session_factory() as session:
            for mb_data in remaining_failed:
                await session.execute(
                    update(Mailbox)
                    .where(Mailbox.id == mb_data["id"])
                    .values(
                        instantly_uploaded=False,
                        instantly_upload_error=None,
                        upload_error=None,
                    )
                )
            await session.commit()

        retry_result = await _run_upload_pass(
            remaining_failed, pass_label=f"retry-{retry_round}"
        )
        if retry_result.get("fatal"):
            raise RuntimeError(
                "; ".join(retry_result.get("errors") or ["Instantly upload workers unavailable"])
            )

        total_uploaded += retry_result["uploaded"]
        total_failed = retry_result["failed"]
        remaining_failed = list(retry_result["failed_mailboxes"])
        all_errors = list(retry_result["errors"])

        if retry_result["failed"] == 0:
            logger.info(f"All failures resolved in retry round {retry_round}!")
            break

    logger.info(
        f"Instantly upload complete: {total_uploaded} uploaded, {total_failed} failed "
        f"(after {batch_retry_rounds} retry round(s))"
    )

    return {
        "total": len(mailbox_list),
        "uploaded": total_uploaded,
        "failed": total_failed,
        "skipped": 0,
        "errors": all_errors[:10],
    }
