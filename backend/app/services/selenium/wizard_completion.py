"""
Domain Wizard Completion - Handles the "Connect your domain" wizard flow.

After TXT verification succeeds in M365, this module handles:
1. Page 1: Select "Add your own DNS records" option
2. Page 2: Expand Advanced options, check DKIM, extract ALL DNS records
3. Page 3: Handle DNS validation errors and retry
4. Page 4: Click Done on completion

This is called from AdminPortalAutomation.setup_domain_complete() after verification.
"""

import re
import time
import logging
import asyncio
from typing import Optional, Dict, Any
from dataclasses import dataclass

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

logger = logging.getLogger(__name__)


@dataclass
class WizardCompletionResult:
    """Result of completing the domain wizard after verification."""
    success: bool
    mx_value: Optional[str] = None
    spf_value: Optional[str] = None
    autodiscover_value: Optional[str] = None
    dkim_selector1: Optional[str] = None
    dkim_selector2: Optional[str] = None
    dmarc_added: bool = False
    error: Optional[str] = None
    error_step: Optional[str] = None


def _is_domain_setup_complete_text(page_text: str) -> bool:
    text = (page_text or "").lower()
    if not text:
        return False
    if any(marker in text for marker in ("error", "failed", "couldn't verify", "doesn't match", "try again")):
        return False
    return "domain setup is complete" in text or "setup is complete" in text or "is all set up" in text


