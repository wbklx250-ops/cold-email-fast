"""
Step 7: Disable Security Defaults - BULLETPROOF VERSION
========================================================

This script disables Security Defaults on M365 tenants via Entra ID.
Designed for headless operation on Railway with slower load times.

Key design principles:
1. Wait generously for all page loads
2. Verify each action succeeded before proceeding
3. Handle popups carefully without interfering with navigation
4. Use exact selectors from UI recorder
5. Retry failed operations
"""

import time
import logging
import pyotp
from typing import Dict, List, Optional
from dataclasses import dataclass

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# Configure logging
logger = logging.getLogger(__name__)

# Screenshot directory
SCREENSHOT_DIR = "/tmp/screenshots/step8"


@dataclass
class TenantCredentials:
    tenant_id: str
    admin_email: str
    admin_password: str
    totp_secret: str
    domain: str


class SecurityDefaultsDisabler:
    """
    Bulletproof automation to disable Security Defaults.
    
    Designed for:
    - Headless operation on Railway
    - Slow network conditions
    - Unpredictable page load order
    - Various popup/tour interruptions
    """
    
    # Timing constants - generous for headless/slow conditions
    WAIT_PAGE_TRANSITION = 12      # Wait after clicking for page to start loading
    WAIT_PAGE_LOAD = 90            # Max wait for page to fully load (increased for headless)
    WAIT_ELEMENT = 20              # Max wait for specific element
    WAIT_AFTER_ACTION = 8          # Wait after clicking something (increased for headless)
    WAIT_SHORT = 3                 # Short pause
    
    def __init__(self, headless: bool = True, worker_id: int = 0):
        self.headless = headless
        self.worker_id = worker_id
        self.driver: Optional[webdriver.Chrome] = None
    
    def _log(self, message: str, level: str = "info"):
        """Log with worker ID prefix."""
        full_msg = f"[W{self.worker_id}] {message}"
        if level == "error":
            logger.error(full_msg)
        elif level == "warning":
            logger.warning(full_msg)
        else:
            logger.info(full_msg)
    
    def _screenshot(self, name: str):
        """Save screenshot for debugging."""
        if self.driver:
            import os
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            path = f"{SCREENSHOT_DIR}/w{self.worker_id}_{name}.png"
            try:
                self.driver.save_screenshot(path)
                self._log(f"Screenshot: {path}")
            except:
                pass
    
    def _setup_driver(self) -> bool:
        """Initialize Chrome driver with optimal settings."""
        try:
            opts = Options()
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--window-size=1920,1080")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            
            # Disable password manager popup
            prefs = {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
                "profile.password_manager_leak_detection": False,
            }
            opts.add_experimental_option("prefs", prefs)
            
            if self.headless:
                opts.add_argument("--headless=new")
            else:
                opts.add_argument("--start-maximized")
            
            self.driver = webdriver.Chrome(options=opts)
            self.driver.implicitly_wait(5)
            
            # Hide automation indicators
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            
            self._log("Browser initialized")
            return True
            
        except Exception as e:
            self._log(f"Failed to setup browser: {e}", "error")
            return False
    
    # =========================================================================
    # LOGIN FLOW
    # =========================================================================
    
    def _login(self, creds: TenantCredentials) -> bool:
        """
        Complete login flow handling any order of:
        - Email entry
        - Password entry
        - MFA code entry
        - Stay signed in prompt
        
        Returns True if logged in successfully.
        """
        self._log(f"Starting login for {creds.domain}...")
        
        # Navigate to Entra
        self.driver.get("https://entra.microsoft.com/")
        time.sleep(self.WAIT_PAGE_TRANSITION)
        self._screenshot("login_01_initial")
        
        # === Enter Email ===
        if not self._enter_email(creds.admin_email):
            return False
        
        # === Enter Password ===
        if not self._enter_password(creds.admin_password):
            return False
        
        # === Handle MFA and Stay Signed In (any order) ===
        if not self._handle_post_password_flow(creds.totp_secret):
            return False
        
        self._log("Login successful!")
        return True
    
    def _enter_email(self, email: str) -> bool:
        """Enter email and click Next."""
        self._log("Entering email...")
        
        try:
            wait = WebDriverWait(self.driver, self.WAIT_ELEMENT)
            
            # Wait for email input
            email_input = wait.until(
                EC.presence_of_element_located((By.ID, "i0116"))
            )
            email_input.clear()
            email_input.send_keys(email)
            self._log("Email entered")
            
            time.sleep(self.WAIT_SHORT)
            
            # Click Next
            next_btn = wait.until(
                EC.element_to_be_clickable((By.ID, "idSIButton9"))
            )
            next_btn.click()
            self._log("Clicked Next")
            
            time.sleep(self.WAIT_PAGE_TRANSITION)
            self._screenshot("login_02_after_email")
            return True
            
        except Exception as e:
            self._log(f"Email entry failed: {e}", "error")
            self._screenshot("login_error_email")
            return False
    
    def _enter_password(self, password: str) -> bool:
        """Enter password and click Sign in."""
        self._log("Entering password...")
        
        try:
            wait = WebDriverWait(self.driver, self.WAIT_ELEMENT)
            
            # Wait for password input
            password_input = wait.until(
                EC.presence_of_element_located((By.ID, "i0118"))
            )
            password_input.clear()
            password_input.send_keys(password)
            self._log("Password entered")
            
            time.sleep(self.WAIT_SHORT)
            
            # Click Sign in
            signin_btn = wait.until(
                EC.element_to_be_clickable((By.ID, "idSIButton9"))
            )
            signin_btn.click()
            self._log("Clicked Sign in")
            
            time.sleep(self.WAIT_PAGE_TRANSITION)
            self._screenshot("login_03_after_password")
            return True
            
        except Exception as e:
            self._log(f"Password entry failed: {e}", "error")
            self._screenshot("login_error_password")
            return False
    
    def _handle_post_password_flow(self, totp_secret: str) -> bool:
        """
        Handle MFA and Stay Signed In prompts in ANY order.
        These can appear: MFA->Stay, Stay->MFA, or just one of them.
        """
        self._log("Handling post-password flow...")
        
        mfa_done = False
        stay_done = False
        
        # Loop to handle pages in any order
        for round_num in range(5):
            self._log(f"Post-password check round {round_num + 1}...")
            time.sleep(self.WAIT_PAGE_TRANSITION)
            
            self._screenshot(f"login_flow_round_{round_num + 1}")
            
            # Check if we're done (on Entra portal)
            current_url = self.driver.current_url
            if "entra.microsoft.com" in current_url and "login" not in current_url:
                if "#" in current_url or "home" in current_url or "view" in current_url:
                    self._log("Reached Entra portal!")
                    return True
            
            # Check for MFA input
            if not mfa_done:
                try:
                    mfa_input = self.driver.find_element(By.ID, "idTxtBx_SAOTCC_OTC")
                    self._log("MFA code input found")
                    
                    # Generate and enter code
                    totp = pyotp.TOTP(totp_secret)
                    code = totp.now()
                    self._log(f"Generated TOTP: {code}")
                    
                    mfa_input.clear()
                    mfa_input.send_keys(code)
                    
                    time.sleep(self.WAIT_SHORT)
                    
                    # Click Verify
                    verify_btn = self.driver.find_element(By.ID, "idSubmit_SAOTCC_Continue")
                    verify_btn.click()
                    self._log("Clicked Verify")
                    
                    mfa_done = True
                    time.sleep(self.WAIT_PAGE_TRANSITION)
                    continue
                    
                except NoSuchElementException:
                    pass
            
            # Check for Stay Signed In
            if not stay_done:
                page_source = self.driver.page_source.lower()
                if "stay signed in" in page_source:
                    self._log("Stay signed in prompt found")
                    
                    try:
                        # Click Yes
                        yes_btn = self.driver.find_element(By.ID, "idSIButton9")
                        yes_btn.click()
                        self._log("Clicked Yes (Stay signed in)")
                        
                        stay_done = True
                        time.sleep(self.WAIT_PAGE_TRANSITION)
                        continue
                        
                    except NoSuchElementException:
                        try:
                            # Try No button
                            no_btn = self.driver.find_element(By.ID, "idBtn_Back")
                            no_btn.click()
                            self._log("Clicked No (Stay signed in)")
                            
                            stay_done = True
                            time.sleep(self.WAIT_PAGE_TRANSITION)
                            continue
                        except:
                            pass
            
            # Check for "Enter code" text as backup MFA detection
            if not mfa_done and ("enter code" in self.driver.page_source.lower() or 
                                  "enter the code" in self.driver.page_source.lower()):
                self._log("MFA page detected via text, waiting for input...")
                time.sleep(self.WAIT_AFTER_ACTION)
                continue
            
            # If nothing to handle, might be done
            if mfa_done or stay_done:
                self._log("Flow items handled, checking if complete...")
            else:
                self._log("No login prompts found, assuming complete")
                break
        
        # Final check
        time.sleep(self.WAIT_AFTER_ACTION)
        current_url = self.driver.current_url
        self._log(f"Final URL: {current_url}")
        
        return "entra.microsoft.com" in current_url or "portal.azure" in current_url
    
    # =========================================================================
    # NAVIGATION
    # =========================================================================
    
    def _navigate_to_security_defaults(self) -> bool:
        """
        Navigate to the Security Defaults panel.
        
        Flow:
        1. Go to Overview page
        2. Click Properties tab
        3. Click "Manage security defaults"
        """
        self._log("Navigating to Security Defaults...")
        
        # Step 1: Navigate to Overview
        if not self._go_to_overview():
            return False
        
        # Step 2: Click Properties tab
        if not self._click_properties_tab():
            return False
        
        # Step 3: Click Manage security defaults
        if not self._click_manage_security_defaults():
            return False
        
        return True
    
    def _go_to_overview(self) -> bool:
        """Navigate to Tenant Overview page."""
        self._log("Step 1: Going to Overview page...")
        
        # Direct URL navigation is most reliable
        overview_url = "https://entra.microsoft.com/#view/Microsoft_AAD_IAM/TenantOverview.ReactView"
        self.driver.get(overview_url)
        
        self._log("Waiting for Overview page to load...")
        
        # Wait for page to load by checking for Properties tab
        for i in range(self.WAIT_PAGE_LOAD // 2):
            time.sleep(2)
            
            # Check URL
            current_url = self.driver.current_url
            if "TenantOverview" not in current_url:
                self._log(f"URL changed unexpectedly: {current_url}", "warning")
                # Try navigating again
                self.driver.get(overview_url)
                time.sleep(5)
                continue
            
            # Check for Properties tab in page
            page_source = self.driver.page_source
            if "Properties" in page_source:
                self._log(f"Overview page loaded (took {i*2}s)")
                self._screenshot("nav_01_overview_loaded")
                
                # Extra wait for any animations
                time.sleep(self.WAIT_AFTER_ACTION)
                return True
            
            # Also check iframes
            try:
                iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
                for iframe in iframes:
                    try:
                        self.driver.switch_to.frame(iframe)
                        if "Properties" in self.driver.page_source:
                            self.driver.switch_to.default_content()
                            self._log(f"Overview page loaded (found in iframe, took {i*2}s)")
                            self._screenshot("nav_01_overview_loaded")
                            time.sleep(self.WAIT_AFTER_ACTION)
                            return True
                        self.driver.switch_to.default_content()
                    except:
                        self.driver.switch_to.default_content()
            except:
                pass
        
        self._log("Timeout waiting for Overview page", "error")
        self._screenshot("nav_error_overview_timeout")
        return False
    
    def _click_properties_tab(self) -> bool:
        """Click the Properties tab."""
        self._log("Step 2: Clicking Properties tab...")
        
        # Properties tab is often in an iframe
        for attempt in range(3):
            self._log(f"Properties click attempt {attempt + 1}...")
            
            # First try main document
            self.driver.switch_to.default_content()
            
            clicked = self.driver.execute_script("""
                // Find Properties tab by text
                var elements = document.querySelectorAll('*');
                for (var i = 0; i < elements.length; i++) {
                    var el = elements[i];
                    if (el.textContent.trim() === 'Properties' && 
                        el.offsetParent !== null &&
                        el.tagName !== 'SCRIPT' &&
                        el.tagName !== 'STYLE') {
                        el.click();
                        return 'main_document';
                    }
                }
                return null;
            """)
            
            if clicked:
                self._log(f"Clicked Properties in {clicked}")
                time.sleep(self.WAIT_PAGE_TRANSITION)
                self._screenshot("nav_02_after_properties")
                return True
            
            # Try each iframe
            iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
            self._log(f"Checking {len(iframes)} iframes...")
            
            for i, iframe in enumerate(iframes):
                try:
                    self.driver.switch_to.frame(iframe)
                    
                    clicked = self.driver.execute_script("""
                        var elements = document.querySelectorAll('*');
                        for (var i = 0; i < elements.length; i++) {
                            var el = elements[i];
                            if (el.textContent.trim() === 'Properties' && 
                                el.offsetParent !== null &&
                                el.tagName !== 'SCRIPT' &&
                                el.tagName !== 'STYLE') {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    """)
                    
                    if clicked:
                        self._log(f"Clicked Properties in iframe {i}")
                        time.sleep(self.WAIT_PAGE_TRANSITION)
                        self._screenshot("nav_02_after_properties")
                        self.driver.switch_to.default_content()
                        return True
                    
                    self.driver.switch_to.default_content()
                    
                except Exception as e:
                    self.driver.switch_to.default_content()
            
            time.sleep(self.WAIT_AFTER_ACTION)
        
        self._log("Failed to click Properties tab", "error")
        self._screenshot("nav_error_properties")
        return False
    
    def _click_manage_security_defaults(self) -> bool:
        """Click 'Manage security defaults' link to open the panel."""
        self._log("Step 3: Clicking 'Manage security defaults'...")
        
        for attempt in range(5):
            self._log(f"Manage security defaults click attempt {attempt + 1}...")
            
            # First try in iframes (where Properties content usually is)
            self.driver.switch_to.default_content()
            iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
            
            for i, iframe in enumerate(iframes):
                try:
                    self.driver.switch_to.frame(iframe)
                    
                    # Scroll to bottom of iframe content to find Security defaults section
                    self.driver.execute_script("""
                        // Scroll the main scrollable container
                        var scrollables = document.querySelectorAll('[class*="scroll"], [style*="overflow"]');
                        for (var i = 0; i < scrollables.length; i++) {
                            scrollables[i].scrollTop = scrollables[i].scrollHeight;
                        }
                        // Also scroll the document
                        window.scrollTo(0, document.body.scrollHeight);
                        document.documentElement.scrollTop = document.documentElement.scrollHeight;
                    """)
                    
                    time.sleep(self.WAIT_SHORT)
                    
                    # Look for the LINK specifically (not just any element with text)
                    clicked = self.driver.execute_script("""
                        // Find specifically a link or clickable element with this exact text
                        var links = document.querySelectorAll('a, [role="link"], [role="button"]');
                        for (var i = 0; i < links.length; i++) {
                            var el = links[i];
                            if (el.textContent.trim() === 'Manage security defaults' ||
                                el.textContent.includes('Manage security defaults')) {
                                el.scrollIntoView({behavior: 'instant', block: 'center'});
                                el.click();
                                return 'link_clicked';
                            }
                        }
                        
                        // Try finding by partial text match on any clickable element
                        var clickables = document.querySelectorAll('a, button, span[class*="link"], div[class*="link"]');
                        for (var i = 0; i < clickables.length; i++) {
                            if (clickables[i].textContent.includes('Manage security defaults') &&
                                clickables[i].offsetParent !== null) {
                                clickables[i].scrollIntoView({behavior: 'instant', block: 'center'});
                                clickables[i].click();
                                return 'clickable_clicked';
                            }
                        }
                        
                        return null;
                    """)
                    
                    if clicked:
                        self._log(f"Clicked 'Manage security defaults' in iframe {i}: {clicked}")
                        self.driver.switch_to.default_content()
                        time.sleep(self.WAIT_PAGE_TRANSITION)
                        
                        # Verify panel opened by checking for dropdown
                        if self._verify_panel_open():
                            self._screenshot("nav_03_panel_opened")
                            return True
                        else:
                            self._log("Panel didn't open, retrying...")
                            continue
                    
                    self.driver.switch_to.default_content()
                    
                except Exception as e:
                    self._log(f"Iframe {i} error: {e}")
                    self.driver.switch_to.default_content()
            
            # Also try main document
            self.driver.switch_to.default_content()
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(self.WAIT_SHORT)
            
            clicked = self.driver.execute_script("""
                var links = document.querySelectorAll('a, [role="link"], [role="button"]');
                for (var i = 0; i < links.length; i++) {
                    var el = links[i];
                    if (el.textContent.includes('Manage security defaults') &&
                        el.offsetParent !== null) {
                        el.scrollIntoView({behavior: 'instant', block: 'center'});
                        el.click();
                        return 'main_clicked';
                    }
                }
                return null;
            """)
            
            if clicked:
                self._log(f"Clicked 'Manage security defaults' in main: {clicked}")
                time.sleep(self.WAIT_PAGE_TRANSITION)
                
                if self._verify_panel_open():
                    self._screenshot("nav_03_panel_opened")
                    return True
            
            time.sleep(self.WAIT_AFTER_ACTION)
        
        self._log("Failed to click 'Manage security defaults'", "error")
        self._screenshot("nav_error_manage_link")
        return False
    
    def _verify_panel_open(self) -> str:
        """
        Verify the Security defaults panel is open AND detect current state.

        Returns:
            "opened_enabled"  — panel open, dropdown shows "Enabled (recommended)"
            "opened_disabled" — panel open, dropdown shows "Disabled (not recommended)"
            "opened_unknown"  — panel open but state indeterminate
            ""                — panel not detected
        """
        self._log("Verifying panel opened...")

        # Wait for panel to appear
        for i in range(10):
            time.sleep(1)

            state = self.driver.execute_script("""
                // Check for the toggle button that's in the panel
                var toggle = document.querySelector('span[role="button"][aria-label="Toggle"]');
                var panelPresent = !!(toggle && toggle.offsetParent !== null);

                if (!panelPresent) {
                    // Secondary check: panel text content
                    var src = document.body.innerHTML;
                    if (src.includes('Enabled (recommended)') && src.includes('Security defaults')) {
                        panelPresent = true;
                    } else if (src.includes('Disabled (not recommended)') && src.includes('Security defaults')) {
                        panelPresent = true;
                    }
                }

                if (!panelPresent) return '';

                // Panel is open — inspect current dropdown value
                var dropdown = document.querySelector('.fxc-dropdown-input');
                var dropText = dropdown ? (dropdown.textContent || '').trim() : '';

                // Also look for the explicit text anywhere visible in the open panel
                var bodyText = document.body.innerText || '';

                if (dropText.startsWith('Disabled') ||
                    (bodyText.includes('Disabled (not recommended)') &&
                     !bodyText.includes('Enabled (recommended)'))) {
                    return 'opened_disabled';
                }

                if (dropText.startsWith('Enabled') ||
                    bodyText.includes('Enabled (recommended)')) {
                    return 'opened_enabled';
                }

                return 'opened_unknown';
            """)

            if state:
                self._log(f"Panel verified open: {state}")
                return state

        self._log("Panel not detected after waiting")
        return ""
    
    # =========================================================================
    # DISABLE SECURITY DEFAULTS
    # =========================================================================
    
    def _disable_security_defaults(self) -> bool:
        """
        Disable Security Defaults in the panel.
        
        Uses EXACT selectors from UI recorder:
        1. Click dropdown toggle: span[role="button"][aria-label="Toggle"]
        2. Select Disabled: div[role="treeitem"] containing "Disabled"
        3. Select reason: span containing "Too many multifactor..."
        4. Click Save: span.fxs-button-text containing "Save"
        5. Click Disable confirm: span.fxs-button-text containing "Disable"
        """
        self._log("Disabling Security Defaults...")
        
        # Ensure we're in main document (panel opens there)
        self.driver.switch_to.default_content()
        time.sleep(self.WAIT_AFTER_ACTION)
        
        self._screenshot("disable_01_starting")
        
        # Step 1: Click dropdown
        if not self._click_dropdown():
            return False
        
        # Step 2: Select Disabled
        if not self._select_disabled():
            return False
        
        # Step 3: Select reason
        if not self._select_reason():
            return False
        
        # Step 4: Click Save
        if not self._click_save():
            return False
        
        # Step 5: Click Disable confirmation if it appears
        if not self._click_disable_confirmation():
            self._log("Disable confirmation dialog was present but all click methods failed", "error")
            self._screenshot("disable_error_confirmation")
            return False

        # Step 6: Post-save verification — re-read panel state to confirm the
        # change actually persisted. Microsoft sometimes rolls back the save
        # silently if there's a downstream validation error.
        self._log("Post-save verification...")
        time.sleep(self.WAIT_AFTER_ACTION)
        final_state = self._verify_panel_open()
        if final_state == "opened_disabled":
            self._log("Post-save verification: SD confirmed disabled ✓")
            self._screenshot("disable_complete")
            return True

        # Panel may have closed after save — try re-opening it once to re-read state
        self._log(f"Post-save state is '{final_state}', re-opening panel to re-verify...")
        if self._navigate_to_security_defaults():
            final_state = self._verify_panel_open()
            if final_state == "opened_disabled":
                self._log("Post-save re-verification: SD confirmed disabled ✓")
                self._screenshot("disable_complete")
                return True

        self._log(
            f"Post-save verification FAILED: panel state is '{final_state}' (expected 'opened_disabled')",
            "error",
        )
        self._screenshot("disable_error_post_save_verify")
        return False
    
    def _click_dropdown(self) -> bool:
        """Click the dropdown toggle to open options."""
        self._log("Clicking dropdown...")
        
        # Import ActionChains
        from selenium.webdriver.common.action_chains import ActionChains
        
        # First scroll the panel to make sure dropdown is visible
        self.driver.execute_script("""
            var toggle = document.querySelector('span[role="button"][aria-label="Toggle"]');
            if (toggle) {
                toggle.scrollIntoView({behavior: 'instant', block: 'center'});
            }
        """)
        
        time.sleep(self.WAIT_SHORT)
        
        for attempt in range(5):
            self._log(f"Dropdown attempt {attempt + 1}...")
            
            clicked = False
            
            # Method 1: Selenium direct click
            try:
                toggle = self.driver.find_element(By.CSS_SELECTOR, 'span[role="button"][aria-label="Toggle"]')
                self._log("Found toggle element")
                
                # Scroll into view
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", toggle)
                time.sleep(0.5)
                
                toggle.click()
                self._log("Selenium click completed")
                clicked = True
            except Exception as e:
                self._log(f"Selenium click failed: {e}")
            
            # Method 2: ActionChains click
            if not clicked:
                try:
                    toggle = self.driver.find_element(By.CSS_SELECTOR, 'span[role="button"][aria-label="Toggle"]')
                    actions = ActionChains(self.driver)
                    actions.move_to_element(toggle).pause(0.3).click().perform()
                    self._log("ActionChains click completed")
                    clicked = True
                except Exception as e:
                    self._log(f"ActionChains click failed: {e}")
            
            # Method 3: JavaScript click with event dispatch
            if not clicked:
                result = self.driver.execute_script("""
                    var toggle = document.querySelector('span[role="button"][aria-label="Toggle"]');
                    if (toggle) {
                        // Dispatch multiple events to ensure click registers
                        toggle.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                        toggle.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                        toggle.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                        return 'events_dispatched';
                    }
                    return 'not_found';
                """)
                self._log(f"JS event dispatch result: {result}")
                if result != 'not_found':
                    clicked = True
            
            if not clicked:
                self._log("All click methods failed, retrying...")
                time.sleep(self.WAIT_SHORT)
                continue
            
            # Wait for dropdown to open
            time.sleep(self.WAIT_AFTER_ACTION)
            
            # Take screenshot to see what happened
            self._screenshot(f"disable_dropdown_attempt_{attempt + 1}")
            
            # Check for options - they might be in a dropdown overlay anywhere in the DOM
            options_found = self.driver.execute_script("""
                // Look everywhere for dropdown options
                var options = document.querySelectorAll('div[role="treeitem"], .fxc-dropdown-option');
                
                for (var i = 0; i < options.length; i++) {
                    var text = options[i].textContent;
                    if (text.includes('Disabled') && options[i].offsetParent !== null) {
                        return {found: true, count: options.length};
                    }
                }
                
                // Check if dropdown shows "open" state
                var openDropdown = document.querySelector('.fxc-dropdown-open');
                var dropdownPopup = document.querySelector('.fxc-dropdown-popup, .fxc-dropdown-list');
                
                return {
                    found: false, 
                    count: options.length,
                    hasOpenClass: !!openDropdown,
                    hasPopup: !!dropdownPopup
                };
            """)
            
            self._log(f"Options check: {options_found}")
            
            if options_found and options_found.get('found'):
                self._log("Dropdown opened successfully - options visible")
                self._screenshot("disable_02_dropdown_open")
                return True
            
            # If we see signs dropdown is opening, wait more
            if options_found and (options_found.get('hasOpenClass') or options_found.get('hasPopup')):
                self._log("Dropdown appears to be opening, waiting for options...")
                time.sleep(self.WAIT_AFTER_ACTION)
                
                # Check again
                options_found2 = self.driver.execute_script("""
                    var options = document.querySelectorAll('div[role="treeitem"], .fxc-dropdown-option');
                    for (var i = 0; i < options.length; i++) {
                        if (options[i].textContent.includes('Disabled') && options[i].offsetParent !== null) {
                            return true;
                        }
                    }
                    return false;
                """)
                
                if options_found2:
                    self._log("Options found after extra wait")
                    self._screenshot("disable_02_dropdown_open")
                    return True
            
            self._log("Dropdown options not visible, retrying...")
            time.sleep(self.WAIT_SHORT)
        
        self._log("Failed to open dropdown after 5 attempts", "error")
        self._screenshot("disable_error_dropdown")
        return False
    
    def _select_disabled(self) -> bool:
        """Select 'Disabled (not recommended)' option."""
        self._log("Selecting 'Disabled'...")
        
        from selenium.webdriver.common.action_chains import ActionChains
        
        # Give dropdown options time to fully render
        time.sleep(self.WAIT_SHORT)
        
        for attempt in range(3):
            self._log(f"Select Disabled attempt {attempt + 1}...")
            
            # Method 1: Try Selenium click
            try:
                options = self.driver.find_elements(By.CSS_SELECTOR, 'div[role="treeitem"], .fxc-dropdown-option')
                self._log(f"Found {len(options)} dropdown options")
                
                for opt in options:
                    if "Disabled" in opt.text:
                        self._log(f"Found Disabled option: {opt.text[:50]}")
                        opt.click()
                        self._log("Selenium click on Disabled completed")
                        time.sleep(self.WAIT_AFTER_ACTION)
                        self._screenshot("disable_03_disabled_selected")
                        return True
            except Exception as e:
                self._log(f"Selenium click failed: {e}")
            
            # Method 2: ActionChains click
            try:
                options = self.driver.find_elements(By.CSS_SELECTOR, 'div[role="treeitem"], .fxc-dropdown-option')
                for opt in options:
                    if "Disabled" in opt.text:
                        self._log("Trying ActionChains click on Disabled...")
                        actions = ActionChains(self.driver)
                        actions.move_to_element(opt).pause(0.3).click().perform()
                        self._log("ActionChains click completed")
                        time.sleep(self.WAIT_AFTER_ACTION)
                        self._screenshot("disable_03_disabled_selected")
                        return True
            except Exception as e:
                self._log(f"ActionChains click failed: {e}")
            
            # Method 3: JavaScript click with events
            result = self.driver.execute_script("""
                var options = document.querySelectorAll('div[role="treeitem"], .fxc-dropdown-option');
                for (var i = 0; i < options.length; i++) {
                    if (options[i].textContent.includes('Disabled') && 
                        options[i].offsetParent !== null) {
                        
                        // Scroll into view
                        options[i].scrollIntoView({behavior: 'instant', block: 'center'});
                        
                        // Dispatch events
                        options[i].dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                        options[i].dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                        options[i].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                        
                        return 'js_events_clicked';
                    }
                }
                return 'not_found';
            """)
            
            self._log(f"JS events result: {result}")
            
            if result != 'not_found':
                time.sleep(self.WAIT_AFTER_ACTION)
                self._screenshot("disable_03_disabled_selected")
                return True
            
            time.sleep(self.WAIT_SHORT)
        
        self._log("Could not select Disabled option", "error")
        self._screenshot("disable_error_no_disabled")
        return False
    
    def _select_reason(self) -> bool:
        """Select the reason for disabling."""
        self._log("Selecting reason...")
        
        from selenium.webdriver.common.action_chains import ActionChains
        
        # Wait for reason options to appear
        time.sleep(self.WAIT_AFTER_ACTION)
        
        # Method 1: Try Selenium click
        try:
            elements = self.driver.find_elements(By.XPATH, "//*[contains(text(), 'Too many multifactor')]")
            self._log(f"Found {len(elements)} elements with reason text")
            
            for el in elements:
                if el.is_displayed():
                    self._log("Clicking reason element with Selenium...")
                    el.click()
                    time.sleep(self.WAIT_AFTER_ACTION)
                    self._screenshot("disable_04_reason_selected")
                    return True
        except Exception as e:
            self._log(f"Selenium reason click failed: {e}")
        
        # Method 2: ActionChains
        try:
            elements = self.driver.find_elements(By.XPATH, "//*[contains(text(), 'Too many multifactor')]")
            for el in elements:
                if el.is_displayed():
                    self._log("Trying ActionChains on reason...")
                    actions = ActionChains(self.driver)
                    actions.move_to_element(el).pause(0.3).click().perform()
                    time.sleep(self.WAIT_AFTER_ACTION)
                    self._screenshot("disable_04_reason_selected")
                    return True
        except Exception as e:
            self._log(f"ActionChains reason click failed: {e}")
        
        # Method 3: JavaScript with events
        result = self.driver.execute_script("""
            var elements = document.querySelectorAll('span, label, div, input');
            for (var i = 0; i < elements.length; i++) {
                var el = elements[i];
                if (el.textContent.includes('Too many multifactor') &&
                    el.offsetParent !== null) {
                    el.scrollIntoView({behavior: 'instant', block: 'center'});
                    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                    el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                    el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                    return 'js_clicked';
                }
            }
            
            // Try radio buttons
            var radios = document.querySelectorAll('input[type="radio"]');
            for (var i = 0; i < radios.length; i++) {
                var parent = radios[i].parentElement;
                if (parent && parent.textContent.includes('Too many')) {
                    radios[i].click();
                    return 'radio_clicked';
                }
            }
            
            return 'not_found';
        """)
        
        self._log(f"Reason selection result: {result}")
        
        time.sleep(self.WAIT_AFTER_ACTION)
        self._screenshot("disable_04_reason_selected")
        
        # Don't fail if reason not found - some tenants might not require it
        return True
    
    def _click_save(self) -> bool:
        """Click the Save button."""
        self._log("Clicking Save...")
        
        from selenium.webdriver.common.action_chains import ActionChains
        
        # Method 1: Selenium click
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, 'span.fxs-button-text, button')
            for btn in buttons:
                if btn.text.strip() == 'Save' and btn.is_displayed():
                    self._log("Clicking Save with Selenium...")
                    btn.click()
                    time.sleep(self.WAIT_AFTER_ACTION)
                    self._screenshot("disable_05_after_save")
                    return True
        except Exception as e:
            self._log(f"Selenium Save click failed: {e}")
        
        # Method 2: ActionChains
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, 'span.fxs-button-text, button')
            for btn in buttons:
                if btn.text.strip() == 'Save' and btn.is_displayed():
                    self._log("Clicking Save with ActionChains...")
                    actions = ActionChains(self.driver)
                    actions.move_to_element(btn).pause(0.3).click().perform()
                    time.sleep(self.WAIT_AFTER_ACTION)
                    self._screenshot("disable_05_after_save")
                    return True
        except Exception as e:
            self._log(f"ActionChains Save click failed: {e}")
        
        # Method 3: JavaScript with events
        result = self.driver.execute_script("""
            var buttons = document.querySelectorAll('span.fxs-button-text, button');
            for (var i = 0; i < buttons.length; i++) {
                if (buttons[i].textContent.trim() === 'Save' && buttons[i].offsetParent !== null) {
                    buttons[i].scrollIntoView({behavior: 'instant', block: 'center'});
                    buttons[i].dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                    buttons[i].dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                    buttons[i].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                    return 'js_clicked';
                }
            }
            return 'not_found';
        """)
        
        self._log(f"Save click result: {result}")
        
        if result == 'not_found':
            self._log("Could not find Save button", "error")
            self._screenshot("disable_error_no_save")
            return False
        
        time.sleep(self.WAIT_AFTER_ACTION)
        self._screenshot("disable_05_after_save")
        return True
    
    def _is_confirmation_dialog_present(self) -> bool:
        """
        Detect whether a confirmation modal/dialog is currently on screen.

        Used to decide whether a failed 'Disable' click is a real error or
        simply the "no confirm modal needed" Microsoft variant.
        """
        try:
            return bool(self.driver.execute_script("""
                // Look for obvious modal/dialog indicators carrying 'Disable' text
                var dialogs = document.querySelectorAll(
                    '[role="dialog"], .fxs-portal-dialog, .fxc-dialog, [class*="dialog"], [class*="Dialog"]'
                );
                for (var i = 0; i < dialogs.length; i++) {
                    var d = dialogs[i];
                    if (d.offsetParent !== null && d.textContent.includes('Disable')) {
                        return true;
                    }
                }
                // Generic: a visible button with just the word 'Disable' on a layer above panel
                var btns = document.querySelectorAll('span.fxs-button-text, button');
                for (var i = 0; i < btns.length; i++) {
                    if (btns[i].textContent.trim() === 'Disable' && btns[i].offsetParent !== null) {
                        return true;
                    }
                }
                return false;
            """))
        except Exception:
            return False

    def _click_disable_confirmation(self) -> bool:
        """
        Click the Disable confirmation button if a confirmation dialog appeared.

        Return semantics:
          True  — button was clicked successfully OR no confirmation dialog
                  was needed (panel went straight to saved state).
          False — a dialog IS present (button visible) but every click
                  method we tried failed. This is a genuine error.
        """
        self._log("Checking for Disable confirmation...")

        from selenium.webdriver.common.action_chains import ActionChains

        time.sleep(self.WAIT_AFTER_ACTION)

        # Track whether we ever saw the confirm button — if we saw it and
        # still couldn't click it, that's a real failure.
        saw_button = False

        # Method 1: Selenium click
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, 'span.fxs-button-text, button')
            for btn in buttons:
                if btn.text.strip() == 'Disable' and btn.is_displayed():
                    saw_button = True
                    self._log("Clicking Disable confirmation with Selenium...")
                    try:
                        btn.click()
                        time.sleep(self.WAIT_AFTER_ACTION)
                        self._screenshot("disable_06_confirmed")
                        return True
                    except Exception as e:
                        self._log(f"Selenium click raised: {e}")
                        break  # Fall through to ActionChains
        except Exception as e:
            self._log(f"Selenium Disable click enumeration failed: {e}")

        # Method 2: ActionChains
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, 'span.fxs-button-text, button')
            for btn in buttons:
                if btn.text.strip() == 'Disable' and btn.is_displayed():
                    saw_button = True
                    self._log("Clicking Disable with ActionChains...")
                    try:
                        actions = ActionChains(self.driver)
                        actions.move_to_element(btn).pause(0.3).click().perform()
                        time.sleep(self.WAIT_AFTER_ACTION)
                        self._screenshot("disable_06_confirmed")
                        return True
                    except Exception as e:
                        self._log(f"ActionChains click raised: {e}")
                        break
        except Exception as e:
            self._log(f"ActionChains Disable click enumeration failed: {e}")

        # Method 3: JavaScript
        result = self.driver.execute_script("""
            var buttons = document.querySelectorAll('span.fxs-button-text, button');
            for (var i = 0; i < buttons.length; i++) {
                if (buttons[i].textContent.trim() === 'Disable' && buttons[i].offsetParent !== null) {
                    buttons[i].dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                    buttons[i].dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                    buttons[i].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                    return 'js_clicked';
                }
            }
            return 'not_found';
        """)

        self._log(f"Disable confirmation JS result: {result}")

        if result == 'js_clicked':
            time.sleep(self.WAIT_AFTER_ACTION)
            self._screenshot("disable_06_confirmed")
            return True

        # Nothing clicked via JS. Decide success vs failure based on
        # whether a dialog was present when we started.
        if not saw_button and not self._is_confirmation_dialog_present():
            self._log("No Disable confirmation dialog present — Microsoft likely skipped it")
            return True

        # Dialog IS present but every click method failed — genuine error
        self._log(
            "Disable confirmation dialog was present but all click methods failed",
            "error",
        )
        self._screenshot("disable_error_confirm_clicks_failed")
        return False
    
    # =========================================================================
    # MAIN ENTRY POINT
    # =========================================================================
    
    def _pre_check_already_disabled(self, creds: TenantCredentials) -> Optional[bool]:
        """
        Non-Selenium pre-check: try to mint a Graph token and read SD state.

        Returns:
            True  — SD is already disabled (full Selenium flow can be skipped)
            False — SD is enabled (proceed with Selenium)
            None  — could not determine (token failed / transient error)

        This is best-effort: ANY failure returns None so the caller proceeds
        with the full Selenium flow.
        """
        try:
            import asyncio as _asyncio
            from app.services.sd_graph import verify_or_repair_sd  # type: ignore

            # Use admin_email's onmicrosoft domain for the token URL.
            # creds.domain is the onmicrosoft sub-domain (e.g. "contoso")
            tenant_domain = creds.domain
            if "." not in tenant_domain:
                tenant_domain = f"{tenant_domain}.onmicrosoft.com"
            elif not tenant_domain.endswith(".onmicrosoft.com"):
                # admin_email should be authoritative
                if "@" in creds.admin_email:
                    td = creds.admin_email.split("@", 1)[1]
                    if td.endswith(".onmicrosoft.com"):
                        tenant_domain = td

            async def _run():
                return await verify_or_repair_sd(
                    tenant_domain=tenant_domain,
                    admin_email=creds.admin_email,
                    admin_password=creds.admin_password,
                    auto_fix=False,
                )

            try:
                loop = _asyncio.get_event_loop()
                if loop.is_running():
                    # We're being called from async context — we shouldn't be,
                    # but just bail out and let Selenium run.
                    return None
            except RuntimeError:
                pass

            res = _asyncio.run(_run())
            action = (res or {}).get("action")
            if action == "already_disabled":
                return True
            if action in ("drift_detected",):
                return False
            # token_failed / unfixable / anything else — inconclusive
            return None
        except Exception as e:
            self._log(f"pre-check exception (non-fatal): {e}")
            return None

    def verify_sd_disabled(self, creds: TenantCredentials) -> Dict:
        """
        Read-only Selenium fallback: open the Security Defaults panel and
        report whether SD is currently disabled WITHOUT attempting to change it.

        Used by the reconciliation layer when Graph ROPC is unavailable and
        we need to know the true state before deciding whether to run a
        repair.

        Returns:
            {"success": bool, "sd_disabled": bool|None, "error": str|None, "domain": str}
        """
        result: Dict = {
            "success": False,
            "sd_disabled": None,
            "error": None,
            "domain": creds.domain,
            "tenant_id": creds.tenant_id,
        }
        try:
            if not self._setup_driver():
                result["error"] = "Browser setup failed"
                return result
            if not self._login(creds):
                result["error"] = "Login failed"
                return result
            if not self._navigate_to_security_defaults():
                result["error"] = "Navigation failed"
                return result

            state = self._verify_panel_open()
            if state == "opened_disabled":
                result["success"] = True
                result["sd_disabled"] = True
            elif state == "opened_enabled":
                result["success"] = True
                result["sd_disabled"] = False
            else:
                result["error"] = f"Could not determine SD state (panel state='{state}')"
        except Exception as e:
            self._log(f"[{creds.domain}] verify_sd_disabled error: {e}", "error")
            result["error"] = str(e)
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None
        return result

    def disable_for_tenant(self, creds: TenantCredentials) -> Dict:
        """
        Complete flow to disable Security Defaults for one tenant.
        
        Returns:
            {"success": bool, "error": str|None, "domain": str}
        """
        result = {"success": False, "error": None, "domain": creds.domain, "tenant_id": creds.tenant_id}

        # Graph pre-check: if SD is already disabled, skip the whole Selenium flow.
        try:
            precheck = self._pre_check_already_disabled(creds)
            if precheck is True:
                self._log(f"[{creds.domain}] Graph pre-check: SD already disabled — skipping Selenium flow")
                result["success"] = True
                result["already_disabled"] = True
                return result
        except Exception as e:
            self._log(f"[{creds.domain}] Pre-check failed (continuing with Selenium): {e}")

        try:
            # Setup browser
            if not self._setup_driver():
                result["error"] = "Browser setup failed"
                return result
            
            # Login
            if not self._login(creds):
                result["error"] = "Login failed"
                return result
            
            # Navigate to Security Defaults panel
            if not self._navigate_to_security_defaults():
                result["error"] = "Navigation failed"
                return result

            # If the panel is already showing Disabled (Selenium-only path),
            # treat this as success to avoid churn.
            current_state = self._verify_panel_open()
            if current_state == "opened_disabled":
                self._log(f"[{creds.domain}] Panel shows SD already disabled — no action needed")
                result["success"] = True
                result["already_disabled"] = True
                return result

            # Disable Security Defaults
            if not self._disable_security_defaults():
                result["error"] = "Failed to disable Security Defaults"
                return result
            
            result["success"] = True
            self._log(f"[{creds.domain}] COMPLETE!")
            
        except Exception as e:
            self._log(f"[{creds.domain}] Error: {e}", "error")
            result["error"] = str(e)
            self._screenshot("error_exception")
        
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
                self.driver = None
        
        return result
    
    def disable_for_batch(self, tenants: List[TenantCredentials]) -> Dict:
        """
        Disable Security Defaults for multiple tenants sequentially.
        
        Returns:
            {
                "total": int,
                "successful": int,
                "failed": int,
                "results": [{"domain": str, "success": bool, "error": str|None}, ...]
            }
        """
        results = []
        successful = 0
        failed = 0
        
        self._log(f"Starting batch processing for {len(tenants)} tenants...")
        
        for i, creds in enumerate(tenants):
            self._log(f"=" * 50)
            self._log(f"Processing {i+1}/{len(tenants)}: {creds.domain}")
            self._log(f"=" * 50)
            
            result = self.disable_for_tenant(creds)
            results.append(result)
            
            if result['success']:
                successful += 1
                self._log(f"✓ {creds.domain}: SUCCESS")
            else:
                failed += 1
                self._log(f"✗ {creds.domain}: FAILED - {result.get('error')}", "error")
            
            # Progress update
            self._log(f"Progress: {i+1}/{len(tenants)} ({successful} success, {failed} failed)")
            
            # Pause between tenants to avoid rate limiting
            if i < len(tenants) - 1:
                self._log("Waiting 5s before next tenant...")
                time.sleep(5)
        
        summary = {
            "total": len(tenants),
            "successful": successful,
            "failed": failed,
            "results": results
        }
        
        self._log(f"=" * 50)
        self._log(f"BATCH COMPLETE: {successful}/{len(tenants)} successful")
        self._log(f"=" * 50)
        
        return summary


