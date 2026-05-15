import time
import re
import os
import json
import pyotp
import tempfile
import uuid
import shutil
import threading
from datetime import datetime
from typing import Optional
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
import logging

# Import the working BrowserWorker from tenant_automation.py
# This ensures Step 5 uses the EXACT same browser setup as Step 4
from app.services.tenant_automation import BrowserWorker

logger = logging.getLogger(__name__)
_driver_state = threading.local()
SCREENSHOTS = "C:/temp/screenshots"
STATUS_DIR = "C:/temp/automation_status"
os.makedirs(SCREENSHOTS, exist_ok=True)
os.makedirs(STATUS_DIR, exist_ok=True)
SCREENSHOT_DIR = "/tmp/screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

MFA_SETUP_URL_MARKERS = ("mfasetup", "registered=false")
MFA_SETUP_TEXT_MARKERS = (
    "you need to set up multifactor authentication",
    "more information required",
    "keep your account secure",
    "set up multifactor authentication",
    "set up your account",
    "skip for now",
)
NON_RETRYABLE_SETUP_ERRORS = (
    "MFA setup is required",
    "MFA required but no TOTP secret",
    "MFA code input appeared but no TOTP secret",
)


def _remember_active_driver(driver):
    """Track the Selenium driver owned by the current worker thread."""
    _driver_state.active_driver = driver


def _clear_active_driver(driver=None):
    active_driver = getattr(_driver_state, "active_driver", None)
    if driver is None or active_driver is driver:
        _driver_state.active_driver = None


def _cleanup_active_driver(domain: str):
    """Close only the failed attempt's driver, not browsers owned by other workers."""
    driver = getattr(_driver_state, "active_driver", None)
    if not driver:
        return
    try:
        _cleanup_driver(driver)
        logger.info(f"[{domain}] Browser closed after failed attempt")
    finally:
        _clear_active_driver(driver)


def _safe_current_url(driver) -> str:
    try:
        return driver.current_url or ""
    except Exception as e:
        logger.debug(f"Could not read current URL: {e}")
        return ""


def _safe_page_text(driver) -> str:
    try:
        text = driver.execute_script("return document.body ? document.body.innerText : '';")
        return text or ""
    except Exception as e:
        logger.debug(f"Could not read page text via JavaScript: {e}")
    try:
        driver.implicitly_wait(1)
    except Exception:
        pass
    try:
        return driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception as e:
        logger.debug(f"Could not read page body text: {e}")
        return ""
    finally:
        try:
            driver.implicitly_wait(15)
        except Exception:
            pass


def _mfa_setup_blocking_reason(driver) -> Optional[str]:
    current_url = _safe_current_url(driver).lower()
    if any(marker in current_url for marker in MFA_SETUP_URL_MARKERS):
        return f"MFA setup page is open ({current_url})"

    page_text = _safe_page_text(driver).lower()
    for marker in MFA_SETUP_TEXT_MARKERS:
        if marker in page_text:
            return f"MFA setup prompt detected: {marker}"
    return None


def _build_admin_url(driver, hash_path: str) -> str:
    """Preserve the admin host Microsoft redirected us to, then append a hash route."""
    current_url = _safe_current_url(driver).lower()
    route = hash_path.lstrip("/")
    if "admin.microsoft.com" in current_url and "admin.cloud.microsoft" not in current_url:
        return f"https://admin.microsoft.com/#{route}"
    return f"https://admin.cloud.microsoft/#{route}"


def _is_domains_page_loaded(driver) -> bool:
    current_url = _safe_current_url(driver).lower()
    if "domains" in current_url and "mfasetup" not in current_url:
        return True

    page_text = _safe_page_text(driver).lower()
    return (
        ".onmicrosoft.com" in page_text
        or "add domain" in page_text
        or ("domains" in page_text and "microsoft 365 admin center" in page_text)
    )


def _handle_mfa_setup_interrupt(driver, domain: str, totp_secret: Optional[str], context: str) -> bool:
    """
    Dismiss an MFA setup interrupt when Microsoft allows it.

    Tenants without a TOTP secret are valid if Microsoft does not require MFA.
    If Microsoft does require MFA setup and there is no stored TOTP, fail with a
    clear error instead of retrying browser navigation until Chrome stalls.
    """
    reason = _mfa_setup_blocking_reason(driver)
    if not reason:
        return False

    logger.warning(f"[{domain}] {reason} during {context}")
    dismissed = dismiss_mfa_setup_interrupt(driver, domain)
    if dismissed and not _mfa_setup_blocking_reason(driver):
        return True

    reason = _mfa_setup_blocking_reason(driver) or reason
    if not totp_secret:
        raise Exception(
            f"MFA setup is required during {context}, but no TOTP secret is stored for this tenant. "
            "Rerun the tenant first-login/MFA enrollment step, or disable the MFA setup requirement for this tenant."
        )

    raise Exception(f"MFA setup interrupt is still blocking {context}: {reason}")


def _is_non_retryable_setup_error(error: Optional[str]) -> bool:
    if not error:
        return False
    return any(marker in error for marker in NON_RETRYABLE_SETUP_ERRORS)


def dismiss_mfa_setup_interrupt(driver, domain: str = "unknown", max_attempts: int = 2) -> bool:
    """
    Dismiss the 'You need to set up multifactor authentication' admin center
    interrupt by clicking 'Skip for now'. Returns True if dismissed, False if
    not present.

    Safe to call anytime after admin portal login — no-op if interrupt absent.
    """
    from selenium.webdriver.common.by import By
    import time

    try:
        driver.implicitly_wait(1)
    except Exception:
        pass

    for attempt in range(max_attempts):
        try:
            current_url = _safe_current_url(driver).lower()

            # Detect by URL first (fastest)
            on_mfa_setup = any(marker in current_url for marker in MFA_SETUP_URL_MARKERS)

            # Also detect by page text (in case URL doesn't match but modal is overlaid)
            if not on_mfa_setup:
                page_text = _safe_page_text(driver).lower()
                on_mfa_setup = any(marker in page_text for marker in MFA_SETUP_TEXT_MARKERS)
                try:
                    driver.implicitly_wait(1)
                except Exception:
                    pass

            if not on_mfa_setup:
                try:
                    driver.implicitly_wait(15)
                except Exception:
                    pass
                return False

            logger.info(f"[{domain}] MFA setup interrupt detected, clicking 'Skip for now'")

            # Try in order: most specific → least specific. Skip for now is a LINK
            # (anchor/button with link styling), Set up now is the primary button.
            skip_selectors = [
                (By.XPATH, "//a[normalize-space()='Skip for now']"),
                (By.XPATH, "//button[normalize-space()='Skip for now']"),
                (By.XPATH, "//*[normalize-space()='Skip for now']"),
                (By.XPATH, "//a[contains(normalize-space(), 'Skip for now')]"),
                (By.XPATH, "//button[contains(normalize-space(), 'Skip for now')]"),
                (By.PARTIAL_LINK_TEXT, "Skip for now"),
            ]

            for by, sel in skip_selectors:
                try:
                    elems = driver.find_elements(by, sel)
                    for elem in elems:
                        if elem.is_displayed():
                            # JS click bypasses any lingering overlays
                            driver.execute_script("arguments[0].click();", elem)
                            logger.info(f"[{domain}] Clicked 'Skip for now' via {sel}")
                            time.sleep(2)
                            # Verify dismissed
                            new_url = _safe_current_url(driver).lower()
                            if "mfasetup" not in new_url:
                                logger.info(f"[{domain}] MFA setup interrupt dismissed")
                                try:
                                    driver.implicitly_wait(15)
                                except Exception:
                                    pass
                                return True
                except Exception as e:
                    logger.debug(f"[{domain}] Selector {sel} failed: {e}")
                    continue

            logger.warning(f"[{domain}] Could not find 'Skip for now' on attempt {attempt + 1}")
            time.sleep(1.5)

        except Exception as e:
            logger.warning(f"[{domain}] Error during MFA interrupt dismiss attempt {attempt + 1}: {e}")
            time.sleep(1)

    # Last resort: try navigating away directly. If the interrupt hard-blocks this,
    # at least we'll see the failure in logs instead of a stuck session.
    try:
        logger.warning(f"[{domain}] Falling back to direct navigation to escape MFA interrupt")
        driver.get("https://admin.cloud.microsoft/#/Domains")
        time.sleep(3)
        final_url = _safe_current_url(driver).lower()
        if "mfasetup" not in final_url:
            try:
                driver.implicitly_wait(15)
            except Exception:
                pass
            return True
    except Exception as e:
        logger.error(f"[{domain}] Fallback navigation failed: {e}")

    try:
        driver.implicitly_wait(15)
    except Exception:
        pass
    return False


def _cleanup_driver(driver):
    """Properly close driver and cleanup temp profile directory."""
    if not driver:
        return
    profile_dir = getattr(driver, '_profile_dir', None)
    try:
        driver.quit()
    except Exception as e:
        logger.warning(f"Error closing driver: {e}")
    if profile_dir:
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
            logger.debug(f"Cleaned up profile dir: {profile_dir}")
        except Exception as e:
            logger.warning(f"Could not cleanup profile dir {profile_dir}: {e}")


def screenshot(driver, name, domain):
    try:
        path = f"{SCREENSHOTS}/{name}_{domain.replace('.','_')}_{int(time.time())}.png"
        driver.save_screenshot(path)
        logger.info(f"Screenshot: {path}")
    except:
        pass


def _save_screenshot(driver, domain: str, step: str):
    """Save screenshot for debugging."""
    try:
        screenshot_dir = os.environ.get("SCREENSHOT_DIR", SCREENSHOT_DIR)
        os.makedirs(screenshot_dir, exist_ok=True)
        safe_domain = domain.replace(".", "_")
        timestamp = int(time.time())
        filepath = os.path.join(screenshot_dir, f"{step}_{safe_domain}_{timestamp}.png")
        driver.save_screenshot(filepath)
        logger.info(f"Screenshot: {filepath}")
    except Exception as e:
        logger.warning(f"Could not save screenshot: {e}")


def update_status_file(domain: str, step: str, status: str, details: str = None):
    """Write status to file for UI polling - enables real-time updates."""
    try:
        filepath = os.path.join(STATUS_DIR, f"{domain.replace('.', '_')}.json")
        status_data = {
            "domain": domain,
            "step": step,
            "status": status,  # "in_progress", "complete", "failed"
            "details": details,
            "timestamp": time.time()
        }
        with open(filepath, "w") as f:
            json.dump(status_data, f)
        logger.debug(f"[{domain}] Status updated: {step}={status}")
    except Exception as e:
        logger.warning(f"[{domain}] Could not update status file: {e}")


def get_all_progress() -> dict:
    """Read all progress files and return current state of all domains.
    
    Used by the API endpoint to provide real-time progress to the UI.
    Returns dict mapping domain -> progress data.
    """
    progress = {}
    try:
        if os.path.exists(STATUS_DIR):
            for filename in os.listdir(STATUS_DIR):
                if filename.endswith(".json"):
                    try:
                        filepath = os.path.join(STATUS_DIR, filename)
                        with open(filepath, "r") as f:
                            data = json.load(f)
                            # Use domain as key
                            domain = data.get("domain", filename.replace("_", ".").replace(".json", ""))
                            progress[domain] = data
                    except Exception as e:
                        logger.warning(f"Could not read progress file {filename}: {e}")
    except Exception as e:
        logger.warning(f"Could not list progress directory: {e}")
    return progress


