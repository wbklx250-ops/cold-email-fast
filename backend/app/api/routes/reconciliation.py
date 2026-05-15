"""
Reconciliation API routes.

Endpoints:
    POST /api/v1/reconciliation/batches/{batch_id}/verify
        Kick off a Graph-primary reconciliation for a batch. Returns 202 with
        the job descriptor; the work runs in a BackgroundTask.

    GET /api/v1/reconciliation/batches/{batch_id}/status
        Returns the latest in-memory reconciliation summary for the batch.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.services.batch_reconciliation import reconcile_batch, reconciliation_jobs
from app.services.objective_reconciliation import (
    objective_reconcile_batch,
    objective_reconciliation_jobs,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/reconciliation",
    tags=["reconciliation"],
)


@router.post("/batches/{batch_id}/verify")
async def start_batch_verify(
    batch_id: UUID,
    background_tasks: BackgroundTasks,
    auto_fix: bool = True,
) -> dict:
    """
    Start a reconciliation job for a batch. Returns immediately with a job
    descriptor; actual work runs in the background.

    Query params:
        auto_fix (default True): if True, repair detected drift; if False,
                                 verify only.
    """
    job_key = str(batch_id)

    existing = reconciliation_jobs.get(job_key)
    if existing and existing.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail=f"Reconciliation already running for batch {batch_id}",
        )

    logger.info(
        "Scheduling reconciliation for batch %s (auto_fix=%s)", batch_id, auto_fix
    )
    background_tasks.add_task(reconcile_batch, batch_id, auto_fix)

    return {
        "batch_id": job_key,
        "status": "scheduled",
        "auto_fix": auto_fix,
        "message": "Reconciliation started in background. Poll /status for progress.",
    }


@router.get("/batches/{batch_id}/status")
async def get_batch_status(batch_id: UUID) -> dict:
    """Return the current in-memory reconciliation summary for a batch."""
    job_key = str(batch_id)
    job = reconciliation_jobs.get(job_key)
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"No reconciliation job found for batch {batch_id}",
        )
    return job


@router.post("/batches/{batch_id}/objective")
async def start_objective_batch_reconciliation(
    batch_id: UUID,
    background_tasks: BackgroundTasks,
    auto_fix: bool = True,
) -> dict:
    """
    Start objective reconciliation.

    This does not trust local completion flags. It checks Microsoft 365,
    Exchange Online, and Cloudflare directly, then repairs drift when
    auto_fix=True.
    """
    job_key = str(batch_id)
    existing = objective_reconciliation_jobs.get(job_key)
    if existing and existing.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail=f"Objective reconciliation already running for batch {batch_id}",
        )

    background_tasks.add_task(objective_reconcile_batch, batch_id, auto_fix)
    return {
        "batch_id": job_key,
        "status": "scheduled",
        "auto_fix": auto_fix,
        "message": "Objective reconciliation started. Poll /objective-status.",
    }


@router.get("/batches/{batch_id}/objective-status")
async def get_objective_batch_status(batch_id: UUID) -> dict:
    job_key = str(batch_id)
    job = objective_reconciliation_jobs.get(job_key)
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"No objective reconciliation job found for batch {batch_id}",
        )
    return job


@router.post("/batches/{batch_id}/objective-recoverable")
async def start_recoverable_objective_reconciliation(
    batch_id: UUID,
    background_tasks: BackgroundTasks,
    auto_fix: bool = True,
) -> dict:
    """
    Re-run objective reconciliation only for incomplete domains that are not
    marked as active-tenant blockers.
    """
    job_key = f"{batch_id}:recoverable"
    existing = objective_reconciliation_jobs.get(job_key)
    if existing and existing.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail=f"Recoverable objective reconciliation already running for batch {batch_id}",
        )

    background_tasks.add_task(
        objective_reconcile_batch,
        batch_id,
        auto_fix,
        recoverable_only=True,
        final_security_smtp=False,
        job_key_suffix="recoverable",
    )
    return {
        "batch_id": str(batch_id),
        "status": "scheduled",
        "auto_fix": auto_fix,
        "mode": "recoverable",
        "message": "Recoverable objective reconciliation started. Poll /objective-recoverable-status.",
    }


@router.get("/batches/{batch_id}/objective-recoverable-status")
async def get_recoverable_objective_status(batch_id: UUID) -> dict:
    job_key = f"{batch_id}:recoverable"
    job = objective_reconciliation_jobs.get(job_key)
    if not job:
        raise HTTPException(
            status_code=404,
            detail=f"No recoverable objective reconciliation job found for batch {batch_id}",
        )
    return job
