"""
M365 Domain Wizard - Add domain and extract TXT value.
"""
import time
import re
import os
import pyotp
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import logging

logger = logging.getLogger(__name__)
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "C:/temp/screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# ============ DEBUG PAUSE UTILITY ============
DEBUG_PAUSE = os.environ.get("DEBUG_PAUSE", "True").lower() == "true"

def pause_for_debug(driver, message):
    """Pause and wait for user input so we can see what's happening."""
    if DEBUG_PAUSE:
        print(f"\n{'='*60}")
        print(f"PAUSED: {message}")
        print(f"Browser is still open - look at it now!")
        print(f"Current URL: {driver.current_url if driver else 'N/A'}")
        print(f"{'='*60}")
        input("Press ENTER to continue...")
# ============================================


def screenshot(driver, step, domain=""):
    """Save screenshot with step name."""
    safe_domain = domain.replace(".", "_") if domain else "nodomain"
    path = f"{SCREENSHOT_DIR}/{step}_{safe_domain}_{int(time.time())}.png"
    driver.save_screenshot(path)
    logger.info(f"Screenshot: {path}")


def create_browser():
    """Create Chrome browser."""
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    prefs = {"credentials_enable_service": False, "profile.password_manager_enabled": False}
    options.add_experimental_option("prefs", prefs)
    driver = webdriver.Chrome(options=options)
    driver.implicitly_wait(10)
    return driver


def do_login(driver, admin_email, admin_password, totp_secret):
    """Login to M365. Returns True/False."""
    logger.info("=== LOGGING IN ===")
    driver.get("https://admin.microsoft.com")
    # Wait for page load
    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(0.5)
    
    # Email - wait for element
    email_field = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.NAME, "loginfmt"))
    )
    email_field.send_keys(admin_email + Keys.RETURN)
    # Wait for password page
    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(0.5)
    
    # Password - wait for element
    password_field = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.NAME, "passwd"))
    )
    password_field.send_keys(admin_password + Keys.RETURN)
    # Wait for MFA or next page
    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(0.5)
    
    # MFA - wait for element
    try:
        totp_field = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.NAME, "otc"))
        )
        code = pyotp.TOTP(totp_secret).now()
        logger.info(f"MFA code: {code[:2]}***")
        totp_field.send_keys(code + Keys.RETURN)
        # Wait for verification
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(0.5)
    except TimeoutException:
        pass
    
    # Stay signed in - No
    try:
        no_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "idBtn_Back"))
        )
        no_btn.click()
        # Wait for page transition
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(0.5)
    except TimeoutException:
        pass
    
    return "admin" in driver.current_url.lower()