def get_progress(domain: str) -> dict:
    """Get current progress for a specific domain."""
    try:
        filepath = os.path.join(STATUS_DIR, f"{domain.replace('.', '_')}.json")
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read progress for {domain}: {e}")
    return {}


def clear_progress(domain: str):
    """Clear progress file for a domain (after completion or cleanup)."""
    try:
        filepath = os.path.join(STATUS_DIR, f"{domain.replace('.', '_')}.json")
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.debug(f"[{domain}] Progress file cleared")
    except Exception as e:
        logger.warning(f"[{domain}] Could not clear progress file: {e}")


def clear_all_progress():
    """Clear all progress files (useful for cleanup before batch start)."""
    try:
        if os.path.exists(STATUS_DIR):
            for filename in os.listdir(STATUS_DIR):
                if filename.endswith(".json"):
                    filepath = os.path.join(STATUS_DIR, filename)
                    os.remove(filepath)
            logger.info("All progress files cleared")
    except Exception as e:
        logger.warning(f"Could not clear all progress files: {e}")


def wait_for_page_change(driver, old_text: str, timeout: int = 30) -> bool:
    """Wait until page content changes - critical for headless mode timing."""
    logger.debug(f"Waiting for page change (timeout={timeout}s)...")
    for i in range(timeout):
        time.sleep(1)
        try:
            new_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            # Page changed if text is different and has reasonable content
            if new_text != old_text and len(new_text) > 100:
                logger.debug(f"Page changed after {i+1}s")
                return True
        except:
            pass
    logger.warning(f"Page did not change within {timeout}s")
    return False


def wait_for_page_settle(driver, domain: str, max_wait: int = 10) -> str:
    """Wait for page to fully load - checks for loading indicators."""
    logger.info(f"[{domain}] Waiting for page to settle (max {max_wait}s)...")
    for i in range(max_wait):
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            # Check if page has stopped loading and has content
            if "loading" not in page_text and len(page_text) > 100:
                logger.info(f"[{domain}] Page settled after {i+1}s (text length: {len(page_text)})")
                return page_text
        except:
            pass
        time.sleep(1)
    # Return whatever we have after max wait
    try:
        return driver.find_element(By.TAG_NAME, "body").text.lower()
    except:
        return ""

def click_element(driver, xpath, description):
    """Find and click an element, trying multiple methods."""
    logger.info(f"Clicking: {description}")
    try:
        elem = WebDriverWait(driver, 15).until(EC.element_to_be_clickable((By.XPATH, xpath)))
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
        time.sleep(0.5)
        try:
            elem.click()
        except:
            driver.execute_script("arguments[0].click();", elem)
        logger.info(f"Clicked: {description}")
        return True
    except Exception as e:
        logger.warning(f"Could not click {description}: {e}")
        return False


# ============================================================
# ROBUST HELPER FUNCTIONS FOR ELEMENT INTERACTION
# ============================================================

def safe_click(driver, element, description="element"):
    """Safely click an element with multiple fallbacks."""
    try:
        # Scroll into view
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(0.5)
        
        # Try regular click
        try:
            element.click()
            logger.debug(f"Clicked {description} via regular click")
            return True
        except:
            pass
        
        # Try JS click
        try:
            driver.execute_script("arguments[0].click();", element)
            logger.debug(f"Clicked {description} via JS click")
            return True
        except:
            pass
        
        # Try ActionChains
        try:
            ActionChains(driver).move_to_element(element).click().perform()
            logger.debug(f"Clicked {description} via ActionChains")
            return True
        except:
            pass
        
        logger.warning(f"Could not click {description} with any method")
        return False
        
    except Exception as e:
        logger.error(f"safe_click error for {description}: {e}")
        return False


def safe_find_and_click(driver, by, value, description="element", timeout=15):
    """Find element and click it safely."""
    try:
        element = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((by, value))
        )
        return safe_click(driver, element, description)
    except Exception as e:
        logger.warning(f"Could not find/click {description}: {e}")
        return False


