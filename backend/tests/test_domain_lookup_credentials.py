import base64

import pyotp
import pytest

from app.api.routes import domain_lookup
from app.models.domain import Domain, DomainStatus
from app.models.tenant import Tenant, TenantStatus
from app.services.domain_lookup import DomainLookupResult


def test_build_tenant_login_details_generates_totp(monkeypatch):
    monkeypatch.setattr(domain_lookup.time, "time", lambda: 0)
    secret = "JBSWY3DPEHPK3PXP"
    tenant = Tenant(
        microsoft_tenant_id="11111111-1111-1111-1111-111111111111",
        name="Example Tenant",
        onmicrosoft_domain="example.onmicrosoft.com",
        provider="Provider",
        admin_email="admin@example.onmicrosoft.com",
        admin_password="Password123!",
        totp_secret=secret,
        status=TenantStatus.NEW,
    )

    details = domain_lookup.build_tenant_login_details(tenant)

    assert details.admin_email == "admin@example.onmicrosoft.com"
    assert details.admin_password == "Password123!"
    assert details.has_totp_secret is True
    assert details.totp_code == pyotp.TOTP(secret).at(0)
    assert details.totp_seconds_remaining == 30


def test_maybe_decode_legacy_password_only_for_likely_base64():
    encoded = base64.b64encode(b"Password123!").decode()

    assert domain_lookup.maybe_decode_legacy_password(encoded) == "Password123!"
    assert domain_lookup.maybe_decode_legacy_password("abcd") == "abcd"
    assert domain_lookup.maybe_decode_legacy_password("Password123!") == "Password123!"


@pytest.mark.asyncio
async def test_bulk_domain_credentials_lookup_returns_login_details(test_session, monkeypatch):
    tenant = Tenant(
        microsoft_tenant_id="22222222-2222-2222-2222-222222222222",
        name="Linked Tenant",
        onmicrosoft_domain="linked.onmicrosoft.com",
        provider="Provider",
        admin_email="admin@linked.onmicrosoft.com",
        admin_password="TenantPassword!",
        totp_secret="JBSWY3DPEHPK3PXP",
        status=TenantStatus.NEW,
    )
    test_session.add(tenant)
    await test_session.flush()

    domain = Domain(
        name="example.com",
        tld="com",
        status=DomainStatus.TENANT_LINKED,
        cloudflare_nameservers=[],
        cloudflare_zone_status="pending",
        tenant_id=tenant.id,
    )
    test_session.add(domain)
    await test_session.commit()

    class FakeLookupService:
        async def check_domains_bulk(self, domains):
            return [
                DomainLookupResult(
                    domain=domain_name,
                    is_connected=True,
                    microsoft_tenant_id="22222222-2222-2222-2222-222222222222",
                    organization_name="Linked Tenant",
                    namespace_type="Managed",
                )
                for domain_name in domains
            ]

    monkeypatch.setattr(domain_lookup, "DomainLookupService", FakeLookupService)

    response = await domain_lookup.bulk_domain_credentials_lookup(
        domain_lookup.BulkLookupRequest(domains=["EXAMPLE.com"]),
        db=test_session,
    )

    assert response.total == 1
    assert response.matched == 1
    assert response.credentials_found == 1
    result = response.results[0]
    assert result.domain == "example.com"
    assert result.db_tenant_name == "Linked Tenant"
    assert result.credentials is not None
    assert result.credentials.admin_email == "admin@linked.onmicrosoft.com"
    assert result.credentials.admin_password == "TenantPassword!"
    assert result.credentials.totp_code is not None
