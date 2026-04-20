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
