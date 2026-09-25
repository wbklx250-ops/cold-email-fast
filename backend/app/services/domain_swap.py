"""Plan, reserve and resume domain swaps using the existing setup pipeline."""
import asyncio
from collections import Counter, defaultdict
import hashlib
import json
import logging
import re
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import SessionLocal, async_engine
from app.models.batch import BatchStatus, SetupBatch
from app.models.domain import Domain, DomainStatus
from app.models.domain_swap import DomainSwapJob, DomainSwapReservation
from app.models.mailbox import Mailbox, MailboxStatus
from app.models.tenant import Tenant, TenantStatus

logger = logging.getLogger(__name__)


class SwapConflict(ValueError):
    pass


def domain_name(value):
    value = value.strip().lower().rstrip(".")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise SwapConflict("Invalid replacement domain")
    labels = value.split(".")
    if (len(value) > 253 or len(labels) < 2 or value.endswith(".onmicrosoft.com")
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels)
            or labels[-1].isdigit()):
        raise SwapConflict("Enter a custom domain name without a URL, path or email address")
    return value


def resources(rows):
    return sorted({key for row in rows for key in (
        f"tenant:{row['tenant_id']}", f"domain:{row['old_domain']}", f"domain:{row['new_domain']}",
    )})


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def public_job(job):
    return {"id": str(job.id), "name": job.name, "status": job.status,
            "created_at": job.created_at.isoformat(), "mappings": job.mappings}


