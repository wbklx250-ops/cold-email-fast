from uuid import uuid4, UUID
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.batch import SetupBatch, BatchStatus
from app.models.domain import Domain, DomainStatus
from app.models.domain_swap import DomainSwapJob, DomainSwapReservation
from app.models.mailbox import Mailbox, MailboxStatus, WarmupStage
from app.models.tenant import Tenant, TenantStatus
from app.services import domain_swap as swaps
from app.services.domain_removal_service import domain_removal_service


async def seed(db, name="old.example", tenant=None, batch=None):
    if not batch:
        batch = SetupBatch(name="Original", status=BatchStatus.COMPLETED, pipeline_status="completed",
                           persona_first_name="Sam", persona_last_name="Taylor", mailboxes_per_tenant=2)
        db.add(batch)
        await db.flush()
    if not tenant:
        suffix = uuid4().hex[:6]
        tenant = Tenant(name=f"Tenant {suffix}", microsoft_tenant_id=str(uuid4()),
                        onmicrosoft_domain=f"{suffix}.onmicrosoft.com", provider="test",
                        admin_email=f"admin@{suffix}.onmicrosoft.com", admin_password="admin-secret",
                        first_login_completed=True, status=TenantStatus.READY, batch_id=batch.id,
                        step6_complete=True, step7_smtp_auth_enabled=True,
                        licensed_user_upn=f"me1@{name}", licensed_user_id="old-user",
                        licensed_user_password="old-secret", licensed_user_created=True, license_assigned=True)
        db.add(tenant)
        await db.flush()
    domain = Domain(name=name, tld="example", status=DomainStatus.ACTIVE, cloudflare_zone_status="active",
                    cloudflare_nameservers=[], batch_id=batch.id, tenant_id=tenant.id,
                    domain_added_to_m365=True, domain_verified_in_m365=True, step5_complete=True,
                    step6_complete=True, licensed_user_created=True, licensed_user_id="old-user",
                    licensed_user_upn=f"me1@{name}", licensed_user_password="old-secret",
                    redirect_url="https://company.example")
    db.add(domain)
    await db.flush()
    if not tenant.domain_id:
        tenant.domain_id, tenant.custom_domain = domain.id, domain.name
    for local in ("sam", "sam.taylor"):
        db.add(Mailbox(email=f"{local}@{name}", display_name="Sam Taylor", password="mail-secret",
                       tenant_id=tenant.id, batch_id=batch.id, status=MailboxStatus.READY, warmup_stage=WarmupStage.NONE))
    await db.commit()
    return domain, tenant, batch


async def plan(db, sources, replacements):
    rows, errors = await swaps.build_plan(db, sources, replacements)
    assert not errors, errors
    job = DomainSwapJob(name="Swap test", status="preview", mappings=rows)
    db.add(job)
    await db.commit()
    return job


def install_runner(monkeypatch, engine, pipeline_status="completed"):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(swaps, "SessionLocal", factory)
    monkeypatch.setattr(swaps, "check_replacement", AsyncMock())
    from app.api.routes import pipeline

    async def fake_pipeline(batch_id, start_from_step=1):
        async with factory() as db:
            batch = await db.get(SetupBatch, batch_id)
            batch.pipeline_status = pipeline_status
            batch.pipeline_step = 2 if pipeline_status == "paused" else 11
            batch.pipeline_step_name = "Update nameservers" if pipeline_status == "paused" else "Complete"
            await db.commit()
    pipeline_mock = AsyncMock(side_effect=fake_pipeline)
    monkeypatch.setattr(pipeline, "run_pipeline", pipeline_mock)
    cleanup = AsyncMock(return_value={"steps": {
        "license_cleanup": {"success": True}, "m365_removal": {"success": True},
        "cloudflare_cleanup": {"success": True},
    }})
    monkeypatch.setattr(domain_removal_service, "_execute_removal", cleanup)
    return factory, cleanup, pipeline_mock


@pytest.mark.parametrize("invalid", ["https://new.example", "new.example/path", "me@new.example", "tenant.onmicrosoft.com", "-new.example", "new..example", "example.123"])
def test_reject_non_custom_domains(invalid):
    with pytest.raises(swaps.SwapConflict):
        swaps.domain_name(invalid)


def test_domain_normalization():
    assert swaps.domain_name(" NEW.Example. ") == "new.example"
    assert swaps.domain_name("café.example") == "xn--caf-dma.example"


