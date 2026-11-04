import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import Response
import pyotp
import pytest
from sqlalchemy import select

from app.api.routes import domain_lookup, readiness
from app.models.readiness import ReadinessCheck
from app.models.tenant import Tenant, TenantStatus
from app.models.tenant_audit import TenantAudit, TenantAuditCredential
from app.services.inventory_credentials import encrypt_inventory_value, decrypt_inventory_value
from app.services.readiness_credentials import sync_credentials

SEED = 'JBSWY3DPEHPK3PXP'
OTHER_SEED = 'KRSXG5DSNFXGOIDB'


def checkpoint(tenant_id, state=None):
    return ReadinessCheck(tenant_id=tenant_id, admin_email='admin@example.onmicrosoft.com', label='1',
        status='needs_user', state=state or {'sender':'admin@example.onmicrosoft.com','stage':'trial_renewal_settings'},
        credentials_ciphertext=encrypt_inventory_value(json.dumps({'password':'synthetic-password'})),
        artifacts_ciphertext=encrypt_inventory_value(json.dumps({'admin-totp.dpapi':SEED})))


@pytest.mark.asyncio
async def test_synced_encrypted_credentials_are_available_in_the_existing_lookup(test_session, monkeypatch):
    record = checkpoint(uuid4())
    test_session.add(record)
    await test_session.commit()
    result = await test_session.run_sync(lambda db: sync_credentials(db, record,
        {'password':'synthetic-password'}, {'admin-totp.dpapi':SEED}))
    await test_session.commit()
    assert result['totp_available'] == 1
    saved = await test_session.scalar(select(TenantAuditCredential))
    assert SEED not in saved.totp_ciphertext
    assert 'synthetic-password' not in saved.password_ciphertext
    monkeypatch.setattr(domain_lookup.time, 'time', lambda: 0)
    response = Response()
    details = await domain_lookup.tenant_credentials_by_email(
        domain_lookup.TenantEmailLookupRequest(email='ADMIN@example.onmicrosoft.com'), response, test_session)
    assert details.admin_password == 'synthetic-password'
    assert details.totp_code == pyotp.TOTP(SEED).at(0)
    assert details.microsoft_tenant_id == str(record.tenant_id)
    assert SEED not in details.model_dump_json()
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.asyncio
async def test_existing_tenant_updates_preserve_status_and_pending_seed_does_not_replace_verified_seed(test_session):
    tenant_id = uuid4()
    tenant = Tenant(microsoft_tenant_id=str(tenant_id), name='Existing', onmicrosoft_domain='example.onmicrosoft.com',
        provider='existing', admin_email='admin@example.onmicrosoft.com', admin_password='old-password',
        initial_password='original-password', totp_secret=OTHER_SEED, status=TenantStatus.ACTIVE)
    record = checkpoint(tenant_id, {'sender':'admin@example.onmicrosoft.com','stage':'totp_registration'})
    test_session.add_all([tenant,record])
    await test_session.commit()
    await test_session.run_sync(lambda db: sync_credentials(db, record,
        {'password':'synthetic-password'}, {'admin-totp.dpapi':SEED}))
    await test_session.commit()
    assert tenant.status == TenantStatus.ACTIVE
    assert tenant.initial_password == 'original-password'
    assert tenant.totp_secret == OTHER_SEED
    record.state = {**record.state, 'admin_totp_verified':True}
    await test_session.run_sync(lambda db: sync_credentials(db, record,
        {'password':'synthetic-password'}, {'admin-totp.dpapi':SEED}))
    await test_session.commit()
    assert tenant.totp_secret == SEED
    assert tenant.status == TenantStatus.ACTIVE


@pytest.mark.asyncio
async def test_authorized_fresh_attempt_archives_old_order_and_can_only_be_used_once(test_session, monkeypatch):
    tenant_id = uuid4()
    record = checkpoint(tenant_id)
    original = {'product':'Microsoft 365 Business Basic','button':'Place order','attempted_at':'2026-11-03T00:00:00Z'}
    record.artifacts_ciphertext = encrypt_inventory_value(json.dumps({
        'admin-totp.dpapi':SEED, 'trial-order-pending.json':json.dumps(original)}))
    test_session.add(record)
    await test_session.commit()
    settings = SimpleNamespace(readiness_operator_key='test-key', readiness_trial_consent_tenant_ids=[str(tenant_id)])
    monkeypatch.setattr(readiness, 'get_settings', lambda: settings)
    signature = readiness.hmac.new(b'test-key', b'checkout-diagnostic', readiness.hashlib.sha256).hexdigest()
    request = SimpleNamespace(headers={'x-readiness-diagnostic':signature})
    body = readiness.FreshCheckoutRequest(tenant_ids=[tenant_id], confirm_duplicate_subscription_risk=True)
    result = await readiness.reconcile_unconfirmed(body, request, test_session)
    assert result['queued'] == [str(tenant_id)]
    await test_session.refresh(record)
    artifacts = json.loads(decrypt_inventory_value(record.artifacts_ciphertext))
    assert 'trial-order-pending.json' not in artifacts
    assert artifacts['admin-totp.dpapi'] == SEED
    assert json.loads(artifacts['trial-order-history.json'])[0]['prior_attempt'] == original
    assert record.status == 'queued'
    result = await readiness.reconcile_unconfirmed(body, request, test_session)
    assert result['queued'] == []
    assert result['held'] == [str(tenant_id)]
