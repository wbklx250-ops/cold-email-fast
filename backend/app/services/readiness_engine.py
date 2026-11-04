"""Isolated Microsoft 365 readiness checks. Never import the migration pipeline."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import html
import json
import os
from pathlib import Path
import queue
import re
import requests
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse, parse_qs
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent
RECIPIENT = "wbklx250@gmail.com"
TERMINAL = {"no_ndr_observed", "blocked_5.7.705", "blocked_5.7.750", "other_ndr", "skipped_by_user"}
EXCHANGE_PLANS = {"EXCHANGE_S_STANDARD", "EXCHANGE_S_ENTERPRISE", "EXCHANGE_S_DESKLESS", "EXCHANGE_S_STANDARD_MIDMARKET"}
HUMAN_LOCK = threading.Lock()
INTERACTIVE_SECONDS = 180
REPORT_FIELDS = ["tenant_id", "tenant_number", "admin", "sender", "recipient", "status", "stage",
                 "subject", "sent_at", "monitor_started_at", "monitor_completed_at", "monitor_seconds",
                 "ndr_code", "ndr_subject", "ndr_id", "sku", "trial_started", "renewal_setting",
                 "updated_at", "detail"]


class NeedsHuman(RuntimeError):
    pass


class UserSkipped(NeedsHuman):
    pass


class TrialUnavailable(NeedsHuman):
    pass


class GraphError(RuntimeError):
    pass


class MFASetupError(GraphError):
    """An enrollment automation failure, never a request for manual setup."""
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_tenants(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"TargetTenantID", "AdminUsername", "AdminPassword"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("CSV must contain TargetTenantID, AdminUsername, and AdminPassword.")
        tenants, ids, accounts = [], set(), set()
        for line, raw in enumerate(reader, 2):
            tenant_id = str(UUID(raw["TargetTenantID"].strip()))
            admin = raw["AdminUsername"].strip().lower()
            password = raw["AdminPassword"]  # Preserve spaces in passwords.
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", admin) or not password:
                raise ValueError(f"Missing or invalid credentials on CSV line {line}.")
            if tenant_id in ids or admin in accounts:
                raise ValueError(f"Duplicate tenant or account on CSV line {line}.")
            ids.add(tenant_id)
            accounts.add(admin)
            # Notes and every other attached-document field are data, never commands.
            tenants.append({"id": tenant_id, "number": raw.get("Tenant Number", "").strip(),
                            "admin": admin, "password": password})
    if not tenants:
        raise ValueError("CSV contains no tenants.")
    return tenants


def exchange_skus(skus):
    return [s for s in skus if s.get("capabilityStatus") == "Enabled"
            and any(p.get("servicePlanName") in EXCHANGE_PLANS and p.get("provisioningStatus") == "Success"
                    for p in s.get("servicePlans", []))]


def choose_sku(skus, assigned):
    enabled = exchange_skus(skus)
    for sku in enabled:
        exchange_ids = {p["servicePlanId"] for p in sku["servicePlans"] if p.get("servicePlanName") in EXCHANGE_PLANS}
        if any(a["skuId"] == sku["skuId"] and exchange_ids - set(a.get("disabledPlans", [])) for a in assigned):
            return sku
    assigned_ids = {a["skuId"] for a in assigned}
    # Do not change disabled service plans on existing assignments.
    available = [s for s in enabled if s["skuId"] not in assigned_ids and s.get("prepaidUnits", {}).get("enabled", 0) > s.get("consumedUnits", 0)]
    return sorted(available, key=lambda s: (s.get("skuPartNumber", "") != "O365_BUSINESS_ESSENTIALS", s.get("skuPartNumber", "")))[0] if available else None


def duration_days(text):
    text = text.strip().lower()
    if re.search(r"\b(monthly|month-to-month)\b", text) and not re.search(r"annual|year", text):
        return 30
    if re.search(r"\b(annual|annually|yearly)\b", text):
        return 365
    match = re.search(r"(\d+)\s*[- ]?\s*(day|week|month|year)s?\b", text)
    if match:
        return int(match[1]) * {"day": 1, "week": 7, "month": 30, "year": 365}[match[2]]
    return None


def shortest_option(options):
    choices = [(duration_days(label), i) for i, label in enumerate(options)]
    choices = [(days, i) for days, i in choices if days is not None]
    return min(choices)[1] if choices else None


def classify_ndr(messages, marker):
    """Only correlate actual NDRs with this probe, never a random old bounce."""
    for msg in messages:
        body = msg.get("body", {}).get("content", "") or ""
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
        subject = msg.get("subject", "") or ""
        if marker.lower() not in (subject + "\n" + body).lower():
            continue
        headers = {h["name"].lower(): h["value"].lower() for h in msg.get("internetMessageHeaders", [])}
        evidence = subject + "\n" + body
        is_ndr = (bool(re.search(r"undeliverable|delivery (?:status notification|has failed|failure)|non.?delivery|couldn.t be delivered|could not be delivered", evidence, re.I))
                  or "report-type=delivery-status" in headers.get("content-type", "")
                  or bool(re.search(r"final-recipient:|diagnostic-code:|action:\s*failed", body, re.I)))
        if not is_ndr:
            continue
        code_match = re.search(r"(?<!\d)([245]\.\d{1,3}\.\d{1,3})(?!\d)", evidence)
        special = re.search(r"(?<!\d)(5\.7\.(?:705|750))(?!\d)", evidence)
        code = special[1] if special else (code_match[1] if code_match else "")
        return {"status": f"blocked_{code}" if code in {"5.7.705", "5.7.750"} else "other_ndr",
                "ndr_code": code, "ndr_subject": subject, "ndr_id": msg.get("id", "")}
    return None


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.lock = threading.RLock()
        skips_path = self.directory / 'operator-skips.json'
        self.user_skips = set(json.loads(skips_path.read_text(encoding='utf-8-sig'))) if skips_path.exists() else set()
        for skipped_id in self.user_skips:
            if skipped_id in self.state:
                self.state[skipped_id].update(status='skipped_by_user', stage='complete', detail='The operator explicitly chose to skip this tenant.')

    def update(self, tenant, **changes):
        with self.lock:
            return self._update(tenant, **changes)

    def _update(self, tenant, **changes):
        row = self.state.setdefault(tenant["id"], {"tenant_id": tenant["id"], "tenant_number": tenant["number"],
                                               "admin": tenant["admin"], "recipient": RECIPIENT,
                                               "renewal_setting": "unchanged"})
        old_progress = (row.get("stage"), row.get("status"))
        row.update(changes, updated_at=utcnow())
        if old_progress != (row.get("stage"), row.get("status")):
            print(f"Tenant {tenant['number'] or tenant['id'][:8]}: {row.get('stage', '')} ({row.get('status', '')})", flush=True)
        skips_path = self.directory / 'operator-skips.json'
        if skips_path.exists():
            for skipped_id in json.loads(skips_path.read_text(encoding='utf-8-sig')):
                if skipped_id in self.state:
                    self.state[skipped_id].update(status='skipped_by_user', stage='complete', detail='The operator explicitly chose to skip this tenant.')
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temp.replace(self.path)
        with (self.directory / "report.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, REPORT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for record in self.state.values():
                # Keep imported labels and remote subjects inert when opened in Excel.
                writer.writerow({k: ("'" + str(v) if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v)
                                 for k, v in record.items() if k in REPORT_FIELDS})
        return row


def protect_password(path, value=None):
    """Windows CurrentUser DPAPI; never save a generated mailbox password as text."""
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
    data = value.encode("utf-8") if value is not None else Path(path).read_bytes()
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if value is not None else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise RuntimeError("Windows credential protection failed.")
    try:
        result = ctypes.string_at(target.pbData, target.cbData)
        if value is not None:
            Path(path).write_bytes(result)
            return value
        return result.decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


class Browser:
    @property
    def mfa_stage(self):
        return getattr(self, '_mfa_stage', '')

    @mfa_stage.setter
    def mfa_stage(self, value):
        previous = getattr(self, '_mfa_stage', '')
        self._mfa_stage = value
        if value != previous and value and getattr(self, 'on_progress', None):
            self.on_progress(value)

    def __init__(self, playwright, directory, tenant, account=None, password=None):
        self.playwright, self.directory, self.tenant = playwright, Path(directory), tenant
        self.account = account or tenant["admin"]
        self.password = password if password is not None else tenant["password"]
        self.background = True
        self.context = None
        self.device_code = None
        self.last_password_submit = 0.0
        self.last_totp_submit = 0.0
        self.enrolling = False
        self.mfa_stage = ''
        self.totp_path = self.directory.parent / (self.directory.name.replace('-profile', '') + '-totp.dpapi')
        self.totp = protect_password(self.totp_path) if self.totp_path.exists() else None
        if self.totp:
            import pyotp
            try:
                pyotp.TOTP(self.totp).now()
            except Exception:
                self.totp_path.rename(self.totp_path.with_name(self.totp_path.name + '.invalid-' + uuid4().hex[:8]))
                self.totp = None
        self.open()

    def open(self, url=None):
        self.context = self.playwright.chromium.launch_persistent_context(
            str(self.directory.resolve()), channel="chrome", headless=False,
            viewport={"width": 1440, "height": 1000}, locale="en-US",
            args=["--disable-notifications", "--disable-background-timer-throttling",
                  "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows"])
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.use_page(self.page)
        self.context.on('page', self.use_page)
        # Minimize a real browser so MFA/card handoff preserves the live page,
        # including POST state and checkout dialogs. Restarting loses that state.
        if self.background:
            self.set_window_state('minimized')
        if url:
            self.page.goto(url, wait_until="domcontentloaded")

    def use_page(self, page):
        self.page = page
        page.set_default_timeout(2500)
        page.set_default_navigation_timeout(60000)

    def navigate(self, url):
        try:
            self.page.goto(url, wait_until='domcontentloaded')
        except Exception as exc:
            # Microsoft can complete the previous authentication redirect while
            # a navigation is starting. Continue the sign-in rather than abort.
            if 'is interrupted by another navigation' not in str(exc):
                raise

    def visible(self):
        self.background = False
        self.set_window_state('normal')
        self.page.bring_to_front()

    def set_window_state(self, state):
        session = self.context.new_cdp_session(self.page)
        try:
            window = session.send('Browser.getWindowForTarget')
            session.send('Browser.setWindowBounds', {'windowId': window['windowId'], 'bounds': {'windowState': state}})
        finally:
            session.detach()

    def begin_device_login(self, code):
        self.device_code = code
        self.page.goto("https://microsoft.com/devicelogin", wait_until="domcontentloaded")
        field = self.page.locator("input[name=otc], input#otc")
        field.first.wait_for(state="visible", timeout=30000)
        field.first.fill(code)
        submit = self.page.locator("#idSIButton9, button[type='submit'], input[type='submit']")
        try:
            submit.first.click()
        except Exception as exc:
            # The login form can redirect between filling and clicking. Let the
            # normal sign-in loop inspect the new page instead of failing auth.
            if type(exc).__name__ != 'TimeoutError' and 'Execution context was destroyed' not in str(exc):
                raise

    def human(self, message):
        if getattr(self, 'on_wait', None):
            self.on_wait(message)
        with HUMAN_LOCK:
            self._human(message)

    def _human(self, message):
        self.visible()
        # Native dialogs are safe from worker threads; Tk interpreters can abort
        # the entire parallel runner when garbage-collected on another thread.
        import ctypes
        from ctypes import wintypes
        title = f"Tenant readiness — {self.tenant['number'] or self.tenant['id'][:8]}"
        body = f"Account: {self.account}\n\n{message}\n\nClick OK after completing the browser step. Click Cancel to defer this tenant."
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        expired = {'value': False}
        def expire():
            handle = user32.FindWindowW(None, title)
            process_id = wintypes.DWORD()
            if handle:
                user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
                if process_id.value == os.getpid():
                    expired['value'] = True
                    user32.PostMessageW(handle, 0x0010, 0, 0)
        timer = threading.Timer(INTERACTIVE_SECONDS, expire)
        timer.daemon = True
        timer.start()
        try:
            result = user32.MessageBoxW(None, body, title, 0x1 | 0x40 | 0x10000 | 0x40000)
        finally:
            timer.cancel()
        if result == 2 and not expired['value']:
            raise UserSkipped('The operator explicitly skipped this tenant at the browser prompt.')
        if result == 0:
            raise RuntimeError('Windows could not display the interactive browser prompt.')
        if result != 1:
            raise NeedsHuman("Skipped at an interactive browser step; rerun to resume.")

    def click(self, pattern):
        for role in ("button", "link"):
            locator = self.page.get_by_role(role, name=re.compile(pattern, re.I))
            for i in range(min(locator.count(), 12)):
                item = locator.nth(i)
                if item.is_visible() and item.is_enabled():
                    try:
                        item.click()
                        return True
                    except Exception as exc:
                        if type(exc).__name__ != "TimeoutError":
                            raise
        return False

    def advance_login(self):
        """Only type credentials into Microsoft's known login hosts."""
        self.process_operator_action()
        if self.page.is_closed():
            remaining = [p for p in self.context.pages if not p.is_closed()]
            if not remaining:
                raise NeedsHuman("The sign-in browser was closed.")
            self.use_page(remaining[-1])
        host = urlparse(self.page.url).hostname or ""
        if host in {"login.microsoftonline.com", "login.microsoft.com", "login.live.com", "mysignins.microsoft.com", "myaccount.microsoft.com", "admin.cloud.microsoft", "admin.microsoft.com"}:
            if self.advance_security():
                return True
        if host not in {"login.microsoftonline.com", "login.microsoft.com", "login.live.com"}:
            return False
        text = self.page.locator('body').inner_text()
        if re.search(r"account or password is incorrect|password is incorrect|incorrect user ID or password|account has been locked|account doesn.t exist|tried to sign in too many times", text, re.I):
            raise GraphError("Microsoft rejected the supplied sign-in credentials or locked the account.")
        # Microsoft's password step retains a hidden/read-only loginfmt text
        # control. Never submit that as if it were the username-entry screen.
        for selector, value, password_step in [
            ("input[name=passwd], input[type=password]", self.password, True),
            ("input[type=email], input[name=loginfmt], input[autocomplete=username]", self.account, False),
        ]:
            for field in self.page.locator(selector).all():
                if not field.is_visible() or not field.is_editable():
                    continue
                # Microsoft's username screen includes a transparent, editable
                # password autofill field. Playwright considers opacity:0
                # visible, so checking visibility alone submits the wrong step.
                if not field.evaluate('''el => {
                    for (let node = el; node; node = node.parentElement) {
                        if (node.getAttribute('aria-hidden') === 'true' || node.inert ||
                            Number(getComputedStyle(node).opacity) === 0) return false;
                    }
                    return true;
                }'''):
                    continue
                if password_step:
                    if time.monotonic() - self.last_password_submit < 15:
                        return False
                    self.last_password_submit = time.monotonic()
                field.fill(value)
                submit = self.page.locator("#idSIButton9, button[type='submit'], input[type='submit'], button[data-testid='reskin-step-next-button']")
                if submit.count() and submit.first.is_visible():
                    submit.first.click()
                else:
                    field.press("Enter")
                return True
        # The requested Graph permissions are explicit in graph_bridge.ps1.
        if self.click(r"^(Accept|Yes|Continue)$"):
            return True
        text = self.page.locator('body').inner_text()
        if re.search(r"pick an account|choose an account", text, re.I):
            tile = self.page.get_by_text(re.compile(r'^\s*' + re.escape(self.account) + r'\s*$', re.I))
            if tile.count() and tile.first.is_visible():
                tile.first.click()
                return True
        return False

    def process_operator_action(self):
        """Local operator mailbox for inspecting a stalled Microsoft UI."""
        action_path = self.directory.parent / 'operator-action.json'
        if not action_path.exists():
            return
        action = json.loads(action_path.read_text(encoding='utf-8'))
        action_path.unlink()
        try:
            if action.get('op') == 'stop':
                raise SystemExit(0)
            elif action.get('op') == 'inspect':
                result = {'url': self.page.url.split('?')[0], 'body': self.page.locator(action.get('body_selector', 'body')).last.inner_text()[:action.get('limit', 5000)],
                    'elements': self.page.locator(action.get('selector', 'button, input, select, [role=dialog], [role=tab]')).evaluate_all("els=>els.filter(e=>e.offsetWidth||e.offsetHeight).map(e=>({tag:e.tagName,text:e.innerText?.slice(0,180),role:e.getAttribute('role'),label:e.getAttribute('aria-label'),type:e.type,checked:e.checked,disabled:e.disabled,placeholder:e.getAttribute('placeholder'),id:e.id}))")}
            else:
                locator = (self.page.get_by_role(action['role'], name=action['name'], exact=True) if 'role' in action
                           else self.page.locator(action['selector']))
                target = locator.nth(action.get('index', 0))
                if action['op'] == 'click':
                    target.click(timeout=5000)
                elif action['op'] == 'fill':
                    target.fill(action['text'])
                elif action['op'] == 'select':
                    target.select_option(label=action['label'])
                elif action['op'] == 'goto':
                    raise ValueError('Navigation is controlled by the runner.')
                else:
                    raise ValueError('Unsupported operator action.')
                result = {'ok': True}
        except Exception as exc:
            result = {'error': str(exc).split('Call log:', 1)[0]}
        def redact(value):
            if isinstance(value, dict):
                return {k: redact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value]
            if isinstance(value, str):
                for secret in [self.password, self.account, self.totp, self.device_code]:
                    if secret:
                        value = value.replace(secret, '[redacted]')
                value = re.sub(r'[\w.+-]+@[\w.-]+', '[account]', value)
                return re.sub(r'(?i)\b[A-Z2-7]{16,64}\b', '[key redacted]', value)
            return value
        safe = json.dumps(redact(result))
        (self.directory.parent / 'operator-result.json').write_text(safe, encoding='utf-8')

    def advance_security(self):
        """Enroll a standard authenticator when Microsoft requires initial MFA."""
        if not self.totp and self.totp_path.exists():
            self.totp = protect_password(self.totp_path)
        text = self.page.locator('body').inner_text(timeout=10000)
        lower = text.lower()
        registration_page = ('mysignins.microsoft.com/register' in self.page.url or any(s in lower for s in (
            'set up your account', 'install microsoft authenticator', 'scan the qr code', 'secret key', 'enter the following into authenticator', 'keep your account secure', 'more information required')))
        if registration_page and not self.enrolling and not getattr(self, 'enrollment_completed', False):
            self.enrolling = True
            self.mfa_stage = 'totp_registration'
        if getattr(self, 'enrollment_completed', False) and urlparse(self.page.url).hostname in {'admin.cloud.microsoft', 'admin.microsoft.com'} and '#/homepage' in self.page.url and 'Users' in text:
            self.page.goto('https://admin.cloud.microsoft/#/catalog', wait_until='domcontentloaded')
            return True
        if "enable dom storage or cookies" in lower and not getattr(self, '_storage_reset', False):
            self._storage_reset = True
            self.page.goto('https://admin.cloud.microsoft/#/catalog', wait_until='domcontentloaded')
            return True
        if (ROOT / 'runtime' / 'debug-security.flag').exists() and time.monotonic() - getattr(self, '_debug_at', 0) > 10:
            self._debug_at = time.monotonic()
            headings = self.page.get_by_role('heading').all_text_contents()
            buttons = self.page.get_by_role('button').all_text_contents()
            safe = json.dumps({'security_debug': True, 'headings': headings, 'buttons': buttons,
                               'qr_type': getattr(self, 'qr_type', None), 'totp_saved': bool(self.totp)})
            print(re.sub(r'[\w.+-]+@[\w.-]+', '[account]', safe), flush=True)
        if "you need to set up multifactor authentication" in lower:
            self.enrolling = True
            self.mfa_stage = 'totp_registration'
            return self.click(r"^Set up now$")
        if "you need to sign in using multifactor authentication" in lower:
            return self.click(r"^Continue$")
        security_page = any(s in lower for s in ["keep your account secure", "more information required", "authenticator", "verification code", "enter code", "security info", "registration is complete", "set up your account"])
        if not security_page:
            return False
        if self.click(r"^(?:Set up a different authentication app|I want to use a different authenticator app|Use a different authenticator app|Use a different app|Use another app)$"):
            return True
        if "add a sign-in method" in lower:
            authenticator = self.page.get_by_text("Microsoft Authenticator", exact=True)
            if authenticator.count() and authenticator.first.is_visible():
                authenticator.first.click()
                return True
        if re.search(r"scan.*(?:qr|code)|secret key|enter the following into authenticator", lower):
            secret = None
            if not secret:
                qr_images = self.page.locator("img[alt*='QR' i], img[src^='data:image'], canvas, [role='img'][aria-label*='QR' i]")
                for i in range(min(qr_images.count(), 6)):
                    image = qr_images.nth(i)
                    if not image.is_visible():
                        continue
                    try:
                        from io import BytesIO
                        from PIL import Image
                        from pyzbar.pyzbar import decode
                        codes = decode(Image.open(BytesIO(image.screenshot(timeout=1500))))
                        for code in codes:
                            uri = urlparse(code.data.decode('utf-8'))
                            self.qr_type = uri.scheme
                            if uri.scheme == 'otpauth':
                                secret = parse_qs(uri.query).get('secret', [None])[0]
                    except Exception:
                        continue
                    if secret:
                        break
            if not secret:
                # Only accept an explicitly labeled setup key. Arbitrary long
                # words and unrelated secret= strings are not TOTP seeds.
                match = re.search(r'(?:^|\n)(?:secret key|setup key|key)\s*:?\s*([A-Z2-7][A-Z2-7 \t]{14,100})[ \t]*(?:\r?\n|$)', text, re.I)
                if match:
                    candidate = re.sub(r'\s+', '', match[1])
                    if re.fullmatch(r'[A-Z2-7]{16,64}', candidate, re.I):
                        secret = candidate
            if secret:
                import pyotp
                try:
                    pyotp.TOTP(secret).now()
                except Exception:
                    secret = None
                if secret:
                    self.totp = protect_password(self.totp_path, secret.upper())
                    self.mfa_stage = 'totp_seed_saved'
                    return self.click(r"^Next$")
            if self.click(r"^(?:Can.t scan.*|Unable to scan.*)$"):
                return True
        if self.totp and re.search(r"enter (?:the )?(?:6-digit )?code|verification code", lower) and "once you enter the code displayed on your app or device" not in lower:
            code_inputs = self.page.locator("input#idTxtBx_SAOTCC_OTC, input[name='otc'], input[maxlength='6'], input[autocomplete='one-time-code'], input[type='tel'], input[inputmode='numeric'], input[aria-label*='code' i], input[type='text']:not([readonly]):not([name=loginfmt])")
            for i in range(code_inputs.count()):
                field = code_inputs.nth(i)
                if field.is_visible() and time.monotonic() - self.last_totp_submit > 25:
                    import pyotp
                    field.fill(pyotp.TOTP(self.totp).now())
                    self.last_totp_submit = time.monotonic()
                    self.mfa_stage = 'totp_verifying'
                    if not self.click(r"^(Verify|Next|Sign in)$"):
                        field.press("Enter")
                    return True
        if not self.totp and 'authenticator app' in lower:
            existing_code = self.page.locator('#idTxtBx_SAOTCC_OTC')
            if existing_code.count() and existing_code.first.is_visible():
                login_challenge = urlparse(self.page.url).hostname in {'login.microsoftonline.com','login.microsoft.com'}
                if self.enrolling and not login_challenge:
                    raise MFASetupError('The registration verification page appeared before a valid TOTP seed was captured. Automatic enrollment needs repair; no manual setup was requested.')
                self.mfa_stage = 'existing_mfa_required'
                self.human('This account already has MFA enrolled. Enter its current code from your phone authenticator in the browser, finish sign-in, then click OK here.')
                return True
        if "keep your account secure" in lower or "more information required" in lower or "set up your account" in lower:
            return self.click(r"^(Next|Done)$")
        if self.totp and re.search(r"success|registration is complete|account is now secure|authenticator app added", lower):
            self.enrollment_completed = True
            self.enrolling = False
            self.mfa_stage = 'totp_enrolled'
            return self.click(r"^(Done|Next)$")
        return False

    def portal(self):
        self.navigate("https://admin.cloud.microsoft/")
        deadline = time.monotonic() + 300
        while True:
            if time.monotonic() >= deadline:
                raise MFASetupError(f'Automatic administrator sign-in did not finish (stage: {self.mfa_stage or "loading"}). This is an automation issue; manual MFA setup was not requested.')
            try:
                self.advance_login()
                if urlparse(self.page.url).hostname in {"admin.microsoft.com", "admin.cloud.microsoft"}:
                    text = self.page.locator("body").inner_text(timeout=15000)
                    if '/mfasetup' not in self.page.url and re.search(r"Your organization|Marketplace|Billing", text, re.I) and 'set up multifactor authentication' not in text.lower():
                        return
            except Exception as exc:
                if type(exc).__name__ != "TimeoutError" and 'Execution context was destroyed' not in str(exc):
                    raise
            time.sleep(1)

    def checkout_scope(self):
        # Some MCA checkout panels have a heading but no ARIA dialog role.
        # Find their nearest shared container with the corresponding action.
        for title, actions in [
            (r'^Trial renewal settings$', ['Place order', 'Start trial']),
            (r'^Checkout$', ['Next', 'Continue']),
        ]:
            headings = self.page.get_by_role('heading', name=re.compile(title, re.I))
            for heading in headings.all():
                if not heading.is_visible():
                    continue
                condition = ' or '.join(f'normalize-space(.)="{action}"' for action in actions)
                panel = heading.locator('xpath=ancestor::*[.//button[' + condition + ']][1]')
                if panel.count() and panel.is_visible():
                    return panel
        dialogs = self.page.get_by_role('dialog')
        return dialogs.last if dialogs.count() and dialogs.last.is_visible() else self.page.locator('body')

    def shortest_terms(self, scope=None):
        # Fluent's checkout overlays leave the product page in the DOM. A
        # background control can report visible while the dialog blocks clicks.
        # Only change terms in the active dialog when checkout is open.
        if scope is None:
            scope = self.checkout_scope()
        selected = []
        for i in range(scope.locator("select").count()):
            control = scope.locator("select").nth(i)
            if not control.is_visible():
                continue
            labels = control.locator("option").all_text_contents()
            index = shortest_option(labels)
            if index is not None:
                control.select_option(index=index)
                selected.append(labels[index].strip())
        for i in range(min(scope.get_by_role("combobox").count(), 15)):
            control = scope.get_by_role("combobox").nth(i)
            if not control.is_visible() or control.evaluate("el => el.tagName") == "SELECT":
                continue
            label = (control.get_attribute("aria-label") or "") + " " + control.inner_text()
            if not re.search(r"term|duration|month|year|billing frequency|subscription length", label, re.I):
                continue
            control.click()
            options = self.page.get_by_role("option")
            options.first.wait_for(state='visible', timeout=15000)
            labels = options.all_text_contents()
            index = shortest_option(labels)
            if index is not None:
                options.nth(index).click()
                selected.append(labels[index].strip())
            else:
                self.page.keyboard.press("Escape")
        # Fluent radio inputs get their accessible names from <label for=id>.
        groups = {}
        for radio in scope.locator('input[type=radio]').all():
            radio_id = radio.get_attribute('id') or ''
            if radio_id:
                group_name = radio.get_attribute('name') or radio_id.rsplit('-', 1)[0]
                groups.setdefault(group_name, []).append(radio_id)
        for radio_ids in groups.values():
            choices = []
            for radio_id in radio_ids:
                radio = scope.locator(f'input[id="{radio_id}"]')
                label = scope.locator(f'label[for="{radio_id}"]')
                if not radio.count() or not radio.is_enabled() or not label.count() or not label.is_visible():
                    continue
                text = radio.get_attribute('aria-label') or label.inner_text()
                days = duration_days(text)
                if days is not None:
                    choices.append((days, radio_id, text))
            if choices:
                _, radio_id, text = min(choices)
                radio = scope.locator(f'input[id="{radio_id}"]')
                if not radio.is_checked():
                    scope.locator(f'label[for="{radio_id}"]').click()
                selected.append(text)
        return selected

    def checkout_feedback(self):
        # MCA can render rejection banners outside the checkout panel. They
        # still belong to this page and must be checked after an order attempt.
        parts = [self.checkout_scope().inner_text(timeout=10000)]
        for message in self.page.locator('[role=alert], [role=status], .ms-MessageBar').all():
            if message.is_visible():
                parts.append(message.inner_text(timeout=2500))
        return '\n'.join(parts)

    def select_trial_plan(self):
        plan = self.page.get_by_role('combobox', name='Select a plan', exact=True)
        plan.first.click()
        options = self.page.get_by_role('option')
        # MCA renders the dropdown asynchronously. An empty initial DOM is not
        # evidence that the tenant is ineligible for a trial.
        options.first.wait_for(state='visible', timeout=15000)
        labels = options.all_text_contents()
        candidates = [(i, label) for i, label in enumerate(labels) if '(Trial)' in label and 'Copilot' not in label and options.nth(i).is_enabled()]
        if not candidates:
            self.page.keyboard.press('Escape')
            raise TrialUnavailable('No eligible Exchange-enabled free trial is offered for this product.')
        index, label = min(candidates, key=lambda item: (duration_days(item[1]) or 30, 'no Teams' not in item[1]))
        # The Fluent popup animates continuously in some minimized windows.
        # The exact eligible option is already visible and enabled; do not wait
        # for its animation to become stable before selecting it.
        options.nth(index).click(force=True)
        return label

    def open_trial_product(self, product):
        self.navigate('https://admin.cloud.microsoft/#/catalog')
        deadline = time.monotonic() + 150
        searched = False
        while time.monotonic() < deadline:
            try:
                self.advance_login()
                self.click(r'^Close this coachmark$')
                all_products = self.page.get_by_role('tab', name='All Products', exact=True)
                if all_products.count() and all_products.first.get_attribute('aria-selected') != 'true':
                    all_products.first.click()
                details = self.page.get_by_role('button', name=re.compile(r'View details for.*' + re.escape(product) + r'[\s\u200e\u200f]*$', re.I))
                if details.count() and details.first.is_visible():
                    details.first.click()
                    self.page.get_by_role('combobox', name='Select a plan', exact=True).wait_for(state='visible', timeout=60000)
                    return
                search = self.page.get_by_role('searchbox', name='Search all products', exact=True)
                if not searched and search.count() and search.first.is_visible():
                    search.first.fill(product)
                    searched = True
            except Exception as exc:
                if type(exc).__name__ != 'TimeoutError' and 'Execution context was destroyed' not in str(exc):
                    raise
            time.sleep(2)
        raise TrialUnavailable(f'{product} is not offered in this tenant catalog.')

    def start_trial(self, graph, on_stage):
        order_path = self.directory.parent / 'trial-order-pending.json'
        rejection_path = self.directory.parent / 'trial-rejections.json'
        if order_path.exists():
            prior = json.loads(order_path.read_text(encoding='utf-8-sig'))
            if prior.get('phase') == 'rejected' and prior.get('reason'):
                rejected = json.loads(rejection_path.read_text(encoding='utf-8-sig')) if rejection_path.exists() else {}
                rejected[prior['product']] = prior['reason']
                rejection_path.write_text(json.dumps(rejected, indent=2), encoding='utf-8')
                order_path.unlink()
                on_stage('trial_rejection_reconciled')
        if order_path.exists():
            # A previous process may have died after clicking the final order.
            # Verify its outcome instead of submitting the same order again.
            for _ in range(6):
                if choose_sku(graph.request('GET', '/subscribedSkus').get('value', []), []):
                    return True
                time.sleep(10)
            raise NeedsHuman('A previous trial order has an unconfirmed outcome. No order was repeated; verify that order in Microsoft Billing before retrying activation.')
        self.portal()
        products = ['Microsoft 365 Business Basic', 'Microsoft 365 Business Standard', 'Microsoft 365 Business Premium']
        rejected = json.loads(rejection_path.read_text(encoding='utf-8-sig')) if rejection_path.exists() else {}
        remaining = [p for p in products if p not in rejected]
        if not remaining:
            raise TrialUnavailable('Microsoft has rejected or exhausted all eligible Exchange-enabled product trials checked for this tenant.')
        product = remaining.pop(0)
        deadline = time.monotonic() + 600
        selected_trial = zero_checkout = submitted = False
        last_submit_kind = ''
        def alternative(reason):
            nonlocal product, selected_trial, zero_checkout, submitted, last_submit_kind, deadline
            rejected[product] = reason
            rejection_path.write_text(json.dumps(rejected, indent=2), encoding='utf-8')
            order_path.unlink(missing_ok=True)
            on_stage('trying_alternative_trial')
            dialogs = self.page.get_by_role('dialog')
            if dialogs.count():
                close = dialogs.last.get_by_role('button', name='Close', exact=True)
                if close.count():
                    close.first.click()
            while remaining:
                product = remaining.pop(0)
                try:
                    self.open_trial_product(product)
                    selected_trial = zero_checkout = submitted = False
                    last_submit_kind = ''
                    deadline = time.monotonic() + 600
                    return
                except TrialUnavailable as exc:
                    rejected[product] = str(exc)
                    rejection_path.write_text(json.dumps(rejected, indent=2), encoding='utf-8')
            raise TrialUnavailable('No eligible Exchange-enabled free trial remains. Microsoft rejected or did not offer: ' + ', '.join(rejected))
        # This is the product route read from the tenant's live Marketplace.
        offer_url = 'https://admin.cloud.microsoft/#/catalog/m/offer-details/microsoft-365-business-basic/CFQ7TTC0LH18'
        try:
            if product == products[0]:
                self.navigate(offer_url)
            else:
                self.open_trial_product(product)
        except TrialUnavailable as exc:
            alternative(str(exc))
        card_prompted = False
        last_license_poll = 0
        checkout_step = 'opening the trial product'
        last_ui_timeout = ''
        while time.monotonic() < deadline:
            try:
                if time.monotonic() - last_license_poll > 8:
                    last_license_poll = time.monotonic()
                    if choose_sku(graph.request('GET', '/subscribedSkus').get('value', []), []):
                        return True
                if submitted:
                    self.process_operator_action()
                    response_scope = self.checkout_scope()
                    response_text = self.checkout_feedback()
                    if re.search(r'maximum number of subscriptions.*already been redeemed|trial.*already (?:been )?(?:used|redeemed)', response_text, re.I):
                        reason = 'Microsoft rejected this product trial because its redemption limit was already reached.'
                        order = json.loads(order_path.read_text(encoding='utf-8')) if order_path.exists() else {'product':product}
                        order.update(phase='rejected', reason=reason, rejected_at=utcnow())
                        order_path.write_text(json.dumps(order), encoding='utf-8')
                        on_stage('trial_order_rejected')
                        alternative(reason)
                        continue
                    if re.search(r"(?:you['’]ve|you have) added", response_text, re.I) and 'trial' in response_text.lower():
                        order = json.loads(order_path.read_text(encoding='utf-8')) if order_path.exists() else {'product':product}
                        if order.get('phase') != 'accepted':
                            order.update(phase='accepted', accepted_at=utcnow())
                            order_path.write_text(json.dumps(order), encoding='utf-8')
                            on_stage('trial_order_accepted_waiting_license')
                    # Some MCA checkouts first show "Start trial" while loading
                    # the renewal options, then require the final "Place order".
                    if last_submit_kind == 'Start trial':
                        place_order = response_scope.get_by_role('button', name='Place order', exact=True)
                        # The intermediate Start trial action can reveal an
                        # unchecked consent box. Return to the renewal handler
                        # even while Place order is disabled so it can finish
                        # the authorized settings for this same checkout.
                        if place_order.count():
                            submitted = False
                            continue
                    time.sleep(3)
                    continue
                self.advance_login()
                if urlparse(self.page.url).hostname in {'admin.cloud.microsoft','admin.microsoft.com'} and '#/homepage' in self.page.url:
                    if product == products[0]:
                        self.navigate(offer_url)
                    else:
                        self.open_trial_product(product)
                    time.sleep(2)
                    continue
                self.click(r'^Close this coachmark$')
                scope = self.checkout_scope()
                text = re.sub(r'[\u200b-\u200f\u2060-\u206f\ufeff]', '', scope.inner_text(timeout=10000))
                if re.search(r'placing your order|loading checkout page|checking if you own this product', text, re.I):
                    time.sleep(2)
                    continue
                if "You've added" in text and 'Trial' in text:
                    submitted = True
                    on_stage('trial_order_submitted')
                    continue
                if 'Trial renewal settings' in text:
                    if not zero_checkout:
                        raise NeedsHuman('Trial renewal checkout appeared without a verified zero-due trial checkout; manual review is required.')
                    checkout_step = 'selecting the shortest term in the renewal dialog'
                    on_stage('trial_renewal_settings')
                    self.shortest_terms(scope)
                    consent = scope.get_by_role('checkbox', name=re.compile(r'authorize\s+recurring\s+payments', re.I))
                    if consent.count() and not consent.first.is_checked():
                        if not getattr(self, 'accept_trial_recurring_payments', False):
                            on_stage('needs_renewal_authorization')
                            raise NeedsHuman('Microsoft requires explicit recurring-payment consent before activating this free trial. Place order remains disabled until that consent is authorized; no order was submitted.')
                        checkout_step = 'accepting the explicitly authorized recurring-payment consent'
                        checkbox_id = consent.first.get_attribute('id')
                        label = scope.locator(f'label[for="{checkbox_id}"]') if checkbox_id else None
                        if label is not None and label.count() and label.first.is_visible():
                            label.first.click()
                        else:
                            consent.first.check()
                        if not consent.first.is_checked():
                            raise GraphError('Microsoft did not accept the authorized recurring-payment consent; the order was not submitted.')
                        on_stage('trial_recurring_payment_consent_accepted')
                    checkout_step = 'locating the final free-trial order button'
                    final = scope.get_by_role('button', name=re.compile(r'^(Place order|Start trial)$'))
                    if final.count() and final.last.is_enabled():
                        # Persist the submission stage before clicking; never repeat an uncertain order.
                        submitted = True
                        last_submit_kind = final.last.inner_text().strip()
                        order = {'product':product, 'button':last_submit_kind, 'attempted_at':utcnow(), 'phase':'prepared'}
                        order_path.write_text(json.dumps(order), encoding='utf-8')
                        on_stage('trial_order_submitting')
                        final.last.click(timeout=15000)
                        order.update(phase='clicked', clicked_at=utcnow())
                        order_path.write_text(json.dumps(order), encoding='utf-8')
                        on_stage('trial_order_awaiting_confirmation')
                    time.sleep(2)
                    continue
                if re.search(r'\bCheckout\b', text) and selected_trial:
                    checkout_step = 'checking the saved billing profile'
                    profile = scope.get_by_role('combobox', name=re.compile(r'Choose billing profile', re.I))
                    saved_profile = bool(profile.count() and profile.first.inner_text().strip() and 'Choose a billing profile' not in profile.first.inner_text())
                    requires_card = bool(re.search(r'no payment methods|payment method is required', text, re.I)) or (scope.get_by_role('button', name='Add a new payment method', exact=True).count() and not saved_profile)
                    if requires_card and not card_prompted:
                        on_stage('needs_card')
                        self.human('Microsoft requires a payment method. Add a card directly in the visible browser, then return to this free trial checkout and click Continue. Keep the shortest term and leave the renewal setting unchanged. Card details are never read or saved by the runner.')
                        card_prompted = True
                        continue
                    # Distinguish today's zero-due trial from the future renewal price.
                    if re.search(r'Total due\s*:?\s*(?:[A-Z]{0,3}\s*)?[$€£]?\s*0[.,]00\b', text, re.I):
                        zero_checkout = True
                        on_stage('trial_zero_due_verified')
                        checkout_step = 'continuing from the zero-due checkout to renewal settings'
                        next_button = scope.get_by_role('button', name=re.compile(r'^(Next|Continue)$'))
                        if next_button.count() and next_button.first.is_enabled():
                            next_button.first.click()
                        elif next_button.count():
                            checkout_step = 'waiting for Microsoft to enable the checkout Next button'
                    time.sleep(2)
                    continue
                plan = self.page.get_by_role('combobox', name='Select a plan', exact=True)
                if plan.count() and plan.first.is_visible():
                    current = plan.first.inner_text()
                    if '(Trial)' not in current:
                        self.select_trial_plan()
                        selected_trial = True
                        on_stage('one_month_trial_selected')
                        time.sleep(1)
                    else:
                        selected_trial = True
                        self.shortest_terms()
                        self.click(r'^Start free trial$')
            except Exception as exc:
                if isinstance(exc, TrialUnavailable):
                    alternative(str(exc))
                    continue
                if type(exc).__name__ == 'TimeoutError':
                    last_ui_timeout = checkout_step
                if type(exc).__name__ != 'TimeoutError' and 'Execution context was destroyed' not in str(exc):
                    raise
            time.sleep(2)
        if submitted:
            raise NeedsHuman('A trial order was attempted, but Microsoft acceptance and an available Exchange license could not be confirmed within ten minutes. The pending order checkpoint was retained; no repeat order was placed.')
        raise GraphError('Trial checkout automation timed out before an order was submitted. Last step: ' + (last_ui_timeout or checkout_step) + '. This is an incomplete checkout, not a confirmed Microsoft trial rejection.')

    def close(self):
        if self.context:
            self.context.close()
            self.context = None


