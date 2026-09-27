"""Exchange DKIM fallback must use Microsoft-returned, domain-specific targets."""

from unittest.mock import AsyncMock

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