def wait_for_page_load(driver, timeout=30):
    """Wait for page to fully load."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(1)  # Extra buffer
        return True
    except:
        logger.warning(f"Page did not fully load within {timeout}s")
        return False


def wait_for_body_text(driver, min_length: int = 50, timeout: int = 30) -> str:
    """Wait until the page has non-trivial body text. Returns last seen text."""
    last_text = ""
    for _ in range(timeout):
        try:
            last_text = driver.find_element(By.TAG_NAME, "body").text
            if last_text and len(last_text.strip()) >= min_length:
                return last_text
        except Exception:
            pass
        time.sleep(1)
    return last_text


def _navigate_to_domains_page(driver, domain: str, totp_secret: Optional[str]) -> None:
    """Navigate to the M365 domains page, handling optional MFA setup interrupts."""
    _handle_mfa_setup_interrupt(driver, domain, totp_secret, "before domains navigation")

    candidate_urls = []
    for url in (
        "https://admin.cloud.microsoft/#/Domains",
        _build_admin_url(driver, "/Domains"),
        "https://admin.microsoft.com/#/Domains",
    ):
        if url not in candidate_urls:
            candidate_urls.append(url)

    last_error = None
    for nav_attempt, domains_url in enumerate(candidate_urls, start=1):
        logger.info(f"[{domain}] Navigating to domains page ({nav_attempt}/{len(candidate_urls)}): {domains_url}")
        try:
            driver.get(domains_url)
            wait_for_page_load(driver, timeout=25)
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[{domain}] Domains navigation attempt {nav_attempt} raised: {e}")

        time.sleep(5)

        if _handle_mfa_setup_interrupt(driver, domain, totp_secret, "domains navigation"):
            time.sleep(2)
            continue

        for check_attempt in range(10):
            if _is_domains_page_loaded(driver):
                logger.info(f"[{domain}] Successfully reached domains page")
                return

            if _mfa_setup_blocking_reason(driver):
                _handle_mfa_setup_interrupt(driver, domain, totp_secret, "domains page load")
                break

            time.sleep(2)

        logger.warning(f"[{domain}] Domains page not ready after navigation attempt {nav_attempt}")

    detail = f": {last_error}" if last_error else ""
    raise Exception(f"Could not reach domains page after {len(candidate_urls)} navigation attempts{detail}")


# ============================================================
# RETRY WRAPPER FOR RESILIENT DOMAIN SETUP
# ============================================================

def setup_domain_with_retry(
    domain: str,
    zone_id: str,
    admin_email: str,
    admin_password: str,
    totp_secret: Optional[str],
    max_retries: int = 2,
    headless: bool = True,
) -> dict:
    """
    Setup domain with automatic retry on failure.
    
    This wrapper adds resilience by automatically retrying failed attempts.
    Useful for handling transient network issues, timing problems, or
    temporary Microsoft portal issues.
    
    Args:
        domain: Domain name to setup
        zone_id: Cloudflare zone ID
        admin_email: M365 admin email
        admin_password: M365 admin password  
        totp_secret: Optional TOTP secret for MFA
        max_retries: Number of retry attempts (default 2, so 3 total attempts)
    
    Returns:
        Dict with success, verified, dns_configured, error keys
    """
    last_error = None
    attempts_used = 0
    
    for attempt in range(max_retries + 1):
        attempts_used = attempt + 1
        _cleanup_active_driver(domain)
        if attempt > 0:
            logger.info(f"[{domain}] Retry attempt {attempt}/{max_retries} - waiting 60s before retry...")
            time.sleep(60)  # Wait before retry to let resources free up (increased from 30s)
        
        try:
            logger.info(f"[{domain}] Starting setup attempt {attempt + 1}/{max_retries + 1}")
            
            result = setup_domain_complete_via_admin_portal(
                domain=domain,
                zone_id=zone_id,
                admin_email=admin_email,
                admin_password=admin_password,
                totp_secret=totp_secret,
                headless=headless,
            )
            
            if result.get("success"):
                if attempt > 0:
                    logger.info(f"[{domain}] SUCCESS on retry attempt {attempt}!")
                return result
            
            last_error = result.get("error", "Unknown error")
            logger.warning(f"[{domain}] Attempt {attempt + 1} failed: {last_error}")
            _cleanup_active_driver(domain)
            if _is_non_retryable_setup_error(last_error):
                logger.error(f"[{domain}] Non-retryable setup error, not retrying: {last_error}")
                break
            
        except Exception as e:
            last_error = str(e)
            logger.error(f"[{domain}] Attempt {attempt + 1} exception: {e}")
            _cleanup_active_driver(domain)
            if _is_non_retryable_setup_error(last_error):
                logger.error(f"[{domain}] Non-retryable setup exception, not retrying: {last_error}")
                break
    
    # All attempts failed
    logger.error(f"[{domain}] FAILED after {attempts_used} attempts. Last error: {last_error}")
    return {
        "success": False, 
        "verified": False,
        "dns_configured": False,
        "error": f"Failed after {attempts_used} attempts: {last_error}"
    }


def _login_with_mfa(driver, admin_email: str, admin_password: str, totp_secret: Optional[str], domain: str) -> None:
    """Log into M365 admin portal with robust optional MFA handling.

    Raises:
        Exception: if required login steps are not reachable.
    """
    logger.info(f"[{domain}] Logging into M365 Admin Portal")
    if not totp_secret:
        logger.info(f"[{domain}] No TOTP secret stored; will proceed if Microsoft does not require MFA")

    driver.get("https://admin.microsoft.com")
    wait_for_page_load(driver, timeout=30)
    time.sleep(3)

    # Email
    email_field = WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.NAME, "loginfmt"))
    )
    email_field.clear()
    email_field.send_keys(admin_email + Keys.RETURN)
    time.sleep(3)

    # Password
    password_field = WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.NAME, "passwd"))
    )
    password_field.clear()
    password_field.send_keys(admin_password + Keys.RETURN)
    time.sleep(3)

    # Handle "Action required" / "More information required" screens
    page_text = driver.page_source.lower()
    if (
        "action required" in page_text
        or "more information required" in page_text
        or "keep your account secure" in page_text
        or "security defaults" in page_text
    ):
        logger.info(f"[{domain}] Detected action-required flow, clicking Next")
        next_selectors = [
            (By.ID, "idSubmit_ProofUp_Redirect"),
            (By.ID, "idSIButton9"),
            (By.XPATH, "//button[normalize-space()='Next']"),
            (By.XPATH, "//button[contains(text(), 'Next')]"),
            (By.CSS_SELECTOR, "button[data-testid='reskin-step-next-button']"),
        ]
        for by, value in next_selectors:
            if safe_find_and_click(driver, by, value, "Action Required Next", timeout=5):
                break
        time.sleep(3)

    # Detect MFA prompt by page text OR input field
    page_text = driver.page_source.lower()
    if "allow access" in page_text or "enter code to allow access" in page_text:
        mfa_detected = False
    else:
        mfa_indicators = [
            "verify your identity",
            "verification code",
            "use the authenticator",
            "sign in with a code",
            "authenticator",
            "approve sign",
            "open your authenticator",
        ]
        mfa_detected = any(indicator in page_text for indicator in mfa_indicators)

    # ---- ROBUST MFA HANDLING ----
    # Microsoft shows DIFFERENT MFA page variants:
    #   1. Push notification page ("Approve sign-in request") — NO otc input
    #   2. TOTP code entry page ("Enter code") — HAS otc input
    # We must detect which variant and switch to TOTP if needed.

    totp_input = None
    if mfa_detected:
        logger.info(f"[{domain}] MFA detected, handling MFA flow...")
        _save_screenshot(driver, domain, "mfa_page_detected")
        if not totp_secret:
            try:
                page_body = driver.find_element(By.TAG_NAME, "body").text
                logger.error(f"[{domain}] MFA is required but no TOTP secret is stored. Page text: {page_body[:500]}")
            except Exception:
                pass
            raise Exception("MFA required but no TOTP secret is stored for this tenant")

        # STEP A: Try to find the TOTP input directly (maybe already on code page)
        try:
            totp_input = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.NAME, "otc"))
            )
            logger.info(f"[{domain}] Found TOTP input directly (otc)")
        except Exception:
            # Also try the ID-based selector
            try:
                totp_input = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((By.ID, "idTxtBx_SAOTCC_OTC"))
                )
                logger.info(f"[{domain}] Found TOTP input directly (idTxtBx_SAOTCC_OTC)")
            except Exception:
                totp_input = None

        # STEP B: If not on code entry page, switch to it
        if not totp_input:
            logger.info(f"[{domain}] TOTP input not found, looking for 'use verification code' link...")
            _save_screenshot(driver, domain, "mfa_no_otc_input")

            # Try clicking various links to switch to TOTP code entry
            switch_selectors = [
                # "Use a verification code" link
                (By.XPATH, "//*[contains(text(), 'verification code')]"),
                (By.XPATH, "//*[contains(text(), 'Verification code')]"),
                # "I can't use my Microsoft Authenticator app right now"
                (By.XPATH, "//*[contains(text(), \"can't use\")]"),
                (By.XPATH, "//*[contains(text(), \"Can't use\")]"),
                # "Sign in another way"
                (By.XPATH, "//*[contains(text(), 'Sign in another way')]"),
                (By.XPATH, "//*[contains(text(), 'sign in another way')]"),
                # "Use a different verification option"
                (By.XPATH, "//*[contains(text(), 'different verification')]"),
                # Direct link IDs Microsoft commonly uses
                (By.ID, "signInAnotherWay"),
                (By.CSS_SELECTOR, "a#signInAnotherWay"),
            ]

            clicked_switch = False
            for by, selector in switch_selectors:
                try:
                    elem = driver.find_element(by, selector)
                    if elem.is_displayed():
                        driver.execute_script("arguments[0].click()", elem)
                        logger.info(f"[{domain}] Clicked MFA switch link: {selector}")
                        clicked_switch = True
                        time.sleep(3)
                        _save_screenshot(driver, domain, "mfa_after_switch_click")
                        break
                except Exception:
                    continue

            if not clicked_switch:
                logger.error(f"[{domain}] Could not find any link to switch MFA method")
                _save_screenshot(driver, domain, "mfa_no_switch_link_ERROR")
                try:
                    page_body = driver.find_element(By.TAG_NAME, "body").text
                    logger.error(f"[{domain}] Page text: {page_body[:500]}")
                except Exception:
                    pass
                raise Exception("Could not switch MFA method to TOTP code entry")

            # After clicking switch, we might be on a method selection page
            # Look for TOTP / authenticator app option
            time.sleep(2)
            method_selectors = [
                (By.XPATH, "//*[contains(text(), 'verification code from')]"),
                (By.XPATH, "//*[contains(text(), 'code from your authenticator')]"),
                (By.XPATH, "//*[contains(text(), 'authenticator app')]"),
                (By.XPATH, "//*[contains(text(), 'TOTP')]"),
                (By.XPATH, "//*[contains(text(), 'software token')]"),
                (By.XPATH, "//div[contains(@data-value, 'PhoneAppOTP')]"),
                (By.XPATH, "//div[contains(@data-value, 'OneWaySMS')]"),
            ]

            for by, selector in method_selectors:
                try:
                    elem = driver.find_element(by, selector)
                    if elem.is_displayed():
                        driver.execute_script("arguments[0].click()", elem)
                        logger.info(f"[{domain}] Selected TOTP method: {selector}")
                        time.sleep(3)
                        _save_screenshot(driver, domain, "mfa_after_method_select")
                        break
                except Exception:
                    continue

            # Now try to find the TOTP input again
            try:
                totp_input = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.NAME, "otc"))
                )
                logger.info(f"[{domain}] Found TOTP input after switching method")
            except Exception:
                # Try alternative selectors
                alt_selectors = [
                    (By.ID, "idTxtBx_SAOTCC_OTC"),
                    (By.CSS_SELECTOR, "input[type='tel']"),
                    (By.CSS_SELECTOR, "input[aria-label*='code']"),
                    (By.CSS_SELECTOR, "input[aria-label*='Code']"),
                    (By.CSS_SELECTOR, "input[placeholder*='code']"),
                    (By.CSS_SELECTOR, "input[autocomplete='one-time-code']"),
                ]
                for by, selector in alt_selectors:
                    try:
                        elem = driver.find_element(by, selector)
                        if elem.is_displayed():
                            totp_input = elem
                            logger.info(f"[{domain}] Found TOTP input with alt selector: {selector}")
                            break
                    except Exception:
                        continue

                if not totp_input:
                    _save_screenshot(driver, domain, "mfa_still_no_input_ERROR")
                    try:
                        page_body = driver.find_element(By.TAG_NAME, "body").text
                        logger.error(f"[{domain}] Still no TOTP input. Page text: {page_body[:500]}")
                    except Exception:
                        pass
                    raise Exception("Could not find TOTP code input after switching MFA method")

    else:
        # No MFA indicators detected — still check if a code input shows up
        try:
            for selector in [(By.ID, "idTxtBx_SAOTCC_OTC"), (By.NAME, "otc")]:
                try:
                    totp_input = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located(selector)
                    )
                    break
                except Exception:
                    continue
            if totp_input:
                logger.info(f"[{domain}] MFA input found without explicit indicators")
        except Exception:
            totp_input = None

    # STEP C: Enter the TOTP code (if we have an input)
    if totp_input:
        if not totp_secret:
            raise Exception("MFA code input appeared but no TOTP secret is stored for this tenant")
        code = pyotp.TOTP(totp_secret).now()
        logger.info(f"[{domain}] Entering TOTP code: {code[:2]}****")
        totp_input.clear()
        totp_input.send_keys(code)
        time.sleep(1)

        # STEP D: Click verify/submit
        verify_selectors = [
            (By.ID, "idSubmit_SAOTCC_Continue"),
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.XPATH, "//input[@value='Verify']"),
            (By.XPATH, "//button[contains(text(), 'Verify')]"),
        ]
        verify_clicked = False
        for by, selector in verify_selectors:
            try:
                btn = driver.find_element(by, selector)
                if btn.is_displayed():
                    safe_click(driver, btn, "MFA Verify")
                    verify_clicked = True
                    logger.info(f"[{domain}] Clicked verify button: {selector}")
                    break
            except Exception:
                continue
        if not verify_clicked:
            totp_input.send_keys(Keys.RETURN)
            logger.info(f"[{domain}] Pressed Enter to submit TOTP code")
        time.sleep(3)
        _save_screenshot(driver, domain, "mfa_after_verify")
    else:
        logger.info(f"[{domain}] No MFA code input detected — proceeding without MFA")

    # Handle "Stay signed in?" prompt
    time.sleep(2)
    try:
        yes_btn = driver.find_element(By.ID, "idSIButton9")
        if yes_btn.is_displayed():
            yes_btn.click()
            logger.info(f"[{domain}] Clicked 'Yes' on stay signed in")
            time.sleep(2)
    except Exception:
        pass
    try:
        no_btn = driver.find_element(By.ID, "idBtn_Back")
        if no_btn.is_displayed():
            no_btn.click()
            logger.info(f"[{domain}] Clicked 'No' on stay signed in")
            time.sleep(2)
    except Exception:
        logger.debug(f"[{domain}] No stay signed in prompt")

    # Dismiss the MFA setup interrupt if Microsoft allows skipping it. Tenants
    # without TOTP can continue only when Microsoft is not requiring MFA setup.
    _handle_mfa_setup_interrupt(driver, domain, totp_secret, "post-login")


def setup_domain_complete_via_admin_portal(domain, zone_id, admin_email, admin_password, totp_secret=None, cloudflare_service=None, headless=False):
    """Complete M365 domain setup following EXACT wizard flow.
    
    IMPORTANT: Each step has individual error handling for better resilience.
    The retry wrapper closes this attempt's browser if an unhandled exception bubbles out.
    """
    from app.services.cloudflare_sync import add_txt, add_mx, add_spf, add_cname, cleanup_before_verification, cleanup_before_dns_setup, resolve_zone_id
    
    logger.info(f"[{domain}] ========== STARTING DOMAIN SETUP ==========")
    driver = None
    result = {
        "success": False, 
        "verified": False, 
        "dns_configured": False, 
        "error": None,
        # DNS values to store in database
        "mx_value": None,
        "spf_value": None,
        "dkim_selector1_cname": None,
        "dkim_selector2_cname": None,
    }
    
    # ===== SETUP BROWSER WITH RETRY =====
    # Chrome can fail to start if resources are exhausted - retry up to 3 times
    logger.info(f"[{domain}] Creating browser with headless={headless}")
    driver = None
    CHROME_STARTUP_RETRIES = 3
    CHROME_RETRY_DELAY = 30  # seconds
    
    for chrome_attempt in range(CHROME_STARTUP_RETRIES):
        try:
            worker = BrowserWorker(worker_id=f"step5-{uuid.uuid4()}", headless=headless)
            driver = worker._create_driver()
            _remember_active_driver(driver)
            driver.implicitly_wait(15)  # Increased from 10
            driver.set_page_load_timeout(60)  # Add page load timeout
            try:
                driver.set_script_timeout(20)
            except Exception:
                pass
            logger.info(f"[{domain}] Browser initialized successfully on attempt {chrome_attempt + 1}")
            break
        except Exception as e:
            _cleanup_active_driver(domain)
            error_msg = str(e).lower()
            if "session not created" in error_msg or "chrome" in error_msg:
                logger.warning(f"[{domain}] Chrome startup failed (attempt {chrome_attempt + 1}/{CHROME_STARTUP_RETRIES}): {e}")
                if chrome_attempt < CHROME_STARTUP_RETRIES - 1:
                    logger.info(f"[{domain}] Waiting {CHROME_RETRY_DELAY}s before retrying Chrome startup...")
                    time.sleep(CHROME_RETRY_DELAY)
                else:
                    logger.error(f"[{domain}] Chrome failed to start after {CHROME_STARTUP_RETRIES} attempts")
                    raise Exception(f"Chrome failed to start after {CHROME_STARTUP_RETRIES} attempts: {e}")
            else:
                # Non-Chrome error, re-raise immediately
                raise
    
    if not driver:
        raise Exception("Failed to create browser driver")
    
    # ===== VALIDATE ZONE ID =====
    try:
        zone_id, zone_was_corrected = resolve_zone_id(zone_id, domain)
        if zone_was_corrected:
            result["corrected_zone_id"] = zone_id
            logger.info(f"[{domain}] Zone ID corrected to {zone_id}")
        else:
            logger.info(f"[{domain}] Zone ID validated OK: {zone_id}")
    except ValueError as e:
        logger.error(f"[{domain}] Zone ID validation failed: {e}")
        result["error"] = f"Zone ID validation failed: {e}"
        _cleanup_driver(driver)
        _clear_active_driver(driver)
        return result

    # ===== STEP 1: LOGIN =====
    logger.info(f"[{domain}] Step 1: Login")
    update_status_file(domain, "login", "in_progress", "Logging into M365 Admin Portal")
    _login_with_mfa(
        driver=driver,
        admin_email=admin_email,
        admin_password=admin_password,
        totp_secret=totp_secret,
        domain=domain,
    )
    
    screenshot(driver, "01_login", domain)
    update_status_file(domain, "login", "complete", "Successfully logged in")
    time.sleep(5)  # Extra wait after login
    
    # ===== STEP 2: NAVIGATE TO DOMAINS =====
    logger.info(f"[{domain}] Step 2: Navigate to domains page")
    _navigate_to_domains_page(driver, domain, totp_secret)
    
    screenshot(driver, "02_domains", domain)
    
    # ===== STEP 3: ADD DOMAIN =====
    logger.info(f"[{domain}] Step 3: Add domain")
    try:
        add_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Add domain')]"))
        )
        safe_click(driver, add_btn, "Add domain button")
    except:
        logger.info(f"[{domain}] Add domain button not found, navigating to wizard directly")
        # Dismiss MFA setup interrupt if it intercepted the page
        _handle_mfa_setup_interrupt(driver, domain, totp_secret, "before domain wizard navigation")
        driver.get("https://admin.cloud.microsoft/#/Domains/Wizard")
        wait_for_page_load(driver, timeout=30)
    time.sleep(5)  # Increased from 3
    
    # ===== STEP 4: ENTER DOMAIN =====
    try:
        logger.info(f"[{domain}] Step 4: Enter domain name")
        update_status_file(domain, "add_domain", "in_progress", "Adding domain to M365")
        
        domain_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='text']"))
        )
        domain_input.clear()
        domain_input.send_keys(domain)
        time.sleep(2)  # Increased from 1
        
        # Click Use this domain or Continue
        try:
            use_btn = driver.find_element(By.XPATH, "//button[contains(., 'Use this domain')]")
            safe_click(driver, use_btn, "Use this domain button")
        except:
            cont_btn = driver.find_element(By.XPATH, "//button[contains(., 'Continue')]")
            safe_click(driver, cont_btn, "Continue button")
        
        screenshot(driver, "03_entered_domain", domain)
        time.sleep(5)  # Increased from 3
        
    except Exception as e:
        logger.error(f"[{domain}] Enter domain failed: {e}")
        result["error"] = f"Enter domain failed: {e}"
        screenshot(driver, "error_enter_domain", domain)
        _cleanup_driver(driver)
        _clear_active_driver(driver)
        return result
    
    # ===== STEP 5: DETECT PAGE STATE AFTER ENTERING DOMAIN =====
    # IMPORTANT: In headless mode, pages load slower - wait longer
    time.sleep(5)  # Changed from 3 to 5
    
    # Take screenshot FIRST to see what we're dealing with
    screenshot(driver, "04_after_domain_entry", domain)
    
    # Wait for page to fully load - check for any loading indicators
    page_text = wait_for_page_settle(driver, domain, max_wait=10)
    
    # Log extensive page state info for debugging
    logger.info(f"[{domain}] Page text length: {len(page_text)}")
    logger.info(f"[{domain}] Page contains 'verify': {'verify' in page_text}")
    logger.info(f"[{domain}] Page contains 'connect': {'connect' in page_text}")
    logger.info(f"[{domain}] Page contains 'dns': {'dns' in page_text}")
    logger.info(f"[{domain}] Page contains 'complete': {'complete' in page_text}")
    
    # Check for VERIFICATION PAGE (multiple indicators)
    verification_indicators = [
        "verify you own",
        "verify your domain", 
        "domain verification",
        "before we can set up",
        "sign in to cloudflare",
        "more options",
        "confirm you own",
        "prove you own"
    ]
    
    is_verification_page = any(indicator in page_text for indicator in verification_indicators)
    if is_verification_page:
        logger.info(f"[{domain}] DETECTED: Verification page (indicators found)")
    
    # ===== CHECK IF ALREADY VERIFIED =====
    
    # If domain already verified, will go straight to connect page
    if "how do you want to connect" in page_text:
        logger.info(f"[{domain}] Domain already verified - skipping verification")
        result["verified"] = True
        # Will continue to Step 7 (connect page handling)
    elif "add dns records" in page_text:
        logger.info(f"[{domain}] Domain already verified and connected - on DNS page")
        result["verified"] = True
        # Will continue to Step 8 (DNS records page)
    elif "domain setup is complete" in page_text:
        logger.info(f"[{domain}] Domain already fully set up!")
        result["success"] = True
        result["verified"] = True
        result["dns_configured"] = True
        # Continue to end of function for proper cleanup
    
    # ===== STEP 5: VERIFICATION PAGE =====
    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    
    if "verify" in page_text and "own" in page_text:
        logger.info(f"[{domain}] Step 5: On verification page")
        update_status_file(domain, "verification", "in_progress", "Verifying domain ownership")
        screenshot(driver, "04_verify_page", domain)
        
        # 5a: Click "More options" LINK
        logger.info(f"[{domain}] Step 5a: Clicking 'More options' link")
        click_element(driver, "//a[contains(text(), 'More options')] | //span[contains(text(), 'More options')] | //*[contains(text(), 'More options')]", "More options link")
        time.sleep(2)
        screenshot(driver, "05_more_options_clicked", domain)
        
        # 5b: Select "Add a TXT record" RADIO BUTTON
        logger.info(f"[{domain}] Step 5b: Selecting TXT record option")
        # Try clicking the radio button or its label
        txt_clicked = False
        for xpath in [
            "//input[@type='radio'][following-sibling::*[contains(text(), 'TXT record')]]",
            "//input[@type='radio'][..//*[contains(text(), 'TXT record')]]",
            "//*[contains(text(), 'Add a TXT record')]",
            "//label[contains(., 'TXT record')]",
            "//div[contains(., 'Add a TXT record') and contains(@class, 'radio')]"
        ]:
            if click_element(driver, xpath, "TXT radio button"):
                txt_clicked = True
                break
        time.sleep(1)
        screenshot(driver, "06_txt_selected", domain)
        
        # 5c: Click Continue
        logger.info(f"[{domain}] Step 5c: Clicking Continue")
        click_element(driver, "//button[contains(., 'Continue')]", "Continue button")
        time.sleep(3)
        screenshot(driver, "07_txt_value_page", domain)
        
        # ===== STEP 6: TXT VALUE PAGE =====
        logger.info(f"[{domain}] Step 6: Extract TXT value")
        page_text = driver.find_element(By.TAG_NAME, "body").text
        txt_match = re.search(r'MS=ms\d+', page_text)
        
        if not txt_match:
            logger.error(f"[{domain}] TXT value not found!")
            screenshot(driver, "error_no_txt", domain)
            result["error"] = "TXT value not found"
            logger.error(f"[{domain}] FAILED - cleaning up browser")
            _cleanup_driver(driver)
            _clear_active_driver(driver)
            return result
        
        txt_value = txt_match.group(0)  
        logger.info(f"[{domain}] Found TXT: {txt_value}")
        
        # 6a: CLEANUP conflicting records, then add TXT to Cloudflare
        logger.info(f"[{domain}] Step 6a: Cleaning up conflicting DNS records before verification")
        cleanup_before_verification(zone_id)
        
        logger.info(f"[{domain}] Step 6a: Adding TXT to Cloudflare")
        add_txt(zone_id, txt_value)
        
        # 6b: Wait for DNS propagation
        logger.info(f"[{domain}] Step 6b: Waiting 30 seconds for DNS propagation")
        time.sleep(30)
        
        # 6c: Click Verify - MUST SUCCEED
        logger.info(f"[{domain}] Step 6c: Clicking Verify button")
        screenshot(driver, "08_before_verify", domain)
        
        # The Verify button is at the bottom of the page - scroll to it first
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
        
        # Try multiple methods to click Verify
        verify_clicked = False
        
        # Method 1: Find button with exact text
        try:
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                if btn.text.strip().lower() == "verify":
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", btn)
                    verify_clicked = True
                    logger.info(f"[{domain}] Clicked Verify button (method 1)")
                    break
        except Exception as e:
            logger.warning(f"Method 1 failed: {e}")
        
        # Method 2: XPath with contains
        if not verify_clicked:
            try:
                btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Verify')]")
                driver.execute_script("arguments[0].click();", btn)
                verify_clicked = True
                logger.info(f"[{domain}] Clicked Verify button (method 2)")
            except Exception as e:
                logger.warning(f"Method 2 failed: {e}")
        
        # Method 3: CSS selector for primary button
        if not verify_clicked:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, "button.ms-Button--primary")
                driver.execute_script("arguments[0].click();", btn)
                verify_clicked = True
                logger.info(f"[{domain}] Clicked Verify button (method 3)")
            except Exception as e:
                logger.warning(f"Method 3 failed: {e}")
        
        # Method 4: Find by aria-label
        if not verify_clicked:
            try:
                btn = driver.find_element(By.XPATH, "//button[@aria-label='Verify']")
                driver.execute_script("arguments[0].click();", btn)
                verify_clicked = True
                logger.info(f"[{domain}] Clicked Verify button (method 4)")
            except Exception as e:
                logger.warning(f"Method 4 failed: {e}")
        
        # IF STILL NOT CLICKED - STOP AND RETURN ERROR
        if not verify_clicked:
            logger.error(f"[{domain}] FAILED TO CLICK VERIFY BUTTON!")
            screenshot(driver, "error_verify_not_clicked", domain)
            result["error"] = "Could not click Verify button"
            update_status_file(domain, "verification", "failed", "Could not click Verify button")
            logger.error(f"[{domain}] FAILED - cleaning up browser")
            _cleanup_driver(driver)
            _clear_active_driver(driver)
            return result
        
        # ===== VERIFICATION RESULT DETECTION WITH RETRY =====
        # M365 shows "Verifying your domain..." spinner first, then the actual result.
        # We MUST wait for the spinner to finish before checking the result.
        # If verification fails, M365 shows "Try again" button - we retry with increasing waits.
        
        MAX_VERIFY_RETRIES = 10  # Up to 10 retry attempts
        VERIFY_RETRY_WAIT = 60   # Wait 60 seconds between retries for DNS propagation
        
        for verify_attempt in range(MAX_VERIFY_RETRIES + 1):
            if verify_attempt > 0:
                logger.info(f"[{domain}] Verification retry {verify_attempt}/{MAX_VERIFY_RETRIES} - waiting {VERIFY_RETRY_WAIT}s for DNS propagation...")
                time.sleep(VERIFY_RETRY_WAIT)
                
                # Click "Try again" button
                try_again_clicked = False
                try:
                    buttons = driver.find_elements(By.TAG_NAME, "button")
                    for btn in buttons:
                        btn_text = btn.text.strip().lower()
                        if "try again" in btn_text or "verify" in btn_text:
                            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                            time.sleep(0.5)
                            driver.execute_script("arguments[0].click();", btn)
                            try_again_clicked = True
                            logger.info(f"[{domain}] Clicked '{btn.text.strip()}' button for retry")
                            break
                except Exception as e:
                    logger.warning(f"[{domain}] Could not click Try again: {e}")
                
                if not try_again_clicked:
                    logger.warning(f"[{domain}] Could not find Try again/Verify button for retry")
                    screenshot(driver, f"error_no_retry_button_{verify_attempt}", domain)
                    break
            
            # Wait for "Verifying your domain..." spinner to FINISH (up to 90 seconds)
            logger.info(f"[{domain}] Waiting for verification to complete (attempt {verify_attempt + 1})...")
            verification_done = False
            for wait_sec in range(90):
                time.sleep(1)
                try:
                    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                except:
                    continue
                
                # Still showing spinner? Keep waiting
                if "verifying your domain" in page_text or "verifying..." in page_text:
                    if wait_sec % 10 == 0:
                        logger.info(f"[{domain}] Still verifying... ({wait_sec}s)")
                    continue
                
                # Spinner gone - check actual result
                verification_done = True
                break
            
            if not verification_done:
                logger.warning(f"[{domain}] Verification spinner still showing after 90s")
                # Get page text anyway
                try:
                    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                except:
                    page_text = ""
            
            screenshot(driver, f"09_after_verify_{verify_attempt}", domain)
            logger.info(f"[{domain}] Post-verify page text (first 500 chars): {page_text[:500]}")
            
            # ===== CHECK FOR POSITIVE SUCCESS INDICATORS =====
            success_indicators = [
                "how do you want to connect",
                "connect domain",
                "add dns records",
                "domain setup is complete",
                "exchange online",
                "mail protection"
            ]
            
            if any(indicator in page_text for indicator in success_indicators):
                result["verified"] = True
                update_status_file(domain, "verification", "complete", "Domain ownership verified")
                logger.info(f"[{domain}] Verification SUCCESS confirmed! (attempt {verify_attempt + 1})")
                break
            
            # ===== CHECK FOR FAILURE INDICATORS =====
            failure_indicators = [
                "didn't detect",
                "try again",
                "couldn't verify",
                "couldn't find",
                "record not detected",
                "we didn't detect",
                "add a record to verify"
            ]
            
            if any(indicator in page_text for indicator in failure_indicators):
                logger.warning(f"[{domain}] Verification FAILED (attempt {verify_attempt + 1}) - M365 didn't detect DNS record yet")
                screenshot(driver, f"verify_failed_{verify_attempt}", domain)
                
                if verify_attempt >= MAX_VERIFY_RETRIES:
                    logger.error(f"[{domain}] Verification failed after {MAX_VERIFY_RETRIES + 1} attempts")
                    result["error"] = f"Domain verification failed after {MAX_VERIFY_RETRIES + 1} attempts - M365 could not detect TXT record"
                    update_status_file(domain, "verification", "failed", result["error"])
                    _cleanup_driver(driver)
                    _clear_active_driver(driver)
                    return result
                # Will retry at top of loop
                continue
            
            # ===== UNKNOWN STATE =====
            # Page changed to something we don't recognize - log it and assume success if not showing errors
            logger.warning(f"[{domain}] Unknown page state after verification. Checking further...")
            if "verify" in page_text and ("own" in page_text or "ownership" in page_text):
                # Still on verification page but no clear failure message
                logger.warning(f"[{domain}] Still appears to be on verification page")
                if verify_attempt >= MAX_VERIFY_RETRIES:
                    result["error"] = "Verification did not complete - unknown page state"
                    _cleanup_driver(driver)
                    _clear_active_driver(driver)
                    return result
                continue
            else:
                # Page changed to something new - likely success
                result["verified"] = True
                update_status_file(domain, "verification", "complete", "Domain ownership verified")
                logger.info(f"[{domain}] Verification appears successful (page moved past verification)")
                break
    
    # ===== STEP 7: WAIT FOR AND HANDLE "HOW DO YOU WANT TO CONNECT" PAGE =====
    # This page appears after verification OR if domain was already verified
    
    logger.info(f"[{domain}] Step 7: Waiting for 'Connect domain' page...")
    
    # Wait up to 30 seconds for the connect page to appear
    connect_page_found = False
    for attempt in range(15):  # 15 attempts x 2 seconds = 30 seconds
        time.sleep(2)
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        screenshot(driver, f"07_waiting_connect_{attempt}", domain)
        
        if "how do you want to connect" in page_text:
            connect_page_found = True
            logger.info(f"[{domain}] Found 'Connect domain' page after {(attempt+1)*2} seconds")
            break
        elif "add dns records" in page_text:
            # Already past connect page - that's fine
            logger.info(f"[{domain}] Already on DNS records page")
            break
        elif "domain setup is complete" in page_text:
            # Already complete!
            logger.info(f"[{domain}] Domain already complete!")
            result["success"] = True
            result["verified"] = True
            result["dns_configured"] = True
            # Continue to end for proper cleanup
            break
        
        logger.info(f"[{domain}] Waiting for connect page... attempt {attempt+1}/15")
    
    # Take screenshot of current state
    screenshot(driver, "08_connect_page", domain)
    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    
    # Handle "How do you want to connect your domain" page
    if "how do you want to connect" in page_text:
        logger.info(f"[{domain}] Step 7a: On 'Connect domain' page - clicking 'More options'")
        
        # Click "More options" link
        more_clicked = False
        for xpath in [
            "//a[contains(text(), 'More options')]",
            "//span[contains(text(), 'More options')]",
            "//*[contains(text(), 'More options')]"
        ]:
            try:
                elem = driver.find_element(By.XPATH, xpath)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", elem)
                more_clicked = True
                logger.info(f"[{domain}] Clicked 'More options'")
                break
            except:
                continue
        
        if not more_clicked:
            logger.warning(f"[{domain}] Could not click 'More options' - may already be expanded")
        
        time.sleep(2)
        screenshot(driver, "09_more_options_expanded", domain)
        
        # Select "Add your own DNS records" radio button
        logger.info(f"[{domain}] Step 7b: Selecting 'Add your own DNS records'")
        dns_selected = False
        for xpath in [
            "//input[@type='radio'][following-sibling::*[contains(text(), 'Add your own')]]",
            "//input[@type='radio'][..//*[contains(text(), 'Add your own')]]",
            "//*[contains(text(), 'Add your own DNS records')]",
            "//label[contains(., 'Add your own')]",
            "//span[contains(text(), 'Add your own DNS records')]"
        ]:
            try:
                elem = driver.find_element(By.XPATH, xpath)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", elem)
                dns_selected = True
                logger.info(f"[{domain}] Selected 'Add your own DNS records'")
                break
            except:
                continue
        
        if not dns_selected:
            logger.warning(f"[{domain}] Could not select 'Add your own DNS records'")
        
        time.sleep(1)
        screenshot(driver, "10_dns_option_selected", domain)
        
        # Click Continue
        logger.info(f"[{domain}] Step 7c: Clicking Continue")
        continue_clicked = False
        try:
            btns = driver.find_elements(By.TAG_NAME, "button")
            for btn in btns:
                if "continue" in btn.text.lower():
                    driver.execute_script("arguments[0].click();", btn)
                    continue_clicked = True
                    logger.info(f"[{domain}] Clicked Continue")
                    break
        except:
            pass
        
        if not continue_clicked:
            logger.error(f"[{domain}] Could not click Continue on connect page!")
            result["error"] = "Could not click Continue on connect page"
            logger.error(f"[{domain}] FAILED - cleaning up browser")
            _cleanup_driver(driver)
            _clear_active_driver(driver)
            return result
        
        # Wait for DNS records page to load
        logger.info(f"[{domain}] Waiting for DNS records page...")
        time.sleep(5)
    
    # ===== STEP 8: WAIT FOR DNS RECORDS PAGE =====
    logger.info(f"[{domain}] Step 8: Waiting for DNS records page to load...")
    
    # DNS page indicators - check for any of these
    dns_indicators = [
        "add dns records",
        "mx records", 
        "exchange and exchange online",
        "dns hosting provider",
        "cname records",
        "txt records",
        "mail protection",
        "exchange online",
        "points to address"
    ]
    
    # Wait up to 30 seconds for DNS records page
    dns_page_found = False
    for attempt in range(15):
        time.sleep(2)
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        screenshot(driver, f"08_dns_page_wait_{attempt}", domain)
        
        # Check if any DNS indicator is present
        if any(indicator in page_text for indicator in dns_indicators):
            dns_page_found = True
            logger.info(f"[{domain}] Found DNS records page after {(attempt+1)*2} seconds")
            break
        elif "domain setup is complete" in page_text:
            logger.info(f"[{domain}] Domain already complete!")
            result["success"] = True
            result["verified"] = True
            result["dns_configured"] = True
            # Continue to end for proper cleanup
            break
        else:
            logger.info(f"[{domain}] Waiting for DNS page... attempt {attempt+1}/15")
    
    if not dns_page_found:
        # Take screenshot and continue anyway - maybe we can still find DNS values
        logger.warning(f"[{domain}] DNS page not detected, but continuing anyway...")
        screenshot(driver, "warning_dns_page_not_detected", domain)
    
    screenshot(driver, "09_dns_records_page", domain)
    update_status_file(domain, "dns_setup", "in_progress", "Configuring DNS records")
    logger.info(f"[{domain}] Step 8: Now on DNS records page - expanding all sections")
    
    # ===== STEP 8a: EXPAND ALL DNS RECORD SECTIONS =====
    logger.info(f"[{domain}] Step 8a: Expanding DNS record sections")
    
    # Scroll to top first
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)
    
    # The expand buttons have aria-label like "Expand MX Records"
    sections_to_expand = [
        ("MX", "Expand MX Records"),
        ("CNAME", "Expand CNAME Records"),
        ("TXT", "Expand TXT Records")
    ]
    
    for section_name, aria_label in sections_to_expand:
        logger.info(f"[{domain}] Expanding {section_name} section...")
        try:
            # Find button by aria-label (contains to handle special chars)
            btn = driver.find_element(By.XPATH, f"//button[contains(@aria-label, 'Expand') and contains(@aria-label, '{section_name}')]")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.3)
            driver.execute_script("arguments[0].click();", btn)
            logger.info(f"[{domain}] Expanded {section_name} section")
            time.sleep(1)
        except Exception as e:
            logger.warning(f"[{domain}] Could not expand {section_name}: {e}")
    
    screenshot(driver, "10_sections_expanded", domain)
    
    # ===== STEP 8b: EXPAND ADVANCED OPTIONS =====
    logger.info(f"[{domain}] Step 8b: Expanding Advanced options")
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)
    
    try:
        # Advanced options might also be a button or clickable div
        adv = driver.find_element(By.XPATH, "//button[contains(@aria-label, 'Advanced')] | //*[contains(text(), 'Advanced options')]")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", adv)
        time.sleep(0.3)
        driver.execute_script("arguments[0].click();", adv)
        logger.info(f"[{domain}] Expanded Advanced options")
        time.sleep(1)
    except Exception as e:
        logger.warning(f"[{domain}] Could not expand Advanced options: {e}")
    
    screenshot(driver, "11_advanced_expanded", domain)
    
    # ===== STEP 8c: CHECK DKIM CHECKBOX =====
    logger.info(f"[{domain}] Step 8c: Checking DKIM checkbox")
    try:
        # Find the DKIM checkbox by its label
        dkim_checkbox = driver.find_element(By.XPATH, "//input[@type='checkbox' and following-sibling::*[contains(text(), 'DKIM')]] | //input[@type='checkbox' and ..//*[contains(text(), 'DKIM')]]")
        if not dkim_checkbox.is_selected():
            driver.execute_script("arguments[0].click();", dkim_checkbox)
            logger.info(f"[{domain}] Checked DKIM checkbox")
        else:
            logger.info(f"[{domain}] DKIM already checked")
        time.sleep(3)  # Wait for DKIM records to load
    except:
        # Try clicking the label instead
        try:
            dkim_label = driver.find_element(By.XPATH, "//*[contains(text(), 'DomainKeys Identified Mail')]")
            driver.execute_script("arguments[0].click();", dkim_label)
            logger.info(f"[{domain}] Clicked DKIM label")
            time.sleep(3)
        except Exception as e:
            logger.warning(f"[{domain}] Could not check DKIM: {e}")
    
    screenshot(driver, "12_dkim_checked", domain)
    
    # ===== STEP 8d: EXPAND DKIM CNAME RECORDS (appears after checking DKIM) =====
    logger.info(f"[{domain}] Step 8d: Expanding DKIM CNAME Records")
    time.sleep(2)
    
    try:
        # Look for "CNAME Records (2)" which contains DKIM records
        btn = driver.find_element(By.XPATH, "//button[contains(@aria-label, 'Expand') and contains(@aria-label, 'CNAME')]")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
        time.sleep(0.3)
        driver.execute_script("arguments[0].click();", btn)
        logger.info(f"[{domain}] Expanded DKIM CNAME section")
        time.sleep(1)
    except Exception as e:
        logger.warning(f"[{domain}] Could not expand DKIM CNAME: {e}")
    
    screenshot(driver, "13_all_expanded", domain)
    
    # ===== STEP 8e: EXTRACT DNS VALUES =====
    logger.info(f"[{domain}] Step 8e: Extracting DNS values")
    
    # Scroll through page to ensure all content is visible
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.5)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(0.5)
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.5)
    
    page_text = driver.find_element(By.TAG_NAME, "body").text
    logger.info(f"[{domain}] Page text length: {len(page_text)}")
    
    # Extract MX and SPF values
    mx_match = re.search(r'([a-zA-Z0-9-]+\.mail\.protection\.outlook\.com)', page_text)
    spf_match = re.search(r'(v=spf1[^\n"<>]+)', page_text)
    
    logger.info(f"[{domain}] MX: {mx_match.group(1) if mx_match else 'NOT FOUND'}")
    logger.info(f"[{domain}] SPF: {spf_match.group(1) if spf_match else 'NOT FOUND'}")
    
    # ===== EXTRACT DKIM VALUES =====
    # The DKIM CNAME targets look like: selector1-domain-tld._domainkey.tenant.p-v1.dkim.mail.microsoft
    # We need to get the FULL target value, not just "selector1._domainkey" (which is the NAME)
    
    # Extract DKIM selector1 target - look for the full CNAME target
    # Pattern: selector1-something._domainkey.something.dkim.mail.microsoft
    sel1_match = re.search(r'(selector1-[a-zA-Z0-9-]+\._domainkey\.[a-zA-Z0-9.-]+\.dkim\.mail\.microsoft)', page_text)
    if not sel1_match:
        # Alternative pattern for older format
        sel1_match = re.search(r'(selector1-[a-zA-Z0-9-]+\._domainkey\.[a-zA-Z0-9.-]+\.onmicrosoft\.com)', page_text)
    
    # Extract DKIM selector2 target
    sel2_match = re.search(r'(selector2-[a-zA-Z0-9-]+\._domainkey\.[a-zA-Z0-9.-]+\.dkim\.mail\.microsoft)', page_text)
    if not sel2_match:
        sel2_match = re.search(r'(selector2-[a-zA-Z0-9-]+\._domainkey\.[a-zA-Z0-9.-]+\.onmicrosoft\.com)', page_text)
    
    # Log what we found
    if sel1_match:
        logger.info(f"[{domain}] DKIM selector1 target: {sel1_match.group(1)}")
    else:
        logger.warning(f"[{domain}] DKIM selector1 NOT FOUND")
        
    if sel2_match:
        logger.info(f"[{domain}] DKIM selector2 target: {sel2_match.group(1)}")
    else:
        logger.warning(f"[{domain}] DKIM selector2 NOT FOUND")
    
    # ===== STEP 8i: ADD ALL RECORDS TO CLOUDFLARE =====
    logger.info(f"[{domain}] Step 8i: Cleaning up conflicting DNS records before adding M365 records")
    cleanup_before_dns_setup(zone_id)
    
    logger.info(f"[{domain}] Step 8i: Adding DNS records to Cloudflare")
    
    if mx_match:
        mx_target = mx_match.group(1)
        logger.info(f"[{domain}] Adding MX: {mx_target}")
        add_mx(zone_id, mx_target, 0)
        # Store in result for database update
        result["mx_value"] = mx_target
    
    if spf_match:
        spf_value = spf_match.group(1).strip()
        logger.info(f"[{domain}] Adding SPF: {spf_value}")
        add_spf(zone_id, spf_value)
        # Store in result for database update
        result["spf_value"] = spf_value
    
    logger.info(f"[{domain}] Adding autodiscover CNAME")
    add_cname(zone_id, "autodiscover", "autodiscover.outlook.com")
    
    # Add DKIM CNAMEs with FULL target values
    if sel1_match:
        dkim1_target = sel1_match.group(1)
        logger.info(f"[{domain}] Adding DKIM: selector1._domainkey -> {dkim1_target}")
        add_cname(zone_id, "selector1._domainkey", dkim1_target)
        # Store in result for database update
        result["dkim_selector1_cname"] = dkim1_target
    
    if sel2_match:
        dkim2_target = sel2_match.group(1)
        logger.info(f"[{domain}] Adding DKIM: selector2._domainkey -> {dkim2_target}")
        add_cname(zone_id, "selector2._domainkey", dkim2_target)
        # Store in result for database update
        result["dkim_selector2_cname"] = dkim2_target
    
    # Only mark dns_configured if we actually found and added the critical records
    if mx_match and spf_match:
        result["dns_configured"] = True
        update_status_file(domain, "dns_setup", "complete", "DNS records added to Cloudflare")
    else:
        missing = []
        if not mx_match:
            missing.append("MX")
        if not spf_match:
            missing.append("SPF")
        if not sel1_match:
            missing.append("DKIM selector1")
        if not sel2_match:
            missing.append("DKIM selector2")
        logger.error(f"[{domain}] DNS setup INCOMPLETE - missing values: {', '.join(missing)}")
        result["dns_configured"] = False
        update_status_file(domain, "dns_setup", "partial", f"Missing DNS values: {', '.join(missing)}")
    
    # ===== STEP 8f: WAIT FOR DNS PROPAGATION =====
    update_status_file(domain, "finalizing", "in_progress", "Waiting for DNS propagation")
    logger.info(f"[{domain}] Step 8f: Waiting 30 seconds for DNS propagation...")
    time.sleep(30)
    
    screenshot(driver, "14_before_continue", domain)
    
    # ===== STEP 9: CLICK CONTINUE AND COMPLETE WITH EXTENDED RETRY =====
    # 10 attempts x 2 minute intervals = 20 minutes total wait time
    logger.info(f"[{domain}] Step 9: Clicking Continue to finish (max 10 attempts, 2 min intervals)")
    
    MAX_CONTINUE_ATTEMPTS = 10
    CONTINUE_RETRY_INTERVAL = 120  # 2 minutes
    
    for attempt in range(MAX_CONTINUE_ATTEMPTS):
        logger.info(f"[{domain}] Continue attempt {attempt + 1}/{MAX_CONTINUE_ATTEMPTS}")
        
        try:
            # Scroll to bottom where Continue button is
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
            
            # Try to find and click Continue button
            cont_btn = None
            
            # Method 1: XPath with text
            try:
                cont_btn = driver.find_element(By.XPATH, "//button[contains(., 'Continue')]")
            except:
                pass
            
            # Method 2: Primary button
            if not cont_btn:
                try:
                    cont_btn = driver.find_element(By.CSS_SELECTOR, "button.ms-Button--primary")
                except:
                    pass
            
            # Method 3: Find all buttons and look for Continue text
            if not cont_btn:
                try:
                    buttons = driver.find_elements(By.TAG_NAME, "button")
                    for btn in buttons:
                        if "continue" in btn.text.lower():
                            cont_btn = btn
                            break
                except:
                    pass
            
            if cont_btn:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", cont_btn)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", cont_btn)
                logger.info(f"[{domain}] Clicked Continue")
            else:
                logger.warning(f"[{domain}] Continue button not found")
        except Exception as e:
            logger.warning(f"[{domain}] Error clicking Continue: {e}")
        
        # Wait for page to process (45 seconds for DNS verification)
        time.sleep(45)
        screenshot(driver, f"15_after_continue_{attempt}", domain)
        
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        
        # Check if we're done
        if "complete" in page_text or "domain setup is complete" in page_text:
            logger.info(f"[{domain}] SUCCESS - Setup complete on attempt {attempt + 1}!")
            result["success"] = True
            break
        
        # Check for error messages
        if "error" in page_text or "failed" in page_text or "couldn't verify" in page_text:
            logger.warning(f"[{domain}] Verification error detected, will retry...")
        
        # Check if still on DNS page (verification in progress)
        if "add dns records" in page_text or "verifying" in page_text:
            logger.info(f"[{domain}] Still verifying DNS, waiting {CONTINUE_RETRY_INTERVAL}s before retry...")
            
            # Only wait if not the last attempt
            if attempt < MAX_CONTINUE_ATTEMPTS - 1:
                time.sleep(CONTINUE_RETRY_INTERVAL)
            else:
                logger.warning(f"[{domain}] Max attempts reached, DNS verification may have failed")
        else:
            # Page changed to something else - might be done or error
            logger.info(f"[{domain}] Page state changed, checking result...")
            break
    
    # Log the final outcome of the retry loop
    if not result["success"]:
        logger.warning(f"[{domain}] Continue/verify did not complete after {MAX_CONTINUE_ATTEMPTS} attempts")
    
    # ===== STEP 10: CLICK DONE =====
    screenshot(driver, "15_final", domain)
    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    
    if "complete" in page_text or "domain setup is complete" in page_text:
        logger.info(f"[{domain}] Clicking Done button")
        try:
            btns = driver.find_elements(By.TAG_NAME, "button")
            for btn in btns:
                if "done" in btn.text.lower():
                    driver.execute_script("arguments[0].click();", btn)
                    logger.info(f"[{domain}] Clicked Done")
                    break
        except:
            pass
        # Only set success=True if we actually reached completion page
        result["success"] = True
    else:
        # Did NOT reach completion - check if DNS was at least configured
        if result["dns_configured"] and result["verified"]:
            logger.warning(f"[{domain}] DNS configured but wizard did not reach 'complete' page - marking as partial success")
            result["success"] = True  # Still consider success if DNS is done
        else:
            logger.error(f"[{domain}] Did not reach completion page and DNS not fully configured")
            if not result["error"]:
                result["error"] = "Setup did not reach completion page"
    
    # ===== FINAL STATUS UPDATE =====
    if result["success"]:
        update_status_file(domain, "complete", "complete", "Domain setup completed successfully")
    else:
        update_status_file(domain, "complete", "failed", result.get("error", "Unknown error"))
    
    # ===== FINAL RESULT LOGGING =====
    logger.info(f"[{domain}] ==========================================")
    logger.info(f"[{domain}] FINAL RESULT:")
    logger.info(f"[{domain}]   Success: {result['success']}")
    logger.info(f"[{domain}]   Verified: {result['verified']}")
    logger.info(f"[{domain}]   DNS Configured: {result['dns_configured']}")
    logger.info(f"[{domain}]   Error: {result.get('error', 'None')}")
    logger.info(f"[{domain}] ==========================================")
    
    # Take final screenshot
    screenshot(driver, "16_final_complete", domain)
    
    # Brief pause so completion page is visible
    time.sleep(3)
    
    # ===== CLOSE BROWSER =====
    _cleanup_driver(driver)
    _clear_active_driver(driver)
    driver = None
    logger.info(f"[{domain}] Browser closed and profile cleaned up")
    
    return result


async def enable_org_smtp_auth(
    admin_email: str,
    admin_password: str,
    totp_secret: Optional[str],
    domain: str,
) -> dict:
    """
    Enable SMTP Auth at the organization level in Exchange Admin Center.

    This is Step 7 of the setup wizard.
    
    NAVIGATION PATH (based on actual UI):
    1. Login to M365
    2. Go to Exchange Admin Center: https://admin.cloud.microsoft.com/exchange#/settings
    3. Click on "Mail flow" row to open the flyout panel
    4. In the flyout, find and UNCHECK "Turn off SMTP AUTH protocol for your organization"
    5. Click Save

    Args:
        admin_email: e.g. "admin@TenantName.onmicrosoft.com"
        admin_password: The admin password from Step 4
        totp_secret: The TOTP secret from Step 4
        domain: e.g. "loancatermail13.info" (for logging)

    Returns:
        {
            "success": bool,
            "smtp_auth_enabled": bool,
            "error": str or None
        }
    """

    driver = None
    result = {
        "success": False,
        "smtp_auth_enabled": False,
        "error": None,
    }

    try:
        logger.info(f"[{domain}] Step 7: Initializing browser")
        worker = BrowserWorker(worker_id=f"step7-{uuid.uuid4()}", headless=True)
        driver = worker._create_driver()
        driver.implicitly_wait(10)
        driver.set_page_load_timeout(120)

        # === LOGIN TO M365 (reuse existing login flow) ===
        logger.info(f"[{domain}] Step 7: Logging into M365 Admin Portal...")
        _login_with_mfa(
            driver=driver,
            admin_email=admin_email,
            admin_password=admin_password,
            totp_secret=totp_secret,
            domain=domain,
        )
        _save_screenshot(driver, domain, "step7_login_complete")
        time.sleep(3)

        # =================================================================
        # STEP 7A: NAVIGATE TO EXCHANGE ADMIN CENTER SETTINGS PAGE
        # =================================================================
        # The correct URL is: https://admin.cloud.microsoft.com/exchange#/settings
        # This shows a list with: List view preference, Mail flow, Hybrid setup
        logger.info(f"[{domain}] Step 7: Opening Exchange Admin Center Settings...")
        # Dismiss MFA setup interrupt if the admin portal threw it at us
        try:
            dismiss_mfa_setup_interrupt(driver, domain)
        except Exception:
            pass
        driver.get("https://admin.cloud.microsoft.com/exchange#/settings")
        wait_for_page_load(driver, timeout=60)
        time.sleep(8)  # Extra time for Settings page to fully load
        _save_screenshot(driver, domain, "step7_settings_page")
        
        # Log current URL and page state for debugging
        logger.info(f"[{domain}] Step 7: Current URL: {driver.current_url}")
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        logger.info(f"[{domain}] Step 7: Page contains 'settings': {'settings' in page_text}")
        logger.info(f"[{domain}] Step 7: Page contains 'mail flow': {'mail flow' in page_text}")

        # Dismiss any Teaching Bubbles / popups
        try:
            bubbles = driver.find_elements(By.XPATH, "//div[contains(@class, 'ms-TeachingBubble')]//button")
            for bubble in bubbles:
                safe_click(driver, bubble, "Teaching bubble")
                time.sleep(0.5)
        except Exception:
            pass

        # =================================================================
        # STEP 7B: CLICK ON "MAIL FLOW" ROW TO OPEN FLYOUT
        # =================================================================
        # The Settings page has a list/table with clickable rows
        # We need to click the ROW itself, not just a text span inside it
        logger.info(f"[{domain}] Step 7: Looking for 'Mail flow' row to click...")
        
        flyout_opened = False
        max_click_attempts = 3
        
        for click_attempt in range(max_click_attempts):
            logger.info(f"[{domain}] Step 7: Click attempt {click_attempt + 1}/{max_click_attempts}")
            
            mail_flow_clicked = False
            
            # Priority selectors - focus on parent row elements, not child spans/divs
            mail_flow_selectors = [
                # TABLE ROW containing Mail flow - highest priority
                "//tr[.//td[contains(text(), 'Mail flow')]]",
                "//tr[contains(., 'Mail flow')]",
                # Fluent UI DetailsRow
                "//div[@role='row'][.//span[contains(text(), 'Mail flow')]]",
                "//div[contains(@class, 'ms-DetailsRow')][.//span[contains(text(), 'Mail flow')]]",
                "//div[@data-automationid='DetailsRow'][.//span[contains(text(), 'Mail flow')]]",
                # Table cell that's clickable
                "//td[contains(text(), 'Mail flow')]",
                # Link or button that opens Mail flow
                "//a[text()='Mail flow']",
                "//button[contains(text(), 'Mail flow')]",
                # Span but get its clickable parent
                "//span[text()='Mail flow']/ancestor::tr",
                "//span[text()='Mail flow']/ancestor::div[@role='row']",
            ]
            
            for selector in mail_flow_selectors:
                try:
                    elem = driver.find_element(By.XPATH, selector)
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
                    time.sleep(0.5)
                    _save_screenshot(driver, domain, f"step7_found_mailflow_attempt{click_attempt}")
                    
                    # Try multiple click methods
                    click_success = False
                    
                    # Method 1: ActionChains with move and click
                    try:
                        ActionChains(driver).move_to_element(elem).click().perform()
                        click_success = True
                        logger.info(f"[{domain}] Step 7: ActionChains click on: {selector}")
                    except Exception as e:
                        logger.debug(f"ActionChains failed: {e}")
                    
                    # Method 2: JavaScript click
                    if not click_success:
                        try:
                            driver.execute_script("arguments[0].click();", elem)
                            click_success = True
                            logger.info(f"[{domain}] Step 7: JS click on: {selector}")
                        except Exception as e:
                            logger.debug(f"JS click failed: {e}")
                    
                    # Method 3: Regular click
                    if not click_success:
                        try:
                            elem.click()
                            click_success = True
                            logger.info(f"[{domain}] Step 7: Regular click on: {selector}")
                        except Exception as e:
                            logger.debug(f"Regular click failed: {e}")
                    
                    # Method 4: Double-click (some UIs need this)
                    if not click_success:
                        try:
                            ActionChains(driver).double_click(elem).perform()
                            click_success = True
                            logger.info(f"[{domain}] Step 7: Double-click on: {selector}")
                        except Exception as e:
                            logger.debug(f"Double-click failed: {e}")
                    
                    if click_success:
                        mail_flow_clicked = True
                        break
                        
                except Exception as e:
                    logger.debug(f"[{domain}] Selector failed: {selector} - {e}")
                    continue
            
            # If XPath selectors didn't work, try JavaScript approach
            if not mail_flow_clicked:
                logger.warning(f"[{domain}] Step 7: Trying JS to find and click Mail flow...")
                try:
                    js_clicked = driver.execute_script("""
                        // Strategy 1: Find the table row containing Mail flow
                        var rows = document.querySelectorAll('tr');
                        for (var row of rows) {
                            if (row.textContent.includes('Mail flow') && 
                                row.textContent.includes('sending and receiving')) {
                                row.click();
                                return 'clicked_tr';
                            }
                        }
                        
                        // Strategy 2: Find Fluent UI DetailsRow
                        var detailRows = document.querySelectorAll('[role="row"], .ms-DetailsRow');
                        for (var row of detailRows) {
                            if (row.textContent.includes('Mail flow')) {
                                row.click();
                                return 'clicked_detailrow';
                            }
                        }
                        
                        // Strategy 3: Find card/list item
                        var items = document.querySelectorAll('[role="listitem"], [role="option"], .ms-List-cell');
                        for (var item of items) {
                            if (item.textContent.includes('Mail flow')) {
                                item.click();
                                return 'clicked_listitem';
                            }
                        }
                        
                        // Strategy 4: Find any clickable element containing Mail flow
                        var links = document.querySelectorAll('a, button, [role="button"]');
                        for (var link of links) {
                            if (link.textContent.includes('Mail flow')) {
                                link.click();
                                return 'clicked_link';
                            }
                        }
                        
                        // Strategy 5: Find td and simulate click on parent tr
                        var tds = document.querySelectorAll('td');
                        for (var td of tds) {
                            if (td.textContent.trim() === 'Mail flow') {
                                var tr = td.closest('tr');
                                if (tr) {
                                    tr.click();
                                    return 'clicked_parent_tr';
                                }
                                td.click();
                                return 'clicked_td';
                            }
                        }
                        
                        return 'not_found';
                    """)
                    if js_clicked != 'not_found':
                        mail_flow_clicked = True
                        logger.info(f"[{domain}] Step 7: JS clicked Mail flow: {js_clicked}")
                except Exception as e:
                    logger.error(f"[{domain}] Step 7: JS fallback failed: {e}")
            
            if not mail_flow_clicked:
                logger.warning(f"[{domain}] Step 7: Could not click Mail flow on attempt {click_attempt + 1}")
                time.sleep(2)
                continue
            
            # Wait for flyout to open and verify it opened
            time.sleep(3)
            _save_screenshot(driver, domain, f"step7_after_click_attempt{click_attempt}")
            
            # Check if flyout opened by looking for SMTP content
            page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            if "mail flow settings" in page_text or "turn off smtp" in page_text or "smtp auth" in page_text:
                flyout_opened = True
                logger.info(f"[{domain}] Step 7: Flyout opened successfully on attempt {click_attempt + 1}")
                break
            else:
                logger.warning(f"[{domain}] Step 7: Click successful but flyout not detected, retrying...")
                time.sleep(2)
        
        if not flyout_opened:
            result["error"] = "Could not open Mail flow settings flyout"
            logger.error(f"[{domain}] Step 7 FAILED: Flyout did not open after {max_click_attempts} attempts")
            _save_screenshot(driver, domain, "step7_flyout_not_opened")
            return result
        
        _save_screenshot(driver, domain, "step7_flyout_opened")
        
        # =================================================================
        # STEP 7C: WAIT FOR FLYOUT CONTENT TO FULLY LOAD
        # =================================================================
        logger.info(f"[{domain}] Step 7: Waiting for Mail flow settings content to load...")
        
        # Wait for flyout content to fully load
        content_loaded = False
        for attempt in range(15):  # Wait up to 15 seconds
            time.sleep(1)
            page_text = driver.find_element(By.TAG_NAME, "body").text
            if "Turn off SMTP AUTH" in page_text or "SMTP AUTH protocol" in page_text:
                content_loaded = True
                logger.info(f"[{domain}] Step 7: Flyout content loaded after {attempt + 1}s")
                break
            logger.debug(f"[{domain}] Step 7: Waiting for SMTP setting... attempt {attempt + 1}/15")
        
        if not content_loaded:
            logger.warning(f"[{domain}] Step 7: SMTP setting text not found in page, trying anyway...")
        
        _save_screenshot(driver, domain, "step7_flyout_loaded")
        
        # Log page content for debugging
        page_text = driver.find_element(By.TAG_NAME, "body").text
        logger.info(f"[{domain}] Step 7: Page contains 'SMTP AUTH': {'SMTP AUTH' in page_text}")
        logger.info(f"[{domain}] Step 7: Page contains 'Turn off SMTP': {'Turn off SMTP' in page_text}")

        # =================================================================
        # STEP 7D: FIND AND HANDLE SMTP AUTH CHECKBOX
        # =================================================================
        # IMPORTANT: The checkbox is "Turn off SMTP AUTH protocol for your organization"
        #   - CHECKED = SMTP AUTH is DISABLED (turned off)
        #   - UNCHECKED = SMTP AUTH is ENABLED (turned on)
        # We ONLY want to UNCHECK (if checked), NEVER re-check on reruns!
        # =================================================================
        smtp_auth_enabled = False
        checkbox_found = False
        
        # The checkbox HTML is: <label class="ms-Checkbox-label label-800" for="checkbox-XXXX">
        # We need to find the checkbox input associated with "Turn off SMTP AUTH"
        smtp_checkbox_selectors = [
            # Fluent UI Checkbox - find input by label text
            "//label[contains(@class, 'ms-Checkbox-label')][contains(text(), 'Turn off SMTP')]/..//input[@type='checkbox']",
            "//label[contains(text(), 'Turn off SMTP')]/preceding-sibling::input[@type='checkbox']",
            "//label[contains(text(), 'Turn off SMTP AUTH')]/../input[@type='checkbox']",
            # Parent div approach
            "//div[contains(@class, 'ms-Checkbox')][.//label[contains(text(), 'SMTP')]]//input",
            "//div[.//label[contains(text(), 'Turn off SMTP')]]//input[@type='checkbox']",
            # Direct checkbox near SMTP text
            "//input[@type='checkbox'][following-sibling::label[contains(text(), 'SMTP')]]",
            "//input[@type='checkbox'][../label[contains(text(), 'Turn off SMTP')]]",
            # By Security section
            "//div[.//text()[contains(., 'Security')]]//input[@type='checkbox'][1]",
        ]
        
        for selector in smtp_checkbox_selectors:
            try:
                checkbox = driver.find_element(By.XPATH, selector)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", checkbox)
                time.sleep(0.5)
                checkbox_found = True
                
                # Check current state - handle both boolean and string "true"/"false"
                checked_attr = checkbox.get_attribute("checked")
                is_checked = checkbox.is_selected() or checked_attr == "true" or checked_attr == True
                logger.info(f"[{domain}] Step 7: Found SMTP checkbox, is_selected={checkbox.is_selected()}, checked_attr={checked_attr}, is_checked={is_checked}")
                
                if is_checked:
                    # Currently CHECKED = SMTP AUTH is OFF
                    # We need to UNCHECK to ENABLE SMTP AUTH
                    logger.info(f"[{domain}] Step 7: SMTP AUTH is OFF (checkbox checked). UNCHECKING to enable...")
                    safe_click(driver, checkbox, "SMTP AUTH checkbox - unchecking to enable")
                    time.sleep(1)
                    
                    # VERIFY the checkbox is now unchecked
                    new_checked_attr = checkbox.get_attribute("checked")
                    is_still_checked = checkbox.is_selected() or new_checked_attr == "true" or new_checked_attr == True
                    
                    if is_still_checked:
                        logger.warning(f"[{domain}] Step 7: Checkbox still checked after click, retrying with JS...")
                        driver.execute_script("arguments[0].checked = false; arguments[0].click();", checkbox)
                        time.sleep(1)
                        # Check again
                        final_checked = checkbox.is_selected() or checkbox.get_attribute("checked") == "true"
                        if final_checked:
                            logger.error(f"[{domain}] Step 7: Could not uncheck SMTP checkbox after retry")
                        else:
                            smtp_auth_enabled = True
                            logger.info(f"[{domain}] Step 7: SMTP AUTH ENABLED (unchecked via JS retry)")
                    else:
                        smtp_auth_enabled = True
                        logger.info(f"[{domain}] Step 7: SMTP AUTH ENABLED (checkbox successfully unchecked)")
                else:
                    # Already UNCHECKED = SMTP AUTH is already ENABLED
                    # DO NOT CLICK - clicking would RE-CHECK and DISABLE SMTP AUTH!
                    smtp_auth_enabled = True
                    logger.info(f"[{domain}] Step 7: SMTP AUTH already ENABLED (checkbox already unchecked) - NO ACTION NEEDED")
                
                break
            except Exception as e:
                logger.debug(f"[{domain}] Checkbox selector failed: {selector} - {e}")
                continue
        
        # JavaScript fallback for finding the checkbox - ONLY if not already handled
        if not checkbox_found:
            logger.warning(f"[{domain}] Step 7: Trying JS to find SMTP checkbox...")
            try:
                # DEFENSIVE JS: Only uncheck if checked, NEVER check if unchecked
                js_result = driver.execute_script("""
                    // Find checkbox by looking for label with SMTP text
                    var labels = document.querySelectorAll('label');
                    for (var label of labels) {
                        if (label.textContent.includes('Turn off SMTP AUTH')) {
                            // Found the label, now find the associated checkbox
                            var forId = label.getAttribute('for');
                            if (forId) {
                                var checkbox = document.getElementById(forId);
                                if (checkbox) {
                                    // ONLY click if CHECKED (to uncheck and enable SMTP AUTH)
                                    if (checkbox.checked) {
                                        checkbox.click();
                                        return 'unchecked_now_enabled';
                                    } else {
                                        // Already unchecked = already enabled - DO NOT CLICK!
                                        return 'already_enabled_no_action';
                                    }
                                }
                            }
                            // Try parent/sibling approach
                            var parent = label.closest('div');
                            if (parent) {
                                var cb = parent.querySelector('input[type="checkbox"]');
                                if (cb) {
                                    // ONLY click if CHECKED (to uncheck and enable SMTP AUTH)
                                    if (cb.checked) {
                                        cb.click();
                                        return 'unchecked_now_enabled';
                                    } else {
                                        // Already unchecked = already enabled - DO NOT CLICK!
                                        return 'already_enabled_no_action';
                                    }
                                }
                            }
                        }
                    }
                    
                    // Alternative: find all checkboxes and check context
                    var checkboxes = document.querySelectorAll('input[type="checkbox"]');
                    for (var cb of checkboxes) {
                        var container = cb.closest('div');
                        if (container && container.textContent.includes('SMTP AUTH')) {
                            // ONLY click if CHECKED (to uncheck and enable SMTP AUTH)
                            if (cb.checked) {
                                cb.click();
                                return 'unchecked_now_enabled';
                            } else {
                                // Already unchecked = already enabled - DO NOT CLICK!
                                return 'already_enabled_no_action';
                            }
                        }
                    }
                    return 'not_found';
                """)
                
                if js_result in ('unchecked_now_enabled', 'already_enabled_no_action'):
                    smtp_auth_enabled = True
                    logger.info(f"[{domain}] Step 7: JS result: {js_result}")
                    if js_result == 'already_enabled_no_action':
                        logger.info(f"[{domain}] Step 7: JS confirmed SMTP AUTH already enabled - no click performed")
                    time.sleep(1)
                else:
                    logger.error(f"[{domain}] Step 7: JS could not find SMTP checkbox")
            except Exception as e:
                logger.error(f"[{domain}] Step 7: JS fallback failed: {e}")

        _save_screenshot(driver, domain, "step7_after_checkbox")

        # =================================================================
        # STEP 7E: CLICK SAVE BUTTON
        # =================================================================
        if smtp_auth_enabled:
            logger.info(f"[{domain}] Step 7: Looking for Save button...")
            save_clicked = False
            
            save_selectors = [
                "//button[contains(@class, 'ms-Button--primary')][.//span[text()='Save']]",
                "//button[.//span[text()='Save']]",
                "//button[contains(text(), 'Save')]",
                "//button[@type='submit']",
                "//div[contains(@class, 'ms-Panel')]//button[contains(@class, 'primary')]",
            ]
            
            for sel in save_selectors:
                try:
                    save_btn = driver.find_element(By.XPATH, sel)
                    if save_btn.is_displayed() and save_btn.is_enabled():
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", save_btn)
                        time.sleep(0.5)
                        safe_click(driver, save_btn, "Save button")
                        save_clicked = True
                        time.sleep(3)
                        logger.info(f"[{domain}] Step 7: Clicked Save button")
                        break
                except Exception:
                    continue
            
            # JS fallback for Save button
            if not save_clicked:
                try:
                    js_save = driver.execute_script("""
                        var buttons = document.querySelectorAll('button');
                        for (var btn of buttons) {
                            if (btn.textContent.trim() === 'Save' || 
                                btn.textContent.includes('Save')) {
                                btn.click();
                                return 'clicked';
                            }
                        }
                        return 'not_found';
                    """)
                    if js_save == 'clicked':
                        save_clicked = True
                        logger.info(f"[{domain}] Step 7: Clicked Save via JS")
                        time.sleep(3)
                except Exception as e:
                    logger.warning(f"[{domain}] Step 7: JS Save failed: {e}")
            
            if not save_clicked:
                logger.warning(f"[{domain}] Step 7: Could not find Save button (may auto-save)")

        _save_screenshot(driver, domain, "step7_complete")

        # Set final result
        result["smtp_auth_enabled"] = smtp_auth_enabled
        result["success"] = smtp_auth_enabled

        if smtp_auth_enabled:
            logger.info(f"[{domain}] Step 7 COMPLETE: Org-level SMTP AUTH enabled")
        else:
            result["error"] = "Could not find or toggle SMTP AUTH setting"
            logger.error(f"[{domain}] Step 7 FAILED: Could not find SMTP AUTH checkbox")

        return result

    except Exception as e:
        logger.error(f"[{domain}] Step 7 FAILED: {e}")
        import traceback
        logger.error(traceback.format_exc())
        result["error"] = str(e)
        if driver:
            _save_screenshot(driver, domain, "step7_error")
        return result

    finally:
        _cleanup_driver(driver)
