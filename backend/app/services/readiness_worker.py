"""Leased database queue. Browser work and NDR monitoring have separate pools."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import and_, or_, select, update

from app.core.config import get_settings
from app.db.session import SyncSessionLocal
from app.models.readiness import ReadinessCheck
from app.services.inventory_credentials import decrypt_inventory_value, encrypt_inventory_value
from app.services import readiness_engine as engine

logger = logging.getLogger(__name__)
ARTIFACTS = {'admin-totp.dpapi', 'admin-tokens.dpapi', 'mailbox-totp.dpapi',
             'mailbox-tokens.dpapi', 'mailbox-password.dpapi',
             'trial-rejections.json', 'trial-order-pending.json'}
ACTIVE_STATUSES = {'running', 'monitoring', 'awaiting_user'}
LIVE: dict[str, dict] = {}
STORES: dict[str, 'DatabaseStore'] = {}
OWNER = str(uuid4())
LEASE_SECONDS = 120


def now():
    return datetime.now(timezone.utc)


def recurring_consent_authorized(tenant_id):
    settings = get_settings()
    return settings.readiness_accept_trial_recurring_payments and str(tenant_id).lower() in {
        value.lower() for value in settings.readiness_trial_consent_tenant_ids}


class LeaseLost(RuntimeError):
    pass


def cloud_secret(path, value=None):
    """Same engine interface as DPAPI, using the app's encryption key on Linux."""
    path = Path(path)
    if value is None:
        return decrypt_inventory_value(path.read_text(encoding='utf-8'))
    path.write_text(encrypt_inventory_value(value), encoding='utf-8')
    store = STORES.get(str(path.parent))
    if store:
        store.persist()
    return value


class CloudBrowser(engine.Browser):
    def open(self, url=None):
        self.accept_trial_recurring_payments = recurring_consent_authorized(self.tenant['id'])
        self.context = self.playwright.chromium.launch_persistent_context(
            str(self.directory.resolve()), executable_path=os.getenv('CHROME_PATH', '/usr/bin/chromium'),
            headless=True, ignore_default_args=['--headless=old', '--headless'],
            viewport={'width': 1440, 'height': 1000}, locale='en-US',
            args=['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-notifications',
                  '--renderer-process-limit=2', '--disable-gpu',
                  '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
                  '--disable-backgrounding-occluded-windows'])
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.use_page(self.page)
        self.context.on('page', self.use_page)
        self.live = {'actions': queue.Queue(20), 'image': None, 'waiting': False, 'heartbeat': 0}
        LIVE[self.tenant['id']] = self.live
        if url:
            self.navigate(url)

    def visible(self):
        # The app displays the existing server browser; it never restarts login.
        self.live['waiting'] = True

    def process_operator_action(self):
        if self.live.pop('inspect_checkout', False):
            try:
                dialogs = self.page.get_by_role('dialog')
                scope = dialogs.last if dialogs.count() and dialogs.last.is_visible() else self.page.locator('body')
                self.live['checkout_inspection'] = {
                    'captured_at': engine.utcnow(),
                    'controls': scope.locator('h1,h2,button,select,[role=combobox],input[type=radio],input[type=checkbox],label').evaluate_all('''els => els.filter(e => e.offsetWidth || e.offsetHeight).slice(0,120).map(e => ({
                        tag:e.tagName, role:e.getAttribute('role'), label:e.getAttribute('aria-label'),
                        id:e.id, name:e.getAttribute('name'), type:e.type,
                        disabled:e.disabled || e.getAttribute('aria-disabled') === 'true',
                        checked:e.checked, text:e.tagName === 'INPUT' ? '' : (e.innerText || '').slice(0,200)
                    }))''')}
            except Exception as exc:
                self.live['checkout_inspection'] = {'captured_at':engine.utcnow(), 'error_type':type(exc).__name__}
        while True:
            try:
                action = self.live['actions'].get_nowait()
            except queue.Empty:
                break
            try:
                if action['op'] == 'click':
                    self.page.mouse.click(action['x'], action['y'])
                elif action['op'] == 'text':
                    self.page.keyboard.insert_text(action['text'])
                elif action['op'] == 'key':
                    self.page.keyboard.press(action['key'])
            except Exception:
                logger.warning('Readiness browser input could not be applied for %s', self.tenant['id'])
        if self.live['waiting'] and time.monotonic() - self.live['heartbeat'] >= 1:
            try:
                self.live['image'] = self.page.screenshot(type='jpeg', quality=65, timeout=4000)
                self.live['heartbeat'] = time.monotonic()
            except Exception:
                pass

    def human(self, message):
        if getattr(self, 'on_wait', None):
            self.on_wait(message)
        self.live['waiting'] = True
        self.live['message'] = message
        deadline = time.monotonic() + 180
        initial_url = self.page.url
        while time.monotonic() < deadline:
            self.process_operator_action()
            # Resume automation after the operator submits a code or the page
            # leaves the sign-in challenge. Input is kept only in the live queue.
            if self.page.url != initial_url or self.live.pop('continue', False):
                self.live['waiting'] = False
                return
            time.sleep(.5)
        raise engine.NeedsHuman(message + ' Open the app and retry this tenant to continue.')

    def close(self):
        if LIVE.get(self.tenant['id']) is getattr(self, 'live', None):
            LIVE.pop(self.tenant['id'], None)
        super().close()