def get_txt_value(driver, domain):
    """
    Navigate to Domains, add domain, get TXT value.
    Returns (txt_value, error) - txt_value is None if failed.
    """
    logger.info(f"=== GETTING TXT VALUE FOR {domain} ===")
    
    # Go to Domains page
    logger.info("Navigating to Domains...")
    driver.get("https://admin.microsoft.com/#/Domains")
    # Wait for page to fully load
    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(0.5)
    screenshot(driver, "01_domains_page", domain)
    
    # Click Add domain - wait for button
    logger.info("Clicking Add domain...")
    try:
        add_btn = WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Add domain')]"))
        )
        add_btn.click()
    except TimeoutException:
        driver.get("https://admin.microsoft.com/#/Domains/Wizard")
    # Wait for wizard page
    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(0.5)
    screenshot(driver, "02_add_domain_clicked", domain)
    
    # Enter domain name - wait for input
    logger.info(f"Entering domain: {domain}")
    domain_input = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.XPATH, "//input[@type='text']"))
    )
    domain_input.clear()
    time.sleep(0.2)
    domain_input.send_keys(domain)
    time.sleep(0.3)
    screenshot(driver, "03_domain_entered", domain)
    
    # Click Use this domain / Continue - wait for button
    try:
        btn = WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Use this domain')]"))
        )
        btn.click()
    except:
        btn = WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Continue')]"))
        )
        btn.click()
    # Wait for next page
    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(0.5)
    
    # ===== STEP 5: DETECT PAGE STATE AFTER ENTERING DOMAIN =====
    screenshot(driver, "04_after_domain_entry", domain)
    
    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    logger.info(f"[{domain}] Detecting page state...")
    
    # Check for VERIFICATION PAGE FIRST (most common for new domains)
    if "verify you own" in page_text or "verify your domain" in page_text or "domain verification" in page_text:
        logger.info(f"[{domain}] Step 5: On VERIFICATION page - need to verify domain")
        
        # Click "More options" to show TXT option
        logger.info(f"[{domain}] Step 5a: Clicking 'More options'")
        try:
            more_opts = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'More options')]"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", more_opts)
            time.sleep(0.3)
            driver.execute_script("arguments[0].click();", more_opts)
            logger.info(f"[{domain}] Clicked 'More options'")
            # Wait for options to appear
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(0.5)
        except TimeoutException as e:
            logger.warning(f"[{domain}] Could not click More options: {e}")
        
        screenshot(driver, "05_more_options_clicked", domain)
        
        # Select "Add a TXT record" radio button
        logger.info(f"[{domain}] Step 5b: Selecting TXT record option")
        try:
            for xpath in [
                "//*[contains(text(), 'Add a TXT record')]",
                "//input[@type='radio'][..//*[contains(text(), 'TXT')]]",
                "//label[contains(., 'TXT record')]"
            ]:
                try:
                    elem = driver.find_element(By.XPATH, xpath)
                    driver.execute_script("arguments[0].click();", elem)
                    logger.info(f"[{domain}] Selected TXT option")
                    break
                except:
                    continue
        except Exception as e:
            logger.warning(f"[{domain}] Could not select TXT option: {e}")
        
        time.sleep(0.3)
        screenshot(driver, "06_txt_selected", domain)
        
        # Click Continue - wait for button
        logger.info(f"[{domain}] Step 5c: Clicking Continue")
        try:
            cont_btn = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Continue')]"))
            )
            driver.execute_script("arguments[0].click();", cont_btn)
            # Wait for TXT value page
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(0.5)
        except TimeoutException:
            pass
        
        screenshot(driver, "07_txt_value_page", domain)
        
        # Extract TXT value
        logger.info(f"[{domain}] Step 6: Extracting TXT value")
        page_text = driver.find_element(By.TAG_NAME, "body").text
        txt_match = re.search(r'MS=ms\d+', page_text)
        
        if txt_match:
            txt_value = txt_match.group(0)
            logger.info(f"[{domain}] Found TXT: {txt_value}")
            return txt_value, None
        else:
            logger.error(f"[{domain}] TXT value not found on page!")
            screenshot(driver, "error_no_txt", domain)
            return None, "Could not find MS=ms... value on page"
    
    # Check for CONNECT DOMAIN PAGE (domain already verified)
    elif "how do you want to connect" in page_text:
        logger.info(f"[{domain}] Domain already verified - on Connect page")
        return "ALREADY_VERIFIED", None
    
    # Check for DNS RECORDS PAGE (already past connect)
    elif "add dns records" in page_text:
        logger.info(f"[{domain}] Already on DNS records page")
        return "ALREADY_VERIFIED", None
    
    # Check for COMPLETE PAGE
    elif "domain setup is complete" in page_text or "is all set up" in page_text:
        logger.info(f"[{domain}] Domain already complete!")
        return "ALREADY_VERIFIED", None
    
    else:
        # Unknown page state - try verification flow anyway
        logger.warning(f"[{domain}] Unknown page state after entering domain - attempting TXT verification")
        screenshot(driver, "unknown_page_state", domain)
        
        # Try clicking "More options" anyway
        logger.info(f"[{domain}] Attempting More options click...")
        try:
            more_opt = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'More options')]"))
            )
            driver.execute_script("arguments[0].click();", more_opt)
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(0.5)
        except TimeoutException as e:
            logger.warning(f"[{domain}] More options not found: {e}")
        
        # Try selecting TXT option
        logger.info(f"[{domain}] Attempting TXT option selection...")
        try:
            txt_opt = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Add a TXT record')]"))
            )
            driver.execute_script("arguments[0].click();", txt_opt)
            time.sleep(0.3)
        except TimeoutException as e:
            logger.warning(f"[{domain}] TXT option not found: {e}")
        
        screenshot(driver, "05_txt_option_selected", domain)
        
        # Click Continue
        try:
            cont = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Continue')]"))
            )
            driver.execute_script("arguments[0].click();", cont)
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(0.5)
        except TimeoutException:
            pass
        
        screenshot(driver, "06_txt_value_page", domain)
        
        # Extract MS=ms######## value
        page_text = driver.find_element(By.TAG_NAME, "body").text
        match = re.search(r'MS=ms\d+', page_text)
        
        if match:
            txt_value = match.group(0)
            logger.info(f"[{domain}] FOUND TXT VALUE: {txt_value}")
            return txt_value, None
        else:
            logger.error(f"[{domain}] TXT VALUE NOT FOUND!")
            screenshot(driver, "error_no_txt", domain)
            return None, "Could not find MS=ms... value on page"


