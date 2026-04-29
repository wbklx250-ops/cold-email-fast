"""
Repair jobs for per-domain licensed users and mailbox delegation.

This is intentionally separate from the pipeline Step 7 selector. The normal
pipeline skips domains that are already marked complete, but this repair needs
to revisit completed tenants where shared mailboxes exist and delegation failed
because the per-domain licensed user was missing or unlicensed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from app.db.session import BackgroundSessionLocal, async_session_factory
from app.models.batch import SetupBatch
from app.models.domain import Domain
from app.models.mailbox import Mailbox
from app.models.tenant import Tenant
from app.services.step7_fast import process_domain_fast

logger = logging.getLogger(__name__)


licensed_user_repair_jobs: Dict[str, Dict[str, Any]] = {}


def _is_onmicrosoft_domain(domain: Optional[str]) -> bool:
    return bool(domain and domain.strip().lower().endswith(".onmicrosoft.com"))


def _display_name_for(domain: Domain, tenant: Tenant, batch: Optional[SetupBatch]) -> str:
    first = (domain.persona_first_name or "").strip()
    last = (domain.persona_last_name or "").strip()
    display_name = f"{first} {last}".strip()
    if display_name:
        return display_name

    if batch:
        first = (batch.persona_first_name or "").strip()
        last = (batch.persona_last_name or "").strip()
        display_name = f"{first} {last}".strip()
        if display_name:
            return display_name

    return (tenant.contact_name or tenant.name or "Mailbox User").strip()


def _batch_data(batch: Optional[SetupBatch]) -> Optional[Dict[str, Any]]:
    if not batch:
        return None
    return {
        "persona_first_name": batch.persona_first_name,
        "persona_last_name": batch.persona_last_name,
        "custom_mailbox_map": batch.custom_mailbox_map,
        "mailboxes_per_tenant": batch.mailboxes_per_tenant or 50,
    }


async def _mailbox_count_for_domain(tenant_id: UUID, domain_name: str) -> int:
    async with async_session_factory() as db:
        return (
            await db.scalar(
                select(func.count(Mailbox.id)).where(
                    Mailbox.tenant_id == tenant_id,
                    Mailbox.email.like(f"%@{domain_name}"),
                )
            )
            or 0
        )


async def _load_work_items(
    *,
    batch_id: Optional[UUID],
    tenant_ids: Optional[List[UUID]],
    all_tenants: bool,
    include_unverified: bool,
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    if not batch_id and not tenant_ids and not all_tenants:
        raise ValueError("Provide batch_id, tenant_ids, or all_tenants=true")

    async with async_session_factory() as db:
        query = (
            select(Domain, Tenant, SetupBatch)
            .join(Tenant, Domain.tenant_id == Tenant.id)
            .outerjoin(SetupBatch, SetupBatch.id == func.coalesce(Domain.batch_id, Tenant.batch_id))
            .where(Domain.tenant_id.isnot(None))
            .order_by(Tenant.created_at, Domain.domain_index_in_tenant, Domain.name)
        )

        if batch_id:
            query = query.where(Domain.batch_id == batch_id)
        if tenant_ids:
            query = query.where(Domain.tenant_id.in_(tenant_ids))
        if limit:
            query = query.limit(limit)

        rows = (await db.execute(query)).all()

    work_items: List[Dict[str, Any]] = []
    for domain, tenant, batch in rows:
        domain_name = (domain.name or "").strip().lower()
        if not domain_name or _is_onmicrosoft_domain(domain_name):
            continue
        if tenant.onmicrosoft_domain and domain_name == tenant.onmicrosoft_domain.lower():
            continue

        mailbox_count = await _mailbox_count_for_domain(tenant.id, domain_name)
        is_added_or_used = (
            bool(domain.domain_added_to_m365)
            or bool(domain.domain_verified_in_m365)
            or mailbox_count > 0
        )
        if not include_unverified and not is_added_or_used:
            continue

        work_items.append(
            {
                "domain_name": domain_name,
                "domain_id": domain.id,
                "tenant_id": tenant.id,
                "tenant_name": tenant.name,
                "tenant_onmicrosoft_domain": tenant.onmicrosoft_domain,
                "admin_email": tenant.admin_email,
                "admin_password": tenant.admin_password,
                "batch_id": domain.batch_id or tenant.batch_id,
                "batch_name": batch.name if batch else None,
                "display_name": _display_name_for(domain, tenant, batch),
                "batch_data": _batch_data(batch),
                "domain_index": domain.domain_index_in_tenant or 0,
                "mailboxes_per_tenant": (batch.mailboxes_per_tenant if batch else None) or 50,
                "persona_first_name": domain.persona_first_name,
                "persona_last_name": domain.persona_last_name,
                "mailbox_count": mailbox_count,
                "db_licensed_user_upn": domain.licensed_user_upn,
                "db_licensed_user_created": bool(domain.licensed_user_created),
                "domain_verified_in_m365": bool(domain.domain_verified_in_m365),
                "domain_added_to_m365": bool(domain.domain_added_to_m365),
            }
        )

    return work_items


def _public_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "domain": item["domain_name"],
        "tenant_id": str(item["tenant_id"]),
        "tenant_name": item["tenant_name"],
        "tenant_onmicrosoft_domain": item["tenant_onmicrosoft_domain"],
        "batch_id": str(item["batch_id"]) if item.get("batch_id") else None,
        "batch_name": item["batch_name"],
        "mailbox_count": item["mailbox_count"],
        "db_licensed_user_upn": item["db_licensed_user_upn"],
        "db_licensed_user_created": item["db_licensed_user_created"],
        "domain_added_to_m365": item["domain_added_to_m365"],
        "domain_verified_in_m365": item["domain_verified_in_m365"],
    }


async def _reset_delegation_flags(item: Dict[str, Any]) -> None:
    async with BackgroundSessionLocal() as db:
        await db.execute(
            update(Mailbox)
            .where(
                Mailbox.tenant_id == item["tenant_id"],
                Mailbox.email.like(f"%@{item['domain_name']}"),
            )
            .values(delegated=False)
        )
        await db.commit()


async def run_licensed_user_repair(
    *,
    batch_id: Optional[UUID] = None,
    tenant_ids: Optional[List[UUID]] = None,
    all_tenants: bool = False,
    dry_run: bool = True,
    include_unverified: bool = False,
    reset_delegation_flags: bool = True,
    max_parallel: int = 2,
    limit: Optional[int] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Repair per-domain licensed users and rerun mailbox setup/delegation.

    Dry-run mode only reports the domains that would be processed. Live mode
    invokes the idempotent Step 7 fast domain processor for each custom domain.
    """
    if not job_id:
        job_id = f"licensed_user_repair_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

    summary: Dict[str, Any] = {
        "job_id": job_id,
        "status": "running",
        "dry_run": dry_run,
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "batch_id": str(batch_id) if batch_id else None,
        "all_tenants": all_tenants,
        "tenant_ids": [str(tid) for tid in tenant_ids] if tenant_ids else [],
        "total_domains": 0,
        "processed": 0,
        "successful": 0,
        "failed": 0,
        "skipped": 0,
        "results": [],
        "errors": [],
    }
    licensed_user_repair_jobs[job_id] = summary

    try:
        work_items = await _load_work_items(
            batch_id=batch_id,
            tenant_ids=tenant_ids,
            all_tenants=all_tenants,
            include_unverified=include_unverified,
            limit=limit,
        )
        summary["total_domains"] = len(work_items)

        if dry_run:
            summary["status"] = "completed"
            summary["processed"] = len(work_items)
            summary["results"] = [
                {**_public_item(item), "status": "would_process"}
                for item in work_items
            ]
            summary["completed_at"] = datetime.utcnow().isoformat()
            return summary

        semaphore = asyncio.Semaphore(max(1, min(max_parallel, 10)))

        async def _process_one(item: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                public = _public_item(item)
                if not item.get("admin_email") or not item.get("admin_password"):
                    public["status"] = "skipped"
                    public["error"] = "Missing admin credentials"
                    summary["skipped"] += 1
                    return public

                try:
                    if reset_delegation_flags:
                        await _reset_delegation_flags(item)

                    result = await process_domain_fast(
                        domain_name=item["domain_name"],
                        domain_id=item["domain_id"],
                        tenant_id=item["tenant_id"],
                        admin_email=item["admin_email"],
                        admin_password=item["admin_password"],
                        display_name=item["display_name"],
                        batch_id=item["batch_id"],
                        batch_data=item["batch_data"],
                        domain_index=item["domain_index"],
                        mailboxes_per_tenant=item["mailboxes_per_tenant"],
                        persona_first_name=item["persona_first_name"],
                        persona_last_name=item["persona_last_name"],
                    )

                    public.update(
                        {
                            "status": "success" if result.get("success") else "failed",
                            "created": result.get("created", 0),
                            "delegated": result.get("delegated", 0),
                            "passwords_set": result.get("passwords_set", 0),
                            "elapsed_seconds": result.get("elapsed_seconds"),
                            "error": result.get("error"),
                        }
                    )
                    if result.get("success"):
                        summary["successful"] += 1
                    else:
                        summary["failed"] += 1
                    return public

                except Exception as exc:
                    logger.exception("[%s] Licensed user repair failed", item["domain_name"])
                    summary["failed"] += 1
                    public["status"] = "failed"
                    public["error"] = str(exc)
                    return public

        tasks = [_process_one(item) for item in work_items]
        for coro in asyncio.as_completed(tasks):
            result = await coro
            summary["processed"] += 1
            summary["results"].append(result)

        summary["status"] = "completed" if summary["failed"] == 0 else "completed_with_errors"
        summary["completed_at"] = datetime.utcnow().isoformat()
        return summary

    except Exception as exc:
        logger.exception("Licensed user repair job %s crashed", job_id)
        summary["status"] = "error"
        summary["completed_at"] = datetime.utcnow().isoformat()
        summary["errors"].append(str(exc))
        return summary
