from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from fastapi import BackgroundTasks, HTTPException
import pytest

from app.api.routes.domain_checker import (
    TenantInventoryCreate,
    TenantAuditUpdate,
    _parse_credentials,
    _persist_audit_results,
    add_inventory_tenant,
    check_saved_inventory_tenant,
    list_inventory,
    update_inventory_disposition,
)
from app.models.tenant_audit import TenantAudit, TenantAuditCredential, TenantDisposition
from app.services.inventory_credentials import decrypt_inventory_value


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


async def test_manual_tenant_keeps_assignment_and_encrypts_saved_credentials(test_session):
    created = await add_inventory_tenant(TenantInventoryCreate(
        admin_email="Admin@one.onmicrosoft.com",
        admin_password=" password with spaces ",
        totp_secret="JBSWY3DPEHPK3PXP",
        assigned_custom_domain="Example.COM",
        disposition=TenantDisposition.BURNED,
    ), test_session)
    assert created.assigned_custom_domain == "example.com"
    assert created.disposition == "burned"
    assert created.last_checked_at is None
    assert created.has_saved_credentials is True
    assert "password" not in created.model_dump()

    credential = (await test_session.execute(select(TenantAuditCredential))).scalar_one()
    assert credential.password_ciphertext != " password with spaces "
    assert decrypt_inventory_value(credential.password_ciphertext) == " password with spaces "
    assert decrypt_inventory_value(credential.totp_ciphertext) == "JBSWY3DPEHPK3PXP"

    # Adding credentials again must not silently turn a burned tenant available.
    updated = await add_inventory_tenant(TenantInventoryCreate(
        admin_email="admin@one.onmicrosoft.com", admin_password="new-password",
    ), test_session)
    assert updated.disposition == "burned"
    assert updated.assigned_custom_domain == "example.com"
    await test_session.refresh(credential)
    assert decrypt_inventory_value(credential.totp_ciphertext) == "JBSWY3DPEHPK3PXP"
    assert len(await list_inventory(None, test_session)) == 1


async def test_domain_assignment_rejects_another_tenant_and_survives_check(test_session):
    one = await add_inventory_tenant(TenantInventoryCreate(
        admin_email="admin@one.onmicrosoft.com", admin_password="first",
        assigned_custom_domain="example.com",
    ), test_session)
    two = await add_inventory_tenant(TenantInventoryCreate(
        admin_email="admin@two.onmicrosoft.com", admin_password="second",
    ), test_session)
    with pytest.raises(HTTPException) as error:
        await update_inventory_disposition(
            two.id, TenantAuditUpdate(assigned_custom_domain="example.com"), test_session,
        )
    assert error.value.status_code == 409

    await _persist_audit_results("job-checked", [{
        "admin_email": "admin@one.onmicrosoft.com", "login_success": True,
        "domain_check_success": True, "login_error": "", "custom_domain_count": 0,
    }], async_sessionmaker(test_session.bind, expire_on_commit=False))
    await test_session.refresh((await test_session.execute(
        select(TenantAudit).where(TenantAudit.id == one.id)
    )).scalar_one())
    read = await list_inventory(None, test_session)
    assigned = next(item for item in read if item.id == one.id)
    assert assigned.assigned_custom_domain == "example.com"
    assert assigned.is_used is False  # App assignment is not an M365 discovery.


async def test_saved_tenant_check_uses_decrypted_credentials(test_session):
    from app.api.routes.domain_checker import checker_jobs

    created = await add_inventory_tenant(TenantInventoryCreate(
        admin_email="admin@one.onmicrosoft.com", admin_password="top secret",
        totp_secret="JBSWY3DPEHPK3PXP",
    ), test_session)
    tasks = BackgroundTasks()
    response = await check_saved_inventory_tenant(created.id, tasks, test_session)
    assert response["total_tenants"] == 1
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].args[1] == [{
        "admin_email": "admin@one.onmicrosoft.com",
        "admin_password": "top secret",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    }]
    checker_jobs.pop(response["job_id"])


