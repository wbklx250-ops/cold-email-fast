"""Exchange DKIM fallback must use Microsoft-returned, domain-specific targets."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import m365_setup, objective_reconciliation
from app.services.powershell.runner import powershell
from app.services.selenium.admin_portal import _get_exchange_dkim_targets


def test_exchange_fallback_accepts_exact_new_format_targets(monkeypatch):
    selector1 = "selector1-example-com._domainkey.Tenant.y-v1.dkim.mail.microsoft"
    selector2 = "selector2-example-com._domainkey.Tenant.y-v1.dkim.mail.microsoft"
    lookup = AsyncMock(return_value=(True, selector1, selector2))
    monkeypatch.setattr(powershell, "get_dkim_config_with_credentials", lookup)

    assert _get_exchange_dkim_targets("example.com", "admin@tenant", "password") == (
        selector1, selector2
    )
    lookup.assert_awaited_once_with("admin@tenant", "password", "example.com")


def test_exchange_fallback_accepts_microsoft_assigned_selector_suffix(monkeypatch):
    selector1 = "selector1-vesselbridgenyc-com03b._domainkey.Tenant.q-v1.dkim.mail.microsoft"
    selector2 = "selector2-vesselbridgenyc-com03b._domainkey.Tenant.q-v1.dkim.mail.microsoft"
    monkeypatch.setattr(
        powershell,
        "get_dkim_config_with_credentials",
        AsyncMock(return_value=(True, selector1, selector2)),
    )
    assert _get_exchange_dkim_targets("vesselbridge-nyc.com", "admin@tenant", "password") == (
        selector1, selector2
    )


def test_exchange_fallback_rejects_missing_or_other_domain_targets(monkeypatch):
    lookup = AsyncMock(return_value=(True, None, None))
    monkeypatch.setattr(powershell, "get_dkim_config_with_credentials", lookup)
    assert _get_exchange_dkim_targets("example.com", "admin@tenant", "password") is None

    lookup.return_value = (
        True,
        "selector1-other-com._domainkey.Tenant.y-v1.dkim.mail.microsoft",
        "selector2-example-com._domainkey.Tenant.y-v1.dkim.mail.microsoft",
    )
    assert _get_exchange_dkim_targets("example.com", "admin@tenant", "password") is None


class _FakeSession:
    def __init__(self, domain, tenant):
        self.domain = domain
        self.tenant = tenant

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, model, record_id):
        return self.domain if model.__name__ == "Domain" else self.tenant

    async def commit(self):
        return None

    async def refresh(self, record):
        return None


@pytest.mark.asyncio
async def test_wizard_completion_keeps_dkim_pending_until_exchange_enables_it(monkeypatch):
    domain_id, tenant_id = uuid4(), uuid4()
    domain = SimpleNamespace(id=domain_id, dkim_enabled=False)
    tenant = SimpleNamespace(id=tenant_id)
    session = _FakeSession(domain, tenant)
    monkeypatch.setattr(m365_setup, "BackgroundSessionLocal", lambda: session)
    monkeypatch.setattr(
        m365_setup,
        "_read_post_wizard_domain_truth",
        AsyncMock(return_value={"ok": True, "exists": True, "is_verified": True,
                                "supported_services": ["Email"]}),
    )
    monkeypatch.setattr(
        objective_reconciliation,
        "_read_dkim_truth",
        AsyncMock(return_value={"success": True, "enabled": False}),
    )
    monkeypatch.setattr(m365_setup, "_sync_tenant_step5_state", AsyncMock())
    result = {
        "success": True,
        "verified": True,
        "dns_configured": True,
        "dmarc_configured": True,
        "mx_value": "example-com.mail.protection.outlook.com",
        "spf_value": "v=spf1 include:spf.protection.outlook.com -all",
        "dkim_selector1_cname": "selector1-example-com._domainkey.tenant.y-v1.dkim.mail.microsoft",
        "dkim_selector2_cname": "selector2-example-com._domainkey.tenant.y-v1.dkim.mail.microsoft",
    }
    await m365_setup._save_step6_result(
        {"domain": "example.com", "domain_id": str(domain_id), "tenant_id": str(tenant_id),
         "admin_email": "admin@tenant", "admin_password": "password"},
        result,
    )

    assert not domain.step5_complete
    assert not domain.dkim_enabled
    assert domain.dkim_cnames_added
    assert domain.mx_record_added and domain.spf_record_added
    assert domain.status.value == "pending_dkim"
