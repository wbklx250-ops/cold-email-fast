"""
Tenant Domain Checker - Selenium-based domain audit tool.

Logs into each tenant's M365 Admin Portal and scrapes the Domains page
to report which custom domains exist and their verification status.

Supports chunked parallel processing for speed on Railway deployments.
"""

import asyncio
import os
import time
import re
import uuid
import logging
from typing import Optional, List, Callable
from dataclasses import dataclass, field, asdict
from concurrent.futures import ThreadPoolExecutor

import pyotp
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException

from app.services.selenium.browser import create_driver, cleanup_driver, kill_all_browsers

logger = logging.getLogger(__name__)


# --- Parallel browser config ---
def _get_checker_parallel_browsers() -> int:
    """Get parallel browser count from settings or env var."""
    try:
        from app.core.config import get_settings
        return get_settings().checker_parallel_browsers
    except Exception:
        return int(os.environ.get("CHECKER_PARALLEL_BROWSERS", "2"))


CHECKER_PARALLEL_BROWSERS = _get_checker_parallel_browsers()


@dataclass
class DomainInfo:
    """A single domain found in a tenant."""
    name: str
    is_verified: bool = False
    is_default: bool = False
    status_text: str = ""  # Raw status text from the page


@dataclass
class TenantCheckResult:
    """Result of checking one tenant."""
    admin_email: str
    tenant_name: str = ""
    login_success: bool = False
    login_error: str = ""
    domains: List[DomainInfo] = field(default_factory=list)
    custom_domains: List[DomainInfo] = field(default_factory=list)
    screenshot_path: str = ""

    def to_dict(self) -> dict:
        # Filter out ALL .onmicrosoft.com domains from every list
        custom = [d for d in self.domains if not d.name.endswith(".onmicrosoft.com")]
        verified = [d for d in custom if d.is_verified]
        unverified = [d for d in custom if not d.is_verified]

        return {
            "admin_email": self.admin_email,
            "tenant_name": self.tenant_name,
            "login_success": self.login_success,
            "login_error": self.login_error,
            "verified_domains": [{"name": d.name, "status": d.status_text or "Healthy"} for d in verified],
            "unverified_domains": [{"name": d.name, "status": d.status_text or "Setup incomplete"} for d in unverified],
            "verified_count": len(verified),
            "unverified_count": len(unverified),
            "custom_domain_count": len(custom),
        }


# =============================================================================
# PARALLEL PROCESSING
# =============================================================================