async def test_preview_identifiers_preserve_mailbox_names_without_credentials(test_session):
    domain, tenant, batch = await seed(test_session)
    for source in (domain.name.upper(), tenant.name, tenant.onmicrosoft_domain, tenant.admin_email,
                   tenant.microsoft_tenant_id, str(tenant.id)):
        rows, errors = await swaps.build_plan(test_session, [source], ["NEW.EXAMPLE"])
        assert not errors
        assert rows[0]["tenant_id"] == str(tenant.id)
        assert rows[0]["mailbox_count"] == 2
        assert {m["email"] for m in rows[0]["mailbox_map"]} == {"sam@new.example", "sam.taylor@new.example"}
        assert "secret" not in str(rows)
    assert domain.tenant_id == tenant.id
    assert tenant.batch_id == batch.id


async def test_reject_duplicate_resolved_sources_and_targets(test_session):
    old, tenant, batch = await seed(test_session)
    _, errors = await swaps.build_plan(test_session, [old.name, tenant.admin_email], ["new.example", "new.example"])
    assert len(errors) == 2
    with pytest.raises(swaps.SwapConflict, match="same nonzero"):
        await swaps.build_plan(test_session, [old.name], [])


async def test_ambiguous_tenant_and_linked_replacement_are_blocked(test_session):
    old, tenant, batch = await seed(test_session)
    second, _, _ = await seed(test_session, "second.example", tenant=tenant, batch=batch)
    _, errors = await swaps.build_plan(test_session, [tenant.onmicrosoft_domain], ["new.example"])
    assert "multiple domains" in errors[0]["error"]
    _, errors = await swaps.build_plan(test_session, [old.name], [second.name])
    assert "already linked" in errors[0]["error"]
    _, errors = await swaps.build_plan(test_session, [old.name], ["new.example"])
    assert not errors


async def test_start_revalidates_and_reserves_without_external_work(test_session):
    old, tenant, _ = await seed(test_session)
    job = await plan(test_session, [old.name], ["new.example"])
    old.redirect_url = "https://changed.example"
    await test_session.commit()
    with pytest.raises(swaps.SwapConflict, match="changed since preview"):
        await swaps.start_plan(test_session, job)
    job = await plan(test_session, [old.name], ["new.example"])
    await swaps.start_plan(test_session, job)
    assert job.status == "queued"
    assert len((await test_session.scalars(select(DomainSwapReservation))).all()) == 3
    assert (await swaps.start_plan(test_session, job)).id == job.id
    _, errors = await swaps.build_plan(test_session, [old.name], ["different.example"])
    assert "reserved" in errors[-1]["error"]
    assert old.tenant_id == tenant.id


@pytest.mark.parametrize("failed_step", ["license_cleanup", "m365_removal", "cloudflare_cleanup"])
async def test_cleanup_failure_never_provisions_or_unlinks(test_session, test_engine, monkeypatch, failed_step):
    old, tenant, batch = await seed(test_session)
    job = await plan(test_session, [old.name], ["new.example"])
    await swaps.start_plan(test_session, job)
    factory, cleanup, pipeline = install_runner(monkeypatch, test_engine)
    cleanup.return_value["steps"][failed_step] = {"success": False, "error": "simulated failure"}
    await swaps.run_job(job.id)
    async with factory() as db:
        saved = await db.get(DomainSwapJob, job.id)
        old = await db.get(Domain, old.id)
        tenant = await db.get(Tenant, tenant.id)
        assert saved.status == "attention"
        assert saved.mappings[0]["error"]
        assert old.tenant_id == tenant.id
        assert old.licensed_user_id == "old-user"
        assert tenant.batch_id == batch.id
        assert (await db.scalars(select(Domain).where(Domain.name == "new.example"))).first() is None
        assert all(m.status == MailboxStatus.READY for m in (await db.scalars(select(Mailbox))).all())
    pipeline.assert_not_called()


