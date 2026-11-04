"""Exchange DKIM fallback must use Microsoft-returned, domain-specific targets."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import m365_setup, objective_reconciliation
from app.services.powershell.runner import powershell
from app.services.selenium.admin_portal import (
    _get_exchange_dkim_targets,
    _graph_confirms_email_service,
)


def test_exchange_fallback_accepts_exact_new_format_targets(monkeypatch):
    selector1 = "selector1-example-com._domainkey.Tenant.y-v1.dkim.mail.microsoft"
    selector2 = "selector2-example-com._domainkey.Tenant.y-v1.dkim.mail.microsoft"
    lookup = AsyncMock(return_value=(True, selector1, selector2))
    monkeypatch.setattr(powershell, "get_dkim_config_with_credentials", lookup)

    assert _get_exchange_dkim_targets("example.com", "admin@tenant", "password") == (
        selector1, selector2
    )
    lookup.assert_awaited_once_with("admin@tenant", "password", "example.com")


def test_exchange_fallback_uses_saved_mfa_key_and_preserves_wizard(monkeypatch):
    from app.services import microsoft_mfa
    from unittest.mock import Mock
    lookup=Mock(return_value={'success':True,'selector1':'selector1-example-com._domainkey.tenant.onmicrosoft.com',
        'selector2':'selector2-example-com._domainkey.tenant.onmicrosoft.com'})
    monkeypatch.setattr(microsoft_mfa,'read_dkim',lookup)
    legacy=AsyncMock(side_effect=AssertionError('MFA accounts must not use password-only Exchange authentication'))
    monkeypatch.setattr(powershell,'get_dkim_config_with_credentials',legacy)
    driver=object()
    assert _get_exchange_dkim_targets('example.com','admin@tenant','synthetic-password','synthetic-seed',driver)
    lookup.assert_called_once_with('admin@tenant','synthetic-password','example.com','synthetic-seed',create=True,driver=driver)
    legacy.assert_not_awaited()


def test_exchange_targets_wait_for_new_domain_provisioning_without_restarting_wizard(monkeypatch):
    from app.services import microsoft_mfa
    from app.services.selenium import admin_portal
    from unittest.mock import Mock
    targets={'success':True,'selector1':'selector1-example-com._domainkey.tenant.onmicrosoft.com',
        'selector2':'selector2-example-com._domainkey.tenant.onmicrosoft.com'}
    lookup=Mock(side_effect=[{'success':False,'error':"Object couldn't be found"},targets])
    sleep=Mock()
    monkeypatch.setattr(microsoft_mfa,'read_dkim',lookup)
    monkeypatch.setattr(admin_portal.time,'sleep',sleep)
    assert _get_exchange_dkim_targets('example.com','admin@tenant','synthetic-password','synthetic-seed')
    assert lookup.call_count==2
    sleep.assert_called_once_with(15)


def test_exchange_targets_do_not_repeat_permanent_permission_failures(monkeypatch):
    from app.services import microsoft_mfa
    from app.services.selenium import admin_portal
    from unittest.mock import Mock
    lookup=Mock(return_value={'success':False,'error':'Access is denied'})
    sleep=Mock()
    monkeypatch.setattr(microsoft_mfa,'read_dkim',lookup)
    monkeypatch.setattr(admin_portal.time,'sleep',sleep)
    assert _get_exchange_dkim_targets('example.com','admin@tenant','synthetic-password','synthetic-seed') is None
    assert lookup.call_count==1
    sleep.assert_not_called()


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


def test_graph_completion_readback_requires_verified_email_service(monkeypatch):
    readback = AsyncMock(return_value={"ok": False, "is_verified": True, "supported_services": []})
    monkeypatch.setattr(m365_setup, "_read_post_wizard_domain_truth", readback)
    assert not _graph_confirms_email_service("example.com", "admin@tenant", "password")

    readback.return_value = {"ok": True, "is_verified": True, "supported_services": ["Email"]}
    assert _graph_confirms_email_service("example.com", "admin@tenant", "password")
    readback.assert_awaited_with(
        "example.com", None, {"admin_email": "admin@tenant", "admin_password": "password"}
    )


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
    saved_result = await m365_setup._save_step6_result(
        {"domain": "example.com", "domain_id": str(domain_id), "tenant_id": str(tenant_id),
         "admin_email": "admin@tenant", "admin_password": "password"},
        result,
    )

    assert not domain.step5_complete
    assert not domain.dkim_enabled
    assert domain.dkim_cnames_added
    assert domain.mx_record_added and domain.spf_record_added
    assert domain.status.value == "pending_dkim"
    assert not saved_result["success"]
    assert saved_result["dkim_pending"]
    assert "signing is pending" in saved_result["error"]


@pytest.mark.asyncio
async def test_pending_dkim_retry_uses_live_dns_without_browser_or_dns_writes(monkeypatch):
    values = {
        "mx_value": "example-com.mail.protection.outlook.com",
        "spf_value": "v=spf1 include:spf.protection.outlook.com -all",
        "dkim_selector1_cname": "selector1-example-com._domainkey.tenant.dkim.mail.microsoft",
        "dkim_selector2_cname": "selector2-example-com._domainkey.tenant.dkim.mail.microsoft",
    }
    data = {"domain": "example.com", "zone_id": "zone", "dkim_only_retry": True,
            "expected_dns_values": values}
    truth = {key: True for key in
             ("zone_ok", "mx", "spf", "autodiscover", "dkim1", "dkim2", "dmarc")}
    truth["zone_id"] = "zone"
    readback = AsyncMock(return_value=truth)
    monkeypatch.setattr(objective_reconciliation, "_ensure_cloudflare_truth", readback)
    monkeypatch.setattr(m365_setup, "cloudflare_service", SimpleNamespace(list_dns_records=AsyncMock(return_value=[
        {"type": "MX", "name": "example.com", "content": values["mx_value"]},
        {"type": "TXT", "name": "example.com", "content": '"' + values["spf_value"] + '"'},
    ])))
    def unexpected_browser(*_):
        raise AssertionError("DKIM-only retry must not start the domain wizard")
    monkeypatch.setattr(m365_setup, "_sync_setup_domain", unexpected_browser)
    result = await m365_setup._run_domain_setup(data)
    assert result["dns_configured"] and result["dmarc_configured"]
    assert result["dkim_selector1_cname"] == values["dkim_selector1_cname"]
    assert readback.await_args.kwargs["auto_fix"] is False


@pytest.mark.asyncio
async def test_pending_dkim_retry_returns_to_wizard_when_dns_is_missing(monkeypatch):
    from unittest.mock import Mock
    data = {"domain": "example.com", "zone_id": "zone", "dkim_only_retry": True,
            "expected_dns_values": {"mx_value": "mx", "spf_value": "spf",
                                    "dkim_selector1": "selector1", "dkim_selector2": "selector2"}}
    monkeypatch.setattr(objective_reconciliation, "_ensure_cloudflare_truth",
                        AsyncMock(return_value={"zone_ok": True, "dkim1": False}))
    wizard = Mock(return_value={"success": False, "error": "wizard retry"})
    monkeypatch.setattr(m365_setup, "_sync_setup_domain", wizard)
    result = await m365_setup._run_domain_setup(data)
    wizard.assert_called_once_with(data)
    assert result["error"] == "wizard retry"


@pytest.mark.asyncio
async def test_pending_dkim_retry_rejects_mx_from_another_domain(monkeypatch):
    values = {"mx_value": "example-com.mail.protection.outlook.com", "spf_value": "spf",
              "dkim_selector1": "selector1", "dkim_selector2": "selector2"}
    data = {"domain": "example.com", "zone_id": "zone", "dkim_only_retry": True,
            "expected_dns_values": values}
    truth = {key: True for key in
             ("zone_ok", "mx", "spf", "autodiscover", "dkim1", "dkim2", "dmarc")}
    truth["zone_id"] = "zone"
    monkeypatch.setattr(objective_reconciliation, "_ensure_cloudflare_truth", AsyncMock(return_value=truth))
    monkeypatch.setattr(m365_setup, "cloudflare_service", SimpleNamespace(list_dns_records=AsyncMock(return_value=[
        {"type": "MX", "name": "example.com", "content": "other-com.mail.protection.outlook.com"},
        {"type": "TXT", "name": "example.com", "content": "spf"},
    ])))
    assert await m365_setup._pending_dkim_retry_result(data) is None


@pytest.mark.asyncio
async def test_focused_dkim_retry_waits_for_exchange_dns_propagation(monkeypatch):
    readback = AsyncMock(side_effect=[
        {"success": False, "enabled": False, "error": "CNAME record does not exist"},
        {"success": True, "enabled": False},
        {"success": True, "enabled": True},
    ])
    monkeypatch.setattr(objective_reconciliation, "_read_dkim_truth", readback)
    sleep = AsyncMock()
    monkeypatch.setattr(m365_setup.asyncio, "sleep", sleep)
    result = await m365_setup._enable_domain_dkim("example.com", {
        "dkim_only_retry": True, "admin_email": "admin@tenant", "admin_password": "password"})
    assert result["enabled"]
    assert [call.args[0] for call in sleep.await_args_list] == [30, 60]
    assert readback.await_count == 3


@pytest.mark.asyncio
async def test_dkim_retry_does_not_repeat_authentication_errors(monkeypatch):
    readback = AsyncMock(return_value={"success": False, "enabled": False, "error": "Authentication failed"})
    monkeypatch.setattr(objective_reconciliation, "_read_dkim_truth", readback)
    sleep = AsyncMock()
    monkeypatch.setattr(m365_setup.asyncio, "sleep", sleep)
    assert not (await m365_setup._enable_domain_dkim("example.com", {
        "dkim_only_retry": True, "admin_email": "admin@tenant", "admin_password": "password"}))["enabled"]
    readback.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_dkim_propagation_wait_is_bounded(monkeypatch):
    readback = AsyncMock(return_value={"success": True, "enabled": False})
    monkeypatch.setattr(objective_reconciliation, "_read_dkim_truth", readback)
    sleep = AsyncMock()
    monkeypatch.setattr(m365_setup.asyncio, "sleep", sleep)
    assert not (await m365_setup._enable_domain_dkim("example.com", {
        "dkim_only_retry": True, "admin_email": "admin@tenant", "admin_password": "password"}))["enabled"]
    assert readback.await_count == 5
    assert sum(call.args[0] for call in sleep.await_args_list) == 300

def test_optional_controls_do_not_reintroduce_slow_implicit_waits():
    from app.services.selenium import admin_portal
    class Driver:
        wait=0
        def implicitly_wait(self,value):
            assert value==0, 'Optional probes must not enable blocking implicit waits'
            self.wait=value
        def find_elements(self,*args):
            assert self.wait==0
            return []
        def execute_script(self,*args):return 'Microsoft 365 admin center'
    driver=Driver()
    assert admin_portal._find_first_visible(driver,[('css selector','#optional-'+str(i)) for i in range(10)])==(None,None)
    assert admin_portal._dismiss_microsoft_feedback_prompt(driver) is False
    assert admin_portal.dismiss_mfa_setup_interrupt(driver) is False
    assert driver.wait==0
