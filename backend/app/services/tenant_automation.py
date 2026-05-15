"""
Parallel Tenant Automation

100% automated M365 first-login:
- Password change
- MFA enrollment (TOTP extraction)

Security Defaults remain ENABLED - we use OAuth/Graph API for email operations
instead of basic SMTP auth. TOTP secrets are stored to handle MFA when needed.

10 workers = 300 tenants in ~45 minutes
"""

import asyncio
import time
import re
import io
import logging
import secrets
import string
import traceback
import uuid
import tempfile
import shutil
from typing import Optional, Dict, Any, List
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import threading

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import pyotp
from PIL import Image
from uuid import UUID

try:
    from pyzbar.pyzbar import decode as decode_qr
    HAS_PYZBAR = True
except (ImportError, FileNotFoundError, OSError):
    HAS_PYZBAR = False
    decode_qr = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Standard password we change all tenants to
STANDARD_NEW_PASSWORD = "#Sendemails1"


# === LOGIN STATE ENUM ===
from enum import Enum as PyEnum

class LoginState(PyEnum):
    """States for resumable login flow."""
    UNKNOWN = "unknown"
    NEEDS_EMAIL = "needs_email"
    NEEDS_PASSWORD = "needs_password"
    WRONG_PASSWORD = "wrong_password"
    NEEDS_PASSWORD_CHANGE = "needs_password_change"
    NEEDS_MFA_SETUP = "needs_mfa_setup"
    NEEDS_MFA_METHOD_SELECT = "needs_mfa_method_select"
    NEEDS_MFA_CODE = "needs_mfa_code"
    NEEDS_STAY_SIGNED_IN = "needs_stay_signed_in"
    LOGGED_IN = "logged_in"
    ACCOUNT_LOCKED = "account_locked"
    ERROR = "error"


# DEBUG FLAGS - Set these to debug login issues
DEBUG_MODE = False         # Enable verbose logging and screenshots
DEBUG_MAX_TENANTS = 0      # Set to 0 to process ALL tenants (was 1 for debugging)
DEBUG_HEADLESS = True      # Set to False to SEE the browser (True for production)
SCREENSHOT_DIR = "/tmp/screenshots"

# MFA timing constants
DEFAULT_WAIT = 10  # seconds for element waits
LONG_WAIT = 20     # seconds for slow operations
CANT_SCAN_WAIT = 5  # seconds to wait after clicking "Can't scan" for secret to appear

MFA_ENROLLMENT_KEYWORDS = [
    "action required",
    "authenticator",
    "protect your account",
    "more information required",
    "keep your account secure",
    "let's keep your account secure",
    "lets keep your account secure",
    "another way to verify",
    "verify it's you",
    "verify it is you",
    "help you set up another way",
    "scan the qr code",
    "set up your account",
]

import os
import psycopg2
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

_progress = {"completed": 0, "failed": 0, "total": 0}
_progress_lock = threading.Lock()


