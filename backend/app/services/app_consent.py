from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import pyotp
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException

from app.core.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
SCREENSHOT_DIR = os.path.join(settings.screenshot_dir or "/tmp/screenshots", "app_consent")


@dataclass
class TenantCredentials:
    tenant_id: str
    admin_email: str
    admin_password: str
    totp_secret: str
    domain: str


KNOWN_APPS = {
    "smartlead": {
        "name": "Smartlead.ai",
        "client_id": "5913b6f2-da52-4f3a-b81c-56a99e05e243",
        "patch_scopes": False,
    },
    "plusvibe": {
        "name": "PlusVibe",
        "client_id": "1766ec29-af23-4877-80cf-f688adeb5b62",
        "patch_scopes": False,
    },
    "instantly": {
        "name": "Instantly.ai",
        "client_id": "65ad96b6-fbeb-40b5-b404-2a415d074c97",
        "patch_scopes": False,
    },
}

APP_CLIENT_ID_ENV = {
    "smartlead": "SMARTLEAD_CLIENT_ID",
    "plusvibe": "PLUSVIBE_CLIENT_ID",
    "instantly": "INSTANTLY_CLIENT_ID",
}

DEFAULT_SEQUENCER_APP_KEY = os.getenv("DEFAULT_SEQUENCER_APP", "instantly").strip().lower()
if DEFAULT_SEQUENCER_APP_KEY not in KNOWN_APPS:
    DEFAULT_SEQUENCER_APP_KEY = "instantly"

GRAPH_RESOURCE_APP_ID = "00000003-0000-0000-c000-000000000000"
EXCHANGE_RESOURCE_APP_ID = "00000002-0000-0ff1-ce00-000000000000"

FULL_GRAPH_SCOPES = (
    "User.Read Mail.ReadWrite Mail.ReadWrite.Shared Mail.Send Mail.Send.Shared "
    "IMAP.AccessAsUser.All SMTP.Send offline_access openid profile email"
)

FULL_EXCHANGE_SCOPES = "Mail.ReadWrite IMAP.AccessAsUser.All SMTP.Send"


def normalize_sequencer_key(app_key: Optional[str], *, strict: bool = False) -> str:
    if not app_key:
        return DEFAULT_SEQUENCER_APP_KEY
    key = app_key.strip().lower()
    if key not in KNOWN_APPS:
        if strict:
            raise ValueError(f"Unknown sequencer app '{app_key}'")
        return DEFAULT_SEQUENCER_APP_KEY
    return key


def get_sequencer_config(app_key: Optional[str], *, strict: bool = False) -> Dict:
    key = normalize_sequencer_key(app_key, strict=strict)
    config = dict(KNOWN_APPS[key])
    config["key"] = key

    env_key = APP_CLIENT_ID_ENV.get(key)
    if env_key:
        env_value = os.getenv(env_key)
        if env_value:
            config["client_id"] = env_value

    return config