# =============================================================================
# DATABASE INTEGRATION HELPERS
# =============================================================================

async def get_tenants_needing_security_defaults_disabled(db_session) -> List[TenantCredentials]:
    """
    Fetch tenants from database that need Security Defaults disabled.
    
    Assumes your tenants table has:
    - admin_email
    - admin_password
    - totp_secret
    - domain
    - security_defaults_disabled (boolean)
    """
    from sqlalchemy import text
    
    query = text("""
        SELECT 
            id,
            domain,
            admin_email,
            admin_password,
            totp_secret
        FROM tenants
        WHERE security_defaults_disabled = false
        AND admin_email IS NOT NULL
        AND totp_secret IS NOT NULL
        ORDER BY created_at
    """)
    
    result = await db_session.execute(query)
    rows = result.fetchall()
    
    tenants = []
    for row in rows:
        tenants.append(TenantCredentials(
            tenant_id=str(row.id),
            domain=row.domain,
            admin_email=row.admin_email,
            admin_password=row.admin_password,
            totp_secret=row.totp_secret
        ))
    
    return tenants


async def update_tenant_security_defaults_status(db_session, tenant_id: str, disabled: bool, error: str = None):
    """Update tenant's security_defaults_disabled status in database."""
    from sqlalchemy import text
    
    query = text("""
        UPDATE tenants
        SET security_defaults_disabled = :disabled,
            security_defaults_error = :error,
            security_defaults_updated_at = NOW()
        WHERE id = :tenant_id
    """)
    
    await db_session.execute(query, {
        "tenant_id": tenant_id,
        "disabled": disabled,
        "error": error
    })
    await db_session.commit()


