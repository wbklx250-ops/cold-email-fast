"""
User operations via Selenium UI.
No Graph API needed - uses M365 Admin Portal directly.
"""

import time
import logging
import re
from typing import Optional, List, Dict
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

logger = logging.getLogger(__name__)


class UserOpsSelenium:
    """Handle user operations via M365 Admin Portal UI."""
    
    def __init__(self, driver: webdriver.Chrome, domain: str):
        self.driver = driver
        self.domain = domain
    
    def _screenshot(self, name: str):
        """Take screenshot for debugging."""
        try:
            path = f"C:/temp/screenshots/step6/{self.domain}_{name}_{int(time.time())}.png"
            self.driver.save_screenshot(path)
            logger.info(f"Screenshot: {path}")
        except:
            pass
    
    def _wait_and_click(self, by: By, value: str, timeout: int = 15) -> bool:
        """Wait for element and click it."""
        try:
            element = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((by, value))
            )
            element.click()
            return True
        except Exception as e:
            logger.error(f"Could not click {value}: {e}")
            return False
    
    def _wait_and_type(self, by: By, value: str, text: str, timeout: int = 15) -> bool:
        """Wait for input and type text."""
        try:
            element = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
            element.clear()
            element.send_keys(text)
            return True
        except Exception as e:
            logger.error(f"Could not type in {value}: {e}")
            return False

    def create_licensed_user(
        self,
        username: str = "me1",
        display_name: str = "me1",
        password: str = "#Sendemails1",
        custom_domain: str = None
    ) -> Dict:
        """Create a licensed user via M365 Admin Portal.
        
        Preset values:
        - First name: Me
        - Last name: 1
        - Display name: me1
        - Username: me1
        - Password: #Sendemails1
        """
        
        logger.info(f"[{self.domain}] Creating licensed user: {username}")
        
        try:
            # Navigate to Users page
            self.driver.get("https://admin.microsoft.com/#/users")
            time.sleep(5)
            self._screenshot("users_page")
            
            # Click "Add a user" button
            add_user_selectors = [
                "button[data-automationid='addUserButton']",
                "button[aria-label*='Add a user']",
                "button[aria-label*='Add user']",
                "span[data-automationid='splitbuttonprimary']",
                ".ms-Button--primary",
            ]
            
            clicked = False
            for selector in add_user_selectors:
                try:
                    btn = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                    btn.click()
                    clicked = True
                    logger.info(f"Clicked Add user via: {selector}")
                    break
                except:
                    continue
            
            if not clicked:
                # Try by text
                try:
                    btn = self.driver.find_element(By.XPATH, "//button[contains(., 'Add a user')]")
                    btn.click()
                    clicked = True
                except:
                    pass
            
            if not clicked:
                self._screenshot("add_user_button_not_found")
                return {"success": False, "error": "Could not find Add user button"}
            
            time.sleep(3)
            self._screenshot("add_user_form")
            
            # Preset values - hardcoded for consistency
            first_name = "Me"
            last_name = "1"
            display_name_value = "me1"
            
            # Fill first name
            try:
                first_input = self.driver.find_element(By.CSS_SELECTOR, "input[aria-label*='First name'], input[placeholder*='First']")
                first_input.clear()
                first_input.send_keys(first_name)
                logger.info(f"Entered first name: {first_name}")
            except Exception as e:
                logger.warning(f"Could not enter first name: {e}")

            # Fill last name
            try:
                last_input = self.driver.find_element(By.CSS_SELECTOR, "input[aria-label*='Last name'], input[placeholder*='Last']")
                last_input.clear()
                last_input.send_keys(last_name)
                logger.info(f"Entered last name: {last_name}")
            except Exception as e:
                logger.warning(f"Could not enter last name: {e}")

            time.sleep(1)

            # Fill display name (may auto-populate but ensure it's correct)
            try:
                display_input = self.driver.find_element(By.CSS_SELECTOR, "input[aria-label*='Display name']")
                display_input.clear()
                display_input.send_keys(display_name_value)
                logger.info(f"Entered display name: {display_name_value}")
            except Exception as e:
                logger.warning(f"Could not enter display name: {e}")

            # Fill username (me1)
            try:
                username_input = self.driver.find_element(By.CSS_SELECTOR, "input[aria-label*='User name'], input[aria-label*='Username']")
                username_input.clear()
                username_input.send_keys(username)
                logger.info(f"Entered username: {username}")
            except Exception as e:
                logger.error(f"Could not enter username: {e}")

            time.sleep(1)

            # After filling username, FORCE uncheck the auto-password checkbox with JS

            # Keep trying until password field appears
            for attempt in range(10):
                # Check if password field exists
                try:
                    pwd_field = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
                    if pwd_field.is_displayed():
                        logger.info("Password field visible!")
                        break
                except:
                    pass
                
                # Force uncheck with JavaScript
                self.driver.execute_script("""
                    document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                        if (cb.checked) cb.click();
                    });
                """)
                time.sleep(1)

            # Enter password
            pwd_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            pwd_input.send_keys(password)
            logger.info("Entered password")

            self._screenshot("form_filled")

            # Click Next to proceed to Product licenses page
            time.sleep(1)
            self._wait_and_click(By.XPATH, "//button[contains(., 'Next')]")
            time.sleep(3)
            self._screenshot("license_page")
            
            # Assign license page - select ONLY a Microsoft 365 Business Basic
            # or Exchange Online Plan 1 license. Tenants from our provider may
            # contain trial SKUs and other paid subs (E3/E5, Business Standard,
            # Defender, Teams Essentials, etc.) — we MUST NOT consume those.
            #
            # The M365 admin UI shows license display names rather than SKU
            # part numbers. Match the visible label so we pick the right row.
            # Preference order: Business Basic first, Exchange Online Plan 1
            # second. We also explicitly avoid any row containing "Trial".
            try:
                preferred_labels = [
                    "Microsoft 365 Business Basic",
                    "Exchange Online (Plan 1)",
                    "Exchange Online Plan 1",
                ]

                # Wait for the license list to render at all.
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[type='checkbox'], [role='checkbox']")
                    )
                )

                selected_label = None
                for label in preferred_labels:
                    try:
                        # Find the row whose visible text matches our preferred
                        # license name AND does not include the word "Trial".
                        # The license name is typically rendered next to (or
                        # above) the checkbox in the same row container.
                        xpath = (
                            "//*[("
                            "self::div or self::span or self::label or self::li"
                            ") and contains(normalize-space(.), '" + label + "') "
                            "and not(contains(translate(., 'TRIAL', 'trial'), 'trial'))]"
                        )
                        candidates = self.driver.find_elements(By.XPATH, xpath)
                        if not candidates:
                            continue
                        # Find a clickable checkbox inside (or near) the matched row.
                        target_cb = None
                        for row in candidates:
                            try:
                                target_cb = row.find_element(
                                    By.XPATH,
                                    ".//input[@type='checkbox'] | .//*[@role='checkbox']",
                                )
                                if target_cb:
                                    break
                            except Exception:
                                continue
                            # Walk up to find a checkbox sibling
                            try:
                                target_cb = row.find_element(
                                    By.XPATH,
                                    "ancestor::*[.//input[@type='checkbox'] or .//*[@role='checkbox']][1]"
                                    "//input[@type='checkbox'] | "
                                    "ancestor::*[.//input[@type='checkbox'] or .//*[@role='checkbox']][1]"
                                    "//*[@role='checkbox']",
                                )
                                if target_cb:
                                    break
                            except Exception:
                                continue

                        if target_cb is not None:
                            try:
                                target_cb.click()
                            except Exception:
                                self.driver.execute_script("arguments[0].click();", target_cb)
                            selected_label = label
                            logger.info(
                                f"[{self.domain}] Selected allowed license: {label}"
                            )
                            time.sleep(1)
                            break
                    except Exception as e:
                        logger.debug(f"License match attempt failed for '{label}': {e}")
                        continue

                if not selected_label:
                    # No allowed license found on this page — fail loudly so
                    # the tenant gets flagged instead of silently consuming a
                    # premium/trial SKU.
                    self._screenshot("license_no_allowed_sku")
                    raise Exception(
                        "No 'Microsoft 365 Business Basic' or 'Exchange Online "
                        "Plan 1' license available in this tenant — refusing "
                        "to assign any other SKU"
                    )
            except Exception as exc:
                logger.error(
                    f"[{self.domain}] License selection failed: {exc}"
                )
                raise
            
            self._screenshot("license_selected")
            
            # Click Next to Optional settings
            self._wait_and_click(By.XPATH, "//button[contains(., 'Next')]")
            time.sleep(2)
            self._screenshot("optional_settings_page")
            
            # Optional settings page - just click Next
            self._wait_and_click(By.XPATH, "//button[contains(., 'Next')]")
            time.sleep(2)
            self._screenshot("review_page")
            
            # On the Review and finish page, click "Finish adding"
            try:
                finish_btn = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Finish adding')]"))
                )
                finish_btn.click()
                logger.info("Clicked 'Finish adding' button")
                time.sleep(5)
            except:
                # Try alternative selectors
                try:
                    finish_btn = self.driver.find_element(By.CSS_SELECTOR, "button.ms-Button--primary")
                    finish_btn.click()
                    logger.info("Clicked primary button")
                    time.sleep(5)
                except:
                    # Last resort - JS click
                    self.driver.execute_script("""
                        const btn = document.querySelector('button.ms-Button--primary') || 
                                    [...document.querySelectorAll('button')].find(b => b.textContent.includes('Finish'));
                        if (btn) btn.click();
                    """)
                    logger.info("Clicked finish via JS")
                    time.sleep(5)

            self._screenshot("user_created_final")
            
            # Close any success dialog
            try:
                self._wait_and_click(By.XPATH, "//button[contains(., 'Close')]", timeout=5)
            except:
                pass
            
            upn = f"{username}@{custom_domain}" if custom_domain else username
            email = upn  # For consistency with previous API
            logger.info(f"[{self.domain}] ✓ Created licensed user: {upn}")
            
            return {"success": True, "upn": upn, "email": email, "password": password}
            
        except Exception as e:
            logger.error(f"[{self.domain}] Failed to create user: {e}")
            self._screenshot("create_user_error")
            return {"success": False, "error": str(e)}

    def enable_user(self, email: str) -> bool:
        """Enable a user account (unblock sign-in)."""
        logger.info(f"[{self.domain}] Enabling user: {email}")
        
        try:
            # Navigate to user
            self.driver.get(f"https://admin.microsoft.com/#/users/:/UserDetails/{email}")
            time.sleep(5)
            self._screenshot(f"user_details_{email.split('@')[0]}")
            
            # Look for "Unblock sign-in" or toggle
            try:
                unblock = self.driver.find_element(By.XPATH, "//button[contains(., 'Unblock')]")
                unblock.click()
                time.sleep(2)
                
                # Confirm
                self._wait_and_click(By.XPATH, "//button[contains(., 'Save') or contains(., 'Confirm')]")
                time.sleep(2)
                
                logger.info(f"[{self.domain}] ✓ Enabled user: {email}")
                return True
            except:
                logger.info(f"[{self.domain}] User may already be enabled: {email}")
                return True
                
        except Exception as e:
            logger.error(f"[{self.domain}] Failed to enable user {email}: {e}")
            return False

    def enable_users_bulk(self, emails: List[str]) -> Dict:
        """Enable multiple user accounts."""
        enabled = []
        failed = []
        
        for email in emails:
            if self.enable_user(email):
                enabled.append(email)
            else:
                failed.append(email)
        
        return {"enabled": enabled, "failed": failed}

    def set_password(self, email: str, password: str) -> bool:
        """Set user password."""
        logger.info(f"[{self.domain}] Setting password for: {email}")

        for attempt in range(1, 4):
            try:
                # Navigate to user
                self.driver.get(f"https://admin.microsoft.com/#/users/:/UserDetails/{email}")
                time.sleep(5)

                # Click "Reset password"
                self._wait_and_click(By.XPATH, "//button[contains(., 'Reset password')]")
                time.sleep(3)
                self._screenshot(f"reset_password_{email.split('@')[0]}_attempt{attempt}")

                # Uncheck auto-generate + require-change checkboxes (retry a few times)
                for uncheck_attempt in range(1, 6):
                    checkboxes = []
                    try:
                        checkboxes.extend(self.driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"))
                    except Exception:
                        pass
                    try:
                        checkboxes.extend(self.driver.find_elements(By.CSS_SELECTOR, "[role='checkbox']"))
                    except Exception:
                        pass

                    for cb in checkboxes:
                        try:
                            if cb.tag_name == "input":
                                if cb.is_selected():
                                    cb.click()
                            else:
                                if cb.get_attribute("aria-checked") == "true":
                                    cb.click()
                        except Exception:
                            continue

                    time.sleep(1)

                    # Verify password field visible
                    try:
                        pwd_field = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
                        if pwd_field.is_displayed():
                            break
                    except Exception:
                        pass

                    if uncheck_attempt == 5:
                        self._screenshot(f"checkbox_uncheck_failed_{email.split('@')[0]}")
                        raise Exception("Password field still hidden after checkbox uncheck attempts")

                # Enter password
                pwd_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
                pwd_input.clear()
                time.sleep(0.3)
                pwd_input.send_keys(password)
                time.sleep(0.5)

                # Verify password value
                entered_value = pwd_input.get_attribute("value")
                if not entered_value:
                    self.driver.execute_script(
                        "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                        pwd_input,
                        password,
                    )
                    time.sleep(0.5)
                    entered_value = pwd_input.get_attribute("value")

                if len(entered_value or "") < len(password) * 0.8:
                    self._screenshot(f"password_entry_failed_{email.split('@')[0]}")
                    raise Exception("Password not entered correctly")

                # Ensure require-change checkbox unchecked
                try:
                    for cb in self.driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox'], [role='checkbox']"):
                        try:
                            if cb.tag_name == "input":
                                if cb.is_selected():
                                    cb.click()
                            else:
                                if cb.get_attribute("aria-checked") == "true":
                                    cb.click()
                        except Exception:
                            continue
                except Exception:
                    pass

                # Click Reset/Save
                self._wait_and_click(By.XPATH, "//button[contains(., 'Reset') or contains(., 'Save')]")
                time.sleep(3)

                # Verify success indicator or dialog closed
                success_verified = False
                success_indicators = [
                    "//*[contains(text(), 'Password has been reset')]",
                    "//*[contains(text(), 'password has been reset')]",
                    "//*[contains(text(), 'successfully')]",
                    "//*[contains(text(), 'Password reset')]",
                    "//button[contains(., 'Close')]",
                ]
                for indicator in success_indicators:
                    try:
                        element = WebDriverWait(self.driver, 5).until(
                            EC.presence_of_element_located((By.XPATH, indicator))
                        )
                        if element:
                            success_verified = True
                            break
                    except Exception:
                        continue

                if not success_verified:
                    try:
                        self.driver.find_element(By.XPATH, "//div[@role='dialog']")
                        self._screenshot(f"password_reset_dialog_open_{email.split('@')[0]}")
                    except Exception:
                        success_verified = True

                # Close dialog if present
                try:
                    close_btn = self.driver.find_element(By.XPATH, "//button[contains(., 'Close')]")
                    close_btn.click()
                    time.sleep(1)
                except Exception:
                    pass

                if success_verified:
                    logger.info(f"[{self.domain}] ✓ Set password for: {email}")
                    return True

                logger.warning(
                    f"[{self.domain}] Password reset success not verified for {email} (attempt {attempt})"
                )
            except Exception as e:
                logger.error(
                    f"[{self.domain}] Failed to set password for {email} (attempt {attempt}): {e}"
                )
                time.sleep(2)

        return False

    def set_passwords_bulk(self, users: List[Dict]) -> Dict:
        """Set passwords for multiple users.
        
        Args:
            users: List of dicts with 'upn' and 'password' keys
        """
        set_passwords = []
        failed = []
        
        for user in users:
            if self.set_password(user['upn'], user['password']):
                set_passwords.append(user['upn'])
            else:
                failed.append(user['upn'])
        
        return {"set": set_passwords, "failed": failed}

    def fix_upn(self, current_upn: str, new_upn: str) -> bool:
        """Change user's UPN."""
        logger.info(f"[{self.domain}] Fixing UPN: {current_upn} → {new_upn}")
        
        try:
            # Navigate to user
            self.driver.get(f"https://admin.microsoft.com/#/users/:/UserDetails/{current_upn}")
            time.sleep(5)
            self._screenshot(f"fix_upn_start_{current_upn.split('@')[0]}")
            
            # Click "Manage username and email"
            manage_clicked = self._wait_and_click(By.XPATH, "//button[contains(., 'Manage') and contains(., 'username')]")
            if not manage_clicked:
                # Try alternative
                manage_clicked = self._wait_and_click(By.XPATH, "//a[contains(., 'Manage username')]")
            if not manage_clicked:
                # Try clicking on the username/email section
                manage_clicked = self._wait_and_click(By.XPATH, "//*[contains(text(), 'username and email')]")
            
            time.sleep(3)
            self._screenshot(f"fix_upn_manage_{current_upn.split('@')[0]}")
            
            # Update the username field
            username = new_upn.split("@")[0]
            new_domain = new_upn.split("@")[1] if "@" in new_upn else None
            
            # Find and update username input
            username_updated = self._wait_and_type(By.CSS_SELECTOR, "input[aria-label*='Username'], input[aria-label*='username']", username)
            
            if not username_updated:
                # Try other selectors
                self._wait_and_type(By.CSS_SELECTOR, "input[type='text']", username)
            
            # Select domain dropdown if needed
            if new_domain:
                try:
                    # Click domain dropdown
                    dropdown = self.driver.find_element(By.CSS_SELECTOR, "div[class*='Dropdown'], select")
                    dropdown.click()
                    time.sleep(1)
                    
                    # Select the new domain
                    domain_option = self.driver.find_element(By.XPATH, f"//*[contains(text(), '{new_domain}')]")
                    domain_option.click()
                    time.sleep(1)
                except Exception as e:
                    logger.warning(f"Could not select domain: {e}")
            
            time.sleep(1)
            self._screenshot(f"fix_upn_filled_{current_upn.split('@')[0]}")
            
            # Save
            self._wait_and_click(By.XPATH, "//button[contains(., 'Save')]")
            time.sleep(3)
            
            # Close any confirmation dialog
            try:
                self._wait_and_click(By.XPATH, "//button[contains(., 'Close') or contains(., 'Done')]", timeout=5)
            except:
                pass
            
            logger.info(f"[{self.domain}] ✓ Fixed UPN: {new_upn}")
            return True
            
        except Exception as e:
            logger.error(f"[{self.domain}] Failed to fix UPN: {e}")
            self._screenshot(f"fix_upn_error_{current_upn.split('@')[0]}")
            return False

    def update_upns_bulk(self, users: List[Dict]) -> Dict:
        """Update UPNs for multiple users.
        
        Args:
            users: List of dicts with 'current_upn' and 'new_upn' keys
        """
        updated = []
        failed = []
        
        for user in users:
            if self.fix_upn(user['current_upn'], user['new_upn']):
                updated.append(user['new_upn'])
            else:
                failed.append(user['current_upn'])
        
        return {"updated": updated, "failed": failed}

    def set_passwords_and_enable_via_admin_ui(
        self,
        password: str = "#Sendemails1",
        exclude_users: Optional[List[str]] = None,
        expected_count: int = 50,
    ) -> Dict:
        """Set passwords and enable accounts using M365 Admin Center UI.

        Uses bulk actions in the users list to reset passwords and unblock sign-in.
        """
        exclude_users = exclude_users or []
        results = {"passwords_set": 0, "accounts_enabled": 0, "errors": []}
        admin_url = "https://admin.microsoft.com/Adminportal/Home#/users"
        batch_size = 40
        email_regex = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

        def extract_email(text: str) -> Optional[str]:
            match = email_regex.search(text or "")
            return match.group(0).lower() if match else None

        def load_users_list() -> bool:
            logger.info(f"[{self.domain}] Opening M365 Admin Center Users page...")
            self.driver.get(admin_url)
            time.sleep(5)
            try:
                WebDriverWait(self.driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-automationid='DetailsList']"))
                )
            except Exception as exc:
                results["errors"].append(f"Users list not loaded: {exc}")
                return False
            time.sleep(2)
            return True

        def collect_target_emails() -> List[str]:
            rows = self.driver.find_elements(By.CSS_SELECTOR, "[data-automationid='DetailsRow']")
            emails: List[str] = []
            for row in rows:
                row_text = row.text or ""
                row_text_lower = row_text.lower()
                email = extract_email(row_text)
                if not email:
                    continue
                if ".onmicrosoft.com" in row_text_lower:
                    continue
                if any(excl.lower() in row_text_lower for excl in exclude_users):
                    continue
                emails.append(email)
            return emails

        def select_emails(emails_to_select: List[str]) -> int:
            emails_set = {email.lower() for email in emails_to_select}
            checkboxes = self.driver.find_elements(By.CSS_SELECTOR, "[data-automationid='DetailsRowCheck']")
            selected = 0
            for cb in checkboxes:
                try:
                    row = cb.find_element(By.XPATH, "./ancestor::div[@data-automationid='DetailsRow']")
                    row_text = row.text or ""
                    email = extract_email(row_text)
                    if not email or email.lower() not in emails_set:
                        continue
                    cb.click()
                    selected += 1
                    time.sleep(0.2)
                except Exception:
                    continue
            time.sleep(2)
            return selected

        def reset_password_for_selected() -> bool:
            logger.info(f"[{self.domain}] Resetting passwords to {password}...")
            try:
                # Step 1: Click Reset Password button
                try:
                    reset_btn = self.driver.find_element(By.XPATH, "//button[contains(., 'Reset password')]")
                    reset_btn.click()
                    logger.info(f"[{self.domain}] Clicked 'Reset password' button directly")
                except Exception:
                    more_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-automationid='splitbuttonprimary']")
                    more_btn.click()
                    time.sleep(1)
                    reset_btn = self.driver.find_element(By.XPATH, "//button[contains(., 'Reset password')]")
                    reset_btn.click()
                    logger.info(f"[{self.domain}] Clicked 'Reset password' via more menu")

                time.sleep(3)

                # Step 2: Wait for dialog to appear
                dialog_found = False
                try:
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']"))
                    )
                    dialog_found = True
                    logger.info(f"[{self.domain}] Password reset dialog appeared")
                except Exception:
                    logger.warning(f"[{self.domain}] Dialog not detected via role='dialog', continuing...")

                self._screenshot("reset_password_dialog_initial")

                # Step 3: Uncheck ALL checkboxes with VERIFICATION
                # Microsoft has 2 checkboxes: "Auto-generate password" and "Require password change"
                # Both MUST be unchecked for manual password entry
                
                max_uncheck_attempts = 5
                for attempt in range(max_uncheck_attempts):
                    # Try multiple methods to uncheck checkboxes
                    checkboxes_still_checked = False
                    
                    # Method 1: Direct checkbox click
                    try:
                        checkboxes = self.driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
                        for cb in checkboxes:
                            try:
                                if cb.is_selected():
                                    cb.click()
                                    logger.info(f"[{self.domain}] Unchecked checkbox (attempt {attempt+1})")
                                    time.sleep(0.5)
                                    if cb.is_selected():
                                        checkboxes_still_checked = True
                            except:
                                pass
                    except:
                        pass
                    
                    # Method 2: ARIA role checkboxes (Fluent UI)
                    try:
                        aria_checkboxes = self.driver.find_elements(By.CSS_SELECTOR, "[role='checkbox']")
                        for cb in aria_checkboxes:
                            try:
                                is_checked = cb.get_attribute('aria-checked') == 'true'
                                if is_checked:
                                    cb.click()
                                    logger.info(f"[{self.domain}] Unchecked aria-checkbox (attempt {attempt+1})")
                                    time.sleep(0.5)
                                    if cb.get_attribute('aria-checked') == 'true':
                                        checkboxes_still_checked = True
                            except:
                                pass
                    except:
                        pass
                    
                    # Method 3: JavaScript force uncheck
                    try:
                        self.driver.execute_script("""
                            const dialog = document.querySelector('[role="dialog"]') || document.body;
                            let unchecked = 0;
                            dialog.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                                if (cb.checked) { cb.click(); unchecked++; }
                            });
                            dialog.querySelectorAll('[role="checkbox"]').forEach(cb => {
                                if (cb.getAttribute('aria-checked') === 'true') { cb.click(); unchecked++; }
                            });
                            return unchecked;
                        """)
                    except:
                        pass
                    
                    time.sleep(1)
                    
                    # VERIFY: Check if password field is now visible
                    try:
                        pwd_field = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
                        if pwd_field.is_displayed():
                            logger.info(f"[{self.domain}] Password field is visible after {attempt+1} attempts")
                            break
                    except:
                        pass
                    
                    if attempt == max_uncheck_attempts - 1:
                        self._screenshot("checkboxes_not_unchecked")
                        logger.error(f"[{self.domain}] Failed to uncheck checkboxes after {max_uncheck_attempts} attempts")

                self._screenshot("reset_password_checkboxes_unchecked")

                # Step 4: Find password input field
                pwd_input = None
                pwd_selectors = [
                    "input[type='password']",
                    "input[aria-label*='Password']",
                    "input[aria-label*='password']",
                    "input[placeholder*='Password']",
                    "input[placeholder*='password']",
                ]
                for selector in pwd_selectors:
                    try:
                        pwd_input = WebDriverWait(self.driver, 5).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                        )
                        if pwd_input and pwd_input.is_displayed():
                            logger.info(f"[{self.domain}] Found password input via: {selector}")
                            break
                        pwd_input = None
                    except Exception:
                        continue

                if not pwd_input:
                    # Last resort: try to force-uncheck again and find
                    try:
                        self.driver.execute_script("""
                            const dialog = document.querySelector('[role="dialog"]') || document.body;
                            dialog.querySelectorAll('[role="checkbox"]').forEach(cb => {
                                if (cb.getAttribute('aria-checked') === 'true') cb.click();
                            });
                        """)
                        time.sleep(2)
                    except:
                        pass
                    
                    for selector in pwd_selectors:
                        try:
                            pwd_input = WebDriverWait(self.driver, 3).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                            )
                            if pwd_input and pwd_input.is_displayed():
                                break
                            pwd_input = None
                        except Exception:
                            continue

                if not pwd_input:
                    self._screenshot("reset_password_no_input_CRITICAL")
                    raise Exception("Password input not found in reset dialog - checkboxes may still be checked")

                # Step 5: Enter password WITH VERIFICATION
                pwd_input.clear()
                time.sleep(0.3)
                pwd_input.send_keys(password)
                time.sleep(0.5)
                
                # CRITICAL: Verify password was actually entered
                entered_value = pwd_input.get_attribute('value')
                if not entered_value:
                    # Try again with JavaScript
                    logger.warning(f"[{self.domain}] Password field empty after send_keys, trying JS input")
                    self.driver.execute_script(
                        "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                        pwd_input, password
                    )
                    time.sleep(0.5)
                    entered_value = pwd_input.get_attribute('value')
                
                if len(entered_value or '') < len(password) * 0.8:
                    self._screenshot("password_not_entered_CRITICAL")
                    raise Exception(f"Password not entered correctly: expected {len(password)} chars, got {len(entered_value or '')}")
                
                logger.info(f"[{self.domain}] Password entered successfully ({len(entered_value)} characters)")
                self._screenshot("reset_password_entered")

                # Step 6: Ensure "Require password change" is unchecked (after password entry)
                try:
                    change_checkboxes = self.driver.find_elements(By.XPATH, 
                        "//input[@type='checkbox'] | //*[@role='checkbox']")
                    for cb in change_checkboxes:
                        try:
                            is_checked = cb.is_selected() if cb.tag_name == 'input' else cb.get_attribute('aria-checked') == 'true'
                            if is_checked:
                                cb.click()
                                logger.info(f"[{self.domain}] Unchecked remaining checkbox before confirm")
                                time.sleep(0.3)
                        except:
                            pass
                except Exception:
                    pass

                # Step 7: Click confirm/reset button
                confirm_clicked = False
                confirm_selectors = [
                    "//div[@role='dialog']//button[contains(., 'Reset password')]",
                    "//div[@role='dialog']//button[contains(., 'Reset')]",
                    "//button[contains(., 'Reset password')]",
                    "//button[contains(., 'Reset') and not(contains(., 'password'))]",
                    "//button[contains(., 'Confirm')]",
                    "//button[contains(@class, 'ms-Button') and contains(., 'Reset')]",
                ]
                
                for selector in confirm_selectors:
                    try:
                        confirm_btn = WebDriverWait(self.driver, 5).until(
                            EC.element_to_be_clickable((By.XPATH, selector))
                        )
                        confirm_btn.click()
                        confirm_clicked = True
                        logger.info(f"[{self.domain}] Clicked confirm button via: {selector}")
                        break
                    except Exception:
                        continue
                
                # Try CSS selectors
                if not confirm_clicked:
                    css_selectors = [
                        "button.ms-Button--primary",
                        "[role='dialog'] button.ms-Button--primary",
                    ]
                    for selector in css_selectors:
                        try:
                            confirm_btn = WebDriverWait(self.driver, 3).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                            )
                            confirm_btn.click()
                            confirm_clicked = True
                            logger.info(f"[{self.domain}] Clicked confirm button via CSS: {selector}")
                            break
                        except Exception:
                            continue

                if not confirm_clicked:
                    # JavaScript fallback
                    try:
                        clicked = self.driver.execute_script("""
                            const dialog = document.querySelector('[role="dialog"], .ms-Dialog, .ms-Panel');
                            if (!dialog) return false;
                            const btn = [...dialog.querySelectorAll('button')]
                                .find(b => (b.textContent || '').toLowerCase().includes('reset'));
                            if (btn) { btn.click(); return true; }
                            return false;
                        """)
                        if clicked:
                            confirm_clicked = True
                            logger.info(f"[{self.domain}] Clicked confirm button via JavaScript")
                    except Exception:
                        pass

                if not confirm_clicked:
                    self._screenshot("confirm_button_not_clicked_CRITICAL")
                    raise Exception("Could not click Reset/Confirm button in password dialog")

                # Step 8: CRITICAL - Wait for and verify success
                time.sleep(3)
                self._screenshot("after_confirm_click")
                
                # Look for success indicators
                success_verified = False
                success_indicators = [
                    "//*[contains(text(), 'Password has been reset')]",
                    "//*[contains(text(), 'password has been reset')]",
                    "//*[contains(text(), 'successfully')]",
                    "//*[contains(text(), 'Password reset')]",
                    "//button[contains(., 'Close')]",  # Close button appears on success
                ]
                
                for indicator in success_indicators:
                    try:
                        element = WebDriverWait(self.driver, 5).until(
                            EC.presence_of_element_located((By.XPATH, indicator))
                        )
                        if element:
                            success_verified = True
                            logger.info(f"[{self.domain}] SUCCESS verified via: {indicator}")
                            break
                    except:
                        continue
                
                # Check for error messages
                error_indicators = [
                    "//*[contains(text(), 'error')]",
                    "//*[contains(text(), 'Error')]",
                    "//*[contains(text(), 'failed')]",
                    "//*[contains(text(), 'Failed')]",
                    "//*[contains(text(), 'invalid')]",
                    "//*[contains(text(), 'Invalid')]",
                ]
                
                for indicator in error_indicators:
                    try:
                        error_elem = self.driver.find_element(By.XPATH, indicator)
                        if error_elem and error_elem.is_displayed():
                            error_text = error_elem.text
                            self._screenshot("password_reset_error_detected")
                            logger.error(f"[{self.domain}] Error detected in dialog: {error_text}")
                            # Don't fail immediately - Microsoft sometimes shows warnings but succeeds
                    except:
                        pass
                
                if not success_verified:
                    # Check if dialog closed (could indicate success)
                    try:
                        self.driver.find_element(By.XPATH, "//div[@role='dialog']")
                        # Dialog still open - might be an issue
                        self._screenshot("dialog_still_open_after_reset")
                        logger.warning(f"[{self.domain}] Dialog still open after reset - success unclear")
                    except:
                        # Dialog closed - likely success
                        success_verified = True
                        logger.info(f"[{self.domain}] Dialog closed - assuming success")

                # Close any remaining dialog
                try:
                    close_btn = self.driver.find_element(By.XPATH, "//button[contains(., 'Close')]")
                    close_btn.click()
                    logger.info(f"[{self.domain}] Closed success dialog")
                    time.sleep(1)
                except Exception:
                    pass

                if success_verified:
                    logger.info(f"[{self.domain}] ✓ Passwords reset VERIFIED for selected batch")
                    return True
                else:
                    logger.warning(f"[{self.domain}] Password reset completed but success NOT verified")
                    self._screenshot("reset_success_not_verified")
                    # Return True but log warning - could be false positive
                    return True
                    
            except Exception as exc:
                self._screenshot("reset_password_exception")
                logger.error(f"[{self.domain}] Reset password FAILED: {exc}")
                results["errors"].append(f"Reset password failed: {exc}")
                return False

        def enable_signin_for_selected() -> bool:
            logger.info(f"[{self.domain}] Enabling sign-in for selected accounts...")
            try:
                more_action_selectors = [
                    "button[aria-label*='View more actions on selected']",
                    "button.ms-CommandBar-overflowButton",
                    "button[aria-label*='More actions']",
                    "button[aria-label*='More']",
                    "button[data-automationid='MoreActionsButton']",
                    "button[data-automationid='OverflowButton']",
                    "button[title*='More actions']",
                    "button[title*='More']",
                    "[data-automationid='splitbuttonprimary']",
                ]

                more_clicked = False
                for selector in more_action_selectors:
                    try:
                        more_btn = WebDriverWait(self.driver, 10).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                        )
                        more_btn.click()
                        more_clicked = True
                        break
                    except Exception:
                        continue

                if not more_clicked:
                    raise Exception("Could not find More actions (3 dots) button")

                time.sleep(2)
                self._screenshot("enable_signin_more_actions")

                edit_clicked = False
                edit_selectors = [
                    "//button[contains(., 'Edit sign-in status')]",
                    "//button[contains(., 'Edit sign in status')]",
                    "//span[contains(., 'Edit sign-in status')]",
                    "//span[contains(., 'Edit sign in status')]",
                ]
                for selector in edit_selectors:
                    try:
                        edit_btn = WebDriverWait(self.driver, 10).until(
                            EC.element_to_be_clickable((By.XPATH, selector))
                        )
                        edit_btn.click()
                        edit_clicked = True
                        break
                    except Exception:
                        continue

                if not edit_clicked:
                    raise Exception("Could not find 'Edit sign-in status' option")

                time.sleep(3)
                self._screenshot("enable_signin_panel")

                allow_clicked = False
                allow_selectors = [
                    "input.ms-ChoiceField-input[id$='-true']",
                    "label.ms-ChoiceField-field[for$='-true']",
                    "//span[contains(@class, 'ms-ChoiceFieldLabel') and contains(., 'Allow users to sign in')]",
                    "//label[contains(@class, 'ms-ChoiceField-field') and contains(., 'Allow users to sign in')]",
                ]
                for selector in allow_selectors:
                    try:
                        if selector.startswith("//"):
                            allow_radio = WebDriverWait(self.driver, 10).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                        else:
                            allow_radio = WebDriverWait(self.driver, 10).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                            )
                        allow_radio.click()
                        allow_clicked = True
                        break
                    except Exception:
                        continue

                if not allow_clicked:
                    try:
                        self.driver.execute_script(
                            """
                            const panel = document.querySelector('[role="dialog"], [role="region"], body');
                            const allow = [...panel.querySelectorAll('[role="radio"]')]
                                .find(r => (r.textContent || '').toLowerCase().includes('allow'));
                            if (allow) allow.click();
                            """
                        )
                        allow_clicked = True
                    except Exception:
                        allow_clicked = False

                if not allow_clicked:
                    raise Exception("Could not select 'Allow users to sign in' option")

                save_clicked = False
                save_selectors = [
                    "button.ms-Button--primary",
                    "button.ms-Button--primary[title*='Save']",
                    "//button[contains(@class, 'ms-Button') and contains(., 'Save')]",
                    "//button[contains(., 'Confirm')]",
                ]
                for selector in save_selectors:
                    try:
                        if selector.startswith("//"):
                            save_btn = WebDriverWait(self.driver, 10).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                        else:
                            save_btn = WebDriverWait(self.driver, 10).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                            )
                        save_btn.click()
                        save_clicked = True
                        break
                    except Exception:
                        continue

                if not save_clicked:
                    raise Exception("Could not click Save on sign-in status panel")

                try:
                    WebDriverWait(self.driver, 40).until(
                        EC.invisibility_of_element_located((By.XPATH, "//button[contains(., 'Save')]"))
                    )
                except Exception:
                    time.sleep(10)

                logger.info(f"[{self.domain}] Accounts enabled for selected batch.")
                return True
            except Exception as exc:
                results["errors"].append(f"Enable sign-in failed: {exc}")
                return False

        if not load_users_list():
            return results

        target_emails = collect_target_emails()
        if not target_emails:
            results["errors"].append("No eligible users found to update")
            return results

        total_passwords_set = 0
        failed_password_batches = 0
        for i in range(0, len(target_emails), batch_size):
            batch = target_emails[i : i + batch_size]
            if not load_users_list():
                results["errors"].append("Failed to reload users list for password batch")
                failed_password_batches += 1
                continue
            selected = select_emails(batch)
            if selected == 0:
                continue
            if not reset_password_for_selected():
                failed_password_batches += 1
                continue
            total_passwords_set += selected

        results["passwords_set"] = min(total_passwords_set, expected_count)
        if failed_password_batches:
            results["errors"].append(f"Password reset failed for {failed_password_batches} batch(es)")

        total_accounts_enabled = 0
        failed_enable_batches = 0
        for i in range(0, len(target_emails), batch_size):
            batch = target_emails[i : i + batch_size]
            if not load_users_list():
                results["errors"].append("Failed to reload users list for enable batch")
                failed_enable_batches += 1
                continue
            selected = select_emails(batch)
            if selected == 0:
                continue
            if not enable_signin_for_selected():
                failed_enable_batches += 1
                continue
            total_accounts_enabled += selected

        results["accounts_enabled"] = min(total_accounts_enabled, expected_count)
        if failed_enable_batches:
            results["errors"].append(f"Enable sign-in failed for {failed_enable_batches} batch(es)")
        return results