def handle_dns_records_page(driver, domain, zone_id) -> dict:
    """
    Handle the "Add DNS records" page:
    1. Extract MX, autodiscover, SPF values
    2. Check DKIM checkbox under Advanced options
    3. Extract DKIM selector values
    4. Add all to Cloudflare (deleting duplicates first)
    5. Click Continue
    
    Returns dict with all extracted values.
    """
    from app.services.cloudflare_sync import set_mx, set_spf, set_autodiscover, set_dkim
    
    logger.info(f"[{domain}] === HANDLING DNS RECORDS PAGE ===")
    screenshot(driver, "30_dns_page_start", domain)
    
    extracted = {
        "mx_target": None,
        "mx_priority": 0,
        "spf_value": None,
        "autodiscover_target": None,
        "dkim_selector1": None,
        "dkim_selector2": None
    }
    
    # Scroll down to make sure page is fully loaded
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(0.5)
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.3)
    
    page_text = driver.find_element(By.TAG_NAME, "body").text
    
    # === STEP 1: EXPAND ALL SECTIONS BY CLICKING ON THEM ===
    # Click on "MX Records", "CNAME Records", "TXT Records" to expand them
    for section_name in ["MX Records", "CNAME Records", "TXT Records"]:
        try:
            section = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, f"//*[contains(text(), '{section_name}')]"))
            )
            section.click()
            time.sleep(0.3)
            logger.info(f"[{domain}] Expanded {section_name}")
        except TimeoutException:
            pass
    
    screenshot(driver, "31_sections_expanded", domain)
    time.sleep(0.5)
    page_text = driver.find_element(By.TAG_NAME, "body").text
    
    # === STEP 2: EXTRACT MX VALUE ===
    mx_match = re.search(r'([a-zA-Z0-9-]+\.mail\.protection\.outlook\.com)', page_text)
    if mx_match:
        extracted["mx_target"] = mx_match.group(1)
        logger.info(f"[{domain}] Found MX: {extracted['mx_target']}")
    
    # Extract priority (usually 0)
    priority_match = re.search(r'Priority[:\s]*(\d+)', page_text, re.IGNORECASE)
    if priority_match:
        extracted["mx_priority"] = int(priority_match.group(1))
    
    # === STEP 3: EXTRACT SPF VALUE ===
    spf_match = re.search(r'(v=spf1[^\n"<>]+outlook\.com[^\n"<>]*-all)', page_text)
    if spf_match:
        extracted["spf_value"] = spf_match.group(1).strip()
        logger.info(f"[{domain}] Found SPF: {extracted['spf_value']}")
    else:
        logger.error(f"[{domain}] SPF value not found on Microsoft DNS records page")
    
    # === STEP 4: EXTRACT AUTODISCOVER ===
    # Usually "autodiscover.outlook.com"
    autodiscover_match = re.search(r'autodiscover\.(outlook\.com)', page_text, re.IGNORECASE)
    if autodiscover_match:
        extracted["autodiscover_target"] = f"autodiscover.{autodiscover_match.group(1)}"
    else:
        logger.error(f"[{domain}] Autodiscover value not found on Microsoft DNS records page")
    if extracted["autodiscover_target"]:
        logger.info(f"[{domain}] Found autodiscover: {extracted['autodiscover_target']}")
    
    # === STEP 5: SCROLL TO ADVANCED OPTIONS AND CHECK DKIM ===
    logger.info(f"[{domain}] Looking for Advanced options / DKIM checkbox...")
    
    # Scroll down to find Advanced options
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(0.5)
    screenshot(driver, "32_scrolled_to_advanced", domain)
    
    # Try to expand "Advanced options" if it's collapsed
    try:
        advanced = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Advanced options')]"))
        )
        advanced.click()
        time.sleep(0.3)
        logger.info(f"[{domain}] Clicked Advanced options")
    except TimeoutException:
        logger.info(f"[{domain}] Advanced options may already be expanded")
    
    # CHECK THE DKIM CHECKBOX
    dkim_checked = False
    try:
        # Find the DKIM checkbox - it might be a checkbox input or a clickable label
        dkim_selectors = [
            "//input[contains(@aria-label, 'DKIM')]",
            "//input[contains(@id, 'dkim')]",
            "//*[contains(text(), 'DomainKeys Identified Mail')]//preceding::input[@type='checkbox'][1]",
            "//*[contains(text(), 'DomainKeys Identified Mail')]//ancestor::div//input[@type='checkbox']",
            "//label[contains(., 'DKIM')]//input",
            "//*[contains(text(), 'DKIM')]"
        ]
        
        for selector in dkim_selectors:
            try:
                dkim_element = driver.find_element(By.XPATH, selector)
                # Check if it's already checked
                if dkim_element.get_attribute("type") == "checkbox":
                    if not dkim_element.is_selected():
                        dkim_element.click()
                        logger.info(f"[{domain}] Checked DKIM checkbox")
                        dkim_checked = True
                        break
                    else:
                        logger.info(f"[{domain}] DKIM already checked")
                        dkim_checked = True
                        break
                else:
                    # It's a label or div, click it
                    dkim_element.click()
                    logger.info(f"[{domain}] Clicked DKIM element")
                    dkim_checked = True
                    break
            except:
                continue
    except Exception as e:
        logger.warning(f"[{domain}] Could not find/check DKIM: {e}")
    
    time.sleep(0.5)
    screenshot(driver, "33_after_dkim_check", domain)
    
    # === STEP 6: EXTRACT DKIM VALUES ===
    # Now the DKIM CNAME records should be visible
    page_text = driver.find_element(By.TAG_NAME, "body").text
    
    # Look for selector1 value - format: selector1-domain-com._domainkey.tenant.onmicrosoft.com
    # OR the new format: selector1-domain._domainkey.tenant.r-v1.dkim.mail.microsoft
    selector1_match = re.search(
        r'(selector1[a-zA-Z0-9._-]+(?:\.onmicrosoft\.com|\.dkim\.mail\.microsoft[a-zA-Z]*))',
        page_text,
        re.IGNORECASE
    )
    if selector1_match:
        extracted["dkim_selector1"] = selector1_match.group(1)
        logger.info(f"[{domain}] Found DKIM selector1: {extracted['dkim_selector1']}")
    
    selector2_match = re.search(
        r'(selector2[a-zA-Z0-9._-]+(?:\.onmicrosoft\.com|\.dkim\.mail\.microsoft[a-zA-Z]*))',
        page_text,
        re.IGNORECASE
    )
    if selector2_match:
        extracted["dkim_selector2"] = selector2_match.group(1)
        logger.info(f"[{domain}] Found DKIM selector2: {extracted['dkim_selector2']}")

    missing_values = [
        name
        for name, value in {
            "MX": extracted["mx_target"],
            "SPF": extracted["spf_value"],
            "autodiscover": extracted["autodiscover_target"],
            "DKIM selector1": extracted["dkim_selector1"],
            "DKIM selector2": extracted["dkim_selector2"],
        }.items()
        if not value
    ]
    if missing_values:
        extracted["error"] = f"Microsoft DNS page missing required values: {', '.join(missing_values)}"
        logger.error(f"[{domain}] {extracted['error']}")
        return extracted
    
    # === STEP 7: ADD ALL RECORDS TO CLOUDFLARE ===
    logger.info(f"[{domain}] Adding records to Cloudflare (deleting duplicates first)...")
    
    # MX
    if extracted["mx_target"]:
        set_mx(zone_id, extracted["mx_target"], extracted["mx_priority"])
    
    # SPF
    if extracted["spf_value"]:
        set_spf(zone_id, extracted["spf_value"])
    
    # Autodiscover
    if extracted["autodiscover_target"]:
        set_autodiscover(zone_id, extracted["autodiscover_target"])
    
    # DKIM
    if extracted["dkim_selector1"] and extracted["dkim_selector2"]:
        set_dkim(zone_id, extracted["dkim_selector1"], extracted["dkim_selector2"])
    else:
        logger.warning(f"[{domain}] DKIM selectors not found, skipping DKIM setup")
        extracted["error"] = "Microsoft DNS page did not expose all required DKIM selectors"
    
    logger.info(f"[{domain}] All DNS records added to Cloudflare")
    
    # === STEP 8: CLICK CONTINUE ===
    logger.info(f"[{domain}] Clicking Continue...")
    time.sleep(0.5)  # Brief pause for Cloudflare DNS to start propagating
    
    try:
        continue_btn = WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Continue')]"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", continue_btn)
        time.sleep(0.3)
        continue_btn.click()
        logger.info(f"[{domain}] Clicked Continue")
        # Wait for next page
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(0.5)
    except TimeoutException as e:
        logger.error(f"[{domain}] Could not click Continue: {e}")
    
    screenshot(driver, "34_after_dns_continue", domain)
    
    return extracted