async def disable_security_defaults_batch_with_db(db_session, batch_size: int = 10, worker_id: int = 0) -> Dict:
    """
    Production function to disable Security Defaults for a batch of tenants.
    
    - Fetches tenants from database
    - Processes them sequentially
    - Updates database with results
    
    Returns summary of batch processing.
    """
    logger.info(f"[W{worker_id}] Fetching tenants needing Security Defaults disabled...")
    
    tenants = await get_tenants_needing_security_defaults_disabled(db_session)
    
    if not tenants:
        logger.info(f"[W{worker_id}] No tenants need Security Defaults disabled")
        return {"total": 0, "successful": 0, "failed": 0, "results": []}
    
    # Limit to batch size
    tenants = tenants[:batch_size]
    logger.info(f"[W{worker_id}] Processing {len(tenants)} tenants...")
    
    disabler = SecurityDefaultsDisabler(headless=True, worker_id=worker_id)
    
    results = []
    successful = 0
    failed = 0
    
    for i, creds in enumerate(tenants):
        logger.info(f"[W{worker_id}] Processing {i+1}/{len(tenants)}: {creds.domain}")
        
        result = disabler.disable_for_tenant(creds)
        results.append(result)
        
        # Update database
        await update_tenant_security_defaults_status(
            db_session,
            creds.tenant_id,
            disabled=result['success'],
            error=result.get('error')
        )
        
        if result['success']:
            successful += 1
            logger.info(f"[W{worker_id}] ✓ {creds.domain}: SUCCESS")
        else:
            failed += 1
            logger.error(f"[W{worker_id}] ✗ {creds.domain}: FAILED - {result.get('error')}")
        
        # Pause between tenants
        if i < len(tenants) - 1:
            time.sleep(5)
    
    return {
        "total": len(tenants),
        "successful": successful,
        "failed": failed,
        "results": results
    }


