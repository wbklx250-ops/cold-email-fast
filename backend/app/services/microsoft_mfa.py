"""Delegated Microsoft tokens and Exchange readback using the saved authenticator."""
import json
import queue
import re
import subprocess
import threading
import time
from uuid import UUID

import msal
from sqlalchemy import func, select

from app.db.session import SyncSessionLocal
from app.models import Tenant, ReadinessCheck

_apps = {}
_verified_apps = {}
_locks = {}
_guard = threading.Lock()


class MicrosoftAuthenticationError(RuntimeError):
    pass


def _drive_signin(driver, account, password, seed, device_code, completed):
    """Advance only visible Microsoft forms until the token endpoint confirms sign-in."""
    import pyotp
    from urllib.parse import urlsplit
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import StaleElementReferenceException, NoSuchElementException

    def visible(selector):
        for field in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if field.is_displayed() and field.is_enabled() and field.get_attribute('readonly') not in ('true',True):
                    return field
            except StaleElementReferenceException:
                continue
        return None

    def submit(field):
        button = visible('#idSIButton9, #idSubmit_SAOTCC_Continue, button[type=submit], input[type=submit]')
        try:
            button.click() if button else field.send_keys(Keys.RETURN)
        except StaleElementReferenceException:
            pass

    def fill(field,value):
        try:
            field.clear(); field.send_keys(value); submit(field)
            return True
        except StaleElementReferenceException:
            return False

    driver.get('https://microsoft.com/devicelogin')
    field = WebDriverWait(driver, 20).until(lambda _:visible('#otc, input[name=otc]'))
    field.send_keys(device_code)
    submit(field)
    deadline = time.monotonic() + 120
    last_code_window = None
    attempts = 0
    while time.monotonic() < deadline:
        if completed():
            return
        host = urlsplit(driver.current_url).hostname
        if host not in {'login.microsoftonline.com','login.microsoft.com','login.live.com','microsoft.com'}:
            raise MicrosoftAuthenticationError('Device authentication left the expected Microsoft sign-in site')
        try:
            body = driver.find_element(By.TAG_NAME,'body').text.lower()
        except (StaleElementReferenceException,NoSuchElementException):
            time.sleep(.5)
            continue
        if any(message in body for message in ['password is incorrect','account or password is incorrect','account has been locked','too many times']):
            raise MicrosoftAuthenticationError('Microsoft rejected the administrator sign-in')
        username = visible('input[name=loginfmt], input[type=email]')
        password_field = visible('input[name=passwd], input[type=password]')
        code = visible('#idTxtBx_SAOTCC_OTC, input[name=otc]:not(#otc), input[autocomplete=one-time-code], input[type=tel][maxlength="6"]')
        if username:
            fill(username,account)
        elif password_field:
            fill(password_field,password)
        elif code:
            if not seed:
                raise MicrosoftAuthenticationError('The existing Microsoft MFA challenge requires a saved authenticator key')
            window = int(time.time() // 30)
            if window != last_code_window and attempts < 3 and time.time() % 30 < 26:
                if fill(code,pyotp.TOTP(seed).now()):
                    last_code_window = window; attempts += 1
        elif 'approve sign-in on another device' in body:
            if account.lower() not in body or not any(name in body for name in (
                    'microsoft exchange rest api based powershell','microsoft graph command line tools')):
                raise MicrosoftAuthenticationError('The device approval does not match the requested administrator and Microsoft application')
            approve = visible('#idBtn_Approve')
            if approve:
                approve.click()
        elif 'pick an account' in body or 'choose an account' in body:
            for tile in driver.find_elements(By.CSS_SELECTOR,'[role=button], .table, .tile'):
                try:
                    if tile.is_displayed() and account.lower() in tile.text.lower():
                        tile.click(); break
                except StaleElementReferenceException:
                    continue
            else:
                for item in driver.find_elements(By.CSS_SELECTOR,'button, a, [role=button], .table'):
                    try:
                        if item.is_displayed() and 'use another account' in item.text.lower():
                            item.click(); break
                    except StaleElementReferenceException:
                        continue
        elif any(message in body for message in ['install microsoft authenticator','scan the qr code','more information required']):
            raise MicrosoftAuthenticationError('Microsoft requires an additional security registration')
        else:
            for button in driver.find_elements(By.CSS_SELECTOR,'button, input[type=submit]'):
                try:
                    label = (button.text or button.get_attribute('value') or '').strip().lower()
                    if button.is_displayed() and button.is_enabled() and label in {'yes','continue','accept','next','sign in'}:
                        button.click(); break
                except StaleElementReferenceException:
                    continue
        time.sleep(1)
    raise MicrosoftAuthenticationError('Microsoft device sign-in did not complete within two minutes')


def _identity(admin_email, totp_secret):
    email = admin_email.strip().lower()
    with SyncSessionLocal() as db:
        records = db.scalars(select(Tenant).where(func.lower(Tenant.admin_email) == email)).all()
        check = db.scalar(select(ReadinessCheck).where(func.lower(ReadinessCheck.admin_email) == email))
        expected = str(check.tenant_id) if check else None
        if not expected:
            for record in records:
                try:
                    expected = str(UUID(record.microsoft_tenant_id))
                    break
                except ValueError:
                    pass
        authority = expected or next((r.onmicrosoft_domain for r in records if r.onmicrosoft_domain), None)
        if not authority:
            raise MicrosoftAuthenticationError('No verified tenant identity is available for Microsoft authentication')
        if not totp_secret:
            seeds = {r.totp_secret for r in records if r.totp_secret}
            if len(seeds) == 1:
                totp_secret = seeds.pop()
            elif len(seeds) > 1:
                raise MicrosoftAuthenticationError('Conflicting saved authenticator keys require review')
    return authority, expected, totp_secret


def _validate_identity(result, account, expected_tenant):
    claims = result.get('id_token_claims') or {}
    username = claims.get('preferred_username') or claims.get('upn') or ''
    if username.lower() != account.lower() or (expected_tenant and claims.get('tid','').lower() != expected_tenant.lower()):
        raise MicrosoftAuthenticationError('Microsoft authenticated a different administrator or tenant')


def get_token(admin_email, admin_password, totp_secret=None, *, resource='exchange', driver=None):
    """Keep delegated tokens in server memory; never persist or log their values."""
    authority, expected, seed = _identity(admin_email, totp_secret)
    if resource == 'exchange':
        client_id = 'fb78d390-0c51-40cd-8e17-fdbfab77341b'
        scopes = ['https://outlook.office365.com/.default']
    elif resource == 'graph':
        client_id = '14d82eec-204b-4c2f-b7e8-296a70dab67e'
        scopes = ['https://graph.microsoft.com/Domain.Read.All', 'https://graph.microsoft.com/User.Read']
    else:
        raise ValueError('Unsupported Microsoft resource')
    key = (authority, admin_email.lower(), resource)
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        app = _apps.get(key)
        if app is None:
            app = msal.PublicClientApplication(client_id, authority='https://login.microsoftonline.com/' + authority)
            _apps[key] = app
        accounts = app.get_accounts(username=admin_email)
        result = app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
        if result and result.get('access_token'):
            if result.get('id_token_claims'):
                _validate_identity(result, admin_email, expected)
                _verified_apps[key] = (app, result['id_token_claims'].copy())
                return result['access_token']
            # MSAL's access-token cache hits deliberately omit ID-token claims.
            # Reuse only the same in-memory client whose device/refresh response
            # we already validated, and check its cached account on every use.
            verified = _verified_apps.get(key)
            if verified and verified[0] is app:
                claims = verified[1]
                _validate_identity({'id_token_claims':claims}, admin_email, expected)
                account = accounts[0]
                if (str(account.get('username','')).lower() != admin_email.lower()
                        or str(account.get('realm','')).lower() != str(claims.get('tid','')).lower()):
                    raise MicrosoftAuthenticationError('Microsoft cached a different administrator or tenant')
                return result['access_token']
            # A cache without a previously validated identity must authenticate
            # again; never infer identity from the access token's mere presence.
        flow = app.initiate_device_flow(scopes=scopes)
        if not flow.get('user_code'):
            raise MicrosoftAuthenticationError('Microsoft could not start delegated device authentication')
        answers = queue.Queue()
        def receive():
            try:
                answers.put(app.acquire_token_by_device_flow(flow))
            except Exception:
                answers.put({})
        threading.Thread(target=receive, daemon=True).start()
        original_window = None
        original_implicit_wait = None
        own_driver = driver is None
        try:
            if own_driver:
                from app.services.selenium.admin_portal import _create_step6_admin_center_driver
                driver = _create_step6_admin_center_driver(headless=True)
            else:
                original_window = driver.current_window_handle
                original_implicit_wait = driver.timeouts.implicit_wait
                driver.switch_to.new_window('tab')
            driver.implicitly_wait(0)
            _drive_signin(driver, admin_email, admin_password, seed, flow['user_code'], lambda:not answers.empty())
            try:
                result = answers.get(timeout=35)
            except queue.Empty:
                raise MicrosoftAuthenticationError('Microsoft did not issue the delegated token in time') from None
            if not result or not result.get('access_token'):
                code = (result or {}).get('error','unknown_error')
                code = code if re.fullmatch(r'[a-z_]+',code) else 'unknown_error'
                raise MicrosoftAuthenticationError(f'Microsoft rejected delegated authentication ({code})')
            _validate_identity(result, admin_email, expected)
            _verified_apps[key] = (app, result['id_token_claims'].copy())
            return result['access_token']
        finally:
            # Cancel only this device flow if the browser could not finish it.
            flow['expires_at'] = 0
            if driver:
                if own_driver:
                    driver.quit()
                elif original_window:
                    try:
                        driver.close()
                    finally:
                        driver.switch_to.window(original_window)
                        if isinstance(original_implicit_wait,(int,float)):
                            driver.implicitly_wait(original_implicit_wait)


def read_dkim(admin_email, admin_password, domain, totp_secret=None, *, create=False, enable=False, driver=None):
    if not re.fullmatch(r'[A-Za-z0-9.-]+', domain):
        raise ValueError('Invalid domain name')
    token = get_token(admin_email, admin_password, totp_secret, driver=driver)
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    script = r'''
$ErrorActionPreference='Stop'
$result=@{success=$false;exists=$false;enabled=$false;selector1=$null;selector2=$null;accepted_domain_exists=$false;error=$null}
try {
 Import-Module ExchangeOnlineManagement -ErrorAction Stop
 Connect-ExchangeOnline -AccessToken __TOKEN__ -UserPrincipalName __ADMIN__ -ShowBanner:$false -ErrorAction Stop
 $identity=Get-ConnectionInformation | Where-Object {$_.UserPrincipalName -ieq __ADMIN__} | Select-Object -First 1
 if (-not $identity) {throw 'Exchange connection identity did not match the requested administrator'}
 $result.accepted_domain_exists=[bool](Get-AcceptedDomain -Identity __DOMAIN__ -ErrorAction SilentlyContinue)
 $config=Get-DkimSigningConfig -Identity __DOMAIN__ -ErrorAction SilentlyContinue
 if (__CREATE__ -and (-not $config -or -not $config.Selector1CNAME -or -not $config.Selector2CNAME)) {
  try {New-DkimSigningConfig -DomainName __DOMAIN__ -Enabled:$false -ErrorAction Stop | Out-Null}
  catch {if ($_.Exception.Message -notmatch 'already|exist') {throw}}
  $config=Get-DkimSigningConfig -Identity __DOMAIN__ -ErrorAction Stop
 }
 if ($config -and __ENABLE__ -and -not $config.Enabled) {
  Set-DkimSigningConfig -Identity __DOMAIN__ -Enabled:$true -ErrorAction Stop
  $config=Get-DkimSigningConfig -Identity __DOMAIN__ -ErrorAction Stop
 }
 if ($config) {$result.exists=$true;$result.enabled=[bool]$config.Enabled;$result.selector1=$config.Selector1CNAME;$result.selector2=$config.Selector2CNAME}
 $result.success=$true
} catch {$result.error=$_.Exception.Message}
finally {Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue}
Write-Output 'READBACK_BEGIN'
$result | ConvertTo-Json -Compress
Write-Output 'READBACK_END'
'''.replace('__TOKEN__',quote(token)).replace('__ADMIN__',quote(admin_email)).replace('__DOMAIN__',quote(domain)).replace('__CREATE__','$true' if create else '$false').replace('__ENABLE__','$true' if enable else '$false')
    result = subprocess.run(['pwsh','-NoProfile','-NonInteractive','-Command',
        '& ([ScriptBlock]::Create([Console]::In.ReadToEnd()))'], input=script,
        capture_output=True,text=True,timeout=100)
    output = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', result.stdout)
    match = re.search(r'READBACK_BEGIN\s*(\{.*?\})\s*READBACK_END', output, re.S)
    if not match:
        raise MicrosoftAuthenticationError('Exchange did not return a valid DKIM readback')
    data = json.loads(match[1])
    if data.get('error'):
        for secret in (token,admin_password,totp_secret):
            if secret:data['error'] = data['error'].replace(secret,'[redacted]')
        data['error'] = data['error'][:500]
    data['ok'] = bool(data.get('success') and data.get('enabled'))
    return data
