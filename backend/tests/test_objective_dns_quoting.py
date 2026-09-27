"""Cloudflare quotes TXT content in its API even when DNS is correct."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import objective_reconciliation as reconciliation


@pytest.mark.asyncio
async def test_reconciliation_accepts_quoted_cloudflare_dmarc(monkeypatch):
    domain = "example.com"
    selector1 = "selector1-example-com._domainkey.tenant.y-v1.dkim.mail.microsoft"
    selector2 = "selector2-example-com._domainkey.tenant.y-v1.dkim.mail.microsoft"
    records = [
        {"type": "MX", "name": domain, "content": "example-com.mail.protection.outlook.com"},
        {"type": "TXT", "name": domain, "content": '"v=spf1 include:spf.protection.outlook.com -all"'},
        {"type": "CNAME", "name": f"autodiscover.{domain}", "content": "autodiscover.outlook.com"},
        {"type": "CNAME", "name": f"selector1._domainkey.{domain}", "content": selector1},
        {"type": "CNAME", "name": f"selector2._domainkey.{domain}", "content": selector2},
        {"type": "TXT", "name": f"_dmarc.{domain}", "content": '"v=DMARC1; p=none;"'},
    ]
    monkeypatch.setattr(reconciliation, "cloudflare_service", SimpleNamespace(
        get_zone_by_id=AsyncMock(return_value={"zone_id": "zone", "status": "active"}),
        list_dns_records=AsyncMock(return_value=records),
    ))
    result = await reconciliation._ensure_cloudflare_truth(
        {"name": domain, "cloudflare_zone_id": "zone"},
        {"selector1": selector1, "selector2": selector2},
        auto_fix=False,
    )
    assert all(result[key] for key in ("zone_ok", "mx", "spf", "autodiscover", "dkim1", "dkim2", "dmarc"))