async def build_plan(db, sources, replacements, allowed_job_id=None):
    if not sources or len(sources) != len(replacements):
        raise SwapConflict("Both lists must contain the same nonzero number of entries")
    domains = list((await db.scalars(select(Domain))).all())
    tenants = list((await db.scalars(select(Tenant))).all())
    batches = {b.id: b for b in (await db.scalars(select(SetupBatch))).all()}
    mailboxes = list((await db.execute(select(
        Mailbox.email, Mailbox.display_name, Mailbox.tenant_id, Mailbox.status,
    ))).all())
    by_name = {d.name.lower(): d for d in domains}
    rows, errors = [], []
    for number, (source, replacement) in enumerate(zip(sources, replacements), 1):
        try:
            source = source.strip()
            key = source.lower().rstrip(".")
            new_name = domain_name(replacement)
            old = by_name.get(key)
            if old:
                matches = [t for t in tenants if t.id == old.tenant_id or t.domain_id == old.id]
            else:
                matches = [t for t in tenants if key in {
                    str(t.id).lower(), (t.microsoft_tenant_id or "").lower(), t.name.lower(),
                    t.onmicrosoft_domain.lower(), t.admin_email.lower(),
                }]
            if len(matches) != 1:
                raise SwapConflict("Source must identify exactly one existing tenant or linked current domain")
            tenant = matches[0]
            linked = [d for d in domains if d.tenant_id == tenant.id or d.id == tenant.domain_id]
            if not old:
                if len(linked) != 1:
                    raise SwapConflict("Tenant has zero or multiple domains; specify the current domain to replace")
                old = linked[0]
            if old.tenant_id not in (None, tenant.id):
                raise SwapConflict("Current domain has conflicting tenant links; repair them first")
            if not tenant.admin_email or not tenant.admin_password:
                raise SwapConflict("Tenant admin credentials are missing")
            if not tenant.admin_email.lower().endswith(".onmicrosoft.com"):
                raise SwapConflict("Use a permanent onmicrosoft.com admin login before swapping domains")
            for batch_id in {tenant.batch_id, old.batch_id} - {None}:
                batch = batches.get(batch_id)
                if batch and (batch.pipeline_status == "running" or batch.auto_progress_enabled):
                    raise SwapConflict("Pause the tenant's existing automation before swapping")
            target = by_name.get(new_name)
            if old.name.lower() == new_name:
                raise SwapConflict("Replacement must differ from the current domain")
            if target and (target.tenant_id or any(t.domain_id == target.id for t in tenants)
                           or target.domain_added_to_m365 or target.domain_verified_in_m365
                           or target.licensed_user_created):
                raise SwapConflict("Replacement domain is already linked or has existing Microsoft setup")
            if target and target.batch_id:
                target_batch = batches.get(target.batch_id)
                if target_batch and target_batch.pipeline_status not in (None, "not_started", "completed"):
                    raise SwapConflict("Replacement belongs to an unfinished setup batch")
            if any(m.email.lower().endswith(f"@{new_name}") for m in mailboxes):
                raise SwapConflict("Replacement has existing mailbox records; resolve those before reuse")
            batch = batches.get(old.batch_id) or batches.get(tenant.batch_id)
            mb = sorted([m for m in mailboxes if m.tenant_id == tenant.id
                         and m.email.lower().endswith(f"@{old.name.lower()}")
                         and m.status != MailboxStatus.SUSPENDED], key=lambda m: m.email.lower())
            mailbox_map = [{"email": f"{m.email.split('@')[0]}@{new_name}", "display_name": m.display_name}
                           for m in mb]
            first = old.persona_first_name or (batch.persona_first_name if batch else None)
            last = old.persona_last_name or (batch.persona_last_name if batch else None)
            if not mailbox_map and batch and batch.custom_mailbox_map:
                for entry in batch.custom_mailbox_map.get(old.name, []):
                    email = entry.get("email", "") if isinstance(entry, dict) else entry
                    if email.lower().endswith(f"@{old.name.lower()}"):
                        mailbox_map.append({"email": f"{email.split('@')[0]}@{new_name}",
                                            "display_name": entry.get("display_name", "") if isinstance(entry, dict) else ""})
            if not mailbox_map and not (first and last):
                raise SwapConflict("Current domain needs mailbox records or a first and last name for provisioning")
            rows.append({
                "row": number, "source": source, "tenant_id": str(tenant.id), "tenant_name": tenant.name,
                "onmicrosoft_domain": tenant.onmicrosoft_domain, "old_domain_id": str(old.id),
                "admin_email": tenant.admin_email, "microsoft_tenant_id": tenant.microsoft_tenant_id,
                "old_domain": old.name, "new_domain": new_name,
                "new_domain_id": str(target.id) if target else None,
                "source_batch_id": str(old.batch_id) if old.batch_id else None,
                "tenant_batch_id": str(tenant.batch_id) if tenant.batch_id else None,
                "primary": tenant.domain_id == old.id, "domain_index": old.domain_index_in_tenant,
                "first_name": first, "last_name": last,
                "redirect_url": old.redirect_url or (batch.redirect_url if batch else None),
                "mailbox_map": mailbox_map,
                "mailbox_count": len(mailbox_map) or (batch.mailboxes_per_tenant if batch else tenant.target_mailbox_count) or 50,
                "sequencer_app_key": batch.sequencer_app_key if batch else "instantly",
                "licensed_user_id": old.licensed_user_id or (
                    tenant.licensed_user_id if (tenant.licensed_user_upn or "").lower().endswith(f"@{old.name.lower()}") else None),
                "phase": "pending", "batch_id": None, "error": None,
            })
        except SwapConflict as exc:
            errors.append({"row": number, "source": source, "error": str(exc)})
    old_counts = Counter(r["old_domain"] for r in rows)
    new_counts = Counter(r["new_domain"] for r in rows)
    old_names = set(old_counts)
    selected = {r["old_domain_id"] for r in rows}
    for row in rows:
        problem = None
        if old_counts[row["old_domain"]] > 1 or new_counts[row["new_domain"]] > 1:
            problem = "Each current domain and replacement may appear only once"
        elif row["new_domain"] in old_names:
            problem = "A replacement cannot also be a current domain in this swap"
        elif any(str(d.id) not in selected and d.tenant_id == UUID(row["tenant_id"])
                 and not d.step6_complete for d in domains):
            problem = "Finish setup of the tenant's other domains before replacing this domain"
        if problem:
            errors.append({"row": row["row"], "source": row["source"], "error": problem})
    if rows:
        claims = list((await db.scalars(select(DomainSwapReservation).where(
            DomainSwapReservation.resource.in_(resources(rows)),
        ))).all())
        if any(c.job_id != allowed_job_id for c in claims):
            errors.append({"row": 0, "error": "A selected tenant or domain is reserved by an unfinished swap"})
    return rows, errors


