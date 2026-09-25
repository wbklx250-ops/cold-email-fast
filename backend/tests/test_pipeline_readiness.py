from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import pipeline_readiness as readiness
from app.services import batch_reconciliation as reconciliation


def ready_batch(count=1):
    tenants = [SimpleNamespace(id=uuid4(), name=f"tenant-{i}", first_login_completed=True,
        step6_complete=True, step7_smtp_auth_enabled=True) for i in range(count)]
    domains = [SimpleNamespace(id=uuid4(), name=f"example-{i}.com", tenant_id=t.id,
        cloudflare_zone_id=f"zone-{i}", cloudflare_zone_status="active",
        cloudflare_nameservers=["a.ns.cloudflare.com", "b.ns.cloudflare.com"],
        ns_propagated_at=datetime(2026, 4, 24, tzinfo=timezone.utc),
        nameservers_updated=True, step5_complete=True, domain_verified_in_m365=True,
        dkim_enabled=True, dkim_cnames_added=True, mx_record_added=True,
        spf_record_added=True, autodiscover_added=True, dmarc_configured=True,
        step6_complete=True, step5_skipped=False, step6_skipped=False)
        for i, t in enumerate(tenants)]
    return domains, tenants


@pytest.mark.parametrize("field,step", [
    ("domain_verified_in_m365", 6), ("dkim_enabled", 6), ("step5_complete", 6),
    ("dmarc_configured", 6), ("mx_record_added", 6), ("step6_complete", 7),
])
def test_ineligible_domain_blocks_completion(field, step):
    domains, tenants = ready_batch()
    setattr(domains[0], field, False)
    blocker = readiness.first_blocker(domains, tenants)
    assert blocker.step == step
    assert domains[0].name in str(blocker)


def test_95_percent_is_not_complete():
    domains, tenants = ready_batch(20)
    domains[-1].cloudflare_zone_status = "pending"
    assert readiness.first_blocker(domains, tenants).step == 3


def test_skipped_failure_stays_incomplete():
    domains, tenants = ready_batch()
    domains[0].step5_complete = False
    domains[0].step5_skipped = True
    assert readiness.first_blocker(domains, tenants).step == 6


def test_resume_checks_only_prior_steps_and_finds_earliest_blocker():
    domains, tenants = ready_batch()
    domains[0].step5_complete = False
    domains[0].step6_complete = False
    assert readiness.first_blocker(domains, tenants, before_step=6) is None
    assert readiness.first_blocker(domains, tenants, before_step=7).step == 6


def test_empty_and_unlinked_batches_cannot_complete():
    assert readiness.first_blocker([], []).step == 1
    domains, tenants = ready_batch()
    domains[0].tenant_id = uuid4()
    assert readiness.first_blocker(domains, tenants).step == 1


def test_complete_batch_has_no_blocker():
    assert readiness.first_blocker(*ready_batch()) is None


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, model, key):
        return self.rows[key]

    async def commit(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "active", "error", "missing"])
async def test_live_nameservers_override_stale_april_timestamp(monkeypatch, status):
    domains, tenants = ready_batch()
    domain = domains[0]
    batch_id = uuid4()
    batch = SimpleNamespace(ns_propagated_count=1)
    monkeypatch.setattr(readiness, "load_batch_state", AsyncMock(return_value=(domains, tenants)))
    monkeypatch.setattr(readiness, "SessionLocal", lambda: FakeSession({domain.id: domain, batch_id: batch}))
    lookup = AsyncMock(return_value=None if status == "missing" else {
        "name": domain.name, "zone_id": domain.cloudflare_zone_id,
        "status": status, "nameservers": domain.cloudflare_nameservers})
    if status == "error":
        lookup.side_effect = RuntimeError("Cloudflare unavailable")
    monkeypatch.setattr(readiness, "cloudflare_service", SimpleNamespace(get_zone_by_name=lookup))
    results = await readiness.refresh_nameservers(batch_id)
    assert lookup.await_count == 1
    assert results[0]["active"] is (status == "active")
    assert bool(domain.ns_propagated_at) is (status == "active")
    assert domain.nameservers_updated is (status == "active")
    assert batch.ns_propagated_count == int(status == "active")


def good_reconciliation():
    return dict(status="completed", total_tenants=1, sd_ok=1, smtp_ok=1, errors=[])


@pytest.mark.parametrize("change", [
    {"total_tenants": 0}, {"sd_ok": 0}, {"smtp_ok": 0}, {"status": "error"},
    {"errors": [{"error": "unhandled failure"}]}, {"smtp_drift_unfixable": 1},
])
def test_reconciliation_cannot_pass_empty_partial_or_error_results(change):
    summary = good_reconciliation()
    summary.update(change)
    assert not readiness.reconciliation_complete(summary, 1)


def test_reconciliation_requires_exact_coverage():
    assert readiness.reconciliation_complete(good_reconciliation(), 1)
    assert not readiness.reconciliation_complete(good_reconciliation(), 2)
    assert not readiness.reconciliation_complete(good_reconciliation(), 0)


@pytest.mark.asyncio
async def test_no_tenants_is_reconciliation_error(monkeypatch):
    monkeypatch.setattr(reconciliation, "_load_batch_tenants", AsyncMock(return_value=[]))
    summary = await reconciliation.reconcile_batch(uuid4())
    assert summary["status"] == "error"
    assert summary["errors"]


