"""Exercise durable claims, protected endpoints and irreversible checkpoints."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import readiness as api
from app.models.readiness import ReadinessCheck
from app.services import readiness_worker as worker
from app.services.inventory_credentials import encrypt_inventory_value, decrypt_inventory_value


@pytest.fixture
def database(monkeypatch):
    db_engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    ReadinessCheck.__table__.create(db_engine)
    factory = sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(worker, 'SyncSessionLocal', factory)
    yield factory
    db_engine.dispose()


def record(database, **extra):
    tenant_id = uuid4()
    row = ReadinessCheck(tenant_id=tenant_id, admin_email='admin@example.onmicrosoft.com', label='1',
        status='queued', state={'tenant_id':str(tenant_id), 'status':'queued'},
        credentials_ciphertext=encrypt_inventory_value(json.dumps({'password':'synthetic-password'})),
        artifacts_ciphertext=encrypt_inventory_value('{}'))
    for name,value in extra.items():
        setattr(row, name, value)
    with database() as db:
        db.add(row)
        db.commit()
    return row


def test_claim_is_exclusive_and_expired_worker_can_be_recovered(database):
    row = record(database)
    claimed = worker._claim(8)
    assert [r.id for r in claimed] == [row.id]
    assert worker._claim(8) == []
    with database() as db:
        stale = db.get(ReadinessCheck, row.id)
        stale.lease_until = worker.now() - timedelta(seconds=5)
        db.commit()
    recovered = worker._claim(8)
    assert recovered[0].id == row.id
    assert recovered[0].attempts == 2


def test_queued_retry_keeps_its_current_lease_until_cleanup_finishes(database):
    row = record(database, lease_owner=worker.OWNER, lease_until=worker.now() + timedelta(seconds=60))
    assert worker._claim(1) == []
    worker._release(row.id)
    assert worker._claim(1)[0].id == row.id


def test_send_and_auth_checkpoints_are_committed_and_survive_restore(database, tmp_path):
    row = record(database)
    claimed = worker._claim(1)[0]
    store = worker.DatabaseStore(claimed, tmp_path / 'first')
    tenant = {'id':str(row.tenant_id)}
    worker.cloud_secret(store.private / 'admin-totp.dpapi', 'JBSWY3DPEHPK3PXP')
    store.update(tenant, sent_at='2026-11-04T00:00:00+00:00', subject='unique-probe', status='running')
    with database() as db:
        saved = db.get(ReadinessCheck, row.id)
        assert 'JBSWY3DPEHPK3PXP' not in saved.artifacts_ciphertext
        assert 'synthetic-password' not in saved.credentials_ciphertext
        assert saved.state['sent_at']
    restored = worker.DatabaseStore(saved, tmp_path / 'second')
    assert restored.state[str(row.tenant_id)]['subject'] == 'unique-probe'
    assert worker.cloud_secret(restored.private / 'admin-totp.dpapi') == 'JBSWY3DPEHPK3PXP'
    assert 'JBSWY3DPEHPK3PXP' not in (restored.private / 'admin-totp.dpapi').read_text()


def test_lost_lease_prevents_send_checkpoint(database, tmp_path):
    row = record(database)
    claimed = worker._claim(1)[0]
    store = worker.DatabaseStore(claimed, tmp_path)
    with database() as db:
        stale = db.get(ReadinessCheck, row.id)
        stale.lease_owner = 'another-server'
        db.commit()
    with pytest.raises(worker.LeaseLost):
        store.update({'id':str(row.tenant_id)}, sent_at='must-not-commit', stage='sending_uncertain')
    with database() as db:
        assert 'sent_at' not in db.get(ReadinessCheck, row.id).state


@pytest.mark.asyncio
async def test_authentication_protects_results_secrets_and_browser_input(monkeypatch):
    monkeypatch.setattr(api, 'get_settings', lambda: SimpleNamespace(readiness_operator_key='test-access-key'))
    app = FastAPI()
    app.include_router(api.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://app.example') as client:
        for method,path,kwargs in [('GET','checks',{}), ('GET',f'{uuid4()}/browser',{}),
            ('POST',f'{uuid4()}/browser',{'json':{'op':'text','text':'private'}}),
            ('POST','import',{'json':{'tenants':[{'id':str(uuid4()),'admin':'a@b.com','password':'private'}]}})]:
            response = await client.request(method, '/api/v1/tenant-readiness/'+path, **kwargs)
            assert response.status_code == 401
        assert (await client.post('/api/v1/tenant-readiness/login', json={'key':'wrong'})).status_code == 401
        response = await client.post('/api/v1/tenant-readiness/login', json={'key':'test-access-key'})
        assert response.status_code == 200
        assert 'HttpOnly' in response.headers['set-cookie']
        assert 'Secure' in response.headers['set-cookie']
        assert 'test-access-key' not in response.text
        request = Mock(headers={}, cookies={api.COOKIE:client.cookies[api.COOKIE]})
        api.authorized(request)
        expired = api.session_token(int(api.time.time()) - 90000)
        with pytest.raises(api.HTTPException):
            api.authorized(Mock(headers={}, cookies={api.COOKIE:expired}))


def test_live_browser_input_is_memory_only_and_restricted(monkeypatch):
    tenant_id = uuid4()
    import queue
    live = {'waiting':True, 'actions':queue.Queue(20)}
    monkeypatch.setitem(api.LIVE, str(tenant_id), live)
    api.browser_input(tenant_id, api.BrowserAction(op='text', text='synthetic-auth-code'))
    assert live['actions'].get_nowait()['text'] == 'synthetic-auth-code'
    with pytest.raises(api.HTTPException):
        api.browser_input(tenant_id, api.BrowserAction(op='key', key='javascript:bad'))
    api.browser_input(tenant_id, api.BrowserAction(op='continue'))
    assert live['continue']


def test_checkout_diagnostic_requires_direct_operator_signature(monkeypatch):
    monkeypatch.setattr(api, 'get_settings', lambda: SimpleNamespace(readiness_operator_key='test-access-key'))
    tenant_id = uuid4()
    monkeypatch.setitem(api.LIVE, str(tenant_id), {})
    with pytest.raises(api.HTTPException) as denied:
        api.checkout_diagnostic(tenant_id, Mock(headers={'x-readiness-key':'test-access-key'}))
    assert denied.value.status_code == 403
    signature = api.hmac.new(b'test-access-key', b'checkout-diagnostic', api.hashlib.sha256).hexdigest()
    api.LIVE[str(tenant_id)]['waiting'] = False
    reply = api.checkout_diagnostic(tenant_id, Mock(headers={'x-readiness-diagnostic':signature}))
    assert json.loads(reply.body) == {'pending':True}
    assert api.LIVE[str(tenant_id)]['inspect_checkout']


def test_recurring_payment_authorization_is_limited_to_the_approved_tenants(monkeypatch):
    approved, unapproved = str(uuid4()), str(uuid4())
    settings = SimpleNamespace(readiness_accept_trial_recurring_payments=True,
                              readiness_trial_consent_tenant_ids=[approved])
    monkeypatch.setattr(worker, 'get_settings', lambda: settings)
    assert worker.recurring_consent_authorized(approved)
    assert not worker.recurring_consent_authorized(unapproved)
    settings.readiness_accept_trial_recurring_payments = False
    assert not worker.recurring_consent_authorized(approved)


def test_public_report_never_exposes_authentication_checkpoint(database):
    row = record(database, state={'password':'must-not-appear', 'admin-tokens.dpapi':'private', 'sent_at':'preserved'})
    public = api.view(row)
    assert public['sent_at'] == 'preserved'
    assert 'password' not in public
    assert 'admin-tokens.dpapi' not in public
    assert 'credentials_ciphertext' not in public