async def test_success_same_tenant_fresh_state_and_archived_old_mailboxes(test_session, test_engine, monkeypatch):
    old, tenant, original = await seed(test_session)
    tenant_id, old_id = tenant.id, old.id
    job = await plan(test_session, [old.name], ["new.example"])
    await swaps.start_plan(test_session, job)
    factory, cleanup, pipeline = install_runner(monkeypatch, test_engine)
    await swaps.run_job(job.id)
    async with factory() as db:
        job = await db.get(DomainSwapJob, job.id)
        tenant = await db.get(Tenant, tenant_id)
        old = await db.get(Domain, old_id)
        new = (await db.scalars(select(Domain).where(Domain.name == "new.example"))).one()
        batch = await db.get(SetupBatch, new.batch_id)
        assert job.status == "completed"
        assert new.tenant_id == tenant.id == tenant_id
        assert tenant.domain_id == new.id and tenant.custom_domain == new.name
        assert old.tenant_id is None and old.status == DomainStatus.RETIRED
        assert tenant.licensed_user_id is None and not tenant.license_assigned
        assert not new.licensed_user_created and not new.step5_complete and not new.step6_complete
        assert not tenant.step6_complete and tenant.first_login_completed
        assert tenant.admin_password == "admin-secret"
        assert batch.id != original.id and tenant.batch_id == batch.id
        assert len(batch.custom_mailbox_map["new.example"]) == 2
        assert all(m.status == MailboxStatus.SUSPENDED for m in (await db.scalars(select(Mailbox))).all())
        assert not (await db.scalars(select(DomainSwapReservation))).all()
    assert cleanup.call_args.kwargs["licensed_user_id"] == "old-user"
    pipeline.assert_awaited_once()


async def test_retry_after_cleanup_checkpoint_does_not_repeat_removal(test_session, test_engine, monkeypatch):
    old, _, _ = await seed(test_session)
    job = await plan(test_session, [old.name], ["new.example"])
    await swaps.start_plan(test_session, job)
    factory, cleanup, pipeline = install_runner(monkeypatch, test_engine, "paused")
    prepare = swaps._prepare_batch
    monkeypatch.setattr(swaps, "_prepare_batch", AsyncMock(side_effect=RuntimeError("interrupted after cleanup")))
    await swaps.run_job(job.id)
    async with factory() as db:
        saved = await db.get(DomainSwapJob, job.id)
        assert saved.mappings[0]["phase"] == "removed"
    monkeypatch.setattr(swaps, "_prepare_batch", prepare)
    await swaps.run_job(job.id)
    async with factory() as db:
        saved = await db.get(DomainSwapJob, job.id)
        assert saved.status == "attention"
        first_batch_id = saved.mappings[0]["batch_id"]
        assert saved.mappings[0]["pipeline"]["status"] == "paused"
    await swaps.run_job(job.id)
    async with factory() as db:
        saved = await db.get(DomainSwapJob, job.id)
        assert saved.mappings[0]["batch_id"] == first_batch_id
    cleanup.assert_awaited_once()
    assert pipeline.await_count == 2


async def test_multiple_domains_share_tenant_and_batch(test_session, test_engine, monkeypatch):
    old, tenant, original = await seed(test_session)
    second, _, _ = await seed(test_session, "second.example", tenant=tenant, batch=original)
    job = await plan(test_session, [old.name, second.name], ["new.example", "second-new.example"])
    await swaps.start_plan(test_session, job)
    factory, cleanup, pipeline = install_runner(monkeypatch, test_engine)
    await swaps.run_job(job.id)
    async with factory() as db:
        saved = await db.get(DomainSwapJob, job.id)
        assert saved.status == "completed"
        assert len({r["batch_id"] for r in saved.mappings}) == 1
        assert {d.tenant_id for d in (await db.scalars(select(Domain).where(Domain.name.in_(["new.example", "second-new.example"])))).all()} == {tenant.id}
    assert cleanup.await_count == 2
    pipeline.assert_awaited_once()


async def test_source_pipeline_cannot_resume_while_reserved(test_session):
    old, _, original = await seed(test_session)
    job = await plan(test_session, [old.name], ["new.example"])
    await swaps.start_plan(test_session, job)
    with pytest.raises(swaps.SwapConflict, match="reserved"):
        await swaps.ensure_pipeline_available(test_session, original.id)