async def test_inventory_http_create_assign_and_list_without_credentials(test_engine):
    from httpx import ASGITransport, AsyncClient
    from app.api.deps import get_db
    from app.main import app

    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def override_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/v1/domain-checker/inventory/tenants", json={
                "admin_email": "admin@one.onmicrosoft.com", "admin_password": "secret-password",
                "totp_secret": "JBSWY3DPEHPK3PXP", "assigned_custom_domain": "example.com",
                "disposition": "burned",
            })
            assert created.status_code == 201
            data = created.json()
            assert data["disposition"] == "burned"
            assert data["assigned_custom_domain"] == "example.com"
            assert data["has_saved_credentials"] is True
            assert "secret-password" not in created.text
            assert "JBSWY3DPEHPK3PXP" not in created.text

            cleared = await client.patch(f"/api/v1/domain-checker/inventory/{data['id']}", json={
                "assigned_custom_domain": None,
            })
            assert cleared.status_code == 200
            assert cleared.json()["assigned_custom_domain"] is None
            assert cleared.json()["disposition"] == "burned"

            listed = await client.get("/api/v1/domain-checker/inventory")
            assert listed.status_code == 200
            assert listed.json()[0]["has_saved_credentials"] is True
            assert "secret-password" not in listed.text
    finally:
        app.dependency_overrides.clear()


async def test_audit_upsert_detects_usage_and_preserves_disposition(test_engine):
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    first_result = {
        "admin_email": "Admin@One.onmicrosoft.com",
        "tenant_name": "one",
        "login_success": True,
        "domain_check_success": True,
        "login_error": "",
        "verified_domains": [{"name": "example.com", "status": "Healthy"}],
        "unverified_domains": [],
        "custom_domain_count": 1,
    }
    await _persist_audit_results("job-one", [first_result], session_factory)

    async with session_factory() as db:
        audit = (await db.execute(select(TenantAudit))).scalar_one()
        assert audit.is_used is True
        assert audit.domain_check_success is True
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


async def test_successful_login_with_failed_domain_read_is_unknown(test_engine):
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    await _persist_audit_results("incomplete", [{
        "admin_email": "admin@one.onmicrosoft.com",
        "login_success": True,
        "domain_check_success": False,
        "login_error": "Domain table did not load",
        "custom_domain_count": 0,
    }], session_factory)
    async with session_factory() as db:
        audit = (await db.execute(select(TenantAudit))).scalar_one()
        assert audit.login_success is True
        assert audit.domain_check_success is False
        assert audit.is_used is None


async def test_legacy_result_without_domain_read_confirmation_is_unknown(test_engine):
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    await _persist_audit_results("legacy", [{
        "admin_email": "admin@one.onmicrosoft.com",
        "login_success": True,
        "custom_domain_count": 0,
    }], session_factory)
    async with session_factory() as db:
        audit = (await db.execute(select(TenantAudit))).scalar_one()
        assert audit.is_used is None


def test_migration_invalidates_legacy_empty_checks_and_preserves_disposition():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    path = Path(__file__).parents[1] / "alembic/versions/030_domain_check_success.py"
    spec = importlib.util.spec_from_file_location("domain_check_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE tenant_audits (
                admin_email TEXT, login_success BOOLEAN, login_error TEXT,
                is_used BOOLEAN, custom_domain_count INTEGER, disposition TEXT
            )
        """))
        connection.execute(text("""
            INSERT INTO tenant_audits VALUES
                ('empty', true, '', false, 0, 'available'),
                ('used', true, '', true, 1, 'burned'),
                ('failed', true, 'Read failed', false, 0, 'unreviewed')
        """))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        rows = {row.admin_email: row for row in connection.execute(text("SELECT * FROM tenant_audits"))}
        assert rows["empty"].is_used is None
        assert not rows["empty"].domain_check_success
        assert "fresh tenant check" in rows["empty"].login_error
        assert rows["empty"].disposition == "available"
        assert rows["used"].domain_check_success
        assert rows["used"].is_used
        assert rows["used"].disposition == "burned"
        assert rows["failed"].login_error == "Read failed"
    engine.dispose()
