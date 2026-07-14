"""Force Step 5 Admin Center wizard repair for recent domains.

This is an incident tool. It intentionally preserves stored DNS target values
while clearing completion booleans so every selected domain is processed by
the Step 5 Admin Center wizard path again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

os.environ["DEBUG"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select

from app.db.session import async_session_factory
from app.models.batch import SetupBatch
from app.models.domain import Domain, DomainStatus
from app.models.tenant import Tenant, TenantStatus
from app.services.m365_setup import run_step5_for_batch
from app.services.objective_reconciliation import objective_reconcile_batch
from app.services.selenium.browser import kill_all_browsers


DEFAULT_CUTOFF = datetime(2026, 4, 12, tzinfo=timezone.utc)
LOG_DIR = Path("logs") / "incident_step5_repair"
LOGGER = logging.getLogger("incident_step5_repair")


def _domain_contract_complete(domain: Domain) -> bool:
    return bool(
        domain.step5_complete
        and domain.domain_verified_in_m365
        and domain.dkim_enabled
        and domain.mx_record_added
        and domain.spf_record_added
        and domain.autodiscover_added
        and domain.dkim_cnames_added
        and domain.dmarc_configured
    )


def _cohort_filter(cutoff: datetime):
    return or_(
        Domain.created_at >= cutoff,
        Tenant.created_at >= cutoff,
        SetupBatch.created_at >= cutoff,
    )


async def _load_cohort(cutoff: datetime, batch_id: UUID | None = None, limit: int | None = None):
    async with async_session_factory() as db:
        stmt = (
            select(Domain, Tenant, SetupBatch)
            .join(Tenant, Domain.tenant_id == Tenant.id)
            .join(SetupBatch, Domain.batch_id == SetupBatch.id)
            .where(Domain.tenant_id.isnot(None), _cohort_filter(cutoff))
            .order_by(SetupBatch.created_at, Tenant.created_at, Domain.domain_index_in_tenant, Domain.name)
        )
        if batch_id:
            stmt = stmt.where(Domain.batch_id == batch_id)
        if limit:
            stmt = stmt.limit(limit)
        return list((await db.execute(stmt)).all())


async def inventory(cutoff: datetime, batch_id: UUID | None = None, limit: int | None = None) -> dict:
    rows = await _load_cohort(cutoff, batch_id=batch_id, limit=limit)
    summary: dict = {
        "cutoff_utc": cutoff.isoformat(),
        "total_domains": len(rows),
        "runnable_domains": 0,
        "already_complete_by_contract": 0,
        "incomplete_by_contract": 0,
        "blocked_domains": [],
        "batches": {},
    }

    for domain, tenant, batch in rows:
        missing = []
        if not tenant.first_login_completed:
            missing.append("first_login_completed")
        if not tenant.admin_email:
            missing.append("admin_email")
        if not tenant.admin_password:
            missing.append("admin_password")
        if not domain.cloudflare_zone_id:
            missing.append("cloudflare_zone_id")

        batch_key = str(batch.id)
        batch_summary = summary["batches"].setdefault(
            batch_key,
            {
                "batch_id": batch_key,
                "batch_name": batch.name,
                "created_at": batch.created_at.isoformat() if batch.created_at else None,
                "domains": 0,
                "runnable_domains": 0,
                "already_complete_by_contract": 0,
                "incomplete_by_contract": 0,
            },
        )
        batch_summary["domains"] += 1

        if missing:
            summary["blocked_domains"].append(
                {
                    "domain_id": str(domain.id),
                    "domain": domain.name,
                    "tenant_id": str(tenant.id),
                    "tenant": tenant.name,
                    "batch_id": batch_key,
                    "batch": batch.name,
                    "missing": missing,
                }
            )
            continue

        summary["runnable_domains"] += 1
        batch_summary["runnable_domains"] += 1
        if _domain_contract_complete(domain):
            summary["already_complete_by_contract"] += 1
            batch_summary["already_complete_by_contract"] += 1
        else:
            summary["incomplete_by_contract"] += 1
            batch_summary["incomplete_by_contract"] += 1

    return summary


async def reset_step5_state(
    cutoff: datetime,
    batch_id: UUID | None = None,
    limit: int | None = None,
    incomplete_only: bool = False,
) -> list[str]:
    rows = await _load_cohort(cutoff, batch_id=batch_id, limit=limit)
    if incomplete_only:
        rows = [
            (domain, tenant, batch)
            for domain, tenant, batch in rows
            if not _domain_contract_complete(domain)
        ]
    if not rows:
        return []

    domain_ids = [domain.id for domain, tenant, batch in rows]
    tenant_ids = sorted({tenant.id for domain, tenant, batch in rows})
    domain_names = [domain.name for domain, tenant, batch in rows]

    async with async_session_factory() as db:
        domains = list((await db.execute(select(Domain).where(Domain.id.in_(domain_ids)))).scalars().all())
        tenants = list((await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))).scalars().all())
        batches = list(
            (
                await db.execute(
                    select(SetupBatch).where(
                        SetupBatch.id.in_(sorted({domain.batch_id for domain in domains if domain.batch_id}))
                    )
                )
            )
            .scalars()
            .all()
        )

        for domain in domains:
            domain.step5_complete = False
            domain.step5_retry_count = 0
            domain.step5_skipped = False
            domain.domain_added_to_m365 = False
            domain.domain_verified_in_m365 = False
            domain.domain_verified_at = None
            domain.m365_verified_at = None
            domain.mx_record_added = False
            domain.spf_record_added = False
            domain.autodiscover_added = False
            domain.dkim_cnames_added = False
            domain.dkim_enabled = False
            domain.dkim_enabled_at = None
            domain.dns_records_created = False
            domain.mx_configured = False
            domain.spf_configured = False
            domain.dmarc_configured = False
            domain.error_message = None
            domain.status = DomainStatus.PENDING_M365

        for tenant in tenants:
            tenant.step5_complete = False
            tenant.step5_completed_at = None
            tenant.step5_retry_count = 0
            tenant.domain_added_to_m365 = False
            tenant.domain_verified_in_m365 = False
            tenant.domain_verified_at = None
            tenant.mx_record_added = False
            tenant.spf_record_added = False
            tenant.autodiscover_added = False
            tenant.dkim_cnames_added = False
            tenant.dkim_enabled = False
            tenant.dkim_enabled_at = None
            tenant.setup_error = None
            if tenant.domain_id:
                tenant.status = TenantStatus.DOMAIN_LINKED
                tenant.setup_step = "6"

        for batch in batches:
            batch.m365_completed = 0
            batch.errors_count = 0
            batch.pipeline_status = "running"
            batch.pipeline_step = 6
            batch.pipeline_step_name = "M365 Domain Setup & DKIM repair"

        await db.commit()

    return domain_names


async def summarize_contract(cutoff: datetime, batch_id: UUID | None = None, limit: int | None = None) -> dict:
    rows = await _load_cohort(cutoff, batch_id=batch_id, limit=limit)
    complete = []
    incomplete = []
    for domain, tenant, batch in rows:
        entry = {
            "domain": domain.name,
            "batch_id": str(batch.id),
            "batch": batch.name,
            "tenant": tenant.name,
            "step5_complete": bool(domain.step5_complete),
            "verified": bool(domain.domain_verified_in_m365),
            "dkim_enabled": bool(domain.dkim_enabled),
            "mx": bool(domain.mx_record_added),
            "spf": bool(domain.spf_record_added),
            "autodiscover": bool(domain.autodiscover_added),
            "dkim_cnames": bool(domain.dkim_cnames_added),
            "dmarc": bool(domain.dmarc_configured),
            "status": str(domain.status.value if hasattr(domain.status, "value") else domain.status),
            "error": domain.error_message,
        }
        if _domain_contract_complete(domain):
            complete.append(entry)
        else:
            incomplete.append(entry)
    return {"complete": complete, "incomplete": incomplete}


async def run_repair(args: argparse.Namespace) -> int:
    cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    batch_id = UUID(args.batch_id) if args.batch_id else None

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = LOG_DIR / f"repair_{run_id}.json"

    before = await inventory(cutoff, batch_id=batch_id, limit=args.limit)
    LOGGER.info("Inventory: %s", json.dumps(before, default=str))
    if before["blocked_domains"]:
        LOGGER.error("Blocked domains found; refusing to mutate until blockers are fixed")
        report_path.write_text(json.dumps({"before": before}, indent=2), encoding="utf-8")
        return 2

    if not args.execute:
        print(json.dumps(before, indent=2))
        report_path.write_text(json.dumps({"before": before, "dry_run": True}, indent=2), encoding="utf-8")
        return 0

    reset_domains = await reset_step5_state(
        cutoff,
        batch_id=batch_id,
        limit=args.limit,
        incomplete_only=args.resume_incomplete_only,
    )
    LOGGER.info("Reset %s domains for Step 5 repair", len(reset_domains))

    after_reset = await inventory(cutoff, batch_id=batch_id, limit=args.limit)
    batch_ids = list(after_reset["batches"].keys())
    batch_results = {}

    for batch_key in batch_ids:
        LOGGER.info("Starting Step 5 wizard repair for batch %s", batch_key)
        kill_all_browsers()

        def on_progress(domain_id, step, status):
            LOGGER.info("Progress batch=%s domain_id=%s step=%s status=%s", batch_key, domain_id, step, status)

        result = await run_step5_for_batch(
            UUID(batch_key),
            on_progress=on_progress,
            max_workers=args.max_workers,
            chunk_size=args.chunk_size,
        )
        batch_results[batch_key] = result
        LOGGER.info("Step 5 wizard repair result for batch %s: %s", batch_key, json.dumps(result, default=str))

        if args.objective_reconcile:
            LOGGER.info("Starting objective reconciliation for batch %s", batch_key)
            recon = await objective_reconcile_batch(
                UUID(batch_key),
                auto_fix=False,
                final_security_smtp=False,
            )
            batch_results[batch_key]["objective_reconciliation"] = recon
            LOGGER.info("Objective reconciliation result for batch %s: %s", batch_key, json.dumps(recon, default=str))

    final = await summarize_contract(cutoff, batch_id=batch_id, limit=args.limit)
    report = {
        "run_id": run_id,
        "cutoff_utc": cutoff.isoformat(),
        "before": before,
        "after_reset": after_reset,
        "batch_results": batch_results,
        "final": final,
        "success": len(final["incomplete"]) == 0,
    }
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"report_path": str(report_path), "success": report["success"], "complete": len(final["complete"]), "incomplete": len(final["incomplete"])}, indent=2))
    return 0 if report["success"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF.isoformat())
    parser.add_argument("--batch-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Reset matching Step 5 state and run the repair. Without this flag the command is read-only.",
    )
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--chunk-size", type=int, default=1)
    parser.add_argument("--objective-reconcile", action="store_true")
    parser.add_argument(
        "--resume-incomplete-only",
        action="store_true",
        help="Only clear Step 5 state for domains that are not already complete by the strict contract.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(run_repair(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