class AppConsentGranter:
    """Grants OAuth admin consent for email apps across M365 tenants."""

    WAIT_PAGE_TRANSITION = 12
    WAIT_PAGE_LOAD = 90
    WAIT_ELEMENT = 20
    WAIT_AFTER_ACTION = 8
    WAIT_SHORT = 3

    def __init__(self, headless: bool = True, worker_id: int = 0):
        self.headless = headless
        self.worker_id = worker_id
        self.driver: Optional[webdriver.Chrome] = None

    def _log(self, message: str, level: str = "info") -> None:
        full_msg = f"[W{self.worker_id}] {message}"
        if level == "error":
            logger.error(full_msg)
        elif level == "warning":
            logger.warning(full_msg)
        else:
            logger.info(full_msg)

    def _screenshot(self, name: str) -> None:
        if not self.driver:
            return
        try:
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            path = os.path.join(SCREENSHOT_DIR, f"w{self.worker_id}_{name}.png")
            self.driver.save_screenshot(path)
        except Exception:
            pass

    def _setup_driver(self) -> bool:
        try:
            opts = Options()
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--window-size=1920,1080")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_argument("--disable-features=ThirdPartyCookieBlocking")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])

            prefs = {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
                "profile.password_manager_leak_detection": False,
                "profile.cookie_controls_mode": 0,
                "profile.block_third_party_cookies": False,
            }
            opts.add_experimental_option("prefs", prefs)

            if self.headless:
                opts.add_argument("--headless=new")
            else:
                opts.add_argument("--start-maximized")

            self.driver = webdriver.Chrome(options=opts)
            self.driver.implicitly_wait(5)

            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            self._log("Browser initialized")
            return True

        except Exception as exc:
            self._log(f"Failed to setup browser: {exc}", "error")
            return False

    def _cleanup(self) -> None:
        if not self.driver:
            return
        try:
            self.driver.quit()
        except Exception:
            pass
        self.driver = None

    def _login(self, creds: TenantCredentials) -> bool:
        self._log(f"Starting login for {creds.domain}...")

        self.driver.get("https://entra.microsoft.com/")
        time.sleep(self.WAIT_PAGE_TRANSITION)
        self._screenshot("login_01_initial")

        if not self._enter_email(creds.admin_email):
            return False

        if not self._enter_password(creds.admin_password):
            return False

        if not self._handle_post_password_flow(creds.totp_secret):
            return False

        self._log("Login successful")
        return True

    def _enter_email(self, email: str) -> bool:
        self._log("Entering email...")

        try:
            wait = WebDriverWait(self.driver, self.WAIT_ELEMENT)
            email_input = wait.until(
                EC.presence_of_element_located((By.ID, "i0116"))
            )
            email_input.clear()
            email_input.send_keys(email)
            self._log("Email entered")

            time.sleep(self.WAIT_SHORT)

            next_btn = wait.until(
                EC.element_to_be_clickable((By.ID, "idSIButton9"))
            )
            next_btn.click()
            self._log("Clicked Next")

            time.sleep(self.WAIT_PAGE_TRANSITION)
            self._screenshot("login_02_after_email")
            return True

        except Exception as exc:
            self._log(f"Email entry failed: {exc}", "error")
            self._screenshot("login_error_email")
            return False

    def _enter_password(self, password: str) -> bool:
        self._log("Entering password...")

        try:
            wait = WebDriverWait(self.driver, self.WAIT_ELEMENT)
            password_input = wait.until(
                EC.presence_of_element_located((By.ID, "i0118"))
            )
            password_input.clear()
            password_input.send_keys(password)
            self._log("Password entered")

            time.sleep(self.WAIT_SHORT)

            signin_btn = wait.until(
                EC.element_to_be_clickable((By.ID, "idSIButton9"))
            )
            signin_btn.click()
            self._log("Clicked Sign in")

            time.sleep(self.WAIT_PAGE_TRANSITION)
            self._screenshot("login_03_after_password")
            return True

        except Exception as exc:
            self._log(f"Password entry failed: {exc}", "error")
            self._screenshot("login_error_password")
            return False

    def _handle_post_password_flow(self, totp_secret: str) -> bool:
        self._log("Handling post-password flow...")

        mfa_done = False
        stay_done = False
        no_mfa_needed = False

        for round_num in range(6):
            self._log(f"Post-password check round {round_num + 1}...")
            time.sleep(self.WAIT_PAGE_TRANSITION)

            self._screenshot(f"login_flow_round_{round_num + 1}")

            current_url = self.driver.current_url
            page_source = self.driver.page_source.lower()

            if "entra.microsoft.com" in current_url and "login" not in current_url.lower():
                if "#" in current_url or "home" in current_url or "view" in current_url:
                    self._log("Reached Entra portal - login complete")
                    return True

            if "portal.azure.com" in current_url and "login" not in current_url.lower():
                self._log("Reached Azure portal - login complete")
                return True

            if not mfa_done and not no_mfa_needed:
                try:
                    mfa_input = self.driver.find_element(By.ID, "idTxtBx_SAOTCC_OTC")
                    self._log("MFA code input found")

                    if totp_secret:
                        totp = pyotp.TOTP(totp_secret)
                        code = totp.now()
                        self._log("Generated TOTP code")

                        mfa_input.clear()
                        mfa_input.send_keys(code)

                        time.sleep(self.WAIT_SHORT)

                        verify_btn = self.driver.find_element(By.ID, "idSubmit_SAOTCC_Continue")
                        verify_btn.click()
                        self._log("Clicked Verify")

                        mfa_done = True
                        time.sleep(self.WAIT_PAGE_TRANSITION)
                        continue

                    self._log("MFA required but no TOTP secret provided", "error")
                    return False

                except NoSuchElementException:
                    pass

            if not stay_done:
                if "stay signed in" in page_source or "keep me signed in" in page_source:
                    self._log("Stay signed in prompt found")
                    try:
                        yes_btn = self.driver.find_element(By.ID, "idSIButton9")
                        yes_btn.click()
                        self._log("Clicked Yes (Stay signed in)")

                        stay_done = True
                        time.sleep(self.WAIT_PAGE_TRANSITION)
                        continue

                    except NoSuchElementException:
                        try:
                            no_btn = self.driver.find_element(By.ID, "idBtn_Back")
                            no_btn.click()
                            self._log("Clicked No (Stay signed in)")

                            stay_done = True
                            time.sleep(self.WAIT_PAGE_TRANSITION)
                            continue
                        except Exception:
                            pass

            if not mfa_done and not no_mfa_needed:
                if (
                    "enter code" in page_source
                    or "enter the code" in page_source
                    or "verification code" in page_source
                ):
                    self._log("MFA page detected via text, waiting for input...")
                    time.sleep(self.WAIT_AFTER_ACTION)
                    continue

            if "password" in page_source and ("incorrect" in page_source or "wrong" in page_source):
                self._log("Password incorrect", "error")
                return False

            if "account has been locked" in page_source or "account is locked" in page_source:
                self._log("Account locked", "error")
                return False

            if mfa_done or stay_done or round_num >= 2:
                self._log("Flow items handled or skipped, checking if complete...")
                if not mfa_done and round_num >= 2:
                    no_mfa_needed = True
                    self._log("No MFA prompt detected - Security Defaults likely disabled")

        time.sleep(self.WAIT_AFTER_ACTION)
        current_url = self.driver.current_url
        self._log(f"Final URL: {current_url}")

        return "entra.microsoft.com" in current_url or "portal.azure" in current_url

    def _click_element_reliably(self, element, description: str) -> bool:
        try:
            try:
                element.click()
                self._log(f"Clicked {description} (standard)")
                return True
            except Exception as exc:
                self._log(f"Standard click failed: {exc}", "warning")

            try:
                self.driver.execute_script("arguments[0].click();", element)
                self._log(f"Clicked {description} (JavaScript)")
                return True
            except Exception as exc:
                self._log(f"JavaScript click failed: {exc}", "warning")

            try:
                from selenium.webdriver.common.action_chains import ActionChains

                actions = ActionChains(self.driver)
                actions.move_to_element(element).click().perform()
                self._log(f"Clicked {description} (ActionChains)")
                return True
            except Exception as exc:
                self._log(f"ActionChains click failed: {exc}", "warning")

            return False
        except Exception:
            return False

    def _grant_admin_consent(
        self,
        app_client_id: str,
        tenant_domain: str,
        admin_email: str,
        admin_password: str,
        totp_secret: str,
    ) -> bool:
        self._log("Navigating to admin consent page...")

        consent_url = f"https://login.microsoftonline.com/{tenant_domain}/adminconsent?client_id={app_client_id}"
        self._log(f"URL: {consent_url}")

        self.driver.get(consent_url)
        time.sleep(self.WAIT_PAGE_TRANSITION)
        self._screenshot("consent_01_page")

        reauth_email_done = False
        reauth_password_done = False
        reauth_mfa_done = False
        account_picked = False

        for attempt in range(15):
            time.sleep(self.WAIT_AFTER_ACTION)
            page_source = self.driver.page_source.lower()
            current_url = self.driver.current_url.lower()

            self._screenshot(f"consent_attempt_{attempt + 1}")

            if app_client_id.lower() not in current_url and "adminconsent" not in current_url:
                if "admin_consent=true" in current_url:
                    self._log("Consent granted (redirect confirmed)")
                    return True
                if "error" in current_url or "callback" in current_url:
                    self._log("Redirected to callback - consent likely granted")
                    return True

            try:
                email_input = self.driver.find_element(By.ID, "i0116")
                if email_input.is_displayed() and not reauth_email_done:
                    pwd_visible = False
                    try:
                        pwd_input = self.driver.find_element(By.ID, "i0118")
                        pwd_visible = pwd_input.is_displayed()
                    except Exception:
                        pass

                    if not pwd_visible:
                        self._log("Re-authentication required - entering email...")
                        email_input.clear()
                        email_input.send_keys(admin_email)
                        time.sleep(self.WAIT_SHORT)

                        next_btn = self.driver.find_element(By.ID, "idSIButton9")
                        self._click_element_reliably(next_btn, "Next (reauth email)")
                        reauth_email_done = True
                        time.sleep(self.WAIT_PAGE_TRANSITION)
                        continue
            except NoSuchElementException:
                pass

            try:
                pwd_input = self.driver.find_element(By.ID, "i0118")
                if pwd_input.is_displayed() and not reauth_password_done:
                    self._log("Re-authentication - entering password...")
                    pwd_input.clear()
                    pwd_input.send_keys(admin_password)
                    time.sleep(self.WAIT_SHORT)

                    signin_btn = self.driver.find_element(By.ID, "idSIButton9")
                    self._click_element_reliably(signin_btn, "Sign in (reauth)")
                    reauth_password_done = True
                    time.sleep(self.WAIT_PAGE_TRANSITION)
                    continue
            except NoSuchElementException:
                pass

            try:
                mfa_input = self.driver.find_element(By.ID, "idTxtBx_SAOTCC_OTC")
                if mfa_input.is_displayed() and not reauth_mfa_done:
                    if totp_secret:
                        self._log("Re-authentication - entering MFA code...")
                        totp = pyotp.TOTP(totp_secret)
                        code = totp.now()
                        mfa_input.clear()
                        mfa_input.send_keys(code)
                        time.sleep(self.WAIT_SHORT)

                        verify_btn = self.driver.find_element(By.ID, "idSubmit_SAOTCC_Continue")
                        self._click_element_reliably(verify_btn, "Verify (reauth MFA)")
                        reauth_mfa_done = True
                        time.sleep(self.WAIT_PAGE_TRANSITION)
                        continue

                    self._log("MFA required but no TOTP secret", "error")
                    return False
            except NoSuchElementException:
                pass

            if "stay signed in" in page_source:
                try:
                    yes_btn = self.driver.find_element(By.ID, "idSIButton9")
                    if yes_btn.is_displayed():
                        self._click_element_reliably(yes_btn, "Yes (stay signed in)")
                        time.sleep(self.WAIT_PAGE_TRANSITION)
                        continue
                except Exception:
                    pass

            if (
                "pick an account" in page_source
                or "choose an account" in page_source
            ) and not account_picked:
                self._log("Pick an account screen detected")
                clicked = False

                try:
                    signed_in_elements = self.driver.find_elements(
                        By.XPATH,
                        "//*[contains(text(), 'Signed in')]",
                    )
                    for elem in signed_in_elements:
                        try:
                            parent = elem.find_element(
                                By.XPATH,
                                "./ancestor::div[@role='button' or contains(@class, 'tile') or contains(@class, 'row')]",
                            )
                            if parent and parent.is_displayed():
                                clicked = self._click_element_reliably(parent, "signed-in account tile")
                                if clicked:
                                    break
                        except Exception:
                            pass
                except Exception:
                    pass

                if not clicked:
                    try:
                        domain_elements = self.driver.find_elements(
                            By.XPATH,
                            f"//*[contains(text(), '{tenant_domain}')]",
                        )
                        for elem in domain_elements:
                            clicked = self._click_element_reliably(elem, "domain element")
                            if clicked:
                                break
                    except Exception:
                        pass

                if not clicked:
                    try:
                        all_divs = self.driver.find_elements(By.TAG_NAME, "div")
                        for div in all_divs:
                            try:
                                if (
                                    div.is_displayed()
                                    and "@" in div.text
                                    and "onmicrosoft.com" in div.text.lower()
                                    and div.size["height"] > 20
                                ):
                                    clicked = self._click_element_reliably(div, "account div")
                                    if clicked:
                                        break
                            except Exception:
                                continue
                    except Exception:
                        pass

                if clicked:
                    account_picked = True
                    time.sleep(self.WAIT_PAGE_TRANSITION)
                    continue

                self._log("Could not click account tile", "warning")

            if "permissions requested" in page_source or (
                "accept" in page_source and "cancel" in page_source
            ):
                self._log("Consent page loaded, clicking Accept...")

                accept_found = False
                accept_selectors = [
                    (By.ID, "idSIButton9"),
                    (By.XPATH, "//input[@value='Accept']"),
                    (By.XPATH, "//button[contains(text(), 'Accept')]"),
                    (By.XPATH, "//input[@type='submit']"),
                ]

                for selector_type, selector in accept_selectors:
                    try:
                        btn = self.driver.find_element(selector_type, selector)
                        if btn.is_displayed():
                            accept_found = self._click_element_reliably(btn, "Accept button")
                            if accept_found:
                                time.sleep(self.WAIT_PAGE_TRANSITION)
                                break
                    except Exception:
                        continue

                if accept_found:
                    self._screenshot("consent_after_accept")
                    self._log("CONSENT GRANTED")
                    return True

            if "need admin approval" in page_source:
                self._log("Need admin approval - not admin?", "error")
                return False

        final_url = self.driver.current_url
        if "admin_consent=true" in final_url:
            self._log("Consent confirmed via URL")
            return True

        self._log("Consent flow did not complete", "error")
        return False

    def _patch_scopes_via_graph(self, tenant_domain: str, app_client_id: str) -> bool:
        self._log("Patching scopes via Graph API...")

        token = self._get_graph_token_device_code(tenant_domain)
        if not token:
            self._log("Could not get Graph token - scope patching skipped", "warning")
            return False

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            self._log("Finding app service principal...")
            resp = requests.get(
                f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{app_client_id}'",
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            sps = resp.json().get("value", [])

            if not sps:
                self._log("App not registered in tenant", "warning")
                return False

            app_sp_id = sps[0]["id"]
            self._log(f"App SP ID: {app_sp_id}")

            resp = requests.get(
                f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{GRAPH_RESOURCE_APP_ID}'",
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            graph_sps = resp.json().get("value", [])

            if not graph_sps:
                self._log("Microsoft Graph SP not found", "error")
                return False

            graph_sp_id = graph_sps[0]["id"]

            resp = requests.get(
                f"https://graph.microsoft.com/v1.0/oauth2PermissionGrants?$filter=clientId eq '{app_sp_id}'",
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            grants = resp.json().get("value", [])

            existing_grant = None
            for grant in grants:
                if grant.get("consentType") == "AllPrincipals" and grant.get("resourceId") == graph_sp_id:
                    existing_grant = grant
                    break

            if existing_grant:
                current_scopes = existing_grant.get("scope", "")
                self._log(f"Current scopes: {current_scopes}")

                if all(s in current_scopes for s in ["IMAP.AccessAsUser.All", "SMTP.Send", "Mail.ReadWrite"]):
                    self._log("Scopes already correct")
                    return True

                self._log("Patching scopes...")
                resp = requests.patch(
                    f"https://graph.microsoft.com/v1.0/oauth2PermissionGrants/{existing_grant['id']}",
                    headers=headers,
                    json={"scope": FULL_GRAPH_SCOPES},
                    timeout=30,
                )
                if resp.status_code in [200, 204]:
                    self._log("Scopes patched successfully")
                    return True
                self._log(f"Patch failed: {resp.text}", "error")
                return False

            self._log("Creating new AllPrincipals grant...")
            resp = requests.post(
                "https://graph.microsoft.com/v1.0/oauth2PermissionGrants",
                headers=headers,
                json={
                    "clientId": app_sp_id,
                    "consentType": "AllPrincipals",
                    "resourceId": graph_sp_id,
                    "scope": FULL_GRAPH_SCOPES,
                },
                timeout=30,
            )
            if resp.status_code in [200, 201]:
                self._log("Grant created successfully")
                return True

            self._log(f"Create failed: {resp.text}", "error")
            return False

        except requests.exceptions.RequestException as exc:
            self._log(f"Graph API request failed: {exc}", "error")
            return False
        except Exception as exc:
            self._log(f"Unexpected error: {exc}", "error")
            return False

    def _get_graph_token_device_code(self, tenant_domain: str) -> Optional[str]:
        try:
            client_id = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"  # Azure CLI

            resp = requests.post(
                f"https://login.microsoftonline.com/{tenant_domain}/oauth2/v2.0/devicecode",
                data={
                    "client_id": client_id,
                    "scope": "https://graph.microsoft.com/.default",
                },
                timeout=30,
            )
            resp.raise_for_status()
            device_data = resp.json()

            user_code = device_data.get("user_code")
            device_code = device_data.get("device_code")
            verification_uri = device_data.get("verification_uri")

            self._log(f"Device code: {user_code}")
            self._log("Completing device code auth in browser...")

            self.driver.get(verification_uri)
            time.sleep(self.WAIT_PAGE_TRANSITION)

            try:
                wait = WebDriverWait(self.driver, self.WAIT_ELEMENT)
                code_input = wait.until(
                    EC.presence_of_element_located((By.ID, "otc"))
                )
                code_input.clear()
                code_input.send_keys(user_code)
                time.sleep(self.WAIT_SHORT)

                next_btn = self.driver.find_element(By.ID, "idSIButton9")
                next_btn.click()
                time.sleep(self.WAIT_PAGE_TRANSITION)

                for _ in range(5):
                    page_source = self.driver.page_source.lower()

                    if "you have signed in" in page_source or "successfully" in page_source:
                        self._log("Device code approved")
                        break

                    try:
                        continue_btn = self.driver.find_element(By.ID, "idSIButton9")
                        if continue_btn.is_displayed():
                            continue_btn.click()
                            time.sleep(self.WAIT_AFTER_ACTION)
                    except Exception:
                        pass

                    time.sleep(self.WAIT_AFTER_ACTION)

            except Exception as exc:
                self._log(f"Device code browser flow failed: {exc}", "warning")

            for _ in range(30):
                time.sleep(2)

                resp = requests.post(
                    f"https://login.microsoftonline.com/{tenant_domain}/oauth2/v2.0/token",
                    data={
                        "client_id": client_id,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                    },
                    timeout=30,
                )

                if resp.status_code == 200:
                    token_data = resp.json()
                    return token_data.get("access_token")
                if resp.status_code == 400:
                    error = resp.json().get("error")
                    if error == "authorization_pending":
                        continue
                    if error == "expired_token":
                        self._log("Device code expired", "error")
                        return None
                    self._log(f"Token error: {error}", "error")
                    return None

            self._log("Device code flow timed out", "error")
            return None

        except Exception as exc:
            self._log(f"Device code flow failed: {exc}", "error")
            return None

    def grant_consent_for_tenant(
        self,
        creds: TenantCredentials,
        app_client_id: str,
        app_name: str,
        patch_scopes: bool = True,
    ) -> Dict:
        result = {
            "success": False,
            "error": None,
            "domain": creds.domain,
            "scopes_patched": False,
        }

        try:
            if not self._setup_driver():
                result["error"] = "Failed to setup browser"
                return result

            if not self._login(creds):
                result["error"] = "Login failed"
                self._cleanup()
                return result

            tenant_domain = creds.admin_email.split("@")[1]

            consent_granted = self._grant_admin_consent(
                app_client_id,
                tenant_domain,
                creds.admin_email,
                creds.admin_password,
                creds.totp_secret,
            )

            if consent_granted:
                result["success"] = True
                self._log(f"Consent granted for {app_name}")

                if patch_scopes:
                    self._log("Attempting to patch scopes with full permissions...")
                    if self._patch_scopes_via_graph(tenant_domain, app_client_id):
                        result["scopes_patched"] = True
                        self._log("Full scopes patched successfully")
                    else:
                        self._log(
                            "Scope patching failed - consent is granted but may have limited scopes",
                            "warning",
                        )
            else:
                result["error"] = "Consent flow failed"
                self._log("FAILED: Could not grant consent", "error")

        except Exception as exc:
            result["error"] = str(exc)
            self._log(f"Exception: {exc}", "error")
            self._screenshot("error_exception")

        finally:
            self._cleanup()

        return result

    def grant_consent_for_batch(
        self,
        tenants: List[TenantCredentials],
        app_client_id: str,
        app_name: str,
        patch_scopes: bool = True,
    ) -> Dict:
        results = []
        successful = 0
        failed = 0
        scopes_patched = 0

        for i, creds in enumerate(tenants):
            self._log(f"Processing {i + 1}/{len(tenants)}: {creds.domain}")
            result = self.grant_consent_for_tenant(creds, app_client_id, app_name, patch_scopes)
            results.append(result)

            if result["success"]:
                successful += 1
                if result.get("scopes_patched"):
                    scopes_patched += 1
            else:
                failed += 1

            if i < len(tenants) - 1:
                time.sleep(5)

        return {
            "total": len(tenants),
            "successful": successful,
            "failed": failed,
            "scopes_patched": scopes_patched,
            "results": results,
        }


def get_plusvibe_client_id() -> str:
    config = get_sequencer_config("plusvibe")
    return config.get("client_id")