# === BUG FIX 4.2: GUARANTEED TOTP save using raw psycopg2 ===
def _save_totp_guaranteed(tenant_id: str, totp_secret: str, new_password: str = None, worker_id: int = 0) -> bool:
    """
    GUARANTEED TOTP save using raw psycopg2.
    
    Uses a brand new connection — completely bypasses SQLAlchemy.
    Cannot be affected by stale sessions, event loop issues, or Neon timeouts.
    This is the CRITICAL save that prevents tenant lockout after MFA enrollment.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    # Normalize to plain postgresql:// for psycopg2
    for prefix in ["postgresql+asyncpg://", "postgresql+psycopg2://", "postgres://"]:
        if db_url.startswith(prefix):
            db_url = "postgresql://" + db_url[len(prefix):]
            break
    
    conn = None
    try:
        conn = psycopg2.connect(db_url, sslmode="require", connect_timeout=15)
        conn.autocommit = False
        cur = conn.cursor()
        
        if new_password:
            cur.execute(
                """UPDATE tenants 
                   SET totp_secret = %s, admin_password = %s, password_changed = true, updated_at = NOW() 
                   WHERE id = %s::uuid""",
                (totp_secret, new_password, tenant_id)
            )
        else:
            cur.execute(
                """UPDATE tenants SET totp_secret = %s, updated_at = NOW() WHERE id = %s::uuid""",
                (totp_secret, tenant_id)
            )
        
        conn.commit()
        
        # VERIFY the save
        cur.execute("SELECT totp_secret FROM tenants WHERE id = %s::uuid", (tenant_id,))
        row = cur.fetchone()
        
        cur.close()
        conn.close()
        
        if row and row[0] == totp_secret:
            logger.info(f"[W{worker_id}] ✅ GUARANTEED TOTP SAVE VERIFIED: {totp_secret[:4]}***")
            return True
        else:
            logger.error(f"[W{worker_id}] ❌ TOTP save verification FAILED")
            return False
            
    except Exception as e:
        logger.error(f"[W{worker_id}] ❌ GUARANTEED TOTP save exception: {e}")
        try:
            if conn:
                conn.close()
        except:
            pass
        return False


# === CRITICAL: Save TOTP immediately (sync helper for thread workers) ===
def _save_totp_immediately_sync(
    tenant_id: str,
    totp_secret: str,
    new_password: Optional[str],
    worker_id: int = 0,
) -> bool:
    """
    Save TOTP (and new password if present) immediately from a sync thread.
    Uses a synchronous session to avoid event-loop issues in Selenium threads.
    """
    try:
        from app.db.session import SyncSessionLocal
        from app.models.tenant import Tenant

        with SyncSessionLocal() as session:
            tenant = session.get(Tenant, UUID(tenant_id))
            if not tenant:
                logger.error(f"[W{worker_id}] ❌ Tenant not found for immediate TOTP save")
                return False

            tenant.totp_secret = totp_secret
            if new_password:
                tenant.admin_password = new_password
                tenant.password_changed = True

            session.commit()
            logger.info(f"[W{worker_id}] ✅ SAVED {tenant.name} to DB (sync)")
            return True
    except Exception as e:
        logger.error(f"[W{worker_id}] ❌ Immediate TOTP save failed: {e}")
        return False


# === CRITICAL: Save TOTP immediately helper ===
async def _save_totp_immediately(db, tenant_id: str, totp_secret: str, worker_id: int = 0) -> bool:
    """
    CRITICAL: Save TOTP to database IMMEDIATELY and verify it was saved.
    This MUST succeed before we complete MFA enrollment to prevent lockouts.
    """
    from sqlalchemy import text
    
    try:
        logger.info(
            f"[W{worker_id}] 💾 Saving TOTP to DB: {totp_secret[:4]}*** for tenant={tenant_id}"
        )
        # Save the TOTP
        await db.execute(
            text("""
                UPDATE tenants 
                SET totp_secret = :secret, 
                    updated_at = NOW()
                WHERE id = :tenant_id::uuid
            """),
            {"secret": totp_secret, "tenant_id": tenant_id}
        )
        await db.commit()
        logger.info(f"[W{worker_id}] ✅ TOTP COMMIT SUCCESSFUL")
        
        # VERIFY it was actually saved
        result = await db.execute(
            text("SELECT totp_secret FROM tenants WHERE id = :tenant_id::uuid"),
            {"tenant_id": tenant_id}
        )
        row = result.fetchone()
        
        if row and row[0] == totp_secret:
            logger.info(f"[W{worker_id}] ✅ TOTP SAVED AND VERIFIED IN DATABASE")
            return True
        else:
            logger.error(f"[W{worker_id}] ❌ TOTP SAVE VERIFICATION FAILED!")
            return False
            
    except Exception as e:
        logger.error(f"[W{worker_id}] ❌ CRITICAL: Failed to save TOTP to database: {e}")
        return False


@dataclass
class TenantResult:
    tenant_id: str
    admin_email: str
    success: bool = False
    new_password: str = None
    totp_secret: str = None
    password_changed: bool = False
    security_defaults_disabled: bool = False
    error: str = None


class BrowserWorker:
    """Single browser for tenant automation."""
    
    def __init__(self, worker_id: int, headless: bool = True):
        self.worker_id = worker_id
        self.headless = headless
        self.driver = None
        self.tenant_id = None  # Set during process() for screenshot naming
    
    def _screenshot(self, step_name: str) -> str:
        """Take screenshot and return path. Always captures current state."""
        if not self.driver:
            return ""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{self.tenant_id}_{step_name}.png"
            path = os.path.join(SCREENSHOT_DIR, filename)
            self.driver.save_screenshot(path)
            logger.info(f"[W{self.worker_id}] 📸 Screenshot: {path}")
            return path
        except Exception as e:
            logger.error(f"[W{self.worker_id}] Screenshot failed: {e}")
            return ""
    
    def _create_driver(self):
        opts = Options()
        if self.headless:
            # Use new headless flag for better compatibility
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        
        # Stability options
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-background-networking")
        opts.add_argument("--disable-default-apps")
        opts.add_argument("--disable-sync")
        opts.add_argument("--disable-translate")
        opts.add_argument("--no-first-run")                    # Skip first-run wizards
        
        # Reduce logging noise
        opts.add_argument("--log-level=3")
        opts.add_experimental_option("excludeSwitches", ["enable-logging"])
        
        profile_dir = tempfile.mkdtemp(prefix=f"chrome-profile-{self.worker_id}-{uuid.uuid4()}-")
        opts.add_argument(f"--user-data-dir={profile_dir}")
        opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
        # Enable performance logging for CDP (Chrome DevTools Protocol)
        # This captures all network requests including Authorization headers
        opts.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
        
        driver = webdriver.Chrome(options=opts)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        driver._profile_dir = profile_dir
        return driver
    
    def _find(self, by, value, timeout=15):
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )
    
    def _click(self, xpath, timeout=8):
        """Click element by xpath with logging."""
        if DEBUG_MODE:
            logger.info(f"[W{self.worker_id}] 🖱️ Attempting click: {xpath[:60]}...")
        try:
            elem = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            time.sleep(0.2)
            elem.click()
            if DEBUG_MODE:
                logger.info(f"[W{self.worker_id}] ✓ Click successful")
            return True
        except Exception as e:
            if DEBUG_MODE:
                logger.warning(f"[W{self.worker_id}] ✗ Click failed for {xpath[:40]}: {type(e).__name__}")
            return False
    
    def _click_if_exists(self, selectors: list, timeout: int = 3) -> bool:
        """Try multiple selectors until one works. Returns True if any clicked."""
        for by, value in selectors:
            try:
                elem = WebDriverWait(self.driver, timeout).until(
                    EC.element_to_be_clickable((by, value))
                )
                time.sleep(0.2)
                elem.click()
                if DEBUG_MODE:
                    logger.info(f"[W{self.worker_id}] ✓ Clicked: {by}={value[:40] if len(value) > 40 else value}")
                return True
            except Exception:
                continue
        return False
    
    def _find_element(self, selectors: list, timeout: int = 5):
        """Find element using multiple selectors. Returns element or None."""
        for by, value in selectors:
            try:
                elem = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((by, value))
                )
                if DEBUG_MODE:
                    logger.info(f"[W{self.worker_id}] ✓ Found: {by}={value[:40] if len(value) > 40 else value}")
                return elem
            except Exception:
                continue
        return None
    
    def _click_next_button(self, timeout: int = 5) -> bool:
        """Click the Next button with comprehensive selectors."""
        return self._click_if_exists([
            # "Action Required" page - MUST BE FIRST!
            (By.ID, "idSubmit_ProofUp_Redirect"),
            
            # New Microsoft UI (reskin) - data-testid
            (By.CSS_SELECTOR, "button[data-testid='reskin-step-next-button']"),
            
            # Text-based (most reliable based on screenshots)
            (By.XPATH, "//button[normalize-space()='Next']"),
            (By.XPATH, "//button[text()='Next']"),
            (By.XPATH, "//button[contains(text(), 'Next')]"),
            
            # Common Microsoft button IDs
            (By.ID, "idSIButton9"),
            
            # Input submit buttons
            (By.CSS_SELECTOR, "input[type='submit'][value='Next']"),
            (By.XPATH, "//input[@value='Next']"),
            
            # Generic submit
            (By.CSS_SELECTOR, "button[type='submit']"),
        ], timeout=timeout)
    
    def _click_cant_scan(self, timeout: int = 3) -> bool:
        """Click the 'Can't scan?' button (NOTE: It's a <button>, not a link!)."""
        return self._click_if_exists([
            # BUTTON selectors - Microsoft uses a button styled as a link!
            (By.CSS_SELECTOR, "button[data-testid='activation-qr-show/hide-info-button']"),
            (By.CSS_SELECTOR, "button.ms-Link"),
            (By.XPATH, "//button[contains(text(), \"Can't scan\")]"),
            (By.XPATH, "//button[contains(text(), 'scan the QR')]"),
            (By.XPATH, "//button[contains(., 'scan')]"),
            (By.XPATH, "//button[contains(., 'QR')]"),
            
            # Fallback: link selectors (in case MS changes the implementation)
            (By.PARTIAL_LINK_TEXT, "Can't scan"),
            (By.PARTIAL_LINK_TEXT, "scan"),
            (By.XPATH, "//a[contains(.,'scan')]"),
            (By.XPATH, "//a[contains(.,'manual')]"),
            
            # Microsoft specific IDs
            (By.ID, "switchToManual"),
            (By.ID, "signInAnotherWay"),
        ], timeout=timeout)
    
    def _validate_totp_secret(self, secret: str) -> bool:
        """Validate a TOTP secret by trying to generate a code."""
        try:
            if not re.match(r'^[A-Z2-7]+$', secret):
                return False
            code = pyotp.TOTP(secret).now()
            if len(code) == 6 and code.isdigit():
                logger.info(f"[W{self.worker_id}] ✓ TOTP secret validated (generates code: {code})")
                return True
        except Exception as e:
            logger.debug(f"[W{self.worker_id}] TOTP validation failed: {e}")
        return False
    
    def _extract_totp_from_visible_page(self) -> Optional[str]:
        """
        Extract TOTP secret from the page WITHOUT clicking anything.
        The "Can't scan" button should have already been clicked.
        """
        logger.info(f"[W{self.worker_id}] Extracting TOTP from visible page...")
        
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            page_text = body.text
            
            logger.info(f"[W{self.worker_id}] Page text ({len(page_text)} chars): {page_text[:500]}...")
            
            # Pattern 1: "Secret key: XXXXX" format
            secret_key_match = re.search(r'[Ss]ecret\s*[Kk]ey[:\s]+([A-Za-z0-7]{16,32})', page_text)
            if secret_key_match:
                secret = secret_key_match.group(1).upper().replace(' ', '')
                logger.info(f"[W{self.worker_id}] Found via 'Secret key:' pattern")
                if self._validate_totp_secret(secret):
                    return secret
            
            # Pattern 2: Base32 strings (uppercase, 16-32 chars)
            base32_matches = re.findall(r'\b([A-Z2-7]{16,32})\b', page_text.upper())
            for match in base32_matches:
                if self._validate_totp_secret(match):
                    logger.info(f"[W{self.worker_id}] Found via base32 pattern")
                    return match
            
            # Pattern 3: Look in specific elements
            for selector in ["[class*='secret']", "[class*='key']", "code", "pre", "[data-testid*='secret']"]:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for elem in elements:
                        text = elem.text.strip().upper().replace(' ', '')
                        if 16 <= len(text) <= 32:
                            if self._validate_totp_secret(text):
                                logger.info(f"[W{self.worker_id}] Found in element: {selector}")
                                return text
                except Exception:
                    continue
            
            # Pattern 4: otpauth URL
            otpauth_match = re.search(r'otpauth://totp/[^?]+\?secret=([A-Za-z2-7]+)', page_text)
            if otpauth_match:
                secret = otpauth_match.group(1).upper()
                logger.info(f"[W{self.worker_id}] Found via otpauth URL")
                if self._validate_totp_secret(secret):
                    return secret
            
            logger.error(f"[W{self.worker_id}] ❌ No valid TOTP secret found on page!")
            return None
            
        except Exception as e:
            logger.error(f"[W{self.worker_id}] Error extracting TOTP: {e}")
            return None
    
    def _find_and_click_cant_scan_link(self) -> bool:
        """
        Find and click the "Can't scan the QR code?" element.
        
        NOTE: Despite looking like a link, this is actually a <button> element!
        Uses multiple fallback strategies and click methods.
        """
        worker_id = getattr(self, 'worker_id', 0)
        
        # Log what elements we can see for debugging
        try:
            buttons = self.driver.find_elements(By.TAG_NAME, "button")
            logger.info(f"[W{worker_id}] Found {len(buttons)} buttons on page:")
            for btn in buttons[:10]:
                btn_text = btn.text.strip() if btn.text else "(empty)"
                btn_testid = btn.get_attribute("data-testid") or "(no testid)"
                logger.info(f"[W{worker_id}]   Button: '{btn_text[:40]}' testid='{btn_testid}'")
        except Exception as e:
            logger.debug(f"[W{worker_id}] Could not list buttons: {e}")
        
        # Primary selector - the data-testid is most reliable
        selectors = [
            # BUTTON selectors (Microsoft uses a button styled as a link)
            (By.CSS_SELECTOR, "button[data-testid='activation-qr-show/hide-info-button']"),
            (By.CSS_SELECTOR, "button.ms-Link"),
            (By.XPATH, "//button[contains(text(), \"Can't scan\")]"),
            (By.XPATH, "//button[contains(text(), 'scan the QR')]"),
            (By.XPATH, "//button[contains(., 'scan')]"),
            
            # Fallback: link selectors (in case MS changes the implementation)
            (By.XPATH, "//a[contains(text(), 'scan')]"),
            (By.PARTIAL_LINK_TEXT, "scan"),
        ]
        
        element = None
        matched_selector = None
        
        for by, value in selectors:
            try:
                element = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((by, value))
                )
                matched_selector = f"{by}={value}"
                logger.info(f"[W{worker_id}] Found 'Can't scan' element via: {matched_selector}")
                break
            except Exception:
                continue
        
        if not element:
            logger.error(f"[W{worker_id}] ✗ Could not find 'Can't scan' button with any selector!")
            return False
        
        # IMPORTANT: This is a TOGGLE button - click ONLY ONCE!
        # Clicking twice will HIDE the secret key again.
        
        # Try standard click first
        try:
            element.click()
            logger.info(f"[W{worker_id}] ✓ Clicked 'Can't scan' button (standard click)")
            return True
        except Exception as e:
            logger.warning(f"[W{worker_id}] Standard click failed: {e}, trying JS click...")
        
        # Fallback: JavaScript click (only if standard failed)
        try:
            self.driver.execute_script("arguments[0].scrollIntoView(true);", element)
            time.sleep(0.3)
            self.driver.execute_script("arguments[0].click();", element)
            logger.info(f"[W{worker_id}] ✓ Clicked 'Can't scan' button (JS click)")
            return True
        except Exception as e:
            logger.error(f"[W{worker_id}] ✗ Failed to click 'Can't scan' button: {e}")
            return False
    
    def _wait_for_page_stable(self, timeout: int = 5):
        """Wait for page to stop loading/changing."""
        worker_id = getattr(self, 'worker_id', 0)
        
        try:
            # Wait for jQuery/AJAX to complete (if present)
            self.driver.execute_script("""
                return (typeof jQuery === 'undefined') || (jQuery.active === 0);
            """)
        except Exception:
            pass
        
        # Wait for document ready state
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass
        
        # Small additional wait for dynamic content
        time.sleep(1)
    
    def _wait_for_page_load(self, timeout: int = 30):
        """
        STEP 4 FIX: Wait for page to fully load with document.readyState == "complete".
        This should be called after every driver.get() or navigation to ensure the page
        is fully loaded before interacting with elements.
        """
        worker_id = getattr(self, 'worker_id', 0)
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            logger.info(f"[W{worker_id}] ✓ Page fully loaded (readyState=complete)")
            time.sleep(1)  # Small buffer for any final rendering
            return True
        except Exception as e:
            logger.warning(f"[W{worker_id}] Page load timeout after {timeout}s: {e}")
            return False
    
    def _wait_for_page_content(self, min_length: int = 50, timeout: int = 10) -> str:
        """
        Wait for page to have actual content (not empty).
        Returns the page text (lowercase) once loaded.
        
        This fixes timing issues where page checks happen before content loads!
        """
        worker_id = getattr(self, 'worker_id', 0)
        page_text = ""
        
        for i in range(timeout):
            time.sleep(1)
            try:
                page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception as e:
                logger.warning(f"[W{worker_id}] Could not get page text: {e}")
                continue
            
            if len(page_text) > min_length:
                logger.info(f"[W{worker_id}] ✓ Page loaded after {i+1}s, text length: {len(page_text)}")
                return page_text
            logger.info(f"[W{worker_id}] Waiting for page... text length: {len(page_text)}")
        
        logger.warning(f"[W{worker_id}] ⚠️ Page still empty/short after {timeout}s! Length: {len(page_text)}")
        return page_text

    def _wait_for_password_change_complete(self, timeout: int = LONG_WAIT) -> bool:
        """
        Wait for the password change page to transition.

        Success when either:
        - URL no longer contains 'update' or 'password'
        - MFA setup page keywords are present
        """
        worker_id = getattr(self, 'worker_id', 0)

        def password_change_complete(driver) -> bool:
            url = driver.current_url.lower()
            url_moved_on = "update" not in url and "password" not in url

            try:
                page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                page_text = ""

            has_mfa_keywords = any(keyword in page_text for keyword in MFA_ENROLLMENT_KEYWORDS)
            return url_moved_on or has_mfa_keywords

        try:
            WebDriverWait(self.driver, timeout, poll_frequency=1).until(password_change_complete)
            url = self.driver.current_url.lower()
            try:
                page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                page_text = ""

            url_moved_on = "update" not in url and "password" not in url
            matched_keywords = [keyword for keyword in MFA_ENROLLMENT_KEYWORDS if keyword in page_text]
            logger.info(
                f"[W{worker_id}] ✓ Password change transition detected: "
                f"url_moved_on={url_moved_on}, keywords={matched_keywords}"
            )
            return True
        except Exception as e:
            logger.warning(f"[W{worker_id}] Password change transition not detected within {timeout}s: {e}")
            return False

    def _combined_page_text_lower(self) -> str:
        """Return current URL, visible body text, and source as one lowercase string."""
        chunks = []
        try:
            chunks.append(self.driver.current_url or "")
        except Exception:
            pass
        try:
            chunks.append(self.driver.find_element(By.TAG_NAME, "body").text or "")
        except Exception:
            pass
        try:
            chunks.append(self.driver.page_source or "")
        except Exception:
            pass
        return "\n".join(chunks).lower()

    def _find_mfa_enrollment_keywords(self) -> List[str]:
        page = self._combined_page_text_lower()
        return [keyword for keyword in MFA_ENROLLMENT_KEYWORDS if keyword in page]

    def _wait_for_mfa_enrollment_decision(self, timeout: int = 45) -> List[str]:
        """
        Wait until post-login prompts settle enough to decide whether MFA enrollment is required.

        Microsoft can show "Stay signed in?" first, then redirect to "Let's keep your
        account secure". Step 5 must not mark the tenant complete until that second page
        has had a chance to appear.
        """
        worker_id = getattr(self, 'worker_id', 0)
        last_state = LoginState.UNKNOWN

        for second in range(timeout):
            keywords = self._find_mfa_enrollment_keywords()
            if keywords:
                logger.info(f"[W{worker_id}] MFA enrollment keywords detected after {second}s: {keywords}")
                return keywords

            state = self._detect_login_state()
            last_state = state

            if state == LoginState.NEEDS_MFA_SETUP:
                logger.info(f"[W{worker_id}] Login state indicates MFA enrollment after {second}s")
                return ["login_state_needs_mfa_setup"]

            if state == LoginState.NEEDS_STAY_SIGNED_IN:
                logger.info(f"[W{worker_id}] Stay signed-in prompt appeared before MFA decision; dismissing it")
                self._handle_stay_signed_in()
                time.sleep(2)
                continue

            if state == LoginState.NEEDS_PASSWORD_CHANGE:
                logger.info(f"[W{worker_id}] Still on password-change page while checking MFA")
                return []

            if state == LoginState.LOGGED_IN and second in (5, 15, 30):
                logger.info(
                    f"[W{worker_id}] Logged in after {second}s; still watching for delayed MFA enrollment redirect"
                )

            time.sleep(1)

        keywords = self._find_mfa_enrollment_keywords()
        if keywords:
            logger.info(f"[W{worker_id}] MFA enrollment keywords detected after final wait: {keywords}")
            return keywords

        logger.info(f"[W{worker_id}] No MFA enrollment prompt after {timeout}s (last_state={last_state.value})")
        return []

    def _submit_existing_mfa_code(self, totp_secret: str) -> bool:
        """Submit a standard Microsoft 6-digit MFA code using an already stored TOTP secret."""
        worker_id = getattr(self, 'worker_id', 0)
        if not totp_secret:
            logger.error(f"[W{worker_id}] MFA code requested but no stored TOTP secret is available")
            return False

        try:
            code = pyotp.TOTP(totp_secret.upper()).now()
            logger.info(f"[W{worker_id}] Existing MFA challenge detected; entering TOTP code: {code[:2]}****")

            code_input = self._find_element([
                (By.CSS_SELECTOR, "input[name='otc']"),
                (By.CSS_SELECTOR, "input[type='tel']"),
                (By.CSS_SELECTOR, "input[maxlength='6']"),
                (By.ID, "idTxtBx_SAOTCC_OTC"),
                (By.XPATH, "//input[@type='tel']"),
                (By.XPATH, "//input[@maxlength='6']"),
            ], timeout=10)

            if not code_input:
                logger.error(f"[W{worker_id}] Could not find MFA code input for stored TOTP challenge")
                self._screenshot("mfa_existing_code_input_missing")
                return False

            code_input.clear()
            code_input.send_keys(code)
            self._screenshot("mfa_existing_code_entered")
            time.sleep(1)

            clicked = self._click_if_exists([
                (By.XPATH, "//button[contains(text(),'Verify')]"),
                (By.ID, "idSubmit_SAOTCC_Continue"),
                (By.ID, "idSIButton9"),
                (By.XPATH, "//input[@type='submit']"),
            ], timeout=5)

            if not clicked:
                code_input.send_keys(Keys.RETURN)

            time.sleep(4)
            self._screenshot("mfa_existing_code_submitted")
            logger.info(f"[W{worker_id}] Submitted existing TOTP MFA code")
            return True
        except Exception as e:
            logger.error(f"[W{worker_id}] Failed to submit stored TOTP MFA code: {e}")
            return False
    
    def _extract_totp_with_retry(self, max_attempts: int = 3) -> Optional[str]:
        """
        Try to extract TOTP secret with retries.
        
        This handles cases where the secret takes time to appear
        or the page hasn't fully loaded.
        """
        worker_id = getattr(self, 'worker_id', 0)
        
        for attempt in range(max_attempts):
            logger.info(f"[W{worker_id}] TOTP extraction attempt {attempt + 1}/{max_attempts}")
            
            # Wait progressively longer each attempt
            time.sleep(2 + attempt * 2)
            
            self._screenshot(f"totp_extraction_attempt_{attempt + 1}")
            
            # Try to extract from page
            secret = self._extract_totp_secret_from_page()
            if secret:
                logger.info(f"[W{worker_id}] ✓ TOTP extracted on attempt {attempt + 1}: {secret[:4]}...")
                return secret
            
            # If failed, try scrolling to trigger lazy-loaded content
            if attempt < max_attempts - 1:
                logger.info(f"[W{worker_id}] Attempt {attempt + 1} failed, scrolling page...")
                try:
                    self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(1)
                    self.driver.execute_script("window.scrollTo(0, 0);")
                except Exception:
                    pass
        
        logger.error(f"[W{worker_id}] ✗ TOTP extraction failed after {max_attempts} attempts!")
        return None
    
    def _debug_page_elements(self):
        """Log all interactive elements on page for debugging."""
        worker_id = getattr(self, 'worker_id', 0)
        logger.info(f"[W{worker_id}] ========== DEBUG: Page Elements ==========")
        logger.info(f"[W{worker_id}] URL: {self.driver.current_url}")
        
        # Log buttons
        try:
            buttons = self.driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons[:10]:  # Limit to 10
                logger.info(f"[W{worker_id}] BUTTON: id='{btn.get_attribute('id')}' text='{btn.text[:50] if btn.text else ''}'")
        except:
            pass
        
        # Log links
        try:
            links = self.driver.find_elements(By.TAG_NAME, "a")
            for link in links[:10]:
                logger.info(f"[W{worker_id}] LINK: text='{link.text[:50] if link.text else ''}' href='{link.get_attribute('href')}'")
        except:
            pass
        
        # Log inputs
        try:
            inputs = self.driver.find_elements(By.TAG_NAME, "input")
            for inp in inputs[:10]:
                logger.info(f"[W{worker_id}] INPUT: id='{inp.get_attribute('id')}' type='{inp.get_attribute('type')}' name='{inp.get_attribute('name')}'")
        except:
            pass
        
        # Log page text snippets that might contain the secret
        try:
            body = self.driver.find_element(By.TAG_NAME, "body").text
            if "secret" in body.lower():
                # Find the line with secret
                for line in body.split('\n'):
                    if "secret" in line.lower():
                        logger.info(f"[W{worker_id}] SECRET LINE: {line}")
        except:
            pass
        
        logger.info(f"[W{worker_id}] ========== END DEBUG ==========")
    
    def _debug_page_links(self):
        """Log all links on page for debugging."""
        worker_id = getattr(self, 'worker_id', 0)
        logger.info(f"[W{worker_id}] ========== DEBUG: Links on Page ==========")
        
        try:
            links = self.driver.find_elements(By.TAG_NAME, "a")
            for link in links:
                text = link.text.strip() if link.text else "(no text)"
                href = link.get_attribute("href") or "(no href)"
                logger.info(f"[W{worker_id}] LINK: '{text}' → {href[:50]}")
        except Exception as e:
            logger.error(f"[W{worker_id}] Error getting links: {e}")
        
        logger.info(f"[W{worker_id}] ==========================================")
    
    def _type(self, elem, text):
        elem.clear()
        elem.send_keys(text)
    
    def _detect_login_state(self) -> LoginState:
        """Detect current login page state including error states."""
        worker_id = getattr(self, 'worker_id', 0)
        
        try:
            url = self.driver.current_url.lower()
            page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            page_source = self.driver.page_source.lower()
        except:
            return LoginState.UNKNOWN
        
        # Check for error states FIRST
        if "your account or password is incorrect" in page_text:
            logger.info(f"[W{worker_id}] [STATE] Wrong password detected")
            return LoginState.WRONG_PASSWORD
        
        if "password is incorrect" in page_text or "wrong password" in page_text:
            logger.info(f"[W{worker_id}] [STATE] Wrong password detected")
            return LoginState.WRONG_PASSWORD
        
        if "account has been locked" in page_text or "account is locked" in page_text:
            logger.info(f"[W{worker_id}] [STATE] Account locked!")
            return LoginState.ACCOUNT_LOCKED
        
        if "something went wrong" in page_text:
            return LoginState.ERROR
        
        # Check for logged in states
        if "portal.azure.com" in url and ("#home" in url or "#blade" in url or "dashboard" in url):
            return LoginState.LOGGED_IN
        if "myapplications" in url or "myapps.microsoft.com" in url:
            return LoginState.LOGGED_IN
        if "office.com" in url and "landing" in url:
            return LoginState.LOGGED_IN
        
        # Stay signed in prompt
        if "kmsi" in url or "stay signed in" in page_text:
            return LoginState.NEEDS_STAY_SIGNED_IN
        
        # Password change required
        if "update your password" in page_text or "change your password" in page_text:
            return LoginState.NEEDS_PASSWORD_CHANGE
        if "passwordchange" in url:
            return LoginState.NEEDS_PASSWORD_CHANGE
        
        # MFA / Security Defaults
        if "action required" in page_text or "more information required" in page_text:
            return LoginState.NEEDS_MFA_SETUP
        if (
            "protect your account" in page_text
            or "keep your account secure" in page_text
            or "another way to verify" in page_text
            or "verify it's you" in page_text
        ):
            return LoginState.NEEDS_MFA_SETUP
        if "sspr" in url:
            return LoginState.NEEDS_MFA_SETUP
        if "scan the qr code" in page_text:
            return LoginState.NEEDS_MFA_SETUP
        if "install microsoft authenticator" in page_text:
            return LoginState.NEEDS_MFA_SETUP
        if "set up your account" in page_text and "authenticator" in page_text:
            return LoginState.NEEDS_MFA_SETUP
        
        # MFA code entry
        code_input = self._find_element([
            (By.CSS_SELECTOR, "input[name='otc']"),
            (By.CSS_SELECTOR, "input[type='tel'][maxlength='6']"),
        ], timeout=1)
        if code_input:
            return LoginState.NEEDS_MFA_CODE
        
        # Email entry
        email_input = self._find_element([
            (By.NAME, "loginfmt"),
            (By.ID, "i0116"),
        ], timeout=1)
        if email_input:
            return LoginState.NEEDS_EMAIL
        
        # Password entry
        pwd_input = self._find_element([
            (By.NAME, "passwd"),
            (By.ID, "i0118"),
        ], timeout=1)
        if pwd_input:
            return LoginState.NEEDS_PASSWORD
        
        return LoginState.UNKNOWN
    
    # Keep old method as alias for backwards compatibility
    def _detect_state(self) -> LoginState:
        """Alias for _detect_login_state for backwards compatibility."""
        return self._detect_login_state()
    
    def _handle_wrong_password_recovery(self):
        """Recover from wrong password to try again."""
        worker_id = getattr(self, 'worker_id', 0)
        
        # Try clicking various "try again" or "back" options
        clicked = self._click_if_exists([
            (By.ID, "idBtn_Back"),
            (By.XPATH, "//*[contains(text(), 'Use a different account')]"),
            (By.XPATH, "//*[contains(text(), 'Back')]"),
            (By.XPATH, "//*[contains(text(), 'Try again')]"),
            (By.XPATH, "//button[contains(text(), 'Sign in with a different account')]"),
        ], timeout=3)
        
        if clicked:
            logger.info(f"[W{worker_id}] Clicked back/recovery button")
            time.sleep(2)
        else:
            # Fallback: refresh the page and start over
            logger.info(f"[W{worker_id}] No back button, refreshing page")
            self.driver.get("https://portal.azure.com")
            self._wait_for_page_load()  # STEP 4 FIX: Wait for page to fully load
            time.sleep(3)
    
    def _enter_email(self, email: str) -> bool:
        """Enter email and submit."""
        worker_id = getattr(self, 'worker_id', 0)
        
        email_input = self._find_element([
            (By.NAME, "loginfmt"),
            (By.ID, "i0116"),
        ], timeout=10)
        
        if not email_input:
            logger.error(f"[W{worker_id}] Email input not found!")
            return False
        
        email_input.clear()
        email_input.send_keys(email)
        logger.info(f"[W{worker_id}] Entered email: {email}")
        time.sleep(0.5)
        email_input.send_keys(Keys.RETURN)
        time.sleep(2)
        return True
    
    def _enter_password(self, password: str) -> bool:
        """Enter password and submit."""
        worker_id = getattr(self, 'worker_id', 0)
        
        pwd_input = self._find_element([
            (By.NAME, "passwd"),
            (By.ID, "i0118"),
            (By.CSS_SELECTOR, "input[type='password']"),
        ], timeout=10)
        
        if not pwd_input:
            logger.error(f"[W{worker_id}] Password input not found!")
            return False
        
        pwd_input.clear()
        pwd_input.send_keys(password)
        logger.info(f"[W{worker_id}] Entered password: {'*' * len(password)}")
        self._screenshot("password_entered")
        time.sleep(0.5)
        pwd_input.send_keys(Keys.RETURN)
        return True
    
    def _debug_buttons_on_page(self):
        """Log all buttons for debugging."""
        worker_id = getattr(self, 'worker_id', 0)
        logger.info(f"[W{worker_id}] === DEBUG: Buttons on page ===")
        try:
            buttons = self.driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                text = btn.text.strip() if btn.text else "(no text)"
                testid = btn.get_attribute("data-testid") or "(no testid)"
                logger.info(f"[W{worker_id}] BUTTON: text='{text}' data-testid='{testid}'")
        except Exception as e:
            logger.error(f"[W{worker_id}] Error getting buttons: {e}")
    
    def _handle_email_entry(self, email: str) -> bool:
        """Enter email and submit."""
        logger.info(f"[W{self.worker_id}] Entering email: {email}")
        
        email_input = self._find_element([
            (By.NAME, "loginfmt"),
            (By.ID, "i0116"),
            (By.CSS_SELECTOR, "input[type='email']"),
        ], timeout=10)
        
        if not email_input:
            logger.error(f"[W{self.worker_id}] Email input not found!")
            return False
        
        email_input.clear()
        email_input.send_keys(email)
        self._screenshot("email_entered")
        time.sleep(0.5)
        email_input.send_keys(Keys.RETURN)
        time.sleep(3)
        self._screenshot("after_email_submit")
        return True
    
    async def _smart_login_async(self, tenant, db) -> bool:
        """
        Attempt login with intelligent password fallback.
        
        Order of passwords tried:
        1. admin_password from DB (could be initial or already changed)
        2. Standard password: #Sendemails1
        3. initial_password from DB (if different from admin_password)
        """
        worker_id = getattr(self, 'worker_id', 0)
        
        # Build list of passwords to try
        passwords_to_try = []
        
        # First, try what's in admin_password (most likely to be current)
        passwords_to_try.append(("db_current", tenant.admin_password))
        
        # Second, try our standard password (in case it was changed previously)
        if tenant.admin_password != STANDARD_NEW_PASSWORD:
            passwords_to_try.append(("standard", STANDARD_NEW_PASSWORD))
        
        # Third, try initial password if we have it and it's different
        if hasattr(tenant, 'initial_password') and tenant.initial_password:
            if tenant.initial_password not in [tenant.admin_password, STANDARD_NEW_PASSWORD]:
                passwords_to_try.append(("initial", tenant.initial_password))
        
        logger.info(f"[W{worker_id}] Will try {len(passwords_to_try)} passwords")
        
        for attempt, (pwd_type, password) in enumerate(passwords_to_try):
            logger.info(f"[W{worker_id}] Login attempt {attempt + 1}: trying {pwd_type} password")
            
            # Check current state
            state = self._detect_login_state()
            
            # If we're not on password page, we might need to enter email first
            if state == LoginState.NEEDS_EMAIL:
                if not self._enter_email(tenant.admin_email):
                    logger.error(f"[W{worker_id}] Failed to enter email")
                    return False
                time.sleep(2)
                state = self._detect_login_state()
            
            # Now we should be on password page
            if state == LoginState.NEEDS_PASSWORD:
                if self._enter_password(password):
                    time.sleep(3)
                    self._screenshot(f"after_password_{pwd_type}")
                    
                    # Check result
                    state = self._detect_login_state()
                    
                    if state == LoginState.WRONG_PASSWORD:
                        logger.warning(f"[W{worker_id}] ✗ {pwd_type} password rejected")
                        
                        # Need to go back and try another password
                        self._handle_wrong_password_recovery()
                        continue
                    
                    elif state == LoginState.ACCOUNT_LOCKED:
                        logger.error(f"[W{worker_id}] ✗ Account is locked!")
                        return False
                    
                    elif state in [LoginState.NEEDS_PASSWORD_CHANGE, LoginState.NEEDS_MFA_SETUP, 
                                  LoginState.LOGGED_IN, LoginState.NEEDS_STAY_SIGNED_IN]:
                        logger.info(f"[W{worker_id}] ✓ {pwd_type} password accepted! State: {state}")
                        
                        # Update DB if we used a different password than what was stored
                        if password != tenant.admin_password:
                            logger.info(f"[W{worker_id}] Updating stored password to working password")
                            tenant.admin_password = password
                            if pwd_type == "standard":
                                tenant.password_changed = True
                            await db.commit()
                        
                        return True
                    
                    else:
                        logger.info(f"[W{worker_id}] Password submitted, state: {state}")
                        return True  # Might be OK, let main loop handle it
            
            elif state in [LoginState.NEEDS_PASSWORD_CHANGE, LoginState.NEEDS_MFA_SETUP, LoginState.LOGGED_IN]:
                # Already past password - good!
                logger.info(f"[W{worker_id}] Already past password stage, state: {state}")
                return True
        
        logger.error(f"[W{worker_id}] ✗ All passwords failed!")
        return False

    def _smart_login(self, admin_email: str, admin_password: str, initial_password: Optional[str], password_changed: bool) -> bool:
        """
        Try to login with known passwords in order of likelihood.
        Returns True if login succeeds.
        
        NOTE: This is the synchronous version for backwards compatibility.
        For new code, use _smart_login_async with tenant object.
        """
        worker_id = getattr(self, 'worker_id', 0)
        
        # Build list of passwords to try
        passwords_to_try = []
        
        # First, try what's in admin_password (most likely to be current)
        passwords_to_try.append(("db_current", admin_password))
        
        # Second, try our standard password (in case it was changed previously)
        if admin_password != STANDARD_NEW_PASSWORD:
            passwords_to_try.append(("standard", STANDARD_NEW_PASSWORD))
        
        # Third, try initial password if we have it and it's different
        if initial_password and initial_password not in [admin_password, STANDARD_NEW_PASSWORD]:
            passwords_to_try.append(("initial", initial_password))
        
        logger.info(f"[W{worker_id}] Will try {len(passwords_to_try)} passwords")
        
        for attempt, (pwd_type, password) in enumerate(passwords_to_try):
            logger.info(f"[W{worker_id}] Login attempt {attempt + 1}: trying {pwd_type} password")
            
            # Check current state
            state = self._detect_login_state()
            
            # If we're not on password page, we might need to enter email first
            if state == LoginState.NEEDS_EMAIL:
                if not self._handle_email_entry(admin_email):
                    logger.error(f"[W{worker_id}] Failed to enter email")
                    return False
                time.sleep(2)
                state = self._detect_login_state()
            
            # Now we should be on password page
            if state == LoginState.NEEDS_PASSWORD:
                if self._enter_password(password):
                    time.sleep(3)
                    self._screenshot(f"after_password_{pwd_type}")
                    
                    # Check result
                    state = self._detect_login_state()
                    
                    if state == LoginState.WRONG_PASSWORD:
                        logger.warning(f"[W{worker_id}] ✗ {pwd_type} password rejected")
                        
                        # Need to go back and try another password
                        self._handle_wrong_password_recovery()
                        continue
                    
                    elif state == LoginState.ACCOUNT_LOCKED:
                        logger.error(f"[W{worker_id}] ✗ Account is locked!")
                        return False
                    
                    elif state in [LoginState.NEEDS_PASSWORD_CHANGE, LoginState.NEEDS_MFA_SETUP, 
                                  LoginState.LOGGED_IN, LoginState.NEEDS_STAY_SIGNED_IN]:
                        logger.info(f"[W{worker_id}] ✓ {pwd_type} password accepted! State: {state}")
                        return True
                    
                    else:
                        logger.info(f"[W{worker_id}] Password submitted, state: {state}")
                        return True  # Might be OK, let main loop handle it
            
            elif state in [LoginState.NEEDS_PASSWORD_CHANGE, LoginState.NEEDS_MFA_SETUP, LoginState.LOGGED_IN]:
                # Already past password - good!
                logger.info(f"[W{worker_id}] Already past password stage, state: {state}")
                return True
        
        logger.error(f"[W{worker_id}] ✗ All passwords failed!")
        return False
    
    def _generate_secure_password(self) -> str:
        """Generate a secure password that meets Microsoft requirements."""
        # Microsoft requires: 8+ chars, uppercase, lowercase, number, special char
        length = 16
        
        # Ensure at least one of each required type
        password = [
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
            secrets.choice("!@#$%^&*"),
        ]
        
        # Fill the rest randomly
        all_chars = string.ascii_letters + string.digits + "!@#$%^&*"
        password += [secrets.choice(all_chars) for _ in range(length - 4)]
        
        # Shuffle
        secrets.SystemRandom().shuffle(password)
        return ''.join(password)
    
    def _extract_totp(self) -> Optional[str]:
        """Extract TOTP secret from MFA page - try text first, then QR code.
        
        NOTE: This method assumes "Can't scan" was already clicked in Step 4.
        Do NOT click it again - it's a TOGGLE button and clicking twice hides the secret!
        """
        logger.info(f"[W{self.worker_id}] 🔑 Attempting to extract TOTP secret...")
        self._screenshot("mfa_totp_extraction_start")
        
        # NOTE: "Can't scan" is a TOGGLE button - Step 4 already clicked it to SHOW the secret.
        # Clicking again would HIDE it! So we just wait for the content to be visible.
        time.sleep(2)  # Wait for secret to be visible
        
        self._screenshot("mfa_extraction_page_state")
        page = self.driver.page_source
        
        # Method 1: Look for secret in otpauth URL
        logger.info(f"[W{self.worker_id}] Looking for TOTP in otpauth URL...")
        match = re.search(r'secret=([A-Z2-7]{16,64})', page, re.IGNORECASE)
        if match:
            secret = match.group(1).upper()
            logger.info(f"[W{self.worker_id}] ✓ Found TOTP in otpauth URL: {secret[:4]}...")
            return secret
        
        # Method 2: Look for base32 secret displayed with spaces (Microsoft format: ABC1 DEF2 GHI3 ...)
        logger.info(f"[W{self.worker_id}] Looking for base32 secret with spaces...")
        matches = re.findall(r'[A-Z2-7]{4}(?:\s+[A-Z2-7]{4}){3,}', page)
        for m in matches:
            secret = m.replace(' ', '')
            try:
                pyotp.TOTP(secret).now()  # Validate it works
                logger.info(f"[W{self.worker_id}] ✓ Found secret with spaces: {secret[:4]}...")
                return secret
            except Exception:
                continue
        
        # Method 3: Look for plain base32 strings
        logger.info(f"[W{self.worker_id}] Looking for plain base32 secret on page...")
        for m in re.findall(r'\b([A-Z2-7]{16,64})\b', page):
            try:
                pyotp.TOTP(m).now()
                logger.info(f"[W{self.worker_id}] ✓ Found valid base32 secret: {m[:4]}...")
                return m
            except Exception as e:
                logger.debug(f"[W{self.worker_id}] Invalid base32 candidate: {m[:8]}... ({e})")
                continue
        
        # Method 4: QR code decoding if pyzbar available
        if HAS_PYZBAR:
            logger.info(f"[W{self.worker_id}] Attempting QR code decode...")
            try:
                # Find QR code images
                qr_elements = self.driver.find_elements(By.TAG_NAME, "img")
                for img_elem in qr_elements:
                    try:
                        src = img_elem.get_attribute("src")
                        if src and ("qr" in src.lower() or "data:image" in src):
                            if src.startswith("data:image"):
                                # Base64 encoded image
                                import base64
                                data = src.split(",")[1]
                                image_data = base64.b64decode(data)
                                image = Image.open(io.BytesIO(image_data))
                            else:
                                # Take screenshot of element instead
                                screenshot = img_elem.screenshot_as_png
                                image = Image.open(io.BytesIO(screenshot))
                            
                            decoded = decode_qr(image)
                            for obj in decoded:
                                text = obj.data.decode()
                                if "otpauth" in text.lower():
                                    match = re.search(r'secret=([A-Z2-7]+)', text, re.IGNORECASE)
                                    if match:
                                        logger.info(f"[W{self.worker_id}] ✓ Extracted from QR code: {match.group(1)[:4]}...")
                                        return match.group(1).upper()
                    except Exception as e:
                        logger.debug(f"[W{self.worker_id}] QR decode attempt failed: {e}")
                        continue
                
                # Try canvas element
                try:
                    canvas = self.driver.find_element(By.XPATH, "//canvas")
                    img = Image.open(io.BytesIO(canvas.screenshot_as_png))
                    for obj in decode_qr(img):
                        m = re.search(r'secret=([A-Z2-7]+)', obj.data.decode(), re.IGNORECASE)
                        if m:
                            logger.info(f"[W{self.worker_id}] ✓ Found TOTP via canvas QR: {m.group(1)[:4]}...")
                            return m.group(1).upper()
                except Exception:
                    pass
                    
            except Exception as e:
                logger.warning(f"[W{self.worker_id}] QR decode error: {e}")
        else:
            logger.warning(f"[W{self.worker_id}] pyzbar not available for QR decode")
        
        logger.error(f"[W{self.worker_id}] ✗ Could not extract TOTP secret!")
        self._screenshot("mfa_totp_extraction_failed")
        return None
    
    def extract_totp_only(self) -> Optional[str]:
        """
        Phase 1: Navigate to secret page and extract TOTP secret.
        Does NOT complete enrollment (no code entry / verify / done clicks).
        """
        worker_id = getattr(self, 'worker_id', 0)
        try:
            logger.info(f"[W{worker_id}] 🔐 Starting MFA setup flow...")
            self._screenshot("mfa_01_start")
            
            # Give page time to fully load
            time.sleep(3)
        
            # ========== STEP 1: "Install Microsoft Authenticator" page ==========
            # Look for and click "Set up a different authentication app"
            logger.info(f"[W{worker_id}] [MFA Step 1] Looking for 'Set up a different authentication app'...")
            
            page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            
            if "install microsoft authenticator" in page_text or "get it on google play" in page_text:
                logger.info(f"[W{worker_id}] [MFA Step 1] On 'Install Microsoft Authenticator' page")
                
                # IT'S A BUTTON, NOT A LINK! Do NOT use LINK_TEXT or PARTIAL_LINK_TEXT!
                different_app_clicked = self._click_if_exists([
                    (By.XPATH, "//button[contains(text(), 'Set up a different authentication app')]"),
                    (By.XPATH, "//button[contains(text(), 'different authentication')]"),
                    (By.XPATH, "//button[contains(., 'different authentication')]"),
                    (By.CSS_SELECTOR, "button.ms-Link"),
                ], timeout=5)
                
                if different_app_clicked:
                    logger.info(f"[W{worker_id}] [MFA Step 1] ✓ Clicked 'Set up a different authentication app' BUTTON")
                    time.sleep(2)
                    self._screenshot("mfa_02_after_different_app")
                else:
                    logger.error(f"[W{worker_id}] [MFA Step 1] ✗ FAILED to click 'different authentication app' button!")
                    # Debug: list all buttons on page
                    self._debug_buttons_on_page()
            
            # ========== STEP 2: "Set up your account in app" page ==========
            # Click "Next" to proceed
            page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            
            if "set up your account" in page_text or "start by adding" in page_text:
                logger.info(f"[W{worker_id}] [MFA Step 2] On 'Set up your account in app' page")
                
                if self._click_next_button():
                    logger.info(f"[W{worker_id}] [MFA Step 2] ✓ Clicked Next")
                    time.sleep(2)
                    self._screenshot("mfa_03_after_setup_next")
            
            # ========== STEP 3: "Scan the QR code" page ==========
            # CRITICAL: Click "Can't scan the QR code?" FIRST, NOT Next!
            time.sleep(2)  # Wait for page to fully render
            self._wait_for_page_stable()
            page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            
            if "scan the qr code" in page_text or "scan the qr" in page_text:
                logger.info(f"[W{worker_id}] [MFA Step 3] On 'Scan the QR code' page")
                logger.info(f"[W{worker_id}] [MFA Step 3] ⚠️ MUST click 'Can't scan' NOT 'Next'!")
                
                self._screenshot("mfa_03_before_cant_scan")
                
                # Use robust method to find and click - NOTE: It's a BUTTON, not a link!
                cant_scan_clicked = self._find_and_click_cant_scan_link()
                
                if cant_scan_clicked:
                    logger.info(f"[W{worker_id}] [MFA Step 3] ✓ Clicked 'Can't scan the QR code?' button")
                    time.sleep(CANT_SCAN_WAIT)  # Wait for secret to appear
                    self._screenshot("mfa_04_after_cant_scan")
                else:
                    logger.error(f"[W{worker_id}] [MFA Step 3] ✗ Could not click 'Can't scan' button!")
                    self._screenshot("mfa_04_cant_scan_FAILED")
                    self._debug_page_elements()
                    
                    # DO NOT click Next here - that's the wrong path!
                    logger.error(f"[W{worker_id}] [MFA Step 3] Cannot proceed without clicking 'Can't scan'")
                    return None
            
            # ========== STEP 4: "Enter the following into Authenticator" page ==========
            # This page should show "Secret key:" with the TOTP secret
            logger.info(f"[W{worker_id}] [MFA Step 4] Looking for secret key...")
            self._screenshot("mfa_05_secret_key_page")
            
            page_text = self.driver.find_element(By.TAG_NAME, "body").text
            logger.info(f"[W{worker_id}] [MFA Step 4] Page text preview: {page_text[:500]}")
            
            # Check if we're on the right page (should have "Secret key")
            if "secret key" not in page_text.lower():
                logger.error(f"[W{worker_id}] [MFA Step 4] ✗ Not on secret key page!")
                logger.error(f"[W{worker_id}] [MFA Step 4] Page says: {page_text[:200]}")
                
                # Check if we accidentally went to the Code/URL page (wrong path)
                if "code:" in page_text.lower() and "url:" in page_text.lower():
                    logger.error(f"[W{worker_id}] [MFA Step 4] ✗ Wrong flow! Got Code/URL page instead of Secret Key page")

            # Extract the secret key
            totp_secret = self._extract_totp_secret_from_page()
            if not totp_secret:
                logger.error(f"[W{worker_id}] [MFA Step 4] ✗ Failed to extract TOTP secret!")
                return None

            logger.info(f"[W{worker_id}] ✓ Extracted TOTP secret: {totp_secret[:4]}...{totp_secret[-4:]}")
            return totp_secret.upper()
            return totp_secret.upper()
            
        except Exception as e:
            logger.error(f"[W{worker_id}] ❌ MFA SETUP CRASHED: {e}")
            logger.error(f"[W{worker_id}] Traceback: {traceback.format_exc()}")
            self._screenshot("mfa_CRASH")
            return None

    def complete_mfa_enrollment(self, totp_secret: str) -> bool:
        """
        Phase 2: Complete MFA enrollment by entering code and finishing steps.
        MUST ONLY be called after TOTP is saved to DB.
        """
        worker_id = getattr(self, 'worker_id', 0)

        # Click Next to go to code entry
        if self._click_next_button():
            logger.info(f"[W{worker_id}] [MFA Step 4] ✓ Clicked Next to proceed to code entry")
        time.sleep(2)
        self._screenshot("mfa_06_code_entry_page")

        # ========== STEP 5: Enter verification code ==========
        logger.info(f"[W{worker_id}] [MFA Step 5] Entering verification code...")

        totp = pyotp.TOTP(totp_secret.upper())
        code = totp.now()
        logger.info(f"[W{worker_id}] [MFA Step 5] Generated code: {code}")

        # Find code input field
        code_input = self._find_element([
            (By.CSS_SELECTOR, "input[name='otc']"),
            (By.CSS_SELECTOR, "input[type='tel']"),
            (By.CSS_SELECTOR, "input[maxlength='6']"),
            (By.CSS_SELECTOR, "input[aria-label*='code']"),
            (By.XPATH, "//input[@type='tel']"),
        ], timeout=10)

        if not code_input:
            logger.error(f"[W{worker_id}] [MFA Step 5] ✗ Code input field not found!")
            return False

        code_input.clear()
        code_input.send_keys(code)
        logger.info(f"[W{worker_id}] [MFA Step 5] ✓ Entered verification code")
        time.sleep(1)
        self._screenshot("mfa_07_code_entered")

        # Submit the code
        self._click_if_exists([
            (By.XPATH, "//button[contains(text(), 'Verify')]") ,
            (By.XPATH, "//button[contains(text(), 'Next')]") ,
            (By.ID, "idSubmit_SAOTCC_Continue"),
            (By.ID, "idSIButton9"),
        ], timeout=5) or code_input.send_keys(Keys.RETURN)

        time.sleep(3)
        self._screenshot("mfa_08_after_verify")

        # ========== STEP 6: Click through completion ==========
        logger.info(f"[W{worker_id}] [MFA Step 6] Completing MFA setup...")

        for i in range(5):
            clicked = self._click_if_exists([
                (By.XPATH, "//button[contains(text(), 'Done')]"),
                (By.XPATH, "//button[contains(text(), 'Finish')]"),
                (By.XPATH, "//button[contains(text(), 'OK')]"),
                (By.XPATH, "//button[contains(text(), 'Yes')]"),
                (By.ID, "idSIButton9"),
            ], timeout=2)

            if clicked:
                logger.info(f"[W{worker_id}] [MFA Step 6] Clicked completion button")
                time.sleep(2)

        self._screenshot("mfa_09_complete")
        logger.info(f"[W{worker_id}] ✓ MFA setup complete!")

        return True

    def _extract_totp_secret_from_page(self) -> Optional[str]:
        """
        Extract TOTP secret from the "Enter the following into Authenticator" page.
        
        The page shows:
        - Account name: (tenant info)
        - Secret key: rp6bmrvdmkrxswkp
        
        The secret is lowercase base32, typically 16-32 characters.
        """
        worker_id = getattr(self, 'worker_id', 0)
        
        page_source = self.driver.page_source
        page_text = self.driver.find_element(By.TAG_NAME, "body").text
        
        # Log the FULL page text for debugging
        logger.info(f"[W{worker_id}] ===== FULL PAGE TEXT FOR EXTRACTION =====")
        logger.info(f"[W{worker_id}] {page_text}")
        logger.info(f"[W{worker_id}] ==========================================")
        
        # Try multiple patterns - the format is "Secret key:\nnnkchnzhsnv6lqm5"
        patterns = [
            r'Secret key:?\s*\n?\s*([a-z2-7]{16,32})',  # With newline
            r'Secret key:?\s*([a-z2-7]{16,32})',         # Without newline
            r'([a-z2-7]{16,32})',                         # Just find any base32 string
        ]
        
        totp_secret = None
        for pattern in patterns:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                candidate = match.group(1)
                # Skip common words that match the pattern
                if candidate.lower() not in ['authenticator', 'international', 'administrator', 'organization']:
                    try:
                        pyotp.TOTP(candidate.upper()).now()
                        totp_secret = candidate.upper()
                        logger.info(f"[W{worker_id}] ✓ Found TOTP with pattern '{pattern}': {totp_secret}")
                        return totp_secret
                    except:
                        logger.debug(f"[W{worker_id}] Candidate '{candidate}' failed TOTP validation")
                        continue
        
        # Method 2: Extract from HTML source
        html_patterns = [
            r'Secret key:?\s*</?\w*>?\s*([a-z2-7]{16,32})',
            r'>([a-z2-7]{16,32})<',
            r'secret.*?([a-z2-7]{16,32})',
        ]
        
        for pattern in html_patterns:
            match = re.search(pattern, page_source, re.IGNORECASE)
            if match:
                secret = match.group(1)
                if secret.lower() not in ['authenticator', 'international', 'administrator', 'organization']:
                    try:
                        pyotp.TOTP(secret.upper()).now()
                        logger.info(f"[W{worker_id}] [TOTP] Found secret via HTML pattern: {pattern[:30]}...")
                        return secret.upper()
                    except:
                        continue
        
        # Method 3: Find any base32-looking string in page text
        all_matches = re.findall(r'\b([a-z2-7]{16,32})\b', page_text, re.IGNORECASE)
        for candidate in all_matches:
            # Skip common words
            if candidate.lower() in ['authenticator', 'international', 'administrator', 'organization']:
                continue
            try:
                pyotp.TOTP(candidate.upper()).now()
                logger.info(f"[W{worker_id}] [TOTP] Found secret via base32 scan: {candidate.upper()}")
                return candidate.upper()
            except:
                continue
        
        logger.error(f"[W{worker_id}] [TOTP] ✗ No TOTP found in page text!")
        logger.error(f"[W{worker_id}] [TOTP] Page text was: {page_text[:500]}")
        return None

    def process(self, tenant_id: str, admin_email: str, initial_pwd: str, new_pwd: str) -> TenantResult:
        """Process one tenant - fully automated."""
        result = TenantResult(tenant_id=tenant_id, admin_email=admin_email)
        self.tenant_id = tenant_id  # Set for screenshot naming
        existing_totp_secret = None
        
        # === BUG FIX 4.3: Check state before retry ===
        # Load fresh state from DB to see if this tenant is already done
        try:
            from app.db.session import SyncSessionLocal
            with SyncSessionLocal() as check_db:
                from app.models.tenant import Tenant as TenantModel
                existing = check_db.get(TenantModel, UUID(tenant_id))
                if existing:
                    existing_totp_secret = existing.totp_secret
                    result.totp_secret = existing_totp_secret
                    if existing.password_changed and existing.totp_secret:
                        logger.info(f"[W{self.worker_id}] ✅ Tenant already complete (pwd_changed + totp), SKIPPING")
                        return TenantResult(
                            tenant_id=tenant_id,
                            admin_email=admin_email,
                            success=True,
                            new_password=existing.admin_password,
                            totp_secret=existing.totp_secret,
                        )
                    elif existing.password_changed and not existing.totp_secret:
                        logger.info(f"[W{self.worker_id}] Password already changed, only MFA needed")
                        # Use the CHANGED password, not the initial one
                        initial_pwd = existing.admin_password
                        logger.info(f"[W{self.worker_id}] Using changed password for login")
        except Exception as e:
            logger.warning(f"[W{self.worker_id}] State check failed (proceeding anyway): {e}")
        
        # === PASSWORD VALIDATION AND FALLBACK ===
        passwords_to_try = []
        
        if initial_pwd:
            passwords_to_try.append(("initial", initial_pwd))
            logger.info(f"[W{self.worker_id}] Initial password provided: {initial_pwd[:3]}***")
        else:
            logger.warning(f"[W{self.worker_id}] ⚠️ NO initial password provided!")
        
        # Always include standard password as fallback (tenant may have been partially processed)
        if STANDARD_NEW_PASSWORD != initial_pwd:
            passwords_to_try.append(("standard", STANDARD_NEW_PASSWORD))
        
        if not passwords_to_try:
            logger.error(f"[W{self.worker_id}] ❌ No passwords to try for {admin_email}!")
            result.error = "No passwords available"
            return result
        
        logger.info(f"[W{self.worker_id}] Will try {len(passwords_to_try)} passwords: {[p[0] for p in passwords_to_try]}")
        
        try:
            logger.info(f"[W{self.worker_id}] ========== STARTING {tenant_id} ==========")
            logger.info(f"[W{self.worker_id}] Email: {admin_email}")
            logger.info(f"[W{self.worker_id}] Creating browser (headless={self.headless})...")
            
            self.driver = self._create_driver()
            logger.info(f"[W{self.worker_id}] ✓ Browser created")
            
            # LOGIN - STEP 1: Navigate to Azure Portal
            logger.info(f"[W{self.worker_id}] 🌐 Navigating to portal.azure.com...")
            self.driver.get("https://portal.azure.com")
            self._wait_for_page_load()  # STEP 4 FIX: Wait for page to fully load
            time.sleep(2)
            self._screenshot("01_azure_portal_loaded")
            logger.info(f"[W{self.worker_id}] Current URL: {self.driver.current_url}")
            
            # LOGIN - STEP 2: Enter email
            logger.info(f"[W{self.worker_id}] 📧 Looking for email input field...")
            email_input = self._find(By.NAME, "loginfmt")
            logger.info(f"[W{self.worker_id}] ✓ Found email input")
            logger.info(f"[W{self.worker_id}] Entering email: {admin_email}")
            self._type(email_input, admin_email)
            self._screenshot("02_email_entered")
            logger.info(f"[W{self.worker_id}] Submitting email...")
            email_input.send_keys(Keys.RETURN)
            time.sleep(2)
            self._screenshot("03_after_email_submit")
            logger.info(f"[W{self.worker_id}] Current URL: {self.driver.current_url}")
            
            # LOGIN - STEP 3: Enter password with fallback
            logger.info(f"[W{self.worker_id}] 🔐 Starting password entry with {len(passwords_to_try)} passwords to try...")
            
            password_accepted = False
            used_password = None
            used_password_type = None
            
            for pwd_idx, (pwd_type, password) in enumerate(passwords_to_try):
                logger.info(f"[W{self.worker_id}] 🔐 Attempt {pwd_idx + 1}/{len(passwords_to_try)}: trying {pwd_type} password")
                
                # Find password input
                pwd_input = self._find_element([
                    (By.NAME, "passwd"),
                    (By.ID, "i0118"),
                    (By.CSS_SELECTOR, "input[type='password']"),
                ], timeout=10)
                
                if not pwd_input:
                    logger.error(f"[W{self.worker_id}] ✗ Password input not found!")
                    raise Exception("Password input field not found")
                
                logger.info(f"[W{self.worker_id}] ✓ Found password input")
                pwd_input.clear()
                pwd_input.send_keys(password)
                self._screenshot(f"04_password_entered_{pwd_type}")
                logger.info(f"[W{self.worker_id}] Submitting {pwd_type} password...")
                pwd_input.send_keys(Keys.RETURN)
                time.sleep(3)
                self._screenshot(f"05_after_password_submit_{pwd_type}")
                logger.info(f"[W{self.worker_id}] Current URL: {self.driver.current_url}")
                
                # Check for wrong password error
                state = self._detect_login_state()
                logger.info(f"[W{self.worker_id}] State after {pwd_type} password: {state}")
                
                if state == LoginState.WRONG_PASSWORD:
                    logger.warning(f"[W{self.worker_id}] ✗ {pwd_type} password rejected!")
                    
                    # If we have more passwords to try, recover and continue
                    if pwd_idx < len(passwords_to_try) - 1:
                        logger.info(f"[W{self.worker_id}] Recovering to try next password...")
                        self._handle_wrong_password_recovery()
                        time.sleep(2)
                        
                        # After recovery, we need to re-enter email
                        new_state = self._detect_login_state()
                        if new_state == LoginState.NEEDS_EMAIL:
                            logger.info(f"[W{self.worker_id}] Re-entering email after recovery...")
                            if not self._enter_email(admin_email):
                                raise Exception("Failed to re-enter email after wrong password")
                            time.sleep(2)
                        continue
                    else:
                        raise Exception(f"All {len(passwords_to_try)} passwords failed!")
                
                elif state == LoginState.ACCOUNT_LOCKED:
                    raise Exception("Account is locked!")

                elif state == LoginState.NEEDS_MFA_CODE:
                    if not existing_totp_secret:
                        raise Exception("MFA code required, but no TOTP secret is stored for this tenant")

                    logger.info(f"[W{self.worker_id}] Existing MFA code challenge detected after password entry")
                    if not self._submit_existing_mfa_code(existing_totp_secret):
                        raise Exception("Failed to submit stored TOTP MFA code")

                    result.totp_secret = existing_totp_secret
                    time.sleep(2)
                    post_mfa_state = self._detect_login_state()
                    logger.info(f"[W{self.worker_id}] State after stored TOTP submit: {post_mfa_state}")

                    password_accepted = True
                    used_password = password
                    used_password_type = pwd_type
                    logger.info(f"[W{self.worker_id}] ✓ {pwd_type} password accepted after stored TOTP challenge")
                    break
                
                else:
                    # Password accepted! (Could be password change, MFA, logged in, etc.)
                    password_accepted = True
                    used_password = password
                    used_password_type = pwd_type
                    logger.info(f"[W{self.worker_id}] ✓ {pwd_type} password accepted! State: {state}")
                    break
            
            if not password_accepted:
                raise Exception("No password was accepted")
            
            logger.info(f"[W{self.worker_id}] ✓ Login successful with {used_password_type} password")
            
            # If we used the standard password, no need to change it
            if used_password_type == "standard":
                logger.info(f"[W{self.worker_id}] Already using standard password, password change handled")
            
            # PASSWORD CHANGE - Check if required
            logger.info(f"[W{self.worker_id}] 🔄 [PASSWORD CHANGE] Starting password change detection...")
            logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Current URL: {self.driver.current_url}")
            
            # === STATE DETECTION: Check what page we're actually on ===
            # This prevents "Password change failed" errors when tenant is on a different page
            page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            page = self.driver.page_source.lower()
            
            # First, determine actual page state before attempting password change
            if "more information required" in page_text or "action required" in page_text:
                logger.info(f"[W{self.worker_id}] [STATE DETECTION] Already past password change - on MFA page")
                result.new_password = used_password  # Password already accepted
                # Skip password change section - will be handled in MFA section below
            elif "stay signed in" in page_text or "kmsi" in self.driver.current_url.lower():
                logger.info(f"[W{self.worker_id}] [STATE DETECTION] Already logged in (stay signed in prompt)")
                result.new_password = used_password
                # Skip password change - already done
            elif (
                "protect your account" in page_text
                or "keep your account secure" in page_text
                or "another way to verify" in page_text
                or "verify it's you" in page_text
            ):
                logger.info(f"[W{self.worker_id}] [STATE DETECTION] On MFA setup page - skipping password change")
                result.new_password = used_password
                # Skip to MFA handling
            elif "scan the qr code" in page_text or "authenticator" in page_text:
                logger.info(f"[W{self.worker_id}] [STATE DETECTION] On QR code/authenticator page - skipping password change")
                result.new_password = used_password
                # Skip to MFA handling
            elif "portal.azure.com" in self.driver.current_url and "#" in self.driver.current_url:
                logger.info(f"[W{self.worker_id}] [STATE DETECTION] Already on Azure portal - login complete!")
                result.new_password = used_password
                # Skip password change - already logged in
            
            password_keywords = ['update your password', 'change password', 'new password', 'updatepassword', 'password reset', 'reset your password']
            found_keywords = [kw for kw in password_keywords if kw in page]
            
            # Only attempt password change if we're actually on the password change page
            if found_keywords and "update your password" in page_text:
                logger.info(f"[W{self.worker_id}] ✓ [PASSWORD CHANGE] DETECTED! Keywords found: {found_keywords}")
                self._screenshot("06_password_change_required")
                
                # Log all input fields on the page for debugging
                logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Scanning page for input fields...")
                try:
                    all_inputs = self.driver.find_elements(By.TAG_NAME, "input")
                    for inp in all_inputs:
                        inp_name = inp.get_attribute("name") or "(no name)"
                        inp_id = inp.get_attribute("id") or "(no id)"
                        inp_type = inp.get_attribute("type") or "(no type)"
                        logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Found input: name='{inp_name}', id='{inp_id}', type='{inp_type}'")
                except Exception as e:
                    logger.warning(f"[W{self.worker_id}] [PASSWORD CHANGE] Could not scan inputs: {e}")
                
                # Enter current password - try many variations
                current_pwd_found = False
                current_password_names = [
                    'currentPassword', 'oldPassword', 'CurrentPassword', 'OldPassword',
                    'passwd', 'password', 'current-password', 'old-password',
                    'currentpassword', 'oldpassword', 'existingPassword'
                ]
                current_password_ids = [
                    'currentPassword', 'oldPassword', 'i0118', 'current-password',
                    'currentpasswordInput', 'oldPasswordInput'
                ]
                
                logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Looking for current password field...")
                
                # Try by name first
                for name in current_password_names:
                    try:
                        elem = self.driver.find_element(By.NAME, name)
                        logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] ✓ Found current password field by NAME: {name}")
                        self._type(elem, initial_pwd)
                        current_pwd_found = True
                        self._screenshot("06a_current_pwd_entered")
                        break
                    except Exception as e:
                        logger.debug(f"[W{self.worker_id}] [PASSWORD CHANGE] Field name '{name}' not found")
                
                # Try by ID if name didn't work
                if not current_pwd_found:
                    for field_id in current_password_ids:
                        try:
                            elem = self.driver.find_element(By.ID, field_id)
                            logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] ✓ Found current password field by ID: {field_id}")
                            self._type(elem, initial_pwd)
                            current_pwd_found = True
                            self._screenshot("06a_current_pwd_entered")
                            break
                        except Exception as e:
                            logger.debug(f"[W{self.worker_id}] [PASSWORD CHANGE] Field id '{field_id}' not found")
                
                if not current_pwd_found:
                    logger.error(f"[W{self.worker_id}] ⚠️ [PASSWORD CHANGE] Could not find current password field!")
                    self._screenshot("06a_current_pwd_NOT_FOUND")
                    # Log page source snippet for debugging
                    logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Page source snippet (first 3000 chars):")
                    logger.info(self.driver.page_source[:3000])
                
                # Enter new password - try many variations
                new_pwd_found = False
                new_password_names = [
                    'newPassword', 'NewPassword', 'new-password', 'newpassword',
                    'password', 'newPwd', 'new_password'
                ]
                new_password_ids = [
                    'newPassword', 'NewPassword', 'newPasswordInput', 'new-password'
                ]
                
                logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Looking for new password field...")
                
                for name in new_password_names:
                    try:
                        elem = self.driver.find_element(By.NAME, name)
                        logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] ✓ Found new password field by NAME: {name}")
                        self._type(elem, new_pwd)
                        new_pwd_found = True
                        self._screenshot("06b_new_pwd_entered")
                        break
                    except Exception as e:
                        logger.debug(f"[W{self.worker_id}] [PASSWORD CHANGE] Field name '{name}' not found")
                
                if not new_pwd_found:
                    for field_id in new_password_ids:
                        try:
                            elem = self.driver.find_element(By.ID, field_id)
                            logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] ✓ Found new password field by ID: {field_id}")
                            self._type(elem, new_pwd)
                            new_pwd_found = True
                            self._screenshot("06b_new_pwd_entered")
                            break
                        except Exception as e:
                            logger.debug(f"[W{self.worker_id}] [PASSWORD CHANGE] Field id '{field_id}' not found")
                
                if not new_pwd_found:
                    logger.error(f"[W{self.worker_id}] ⚠️ [PASSWORD CHANGE] Could not find new password field!")
                    self._screenshot("06b_new_pwd_NOT_FOUND")
                
                # Confirm password - try many variations
                confirm_pwd_found = False
                confirm_password_names = [
                    'confirmPassword', 'reenterPassword', 'ConfirmPassword', 'ReenterPassword',
                    'confirm-password', 'confirmpassword', 'confirmNewPassword',
                    'password2', 'newPasswordConfirm', 'passwordConfirm'
                ]
                confirm_password_ids = [
                    'confirmPassword', 'reenterPassword', 'confirmPasswordInput',
                    'confirm-password', 'confirmNewPassword'
                ]
                
                logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Looking for confirm password field...")
                
                for name in confirm_password_names:
                    try:
                        elem = self.driver.find_element(By.NAME, name)
                        logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] ✓ Found confirm password field by NAME: {name}")
                        self._type(elem, new_pwd)
                        confirm_pwd_found = True
                        self._screenshot("07_password_fields_filled")
                        logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Submitting password change...")
                        elem.send_keys(Keys.RETURN)
                        break
                    except Exception as e:
                        logger.debug(f"[W{self.worker_id}] [PASSWORD CHANGE] Field name '{name}' not found")
                
                if not confirm_pwd_found:
                    for field_id in confirm_password_ids:
                        try:
                            elem = self.driver.find_element(By.ID, field_id)
                            logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] ✓ Found confirm password field by ID: {field_id}")
                            self._type(elem, new_pwd)
                            confirm_pwd_found = True
                            self._screenshot("07_password_fields_filled")
                            logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Submitting password change...")
                            elem.send_keys(Keys.RETURN)
                            break
                        except Exception as e:
                            logger.debug(f"[W{self.worker_id}] [PASSWORD CHANGE] Field id '{field_id}' not found")
                
                if not confirm_pwd_found:
                    logger.error(f"[W{self.worker_id}] ⚠️ [PASSWORD CHANGE] Could not find confirm password field!")
                    self._screenshot("06c_confirm_pwd_NOT_FOUND")
                    # Try clicking submit button instead
                    logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Trying to click submit button...")
                    submit_clicked = self._click("//button[@type='submit']", 3) or \
                                     self._click("//input[@type='submit']", 3) or \
                                     self._click("//button[contains(.,'Submit')]", 3) or \
                                     self._click("//button[contains(.,'Change')]", 3)
                    logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Submit button clicked: {submit_clicked}")
                
                # Longer explicit wait after password change submission
                logger.info(
                    f"[W{self.worker_id}] [PASSWORD CHANGE] Waiting for page transition (up to {LONG_WAIT}s)..."
                )
                self._wait_for_password_change_complete(timeout=LONG_WAIT)

                # Wait for actual page transition before checking result
                logger.info(
                    f"[W{self.worker_id}] [PASSWORD CHANGE] Password change submitted, waiting for page transition..."
                )

                try:
                    old_page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
                except Exception:
                    old_page_text = ""

                page_changed = False
                transition_indicators = [
                    "more information required",
                    "action required",
                    "stay signed in",
                    "keep me signed in",
                    "microsoft authenticator",
                ]

                for i in range(15):
                    time.sleep(1)
                    try:
                        new_page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
                    except Exception:
                        new_page_text = ""

                    if new_page_text != old_page_text and len(new_page_text) > 50:
                        logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Page changed after {i + 1} seconds")
                        page_changed = True
                        break

                    if any(indicator in new_page_text for indicator in transition_indicators):
                        logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Detected next page after {i + 1} seconds")
                        page_changed = True
                        break

                    logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Waiting for transition... ({i + 1}s)")

                if not page_changed:
                    self._screenshot("password_change_timeout")
                    logger.warning(
                        f"[W{self.worker_id}] [PASSWORD CHANGE] Page didn't change after 15s, checking anyway..."
                    )

                self._screenshot("08_after_password_change")
                logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] After submit URL: {self.driver.current_url}")

                # Verify password change succeeded
                try:
                    current_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
                except Exception:
                    current_text = self.driver.page_source.lower()

                if "update your password" in current_text or "change password" in current_text:
                    logger.error(
                        f"[W{self.worker_id}] ✗ [PASSWORD CHANGE] FAILED - still on password change page!"
                    )
                    self._screenshot("08a_password_change_FAILED")
                    # Log what's on the page for debugging
                    logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Page title: {self.driver.title}")
                    logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Current URL: {self.driver.current_url}")
                    raise Exception("Password change failed - still showing password change prompt")
                else:
                    logger.info(f"[W{self.worker_id}] ✓ [PASSWORD CHANGE] Password change appears successful!")
                    result.new_password = new_pwd
                    result.password_changed = True
                    logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Password was CHANGED, saving new password")
            else:
                logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] No password change required (keywords not found)")
                logger.info(f"[W{self.worker_id}] [PASSWORD CHANGE] Password was NOT changed, keeping original password")
                # Do NOT overwrite result.new_password - leave it as None so DB keeps original
                result.password_changed = False
            
            # MFA ENROLLMENT - Check if required
            logger.info(f"[W{self.worker_id}] 🔐 Checking if MFA enrollment is required...")
            found_mfa_keywords = self._wait_for_mfa_enrollment_decision(timeout=45)
            
            if found_mfa_keywords:
                logger.info(f"[W{self.worker_id}] ✓ MFA enrollment DETECTED! Keywords: {found_mfa_keywords}")
                self._screenshot("09_mfa_enrollment_required")
                
                # Debug: log all page elements before clicking Next
                if DEBUG_MODE:
                    self._debug_page_elements()
                
                # ===== MFA STEP 1: Action Required page =====
                logger.info(f"[W{self.worker_id}] ===== MFA STEP 1: Clicking Next on Action Required page =====")
                self._click_next_button(timeout=5)
                
                # CRITICAL: Wait for page to actually load before checking content!
                logger.info(f"[W{self.worker_id}] MFA STEP 1: Waiting for next page to load...")
                page_text_lower = self._wait_for_page_content(min_length=50, timeout=10)
                self._screenshot("mfa_step1_after_action_required")
                
                # ===== MFA STEP 2: Install Microsoft Authenticator page =====
                logger.info(f"[W{self.worker_id}] ===== MFA STEP 2: PAGE TEXT AFTER FIRST NEXT =====")
                logger.info(f"[W{self.worker_id}] {page_text_lower[:500]}")
                logger.info(f"[W{self.worker_id}] ==================================================")
                
                if "install microsoft authenticator" in page_text_lower or "get it on" in page_text_lower or "get the app" in page_text_lower:
                    logger.info(f"[W{self.worker_id}] ✓ MFA STEP 2: ON Install Authenticator page - clicking 'different app' BUTTON")
                    
                    # Debug: Find ALL buttons on page (it's a BUTTON, not a link!)
                    self._debug_buttons_on_page()
                    
                    # IT'S A BUTTON, NOT A LINK! Do NOT use LINK_TEXT or PARTIAL_LINK_TEXT!
                    diff_clicked = self._click_if_exists([
                        (By.XPATH, "//button[contains(text(), 'Set up a different authentication app')]"),
                        (By.XPATH, "//button[contains(text(), 'different authentication')]"),
                        (By.XPATH, "//button[contains(., 'different authentication')]"),
                        (By.CSS_SELECTOR, "button.ms-Link"),
                    ], timeout=5)
                    
                    if diff_clicked:
                        logger.info(f"[W{self.worker_id}] ✓ MFA STEP 2: Clicked 'Set up a different authentication app' BUTTON")
                    else:
                        logger.error(f"[W{self.worker_id}] ✗ MFA STEP 2: FAILED to click 'different app' button!")
                    
                    time.sleep(2)
                    self._screenshot("mfa_step2_after_different_app")
                else:
                    logger.info(f"[W{self.worker_id}] ✗ MFA STEP 2: NOT on Install Authenticator page")
                    logger.info(f"[W{self.worker_id}] Page contains: {page_text_lower[:200]}")
                
                # ===== MFA STEP 3: Set up your account page =====
                logger.info(f"[W{self.worker_id}] ===== MFA STEP 3: Clicking Next on 'Set up your account' page =====")
                self._click_next_button(timeout=5)
                
                # CRITICAL: Wait for page to actually load before checking content!
                logger.info(f"[W{self.worker_id}] MFA STEP 3: Waiting for QR code page to load...")
                page_text_lower = self._wait_for_page_content(min_length=50, timeout=10)
                self._screenshot("mfa_step3_after_setup")
                
                # ===== MFA STEP 4: Scan QR code page - click Can't scan =====
                logger.info(f"[W{self.worker_id}] ===== MFA STEP 4: On QR code page - clicking 'Can't scan' =====")
                logger.info(f"[W{self.worker_id}] MFA STEP 4 page text: {page_text_lower[:300]}")
                
                cant_scan_clicked = self._click_if_exists([
                    (By.CSS_SELECTOR, "button[data-testid='activation-qr-show/hide-info-button']"),
                    (By.CSS_SELECTOR, "button.ms-Link"),
                    (By.XPATH, "//button[contains(text(), \"Can't scan\")]"),
                    (By.XPATH, "//button[contains(., 'scan')]"),
                ], timeout=5)
                
                if cant_scan_clicked:
                    logger.info(f"[W{self.worker_id}] ✓ MFA STEP 4: Clicked 'Can't scan'")
                else:
                    logger.error(f"[W{self.worker_id}] ✗ MFA STEP 4: Could not click 'Can't scan'")
                
                time.sleep(3)  # CRITICAL: Wait for secret key page to load!
                self._screenshot("mfa_step4_after_cant_scan")
                
                # ===== MFA STEP 5: Extract secret key =====
                logger.info(f"[W{self.worker_id}] ===== MFA STEP 5: Extracting secret key =====")
                
                # Get and LOG the page text BEFORE extraction
                page_text = self.driver.find_element(By.TAG_NAME, "body").text
                logger.info(f"[W{self.worker_id}] ===== PAGE TEXT FOR TOTP EXTRACTION =====")
                logger.info(f"[W{self.worker_id}] {page_text}")
                logger.info(f"[W{self.worker_id}] ==========================================")
                self._screenshot("mfa_step5_secret_key_page")
                
                # Extract TOTP directly here - the secret is like "bgtcmw5mdwrnycjs" (lowercase, 16 chars)
                totp = None
                
                # Method 1: Look for "Secret key:" followed by the value on next line or same line
                match = re.search(r'Secret key:?\s*\n?\s*([a-z2-7]{16,32})', page_text, re.IGNORECASE)
                if match:
                    totp = match.group(1).upper()
                    logger.info(f"[W{self.worker_id}] ✓ Found TOTP via 'Secret key:' pattern: {totp}")
                
                # Method 2: Fallback - find any 16-char lowercase base32 string
                if not totp:
                    matches = re.findall(r'\b([a-z2-7]{16})\b', page_text, re.IGNORECASE)
                    logger.info(f"[W{self.worker_id}] Fallback search found candidates: {matches}")
                    for candidate in matches:
                        if candidate.lower() not in ['authenticator', 'administrator', 'international']:
                            try:
                                pyotp.TOTP(candidate.upper()).now()  # Validate it works
                                totp = candidate.upper()
                                logger.info(f"[W{self.worker_id}] ✓ Using fallback TOTP: {totp}")
                                break
                            except:
                                continue
                
                # Method 3: Try the old _extract_totp method as last resort
                if not totp:
                    logger.info(f"[W{self.worker_id}] Trying legacy _extract_totp_secret_from_page...")
                    totp = self._extract_totp_secret_from_page()
                if totp:
                    logger.info(f"[W{self.worker_id}] ✓ TOTP secret extracted successfully")
                    result.totp_secret = totp
                    # CRITICAL: Save immediately BEFORE completing MFA enrollment
                    # BUG FIX 4.2: Use guaranteed psycopg2 save instead of SQLAlchemy
                    saved = _save_totp_guaranteed(
                        tenant_id=tenant_id,
                        totp_secret=totp,
                        new_password=result.new_password,
                        worker_id=self.worker_id,
                    )
                    if not saved:
                        raise Exception("Failed to save TOTP to database before MFA enrollment")
                    code = pyotp.TOTP(totp).now()
                    logger.info(f"[W{self.worker_id}] Generated MFA code: {code}")
                    
                    # Click Next to go to code entry page
                    self._click_next_button()
                    time.sleep(2)
                    
                    # Enter the verification code using multiple selectors
                    logger.info(f"[W{self.worker_id}] Looking for MFA code input field...")
                    code_input = self._find_element([
                        (By.CSS_SELECTOR, "input[name='otc']"),
                        (By.CSS_SELECTOR, "input[type='tel']"),
                        (By.CSS_SELECTOR, "input[maxlength='6']"),
                        (By.ID, "idTxtBx_SAOTCC_OTC"),
                        (By.XPATH, "//input[@type='tel']"),
                        (By.XPATH, "//input[@maxlength='6']"),
                    ], timeout=5)
                    
                    if code_input:
                        logger.info(f"[W{self.worker_id}] ✓ Found MFA code input")
                        code_input.clear()
                        code_input.send_keys(code)
                        self._screenshot("12_mfa_code_entered")
                        time.sleep(1)
                        
                        # Click verify/next
                        verify_clicked = self._click_if_exists([
                            (By.XPATH, "//button[contains(text(),'Verify')]"),
                            (By.ID, "idSubmit_SAOTCC_Continue"),
                        ], timeout=3)
                        
                        if not verify_clicked:
                            self._click_next_button()
                        
                        logger.info(f"[W{self.worker_id}] ✓ Submitted verification code")
                    else:
                        logger.warning(f"[W{self.worker_id}] ⚠️ Could not find MFA code input!")
                        self._screenshot("12a_mfa_code_input_not_found")
                    
                    time.sleep(4)
                    self._screenshot("13_after_mfa_verify")
                    
                    # Click through completion screens (Done, Finish, OK, etc.)
                    logger.info(f"[W{self.worker_id}] Clicking through MFA completion prompts...")
                    for i in range(5):
                        self._click_if_exists([
                            (By.XPATH, "//button[contains(text(),'Done')]"),
                            (By.XPATH, "//button[contains(text(),'Finish')]"),
                            (By.XPATH, "//button[contains(text(),'OK')]"),
                            (By.ID, "idBtn_Back"),
                        ], timeout=2)
                        time.sleep(1)
                    
                    self._screenshot("14_mfa_complete")
                    logger.info(f"[W{self.worker_id}] ✓ MFA enrollment completed")
                else:
                    logger.error(f"[W{self.worker_id}] ✗ TOTP extraction failed!")
                    raise Exception("Could not extract TOTP secret during MFA setup")
            else:
                logger.info(f"[W{self.worker_id}] No MFA enrollment required (keywords not found)")
            
            # STAY SIGNED IN
            logger.info(f"[W{self.worker_id}] Checking for 'Stay signed in?' prompt...")
            self._screenshot("15_checking_stay_signed_in")
            stay_clicked = self._click("//button[contains(.,'No')]", 3)
            if stay_clicked:
                logger.info(f"[W{self.worker_id}] ✓ Clicked 'No' on stay signed in")
            time.sleep(1)
            
            # NOTE: Security Defaults remain ENABLED - we use OAuth/Graph API with MFA
            # The TOTP secret is stored so we can handle MFA when needed
            
            # FINAL STATUS - Mark tenant as complete
            self._screenshot("99_final_state")
            result.success = True
            result.security_defaults_disabled = False  # We keep Security Defaults ENABLED
            
            logger.info(f"[W{self.worker_id}] ========== COMPLETED {tenant_id} ==========")
            logger.info(f"[W{self.worker_id}] ✅ Password changed: {result.new_password is not None}")
            logger.info(f"[W{self.worker_id}] ✅ TOTP extracted: {bool(result.totp_secret)}")
            logger.info(f"[W{self.worker_id}] ✅ Security Defaults: ENABLED (using OAuth)")
            logger.info(f"[W{self.worker_id}] ✅ Ready for Step 5")
            
        except Exception as e:
            result.error = str(e)
            logger.error(f"[W{self.worker_id}] ❌ EXCEPTION for {admin_email}: {type(e).__name__}: {e}")
            self._screenshot("ERROR_exception")
            import traceback
            logger.error(f"[W{self.worker_id}] Traceback:\n{traceback.format_exc()}")
        
        finally:
            if self.driver:
                # In DEBUG_MODE, keep browser open on error for manual inspection
                if DEBUG_MODE and not result.success:
                    logger.warning(f"[W{self.worker_id}] 🔍 DEBUG MODE: Browser kept open for debugging!")
                    logger.warning(f"[W{self.worker_id}] 🔍 Current URL: {self.driver.current_url}")
                    logger.warning(f"[W{self.worker_id}] 🔍 Press Ctrl+C to stop, or wait 60 seconds...")
                    logger.warning(f"[W{self.worker_id}] 🔍 Screenshots saved to: {SCREENSHOT_DIR}")
                    try:
                        # Wait 60 seconds for manual inspection
                        time.sleep(60)
                    except KeyboardInterrupt:
                        logger.info(f"[W{self.worker_id}] User interrupted, closing browser...")
                    finally:
                        try:
                            self.driver.quit()
                        except:
                            pass
                else:
                    try:
                        self.driver.quit()
                    except:
                        pass
        
        with _progress_lock:
            if result.success:
                _progress["completed"] += 1
            else:
                _progress["failed"] += 1
        
        return result
    
    def _handle_password_change_standard(self, current_password: str) -> Optional[str]:
        """
        Handle password change screen.
        Returns new password if successful, None otherwise.
        """
        worker_id = getattr(self, 'worker_id', 0)
        logger.info(f"[W{worker_id}] 🔄 Handling password change...")
        self._screenshot("password_change_start")
        
        new_password = STANDARD_NEW_PASSWORD
        
        # Enter current password
        current_input = self._find_element([
            (By.ID, "currentPassword"),
            (By.NAME, "currentPassword"),
            (By.NAME, "oldPassword"),
            (By.CSS_SELECTOR, "input[name*='current']"),
        ], timeout=5)
        
        if current_input:
            current_input.clear()
            current_input.send_keys(current_password)
            logger.info(f"[W{worker_id}] ✓ Entered current password")
        else:
            logger.error(f"[W{worker_id}] ✗ Current password field not found!")
            return None
        
        # Enter new password
        new_input = self._find_element([
            (By.ID, "newPassword"),
            (By.NAME, "newPassword"),
            (By.NAME, "newpasswd"),
        ], timeout=5)
        
        if new_input:
            new_input.clear()
            new_input.send_keys(new_password)
            logger.info(f"[W{worker_id}] ✓ Entered new password")
        else:
            logger.error(f"[W{worker_id}] ✗ New password field not found!")
            return None
        
        # Enter confirm password
        confirm_input = self._find_element([
            (By.ID, "confirmNewPassword"),
            (By.NAME, "confirmNewPassword"),
            (By.NAME, "confirmnewpasswd"),
        ], timeout=5)
        
        if confirm_input:
            confirm_input.clear()
            confirm_input.send_keys(new_password)
            logger.info(f"[W{worker_id}] ✓ Entered confirm password")
        else:
            logger.error(f"[W{worker_id}] ✗ Confirm password field not found!")
            return None
        
        self._screenshot("password_fields_filled")
        
        # Submit
        submit_clicked = self._click_if_exists([
            (By.ID, "idSIButton9"),
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.CSS_SELECTOR, "button[type='submit']"),
        ], timeout=3)
        
        if not submit_clicked:
            confirm_input.send_keys(Keys.RETURN)
        
        logger.info(f"[W{worker_id}] Submitted password change...")
        time.sleep(5)
        self._screenshot("after_password_change")
        
        # Verify success
        page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
        if "password" in page_text and ("requirements" in page_text or "must" in page_text or "invalid" in page_text):
            logger.error(f"[W{worker_id}] ✗ Password change failed - requirements not met")
            return None
        
        logger.info(f"[W{worker_id}] ✓ Password change successful!")
        return new_password

    def _handle_password_change(self, current_password: str, new_password: str) -> bool:
        """Handle the password change form."""
        logger.info(f"[W{self.worker_id}] 🔄 Starting password change flow")
        self._screenshot("password_change_page")
        
        # Find and fill current password
        current_input = self._find_element([
            (By.ID, "currentPassword"),
            (By.NAME, "currentPassword"),
            (By.NAME, "oldPassword"),
            (By.CSS_SELECTOR, "input[name*='current']"),
        ])
        
        if current_input:
            current_input.clear()
            current_input.send_keys(current_password)
            logger.info(f"[W{self.worker_id}] ✓ Entered current password")
        else:
            logger.error(f"[W{self.worker_id}] ✗ Current password field not found!")
            self._debug_page_elements()
            return False
        
        # Find and fill new password
        new_input = self._find_element([
            (By.ID, "newPassword"),
            (By.NAME, "newPassword"),
            (By.NAME, "newpasswd"),
            (By.CSS_SELECTOR, "input[name*='new']"),
        ])
        
        if new_input:
            new_input.clear()
            new_input.send_keys(new_password)
            logger.info(f"[W{self.worker_id}] ✓ Entered new password")
        else:
            logger.error(f"[W{self.worker_id}] ✗ New password field not found!")
            return False
        
        # Find and fill confirm password
        confirm_input = self._find_element([
            (By.ID, "confirmNewPassword"),
            (By.NAME, "confirmNewPassword"),
            (By.NAME, "confirmPassword"),
            (By.NAME, "confirmnewpasswd"),
            (By.CSS_SELECTOR, "input[name*='confirm']"),
        ])
        
        if confirm_input:
            confirm_input.clear()
            confirm_input.send_keys(new_password)
            logger.info(f"[W{self.worker_id}] ✓ Entered confirm password")
        else:
            logger.error(f"[W{self.worker_id}] ✗ Confirm password field not found!")
            return False
        
        self._screenshot("password_fields_filled")
        
        # Submit
        submitted = self._click_if_exists([
            (By.ID, "idSIButton9"),
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.XPATH, "//button[contains(text(),'Submit')]"),
            (By.XPATH, "//button[contains(text(),'Sign in')]"),
        ])
        
        if not submitted and confirm_input:
            confirm_input.send_keys(Keys.RETURN)
        
        logger.info(f"[W{self.worker_id}] Submitted password change, waiting...")
        time.sleep(5)
        self._screenshot("after_password_change_submit")
        
        # Check for errors
        page = self.driver.page_source.lower()
        if "password" in page and ("requirements" in page or "must" in page or "invalid" in page):
            logger.error(f"[W{self.worker_id}] ✗ Password change failed - requirements not met?")
            return False
        
        logger.info(f"[W{self.worker_id}] ✓ Password change submitted successfully")
        return True
    
    def _handle_stay_signed_in(self) -> bool:
        """Handle the 'Stay signed in?' prompt."""
        logger.info(f"[W{self.worker_id}] Handling 'Stay signed in?' prompt...")
        clicked = self._click_if_exists([
            (By.ID, "idBtn_Back"),  # No button
            (By.XPATH, "//button[contains(.,'No')]"),
            (By.ID, "declineButton"),
        ])
        if clicked:
            logger.info(f"[W{self.worker_id}] ✓ Clicked 'No' on stay signed in")
        return clicked


def _worker_task(args):
    worker_id, tenant_id, email, initial_pwd, new_pwd = args
    # Use DEBUG_HEADLESS flag - set to False to SEE the browser
    return BrowserWorker(worker_id, headless=DEBUG_HEADLESS).process(tenant_id, email, initial_pwd, new_pwd)


class ParallelAutomation:
    """Process multiple tenants in parallel."""
    
    def __init__(self, max_workers: int = 10):
        self.max_workers = max_workers
    
    def process_all(self, tenants: List[Dict], new_password: str) -> List[TenantResult]:
        global _progress
        
        # DEBUG MODE: Limit number of tenants processed
        if DEBUG_MODE and DEBUG_MAX_TENANTS > 0:
            original_count = len(tenants)
            tenants = tenants[:DEBUG_MAX_TENANTS]
            logger.warning(f"⚠️ DEBUG MODE: Processing only {len(tenants)} of {original_count} tenants")
            logger.warning(f"⚠️ DEBUG MODE: Headless={DEBUG_HEADLESS} (False=visible browser)")
            logger.warning(f"⚠️ DEBUG MODE: Screenshots will be saved to {SCREENSHOT_DIR}")
        
        _progress = {"completed": 0, "failed": 0, "total": len(tenants)}
        
        work = [
            (i % self.max_workers, t["tenant_id"], t["admin_email"], t["initial_password"], new_password)
            for i, t in enumerate(tenants)
        ]
        
        results = []
        logger.info(f"Starting: {len(tenants)} tenants, {self.max_workers} workers")
        
        # STEP 4 FIX: Stagger worker startups by 5 seconds to prevent browser collision issues
        STEP4_STAGGER_SECONDS = 5
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            for i, w in enumerate(work):
                if i > 0:
                    logger.info(f"Staggering worker startup: waiting {STEP4_STAGGER_SECONDS}s before starting worker {i}")
                    time.sleep(STEP4_STAGGER_SECONDS)
                futures[executor.submit(_worker_task, w)] = w
            
            for future in as_completed(futures):
                results.append(future.result())
        
        success = sum(1 for r in results if r.success)
        logger.info(f"Done: {success}/{len(tenants)} successful")
        
        return results


async def process_tenants_parallel(
    tenants: List[Dict],
    new_password: str,
    max_workers: int = 10
) -> List[Dict]:
    """Async wrapper."""
    processor = ParallelAutomation(max_workers)
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, lambda: processor.process_all(tenants, new_password))
    
    return [
        {
            "tenant_id": r.tenant_id,
            "admin_email": r.admin_email,
            "success": r.success,
            "new_password": r.new_password,
            "totp_secret": r.totp_secret,
            "password_changed": r.password_changed,
            "security_defaults_disabled": r.security_defaults_disabled,
            "error": r.error
        }
        for r in results
    ]


def get_progress():
    with _progress_lock:
        return dict(_progress)


# === RESUMABLE AUTOMATION (New Entry Point) ===

async def process_tenant(tenant, db, worker_id: int = 0) -> dict:
    """
    Main entry point - process a tenant through login, password change, and MFA.
    Handles all partial states. Uses state machine approach.
    """
    logger.info(f"[W{worker_id}] ========== STARTING {tenant.id} ==========")
    logger.info(f"[W{worker_id}] Email: {tenant.admin_email}")
    logger.info(f"[W{worker_id}] Password Changed: {tenant.password_changed}")
    logger.info(f"[W{worker_id}] TOTP Secret: {'Set' if tenant.totp_secret else 'Not set'}")
    
    result = {
        "status": "failed",
        "new_password": tenant.admin_password,
        "totp_secret": tenant.totp_secret,
        "error": None
    }
    
    # Skip if already complete
    if tenant.first_login_completed and tenant.totp_secret:
        logger.info(f"[W{worker_id}] ✓ Already complete, skipping")
        return {"status": "skipped", "reason": "already_complete"}
    
    worker = BrowserWorker(worker_id, headless=DEBUG_HEADLESS)
    worker.tenant_id = str(tenant.id)
    
    try:
        worker.driver = worker._create_driver()
        worker.driver.get("https://portal.azure.com")
        time.sleep(3)
        worker._screenshot("01_start")
        
        # STEP 1: Login with smart password fallback
        logger.info(f"[W{worker_id}] === STEP 1: Login ===")
        if not await worker._smart_login_async(tenant, db):
            raise Exception("Login failed with all passwords")
        
        # STEP 2: Handle whatever state we're in
        max_iterations = 10
        for i in range(max_iterations):
            state = worker._detect_login_state()
            logger.info(f"[W{worker_id}] Iteration {i+1}, State: {state}")
            
            if state == LoginState.LOGGED_IN:
                logger.info(f"[W{worker_id}] ✓ Logged in!")
                break
            
            elif state == LoginState.NEEDS_PASSWORD_CHANGE:
                logger.info(f"[W{worker_id}] === Password Change Required ===")
                new_pwd = worker._handle_password_change_standard(tenant.admin_password)
                if new_pwd:
                    # IMMEDIATELY save to DB
                    tenant.admin_password = new_pwd
                    tenant.password_changed = True
                    await db.commit()
                    result["new_password"] = new_pwd
                    logger.info(f"[W{worker_id}] ✓ Password changed and saved")
                else:
                    raise Exception("Password change failed")
                time.sleep(2)
            
            elif state == LoginState.NEEDS_MFA_SETUP:
                logger.info(f"[W{worker_id}] === MFA Setup Required (Two-Phase) ===")
                # PHASE 1: Extract TOTP secret
                totp = worker.extract_totp_only()
                if not totp:
                    raise Exception("Failed to extract TOTP secret")

                # PHASE 2: Save to DB and verify
                success = await _save_totp_immediately(db, str(tenant.id), totp, worker_id)
                if not success:
                    raise Exception("Failed to save TOTP to database")

                result["totp_secret"] = totp
                logger.info(f"[W{worker_id}] ✓ TOTP saved and verified in DB")

                # PHASE 3: Complete MFA enrollment
                if not worker.complete_mfa_enrollment(totp):
                    raise Exception("MFA enrollment completion failed")

                logger.info(f"[W{worker_id}] ✓ MFA setup complete")
                time.sleep(2)
            
            elif state == LoginState.NEEDS_STAY_SIGNED_IN:
                logger.info(f"[W{worker_id}] Clicking 'Stay signed in'...")
                worker._click_if_exists([
                    (By.ID, "idSIButton9"),
                    (By.XPATH, "//button[contains(text(), 'Yes')]"),
                    (By.XPATH, "//button[contains(text(), 'No')]"),
                ], timeout=5)
                time.sleep(2)
            
            elif state == LoginState.WRONG_PASSWORD:
                raise Exception("Unexpected wrong password state in main loop")
            
            elif state == LoginState.ACCOUNT_LOCKED:
                raise Exception("Account is locked!")
            
            elif state == LoginState.ERROR:
                raise Exception("Error state detected")
            
            else:
                logger.warning(f"[W{worker_id}] Unknown state, waiting...")
                time.sleep(3)
        
        logger.info(
            f"[W{worker_id}] 💾 Saving to DB: totp="
            f"{tenant.totp_secret[:4] if tenant.totp_secret else 'NONE'}***, "
            "password_changed=True, first_login_completed=True"
        )
        # Mark complete - update all relevant fields
        tenant.first_login_completed = True
        tenant.first_login_at = datetime.utcnow()
        tenant.password_changed = True
        tenant.status = "first_login_complete"
        tenant.setup_step = 5  # Ready for Step 5 (OAuth/App Registration)
        tenant.setup_error = None  # Clear any previous errors
        tenant.security_defaults_disabled = False  # We're keeping Security Defaults ENABLED
        await db.commit()
        logger.info(f"[W{worker_id}] ✅ DB COMMIT SUCCESSFUL")

        # Verify save
        await db.refresh(tenant)
        logger.info(
            f"[W{worker_id}] Verified: totp={tenant.totp_secret is not None}, "
            f"pwd_changed={tenant.password_changed}, "
            f"first_login_completed={tenant.first_login_completed}"
        )
        
        result["status"] = "success"
        logger.info(f"[W{worker_id}] ========== COMPLETED {tenant.id} ==========")
        logger.info(f"[W{worker_id}] ✅ Password changed: {tenant.password_changed}")
        logger.info(f"[W{worker_id}] ✅ TOTP extracted: {bool(tenant.totp_secret)}")
        logger.info(f"[W{worker_id}] ✅ Security Defaults: ENABLED (using OAuth)")
        logger.info(f"[W{worker_id}] ✅ Ready for Step 5")
        
    except Exception as e:
        logger.error(f"[W{worker_id}] ❌ EXCEPTION: {e}")
        logger.error(f"[W{worker_id}] Traceback: {traceback.format_exc()}")
        result["status"] = "failed"
        result["error"] = str(e)
        tenant.setup_error = str(e)
        await db.commit()
        worker._screenshot("ERROR_crash")
    
    finally:
        if worker.driver:
            # Keep browser open for debugging in debug mode
            if DEBUG_MODE and result.get("status") == "failed":
                logger.warning(f"[W{worker_id}] DEBUG: Browser kept open. Press Ctrl+C to close.")
                logger.warning(f"[W{worker_id}] Current URL: {worker.driver.current_url}")
                logger.warning(f"[W{worker_id}] Screenshots saved to: {SCREENSHOT_DIR}")
                try:
                    time.sleep(60)
                except KeyboardInterrupt:
                    logger.info(f"[W{worker_id}] User interrupted, closing browser...")
            try:
                worker.driver.quit()
            except:
                pass
            # Clean up temporary profile directory
            try:
                if hasattr(worker.driver, '_profile_dir'):
                    shutil.rmtree(worker.driver._profile_dir)
                    logger.info(f"[W{worker_id}] 🔄 Cleaned up profile: {worker.driver._profile_dir}")
            except Exception as e:
                logger.debug(f"[W{worker_id}] Profile cleanup failed: {e}")
    
    return result


# Alias for backwards compatibility
async def process_tenant_resumable(tenant, db, worker_id: int = 0) -> Dict[str, Any]:
    """Alias for process_tenant for backwards compatibility."""
    return await process_tenant(tenant, db, worker_id)
