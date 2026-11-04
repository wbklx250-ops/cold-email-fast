"""Keep readiness credentials available to the app's existing tenant inventory."""
import json

import pyotp
from sqlalchemy import func, select

from app.models.tenant import Tenant
from app.models.tenant_audit import TenantAudit, TenantAuditCredential
from app.services.inventory_credentials import encrypt_inventory_value, decrypt_inventory_value


def setup_confirmed(state):
    if state.get('admin_totp_verified'):
        return True
    return bool(state.get('sender')) and state.get('stage') not in {
        'totp_registration', 'totp_seed_saved', 'totp_verifying', 'admin_authentication'}


def sync_credentials(db, record, credentials, artifacts, state=None):
    state = record.state if state is None else state
    email = record.admin_email.lower()
    audit = db.scalar(select(TenantAudit).where(func.lower(TenantAudit.admin_email) == email))
    if audit is None:
        audit = TenantAudit(admin_email=email, tenant_name=email.split('@',1)[1].removesuffix('.onmicrosoft.com'),
            disposition='unreviewed', is_used=None, verified_domains=[], unverified_domains=[], custom_domain_count=0)
        db.add(audit)
        db.flush()
    if state.get('sender'):
        audit.login_success = True
    saved = db.scalar(select(TenantAuditCredential).where(TenantAuditCredential.audit_id == audit.id))
    if saved is None:
        saved = TenantAuditCredential(audit_id=audit.id, password_ciphertext=encrypt_inventory_value(credentials['password']))
        db.add(saved)
    elif decrypt_inventory_value(saved.password_ciphertext) != credentials['password']:
        saved.password_ciphertext = encrypt_inventory_value(credentials['password'])
    seed = artifacts.get('admin-totp.dpapi')
    confirmed = setup_confirmed(state)
    if seed:
        pyotp.TOTP(seed).now()  # Validate before promoting a captured key.
        old = decrypt_inventory_value(saved.totp_ciphertext) if saved.totp_ciphertext else None
        if not old or old == seed or confirmed:
            if old != seed:
                saved.totp_ciphertext = encrypt_inventory_value(seed)
    # Update only an exact tenant/admin match; preserve pipeline status and
    # other administrators, and do not replace a working seed with pending setup.
    tenant = db.scalar(select(Tenant).where(
        func.lower(Tenant.microsoft_tenant_id) == str(record.tenant_id).lower(),
        func.lower(Tenant.admin_email) == email))
    main_updated = False
    if tenant is not None and state.get('sender'):
        tenant.admin_password = credentials['password']
        if seed and confirmed:
            tenant.totp_secret = seed
        main_updated = True
    return {'inventory_saved':1, 'totp_available':int(bool(saved.totp_ciphertext)),
            'existing_tenant_updated':int(main_updated)}