# =============================================================================
# STANDALONE TESTING
# =============================================================================

if __name__ == "__main__":
    import sys
    import os
    
    # Windows-friendly screenshot directory
    if os.name == 'nt':
        SCREENSHOT_DIR = r"C:\tmp\screenshots\step7"
    
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    
    print("=" * 60)
    print("  STEP 7: DISABLE SECURITY DEFAULTS")
    print("=" * 60)
    print()
    print("Options:")
    print("  1. Test single tenant (visible browser)")
    print("  2. Test single tenant (headless)")
    print("  3. Test batch from CSV file (headless)")
    print()
    
    choice = input("Enter choice (1/2/3): ").strip()
    
    if choice == "1":
        # Single tenant, visible browser
        print()
        admin_email = input("Admin Email: ").strip()
        admin_password = input("Admin Password: ").strip()
        totp_secret = input("TOTP Secret: ").strip()
        
        domain = admin_email.split("@")[1].replace(".onmicrosoft.com", "") if "@" in admin_email else "unknown"
        
        creds = TenantCredentials(
            tenant_id="test",
            admin_email=admin_email,
            admin_password=admin_password,
            totp_secret=totp_secret,
            domain=domain,
        )
        
        print()
        print(f"Testing for: {creds.admin_email}")
        print(f"Screenshots: {SCREENSHOT_DIR}")
        print("-" * 60)
        
        input("Press ENTER to start...")
        
        disabler = SecurityDefaultsDisabler(headless=False, worker_id=0)
        result = disabler.disable_for_tenant(creds)
        
        print()
        print("=" * 60)
        if result['success']:
            print("  SUCCESS! Security Defaults disabled.")
        else:
            print(f"  FAILED: {result.get('error')}")
        print("=" * 60)
        
    elif choice == "2":
        # Single tenant, headless
        print()
        admin_email = input("Admin Email: ").strip()
        admin_password = input("Admin Password: ").strip()
        totp_secret = input("TOTP Secret: ").strip()
        
        domain = admin_email.split("@")[1].replace(".onmicrosoft.com", "") if "@" in admin_email else "unknown"
        
        creds = TenantCredentials(
            tenant_id="test",
            admin_email=admin_email,
            admin_password=admin_password,
            totp_secret=totp_secret,
            domain=domain,
        )
        
        print()
        print(f"Testing HEADLESS for: {creds.admin_email}")
        print(f"Screenshots: {SCREENSHOT_DIR}")
        print("-" * 60)
        
        disabler = SecurityDefaultsDisabler(headless=True, worker_id=0)
        result = disabler.disable_for_tenant(creds)
        
        print()
        print("=" * 60)
        if result['success']:
            print("  SUCCESS! Security Defaults disabled.")
        else:
            print(f"  FAILED: {result.get('error')}")
        print("=" * 60)
        
    elif choice == "3":
        # Batch from CSV
        import csv
        
        print()
        csv_path = input("CSV file path (columns: admin_email,admin_password,totp_secret): ").strip()
        
        if not os.path.exists(csv_path):
            print(f"File not found: {csv_path}")
            sys.exit(1)
        
        tenants = []
        with open(csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                domain = row['admin_email'].split("@")[1].replace(".onmicrosoft.com", "")
                tenants.append(TenantCredentials(
                    tenant_id=domain,
                    admin_email=row['admin_email'],
                    admin_password=row['admin_password'],
                    totp_secret=row['totp_secret'],
                    domain=domain,
                ))
        
        print(f"Loaded {len(tenants)} tenants from CSV")
        print("-" * 60)
        
        confirm = input(f"Process all {len(tenants)} tenants in HEADLESS mode? (yes/no): ").strip()
        if confirm.lower() != 'yes':
            print("Cancelled")
            sys.exit(0)
        
        disabler = SecurityDefaultsDisabler(headless=True, worker_id=0)
        summary = disabler.disable_for_batch(tenants)
        
        print()
        print("=" * 60)
        print(f"  BATCH COMPLETE")
        print(f"  Total: {summary['total']}")
        print(f"  Successful: {summary['successful']}")
        print(f"  Failed: {summary['failed']}")
        print("=" * 60)
        
        # Print failed tenants
        if summary['failed'] > 0:
            print()
            print("Failed tenants:")
            for r in summary['results']:
                if not r['success']:
                    print(f"  - {r['domain']}: {r['error']}")
    
    else:
        print("Invalid choice")
        sys.exit(1)
    
    print()
    input("Press ENTER to exit...")