engine.Browser = CloudBrowser
engine.protect_password = cloud_secret


class DatabaseStore:
    def __init__(self, record, directory):
        self.id = record.id
        self.tenant_id = str(record.tenant_id)
        self.directory = Path(directory)
        self.private = self.directory / 'private' / self.tenant_id
        self.private.mkdir(parents=True)
        self.lock = threading.RLock()
        self.state = {self.tenant_id: dict(record.state)}
        self.credentials = json.loads(decrypt_inventory_value(record.credentials_ciphertext))
        self.artifacts = json.loads(decrypt_inventory_value(record.artifacts_ciphertext))
        for name, value in self.artifacts.items():
            if name not in ARTIFACTS:
                continue
            target = self.private / name
            target.write_text(encrypt_inventory_value(value) if name.endswith('.dpapi') else value, encoding='utf-8')
        STORES[str(self.private)] = self

    def collect_artifacts(self):
        result = {}
        for name in ARTIFACTS:
            path = self.private / name
            if path.exists():
                result[name] = cloud_secret(path) if name.endswith('.dpapi') else path.read_text(encoding='utf-8')
        return result

    def persist(self, changes=None):
        with self.lock:
            previous = self.state[self.tenant_id]
            state = {**previous, **(changes or {}), 'updated_at': engine.utcnow()}
            artifacts = self.collect_artifacts()
            # This commit is required before sendMail, final trial order, new
            # mailbox creation, and TOTP verification can proceed.
            with SyncSessionLocal() as db:
                result = db.execute(update(ReadinessCheck).where(
                    ReadinessCheck.id == self.id, ReadinessCheck.lease_owner == OWNER,
                    ReadinessCheck.lease_until > now(),
                ).values(state=state, status=state.get('status', 'running'),
                         artifacts_ciphertext=encrypt_inventory_value(json.dumps(artifacts))))
                if result.rowcount != 1:
                    raise LeaseLost('This check belongs to another worker; this worker stopped before changing the tenant.')
                db.commit()
            previous.clear()
            previous.update(state)

    def update(self, tenant, **changes):
        # Exceptions from Microsoft may quote submitted data. Never persist it
        # in the public report, even when the credential submission failed.
        if 'detail' in changes:
            detail = str(changes['detail'])
            for secret in [self.credentials.get('password'), *(
                v for k, v in self.collect_artifacts().items() if k.endswith('totp.dpapi'))]:
                if secret:
                    detail = detail.replace(secret, '[redacted]')
            changes['detail'] = detail[:1500]
        self.persist(changes)
        return self.state[self.tenant_id]


def _claim(limit):
    with SyncSessionLocal() as db:
        candidates = db.scalars(select(ReadinessCheck).where(or_(
            and_(ReadinessCheck.status == 'queued',
                 or_(ReadinessCheck.lease_until < now(), ReadinessCheck.lease_until.is_(None))),
            and_(ReadinessCheck.status.in_(ACTIVE_STATUSES),
                 or_(ReadinessCheck.lease_until < now(), ReadinessCheck.lease_until.is_(None))),
        )).order_by(ReadinessCheck.created_at).limit(limit).with_for_update(skip_locked=True)).all()
        for record in candidates:
            record.lease_owner = OWNER
            record.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
            record.status = 'running'
            record.state = {**record.state, 'status': 'running', 'detail': ''}
            record.attempts += 1
        db.commit()
        return candidates


def _release(check_id):
    with SyncSessionLocal() as db:
        db.execute(update(ReadinessCheck).where(ReadinessCheck.id == check_id,
            ReadinessCheck.lease_owner == OWNER).values(lease_owner=None, lease_until=None))
        db.commit()


