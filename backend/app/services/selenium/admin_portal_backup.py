import time
import re
import os
import json
import pyotp
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
import logging

logger = logging.getLogger(__name__)
SCREENSHOTS = "C:/temp/screenshots"
STATUS_DIR = "C:/temp/automation_status"
os.makedirs(SCREENSHOTS, exist_ok=True)
os.makedirs(STATUS_DIR, exist_ok=True)

def screenshot(driver, name, domain):
    try:
        path = f"{SCREENSHOTS}/{name}_{domain.replace('.','_')}_{int(time.time())}.png"
        driver.save_screenshot(path)
        logger.info(f"Screenshot: {path}")
    except:
        pass


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


# ============================================================
# RETRY WRAPPER FOR RESILIENT DOMAIN SETUP
# ============================================================

def setup_domain_with_retry(
    domain: str,
    zone_id: str,
    admin_email: str,
    admin_password: str,
    totp_secret: str,
    max_retries: int = 2
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
        totp_secret: TOTP secret for MFA
        max_retries: Number of retry attempts (default 2, so 3 total attempts)
    
    Returns:
        Dict with success, verified, dns_configured, error keys
    """
    last_error = None
    
    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.info(f"[{domain}] Retry attempt {attempt}/{max_retries} - waiting 30s before retry...")
            time.sleep(30)  # Wait before retry to let things settle
        
        try:
            logger.info(f"[{domain}] Starting setup attempt {attempt + 1}/{max_retries + 1}")
            
            result = setup_domain_complete_via_admin_portal(
                domain=domain,
                zone_id=zone_id,
                admin_email=admin_email,
                admin_password=admin_password,
                totp_secret=totp_secret
            )
            
            if result.get("success"):
                if attempt > 0:
                    logger.info(f"[{domain}] SUCCESS on retry attempt {attempt}!")
                return result
            
            last_error = result.get("error", "Unknown error")
            logger.warning(f"[{domain}] Attempt {attempt + 1} failed: {last_error}")
            
        except Exception as e:
            last_error = str(e)
            logger.error(f"[{domain}] Attempt {attempt + 1} exception: {e}")
    
    # All attempts failed
    logger.error(f"[{domain}] FAILED after {max_retries + 1} attempts. Last error: {last_error}")
    return {
        "success": False, 
        "verified": False,
        "dns_configured": False,
        "error": f"Failed after {max_retries + 1} attempts: {last_error}"
    }


def setup_domain_complete_via_admin_portal(domain, zone_id, admin_email, admin_password, totp_secret, cloudflare_service=None, headless=False):
    """Complete M365 domain setup following EXACT wizard flow.
    
    IMPORTANT: Each step has individual error handling for better resilience.
    Browser is closed in finally block to ensure cleanup.
    """
    from app.services.cloudflare_sync import add_txt, add_mx, add_spf, add_cname
    
    logger.info(f"[{domain}] ========== STARTING DOMAIN SETUP ==========")
    driver = None
    result = {"success": False, "verified": False, "dns_configured": False, "error": None}
    
    # ===== SETUP BROWSER =====
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")
    opts.add_experimental_option("prefs", {"credentials_enable_service": False, "profile.password_manager_enabled": False})
    
    driver = webdriver.Chrome(options=opts)
    driver.implicitly_wait(15)  # Increased from 10
    driver.set_page_load_timeout(60)  # Add page load timeout
    logger.info(f"[{domain}] Browser initialized successfully")
    
    # ===== STEP 1: LOGIN =====
    logger.info(f"[{domain}] Step 1: Login")
    update_status_file(domain, "login", "in_progress", "Logging into M365 Admin Portal")
    driver.get("https://admin.microsoft.com")
    wait_for_page_load(driver, timeout=30)
    time.sleep(5)  # Extra wait after page load
    
    # Enter email
    email_field = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.NAME, "loginfmt"))
    )
    email_field.send_keys(admin_email + Keys.RETURN)
    time.sleep(5)  # Increased from 3
    
    # Enter password
    password_field = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.NAME, "passwd"))
    )
    password_field.send_keys(admin_password + Keys.RETURN)
    time.sleep(5)  # Increased from 3
    
    # Handle TOTP if required
    try:
        totp_field = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.NAME, "otc"))
        )
        totp_field.send_keys(pyotp.TOTP(totp_secret).now() + Keys.RETURN)
        time.sleep(5)  # Increased from 3
    except:
        logger.debug(f"[{domain}] No TOTP field found (may not be required)")
    
    # Handle "Stay signed in?" prompt
    try:
        stay_signed_in = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.ID, "idBtn_Back"))
        )
        stay_signed_in.click()
        time.sleep(3)
    except:
        logger.debug(f"[{domain}] No 'Stay signed in' prompt found")
    
    screenshot(driver, "01_login", domain)
    update_status_file(domain, "login", "complete", "Successfully logged in")
    time.sleep(5)  # Extra wait after login
    
    # ===== STEP 2: NAVIGATE TO DOMAINS =====
    logger.info(f"[{domain}] Step 2: Navigate to domains page")
    driver.get("https://admin.microsoft.com/#/Domains")
    wait_for_page_load(driver, timeout=30)
    time.sleep(8)  # Increased from 5 for page to fully render
    
    # Verify we're on domains page
    for check_attempt in range(10):
        if "domains" in driver.current_url.lower():
            logger.info(f"[{domain}] Successfully reached domains page")
            break
        time.sleep(1)
    else:
        raise Exception("Could not reach domains page after 10 attempts")
    
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
        driver.get("https://admin.microsoft.com/#/Domains/Wizard")
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
            logger.error(f"[{domain}] FAILED - browser left open for inspection")
            return result
        
        txt_value = txt_match.group(0)
        logger.info(f"[{domain}] Found TXT: {txt_value}")
        
        # 6a: Add TXT to Cloudflare
        logger.info(f"[{domain}] Step 6a: Adding TXT to Cloudflare")
        add_txt(zone_id, txt_value)
        
        # 6b: Wait for DNS
        logger.info(f"[{domain}] Step 6b: Waiting 10 seconds for DNS")
        time.sleep(10)
        
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
            logger.error(f"[{domain}] FAILED - browser left open for inspection")
            return result
        
        # Wait for page to change - use wait_for_page_change for more reliable timing
        logger.info(f"[{domain}] Waiting for verification result (up to 60s)...")
        old_page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        page_changed = wait_for_page_change(driver, old_page_text, timeout=60)
        
        if not page_changed:
            logger.warning(f"[{domain}] Page did not change after clicking Verify - waiting additional time...")
            time.sleep(10)
        
        screenshot(driver, "09_after_verify", domain)
        
        # Check the page ACTUALLY changed
        current_url = driver.current_url
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        
        # Still on verification page? That's a failure
        if "add a record to verify" in page_text or "txt value" in page_text:
            logger.error(f"[{domain}] Still on verification page - verification may have failed")
            # Check for specific error messages
            if "failed" in page_text or "try again" in page_text or "couldn't verify" in page_text:
                result["error"] = "Domain verification failed"
                logger.error(f"[{domain}] FAILED - browser left open for inspection")
                return result
            # Maybe need more time - wait and try checking again
            time.sleep(10)
            page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            if "add a record to verify" in page_text:
                result["error"] = "Verification did not complete - still on verification page"
                logger.error(f"[{domain}] FAILED - browser left open for inspection")
                return result
        
        result["verified"] = True
        update_status_file(domain, "verification", "complete", "Domain ownership verified")
        logger.info(f"[{domain}] Verification SUCCESS - page changed!")
    
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
            logger.error(f"[{domain}] FAILED - browser left open for inspection")
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

    missing_values = []
    if not mx_match:
        missing_values.append("MX")
    if not spf_match:
        missing_values.append("SPF")
    if not sel1_match:
        missing_values.append("DKIM selector1")
    if not sel2_match:
        missing_values.append("DKIM selector2")
    if missing_values:
        result["error"] = f"Microsoft DNS page missing required values: {', '.join(missing_values)}"
        update_status_file(domain, "dns_setup", "failed", result["error"])
        _cleanup_driver(driver)
        return result
    
    # ===== STEP 8i: ADD ALL RECORDS TO CLOUDFLARE =====
    logger.info(f"[{domain}] Step 8i: Adding DNS records to Cloudflare")
    
    if mx_match:
        mx_target = mx_match.group(1)
        logger.info(f"[{domain}] Adding MX: {mx_target}")
        add_mx(zone_id, mx_target, 0)
    
    if spf_match:
        spf_value = spf_match.group(1).strip()
        logger.info(f"[{domain}] Adding SPF: {spf_value}")
        add_spf(zone_id, spf_value)
    
    logger.info(f"[{domain}] Adding autodiscover CNAME")
    add_cname(zone_id, "autodiscover", "autodiscover.outlook.com")
    
    # Add DKIM CNAMEs with FULL target values
    if sel1_match:
        dkim1_target = sel1_match.group(1)
        logger.info(f"[{domain}] Adding DKIM: selector1._domainkey -> {dkim1_target}")
        add_cname(zone_id, "selector1._domainkey", dkim1_target)
    
    if sel2_match:
        dkim2_target = sel2_match.group(1)
        logger.info(f"[{domain}] Adding DKIM: selector2._domainkey -> {dkim2_target}")
        add_cname(zone_id, "selector2._domainkey", dkim2_target)
    
    result["dns_configured"] = True
    update_status_file(domain, "dns_setup", "complete", "DNS records added to Cloudflare")
    
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
        if "domain setup is complete" in page_text or "setup is complete" in page_text:
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
    
    if "domain setup is complete" in page_text or "setup is complete" in page_text:
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
        result["success"] = True
    else:
        result["success"] = False
        result["error"] = result.get("error") or "Microsoft domain setup wizard did not reach completion page"
    
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
    if driver:
        try:
            driver.quit()
            logger.info(f"[{domain}] Browser closed")
        except:
            pass
    
    return result
