"""Review and execute durable bulk domain replacements."""
from uuid import UUID
from copy import deepcopy

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.batch import SetupBatch
from app.models.domain_swap import DomainSwapJob
from app.services.domain_swap import build_plan, public_job, start_plan, SwapConflict

router = APIRouter(prefix="/api/v1/domain-swaps", tags=["domain-swaps"])


class PreviewRequest(BaseModel):
    name: str = Field(default="Domain swap", min_length=1, max_length=200)
    sources: list[str] = Field(min_length=1, max_length=500)
    replacements: list[str] = Field(min_length=1, max_length=500)

    @field_validator("sources", "replacements")
    @classmethod
    def nonempty_entries(cls, values):
        if any(not value.strip() or len(value) > 255 for value in values):
            raise ValueError("Every entry must contain between 1 and 255 characters")
        return values


async def get_job(db, job_id, lock=False):
    query = select(DomainSwapJob).where(DomainSwapJob.id == job_id)
    if lock:
        query = query.with_for_update()
    job = (await db.scalars(query)).one_or_none()
    if not job:
        raise HTTPException(404, "Swap not found")
    return job


async def job_view(db, job):
    # Read current pipeline progress even while the serialized worker is busy.
    result = public_job(job)
    result["mappings"] = deepcopy(result["mappings"])
    for row in result["mappings"]:
        if row["batch_id"]:
            batch = await db.get(SetupBatch, UUID(row["batch_id"]))
            if batch:
                row["pipeline"] = {"status": batch.pipeline_status, "step": batch.pipeline_step,
                                   "message": batch.pipeline_step_name}
                if batch.pipeline_status == "completed":
                    row["phase"], row["error"] = "completed", None
    if result["mappings"] and all(r["phase"] == "completed" for r in result["mappings"]):
        result["status"] = "completed"
    return result


@router.post("/preview")
async def preview(request: PreviewRequest, db: AsyncSession = Depends(get_db_session)):
    try:
        rows, errors = await build_plan(db, request.sources, request.replacements)
    except SwapConflict as exc:
        return {"valid": False, "errors": [{"row": 0, "error": str(exc)}], "mappings": []}
    if errors:
        return {"valid": False, "errors": errors, "mappings": rows}
    job = DomainSwapJob(name=request.name.strip() or "Domain swap", status="preview", mappings=rows)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return {"valid": True, "errors": [], "job": public_job(job), "mappings": rows}


@router.post("/{job_id}/start")
async def start(job_id: UUID, db: AsyncSession = Depends(get_db_session)):
    job = await get_job(db, job_id, lock=True)
    try:
        await start_plan(db, job)
    except (SwapConflict, IntegrityError) as exc:
        await db.rollback()
        message = str(exc) if isinstance(exc, SwapConflict) else "Another swap reserved one of these tenants or domains"
        raise HTTPException(409, message)
    return public_job(job)


@router.post("/{job_id}/retry")
async def retry(job_id: UUID, db: AsyncSession = Depends(get_db_session)):
    job = await get_job(db, job_id, lock=True)
    if job.status in ("queued", "running", "completed"):
        return public_job(job)
    if job.status != "attention":
        raise HTTPException(409, "Review and start the swap before retrying")
    for row in job.mappings:
        if row["batch_id"]:
            batch = await db.get(SetupBatch, UUID(row["batch_id"]))
            if batch and batch.pipeline_status == "running":
                raise HTTPException(409, "Replacement setup is already running; follow its progress")
    job.status = "queued"
    await db.commit()
    return public_job(job)


@router.get("")
async def list_jobs(db: AsyncSession = Depends(get_db_session)):
    jobs = (await db.scalars(select(DomainSwapJob).where(DomainSwapJob.status != "preview")
                            .order_by(DomainSwapJob.created_at.desc()).limit(50))).all()
    return {"jobs": [await job_view(db, job) for job in jobs]}


@router.get("/{job_id}")
async def status(job_id: UUID, db: AsyncSession = Depends(get_db_session)):
    return await job_view(db, await get_job(db, job_id))