async def check_tenants_parallel(
    tenants: list[dict],
    headless: bool = True,
    max_workers: int | None = None,
    progress_callback: Callable | None = None,
) -> list[TenantCheckResult]:
    """
    Check multiple tenants with chunked parallel processing.

    Strategy:
    - Process in chunks of max_workers (default: CHECKER_PARALLEL_BROWSERS)
    - Each chunk runs N browsers simultaneously via ThreadPoolExecutor
    - After each chunk: quit all drivers, force cleanup Chrome processes,
      clean up temp profiles, brief pause
    - This prevents Chrome memory accumulation that crashes Railway containers

    Args:
        tenants: List of dicts with admin_email, admin_password, totp_secret
        headless: Run Chrome headless (should ALWAYS be True on Railway)
        max_workers: Override parallel browser count
        progress_callback: Called with (processed_count, total, latest_result) after each tenant

    Returns:
        List of TenantCheckResult
    """
    max_workers = max_workers or CHECKER_PARALLEL_BROWSERS
    total = len(tenants)
    results: list[TenantCheckResult] = []

    logger.info(f"Starting domain check: {total} tenants, {max_workers} parallel browsers")

    # Process in chunks
    for chunk_start in range(0, total, max_workers):
        chunk = tenants[chunk_start:chunk_start + max_workers]
        chunk_num = (chunk_start // max_workers) + 1
        total_chunks = (total + max_workers - 1) // max_workers

        logger.info(f"=== Chunk {chunk_num}/{total_chunks}: processing {len(chunk)} tenants ===")

        # Run chunk in parallel using threads (Selenium is sync)
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=len(chunk)) as executor:
            futures = []
            for tenant in chunk:
                future = loop.run_in_executor(
                    executor,
                    check_tenant_domains,
                    tenant["admin_email"],
                    tenant["admin_password"],
                    tenant.get("totp_secret"),
                    headless,
                )
                futures.append(future)

            # Wait for all in this chunk to complete
            chunk_results = await asyncio.gather(*futures, return_exceptions=True)

        # Process results
        for i, result in enumerate(chunk_results):
            if isinstance(result, Exception):
                tenant = chunk[i]
                tenant_name = tenant["admin_email"].split("@")[1].replace(".onmicrosoft.com", "")
                error_result = TenantCheckResult(
                    admin_email=tenant["admin_email"],
                    tenant_name=tenant_name,
                    login_success=False,
                    login_error=f"Exception: {str(result)[:200]}",
                )
                results.append(error_result)
                logger.error(f"[{tenant_name}] Exception: {result}")
            else:
                results.append(result)

            # Progress callback
            if progress_callback:
                try:
                    progress_callback(len(results), total, results[-1])
                except Exception:
                    pass

        # === CLEANUP BETWEEN CHUNKS ===
        # Critical on Railway — Chrome temp profiles and zombie processes accumulate
        logger.info(f"Chunk {chunk_num} complete. Cleaning up Chrome processes...")
        try:
            kill_all_browsers()
        except Exception as e:
            logger.warning(f"Cleanup warning: {e}")

        # Brief pause between chunks to let OS reclaim memory
        # and avoid Microsoft rate-limiting from rapid sequential logins
        if chunk_start + max_workers < total:
            pause = 3
            logger.info(f"Pausing {pause}s before next chunk...")
            await asyncio.sleep(pause)

    logger.info(f"Domain check complete: {len(results)} tenants processed")
    return results


# =============================================================================
# SINGLE TENANT CHECK (sync — runs inside ThreadPoolExecutor)
# =============================================================================


def check_tenant_domains(
    admin_email: str,
    admin_password: str,
    totp_secret: Optional[str] = None,
    headless: bool = True,
    _max_retries: int = 2,
) -> TenantCheckResult:
    """
    Log into a tenant and scrape its domain list.

    This is a SYNCHRONOUS function (Selenium is sync).
    Run it in a thread via asyncio.to_thread() or ThreadPoolExecutor.
    Includes retry logic for Chrome crashes / connection errors.

    Args:
        admin_email: e.g. "admin@brightnova.onmicrosoft.com"
        admin_password: The admin password
        totp_secret: Optional TOTP secret for MFA (None if not enrolled yet)
        headless: Run Chrome headless (default True)
        _max_retries: Max attempts (default 2 = 1 initial + 1 retry)

    Returns:
        TenantCheckResult with domains found
    """
    tenant_name = (
        admin_email.split("@")[1].replace(".onmicrosoft.com", "")
        if "@" in admin_email
        else admin_email
    )

    last_error = ""
    for attempt in range(1, _max_retries + 1):
        result = TenantCheckResult(admin_email=admin_email, tenant_name=tenant_name)
        driver = None
        try:
            # === CREATE BROWSER ===
            driver = create_driver(headless=headless)
            driver.implicitly_wait(8)
            driver.set_page_load_timeout(45)

            # === LOGIN ===
            login_ok = _do_login(driver, tenant_name, admin_email, admin_password, totp_secret)
            if not login_ok:
                result.login_success = False
                result.login_error = "Login failed — see logs for details"
                return result

            result.login_success = True

            # === NAVIGATE TO DOMAINS PAGE ===
            # Use the current admin URL (admin.cloud.microsoft) to avoid
            # broken redirect chains from admin.microsoft.com
            domains_url = _get_domains_url(driver, tenant_name)
            logger.info(f"[{tenant_name}] Navigating to Domains page: {domains_url}")
            driver.get(domains_url)
            time.sleep(4)

            _wait_for_domains_page(driver, tenant_name, domains_url)

            # === SCRAPE DOMAINS ===
            domains = _scrape_domains(driver, tenant_name)
            result.domains = domains
            result.custom_domains = [d for d in domains if not d.name.endswith(".onmicrosoft.com")]

            custom_count = len(result.custom_domains)
            verified_count = sum(1 for d in result.custom_domains if d.is_verified)
            if custom_count > 0:
                names = ", ".join(d.name for d in result.custom_domains)
                logger.info(
                    f"[{tenant_name}] Found {verified_count}/{custom_count} verified custom domains: {names}"
                )
            else:
                logger.info(f"[{tenant_name}] No custom domains found")

            return result  # Success — return immediately

        except (WebDriverException, ConnectionError, OSError) as e:
            error_msg = str(e)[:200]
            last_error = error_msg
            logger.warning(f"[{tenant_name}] Attempt {attempt}/{_max_retries} failed (browser error): {error_msg}")
        except Exception as e:
            error_msg = str(e)[:200]
            last_error = error_msg
            # Only retry on connection-like errors
            if "connection" in error_msg.lower() or "max retries" in error_msg.lower():
                logger.warning(f"[{tenant_name}] Attempt {attempt}/{_max_retries} failed (connection): {error_msg}")
            else:
                logger.error(f"[{tenant_name}] Unexpected error (no retry): {error_msg}")
                result.login_error = f"Error: {error_msg}"
                return result
        finally:
            if driver:
                try:
                    cleanup_driver(driver)
                except Exception:
                    pass

        # Clean up zombie Chrome processes before retry
        if attempt < _max_retries:
            try:
                kill_all_browsers()
            except Exception:
                pass
            logger.info(f"[{tenant_name}] Retrying in 3s...")
            time.sleep(3)

    # All retries exhausted
    logger.error(f"[{tenant_name}] All {_max_retries} attempts failed: {last_error}")
    result = TenantCheckResult(admin_email=admin_email, tenant_name=tenant_name)
    result.login_error = f"Failed after {_max_retries} attempts: {last_error}"
    return result