class DomainWizardCompleter:
    """
    Handles the domain wizard completion flow after TXT verification.
    
    The wizard has 4 pages:
    1. "How do you want to connect your domain?" - Select "Add your own DNS records"
    2. "Add DNS records" - Check DKIM, extract all record values
    3. DNS Validation Results - Handle errors, retry if needed
    4. "Domain setup is complete" - Click Done
    """
    
    def __init__(self, driver, domain_name: str, screenshot_func=None, safe_click_func=None):
        """
        Args:
            driver: Selenium WebDriver instance
            domain_name: Domain being configured
            screenshot_func: Optional function to take screenshots
            safe_click_func: Optional safe click function
        """
        self.driver = driver
        self.domain_name = domain_name
        self._screenshot = screenshot_func or (lambda name, domain: None)
        self._safe_click = safe_click_func or self._default_safe_click
    
    def _default_safe_click(self, element, description: str = "element"):
        """Simple click with scroll into view."""
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});",
                element
            )
            time.sleep(0.3)
            element.click()
            logger.info(f"[{self.domain_name}] Clicked {description}")
        except Exception as e:
            logger.warning(f"[{self.domain_name}] Click failed for {description}, trying JS: {e}")
            self.driver.execute_script("arguments[0].click();", element)
    
    def _find_element(self, selectors: list, timeout: int = 10):
        """Try multiple selectors, return first match."""
        wait = WebDriverWait(self.driver, timeout)
        for by, value in selectors:
            try:
                return wait.until(EC.presence_of_element_located((by, value)))
            except TimeoutException:
                continue
        return None
    
    def _find_clickable(self, selectors: list, timeout: int = 10):
        """Find clickable element."""
        wait = WebDriverWait(self.driver, timeout)
        for by, value in selectors:
            try:
                return wait.until(EC.element_to_be_clickable((by, value)))
            except TimeoutException:
                continue
        return None
    
    def _click_if_exists(self, selectors: list, timeout: int = 5) -> bool:
        """Click element if it exists."""
        elem = self._find_element(selectors, timeout)
        if elem:
            try:
                self._safe_click(elem, "element")
                return True
            except:
                pass
        return False
    
    async def complete_wizard(self, cloudflare_zone_id: str, cloudflare_service) -> WizardCompletionResult:
        """
        Complete the domain wizard after TXT verification succeeds.
        
        Args:
            cloudflare_zone_id: Cloudflare zone ID for adding DNS records
            cloudflare_service: CloudflareService instance
        
        Returns:
            WizardCompletionResult with DNS values and success status
        """
        result = WizardCompletionResult(success=False)
        
        try:
            # ========== PAGE 1: Select "Add your own DNS records" ==========
            logger.info(f"[{self.domain_name}] Wizard Page 1: Selecting 'Add your own DNS records'...")
            self._screenshot("wizard_page1_start", self.domain_name)
            
            page1_success = self._handle_page1_dns_option()
            if not page1_success:
                # May already be on page 2, continue
                logger.warning(f"[{self.domain_name}] Page 1 handling uncertain, continuing...")
            
            time.sleep(3)
            self._screenshot("wizard_after_page1", self.domain_name)
            
            # ========== PAGE 2: Expand DKIM, Extract DNS Records ==========
            logger.info(f"[{self.domain_name}] Wizard Page 2: Expanding Advanced options for DKIM...")
            
            dns_records = self._handle_page2_extract_records()
            result.mx_value = dns_records.get("mx_value")
            result.spf_value = dns_records.get("spf_value")
            result.autodiscover_value = dns_records.get("autodiscover_value")
            result.dkim_selector1 = dns_records.get("dkim_selector1")
            result.dkim_selector2 = dns_records.get("dkim_selector2")
            
            logger.info(f"[{self.domain_name}] Extracted: MX={result.mx_value}, DKIM1={result.dkim_selector1}")
            self._screenshot("wizard_dns_records_extracted", self.domain_name)

            missing_values = [
                name
                for name, value in {
                    "MX": result.mx_value,
                    "SPF": result.spf_value,
                    "autodiscover": result.autodiscover_value,
                    "DKIM selector1": result.dkim_selector1,
                    "DKIM selector2": result.dkim_selector2,
                }.items()
                if not value
            ]
            if missing_values:
                result.error = f"Wizard DNS page missing required values: {', '.join(missing_values)}"
                result.error_step = "dns_extraction"
                logger.error(f"[{self.domain_name}] {result.error}")
                return result
            
            # ========== Add DNS to Cloudflare ==========
            logger.info(f"[{self.domain_name}] Adding DNS records to Cloudflare...")
            
            dns_write_results = await self._add_dns_to_cloudflare(
                cloudflare_zone_id=cloudflare_zone_id,
                cloudflare_service=cloudflare_service,
                mx_value=result.mx_value,
                spf_value=result.spf_value,
                autodiscover_value=result.autodiscover_value,
                dkim_selector1=result.dkim_selector1,
                dkim_selector2=result.dkim_selector2,
            )
            result.dmarc_added = dns_write_results.get("dmarc", False)
            
            # ========== Wait for DNS propagation ==========
            logger.info(f"[{self.domain_name}] Waiting 30s for DNS propagation...")
            await asyncio.sleep(30)
            
            # ========== PAGE 3: Click Continue, Handle Validation ==========
            logger.info(f"[{self.domain_name}] Wizard Page 3: Clicking Continue for DNS validation...")
            self._screenshot("wizard_before_validation", self.domain_name)
            
            # Click Continue
            self._click_if_exists([
                (By.XPATH, "//button[contains(., 'Continue')]"),
                (By.XPATH, "//button[contains(., 'Next')]"),
            ], timeout=5)
            
            time.sleep(10)
            self._screenshot("wizard_validation_result", self.domain_name)
            
            # Check for errors and retry if needed
            validation_success = await self._handle_page3_validation(
                cloudflare_zone_id, cloudflare_service,
                result.spf_value, result.dkim_selector1, result.dkim_selector2
            )
            
            if not validation_success:
                result.error = "DNS validation failed after retries"
                result.error_step = "dns_validation"
                return result
            
            # ========== PAGE 4: Click Done ==========
            logger.info(f"[{self.domain_name}] Wizard Page 4: Looking for completion...")
            self._screenshot("wizard_looking_for_done", self.domain_name)
            
            page_text = self.driver.page_source.lower()
            if _is_domain_setup_complete_text(page_text):
                logger.info(f"[{self.domain_name}] ✓ Domain setup complete! Clicking Done...")
                
                done_btn = self._find_clickable([
                    (By.XPATH, "//button[contains(., 'Done')]"),
                    (By.XPATH, "//button[contains(., 'Finish')]"),
                    (By.XPATH, "//button[contains(., 'Close')]"),
                ], timeout=10)
                
                if done_btn:
                    self._safe_click(done_btn, "Done button")
                    time.sleep(2)
                
                self._screenshot("wizard_complete", self.domain_name)
                logger.info(f"[{self.domain_name}] ✓✓✓ DOMAIN WIZARD COMPLETE ✓✓✓")
                result.success = True
            else:
                # Try clicking through remaining buttons
                for _ in range(3):
                    clicked = self._click_if_exists([
                        (By.XPATH, "//button[contains(., 'Continue')]"),
                        (By.XPATH, "//button[contains(., 'Done')]"),
                        (By.XPATH, "//button[contains(., 'Finish')]"),
                    ], timeout=2)
                    if clicked:
                        time.sleep(2)
                    else:
                        break
                
                # Check again
                if _is_domain_setup_complete_text(self.driver.page_source):
                    result.success = True
                    logger.info(f"[{self.domain_name}] ✓ Wizard completed after clicking through")
                else:
                    result.error = "Microsoft domain setup wizard did not reach completion page"
                    result.error_step = "wizard_completion"
                    logger.error(f"[{self.domain_name}] {result.error}")
            
            return result
            
        except Exception as e:
            logger.exception(f"[{self.domain_name}] Error in wizard completion: {e}")
            result.error = str(e)
            result.error_step = "exception"
            return result
    
    def _handle_page1_dns_option(self) -> bool:
        """
        Handle Page 1: Select "Add your own DNS records" option.
        
        Returns True if successfully clicked the option and Continue.
        """
        # Look for "Add your own DNS records" radio/option
        add_own_dns_selectors = [
            (By.XPATH, "//input[@type='radio'][following-sibling::*[contains(text(), 'Add your own DNS')]]"),
            (By.XPATH, "//*[contains(text(), 'Add your own DNS records')]/preceding-sibling::input[@type='radio']"),
            (By.XPATH, "//label[contains(., 'Add your own DNS records')]"),
            (By.XPATH, "//*[contains(text(), 'Add your own DNS records')]"),
            (By.XPATH, "//div[contains(@class, 'radio')]//*[contains(text(), 'your own')]"),
            (By.XPATH, "//span[contains(text(), 'Add your own')]"),
        ]
        
        option_clicked = False
        for by, value in add_own_dns_selectors:
            try:
                elements = self.driver.find_elements(by, value)
                for elem in elements:
                    if elem.is_displayed():
                        logger.info(f"[{self.domain_name}] Found 'Add your own DNS records' option")
                        self._safe_click(elem, "'Add your own DNS records'")
                        option_clicked = True
                        time.sleep(1)
                        break
                if option_clicked:
                    break
            except:
                continue
        
        if not option_clicked:
            logger.warning(f"[{self.domain_name}] Could not find 'Add your own DNS records' option")
            # Check if we're already past this page
            page_text = self.driver.page_source.lower()
            if "exchange" in page_text and "mx" in page_text:
                logger.info(f"[{self.domain_name}] Appears to already be on DNS records page")
                return True
        
        # Click Continue
        time.sleep(1)
        self._click_if_exists([
            (By.XPATH, "//button[contains(., 'Continue')]"),
            (By.XPATH, "//button[contains(., 'Next')]"),
        ], timeout=5)
        
        return option_clicked
    
    def _handle_page2_extract_records(self) -> Dict[str, Optional[str]]:
        """
        Handle Page 2: Expand Advanced options, check DKIM, extract DNS records.
        
        Returns dict with all extracted DNS record values.
        """
        result = {
            "mx_value": None,
            "spf_value": None,
            "autodiscover_value": None,
            "dkim_selector1": None,
            "dkim_selector2": None,
        }
        
        # Wait for page to load
        time.sleep(3)
        
        # Click "Advanced options" to expand DKIM section
        advanced_selectors = [
            (By.XPATH, "//*[contains(text(), 'Advanced options')]"),
            (By.XPATH, "//button[contains(., 'Advanced')]"),
            (By.XPATH, "//span[contains(text(), 'Advanced')]"),
            (By.XPATH, "//*[contains(@class, 'expand')]//*[contains(text(), 'Advanced')]"),
        ]
        
        for by, value in advanced_selectors:
            try:
                elements = self.driver.find_elements(by, value)
                for elem in elements:
                    if elem.is_displayed():
                        logger.info(f"[{self.domain_name}] Clicking 'Advanced options'...")
                        self._safe_click(elem, "Advanced options")
                        time.sleep(2)
                        break
            except:
                continue
        
        # Check DKIM checkbox
        dkim_checkbox_selectors = [
            (By.XPATH, "//input[@type='checkbox'][following-sibling::*[contains(text(), 'DKIM')]]"),
            (By.XPATH, "//*[contains(text(), 'DomainKeys Identified Mail')]//input[@type='checkbox']"),
            (By.XPATH, "//label[contains(., 'DKIM')]//input"),
            (By.XPATH, "//input[contains(@id, 'dkim') or contains(@name, 'dkim')]"),
            (By.XPATH, "//*[contains(text(), 'DKIM')]/preceding::input[@type='checkbox'][1]"),
        ]
        
        for by, value in dkim_checkbox_selectors:
            try:
                elements = self.driver.find_elements(by, value)
                for elem in elements:
                    if elem.is_displayed():
                        # Check if not already checked
                        is_checked = elem.get_attribute('checked') or elem.is_selected()
                        if not is_checked:
                            logger.info(f"[{self.domain_name}] Checking DKIM checkbox...")
                            self._safe_click(elem, "DKIM checkbox")
                            time.sleep(2)
                        else:
                            logger.info(f"[{self.domain_name}] DKIM checkbox already checked")
                        break
            except:
                continue
        
        # Also try clicking the label for DKIM
        self._click_if_exists([
            (By.XPATH, "//label[contains(., 'DomainKeys')]"),
            (By.XPATH, "//*[contains(text(), 'DomainKeys Identified Mail')]"),
        ], timeout=2)
        
        time.sleep(2)
        
        # Now extract all DNS record values from the page
        page_source = self.driver.page_source
        
        # Extract MX record
        mx_patterns = [
            r'([a-z0-9\-]+\.mail\.protection\.outlook\.com)',
        ]
        for pattern in mx_patterns:
            match = re.search(pattern, page_source, re.IGNORECASE)
            if match:
                result["mx_value"] = match.group(1)
                logger.info(f"[{self.domain_name}] Found MX: {result['mx_value']}")
                break
        
        # Extract DKIM CNAME values
        # New Microsoft format: selector1-domain._domainkey.tenant.r-v1.dkim.mail.microsoft
        # Old format: selector1-domain._domainkey.tenant.onmicrosoft.com
        dkim1_patterns = [
            r'(selector1-[a-z0-9\-]+\._domainkey\.[a-z0-9\-]+\.r-v1\.dkim\.mail\.microsoft)',
            r'(selector1-[a-z0-9\-]+\._domainkey\.[a-z0-9\-]+\.onmicrosoft\.com)',
        ]
        
        for pattern in dkim1_patterns:
            match = re.search(pattern, page_source, re.IGNORECASE)
            if match:
                result["dkim_selector1"] = match.group(1)
                logger.info(f"[{self.domain_name}] Found DKIM selector1: {result['dkim_selector1']}")
                break
        
        dkim2_patterns = [
            r'(selector2-[a-z0-9\-]+\._domainkey\.[a-z0-9\-]+\.r-v1\.dkim\.mail\.microsoft)',
            r'(selector2-[a-z0-9\-]+\._domainkey\.[a-z0-9\-]+\.onmicrosoft\.com)',
        ]
        
        for pattern in dkim2_patterns:
            match = re.search(pattern, page_source, re.IGNORECASE)
            if match:
                result["dkim_selector2"] = match.group(1)
                logger.info(f"[{self.domain_name}] Found DKIM selector2: {result['dkim_selector2']}")
                break
        
        # Also check input fields for values
        for inp in self.driver.find_elements(By.TAG_NAME, "input"):
            try:
                val = inp.get_attribute('value') or ''
                if 'selector1' in val.lower() and not result["dkim_selector1"]:
                    result["dkim_selector1"] = val.strip()
                    logger.info(f"[{self.domain_name}] Found DKIM1 in input: {val[:50]}...")
                elif 'selector2' in val.lower() and not result["dkim_selector2"]:
                    result["dkim_selector2"] = val.strip()
                    logger.info(f"[{self.domain_name}] Found DKIM2 in input: {val[:50]}...")
                elif 'mail.protection.outlook' in val.lower() and not result["mx_value"]:
                    result["mx_value"] = val.strip()
                    logger.info(f"[{self.domain_name}] Found MX in input: {val}")
                elif 'autodiscover.outlook.com' in val.lower() and not result["autodiscover_value"]:
                    result["autodiscover_value"] = val.strip()
                    logger.info(f"[{self.domain_name}] Found autodiscover in input: {val}")
            except:
                continue

        autodiscover_match = re.search(r'(autodiscover\.outlook\.com)', page_source, re.IGNORECASE)
        if autodiscover_match and not result["autodiscover_value"]:
            result["autodiscover_value"] = autodiscover_match.group(1)
            logger.info(f"[{self.domain_name}] Found autodiscover: {result['autodiscover_value']}")
        
        return result
    
    async def _add_dns_to_cloudflare(
        self,
        cloudflare_zone_id: str,
        cloudflare_service,
        mx_value: Optional[str],
        spf_value: Optional[str],
        autodiscover_value: Optional[str],
        dkim_selector1: Optional[str],
        dkim_selector2: Optional[str],
    ) -> dict[str, bool]:
        """Add all DNS records to Cloudflare, handling duplicates."""
        missing_values = [
            name
            for name, value in {
                "MX": mx_value,
                "SPF": spf_value,
                "autodiscover": autodiscover_value,
                "DKIM selector1": dkim_selector1,
                "DKIM selector2": dkim_selector2,
            }.items()
            if not value
        ]
        if missing_values:
            raise RuntimeError(f"missing required DNS values: {', '.join(missing_values)}")

        write_results = {
            "mx": False,
            "spf": False,
            "autodiscover": False,
            "dkim_selector1": False,
            "dkim_selector2": False,
            "dmarc": False,
        }
        
        # First, delete conflicting SPF records
        logger.info(f"[{self.domain_name}] Cleaning up conflicting DNS records...")
        try:
            records = await cloudflare_service.get_dns_records(cloudflare_zone_id, record_type="TXT")
            for record in records:
                content = record.get('content', '')
                if 'v=spf1' in content:
                    logger.info(f"[{self.domain_name}] Deleting old SPF: {content[:50]}...")
                    await cloudflare_service.delete_dns_record(cloudflare_zone_id, record['id'])
        except Exception as e:
            logger.warning(f"[{self.domain_name}] Error cleaning SPF records: {e}")
        
        # Delete conflicting DKIM CNAMEs
        try:
            records = await cloudflare_service.get_dns_records(cloudflare_zone_id, record_type="CNAME")
            for record in records:
                name = record.get('name', '')
                if 'selector1._domainkey' in name or 'selector2._domainkey' in name:
                    logger.info(f"[{self.domain_name}] Deleting old DKIM CNAME: {name}")
                    await cloudflare_service.delete_dns_record(cloudflare_zone_id, record['id'])
        except Exception as e:
            logger.warning(f"[{self.domain_name}] Error cleaning DKIM records: {e}")
        
        # Add MX record
        try:
            await cloudflare_service.ensure_mx_record(cloudflare_zone_id, "@", mx_value, 0, self.domain_name)
            write_results["mx"] = True
            logger.info(f"[{self.domain_name}] ✓ MX record added")
        except Exception as e:
            logger.error(f"[{self.domain_name}] MX error: {e}")
        
        # Add SPF record
        try:
            if hasattr(cloudflare_service, "replace_spf_record"):
                await cloudflare_service.replace_spf_record(cloudflare_zone_id, self.domain_name, spf_value)
            else:
                await cloudflare_service.create_txt_record(cloudflare_zone_id, "@", spf_value)
            write_results["spf"] = True
            logger.info(f"[{self.domain_name}] ✓ SPF record added")
        except Exception as e:
            logger.error(f"[{self.domain_name}] SPF error: {e}")
        
        # Add autodiscover CNAME
        try:
            await cloudflare_service.ensure_autodiscover_cname(cloudflare_zone_id, self.domain_name, autodiscover_value)
            write_results["autodiscover"] = True
            logger.info(f"[{self.domain_name}] ✓ Autodiscover CNAME added")
        except Exception as e:
            logger.error(f"[{self.domain_name}] Autodiscover error: {e}")
        
        # Add DKIM CNAMEs
        try:
            dkim_result = await cloudflare_service.ensure_dkim_cnames(
                cloudflare_zone_id, self.domain_name,
                dkim_selector1, dkim_selector2
            )
            dkim_errors = dkim_result.get("errors") if isinstance(dkim_result, dict) else None
            write_results["dkim_selector1"] = not dkim_errors and bool(dkim_result.get("selector1_id") if isinstance(dkim_result, dict) else True)
            write_results["dkim_selector2"] = not dkim_errors and bool(dkim_result.get("selector2_id") if isinstance(dkim_result, dict) else True)
            logger.info(f"[{self.domain_name}] ✓ DKIM CNAMEs added")
        except Exception as e:
            logger.error(f"[{self.domain_name}] DKIM error: {e}")

        try:
            await cloudflare_service.ensure_txt_record(
                cloudflare_zone_id,
                "_dmarc",
                "v=DMARC1; p=none;",
                self.domain_name,
            )
            write_results["dmarc"] = True
            logger.info(f"[{self.domain_name}] ✓ DMARC record added")
        except Exception as e:
            logger.error(f"[{self.domain_name}] DMARC error: {e}")

        failed_records = [name for name, ok in write_results.items() if not ok]
        if failed_records:
            raise RuntimeError(f"Cloudflare DNS setup failed for: {', '.join(failed_records)}")

        return write_results
    
    async def _handle_page3_validation(
        self,
        cloudflare_zone_id: str,
        cloudflare_service,
        spf_value: Optional[str],
        dkim_selector1: Optional[str],
        dkim_selector2: Optional[str],
        max_retries: int = 3
    ) -> bool:
        """
        Handle Page 3: DNS validation results.
        
        If there are errors, try to fix them and retry.
        
        Returns True if validation succeeds.
        """
        for retry in range(max_retries):
            page_text = self.driver.page_source.lower()
            
            # Check for success
            if _is_domain_setup_complete_text(page_text):
                logger.info(f"[{self.domain_name}] ✓ DNS validation passed!")
                return True
            
            # Check for all green checkmarks (validation passed)
            # Look for patterns that indicate all records validated
            if "✓" in self.driver.page_source or "check" in page_text:
                # Count errors vs successes
                error_count = page_text.count("error") + page_text.count("invalid") + page_text.count("doesn't match")
                if error_count == 0:
                    logger.info(f"[{self.domain_name}] ✓ No errors detected, validation likely passed")
                    return True
            
            # Check for specific errors
            has_spf_error = "only have one spf" in page_text or "multiple spf" in page_text
            has_dkim_error = "doesn't match" in page_text and "dkim" in page_text
            has_any_error = "error" in page_text or "invalid" in page_text or "doesn't match" in page_text
            
            if not has_any_error:
                # No errors detected, try clicking Continue
                logger.info(f"[{self.domain_name}] No errors detected, clicking Continue...")
                self._click_if_exists([
                    (By.XPATH, "//button[contains(., 'Continue')]"),
                    (By.XPATH, "//button[contains(., 'Done')]"),
                ], timeout=3)
                time.sleep(3)
                
                # Check again
                if _is_domain_setup_complete_text(self.driver.page_source):
                    return True
                continue
            
            logger.warning(f"[{self.domain_name}] DNS validation error detected (retry {retry + 1}/{max_retries})")
            self._screenshot(f"wizard_validation_error_{retry}", self.domain_name)
            
            # Fix specific errors
            if has_spf_error:
                logger.info(f"[{self.domain_name}] Fixing duplicate SPF records...")
                try:
                    records = await cloudflare_service.get_dns_records(cloudflare_zone_id, record_type="TXT")
                    spf_records = [r for r in records if 'v=spf1' in r.get('content', '')]
                    
                    # Delete all SPF records
                    for record in spf_records:
                        await cloudflare_service.delete_dns_record(cloudflare_zone_id, record['id'])
                    
                    # Add the correct one
                    if spf_value:
                        await cloudflare_service.create_txt_record(cloudflare_zone_id, "@", spf_value)
                    
                    logger.info(f"[{self.domain_name}] SPF records fixed")
                except Exception as e:
                    logger.error(f"[{self.domain_name}] Failed to fix SPF: {e}")
            
            if has_dkim_error:
                logger.info(f"[{self.domain_name}] Fixing DKIM CNAME records...")
                try:
                    records = await cloudflare_service.get_dns_records(cloudflare_zone_id, record_type="CNAME")
                    for record in records:
                        name = record.get('name', '')
                        if '_domainkey' in name:
                            await cloudflare_service.delete_dns_record(cloudflare_zone_id, record['id'])
                    
                    # Re-add with correct values
                    if dkim_selector1 and dkim_selector2:
                        await cloudflare_service.ensure_dkim_cnames(
                            cloudflare_zone_id, self.domain_name,
                            dkim_selector1, dkim_selector2
                        )
                    
                    logger.info(f"[{self.domain_name}] DKIM records fixed")
                except Exception as e:
                    logger.error(f"[{self.domain_name}] Failed to fix DKIM: {e}")
            
            # Wait for DNS propagation
            logger.info(f"[{self.domain_name}] Waiting 15s for DNS propagation...")
            await asyncio.sleep(15)
            
            # Click Continue/Retry
            self._click_if_exists([
                (By.XPATH, "//button[contains(., 'Continue')]"),
                (By.XPATH, "//button[contains(., 'Retry')]"),
                (By.XPATH, "//button[contains(., 'Check again')]"),
            ], timeout=5)
            
            time.sleep(10)
        
        # Max retries exceeded
        logger.error(f"[{self.domain_name}] DNS validation failed after {max_retries} retries")
        return False


async def complete_domain_wizard_after_verification(
    driver,
    domain_name: str,
    cloudflare_zone_id: str,
    cloudflare_service,
    screenshot_func=None,
    safe_click_func=None,
) -> WizardCompletionResult:
    """
    Complete the domain wizard after TXT verification succeeds.
    
    This is the main entry point called from AdminPortalAutomation.setup_domain_complete().
    
    Args:
        driver: Selenium WebDriver instance
        domain_name: Domain being configured
        cloudflare_zone_id: Cloudflare zone ID
        cloudflare_service: CloudflareService instance
        screenshot_func: Optional screenshot function
        safe_click_func: Optional safe click function
    
    Returns:
        WizardCompletionResult with DNS values and success status
    """
    completer = DomainWizardCompleter(
        driver=driver,
        domain_name=domain_name,
        screenshot_func=screenshot_func,
        safe_click_func=safe_click_func,
    )
    
    return await completer.complete_wizard(cloudflare_zone_id, cloudflare_service)