class PowerShellGraph:
    def __init__(self, tenant_id, mode="Admin"):
        self.proc = subprocess.Popen([shutil.which("pwsh") or "pwsh", "-NoProfile", "-File", str(ROOT / "graph_bridge.ps1"),
                                      "-TenantId", tenant_id, "-Mode", mode], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.messages = queue.Queue()
        self.sequence = 0
        def read():
            for line in self.proc.stdout:
                self.messages.put(line.rstrip())
            self.messages.put(None)
        threading.Thread(target=read, daemon=True).start()

    def authenticate(self, browser, on_stage=None):
        deadline = time.monotonic() + 890
        device_started = False
        interaction_start = time.monotonic()
        while time.monotonic() < deadline:
            try:
                line = self.messages.get(timeout=1)
            except queue.Empty:
                line = ""
            if line is None:
                raise GraphError("Graph authentication process exited.")
            if line.startswith("READINESS_JSON:"):
                data = json.loads(line.split(":", 1)[1])
                if data.get("event") == "fatal":
                    raise GraphError(data["error"])
                if data.get("event") == "ready":
                    if data.get("account", "").lower() != browser.account.lower():
                        raise GraphError("Signed-in account does not match the requested account.")
                    browser.device_code = None
                    return
            match = re.search(r"enter (?:the )?code\s+([A-Z0-9]{8,12})", line, re.I)
            if match and not device_started:
                browser.begin_device_login(match[1])
                device_started = True
                interaction_start = time.monotonic()
            if device_started:
                try:
                    browser.advance_login()
                except Exception:
                    pass  # Navigation can replace the document between selectors.
                if time.monotonic() - interaction_start > 45:
                    if on_stage:
                        on_stage()
                    browser.human("Complete the Microsoft sign-in, MFA enrollment, or permissions prompt in the browser, then click Continue here. Keep the existing tenant security settings enabled.")
                    interaction_start = time.monotonic()
        raise NeedsHuman("Microsoft authentication timed out; rerun to resume.")

    def request(self, method, path, body=None):
        self.sequence += 1
        message = {"id": self.sequence, "method": method, "path": path}
        if body is not None:
            message["body"] = body
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                line = self.messages.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                raise GraphError("Graph process exited during a request.")
            if line.startswith("READINESS_JSON:"):
                reply = json.loads(line.split(":", 1)[1])
                if reply.get("event") == "fatal":
                    raise GraphError(reply["error"])
                if reply.get("id") == self.sequence:
                    if not reply["ok"]:
                        raise GraphError(reply["error"])
                    return reply.get("data") or {}
        raise GraphError("Graph request timed out; its outcome is uncertain.")

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write('{"method":"QUIT"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.terminate()
                self.proc.wait(timeout=5)


class Graph:
    """MSAL device authentication and Graph REST, with no idle PowerShell process."""
    CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft Graph Command Line Tools

    def __init__(self, tenant_id, mode="Admin"):
        import requests
        self.tenant_id, self.mode = tenant_id, mode
        self.session = requests.Session()
        self.scopes = ["Mail.Send", "Mail.Read", "User.Read"]
        if mode == "Admin":
            self.scopes += ["Organization.Read.All", "User.ReadWrite.All", "LicenseAssignment.ReadWrite.All"]
        self.app = self.cache = self.cache_path = self.account = self.result = None
        self.flow = None

    def save_cache(self):
        if self.cache.has_state_changed:
            protect_password(self.cache_path, self.cache.serialize())

    def authenticate(self, browser, on_stage=None):
        import msal
        self.cache = msal.SerializableTokenCache()
        self.cache_path = browser.directory.parent / (self.mode.lower() + "-tokens.dpapi")
        if self.cache_path.exists():
            self.cache.deserialize(protect_password(self.cache_path))
        self.app = msal.PublicClientApplication(self.CLIENT_ID,
            authority="https://login.microsoftonline.com/" + self.tenant_id, token_cache=self.cache)
        accounts = self.app.get_accounts(username=browser.account)
        result = self.app.acquire_token_silent(self.scopes, account=accounts[0]) if accounts else None
        if not result or "access_token" not in result:
            self.flow = self.app.initiate_device_flow(scopes=self.scopes)
            if "user_code" not in self.flow:
                raise GraphError(self.flow.get("error_description", "Could not start Microsoft device authentication."))
            replies = queue.Queue()
            def acquire():
                try:
                    replies.put(self.app.acquire_token_by_device_flow(self.flow))
                except Exception as exc:
                    replies.put({"error_description": str(exc)})
            threading.Thread(target=acquire, daemon=True).start()
            browser.begin_device_login(self.flow["user_code"])
            last_interaction = time.monotonic()
            while True:
                try:
                    result = replies.get(timeout=0.5)
                    break
                except queue.Empty:
                    pass
                try:
                    browser.advance_login()
                except Exception as exc:
                    transient = "Execution context was destroyed" in str(exc) or "Frame was detached" in str(exc)
                    if type(exc).__name__ != "TimeoutError" and not transient:
                        raise
                if time.monotonic() - last_interaction > 240:
                    raise MFASetupError(f'Automatic Microsoft sign-in did not finish (stage: {browser.mfa_stage or "loading"}). No manual enrollment was requested.')
        if not result or "access_token" not in result:
            raise GraphError((result or {}).get("error_description", "Microsoft did not issue an access token."))
        claims = result.get("id_token_claims", {})
        identities = self.app.get_accounts(username=browser.account)
        identity = identities[0] if identities else {}
        authenticated_tenant = claims.get("tid") or identity.get("realm") or ""
        if authenticated_tenant.lower() != self.tenant_id.lower():
            raise GraphError("Authenticated tenant does not match the CSV.")
        username = claims.get("preferred_username") or claims.get("upn") or identity.get("username") or ""
        if username.lower() != browser.account.lower():
            raise GraphError("Authenticated account does not match the requested account.")
        self.result = result
        self.account = next((a for a in self.app.get_accounts() if a.get("username", "").lower() == browser.account.lower()), None)
        self.save_cache()
        browser.device_code = None

    def request(self, method, path, body=None):
        if not path.startswith("/") or "://" in path or ".." in path:
            raise GraphError("Invalid Graph path.")
        result = self.app.acquire_token_silent(self.scopes, account=self.account)
        if not result or "access_token" not in result:
            raise GraphError("Microsoft session could not be refreshed; sign in again.")
        self.save_cache()
        for attempt in range(4):
            idempotent = method in {"GET", "PATCH"} or path.endswith("/assignLicense")
            try:
                response = self.session.request(method, "https://graph.microsoft.com/v1.0" + path,
                    headers={"Authorization": "Bearer " + result["access_token"]}, json=body, timeout=60)
            except (requests.ConnectionError, requests.Timeout):
                if not idempotent or attempt == 3:
                    raise
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (409, 429, 502, 503, 504) and idempotent and attempt < 3:
                retry = response.headers.get("Retry-After", "")
                time.sleep(min(int(retry) if retry.isdigit() else 2 ** attempt, 30))
                continue
            if not response.ok:
                try:
                    error = response.json().get("error", {})
                    detail = f"Graph {response.status_code}: {error.get('code', '')}: {error.get('message', '')}"
                except ValueError:
                    detail = f"Graph HTTP {response.status_code}"
                raise GraphError(detail)
            return response.json() if response.content else {}
        raise GraphError("Graph remained unavailable after bounded retries.")

    def close(self):
        if self.flow:
            self.flow["expires_at"] = 0
        self.session.close()


def get_messages(graph, since):
    cutoff = datetime.fromisoformat(since).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = "/me/messages?" + "$filter=" + quote(f"receivedDateTime ge {cutoff}", safe="") + "&$select=id,subject,body,internetMessageHeaders&$top=100"
    messages, pages = [], 0
    while path:
        reply = graph.request("GET", path)
        messages.extend(reply.get("value", []))
        pages += 1
        next_link = reply.get("@odata.nextLink", "")
        if next_link and not next_link.startswith("https://graph.microsoft.com/v1.0/"):
            raise GraphError("Unexpected pagination host.")
        path = next_link.removeprefix("https://graph.microsoft.com/v1.0") if next_link else None
        if pages >= 100 and path:
            raise GraphError("Message scan exceeded pagination limit; no clean result can be recorded.")
    return messages


def monitor(graph, tenant, store, minutes):
    row = store.state[tenant["id"]]
    start = time.monotonic()
    store.update(tenant, stage="monitoring_ndrs", status="monitoring", monitor_started_at=utcnow(), monitor_completed_at="")
    while True:
        result = classify_ndr(get_messages(graph, row["sent_at"]), row["subject"])
        elapsed = round(time.monotonic() - start)
        if result:
            store.update(tenant, **result, stage="complete", monitor_seconds=elapsed, monitor_completed_at=utcnow(),
                         detail="A correlated NDR was found in the sending mailbox.")
            return
        if time.monotonic() - start >= minutes * 60:
            if not row.get("send_accepted"):
                store.update(tenant, status="send_uncertain", stage="sending_uncertain", monitor_seconds=elapsed,
                             monitor_completed_at=utcnow(), detail="No NDR was found, but the earlier send could not be confirmed as accepted. No replacement message was sent.")
                return
            store.update(tenant, status="no_ndr_observed", stage="complete", monitor_seconds=elapsed,
                         monitor_completed_at=utcnow(), detail=f"No correlated NDR observed during {minutes:g} minutes of successful mailbox checks. Receipt was not checked; later bounces remain possible.")
            return
        time.sleep(min(15, max(0.1, minutes * 60 - (time.monotonic() - start))))


def run_tenant(tenant, store, playwright, args):
    row = store.state.get(tenant["id"], {})
    if row.get("status") in TERMINAL and not args.recheck:
        return
    tenant_dir = (getattr(args, 'profile_root', None) or store.directory / "private") / tenant["id"]
    tenant_dir.mkdir(parents=True, exist_ok=True)
    browser = graph = mail_browser = mail_graph = None
    transferred_graph = None
    secret_values = [tenant["password"]]
    try:
        store.update(tenant, status="running", stage="admin_authentication", detail="")
        browser = Browser(playwright, tenant_dir / "admin-profile", tenant)
        browser.defer_auth = getattr(args, "defer_auth", False)
        browser.on_wait = lambda message: store.update(tenant, status='awaiting_user', stage='needs_card' if 'payment' in message.lower() or 'card' in message.lower() else 'needs_mfa', detail=message)
        browser.on_progress = lambda stage: store.update(tenant, status='running', stage=stage)
        graph = Graph(tenant["id"])
        graph.authenticate(browser, lambda: store.update(tenant, status="awaiting_user", stage="admin_authentication"))
        orgs = graph.request("GET", "/organization?$select=id,countryLetterCode,verifiedDomains").get("value", [])
        if len(orgs) != 1 or orgs[0]["id"].lower() != tenant["id"]:
            raise GraphError("Organization identity does not match the input CSV.")
        org = orgs[0]
        me = graph.request("GET", "/me?$select=id,userPrincipalName,mail,usageLocation,assignedLicenses")
        if me["userPrincipalName"].lower() != tenant["admin"]:
            raise GraphError("Administrator identity does not match the input CSV.")
        if getattr(args, "preflight", False):
            skus = graph.request("GET", "/subscribedSkus").get("value", [])
            sku = choose_sku(skus, me.get("assignedLicenses", []))
            store.update(tenant, stage="preflight_complete", status="license_available" if sku else "needs_trial",
                         sku=sku.get("skuPartNumber", "") if sku else "",
                         detail="Authentication verified. " + ("An Exchange-enabled license is available." if sku else "No available Exchange-enabled license; trial activation is required."))
            if not (sku and getattr(args, "test_existing", False)):
                return
        initial = next((d["name"] for d in org["verifiedDomains"] if d.get("isInitial") and d["name"].endswith(".onmicrosoft.com")), None)
        if not initial:
            raise GraphError("No verified initial onmicrosoft.com domain found.")
        previous = dict(store.state[tenant["id"]])
        use_admin = me["userPrincipalName"].lower().endswith("@" + initial.lower()) and (not me.get("mail") or me["mail"].lower().endswith("@" + initial.lower()))
        sender_upn = previous.get("sender") or (me["userPrincipalName"] if use_admin else "tenant-readiness-check@" + initial)
        is_admin = sender_upn.lower() == tenant["admin"]
        if not sender_upn.lower().endswith("@" + initial.lower()):
            raise GraphError("Sender is not in the tenant's initial onmicrosoft.com domain.")
        user_password = None
        if is_admin:
            user = me
        else:
            password_path = tenant_dir / "mailbox-password.dpapi"
            if password_path.exists():
                user_password = protect_password(password_path)
                secret_values.append(user_password)
            try:
                user = graph.request("GET", "/users/" + quote(sender_upn, safe="") + "?$select=id,userPrincipalName,mail,usageLocation,assignedLicenses")
            except GraphError as exc:
                if not re.search(r"Request_ResourceNotFound|ResourceNotFound|404", str(exc)):
                    raise
                user = None
            if user is None:
                user_password = protect_password(password_path, secrets.token_urlsafe(26) + "!aA7")
                secret_values.append(user_password)
                user = graph.request("POST", "/users", {"accountEnabled": True, "displayName": "Tenant readiness diagnostic",
                    "mailNickname": "tenant-readiness-check", "userPrincipalName": sender_upn,
                    "passwordProfile": {"forceChangePasswordNextSignIn": False, "password": user_password}})
            elif not user_password:
                raise NeedsHuman("Diagnostic username already exists without a locally saved password. No password was reset.")
        store.update(tenant, sender=sender_upn)
        if user.get("mail") and not user["mail"].lower().endswith("@" + initial.lower()):
            raise NeedsHuman("Selected mailbox has a custom primary email address; an onmicrosoft.com sender is required.")
        if not previous.get("sent_at"):
            store.update(tenant, stage="checking_licenses")
            skus = graph.request("GET", "/subscribedSkus").get("value", [])
            sku = choose_sku(skus, user.get("assignedLicenses", []))
            if not sku:
                if getattr(args, "preflight", False):
                    store.update(tenant, status="needs_trial", stage="preflight_complete", detail="A separate onmicrosoft.com diagnostic mailbox needs an available Exchange license.")
                    return
                before_skus = {s["skuId"] for s in skus}
                store.update(tenant, stage="trial_activation")
                browser.start_trial(graph, lambda stage: store.update(tenant, stage=stage, status="awaiting_user" if stage.startswith('needs_') else "running"))
                skus = graph.request("GET", "/subscribedSkus").get("value", [])
                sku = choose_sku(skus, user.get("assignedLicenses", []))
                store.update(tenant, trial_started=True, trial_skus_added=sorted({s["skuId"] for s in skus} - before_skus))
            if not sku:
                raise GraphError("No active Exchange license with a free seat is available.")
            if sku["skuId"] not in {a["skuId"] for a in user.get("assignedLicenses", [])}:
                location = user.get("usageLocation") or org.get("countryLetterCode") or args.usage_location
                if not location or not re.fullmatch(r"[A-Z]{2}", location):
                    raise NeedsHuman("No usage location is configured; provide --usage-location with the correct two-letter country code.")
                if not user.get("usageLocation"):
                    graph.request("PATCH", "/users/" + user["id"], {"usageLocation": location})
                graph.request("POST", "/users/" + user["id"] + "/assignLicense", {"addLicenses": [{"skuId": sku["skuId"]}], "removeLicenses": []})
            store.update(tenant, sku=sku.get("skuPartNumber", sku["skuId"]), status="running", stage="mailbox_provisioning")
        if is_admin:
            mail_graph = graph
        else:
            mail_browser = Browser(playwright, tenant_dir / "mailbox-profile", tenant, sender_upn, user_password)
            mail_browser.defer_auth = getattr(args, "defer_auth", False)
            mail_browser.on_wait = browser.on_wait
            mail_browser.on_progress = browser.on_progress
            mail_graph = Graph(tenant["id"], "Mailbox")
            mail_graph.authenticate(mail_browser, lambda: store.update(tenant, status="awaiting_user", stage="mailbox_authentication"))
        sender = mail_graph.request("GET", "/me?$select=id,userPrincipalName,mail")
        if sender["id"] != user["id"] or not (sender.get("mail") or sender["userPrincipalName"]).lower().endswith("@" + initial.lower()):
            raise GraphError("Authenticated sender identity or primary email domain does not match.")
        if not previous.get("sent_at"):
            # Readiness errors are separate from SMTP delivery blocks.
            deadline = time.monotonic() + args.provision_minutes * 60
            while True:
                try:
                    mail_graph.request("GET", "/me/mailFolders/inbox?$select=id")
                    break
                except GraphError as exc:
                    if not re.search(r"MailboxNotEnabledForRESTAPI|MailboxNotFound|ErrorNonExistentMailbox|ErrorItemNotFound|ResourceNotFound|404", str(exc)):
                        raise
                    if time.monotonic() >= deadline:
                        raise NeedsHuman("Mailbox provisioning has not finished. Rerun later to resume.")
                    time.sleep(15)
            subject = previous.get("subject") or f"Tenant readiness {uuid4()}"
            if previous.get("stage") == "sending_uncertain":
                raise NeedsHuman("An earlier send has an uncertain outcome. Use --recheck to monitor that probe; it will not be sent again automatically.")
            # Save before sending. A crash after submission must never silently duplicate it.
            sent_at = utcnow()
            store.update(tenant, subject=subject, sent_at=sent_at, stage="sending_uncertain", status="running")
            mail_graph.request("POST", "/me/sendMail", {"message": {"subject": subject,
                "body": {"contentType": "Text", "content": f"Single outbound mail readiness check. Reference: {subject}. No reply is needed."},
                "toRecipients": [{"emailAddress": {"address": RECIPIENT}}]}, "saveToSentItems": True})
            store.update(tenant, stage="sent", send_accepted=True)
        elif not previous.get("send_accepted"):
            # Recover a crash after sendMail accepted the probe, using Sent Items.
            escaped = previous["subject"].replace("'", "''")
            sent = mail_graph.request("GET", "/me/mailFolders/sentitems/messages?$filter=" + quote(f"subject eq '{escaped}'", safe="") + "&$select=id,subject,toRecipients&$top=10")
            if any(any(r.get("emailAddress", {}).get("address", "").lower() == RECIPIENT for r in m.get("toRecipients", [])) for m in sent.get("value", [])):
                store.update(tenant, send_accepted=True, stage="sent_recovered")
        monitor_pool = getattr(args, "monitor_pool", None)
        if monitor_pool:
            store.update(tenant, stage="queued_for_ndr_monitoring", status="monitoring")
            def finish_monitor():
                try:
                    monitor(mail_graph, tenant, store, args.monitor_minutes)
                except Exception as exc:
                    store.update(tenant, status="error", stage="monitoring_ndrs", detail=str(exc)[:1500])
                finally:
                    mail_graph.close()
            monitor_pool.submit(finish_monitor)
            transferred_graph = mail_graph
        else:
            monitor(mail_graph, tenant, store, args.monitor_minutes)
    except (KeyboardInterrupt, SystemExit):
        store.update(tenant, status="interrupted", detail="Interrupted; saved progress can be resumed.")
        raise
    except Exception as exc:
        detail = str(exc).split("Browser logs:", 1)[0]
        for value in secret_values:
            detail = detail.replace(value, "[REDACTED]")
        store.update(tenant, status="skipped_by_user" if isinstance(exc, UserSkipped) else ("needs_user" if isinstance(exc, NeedsHuman) else "error"), detail=detail[:1500])
    finally:
        # A user may close the visible window while skipping; keep the batch alive.
        resources = ([mail_graph] if mail_graph and mail_graph is not graph else []) + [graph, mail_browser, browser]
        for resource in resources:
            if resource and resource is not transferred_graph:
                try:
                    resource.close()
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=ROOT / "runtime")
    parser.add_argument("--profile-root", type=Path, help="Reuse existing isolated tenant profiles for a nonoverlapping retry.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--defer-auth", action="store_true", help="Defer sign-ins needing human intervention and continue other tenants.")
    parser.add_argument("--preflight", action="store_true", help="Authenticate and inventory licenses without changing the tenant.")
    parser.add_argument("--test-existing", action="store_true", help="With --preflight, test tenants that already have an available license.")
    parser.add_argument("--exclude", action="append", default=[], help="Tenant GUID to exclude from this invocation.")
    parser.add_argument("--only", action="append", default=[], help="Only process the specified tenant GUID; repeat for multiple tenants.")
    parser.add_argument("--prioritize", action="append", default=[], help="Process this tenant GUID before the other pending tenants.")
    parser.add_argument("--monitor-minutes", type=float, default=5)
    parser.add_argument("--provision-minutes", type=float, default=20)
    parser.add_argument("--usage-location", help="Fallback country code only when tenant and user have none.")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--recheck", action="store_true", help="Monitor an existing probe again without sending another message.")
    args = parser.parse_args()
    if args.monitor_minutes <= 0 or args.provision_minutes <= 0 or args.limit < 0 or not 1 <= args.workers <= 8:
        parser.error("Time limits must be positive and --limit must be nonnegative.")
    tenants = load_tenants(args.csv)
    print(json.dumps({"tenants": len(tenants), "recipient": RECIPIENT,
                      "monitor_minutes": args.monitor_minutes, "renewal_setting": "unchanged"}), flush=True)
    if args.validate_only:
        return 0
    if os.name != "nt":
        parser.error("This runner uses Windows DPAPI and requires Windows.")
    if not shutil.which("pwsh"):
        parser.error("PowerShell 7 (pwsh) and Microsoft.Graph.Authentication are required.")
    args.runtime.mkdir(parents=True, exist_ok=True)
    # Hold an OS file lock for the run; a crash releases it without stale PID checks.
    import msvcrt
    lock = (args.runtime / "runner.lock").open("a+b")
    lock.seek(0)
    lock.write(b"0")
    lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        parser.error("Another readiness runner is already using this runtime directory.")
    store = Store(args.runtime)
    # Profiles contain authenticated sessions: restrict runtime to this Windows user.
    sid = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
    current_sid = next(csv.reader([sid.strip()]))[1]
    acl = subprocess.run(["icacls", str(args.runtime.resolve()), "/inheritance:r", "/grant:r", f"*{current_sid}:(OI)(CI)F", "/Q"], capture_output=True)
    if acl.returncode:
        raise RuntimeError("Could not restrict access to the runtime directory.")
    for child in args.runtime.iterdir():
        inherited = subprocess.run(["icacls", str(child.resolve()), "/inheritance:e", "/T", "/Q"], capture_output=True)
        if inherited.returncode:
            raise RuntimeError("Could not enable private runtime inheritance.")
    from playwright.sync_api import sync_playwright
    pending = [t for t in tenants if t["id"] not in args.exclude and t["id"] not in store.user_skips and (not args.only or t["id"] in args.only) and (store.state.get(t["id"], {}).get("sent_at") if args.recheck
                else store.state.get(t["id"], {}).get("status") not in TERMINAL)]
    if args.limit:
        pending = pending[:args.limit]
    pending.sort(key=lambda tenant: 0 if tenant['id'] in args.prioritize else 1)
    for tenant in pending:
        store.update(tenant, status='queued', stage='queued', detail='')
    def work(tenant):
        print(f"Tenant {tenant['number'] or tenant['id'][:8]}: starting", flush=True)
        with sync_playwright() as playwright:
            run_tenant(tenant, store, playwright, args)
        final = store.state[tenant["id"]]
        print(f"Tenant {tenant['number'] or tenant['id'][:8]}: {final['status']}", flush=True)
    with ThreadPoolExecutor(max_workers=34) as monitor_pool, ThreadPoolExecutor(max_workers=args.workers) as pool:
        args.monitor_pool = monitor_pool
        futures = [pool.submit(work, tenant) for tenant in pending]
        for future in as_completed(futures):
            future.result()
    print(f"Report: {store.directory / 'report.csv'}", flush=True)
    lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
