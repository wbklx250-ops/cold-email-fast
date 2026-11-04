"""Keep readiness credentials available to the app's existing tenant inventory."""
import json
from uuid import UUID

import pyotp
from sqlalchemy import func, select

from app.models.tenant import Tenant
from app.models.readiness import ReadinessCheck
from app.models.tenant_audit import TenantAudit, TenantAuditCredential
from app.services.inventory_credentials import encrypt_inventory_value, decrypt_inventory_value


def setup_confirmed(state):
    if state.get('admin_totp_verified'):
        return True
    return bool(state.get('sender')) and state.get('stage') not in {
        'totp_registration', 'totp_seed_saved', 'totp_verifying', 'admin_authentication'}


def matches_tenant(tenant, record):
    """Bind a known admin to its real ID or a matching import placeholder."""
    if tenant.admin_email.strip().lower() != record.admin_email.strip().lower():
        return False
    identity = (tenant.microsoft_tenant_id or '').strip()
    if identity.lower() == str(record.tenant_id).lower():
        return True
    if not identity.startswith('PENDING-') or not record.state.get('sender'):
        return False
    try:
        UUID(identity.removeprefix('PENDING-'))
    except ValueError:
        return False
    initial = record.state['sender'].rsplit('@', 1)[-1].lower()
    return initial.endswith('.onmicrosoft.com') and tenant.onmicrosoft_domain.strip().lower() == initial


def restore_tenant_totp(db, tenant):
    """Hydrate a missing pipeline key without changing identity or step flags."""
    if tenant.totp_secret:
        return False
    records = list(db.scalars(select(ReadinessCheck).where(
        func.lower(ReadinessCheck.admin_email) == tenant.admin_email.strip().lower()).limit(2)))
    if len(records) != 1:
        return False
    record = records[0]
    if not matches_tenant(tenant, record) or not setup_confirmed(record.state):
        return False
    seed = json.loads(decrypt_inventory_value(record.artifacts_ciphertext)).get('admin-totp.dpapi')
    if not seed:
        return False
    pyotp.TOTP(seed).now()
    tenant.totp_secret = seed
    return True


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
    # Imported accounts retain PENDING IDs until their first-login step. Match
    # those by exact admin plus verified initial domain, preserving step flags.
    tenants = db.scalars(select(Tenant).where(func.lower(Tenant.admin_email) == email)).all()
    main_updated = 0
    for tenant in tenants:
        if not matches_tenant(tenant, record) or not state.get('sender'):
            continue
        # A later batch may already have changed the password successfully.
        if not tenant.password_changed or not tenant.admin_password:
            tenant.admin_password = credentials['password']
        if seed and confirmed:
            tenant.totp_secret = seed
        main_updated += 1
    return {'inventory_saved':1, 'totp_available':int(bool(saved.totp_ciphertext)),
            'existing_tenant_updated':main_updated}