async def start_plan(db, job):
    if job.status != "preview":
        # Repeated submissions of the same reviewed plan are idempotent.
        return job
    rows, errors = await build_plan(db, [r["source"] for r in job.mappings],
                                   [r["new_domain"] for r in job.mappings])
    if errors or fingerprint(rows) != fingerprint(job.mappings):
        raise SwapConflict("The mappings or setup settings changed since preview. Generate a fresh preview.")
    for resource in resources(rows):
        db.add(DomainSwapReservation(resource=resource, job_id=job.id))
    job.status = "queued"
    await db.commit()
    return job


def reset_tenant_domain_state(tenant):
    """Reset domain-specific evidence while retaining admin and first-login state."""
    for field in (
        "step5_complete", "step6_started", "step6_complete", "mailboxes_generated", "mailboxes_created",
        "delegation_completed", "domain_added_to_m365", "domain_verified_in_m365", "mx_record_added",
        "spf_record_added", "autodiscover_added", "dkim_cnames_added", "dkim_enabled",
    ):
        setattr(tenant, field, False)
    for field in (
        "step5_completed_at", "step6_started_at", "step6_completed_at", "step6_error", "setup_error",
        "domain_verified_at", "mailboxes_created_at", "m365_verification_txt", "mx_value", "spf_value",
        "dkim_selector1", "dkim_selector2", "dkim_selector1_cname", "dkim_selector2_cname", "dkim_enabled_at",
    ):
        setattr(tenant, field, None)
    for field in (
        "step5_retry_count", "step6_retry_count", "step6_mailboxes_created", "step6_display_names_fixed",
        "step6_accounts_enabled", "step6_passwords_set", "step6_upns_fixed", "step6_delegations_done",
        "mailboxes_configured", "mailbox_count", "dkim_retry_count",
    ):
        setattr(tenant, field, 0)
    tenant.status = TenantStatus.DOMAIN_LINKED


async def _remove_one(db, job, index):
    from app.services.domain_removal_service import domain_removal_service

    row = job.mappings[index]
    old = await db.get(Domain, UUID(row["old_domain_id"]))
    tenant = await db.get(Tenant, UUID(row["tenant_id"]))
    if not old or not tenant or old.name != row["old_domain"] or (
        old.tenant_id != tenant.id and not (old.tenant_id is None and tenant.domain_id == old.id)
    ):
        raise SwapConflict("Current domain/tenant link changed. Restore the reviewed mapping before retrying.")
    if (str(tenant.batch_id) if tenant.batch_id else None) != row["tenant_batch_id"]:
        raise SwapConflict("Tenant moved to another batch after preview")
    if any(getattr(tenant, field) != row[field] for field in ("onmicrosoft_domain", "admin_email", "microsoft_tenant_id")):
        raise SwapConflict("Tenant identity or admin login changed after preview")
    # Check the replacement again immediately before doing external cleanup.
    target = (await db.scalars(select(Domain).where(Domain.name == row["new_domain"]))).one_or_none()
    if target and (target.tenant_id or target.domain_added_to_m365 or target.licensed_user_created):
        raise SwapConflict("Replacement domain is no longer available")
    for batch_id in {tenant.batch_id, old.batch_id} - {None}:
        batch = await db.get(SetupBatch, batch_id)
        if batch and (batch.pipeline_status == "running" or batch.auto_progress_enabled):
            raise SwapConflict("Existing automation is running; pause it before retrying")
    await check_replacement(row["new_domain"])
    row["phase"], row["error"] = "removing", None
    flag_modified(job, "mappings")
    await db.commit()

    async def checkpoint(_db, removed_domain, removed_tenant):
        # This checkpoint commits WITH unlinking/archival in the removal service.
        removed_domain.status = DomainStatus.RETIRED
        if (removed_tenant.licensed_user_upn or "").lower().endswith(f"@{row['old_domain'].lower()}"):
            for field in ("licensed_user_upn", "licensed_user_password", "licensed_user_id"):
                setattr(removed_tenant, field, None)
            removed_tenant.licensed_user_created = removed_tenant.license_assigned = False
        row["phase"] = "removed"
        row["error"] = None
        flag_modified(job, "mappings")

    result = await domain_removal_service.remove_domain_from_db(
        db, row["old_domain"], headless=True, require_full_cleanup=True,
        on_removed=checkpoint, licensed_user_id=row["licensed_user_id"],
    )
    if not result.get("success"):
        raise SwapConflict(result.get("error") or "Old domain cleanup failed")