class QueueWorker:
    def __init__(self):
        self.parallel = max(1, min(get_settings().readiness_parallel_browsers, 12))
        try:
            pid_limit = Path('/sys/fs/cgroup/pids.max').read_text().strip()
            if pid_limit != 'max':
                # Each Chromium also needs renderer/network/utility threads.
                # Reserve capacity for the API, database and control channel.
                self.parallel = min(self.parallel, max(1, (int(pid_limit) - 128) // 96))
        except (OSError, ValueError):
            pass
        self.browsers = ThreadPoolExecutor(self.parallel, thread_name_prefix='readiness-browser')
        self.monitors = ThreadPoolExecutor(40, thread_name_prefix='readiness-ndr')
        self.active = {}
        self.lock = threading.Lock()
        self.stopping = False

    def provision(self, record, slot):
        from playwright.sync_api import sync_playwright
        temporary = tempfile.TemporaryDirectory(prefix='readiness-')
        store = None
        monitor_futures = []
        worker = self
        class MonitorPool:
            def submit(self, fn):
                future = worker.monitors.submit(fn)
                monitor_futures.append(future)
                return future
        def finish(_=None):
            try:
                if store:
                    state = store.state[store.tenant_id]
                    detail = state.get('detail', '').lower()
                    transient = any(marker in detail for marker in (
                        'target crashed', 'target page, context or browser has been closed',
                        'timeout', 'connection aborted', 'connection reset', 'name resolution'))
                    if state.get('status') == 'error' and transient and record.attempts < 3:
                        store.update({'id':store.tenant_id}, status='queued', detail='Retrying a temporary browser or connection failure using the saved checkpoint.')
                    STORES.pop(str(store.private), None)
                _release(record.id)
            except Exception:
                logger.exception('Could not release readiness lease %s', record.id)
            finally:
                temporary.cleanup()
                with self.lock:
                    self.active.pop(record.id, None)
        try:
            store = DatabaseStore(record, temporary.name)
            tenant = {'id': str(record.tenant_id), 'admin': record.admin_email,
                      'number': record.label, 'password': store.credentials['password']}
            args = SimpleNamespace(recheck=False, defer_auth=True, preflight=False,
                usage_location=None, provision_minutes=20, monitor_minutes=5,
                monitor_pool=MonitorPool(), profile_root=None)
            with sync_playwright() as playwright:
                engine.run_tenant(tenant, store, playwright, args)
        except Exception:
            logger.exception('Readiness worker failed for check %s', record.id)
            if store:
                try:
                    store.update(tenant, status='error', detail='Server worker failed. Saved checkpoints are retained; retry this check.')
                except Exception:
                    pass
            else:
                with SyncSessionLocal() as db:
                    db.execute(update(ReadinessCheck).where(ReadinessCheck.id == record.id,
                        ReadinessCheck.lease_owner == OWNER).values(status='error', state={
                            **record.state, 'status':'error', 'detail':'Could not restore the encrypted checkpoint.'}))
                    db.commit()
        finally:
            slot['provisioning'] = False
            if monitor_futures:
                monitor_futures[0].add_done_callback(finish)
            else:
                finish()

    def tick(self):
        with self.lock:
            ids = list(self.active)
            capacity = self.parallel - sum(s['provisioning'] for s in self.active.values())
        if ids:
            with SyncSessionLocal() as db:
                db.execute(update(ReadinessCheck).where(ReadinessCheck.id.in_(ids),
                    ReadinessCheck.lease_owner == OWNER).values(lease_until=now() + timedelta(seconds=LEASE_SECONDS)))
                db.commit()
        if capacity > 0 and not self.stopping:
            try:
                maximum = Path('/sys/fs/cgroup/pids.max').read_text().strip()
                used = int(Path('/sys/fs/cgroup/pids.current').read_text())
                if maximum != 'max':
                    capacity = min(capacity, max(0, (int(maximum) - used - 64) // 96))
            except (OSError, ValueError):
                pass
            for record in _claim(capacity):
                slot = {'provisioning': True}
                with self.lock:
                    self.active[record.id] = slot
                self.browsers.submit(self.provision, record, slot)

    async def run(self):
        while not self.stopping:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                logger.exception('Readiness queue poll failed; pending work remains in the database')
            await asyncio.sleep(5)

    def stop(self):
        self.stopping = True
        self.browsers.shutdown(wait=False, cancel_futures=True)
        self.monitors.shutdown(wait=False, cancel_futures=True)


WORKER: QueueWorker | None = None


def start_worker():
    global WORKER
    if not get_settings().readiness_operator_key:
        logger.info('Readiness worker is disabled until its operator access key is configured')
        return None
    WORKER = QueueWorker()
    return asyncio.create_task(WORKER.run())


def stop_worker():
    if WORKER:
        WORKER.stop()