@pytest.mark.asyncio
async def test_incomplete_tenant_cannot_disappear_from_reconciliation(monkeypatch):
    _, tenants = ready_batch()
    tenants[0].custom_domain = "example.com"
    tenants[0].step6_complete = False
    monkeypatch.setattr(reconciliation, "_load_batch_tenants", AsyncMock(return_value=tenants))
    monkeypatch.setattr(reconciliation, "find_powershell_exe", lambda *args: "pwsh")
    verify = AsyncMock()
    monkeypatch.setattr(reconciliation, "_reconcile_sd_for_tenant", verify)
    summary = await reconciliation.reconcile_batch(uuid4())
    assert summary["status"] == "error"
    assert summary["total_tenants"] == 1
    assert summary["errors"][0]["stage"] == "prerequisites"
    verify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("start_step", [6, 7])
async def test_pipeline_does_not_complete_when_worker_query_returns_zero(monkeypatch, start_step):
    from app.api.routes import pipeline
    domains, tenants = ready_batch()
    domains[0].step5_complete = False
    batch_id = uuid4()
    batch = SimpleNamespace(id=batch_id, name="recovery", total_domains=1, total_tenants=1,
        errors_count=0, pipeline_status="completed", pipeline_completed_at=datetime.now(timezone.utc))

    class EmptyQuerySession(FakeSession):
        async def execute(self, query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

        async def scalar(self, query):
            return 0

    monkeypatch.setattr(pipeline, "SessionLocal", lambda: EmptyQuerySession({batch_id: batch}))
    monkeypatch.setattr(pipeline, "load_batch_state", AsyncMock(return_value=(domains, tenants)))
    monkeypatch.setattr(readiness, "load_batch_state", AsyncMock(return_value=(domains, tenants)))
    monkeypatch.setattr(pipeline, "refresh_nameservers", AsyncMock())
    monkeypatch.setattr(pipeline, "sync_manual_m365_setup", AsyncMock())
    monkeypatch.setattr(pipeline, "refresh_counters", AsyncMock())
    monkeypatch.setattr(pipeline, "kill_all_browsers", lambda: None)
    monkeypatch.setattr(pipeline.asyncio, "sleep", AsyncMock())
    activity = AsyncMock()
    monkeypatch.setattr(pipeline, "log_activity", activity)
    await pipeline.run_pipeline(batch_id, start_step)
    assert batch.pipeline_status == "error"
    assert batch.pipeline_step == 6
    assert batch.pipeline_completed_at is None
    assert not any(call.kwargs.get("message") == "All steps finished" for call in activity.call_args_list)
    pipeline.pipeline_jobs.pop(str(batch_id), None)


@pytest.mark.asyncio
async def test_pipeline_stops_at_ns_timeout_without_advancing(monkeypatch):
    from app.api.routes import pipeline
    domains, tenants = ready_batch(20)
    domains[-1].cloudflare_zone_status = "pending"
    batch_id = uuid4()
    batch = SimpleNamespace(id=batch_id, name="propagation", total_domains=20, total_tenants=20,
        errors_count=0, ns_confirmed_at=True, pipeline_status="running")
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: FakeSession({batch_id: batch}))
    monkeypatch.setattr(pipeline, "load_batch_state", AsyncMock(return_value=(domains, tenants)))
    monkeypatch.setattr(readiness, "load_batch_state", AsyncMock(return_value=(domains, tenants)))
    monkeypatch.setattr(pipeline, "refresh_nameservers", AsyncMock(return_value=[
        {"domain": d.name, "active": d.cloudflare_zone_status == "active"} for d in domains]))
    monkeypatch.setattr(pipeline, "refresh_counters", AsyncMock())
    monkeypatch.setattr(pipeline, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    clock = iter([0, 14401])
    monkeypatch.setattr(pipeline, "log_activity", AsyncMock())
    await pipeline.run_pipeline(batch_id, 3)
    assert batch.pipeline_status == "error"
    assert batch.pipeline_step == 3
    assert pipeline.pipeline_jobs[str(batch_id)]["steps"]["4"]["status"] == "pending"
    pipeline.pipeline_jobs.pop(str(batch_id), None)


def test_dashboard_does_not_infer_success_from_current_step():
    from app.api.routes.pipeline import _default_pipeline_steps
    batch = SimpleNamespace(pipeline_step=11, pipeline_status="completed", total_domains=1,
        total_tenants=1, zones_completed=1, ns_propagated_count=0, dns_completed=1,
        first_login_completed_count=1, m365_completed=0, mailboxes_completed_count=0,
        smtp_completed=0, sequencer_uploaded_count=0)
    stages = _default_pipeline_steps(batch)
    assert stages["1"]["status"] == "completed"
    for step in (2, 3, 6, 7, 8):
        assert stages[str(step)]["status"] != "completed"


@pytest.mark.asyncio
async def test_manual_setup_requires_live_verification_before_saving(monkeypatch):
    from app.services import objective_reconciliation as objective
    domains, tenants = ready_batch()
    domains[0].step5_complete = False
    data = {"id": domains[0].id, "name": domains[0].name}
    monkeypatch.setattr(readiness, "load_batch_state", AsyncMock(return_value=(domains, tenants)))
    monkeypatch.setattr(objective, "_load_batch_domain_data", AsyncMock(return_value=({}, [data])))
    verify = AsyncMock(return_value={"verified": False})
    monkeypatch.setattr(objective, "_verify_m365_domain", verify)
    dkim = AsyncMock(return_value=({"enabled": True}, {"zone_ok": True}))
    monkeypatch.setattr(objective, "_repair_dkim_if_needed", dkim)
    save = AsyncMock()
    monkeypatch.setattr(objective, "_save_domain_truth", save)
    await readiness.sync_manual_m365_setup(uuid4())
    save.assert_not_awaited()
    verify.return_value = {"verified": True, "ok": True}
    await readiness.sync_manual_m365_setup(uuid4())
    verify.assert_awaited_with(data, auto_fix=False)
    dkim.assert_awaited_with(data, auto_fix=False)
    save.assert_awaited_once_with(data, verify.return_value, {"enabled": True}, {"zone_ok": True}, None, [])
