"""API endpoints for repairing per-domain licensed users and delegation."""

from __future__ import annotations

import logging
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.services.licensed_user_repair import (
    licensed_user_repair_jobs,
    run_licensed_user_repair,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/licensed-user-repair",
    tags=["licensed-user-repair"],
)


class LicensedUserRepairRequest(BaseModel):
    batch_id: Optional[UUID] = None
    tenant_ids: Optional[List[UUID]] = None
    all_tenants: bool = False
    dry_run: bool = True
    include_unverified: bool = False
    reset_delegation_flags: bool = True
    max_parallel: int = Field(default=2, ge=1, le=10)
    limit: Optional[int] = Field(default=None, ge=1)


@router.post("/start")
async def start_licensed_user_repair(
    request: LicensedUserRepairRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Start a repair job.

    Use dry_run=true first to inspect the domains that will be touched. For a
    live all-tenant run, all_tenants=true must be explicit.
    """
    if not request.batch_id and not request.tenant_ids and not request.all_tenants:
        raise HTTPException(
            status_code=400,
            detail="Provide batch_id, tenant_ids, or all_tenants=true",
        )

    job_id = f"licensed_user_repair_{uuid4().hex[:12]}"
    licensed_user_repair_jobs[job_id] = {
        "job_id": job_id,
        "status": "scheduled",
        "dry_run": request.dry_run,
    }

    logger.info(
        "Scheduling licensed-user repair job %s dry_run=%s batch_id=%s all_tenants=%s",
        job_id,
        request.dry_run,
        request.batch_id,
        request.all_tenants,
    )
    background_tasks.add_task(
        run_licensed_user_repair,
        batch_id=request.batch_id,
        tenant_ids=request.tenant_ids,
        all_tenants=request.all_tenants,
        dry_run=request.dry_run,
        include_unverified=request.include_unverified,
        reset_delegation_flags=request.reset_delegation_flags,
        max_parallel=request.max_parallel,
        limit=request.limit,
        job_id=job_id,
    )

    return {
        "success": True,
        "job_id": job_id,
        "status": "scheduled",
        "dry_run": request.dry_run,
        "message": "Licensed user repair scheduled. Poll /status/{job_id}.",
    }


@router.get("/status/{job_id}")
async def get_licensed_user_repair_status(job_id: str) -> dict:
    job = licensed_user_repair_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Repair job not found")
    return job


@router.get("/jobs")
async def list_licensed_user_repair_jobs() -> dict:
    jobs = list(licensed_user_repair_jobs.values())
    jobs.sort(key=lambda job: job.get("started_at") or job.get("job_id") or "", reverse=True)
    return {"jobs": jobs}
