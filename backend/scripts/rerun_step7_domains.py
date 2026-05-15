"""Targeted Step 7 mailbox rerun for specific tenant/domain pairs.

Runs the fast mailbox creation path directly for an explicit allowlist.
Intended for production recovery cases where a batch-level retry would be too broad.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.getenv("DEBUG") not in (None, "", "true", "false", "True", "False", "1", "0"):
    os.environ["DEBUG"] = "false"


from sqlalchemy import func, select  # noqa: E402

from app.db.session import async_session_factory  # noqa: E402
from app.models.batch import SetupBatch  # noqa: E402
from app.models.domain import Domain  # noqa: E402
from app.models.mailbox import Mailbox  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.services.step7_fast import process_domain_fast  # noqa: E402


logger = logging.getLogger("rerun_step7_domains")


def parse_target(value: str) -> tuple[str, list[str]]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("target must be admin@tenant=domain1,domain2")
    admin_email, raw_domains = value.split("=", 1)
    admin_email = admin_email.strip().lower()
    domains = [d.strip().lower() for d in raw_domains.split(",") if d.strip()]
    if not admin_email or not domains:
        raise argparse.ArgumentTypeError("target must include an admin email and at least one domain")
    return admin_email, domains


async def mailbox_counts(db, tenant_id, domain_name: str) -> dict[str, int]:
    base = [Mailbox.tenant_id == tenant_id, Mailbox.email.ilike(f"%@{domain_name}")]

    async def count(*extra) -> int:
        return await db.scalar(select(func.count(Mailbox.id)).where(*base, *extra)) or 0

    return {
        "total": await count(),
        "created": await count(Mailbox.created_in_exchange == True),  # noqa: E712
        "delegated": await count(Mailbox.delegated == True),  # noqa: E712
        "password_set": await count(Mailbox.password_set == True),  # noqa: E712
        "account_enabled": await count(Mailbox.account_enabled == True),  # noqa: E712
        "upn_fixed": await count(Mailbox.upn_fixed == True),  # noqa: E712
    }


async def collect_work(batch_id: UUID, targets: list[tuple[str, list[str]]], dry_run: bool):
    domain_names = [domain for _, domains in targets for domain in domains]
    admin_by_domain = {domain: admin for admin, domains in targets for domain in domains}

    async with async_session_factory() as db:
        batch = await db.get(SetupBatch, batch_id)
        if not batch:
            raise RuntimeError(f"Batch not found: {batch_id}")

        batch_data = {
            "persona_first_name": batch.persona_first_name,
            "persona_last_name": batch.persona_last_name,
            "custom_mailbox_map": batch.custom_mailbox_map,
            "mailboxes_per_tenant": batch.mailboxes_per_tenant or 50,
        }

        result = await db.execute(
            select(Domain).where(
                Domain.batch_id == batch_id,
                Domain.name.in_(domain_names),
            )
        )
        domains_by_name = {d.name.lower(): d for d in result.scalars().all()}

        missing = [name for name in domain_names if name not in domains_by_name]
        if missing:
            raise RuntimeError(f"Domain(s) not found in batch: {', '.join(missing)}")

        work_items = []
        print(
            f"Batch {batch_id} ({batch.name}) mailboxes_per_tenant={batch.mailboxes_per_tenant or 50}",
            flush=True,
        )

        for domain_name in domain_names:
            domain = domains_by_name[domain_name]
            tenant = await db.get(Tenant, domain.tenant_id) if domain.tenant_id else None
            expected_admin = admin_by_domain[domain_name]
            if not tenant:
                raise RuntimeError(f"{domain_name}: no linked tenant")
            if (tenant.admin_email or "").lower() != expected_admin:
                raise RuntimeError(
                    f"{domain_name}: linked to {tenant.admin_email}, expected {expected_admin}"
                )
            if str(tenant.batch_id) != str(batch_id):
                raise RuntimeError(f"{domain_name}: tenant is not in batch {batch_id}")
            if not tenant.admin_email or not tenant.admin_password:
                raise RuntimeError(f"{domain_name}: tenant is missing admin credentials")

            counts = await mailbox_counts(db, tenant.id, domain_name)
            print(
                {
                    "domain": domain_name,
                    "tenant_admin": tenant.admin_email,
                    "domain_id": str(domain.id),
                    "tenant_id": str(tenant.id),
                    "domain_index": domain.domain_index_in_tenant,
                    "verified": domain.domain_verified_in_m365,
                    "dkim_enabled": domain.dkim_enabled,
                    "step6_complete": domain.step6_complete,
                    "step6_skipped": domain.step6_skipped,
                    "licensed_user_upn": domain.licensed_user_upn,
                    "mailboxes_before": counts,
                },
                flush=True,
            )

            if dry_run:
                continue

            work_items.append(
                {
                    "domain_name": domain.name,
                    "domain_id": domain.id,
                    "tenant_id": tenant.id,
                    "admin_email": tenant.admin_email,
                    "admin_password": tenant.admin_password,
                    "display_name": f"{batch.persona_first_name or ''} {batch.persona_last_name or ''}".strip(),
                    "batch_id": batch.id,
                    "batch_data": batch_data,
                    "domain_index": domain.domain_index_in_tenant or 0,
                    "mailboxes_per_tenant": batch.mailboxes_per_tenant or 50,
                    "persona_first_name": domain.persona_first_name,
                    "persona_last_name": domain.persona_last_name,
                }
            )

        return batch, work_items


async def update_batch_counts(batch_id: UUID) -> None:
    async with async_session_factory() as db:
        completed = await db.scalar(
            select(func.count(Domain.id)).where(
                Domain.batch_id == batch_id,
                Domain.tenant_id.isnot(None),
                Domain.step6_complete == True,  # noqa: E712
            )
        ) or 0
        batch = await db.get(SetupBatch, batch_id)
        if batch:
            batch.mailboxes_completed_count = completed
            await db.commit()


async def print_final_counts(batch_id: UUID, work_items: list[dict]) -> None:
    async with async_session_factory() as db:
        print("FINAL COUNTS", flush=True)
        for item in work_items:
            counts = await mailbox_counts(db, item["tenant_id"], item["domain_name"])
            domain = await db.get(Domain, item["domain_id"])
            print(
                {
                    "domain": item["domain_name"],
                    "success_flag": bool(domain.step6_complete) if domain else None,
                    "step6_mailboxes_created": domain.step6_mailboxes_created if domain else None,
                    "error_message": domain.error_message if domain else None,
                    "mailboxes_after": counts,
                },
                flush=True,
            )


async def async_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--target", action="append", required=True, type=parse_target)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    batch_id = UUID(args.batch_id)
    batch, work_items = await collect_work(batch_id, args.target, args.dry_run)
    if args.dry_run:
        print("Dry run complete. No mailbox changes were made.", flush=True)
        return 0

    successful = 0
    failed = 0
    started = datetime.utcnow()

    for index, item in enumerate(work_items, 1):
        print(
            f"START {index}/{len(work_items)} {item['domain_name']} at {datetime.utcnow().isoformat()}Z",
            flush=True,
        )
        result = await process_domain_fast(**item)
        print({"domain": item["domain_name"], "result": result}, flush=True)
        if result.get("success"):
            successful += 1
        else:
            failed += 1

    await update_batch_counts(batch.id)
    await print_final_counts(batch.id, work_items)

    elapsed = (datetime.utcnow() - started).total_seconds()
    print(
        {
            "success": failed == 0,
            "total": len(work_items),
            "successful": successful,
            "failed": failed,
            "elapsed_seconds": elapsed,
        },
        flush=True,
    )
    return 0 if failed == 0 else 2


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