async def check_replacement(name):
    """Screen external prerequisites before releasing any old licenses."""
    from app.services.domain_removal_service import domain_removal_service
    from app.services.domain_lookup import DomainLookupService

    if domain_removal_service._get_cf_service() is None:
        raise SwapConflict("Configure Cloudflare before replacing domains")
    lookup = await DomainLookupService().check_domain(name)
    if lookup.error:
        raise SwapConflict("Microsoft replacement-domain lookup failed; retry before removing the old domain")
    if lookup.is_connected:
        raise SwapConflict("Microsoft already reports the replacement domain connected to a tenant; release it before retrying")


async def _prepare_batch(db, job, indexes):
    rows = [job.mappings[i] for i in indexes]
    if any(r["batch_id"] for r in rows):
        return UUID(rows[0]["batch_id"])
    if any(r["phase"] != "removed" for r in rows):
        return None
    first = rows[0]
    tenant = await db.get(Tenant, UUID(first["tenant_id"]))
    if not tenant or (str(tenant.batch_id) if tenant.batch_id else None) != first["tenant_batch_id"]:
        raise SwapConflict("Tenant changed batches during cleanup")
    batch = SetupBatch(
        name=f"{job.name} · {tenant.name}"[:255], status=BatchStatus.IN_PROGRESS,
        pipeline_status="not_started", current_step=1, pipeline_step=1,
        total_domains=len(rows), total_tenants=1, domains_per_tenant=len(rows),
        persona_first_name=first["first_name"], persona_last_name=first["last_name"],
        mailboxes_per_tenant=first["mailbox_count"], new_admin_password=tenant.admin_password,
        sequencer_app_key=first["sequencer_app_key"],
        custom_mailbox_map={r["new_domain"]: r["mailbox_map"] for r in rows if r["mailbox_map"]},
        description=f"Domain swap {job.id}. Old domain and mailbox records are retained as history.",
    )
    db.add(batch)
    await db.flush()
    for row in rows:
        target = (await db.scalars(select(Domain).where(Domain.name == row["new_domain"]))).one_or_none()
        if target and (target.tenant_id or target.domain_added_to_m365 or target.licensed_user_created):
            raise SwapConflict("Replacement became linked to another tenant; setup stopped")
        if not target:
            target = Domain(name=row["new_domain"], tld=row["new_domain"].rsplit(".", 1)[1],
                            status=DomainStatus.PURCHASED, cloudflare_zone_status="pending", cloudflare_nameservers=[])
            db.add(target)
        else:
            # Keep Cloudflare identifiers, but require fresh evidence for every setup step.
            for column in Domain.__table__.columns:
                if column.name.startswith(("step", "licensed_user", "dkim", "m365", "verification", "domain_added", "domain_verified")):
                    if column.type.python_type is bool:
                        setattr(target, column.name, False)
                    elif column.type.python_type is int:
                        setattr(target, column.name, 0)
                    elif column.nullable:
                        setattr(target, column.name, None)
            for field in ("phase1_cname_added", "phase1_dmarc_added", "dns_records_created", "nameservers_updated",
                          "redirect_configured", "mx_record_added", "spf_record_added", "autodiscover_added",
                          "mx_configured", "spf_configured", "dmarc_configured"):
                setattr(target, field, False)
            target.ns_propagated_at = target.nameservers_updated_at = None
            target.mx_value = target.spf_value = None
        target.tenant_id, target.batch_id = tenant.id, batch.id
        target.status, target.error_message = DomainStatus.TENANT_LINKED, None
        target.redirect_url = row["redirect_url"]
        target.persona_first_name, target.persona_last_name = row["first_name"], row["last_name"]
        target.domain_index_in_tenant = row["domain_index"]
        target.cloudflare_zone_status = "pending"
        await db.flush()
        if row["primary"] or tenant.domain_id is None:
            tenant.domain_id, tenant.custom_domain = target.id, target.name
        row["new_domain_id"], row["batch_id"] = str(target.id), str(batch.id)
        row["phase"], row["error"] = "provisioning", None
    tenant.batch_id = batch.id
    reset_tenant_domain_state(tenant)
    flag_modified(job, "mappings")
    await db.commit()
    return batch.id