def click_verify_and_confirm(driver, domain) -> tuple:
    """
    Click the Verify button and confirm verification success.
    
    Returns: (success: bool, error: str or None)
    """
    logger.info(f"[{domain}] === CLICKING VERIFY BUTTON ===")
    screenshot(driver, "20_before_verify", domain)
    
    # Find and click the Verify button
    verify_clicked = False
    verify_selectors = [
        "//button[contains(., 'Verify')]",
        "//button[@type='submit' and contains(., 'Verify')]",
        "//button[contains(@class, 'primary') and contains(., 'Verify')]",
        "//*[contains(@data-testid, 'verify')]//button",
    ]
    
    for selector in verify_selectors:
        try:
            verify_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, selector))
            )
            if verify_btn.is_displayed() and verify_btn.is_enabled():
                # Scroll to button and click
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", verify_btn)
                time.sleep(0.3)
                verify_btn.click()
                verify_clicked = True
                logger.info(f"[{domain}] Clicked Verify button")
                break
        except TimeoutException:
            continue
    
    if not verify_clicked:
        screenshot(driver, "error_verify_not_found", domain)
        return False, "Could not find Verify button"
    
    # Wait for verification to process
    try:
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(0.5)
    except:
        pass
    screenshot(driver, "21_after_verify_click", domain)
    
    # Check for verification result
    for attempt in range(6):  # Up to 30 seconds of waiting
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        
        # Success indicators - now on DNS records page
        if any(indicator in page_text for indicator in [
            "add dns records", 
            "how do you want to connect",
            "connect your domain",
            "mx records",
            "dns configuration"
        ]):
            logger.info(f"[{domain}] VERIFICATION SUCCESSFUL - on DNS records page")
            screenshot(driver, "22_verification_success", domain)
            return True, None
        
        # Error indicators
        if any(error in page_text for error in [
            "record not found",
            "we couldn't confirm",
            "verification failed",
            "txt record doesn't match"
        ]):
            screenshot(driver, "error_verification_failed", domain)
            return False, "DNS verification failed - TXT record not found"
        
        # Still processing
        logger.info(f"[{domain}] Verification in progress, waiting... (attempt {attempt + 1})")
        try:
            WebDriverWait(driver, 5).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except:
            pass
        time.sleep(0.5)
        screenshot(driver, f"21_verify_wait_{attempt}", domain)
    
    # Timeout - check if we made it to DNS page anyway
    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    if "add dns records" in page_text or "mx" in page_text:
        return True, None
    
    screenshot(driver, "error_verify_timeout", domain)
    return False, "Verification timed out"


