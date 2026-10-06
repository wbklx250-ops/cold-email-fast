"""
PowerShell Exchange Service - Uses device code auth with Selenium MFA handling.
"""

import os
import asyncio

try:
    current_loop = asyncio.get_running_loop()
except RuntimeError:
    current_loop = None

if current_loop is None or type(current_loop).__module__ != "uvloop":
    import nest_asyncio

    nest_asyncio.apply()
import subprocess
import re
import logging
import json
from typing import List, Dict, Any, Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class PowerShellExchangeService:
    """
    Execute Exchange operations via PowerShell with device code auth.
    Selenium handles the device code login + MFA.
    """

    def __init__(self, driver: webdriver.Chrome, admin_email: str, admin_password: str, totp_secret: str = None):
        self.driver = driver
        self.admin_email = admin_email
        self.admin_password = admin_password
        self.totp_secret = totp_secret
        self.ps_process = None
        self.connected = False
        settings = get_settings()
        self._headless_delay_seconds = settings.headless_delay_seconds
        self._headless_page_settle_seconds = settings.headless_page_settle_seconds

    async def _sleep_after_action(self, base_delay: float = 0.5, extra_delay: float = 0.0) -> None:
        delay = base_delay
        try:
            if self.driver and getattr(self.driver, "capabilities", {}).get("browserName"):
                delay = max(delay, self._headless_delay_seconds)
        except Exception:
            delay = max(delay, self._headless_delay_seconds)
        delay += extra_delay
        await asyncio.sleep(delay)

    @staticmethod
    def _ps_escape(value: str) -> str:
        """Escape string for safe use in PowerShell double-quoted strings."""
        if value is None:
            return ""
        return value.replace("`", "``").replace('"', '`"')

    async def connect(self) -> bool:
        """
        Connect to Exchange Online using device code flow.
        Selenium handles the interactive login.
        """
        logger.info("Starting PowerShell Exchange connection with device code...")

        # HEALTH CHECK: Verify browser is alive before attempting device code auth
        if self.driver is None:
            logger.error("No browser driver available for device code auth")
            return False

        try:
            _ = self.driver.current_url
        except Exception as e:
            logger.error("Browser is dead, cannot perform device code auth: %s", e)
            return False

        # Start PowerShell process
        self.ps_process = subprocess.Popen(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        # Import module and start device code auth
        connect_cmd = '''
Import-Module ExchangeOnlineManagement -ErrorAction Stop
Write-Output "MODULE_LOADED"
Connect-ExchangeOnline -Device -ErrorAction Stop
Write-Output "CONNECTED_SUCCESS"
'''

        # Send command
        self.ps_process.stdin.write(connect_cmd)
        self.ps_process.stdin.flush()

        # Read output looking for device code
        device_code = None
        timeout = 30
        start = time.time()

        while time.time() - start < timeout:
            line = self.ps_process.stdout.readline()
            if not line:
                await asyncio.sleep(0.1)
                continue

            line = line.strip()
            logger.debug("PS Output: %s", line)

            # Look for device code pattern
            # "To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code XXXXXXXX to authenticate."
            code_match = re.search(r"enter the code\s+([A-Z0-9]{8,})", line, re.IGNORECASE)
            if code_match:
                device_code = code_match.group(1)
                logger.info("Got device code: %s", device_code)
                break

            # Alternative pattern
            code_match2 = re.search(r"code[:\s]+([A-Z0-9]{8,})", line, re.IGNORECASE)
            if code_match2:
                device_code = code_match2.group(1)
                logger.info("Got device code (alt): %s", device_code)
                break

            if "MODULE_LOADED" in line:
                logger.info("Exchange module loaded")

        if not device_code:
            logger.error("Failed to get device code from PowerShell")
            return False

        # Use Selenium to complete device code login
        success = await self._complete_device_login(device_code)

        if success:
            # Wait for PowerShell to confirm connection
            timeout = 60
            start = time.time()

            while time.time() - start < timeout:
                line = self.ps_process.stdout.readline()
                if not line:
                    await asyncio.sleep(0.5)
                    continue

                line = line.strip()
                logger.debug("PS Output: %s", line)

                if "CONNECTED_SUCCESS" in line or "completed" in line.lower():
                    self.connected = True
                    logger.info("✓ PowerShell connected to Exchange Online!")
                    return True

            # Check if we're actually connected
            test_result = await self._run_command(
                "Get-OrganizationConfig | Select-Object Name | ConvertTo-Json"
            )
            if test_result and "Name" in str(test_result):
                self.connected = True
                logger.info("✓ PowerShell connected to Exchange Online (verified)!")
                return True

        logger.error("Failed to complete device code authentication")
        return False

    async def _complete_device_login(self, device_code: str) -> bool:
        """Complete device code login using Selenium, handling all Microsoft screens."""

        logger.info("Completing device login with code: %s", device_code)

        try:
            preferred_text = (self.admin_email or "").lower()
            account_picker_seen = False

            # Navigate to device login page
            self.driver.get("https://microsoft.com/devicelogin")
            await self._sleep_after_action(base_delay=2, extra_delay=self._headless_page_settle_seconds)

            # Enter device code
            code_input = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.ID, "otc"))
            )
            code_input.clear()
            code_input.send_keys(device_code)

            # Click Next
            next_btn = self.driver.find_element(By.ID, "idSIButton9")
            next_btn.click()
            await self._sleep_after_action(base_delay=3, extra_delay=self._headless_page_settle_seconds)

            # SCREEN 1: "Pick an account"
            page_source = self.driver.page_source.lower()
            if "pick an account" in page_source or "choose an account" in page_source:
                account_picker_seen = True
                logger.info("Account picker detected, selecting account...")
                account_clicked = False
                clicked_preferred = False

                def _safe_click(element) -> bool:
                    if not element:
                        return False
                    try:
                        if element.is_displayed() and element.is_enabled():
                            element.click()
                            return True
                    except Exception:
                        return False
                    return False

                def _js_click(element) -> bool:
                    """Click using JavaScript as fallback."""
                    if not element:
                        return False
                    try:
                        self.driver.execute_script("arguments[0].click();", element)
                        return True
                    except Exception:
                        return False

                try:
                    # Wait a moment for account tiles to fully load
                    await asyncio.sleep(1)

                    tile_wait_selectors = [
                        "#tilesHolder",
                        "#tilesHolder div[role='button']",
                        "div.tile",
                        "div.table",
                    ]

                    for selector in tile_wait_selectors:
                        try:
                            WebDriverWait(self.driver, 6).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                            )
                            break
                        except Exception:
                            continue
                    
                    # More comprehensive selectors for Microsoft account tiles
                    account_selectors = [
                        "#tilesHolder .tile",
                        "#tilesHolder .tile-container",
                        "#tilesHolder .table",
                        "#tilesHolder div.table[role='button']",
                        "#tilesHolder div.table",
                        "#tilesHolder div[role='button']",
                        "#tilesHolder div[role='listitem']",
                        "#tilesHolder div[role='option']",
                        "div.table[role='button']",
                        "div.table",
                        "div.tile",
                        "div.tile-container",
                        "div[data-test-id]",
                        "div[role='option']",
                        "div[role='listitem']",
                        "div[role='button']",
                        "button[data-test-id]",
                        "div.row",
                        "div.identity-credential",
                        "small.table-text",
                    ]

                    elements = []
                    for selector in account_selectors:
                        try:
                            found = self.driver.find_elements(By.CSS_SELECTOR, selector)
                            elements.extend(found)
                        except Exception:
                            continue

                    logger.info(f"Found {len(elements)} potential account elements")

                    # Try to find and click preferred account first
                    if preferred_text:
                        for element in elements:
                            try:
                                text = (element.text or "").lower()
                                if preferred_text in text:
                                    logger.info(f"Found matching account element with text: {text[:50]}")
                                    account_clicked = _safe_click(element)
                                    if not account_clicked:
                                        account_clicked = _js_click(element)
                                    if account_clicked:
                                        logger.info("✓ Clicked preferred account")
                                        clicked_preferred = True
                                        break
                            except Exception:
                                continue

                    def _click_use_another_account() -> bool:
                        selectors = [
                            "#otherTile",
                            "#tilesHolder div[role='button']",
                            "div.table",
                            "div[role='button']",
                            "button",
                            "a",
                        ]
                        for selector in selectors:
                            try:
                                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                                    text = (element.text or "").lower()
                                    if "use another account" in text:
                                        logger.info("Clicking 'Use another account'...")
                                        if _safe_click(element) or _js_click(element):
                                            return True
                            except Exception:
                                continue

                        try:
                            xpath = "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'use another account')]"
                            for element in self.driver.find_elements(By.XPATH, xpath):
                                if _safe_click(element) or _js_click(element):
                                    return True
                        except Exception:
                            pass
                        return False

                    # If preferred account wasn't found, force "Use another account"
                    if preferred_text and not account_clicked:
                        account_clicked = _click_use_another_account()
                        if account_clicked:
                            logger.info("✓ Selected 'Use another account' for fresh login")

                    # If no preferred account specified, try any account that isn't "use another"
                    if not account_clicked and not preferred_text:
                        for element in elements:
                            try:
                                text = (element.text or "").lower()
                                if "use another account" in text or "back" in text or not text.strip():
                                    continue
                                if "@" in text or "signed in" in text:
                                    logger.info(f"Trying to click account: {text[:50]}")
                                    account_clicked = _safe_click(element)
                                    if not account_clicked:
                                        account_clicked = _js_click(element)
                                    if account_clicked:
                                        logger.info("✓ Clicked account tile")
                                        break
                            except Exception:
                                continue

                    # If still not clicked, try the first visible tile in tilesHolder
                    if not account_clicked and not preferred_text:
                        try:
                            tiles_holder = self.driver.find_element(By.ID, "tilesHolder")
                            tiles = tiles_holder.find_elements(By.CSS_SELECTOR, "div[role='button'], div[role='listitem'], div.table, div.tile")
                            for tile in tiles:
                                text = (tile.text or "").lower()
                                if "use another account" in text or "back" in text or not text.strip():
                                    continue
                                if tile.is_displayed():
                                    logger.info("Trying first visible tile in tilesHolder")
                                    account_clicked = _safe_click(tile)
                                    if not account_clicked:
                                        account_clicked = _js_click(tile)
                                    if account_clicked:
                                        logger.info("✓ Clicked tile via tilesHolder")
                                        break
                        except Exception:
                            pass

                    # If still not clicked, try to locate the account tile from text nodes
                    if not account_clicked and not preferred_text:
                        def _find_clickable_parent(node):
                            current = node
                            for _ in range(6):
                                if not current:
                                    break
                                try:
                                    role = (current.get_attribute("role") or "").lower()
                                    classes = (current.get_attribute("class") or "").lower()
                                    data_test = (current.get_attribute("data-test-id") or "").lower()
                                    if role in {"button", "option", "listitem"}:
                                        return current
                                    if "table" in classes or "tile" in classes or "identity" in classes:
                                        return current
                                    if data_test:
                                        return current
                                except Exception:
                                    pass
                                try:
                                    current = current.find_element(By.XPATH, "..")
                                except Exception:
                                    break
                            return None

                        preferred_xpath = None
                        if preferred_text:
                            preferred_xpath = (
                                "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                                f"'{preferred_text}')][not(contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'use another'))]"
                            )

                        xpath_candidates = [
                            preferred_xpath,
                            "//*[contains(., '@')]",
                            "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'signed in')]",
                        ]

                        for xpath in [x for x in xpath_candidates if x]:
                            try:
                                found_nodes = self.driver.find_elements(By.XPATH, xpath)
                                for node in found_nodes:
                                    try:
                                        text = (node.text or "").lower()
                                        if "use another account" in text or not text.strip():
                                            continue
                                        candidate = _find_clickable_parent(node)
                                        if candidate:
                                            logger.info("Trying clickable parent for account text: %s", text[:50])
                                            account_clicked = _safe_click(candidate)
                                            if not account_clicked:
                                                account_clicked = _js_click(candidate)
                                            if account_clicked:
                                                logger.info("✓ Clicked account tile via text match")
                                                break
                                    except Exception:
                                        continue
                                if account_clicked:
                                    break
                            except Exception:
                                continue

                    # Try finding by XPath containing email or "Signed in"
                    if not account_clicked:
                        xpaths_to_try = [
                            "//div[contains(@class, 'table') and contains(., 'Signed in')]",
                            "//div[contains(@class, 'table') and contains(., '@')]",
                            "//div[@role='button' and contains(., 'Signed in')]",
                            "//div[@data-test-id and contains(., '@')]",
                            "//div[contains(@class, 'tile') and contains(., 'Signed in')]",
                            "//div[contains(@class, 'tile') and contains(., '@')]",
                        ]
                        if preferred_text:
                            xpaths_to_try.insert(0, f"//div[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{preferred_text}')]")
                        
                        for xpath in xpaths_to_try:
                            try:
                                found_elements = self.driver.find_elements(By.XPATH, xpath)
                                for element in found_elements:
                                    text = (element.text or "").lower()
                                    if "use another account" in text:
                                        continue
                                    logger.info(f"Trying XPath element: {text[:50]}")
                                    account_clicked = _safe_click(element)
                                    if not account_clicked:
                                        account_clicked = _js_click(element)
                                    if account_clicked:
                                        logger.info("✓ Clicked via XPath")
                                        break
                                if account_clicked:
                                    break
                            except Exception:
                                continue

                    if account_clicked:
                        await self._sleep_after_action(base_delay=3, extra_delay=self._headless_page_settle_seconds)
                except Exception as exc:
                    logger.warning("Account picker click attempt failed: %s", exc)

                if not account_clicked:
                    try:
                        body = self.driver.find_element(By.TAG_NAME, "body")
                        body.click()
                        await asyncio.sleep(0.3)
                        for _ in range(6):
                            body.send_keys(Keys.TAB)
                            await asyncio.sleep(0.2)
                            active = self.driver.switch_to.active_element
                            active_text = (active.text or "").lower() if active else ""
                            active_role = (active.get_attribute("role") or "").lower() if active else ""
                            if "use another account" in active_text:
                                continue
                            if active_role in {"button", "option", "listitem"} or "@" in active_text or "signed in" in active_text:
                                active.send_keys(Keys.ENTER)
                                account_clicked = True
                                logger.info("✓ Clicked account tile via keyboard fallback")
                                await asyncio.sleep(2)
                                break
                    except Exception:
                        pass

                if not account_clicked:
                    try:
                        os.makedirs("logs", exist_ok=True)
                        screenshot_path = os.path.join(
                            "logs", f"account_picker_missing_{int(time.time())}.png"
                        )
                        self.driver.save_screenshot(screenshot_path)
                        logger.warning(
                            "Account picker not clicked. URL=%s Title=%s Screenshot=%s",
                            self.driver.current_url,
                            self.driver.title,
                            screenshot_path,
                        )
                    except Exception:
                        logger.warning("Account picker not clicked and screenshot failed")

            # SCREEN 2: Email entry (after choosing "Use another account")
            if account_picker_seen and preferred_text and not clicked_preferred:
                try:
                    await asyncio.sleep(1)
                    email_input = None
                    email_selectors = [
                        (By.ID, "i0116"),
                        (By.NAME, "loginfmt"),
                        (By.CSS_SELECTOR, "input[type='email']"),
                    ]
                    for selector in email_selectors:
                        try:
                            email_input = WebDriverWait(self.driver, 6).until(
                                EC.presence_of_element_located(selector)
                            )
                            if email_input and email_input.is_displayed():
                                break
                        except Exception:
                            continue

                    if email_input:
                        logger.info("Entering admin email for fresh login")
                        email_input.clear()
                        email_input.send_keys(self.admin_email)
                        next_btn = self.driver.find_element(By.ID, "idSIButton9")
                        next_btn.click()
                        await self._sleep_after_action(base_delay=3, extra_delay=self._headless_page_settle_seconds)
                except Exception as exc:
                    logger.warning("Email entry step skipped: %s", exc)

            # SCREEN 3: "Are you trying to sign in to Microsoft Exchange..."
            # CRITICAL FIX: Wait for account picker to be gone before checking consent screen
            # The Pick an Account page contains "Microsoft Exchange REST API Based Powershell"
            # which was falsely triggering consent screen detection
            max_consent_wait_attempts = 10
            consent_screen_found = False
            
            for consent_attempt in range(max_consent_wait_attempts):
                await self._sleep_after_action(base_delay=2, extra_delay=self._headless_page_settle_seconds)
                page_source = self.driver.page_source.lower()
                
                # First check if we're still on account picker - if so, wait
                if "pick an account" in page_source or "choose an account" in page_source:
                    logger.warning("Still on account picker (attempt %s/%s), waiting...", 
                                   consent_attempt + 1, max_consent_wait_attempts)
                    
                    # Try clicking account again if we're stuck
                    if consent_attempt >= 2:
                        logger.info("Retrying account selection...")
                        try:
                            # Try to find and click an account tile
                            account_tiles = self.driver.find_elements(By.CSS_SELECTOR, 
                                "#tilesHolder div[role='button'], div.table, div.tile")
                            for tile in account_tiles:
                                try:
                                    text = (tile.text or "").lower()
                                    if "use another account" in text or not text.strip():
                                        continue
                                    if "@" in text or "signed in" in text:
                                        self.driver.execute_script("arguments[0].click();", tile)
                                        logger.info("Retried clicking account tile: %s", text[:30])
                                        break
                                except Exception:
                                    continue
                        except Exception as e:
                            logger.warning("Retry account click failed: %s", e)
                    continue
                
                # Now check for actual consent screen (not account picker)
                # Use more specific check - "are you trying to sign in" is unique to consent screen
                if "are you trying to sign in" in page_source:
                    consent_screen_found = True
                    logger.info("Consent screen detected (attempt %s), clicking Continue...", consent_attempt + 1)
                    try:
                        continue_btn = WebDriverWait(self.driver, 10).until(
                            EC.element_to_be_clickable((By.ID, "idSIButton9"))
                        )
                        continue_btn.click()
                        await self._sleep_after_action(base_delay=3, extra_delay=self._headless_page_settle_seconds)
                    except Exception as e:
                        logger.warning("Continue button: %s", e)
                    break
                
                # If we're past account picker but not on consent screen, we may have succeeded
                if consent_attempt > 0:
                    logger.info("Not on account picker or consent screen, proceeding...")
                    break
            
            if not consent_screen_found:
                logger.info("Consent screen not encountered, checking for password/MFA screens...")

            # SCREEN 4: Password entry (if session expired)
            page_source = self.driver.page_source.lower()
            if "enter password" in page_source or "passwd" in self.driver.page_source:
                logger.info("Password entry detected...")
                try:
                    pwd_input = WebDriverWait(self.driver, 5).until(
                        EC.presence_of_element_located((By.NAME, "passwd"))
                    )
                    pwd_input.clear()
                    pwd_input.send_keys(self.admin_password)
                    submit_btn = self.driver.find_element(By.ID, "idSIButton9")
                    submit_btn.click()
                    await self._sleep_after_action(base_delay=3, extra_delay=self._headless_page_settle_seconds)
                except Exception:
                    pass

            # SCREEN 5: MFA/TOTP
            await self._handle_mfa()

            # SCREEN 6: "Stay signed in?"
            await self._sleep_after_action(base_delay=2)
            try:
                page_source = self.driver.page_source.lower()
                if "stay signed in" in page_source:
                    yes_btn = self.driver.find_element(By.ID, "idSIButton9")
                    yes_btn.click()
                    await self._sleep_after_action(base_delay=2)
            except Exception:
                pass

            # Check for success
            await self._sleep_after_action(base_delay=3, extra_delay=self._headless_page_settle_seconds)
            page_source = self.driver.page_source.lower()
            if (
                "you have signed in" in page_source
                or "you're signed in" in page_source
                or "close this window" in page_source
            ):
                logger.info("✓ Device code authentication successful!")
                return True

            return True

        except Exception as exc:
            logger.error("Device login failed: %s", exc)
            return False

    async def _handle_mfa(self):
        """Handle MFA challenge using TOTP."""

        await asyncio.sleep(2)
        page_source = self.driver.page_source.lower()

        # Avoid false positives on various screens
        if "allow access" in page_source or "enter code to allow access" in page_source:
            return
        if "pick an account" in page_source or "choose an account" in page_source:
            logger.debug("Skipping MFA - still on account picker")
            return
        if "are you trying to sign in" in page_source:
            logger.debug("Skipping MFA - on consent screen")
            return

        # Check if TOTP input is present (more specific check)
        if (
            ("authenticator" in page_source and "code" in page_source)
            or "verification code" in page_source
            or "verify your identity" in page_source
            or "enter the code" in page_source
        ):
            if self.totp_secret:
                import pyotp

                totp = pyotp.TOTP(self.totp_secret)
                code = totp.now()

                logger.info("Entering TOTP code for MFA")

                # Find and fill TOTP input
                totp_input = None
                selectors = [
                    (By.ID, "idTxtBx_SAOTCC_OTC"),
                    (By.ID, "idTxtBx_TOTP_OTC"),
                    (By.NAME, "otc"),
                    (By.NAME, "ProofConfirmation"),
                    (By.CSS_SELECTOR, "input[type='tel']"),
                    (By.CSS_SELECTOR, "input[inputmode='numeric']"),
                    (By.CSS_SELECTOR, "input[autocomplete='one-time-code']"),
                    (By.CSS_SELECTOR, "input[aria-label*='code']"),
                    (By.XPATH, "//input[contains(@aria-label, 'code') or contains(@placeholder, 'code')]")
                ]

                def _find_totp_input() -> Optional[webdriver.remote.webelement.WebElement]:
                    for selector in selectors:
                        try:
                            element = WebDriverWait(self.driver, 6).until(
                                EC.presence_of_element_located(selector)
                            )
                            if element and element.is_displayed():
                                return element
                        except Exception:
                            continue
                    return None

                totp_input = _find_totp_input()

                if not totp_input:
                    try:
                        self.driver.switch_to.default_content()
                        frames = self.driver.find_elements(By.TAG_NAME, "iframe")
                        for frame in frames:
                            try:
                                self.driver.switch_to.frame(frame)
                                totp_input = _find_totp_input()
                                if totp_input:
                                    break
                            except Exception:
                                continue
                            finally:
                                self.driver.switch_to.default_content()
                    except Exception:
                        self.driver.switch_to.default_content()

                if totp_input:
                    totp_input.clear()
                    totp_input.send_keys(code)

                    try:
                        verify_btn = self.driver.find_element(By.ID, "idSubmit_SAOTCC_Continue")
                        verify_btn.click()
                    except Exception:
                        try:
                            verify_btn = self.driver.find_element(By.ID, "idSIButton9")
                            verify_btn.click()
                        except Exception:
                            totp_input.send_keys("\n")
                    await asyncio.sleep(3)
                else:
                    try:
                        self.driver.switch_to.default_content()
                        os.makedirs("logs", exist_ok=True)
                        screenshot_path = os.path.join("logs", f"mfa_totp_missing_{int(time.time())}.png")
                        self.driver.save_screenshot(screenshot_path)
                        logger.warning(
                            "Could not find TOTP input field. URL=%s Title=%s Screenshot=%s",
                            self.driver.current_url,
                            self.driver.title,
                            screenshot_path,
                        )
                    except Exception:
                        logger.warning("Could not find TOTP input field after waiting")
                    finally:
                        self.driver.switch_to.default_content()

        # Handle "Stay signed in?" prompt
        try:
            stay_signed_in = self.driver.find_element(By.ID, "idBtn_Back")
            stay_signed_in.click()  # Click "No"
        except Exception:
            try:
                yes_btn = self.driver.find_element(By.ID, "idSIButton9")
                yes_btn.click()  # Click "Yes"
            except Exception:
                pass

    async def _run_command(self, command: str) -> Optional[str]:
        """Run a PowerShell command and return output."""

        if not self.ps_process:
            logger.error("PowerShell process not running")
            return None

        # Add output marker
        full_cmd = f"{command}\nWrite-Output \"CMD_COMPLETE\"\n"

        self.ps_process.stdin.write(full_cmd)
        self.ps_process.stdin.flush()

        # Collect output
        output_lines = []
        timeout = 120
        start = time.time()

        while time.time() - start < timeout:
            line = self.ps_process.stdout.readline()
            if not line:
                await asyncio.sleep(0.1)
                continue

            line = line.strip()

            if "CMD_COMPLETE" in line:
                break

            output_lines.append(line)

        return "\n".join(output_lines)

    async def _run_command_interactive(self, script: str, timeout: int = 600) -> str:
        """Run a PowerShell script that requires interactive device code auth."""

        if not self.ps_process:
            raise Exception("PowerShell process not running")

        self.ps_process.stdin.write(script)
        self.ps_process.stdin.flush()

        output_lines = []
        device_code = None
        start = time.time()

        while time.time() - start < timeout:
            line = self.ps_process.stdout.readline()
            if not line:
                await asyncio.sleep(0.1)
                continue

            line = line.strip()
            output_lines.append(line)
            logger.debug("PS Interactive Output: %s", line)

            if not device_code:
                code_match = re.search(r"enter the code\s+([A-Z0-9]{8,})", line, re.IGNORECASE)
                if code_match:
                    device_code = code_match.group(1)
                    logger.info("Got device code: %s", device_code)
                    success = await self._complete_device_login(device_code)
                    logger.info("Device login completed: %s", success)
                    if not success:
                        break

                code_match2 = re.search(r"code[:\s]+([A-Z0-9]{8,})", line, re.IGNORECASE)
                if not device_code and code_match2:
                    device_code = code_match2.group(1)
                    logger.info("Got device code (alt): %s", device_code)
                    success = await self._complete_device_login(device_code)
                    logger.info("Device login completed: %s", success)
                    if not success:
                        break

            if "MG_COMPLETE" in line:
                break

        return "\n".join(output_lines)

    async def create_shared_mailboxes(
        self,
        mailboxes: List[Dict[str, str]],
        delegate_to: str,
        start_index: int = 1,
    ) -> Dict[str, Any]:
        """
        Create shared mailboxes with numbered names, fix display names, add delegation.

        Args:
            mailboxes: List of {"email": "...", "display_name": "...", "password": "..."}
            delegate_to: Licensed user email (me1@domain)
            start_index: Starting index for numbered display names (e.g., 51 for second domain)
        """

        if not self.connected:
            raise Exception("Not connected to Exchange Online")

        results = {
            "created": [],
            "failed": [],
            "delegated": [],
            "upns_fixed": [],
            "passwords_set": [],
        }

        base_display_name = mailboxes[0].get("display_name", "User") if mailboxes else "User"

        # STEP 1: Create mailboxes with NUMBERED display names
        # RELIABILITY FIX: Check if mailbox exists FIRST via Get-Mailbox before attempting New-Mailbox
        # This prevents "name already being used" errors on retry
        logger.info("Creating %s shared mailboxes (start_index=%s)...", len(mailboxes), start_index)
        for i, mb in enumerate(mailboxes, start_index):
            email = mb["email"]
            numbered_name = f"{base_display_name} {i}"
            escaped_email = self._ps_escape(email)
            escaped_name = self._ps_escape(numbered_name)

            create_cmd = f'''
$email = "{escaped_email}"
$numberedName = "{escaped_name}"

# First check if mailbox already exists by email address
$existing = $null
try {{
    $existing = Get-Mailbox -Identity $email -ErrorAction SilentlyContinue
}} catch {{
    # Ignore - mailbox doesn't exist
}}

if ($existing) {{
    Write-Output "EXISTS:$email"
}} else {{
    try {{
        New-Mailbox -Shared -Name $numberedName -DisplayName $numberedName -PrimarySmtpAddress $email -ErrorAction Stop | Out-Null
        Write-Output "CREATED:$email"
    }} catch {{
        $errMsg = $_.Exception.Message
        if ($errMsg -like "*already being used*" -or $errMsg -like "*already exists*" -or $errMsg -like "*is already used*") {{
            # Name collision but email didn't exist - mailbox exists under different identity
            Write-Output "EXISTS:$email"
        }} else {{
            Write-Output "FAILED:$email||$errMsg"
        }}
    }}
}}
'''
            output = await self._run_command(create_cmd)

            if output and (f"CREATED:{email}" in output or f"EXISTS:{email}" in output):
                results["created"].append(email)
                if f"EXISTS:{email}" in output:
                    logger.info("  ✓ Already exists: %s", email)
                else:
                    logger.info("  ✓ Created: %s", email)
            else:
                error_msg = "Unknown error"
                if output and "FAILED:" in output:
                    # Parse error after the || separator
                    parts = output.split("FAILED:")[-1]
                    if "||" in parts:
                        error_msg = parts.split("||", 1)[1].strip()
                    else:
                        error_msg = parts.strip()
                results["failed"].append({"email": email, "error": error_msg})
                logger.error("  ✗ Failed: %s - %s", email, error_msg)

            await asyncio.sleep(0.3)

        # STEP 2: Fix display names (remove numbers, all same name)
        logger.info("Fixing display names to '%s'...", base_display_name)
        # Wait for newly created mailboxes to propagate in Exchange
        if results["created"]:
            logger.info("Waiting 10s for mailbox propagation before fixing display names...")
            await asyncio.sleep(10)
        else:
            await asyncio.sleep(2)

        for mb in mailboxes:
            email = mb["email"]
            fix_cmd = f'''
try {{
    Set-Mailbox -Identity "{email}" -DisplayName "{base_display_name}" -ErrorAction Stop
    Write-Output "FIXED:{email}"
}} catch {{
    Write-Output "FIXFAILED:{email}:$($_.Exception.Message)"
}}
'''
            output = await self._run_command(fix_cmd)
            if output and f"FIXED:{email}" in output:
                logger.info("  ✓ Display name fixed: %s", email)

            await asyncio.sleep(0.2)

        # STEP 3: Add delegation (FullAccess + SendAs)
        logger.info("Adding delegation to %s...", delegate_to)
        await asyncio.sleep(2)

        for mb in mailboxes:
            email = mb["email"]
            delegate_cmd = f'''
try {{
    Add-MailboxPermission -Identity "{email}" -User "{delegate_to}" -AccessRights FullAccess -AutoMapping $true -ErrorAction SilentlyContinue | Out-Null
    Add-RecipientPermission -Identity "{email}" -Trustee "{delegate_to}" -AccessRights SendAs -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Write-Output "DELEGATED:{email}"
}} catch {{
    Write-Output "DELEGATEFAILED:{email}:$($_.Exception.Message)"
}}
'''
            output = await self._run_command(delegate_cmd)

            if output and f"DELEGATED:{email}" in output:
                results["delegated"].append(email)
                logger.info("  ✓ Delegated: %s -> %s", email, delegate_to)

            await asyncio.sleep(0.2)

        # STEP 4: Fix UPNs to match email addresses (critical for Admin UI operations)
        logger.info("Fixing UPNs to match email addresses...")
        await asyncio.sleep(3)

        for mb in mailboxes:
            email = mb["email"]
            upn_cmd = f'''
try {{
    Set-Mailbox -Identity "{email}" -MicrosoftOnlineServicesID "{email}" -ErrorAction Stop
    Write-Output "UPNFIXED:{email}"
}} catch {{
    Write-Output "UPNFAILED:{email}:$($_.Exception.Message)"
}}
'''
            output = await self._run_command(upn_cmd)

            if output and f"UPNFIXED:{email}" in output:
                results["upns_fixed"].append(email)
                logger.info("  ✓ UPN fixed: %s", email)
            else:
                logger.warning("  ⚠ UPN fix issue: %s", email)

            await asyncio.sleep(0.2)

        logger.info(
            "PowerShell complete: %s created, %s delegated, %s UPNs fixed",
            len(results["created"]),
            len(results["delegated"]),
            len(results["upns_fixed"]),
        )
        return results

    async def set_mailbox_passwords(
        self,
        mailboxes: List[Dict[str, str]],
        admin_email: str,
        admin_password: str,
    ) -> Dict[str, Any]:
        """
        Set passwords, enable accounts, and fix UPNs for mailboxes using Microsoft Graph.

        Args:
            mailboxes: List of {"email": "...", "password": "..."}
            admin_email: Admin UPN for Graph connection
            admin_password: Admin password for Graph connection

        Returns:
            {"results": [...], "updated": [...], "failed": [...]}
        """

        if not self.connected:
            raise Exception("Not connected to Exchange Online")

        escaped_admin_email = self._ps_escape(admin_email)
        escaped_admin_password = self._ps_escape(admin_password)

        disconnect_cmd = "Disconnect-MgGraph -ErrorAction SilentlyContinue"
        await self._run_command(disconnect_cmd)
        await asyncio.sleep(1)

        connect_graph_cmd = f'''
try {{
    Import-Module Microsoft.Graph.Users -RequiredVersion 2.41.0 -ErrorAction Stop
    $securePassword = ConvertTo-SecureString "{escaped_admin_password}" -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential("{escaped_admin_email}", $securePassword)
    Connect-MgGraph -Credential $credential -NoWelcome -ErrorAction Stop
    Write-Output "MG_CONNECTED"
}} catch {{
    Write-Output "MG_CONNECT_FAILED:$($_.Exception.Message)"
}}
'''

        connect_output = await self._run_command(connect_graph_cmd)
        if not connect_output or "MG_CONNECTED" not in connect_output:
            raise Exception(f"Microsoft Graph connection failed: {connect_output}")

        results = []
        updated = []
        failed = []

        for mb in mailboxes:
            email = mb["email"]
            password = mb.get("password", "")
            escaped_email = self._ps_escape(email)
            escaped_password = self._ps_escape(password)

            cmd = f'''
$mailboxEmail = "{escaped_email}"
$passwordValue = "{escaped_password}"
$errors = @()
$upnFixed = $false
$passwordSet = $false
$accountEnabled = $false

try {{
    $mailbox = Get-Mailbox -Identity $mailboxEmail -ErrorAction Stop
    $userPrincipalName = $mailbox.UserPrincipalName
    $user = Get-MgUser -UserId $userPrincipalName -ErrorAction Stop

    if ($user.UserPrincipalName -ne $mailboxEmail) {{
        try {{
            Update-MgUser -UserId $user.Id -UserPrincipalName $mailboxEmail -ErrorAction Stop
            $upnFixed = $true
        }} catch {{
            $errors += "Failed to update UPN: $($_.Exception.Message)"
        }}
    }} else {{
        $upnFixed = $true
    }}

    try {{
        $passwordProfile = @{{
            Password = $passwordValue
            ForceChangePasswordNextSignIn = $false
        }}
        Update-MgUser -UserId $user.Id -PasswordProfile $passwordProfile -ErrorAction Stop
        $passwordSet = $true
    }} catch {{
        $errors += "Failed to set password: $($_.Exception.Message)"
    }}

    try {{
        Update-MgUser -UserId $user.Id -AccountEnabled:$true -ErrorAction Stop
        $accountEnabled = $true
    }} catch {{
        $errors += "Failed to enable account: $($_.Exception.Message)"
    }}
}} catch {{
    $errors += "Mailbox lookup failed: $($_.Exception.Message)"
}}

$result = @{{
    email = $mailboxEmail
    upn_fixed = $upnFixed
    password_set = $passwordSet
    account_enabled = $accountEnabled
    errors = $errors
}}

if ($errors.Count -eq 0) {{
    $result.success = $true
}} else {{
    $result.success = $false
}}

$result | ConvertTo-Json -Compress
'''

            output = await self._run_command(cmd)
            parsed = None
            if output:
                for line in output.splitlines():
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            parsed = json.loads(line)
                        except json.JSONDecodeError:
                            continue

            if parsed:
                results.append(parsed)
                if parsed.get("success"):
                    updated.append(parsed["email"])
                    logger.info("  [OK] Password set: %s", parsed["email"])
                else:
                    failed.append({"email": parsed.get("email", email), "error": "; ".join(parsed.get("errors", []))})
                    logger.error(
                        "  [FAIL] Password set: %s - %s",
                        parsed.get("email", email),
                        "; ".join(parsed.get("errors", [])),
                    )
            else:
                failed.append({"email": email, "error": output or "No output from PowerShell"})
                logger.error(
                    "  [FAIL] Password set: %s - %s",
                    email,
                    output or "No output from PowerShell",
                )

            await asyncio.sleep(0.5)

        await self._run_command("Disconnect-MgGraph -ErrorAction SilentlyContinue")

        logger.info(
            "Graph PowerShell password summary: updated=%s failed=%s",
            len(updated),
            len(failed),
        )
        if failed:
            preview = "; ".join(
                f"{item.get('email')}: {item.get('error')}" for item in failed[:5]
            )
            logger.warning("Graph PowerShell password failures (first 5): %s", preview)

        return {
            "results": results,
            "updated": updated,
            "failed": failed,
        }

    async def set_passwords_via_graph_powershell(
        self,
        mailboxes: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """
        Set passwords, enable accounts, and fix UPNs using Microsoft.Graph PowerShell
        with device code authentication.

        Args:
            mailboxes: List of {"email": "...", "password": "..."}

        Returns:
            {"results": [...], "updated": [...], "failed": [...]}
        """

        if not self.connected:
            raise Exception("Not connected to Exchange Online")

        if not self.ps_process:
            raise Exception("PowerShell process not running")

        password_commands = []
        for mb in mailboxes:
            email = self._ps_escape(mb["email"])
            password = self._ps_escape(mb.get("password", ""))
            password_commands.append(
                f'''
try {{
    Update-MgUser -UserId "{email}" -PasswordProfile @{{
        Password = "{password}"
        ForceChangePasswordNextSignIn = $false
    }} -AccountEnabled $true -ErrorAction Stop
    Write-Output "PWDSET:{email}"
}} catch {{
    Write-Output "PWDFAILED:{email}:$($_.Exception.Message)"
}}
Start-Sleep -Milliseconds 300
'''
            )

        full_script = f'''
Import-Module Microsoft.Graph.Users -RequiredVersion 2.41.0 -ErrorAction Stop
Write-Output "MG_MODULE_LOADED"

Disconnect-MgGraph -ErrorAction SilentlyContinue

Connect-MgGraph -Scopes "User.ReadWrite.All" -UseDeviceCode -NoWelcome

$ctx = Get-MgContext
if ($ctx) {{
    Write-Output "MG_CONNECTED"
    Write-Output "MG_USER:$($ctx.Account)"
}} else {{
    Write-Output "MG_CONNECT_FAILED"
    exit 1
}}

{"".join(password_commands)}

Disconnect-MgGraph
Write-Output "MG_COMPLETE"
'''

        output = await self._run_command_interactive(full_script)

        updated = []
        failed = []

        for line in output.splitlines():
            if line.startswith("PWDSET:"):
                email = line.replace("PWDSET:", "").strip()
                updated.append(email)
                logger.info("  [OK] Password set: %s", email)
            elif line.startswith("PWDFAILED:"):
                payload = line.replace("PWDFAILED:", "")
                parts = payload.split(":", 1)
                email = parts[0].strip()
                error = parts[1].strip() if len(parts) > 1 else "Unknown"
                failed.append({"email": email, "error": error})
                logger.error("  [FAIL] Password: %s - %s", email, error)

        logger.info(
            "Graph PowerShell password summary: updated=%s failed=%s",
            len(updated),
            len(failed),
        )
        if failed:
            preview = "; ".join(
                f"{item.get('email')}: {item.get('error')}" for item in failed[:5]
            )
            logger.warning("Graph PowerShell password failures (first 5): %s", preview)

        return {
            "results": [],
            "updated": updated,
            "failed": failed,
        }

    async def add_mailbox_delegation(
        self,
        mailboxes: List[Dict[str, str]],
        delegate_to: str,
    ) -> Dict[str, Any]:
        """
        Add mailbox delegation for a list of mailboxes.

        Args:
            mailboxes: List of {"email": "..."}
            delegate_to: User UPN to grant permissions to

        Returns:
            {"delegated": [...], "failed": [...]}
        """

        if not self.connected:
            raise Exception("Not connected to Exchange Online")

        results = {"delegated": [], "failed": []}
        escaped_delegate = self._ps_escape(delegate_to)

        for mb in mailboxes:
            email = mb["email"]
            escaped_email = self._ps_escape(email)

            cmd = f'''
$mailboxEmail = "{escaped_email}"
$delegateUser = "{escaped_delegate}"
$errors = @()

try {{
    Add-MailboxPermission -Identity $mailboxEmail -User $delegateUser -AccessRights FullAccess -InheritanceType All -AutoMapping $true -ErrorAction Stop | Out-Null
}} catch {{
    if ($_.Exception.Message -notlike "*already*") {{
        $errors += "Failed FullAccess: $($_.Exception.Message)"
    }}
}}

try {{
    Add-RecipientPermission -Identity $mailboxEmail -Trustee $delegateUser -AccessRights SendAs -Confirm:$false -ErrorAction Stop | Out-Null
}} catch {{
    if ($_.Exception.Message -notlike "*already*") {{
        $errors += "Failed SendAs: $($_.Exception.Message)"
    }}
}}

try {{
    Set-Mailbox -Identity $mailboxEmail -GrantSendOnBehalfTo @{{Add=$delegateUser}} -ErrorAction Stop | Out-Null
}} catch {{
    if ($_.Exception.Message -notlike "*already*") {{
        $errors += "Failed SendOnBehalf: $($_.Exception.Message)"
    }}
}}

if ($errors.Count -eq 0) {{
    Write-Output "DELEGATED:$mailboxEmail"
}} else {{
    Write-Output "DELEGATE_FAILED:$mailboxEmail:$($errors -join '; ')"
}}
'''

            output = await self._run_command(cmd)
            if output and f"DELEGATED:{email}" in output:
                results["delegated"].append(email)
            else:
                error_detail = "Delegation failed"
                if output and "DELEGATE_FAILED" in output:
                    error_detail = output.split("DELEGATE_FAILED:")[-1].strip()
                results["failed"].append({"email": email, "error": error_detail})

            await asyncio.sleep(0.5)

        return results

    async def fix_display_names(self, mailboxes: List[Dict[str, str]]) -> Dict[str, Any]:
        """Fix display names for mailboxes."""

        if not self.connected:
            raise Exception("Not connected to Exchange Online")

        results = {"updated": [], "failed": []}

        for mb in mailboxes:
            email = mb["email"]
            display_name = mb["display_name"]

            cmd = f'''
try {{
    Set-Mailbox -Identity "{email}" -DisplayName "{display_name}" -ErrorAction Stop
    Write-Output "UPDATED:{email}"
}} catch {{
    Write-Output "FAILED:{email}:$($_.Exception.Message)"
}}
'''
            output = await self._run_command(cmd)

            if output and f"UPDATED:{email}" in output:
                results["updated"].append(email)
                logger.info("  ✓ Updated display name: %s", email)
            else:
                error = (
                    output.split("FAILED:")[-1]
                    if output and "FAILED:" in output
                    else "Unknown error"
                )
                results["failed"].append({"email": email, "error": error})

        return results

    async def disconnect(self):
        """Disconnect from Exchange Online and cleanup PowerShell processes."""
        import subprocess
        import platform

        if self.ps_process:
            try:
                # Try graceful disconnect
                self.ps_process.stdin.write("Disconnect-ExchangeOnline -Confirm:$false\n")
                self.ps_process.stdin.write("Disconnect-MgGraph -ErrorAction SilentlyContinue\n")
                self.ps_process.stdin.write("exit\n")
                self.ps_process.stdin.flush()
                self.ps_process.wait(timeout=10)
            except Exception as e:
                logger.warning("Graceful PowerShell disconnect failed: %s", e)
                try:
                    self.ps_process.kill()
                except Exception:
                    pass

            self.ps_process = None
            self.connected = False
            logger.info("Disconnected from Exchange Online")

        # Force kill orphaned PowerShell processes on Windows
        if platform.system() == "Windows":
            try:
                # Kill any orphaned pwsh processes that might be stuck
                # Only kill processes related to our Exchange module operations
                result = subprocess.run(
                    ["wmic", "process", "where", "name='pwsh.exe'", "get", "commandline,processid"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                for line in result.stdout.split('\n'):
                    if 'ExchangeOnlineManagement' in line or 'Microsoft.Graph' in line:
                        parts = line.strip().split()
                        if parts:
                            try:
                                pid = parts[-1]
                                subprocess.run(
                                    ["taskkill", "/F", "/PID", pid],
                                    capture_output=True,
                                    timeout=5
                                )
                                logger.debug("Killed orphaned pwsh process: %s", pid)
                            except Exception:
                                pass
            except Exception as e:
                logger.debug("PowerShell cleanup: %s", e)
        
        # Small delay to ensure cleanup completes
        await asyncio.sleep(2)