async def run_job(job_id):
    """Caller holds the database worker lock. Failures isolate one tenant group."""
    from app.api.routes.pipeline import run_pipeline, pipeline_jobs

    async with SessionLocal() as db:
        job = await db.get(DomainSwapJob, job_id)
        job.status = "running"
        await db.commit()
        groups = defaultdict(list)
        for i, row in enumerate(job.mappings):
            groups[row["tenant_id"]].append(i)
    for indexes in groups.values():
        try:
            async with SessionLocal() as db:
                job = await db.get(DomainSwapJob, job_id)
                for index in indexes:
                    if job.mappings[index]["phase"] in ("pending", "removing"):
                        await _remove_one(db, job, index)
                batch_id = await _prepare_batch(db, job, indexes)
                batch = await db.get(SetupBatch, batch_id) if batch_id else None
                status, step = (batch.pipeline_status, batch.pipeline_step or 1) if batch else (None, 1)
            if batch_id and status != "completed":
                if pipeline_jobs.get(str(batch_id), {}).get("status") == "running":
                    continue
                await run_pipeline(batch_id, start_from_step=step)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Swap %s failed for tenant group", job_id)
            async with SessionLocal() as db:
                job = await db.get(DomainSwapJob, job_id)
                for index in indexes:
                    if job.mappings[index]["phase"] != "completed":
                        job.mappings[index]["error"] = (
                            "Database update failed; retry from the saved checkpoint"
                            if isinstance(exc, SQLAlchemyError) else str(exc)[:1000]
                        )
                flag_modified(job, "mappings")
                await db.commit()
    async with SessionLocal() as db:
        job = await db.get(DomainSwapJob, job_id)
        job.status = "attention"
        await sync_progress(db, job)
        await db.commit()


async def sync_progress(db, job):
    """Use persisted pipeline status, including actions taken on the setup page."""
    changed = False
    for row in job.mappings:
        if not row["batch_id"]:
            continue
        batch = await db.get(SetupBatch, UUID(row["batch_id"]))
        if not batch:
            row["error"] = "Replacement setup batch was deleted; restore it before continuing"
            changed = True
            continue
        state = {"status": batch.pipeline_status, "step": batch.pipeline_step, "message": batch.pipeline_step_name}
        if row.get("pipeline") != state:
            row["pipeline"] = state
            changed = True
        if batch.pipeline_status == "completed" and row["phase"] != "completed":
            row["phase"], row["error"] = "completed", None
            changed = True
    if changed:
        flag_modified(job, "mappings")
    if all(r["phase"] == "completed" for r in job.mappings):
        job.status = "completed"
        await db.execute(delete(DomainSwapReservation).where(DomainSwapReservation.job_id == job.id))


async def ensure_pipeline_available(db, batch_id):
    """A paused source batch must not reclaim a tenant during its swap."""
    domains = list((await db.scalars(select(Domain).where(Domain.batch_id == batch_id))).all())
    tenants = list((await db.scalars(select(Tenant).where(Tenant.batch_id == batch_id))).all())
    keys = [f"domain:{d.name}" for d in domains] + [f"tenant:{t.id}" for t in tenants]
    if not keys:
        return
    claims = (await db.scalars(select(DomainSwapReservation).where(
        DomainSwapReservation.resource.in_(keys),
    ))).all()
    for job_id in {claim.job_id for claim in claims}:
        job = await db.get(DomainSwapJob, job_id)
        if not any(row["batch_id"] == str(batch_id) for row in job.mappings):
            raise SwapConflict("This batch contains a tenant or domain reserved for a domain swap")


async def worker_tick():
    # A transaction-scoped advisory lock works with transaction-pooling proxies
    # and is released if the process dies. Separate sessions persist checkpoints.
    async with async_engine.begin() as connection:
        if not await connection.scalar(text("SELECT pg_try_advisory_xact_lock(731904225)")):
            return
        async with SessionLocal() as db:
            jobs = list((await db.scalars(select(DomainSwapJob).where(
                DomainSwapJob.status.in_(["queued", "running", "attention"]),
            ).order_by(DomainSwapJob.created_at))).all())
            queued = [job.id for job in jobs if job.status in ("queued", "running")]
            for job in jobs:
                if job.status == "attention":
                    await sync_progress(db, job)
            await db.commit()
        for job_id in queued:
            await run_job(job_id)


async def worker_loop():
    while True:
        try:
            await worker_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Domain swap worker failed; persisted checkpoints retained")
        await asyncio.sleep(10)