def complete_wizard(driver, domain) -> bool:
    """Click Done/Finish to complete the wizard."""
    logger.info(f"[{domain}] Completing wizard...")
    
    # The page after DNS records might show success or ask to verify DNS
    for attempt in range(5):
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        screenshot(driver, f"40_wizard_step_{attempt}", domain)
        
        if "domain setup is complete" in page_text or "setup is complete" in page_text:
            logger.info(f"[{domain}] WIZARD COMPLETE!")
            return True
        
        # Click any Continue/Done/Finish buttons
        for btn_text in ["Continue", "Done", "Finish", "Close", "Got it"]:
            try:
                btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, f"//button[contains(., '{btn_text}')]"))
                )
                if btn.is_displayed():
                    btn.click()
                    logger.info(f"[{domain}] Clicked {btn_text}")
                    try:
                        WebDriverWait(driver, 10).until(
                            lambda d: d.execute_script("return document.readyState") == "complete"
                        )
                        time.sleep(0.5)
                    except:
                        pass
                    break
            except TimeoutException:
                continue
    
    screenshot(driver, "41_wizard_final", domain)
    return False


def run_full_domain_setup(domain: str, zone_id: str, admin_email: str, 
                          admin_password: str, totp_secret: str) -> dict:
    """Complete domain setup from login to finish."""
    from app.services.cloudflare_sync import set_verification_txt
    
    driver = None
    result = {
        "success": False,
        "verified": False,
        "dns_configured": False,
        "dkim_enabled": False,
        "mx_target": None,
        "spf_value": None,
        "dkim_selector1": None,
        "dkim_selector2": None,
        "error": None
    }
    
    try:
        driver = create_browser()
        
        # Step 1: Login
        logger.info(f"[{domain}] === STEP 1: LOGIN ===")
        if not do_login(driver, admin_email, admin_password, totp_secret):
            result["error"] = "Login failed"
            return result
        
        # Step 2: Get TXT verification value
        logger.info(f"[{domain}] === STEP 2: GET TXT VALUE ===")
        txt_value, error = get_txt_value(driver, domain)
        
        # DEBUG: Pause after getting TXT value
        pause_for_debug(driver, f"Got TXT value: {txt_value}, error: {error}")
        
        if txt_value == "ALREADY_VERIFIED":
            result["verified"] = True
            logger.info(f"[{domain}] Domain already verified")
            pause_for_debug(driver, "Domain already verified - about to handle DNS records page")
        elif txt_value:
            # Step 3: Add TXT to Cloudflare
            logger.info(f"[{domain}] === STEP 3: ADD TXT TO CLOUDFLARE ===")
            pause_for_debug(driver, f"About to add TXT '{txt_value}' to Cloudflare")
            if not set_verification_txt(zone_id, txt_value):
                result["error"] = "Failed to add TXT to Cloudflare"
                pause_for_debug(driver, "FAILED to add TXT to Cloudflare - about to return early!")
                return result
            
            # Step 4: Wait for DNS
            logger.info(f"[{domain}] === STEP 4: WAIT 45s FOR DNS ===")
            pause_for_debug(driver, "TXT added to Cloudflare - now waiting 45s for DNS propagation")
            # Wait for DNS propagation
            try:
                WebDriverWait(driver, 45).until(
                    lambda d: False  # Just use this as a timer
                )
            except TimeoutException:
                pass  # Expected timeout after 45 seconds
            
            # Step 5: Click Verify
            logger.info(f"[{domain}] === STEP 5: CLICK VERIFY ===")
            pause_for_debug(driver, "DNS wait complete - ABOUT TO CLICK VERIFY BUTTON")
            success, error = click_verify_and_confirm(driver, domain)
            if not success:
                result["error"] = error
                pause_for_debug(driver, f"VERIFICATION FAILED: {error} - about to return early!")
                return result
            result["verified"] = True
            pause_for_debug(driver, "VERIFICATION SUCCESSFUL! About to handle DNS records page")
        else:
            result["error"] = error or "Could not get TXT value"
            pause_for_debug(driver, f"FAILED to get TXT value: {error} - about to return early!")
            return result
        
        # Step 6: Handle DNS records page (MX, SPF, autodiscover, DKIM)
        logger.info(f"[{domain}] === STEP 6: DNS RECORDS PAGE ===")
        dns_values = handle_dns_records_page(driver, domain, zone_id)
        if dns_values.get("error"):
            result["error"] = dns_values["error"]
            return result
        missing_dns_values = [
            name
            for name, value in {
                "MX": dns_values.get("mx_target"),
                "SPF": dns_values.get("spf_value"),
                "autodiscover": dns_values.get("autodiscover_target"),
                "DKIM selector1": dns_values.get("dkim_selector1"),
                "DKIM selector2": dns_values.get("dkim_selector2"),
            }.items()
            if not value
        ]
        if missing_dns_values:
            result["error"] = f"Microsoft DNS page missing required values: {', '.join(missing_dns_values)}"
            return result
        
        result["mx_target"] = dns_values.get("mx_target")
        result["spf_value"] = dns_values.get("spf_value")
        result["dkim_selector1"] = dns_values.get("dkim_selector1")
        result["dkim_selector2"] = dns_values.get("dkim_selector2")
        result["dns_configured"] = True
        
        if dns_values.get("dkim_selector1") and dns_values.get("dkim_selector2"):
            result["dkim_enabled"] = True
        
        # Step 7: Complete wizard
        logger.info(f"[{domain}] === STEP 7: COMPLETE WIZARD ===")
        if not complete_wizard(driver, domain):
            result["error"] = "Microsoft domain setup wizard did not reach completion page"
            return result
        
        result["success"] = True
        logger.info(f"[{domain}] ========== SETUP COMPLETE ==========")
        return result
        
    except Exception as e:
        logger.error(f"[{domain}] Exception: {e}")
        import traceback
        traceback.print_exc()
        result["error"] = str(e)
        if driver:
            screenshot(driver, "exception", domain)
            pause_for_debug(driver, f"EXCEPTION OCCURRED: {e}")
        return result
        
    finally:
        if driver:
            pause_for_debug(driver, "FINALLY block reached - browser about to close")
            # driver.quit()  # COMMENTED OUT FOR DEBUGGING - uncomment when done!
            print("DEBUG: driver.quit() is COMMENTED OUT - close browser manually!")


# TEST FUNCTION
def test_get_txt():
    """Test getting TXT value."""
    driver = create_browser()
    
    try:
        # Login
        success = do_login(
            driver,
            admin_email="admin@YourTenant.onmicrosoft.com",
            admin_password="YourPassword",
            totp_secret="YourTOTPSecret"
        )
        
        if not success:
            print("LOGIN FAILED")
            return
        
        print("LOGIN OK")
        
        # Get TXT
        txt_value, error = get_txt_value(driver, "yourdomain.com")
        
        if txt_value:
            print(f"SUCCESS! TXT value: {txt_value}")
        else:
            print(f"FAILED: {error}")
        
        input("Press Enter to close...")
        
    finally:
        driver.quit()


if __name__ == "__main__":
    test_get_txt()