async def test_api_preview_start_status_and_duplicate_submission(client, test_session):
    old, tenant, _ = await seed(test_session)
    response = await client.post("/api/v1/domain-swaps/preview", json={
        "name": "API swap", "sources": [old.name], "replacements": ["new.example"],
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["valid"] and data["job"]["status"] == "preview"
    assert "admin-secret" not in response.text and "mail-secret" not in response.text
    job_id = data["job"]["id"]
    for _ in range(2):
        response = await client.post(f"/api/v1/domain-swaps/{job_id}/start")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "queued"
    response = await client.get(f"/api/v1/domain-swaps/{job_id}")
    assert response.json()["mappings"][0]["tenant_id"] == str(tenant.id)
    assert (await client.get("/api/v1/domain-swaps")).json()["jobs"][0]["id"] == job_id
    response = await client.post("/api/v1/domain-swaps/preview", json={
        "sources": [old.name, tenant.name], "replacements": ["other.example"],
    })
    assert not response.json()["valid"]


async def test_api_conflict_is_actionable_and_blocks_source_resume(client, test_session):
    old, _, original = await seed(test_session)
    job = await plan(test_session, [old.name], ["new.example"])
    original.persona_first_name = "Changed"
    await test_session.commit()
    response = await client.post(f"/api/v1/domain-swaps/{job.id}/start")
    assert response.status_code == 409
    assert "fresh preview" in response.json()["detail"]
    job = await plan(test_session, [old.name], ["new.example"])
    await swaps.start_plan(test_session, job)
    response = await client.post(f"/api/v1/pipeline/{original.id}/resume")
    assert response.status_code == 409 and "reserved" in response.json()["detail"]


async def test_api_reads_live_pipeline_and_retry_preserves_mapping(client, test_session, test_engine, monkeypatch):
    old, _, _ = await seed(test_session)
    job = await plan(test_session, [old.name], ["new.example"])
    await swaps.start_plan(test_session, job)
    factory, cleanup, pipeline = install_runner(monkeypatch, test_engine, "paused")
    await swaps.run_job(job.id)
    response = await client.get(f"/api/v1/domain-swaps/{job.id}")
    row = response.json()["mappings"][0]
    batch_id = UUID(row["batch_id"])
    async with factory() as db:
        batch = await db.get(SetupBatch, batch_id)
        batch.pipeline_status = "running"
        batch.pipeline_step = 6
        batch.pipeline_step_name = "Setting up Microsoft domain"
        await db.commit()
    response = await client.get(f"/api/v1/domain-swaps/{job.id}")
    assert response.json()["mappings"][0]["pipeline"]["step"] == 6
    response = await client.post(f"/api/v1/domain-swaps/{job.id}/retry")
    assert response.status_code == 409
    async with factory() as db:
        batch = await db.get(SetupBatch, batch_id)
        batch.pipeline_status = "error"
        await db.commit()
    response = await client.post(f"/api/v1/domain-swaps/{job.id}/retry")
    assert response.status_code == 200 and response.json()["status"] == "queued"
    assert response.json()["mappings"][0]["batch_id"] == str(batch_id)
    cleanup.assert_awaited_once()


@pytest.mark.parametrize("connected,error", [(True, None), (False, "timeout")])
async def test_external_lookup_blocks_removal(monkeypatch, connected, error):
    from app.services.domain_lookup import DomainLookupService
    monkeypatch.setattr(domain_removal_service, "_get_cf_service", lambda: Mock())
    monkeypatch.setattr(DomainLookupService, "check_domain", AsyncMock(return_value=SimpleNamespace(is_connected=connected, error=error)))
    with pytest.raises(swaps.SwapConflict):
        await swaps.check_replacement("new.example")


async def test_replacement_preflight_failure_leaves_old_domain_untouched(test_session, test_engine, monkeypatch):
    old, tenant, _ = await seed(test_session)
    job = await plan(test_session, [old.name], ["new.example"])
    await swaps.start_plan(test_session, job)
    factory, cleanup, pipeline = install_runner(monkeypatch, test_engine)
    monkeypatch.setattr(swaps, "check_replacement", AsyncMock(side_effect=swaps.SwapConflict("Already connected")))
    await swaps.run_job(job.id)
    cleanup.assert_not_awaited()
    pipeline.assert_not_awaited()
    async with factory() as db:
        saved = await db.get(Domain, old.id)
        assert saved.tenant_id == tenant.id and saved.licensed_user_id == "old-user"


async def test_no_checkpoint_transaction_held_during_microsoft_wait(test_session, monkeypatch):
    old, _, _ = await seed(test_session)
    job = await plan(test_session, [old.name], ["new.example"])
    await swaps.start_plan(test_session, job)
    monkeypatch.setattr(swaps, "check_replacement", AsyncMock())

    async def external_cleanup(**kwargs):
        assert not test_session.in_transaction()
        return {"steps": {key: {"success": True} for key in
                          ("license_cleanup", "m365_removal", "cloudflare_cleanup")}}

    monkeypatch.setattr(domain_removal_service, "_execute_removal", external_cleanup)
    await swaps._remove_one(test_session, job, 0)
    assert job.mappings[0]["phase"] == "removed"
    assert old.tenant_id is None