def _do_login(
    driver: webdriver.Chrome,
    tenant_name: str,
    admin_email: str,
    admin_password: str,
    totp_secret: Optional[str],
) -> bool:
    """
    Login to M365 Admin Portal. Handles email, password, MFA, stay-signed-in.

    IMPORTANT: This follows the EXACT same patterns as the proven login code
    in admin_portal.py and login.py. Do NOT deviate.

    Returns True if login succeeded, False otherwise.
    """
    logger.info(f"[{tenant_name}] Logging in as {admin_email}...")

    driver.get("https://admin.microsoft.com")
    time.sleep(2)

    # --- ENTER EMAIL ---
    try:
        email_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "loginfmt"))
        )
        email_input.clear()
        email_input.send_keys(admin_email)
        email_input.send_keys(Keys.RETURN)
        logger.info(f"[{tenant_name}] Entered email")
        time.sleep(2)
    except TimeoutException:
        logger.error(f"[{tenant_name}] Could not find email input")
        return False

    # --- CHECK FOR ERRORS (account doesn't exist, etc) ---
    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    if "doesn't exist" in page_text or "couldn't find" in page_text:
        logger.error(f"[{tenant_name}] Account not found")
        return False

    # --- ENTER PASSWORD ---
    try:
        password_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "passwd"))
        )
        password_input.clear()
        password_input.send_keys(admin_password)
        password_input.send_keys(Keys.RETURN)
        logger.info(f"[{tenant_name}] Entered password")
        time.sleep(2)
    except TimeoutException:
        # Might be a password change screen or other flow
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        if "update your password" in page_text or "change password" in page_text:
            logger.warning(f"[{tenant_name}] Password change required — needs first login")
            return False
        logger.error(f"[{tenant_name}] Could not find password input")
        return False

    # --- CHECK FOR PASSWORD ERRORS ---
    time.sleep(1.5)
    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    for err in [
        "password is incorrect",
        "account or password is incorrect",
        "account has been locked",
    ]:
        if err in page_text:
            logger.error(f"[{tenant_name}] Login failed: {err}")
            return False

    # --- DETECT PASSWORD CHANGE REQUIRED ---
    if "update your password" in page_text or "change password" in page_text:
        logger.warning(f"[{tenant_name}] Password change required — needs first login")
        return False

    # --- HANDLE MFA ---
    try:
        totp_input = WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.NAME, "otc"))
        )
        if not totp_secret:
            logger.warning(f"[{tenant_name}] MFA required but no TOTP secret provided")
            return False

        totp = pyotp.TOTP(totp_secret)
        code = totp.now()
        logger.info(f"[{tenant_name}] MFA detected, entering TOTP code")
        totp_input.clear()
        totp_input.send_keys(code)
        totp_input.send_keys(Keys.RETURN)
        time.sleep(2.5)
    except TimeoutException:
        # No MFA prompt — either not required or different flow
        logger.info(f"[{tenant_name}] No MFA prompt detected")

        # Check if we're on MFA enrollment screen (fresh tenant)
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        if "more information required" in page_text or "set up your account" in page_text:
            logger.warning(f"[{tenant_name}] MFA enrollment required — needs first login")
            return False

    # --- HANDLE "STAY SIGNED IN?" ---
    time.sleep(1.5)
    try:
        yes_btn = driver.find_element(By.ID, "idSIButton9")
        if yes_btn.is_displayed():
            yes_btn.click()
            logger.info(f"[{tenant_name}] Clicked 'Yes' on stay signed in")
            time.sleep(1.5)
    except (NoSuchElementException, Exception):
        pass
    try:
        no_btn = driver.find_element(By.ID, "idBtn_Back")
        if no_btn.is_displayed():
            no_btn.click()
            logger.info(f"[{tenant_name}] Clicked 'No' on stay signed in")
            time.sleep(1.5)
    except (NoSuchElementException, Exception):
        pass

    # --- DISMISS MFA SETUP INTERRUPT ---
    # admin.cloud.microsoft/mfasetup?registered=false shows after every login
    # even when TOTP is already enrolled via SSPR. We always click "Skip for now".
    try:
        from app.services.selenium.admin_portal import dismiss_mfa_setup_interrupt
        dismiss_mfa_setup_interrupt(driver, tenant_name)
    except Exception as e:
        logger.warning(f"[{tenant_name}] MFA setup interrupt dismiss raised: {e}")

    # --- VERIFY WE'RE IN THE ADMIN PORTAL ---
    time.sleep(2)
    current_url = driver.current_url.lower()
    if "admin.microsoft.com" in current_url or "portal.office.com" in current_url:
        logger.info(f"[{tenant_name}] Login successful — URL: {current_url}")
        return True

    # Sometimes there's a redirect delay
    time.sleep(4)
    current_url = driver.current_url.lower()
    if "admin" in current_url or "office" in current_url or "microsoft" in current_url:
        logger.info(f"[{tenant_name}] Login appears successful — URL: {current_url}")
        return True

    logger.warning(f"[{tenant_name}] Login uncertain — URL: {current_url}")
    # Still return True — the domains page navigation will confirm
    return True


