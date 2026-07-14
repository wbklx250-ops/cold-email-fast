from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routes import domains as domains_routes
from app.api.routes import tenants as tenants_routes
from app.api.routes import wizard as wizard_routes
from app.services.selenium.admin_portal import _is_domain_setup_complete_page_text
from app.services.selenium.wizard_completion import DomainWizardCompleter


def test_domain_setup_complete_detection_is_not_loose_complete_match():
    assert _is_domain_setup_complete_page_text("Domain setup is complete")
    assert not _is_domain_setup_complete_page_text("Complete the following DNS records")
    assert not _is_domain_setup_complete_page_text("Setup is complete, but DKIM doesn't match")


@pytest.mark.asyncio
async def test_legacy_m365_dns_endpoints_are_disabled():
    disabled_calls = [
        tenants_routes.bulk_add_domains_to_m365(),
        tenants_routes.bulk_setup_dns(),
        tenants_routes.bulk_setup_dkim(),
        domains_routes.create_dns_records(uuid4()),
        wizard_routes.batch_setup_m365(uuid4()),
        wizard_routes.batch_setup_dkim(uuid4()),
        wizard_routes.wizard_setup_m365(),
        wizard_routes.wizard_setup_dkim(),
        wizard_routes.mark_domain_verified(uuid4(), uuid4()),
        wizard_routes.add_mail_dns(uuid4(), uuid4()),
        wizard_routes.save_dkim_values(uuid4(), uuid4(), "selector1", "selector2"),
        wizard_routes.mark_dkim_enabled(uuid4(), uuid4()),
        wizard_routes.mark_tenant_step5_complete(uuid4(), uuid4()),
        wizard_routes.mark_bulk_tenants_step5_complete(uuid4(), domains=["example.com"]),
    ]

    for call in disabled_calls:
        with pytest.raises(HTTPException) as exc_info:
            await call
        assert exc_info.value.status_code == 410


class _FakeCloudflareService:
    def __init__(self, *, dkim_errors=False):
        self.dkim_errors = dkim_errors

    async def get_dns_records(self, zone_id, record_type=None):
        return []

    async def delete_dns_record(self, zone_id, record_id):
        return True

    async def ensure_mx_record(self, zone_id, name, target, priority, domain):
        return "mx-id"

    async def replace_spf_record(self, zone_id, domain, spf_value):
        return "spf-id"

    async def ensure_autodiscover_cname(self, zone_id, domain, target):
        return "autodiscover-id"

    async def ensure_dkim_cnames(self, zone_id, domain, selector1, selector2):
        if self.dkim_errors:
            return {"selector1_id": "selector1-id", "selector2_id": None, "errors": ["selector2 failed"]}
        return {"selector1_id": "selector1-id", "selector2_id": "selector2-id", "errors": []}

    async def ensure_txt_record(self, zone_id, name, content, domain):
        return "dmarc-id"


def _completer():
    return DomainWizardCompleter(driver=object(), domain_name="example.com")


@pytest.mark.asyncio
async def test_wizard_dns_writer_requires_every_record():
    with pytest.raises(RuntimeError, match="missing required DNS values"):
        await _completer()._add_dns_to_cloudflare(
            cloudflare_zone_id="zone",
            cloudflare_service=_FakeCloudflareService(),
            mx_value="example-com.mail.protection.outlook.com",
            spf_value=None,
            autodiscover_value="autodiscover.outlook.com",
            dkim_selector1="selector1-example._domainkey.tenant.dkim.mail.microsoft",
            dkim_selector2="selector2-example._domainkey.tenant.dkim.mail.microsoft",
        )


@pytest.mark.asyncio
async def test_wizard_dns_writer_fails_on_dkim_write_error():
    with pytest.raises(RuntimeError, match="dkim_selector2"):
        await _completer()._add_dns_to_cloudflare(
            cloudflare_zone_id="zone",
            cloudflare_service=_FakeCloudflareService(dkim_errors=True),
            mx_value="example-com.mail.protection.outlook.com",
            spf_value="v=spf1 include:spf.protection.outlook.com -all",
            autodiscover_value="autodiscover.outlook.com",
            dkim_selector1="selector1-example._domainkey.tenant.dkim.mail.microsoft",
            dkim_selector2="selector2-example._domainkey.tenant.dkim.mail.microsoft",
        )


@pytest.mark.asyncio
async def test_wizard_dns_writer_succeeds_only_when_all_writes_succeed():
    result = await _completer()._add_dns_to_cloudflare(
        cloudflare_zone_id="zone",
        cloudflare_service=_FakeCloudflareService(),
        mx_value="example-com.mail.protection.outlook.com",
        spf_value="v=spf1 include:spf.protection.outlook.com -all",
        autodiscover_value="autodiscover.outlook.com",
        dkim_selector1="selector1-example._domainkey.tenant.dkim.mail.microsoft",
        dkim_selector2="selector2-example._domainkey.tenant.dkim.mail.microsoft",
    )

    assert result == {
        "mx": True,
        "spf": True,
        "autodiscover": True,
        "dkim_selector1": True,
        "dkim_selector2": True,
        "dmarc": True,
    }
