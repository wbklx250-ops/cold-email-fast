from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.routes.domain_checker import (
    TenantAuditUpdate,
    _parse_credentials,
    _persist_audit_results,
    update_inventory_disposition,
)
from app.models.tenant_audit import TenantAudit, TenantDisposition


def test_parse_credentials_accepts_pasted_and_headed_rows():
    pasted = _parse_credentials(
        "Admin@One.onmicrosoft.com,p@ss,TOTP1\nadmin@two.onmicrosoft.com,p@ss2"
    )
    headed = _parse_credentials(
        "Email,Password,TOTP Secret\nAdmin@Three.onmicrosoft.com,p@ss3,TOTP3"
    )

    assert [row["admin_email"] for row in pasted] == [
        "admin@one.onmicrosoft.com",
        "admin@two.onmicrosoft.com",
    ]
    assert pasted[0]["totp_secret"] == "TOTP1"
    assert headed[0]["admin_email"] == "admin@three.onmicrosoft.com"
    assert headed[0]["admin_password"] == "p@ss3"


async def test_audit_upsert_detects_usage_and_preserves_disposition(test_engine):
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    first_result = {
        "admin_email": "Admin@One.onmicrosoft.com",
        "tenant_name": "one",
        "login_success": True,
        "login_error": "",
        "verified_domains": [{"name": "example.com", "status": "Healthy"}],
        "unverified_domains": [],
        "custom_domain_count": 1,
    }
    await _persist_audit_results("job-one", [first_result], session_factory)

    async with session_factory() as db:
        audit = (await db.execute(select(TenantAudit))).scalar_one()
        assert audit.is_used is True
        assert audit.disposition == TenantDisposition.UNREVIEWED.value
        audit.disposition = TenantDisposition.BURNED.value
        await db.commit()

    second_result = {
        **first_result,
        "admin_email": "admin@one.onmicrosoft.com",
        "verified_domains": [],
        "custom_domain_count": 0,
    }
    await _persist_audit_results("job-two", [second_result], session_factory)

    async with session_factory() as db:
        audit = (await db.execute(select(TenantAudit))).scalar_one()
        assert audit.is_used is False
        assert audit.disposition == TenantDisposition.BURNED.value
        assert audit.last_job_id == "job-two"


async def test_failed_login_has_unknown_usage_and_disposition_can_change(test_session):
    from datetime import datetime, timezone

    audit = TenantAudit(
        admin_email="admin@unknown.onmicrosoft.com",
        tenant_name="unknown",
        disposition=TenantDisposition.UNREVIEWED.value,
        login_success=False,
        login_error="Bad password",
        is_used=None,
        verified_domains=[],
        unverified_domains=[],
        custom_domain_count=0,
        last_checked_at=datetime.now(timezone.utc),
    )
    test_session.add(audit)
    await test_session.commit()
    await test_session.refresh(audit)

    updated = await update_inventory_disposition(
        audit.id,
        TenantAuditUpdate(disposition=TenantDisposition.AVAILABLE),
        test_session,
    )

    assert updated.disposition == TenantDisposition.AVAILABLE.value
    assert updated.is_used is None