def _get_domains_url(driver: webdriver.Chrome, tenant_name: str) -> str:
    """
    Build the Domains page URL using the browser's current admin domain.

    After login, Microsoft redirects to admin.cloud.microsoft (new) or
    admin.microsoft.com (legacy). We use whichever the browser is on
    to avoid a broken redirect chain.
    """
    current_url = driver.current_url
    # Extract base: e.g. "https://admin.cloud.microsoft" or "https://admin.microsoft.com"
    # Strip hash fragment and query params
    base = current_url.split("#")[0].split("?")[0].rstrip("/")
    domains_url = f"{base}/#/Domains"
    logger.info(f"[{tenant_name}] Admin base URL: {base}")
    return domains_url


def _wait_for_domains_page(
    driver: webdriver.Chrome,
    tenant_name: str,
    domains_url: str = "",
    timeout: int = 30,
):
    """Wait for the Domains page to fully load its domain list content."""
    logger.info(f"[{tenant_name}] Waiting for Domains page to load...")

    # Phase 1: Wait for .onmicrosoft.com to appear in page text.
    # This proves the actual domain TABLE data has rendered (every tenant has one).
    # Do NOT check for generic "domain" — that matches the page nav/header immediately.
    for attempt in range(timeout // 2):
        time.sleep(1.5)
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()

        if ".onmicrosoft.com" in page_text:
            logger.info(f"[{tenant_name}] Domains page loaded — .onmicrosoft.com found (attempt {attempt + 1})")
            # Phase 2: Stabilization delay — let React finish rendering ALL rows
            time.sleep(2)
            return

    # Phase 3: Timeout — domains data never appeared.
    # Reload the page and try once more (handles broken SPA routing).
    logger.warning(f"[{tenant_name}] Domains page didn't load after {timeout}s — reloading...")
    if domains_url:
        driver.get(domains_url)
    else:
        driver.refresh()
    time.sleep(5)

    # Second wait — shorter, since we just reloaded
    for attempt in range(8):
        time.sleep(1.5)
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        if ".onmicrosoft.com" in page_text:
            logger.info(f"[{tenant_name}] Domains page loaded after reload (attempt {attempt + 1})")
            time.sleep(2)
            return

    logger.warning(f"[{tenant_name}] Domains page still not loaded after reload — proceeding anyway")
    time.sleep(2)


def _scrape_domains(driver: webdriver.Chrome, tenant_name: str) -> List[DomainInfo]:
    """
    Scrape the domain list from the M365 Admin Domains page.

    The M365 admin portal domains page is a React SPA. The domain list typically
    appears as rows with domain name + status. We extract using multiple strategies.

    Includes a retry loop: if 0 domains found, waits and retries up to 2 more times
    to handle slow React rendering.
    """
    domain_pattern = re.compile(
        r"([a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
        r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*"
        r"\.[a-zA-Z]{2,})"
    )

    # Retry loop — React SPA may need more time to render the domain table
    max_scrape_attempts = 3
    for scrape_attempt in range(1, max_scrape_attempts + 1):
        domains: List[DomainInfo] = []
        found_domains: set = set()

        # --- Phase 0: Wait for [role='row'] elements containing domain text ---
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[role='row']"))
            )
            # Validate: check if ANY row contains .onmicrosoft.com (proves domain table, not sidebar)
            rows = driver.find_elements(By.CSS_SELECTOR, "[role='row']")
            has_domain_rows = any(".onmicrosoft.com" in r.text.lower() for r in rows if r.text.strip())
            if has_domain_rows:
                logger.info(f"[{tenant_name}] Domain table rows found (scrape attempt {scrape_attempt})")
            else:
                logger.warning(f"[{tenant_name}] [role='row'] found but no domain text — table may not be loaded (scrape attempt {scrape_attempt})")
                if scrape_attempt < max_scrape_attempts:
                    logger.info(f"[{tenant_name}] Reloading page before scrape retry...")
                    driver.refresh()
                    time.sleep(5)
                    continue
        except TimeoutException:
            logger.warning(f"[{tenant_name}] No [role='row'] elements found after 10s (scrape attempt {scrape_attempt})")
            if scrape_attempt < max_scrape_attempts:
                logger.info(f"[{tenant_name}] Reloading page before scrape retry...")
                driver.refresh()
                time.sleep(5)
                continue

        page_text = driver.find_element(By.TAG_NAME, "body").text

        # --- STRATEGY 1: Look for table rows / list items with domain patterns ---
        try:
            # M365 admin uses FluentUI — look for domain entries in the list
            raw_rows = driver.find_elements(
                By.CSS_SELECTOR,
                "[role='row'], [role='listitem'], tr, .ms-DetailsRow",
            )

            # Collect row texts and sort by length (shortest first).
            # This ensures individual domain rows are processed before
            # parent/container rows that aggregate multiple domains' text.
            row_texts = []
            for row in raw_rows:
                row_text = row.text.strip()
                if row_text:
                    row_texts.append(row_text)
            row_texts.sort(key=len)

            for row_text in row_texts:
                matches = domain_pattern.findall(row_text)
                # Filter to real domain matches
                real_matches = []
                for match in matches:
                    if "." in match and len(match) > 4:
                        if any(skip in match.lower() for skip in [
                            "microsoft.com/", "aka.ms", "office.com/", "learn.microsoft",
                        ]):
                            continue
                        real_matches.append(match)

                # Skip rows with 2+ domains — these are parent/container rows
                # that aggregate text from multiple child rows
                if len(real_matches) > 1:
                    continue

                for match in real_matches:
                    if match.lower() in found_domains:
                        continue

                    domain_info = DomainInfo(name=match.lower())

                    if match.lower().endswith(".onmicrosoft.com"):
                        domain_info.is_default = True

                    # Default: custom domains are verified (M365 shows them as Healthy)
                    # Only mark unverified if explicit unverified indicators found
                    row_lower = row_text.lower()
                    is_custom = not match.lower().endswith(".onmicrosoft.com")
                    domain_info.is_verified = is_custom  # Custom = verified by default

                    # Explicitly unverified indicators override the default
                    if any(kw in row_lower for kw in [
                        "setup in progress", "incomplete", "action required",
                        "not verified", "pending", "setup incomplete",
                    ]):
                        domain_info.is_verified = False
                        domain_info.status_text = next(
                            (kw.title() for kw in ["setup in progress", "incomplete", "action required", "pending"]
                             if kw in row_lower),
                            "Setup in progress"
                        )

                    # Explicitly verified indicators confirm the default
                    if "healthy" in row_lower or "verified" in row_lower:
                        domain_info.is_verified = True
                        domain_info.status_text = "Healthy"

                    found_domains.add(match.lower())
                    domains.append(domain_info)
                    safe_row = row_text[:120].encode('ascii', 'replace').decode('ascii')
                    logger.info(f"[{tenant_name}] Domain: {match.lower()} verified={domain_info.is_verified} status='{domain_info.status_text}' row='{safe_row}'")

        except Exception as e:
            logger.warning(f"[{tenant_name}] Structured scraping failed: {e}")

        # --- STRATEGY 2: Fallback — extract from full page text ---
        if not domains:
            logger.info(f"[{tenant_name}] Strategy 1 found 0, trying full page text scraping")
            lines = page_text.split("\n")
            for line in lines:
                matches = domain_pattern.findall(line.strip())
                for match in matches:
                    if "." in match and len(match) > 4 and match.lower() not in found_domains:
                        if any(
                            skip in match.lower()
                            for skip in [
                                "microsoft.com/",
                                "aka.ms",
                                "office.com/",
                                "learn.microsoft",
                            ]
                        ):
                            continue

                        domain_info = DomainInfo(name=match.lower())
                        if match.lower().endswith(".onmicrosoft.com"):
                            domain_info.is_default = True

                        # Default custom domains to verified, same logic as Strategy 1
                        is_custom = not match.lower().endswith(".onmicrosoft.com")
                        domain_info.is_verified = is_custom

                        # Check adjacent text for explicit status overrides
                        line_lower = line.lower()
                        if any(kw in line_lower for kw in [
                            "setup in progress", "incomplete", "action required",
                            "not verified", "pending", "setup incomplete",
                        ]):
                            domain_info.is_verified = False
                            domain_info.status_text = "Setup in progress"
                        if "healthy" in line_lower or "verified" in line_lower:
                            domain_info.is_verified = True
                            domain_info.status_text = "Healthy"

                        found_domains.add(match.lower())
                        domains.append(domain_info)

        # If we found domains, we're done
        if domains:
            logger.info(f"[{tenant_name}] Scraped {len(domains)} domains total (attempt {scrape_attempt})")
            return domains

        # No domains found — retry with a wait if attempts remain
        if scrape_attempt < max_scrape_attempts:
            logger.warning(f"[{tenant_name}] 0 domains found on scrape attempt {scrape_attempt}, retrying in 4s...")
            time.sleep(4)

    # All scrape attempts exhausted
    logger.warning(f"[{tenant_name}] Scraped 0 domains after {max_scrape_attempts} attempts")
    return domains
