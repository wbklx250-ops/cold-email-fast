from types import SimpleNamespace

import pytest

from app.services import batch_reconciliation


@pytest.mark.asyncio
async def test_sd_graph_read_failure_uses_selenium_fallback(monkeypatch):
    async def fake_verify_or_repair_sd(**_kwargs):
        return {
            "success": False,
            "sd_disabled": None,
            "action": "unfixable",
            "error": "Could not read SD state via Graph",
        }

    calls = []

    class FakeDisabler:
        def __init__(self, **_kwargs):
            pass

        def disable_for_tenant(self, credentials):
            calls.append(credentials.domain)
            return {"success": False, "error": "Selenium verification failed"}

    monkeypatch.setattr(
        batch_reconciliation,
        "verify_or_repair_sd",
        fake_verify_or_repair_sd,
    )

    from app.services import step8_security_defaults

    monkeypatch.setattr(
        step8_security_defaults,
        "SecurityDefaultsDisabler",
        FakeDisabler,
    )

    tenant = SimpleNamespace(
        id="12345678-1234-1234-1234-123456789abc",
        custom_domain="example.com",
        onmicrosoft_domain="example.onmicrosoft.com",
        name="example",
        admin_email="admin@example.onmicrosoft.com",
        admin_password="secret",
        totp_secret="ABC123",
    )
    summary = {
        "sd_ok": 0,
        "sd_drift_fixed": 0,
        "sd_drift_unfixable": 0,
        "errors": [],
    }

    result = await batch_reconciliation._reconcile_sd_for_tenant(
        tenant=tenant,
        summary=summary,
        auto_fix=True,
    )

    assert calls == ["example.com"]
    assert result["sd"]["action"] == "selenium_failed"
    assert summary["sd_drift_unfixable"] == 1
    assert summary["errors"][0]["stage"] == "sd_selenium_fallback"
