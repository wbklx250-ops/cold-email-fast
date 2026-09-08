"""Prerequisite checks must include blocked items, not just eligible workers."""

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.batch import SetupBatch
from app.models.domain import Domain
from app.models.tenant import Tenant
from app.services.cloudflare import cloudflare_service


class PipelineBlocked(RuntimeError):
    def __init__(self, step, message):
        super().__init__(message)
        self.step = step


def m365_ready(domain):
    return all(getattr(domain, field, False) for field in (
        "step5_complete", "domain_verified_in_m365", "dkim_enabled",
        "dkim_cnames_added", "mx_record_added", "spf_record_added",
        "autodiscover_added", "dmarc_configured",
    ))


def first_blocker(domains, tenants, before_step=12):
    """Inspect the entire batch; failed/skipped/unlinked rows remain unfinished."""
    if not domains or not tenants:
        return PipelineBlocked(1, "Batch requires at least one domain and one tenant")
    tenant_by_id = {t.id: t for t in tenants}
    for domain in domains:
        if domain.tenant_id not in tenant_by_id:
            return PipelineBlocked(1, f"{domain.name}: link a tenant belonging to this batch")
    for tenant in tenants:
        if not any(d.tenant_id == tenant.id for d in domains):
            return PipelineBlocked(1, f"Tenant {tenant.id} has no domain in this batch")
    checks = (
        (1, domains, lambda d: bool(d.cloudflare_zone_id), "Cloudflare zone is missing"),
        (3, domains, lambda d: d.cloudflare_zone_status == "active" and bool(d.ns_propagated_at),
         "nameservers are not verified against an active Cloudflare zone"),
        (5, tenants, lambda t: t.first_login_completed, "first login is incomplete"),
        (6, domains, m365_ready, "M365 verification, DKIM or email DNS is incomplete"),
        (7, domains, lambda d: d.step6_complete, "mailbox creation/delegation is incomplete"),
        (7, tenants, lambda t: t.step6_complete, "tenant mailbox setup is incomplete"),
        (8, tenants, lambda t: t.step7_smtp_auth_enabled, "SMTP authentication is incomplete"),
    )
    for step, items, ready, reason in checks:
        if step >= before_step:
            continue
        failed = [getattr(item, "name", None) or str(item.id) for item in items if not ready(item)]
        if failed:
            return PipelineBlocked(step, f"Step {step} incomplete: {reason}: {', '.join(failed[:10])} ({len(failed)} items)")
    return None


async def load_batch_state(batch_id):
    async with SessionLocal() as db:
        domains = list((await db.execute(select(Domain).where(Domain.batch_id == batch_id))).scalars())
        tenants = list((await db.execute(select(Tenant).where(Tenant.batch_id == batch_id))).scalars())
    return domains, tenants


async def require_ready(batch_id, before_step):
    domains, tenants = await load_batch_state(batch_id)
    blocker = first_blocker(domains, tenants, before_step)
    if blocker:
        raise blocker


async def refresh_nameservers(batch_id):
    """Recheck every zone, including reused domains with historic timestamps."""
    domains, _ = await load_batch_state(batch_id)
    results = []
    for domain in domains:
        zone = None
        error = None
        try:
            # Search accounts by name so a deleted/recreated zone can be recovered.
            zone = await cloudflare_service.get_zone_by_name(domain.name)
            if zone and (zone.get("name") or "").rstrip(".").lower() != domain.name.lower():
                zone = None
            if not zone:
                error = "Cloudflare zone could not be found or verified"
        except Exception as exc:
            error = f"Cloudflare readiness check failed: {exc}"
        active = bool(zone and zone.get("zone_id") and zone.get("status") == "active")
        async with SessionLocal() as db:
            row = await db.get(Domain, domain.id)
            if row:
                if zone:
                    changed = row.cloudflare_zone_id != zone.get("zone_id") or sorted(row.cloudflare_nameservers or []) != sorted(zone.get("nameservers") or [])
                    row.cloudflare_zone_id = zone.get("zone_id")
                    row.cloudflare_nameservers = zone.get("nameservers") or []
                    if changed:
                        row.dns_records_created = False
                        row.redirect_configured = False
                        row.step5_complete = False
                row.cloudflare_zone_status = zone.get("status", "unknown") if zone else "unknown"
                row.nameservers_updated = active
                row.ns_propagated_at = datetime.now(timezone.utc) if active else None
                if not active:
                    row.error_message = error or f"Cloudflare zone is {row.cloudflare_zone_status}; waiting for nameservers"
                await db.commit()
        results.append({"domain": domain.name, "active": active, "error": error})
    async with SessionLocal() as db:
        batch = await db.get(SetupBatch, batch_id)
        if batch:
            batch.ns_propagated_count = sum(r["active"] for r in results)
            await db.commit()
    return results


async def sync_manual_m365_setup(batch_id):
    """Adopt externally completed prerequisites only after read-only verification."""
    from app.services.objective_reconciliation import (
        _load_batch_domain_data, _verify_m365_domain, _repair_dkim_if_needed,
        _save_domain_truth,
    )
    domains, tenants = await load_batch_state(batch_id)
    logged_in = {t.id for t in tenants if t.first_login_completed}
    incomplete = {d.id for d in domains if not m365_ready(d)
                  and d.cloudflare_zone_status == "active" and d.tenant_id in logged_in}
    if not incomplete:
        return
    _, domain_data = await _load_batch_domain_data(batch_id)
    for data in domain_data:
        if data["id"] not in incomplete:
            continue
        m365 = await _verify_m365_domain(data, auto_fix=False)
        if not m365.get("verified"):
            continue
        dkim, cf = await _repair_dkim_if_needed(data, auto_fix=False)
        await _save_domain_truth(data, m365, dkim, cf, None, [])


async def refresh_counters(batch_id):
    domains, tenants = await load_batch_state(batch_id)
    async with SessionLocal() as db:
        batch = await db.get(SetupBatch, batch_id)
        if batch:
            batch.zones_completed = sum(bool(d.cloudflare_zone_id) for d in domains)
            batch.ns_propagated_count = sum(d.cloudflare_zone_status == "active" and bool(d.ns_propagated_at) for d in domains)
            batch.first_login_completed_count = sum(bool(t.first_login_completed) for t in tenants)
            batch.m365_completed = sum(m365_ready(d) for d in domains)
            batch.mailboxes_completed_count = sum(bool(d.step6_complete) for d in domains)
            batch.smtp_completed = sum(bool(t.step7_smtp_auth_enabled) for t in tenants)
            await db.commit()


def reconciliation_complete(summary, expected_tenants):
    return bool(
        expected_tenants > 0
        and summary.get("status") == "completed"
        and summary.get("total_tenants") == expected_tenants
        and summary.get("sd_ok", 0) + summary.get("sd_drift_fixed", 0) == expected_tenants
        and summary.get("smtp_ok", 0) + summary.get("smtp_drift_fixed", 0) == expected_tenants
        and not summary.get("sd_drift_unfixable")
        and not summary.get("smtp_drift_unfixable")
        and not summary.get("errors")
    )
