"""Operator-only tenant readiness jobs, reports, and live Microsoft handoffs."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import queue
import time
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import get_settings
from app.models.readiness import ReadinessCheck
from app.services.inventory_credentials import encrypt_inventory_value
from app.services import readiness_engine as engine
from app.services.readiness_worker import ARTIFACTS, LIVE, now
from app.services import readiness_worker

router = APIRouter(prefix='/api/v1/tenant-readiness', tags=['tenant-readiness'])
COOKIE = 'readiness_session'


def session_token(stamp=None):
    stamp = str(int(time.time()) if stamp is None else stamp)
    key = get_settings().readiness_operator_key
    return stamp + '.' + hmac.new(key.encode(), stamp.encode(), hashlib.sha256).hexdigest()


def authorized(request: Request):
    key = get_settings().readiness_operator_key
    if not key:
        raise HTTPException(503, 'Tenant readiness has not been configured on this server.')
    supplied = request.headers.get('x-readiness-key', '')
    if supplied and hmac.compare_digest(supplied, key):
        return
    token = request.cookies.get(COOKIE, '')
    stamp = token.partition('.')[0]
    if stamp.isdigit() and 0 <= time.time() - int(stamp) < 86400 and hmac.compare_digest(token, session_token(stamp)):
        return
    raise HTTPException(401, 'Sign in to tenant readiness with your access key.')


class Login(BaseModel):
    key: str = Field(max_length=200)


@router.post('/login')
def login(body: Login, response: Response):
    expected = get_settings().readiness_operator_key
    if not expected or not hmac.compare_digest(body.key, expected):
        raise HTTPException(401, 'The access key was not accepted.')
    response.set_cookie(COOKIE, session_token(), max_age=86400, httponly=True, secure=True, samesite='strict', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return {'authenticated': True}


class ImportedTenant(BaseModel):
    id: UUID
    admin: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=1000)
    number: str = Field(default='', max_length=120)
    state: dict = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)


class ImportRequest(BaseModel):
    tenants: list[ImportedTenant] = Field(min_length=1, max_length=500)


def view(row):
    safe = {k: v for k, v in row.state.items() if k in set(engine.REPORT_FIELDS) | {'send_accepted'}}
    live = LIVE.get(str(row.tenant_id))
    return {**safe, 'id':str(row.id), 'tenant_id':str(row.tenant_id), 'admin':row.admin_email,
        'tenant_number':row.label, 'status':row.status,
        'browser_available':bool(live and live.get('waiting')), 'attempts':row.attempts}


async def import_records(body: ImportRequest, db: AsyncSession):
    ids = [tenant.id for tenant in body.tenants]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, 'The upload contains a repeated tenant GUID.')
    # Serialize imports so a simultaneous upload cannot enqueue the same tenant twice.
    await db.execute(text('SELECT pg_advisory_xact_lock(725881031)'))
    existing = {row.tenant_id: row for row in (await db.scalars(
        select(ReadinessCheck).where(ReadinessCheck.tenant_id.in_(ids)).with_for_update())).all()}
    for tenant in body.tenants:
        if '@' not in tenant.admin or any(c.isspace() for c in tenant.admin):
            raise HTTPException(422, 'Each administrator must have a valid Microsoft sign-in address.')
        if any(name not in ARTIFACTS or len(value) > 2_000_000 for name, value in tenant.artifacts.items()):
            raise HTTPException(422, 'An imported authentication checkpoint is invalid.')
        row = existing.get(tenant.id)
        if row and row.lease_owner and row.lease_until and row.lease_until > now():
            raise HTTPException(409, 'One of these tenants is already being processed. Wait for that check before importing it again.')
        if row and row.admin_email != tenant.admin.lower():
            raise HTTPException(409, 'This tenant GUID is already registered with a different administrator.')
        if not row:
            allowed = set(engine.REPORT_FIELDS) | {'send_accepted', 'trial_skus_added'}
            state = {k: v for k,v in tenant.state.items() if k in allowed}
            imported_status = state.get('status', '')
            status = imported_status if imported_status in engine.TERMINAL else 'queued'
            state.update(tenant_id=str(tenant.id), admin=tenant.admin.lower(), tenant_number=tenant.number,
                recipient=engine.RECIPIENT, renewal_setting='unchanged', status=status)
            row = ReadinessCheck(tenant_id=tenant.id, admin_email=tenant.admin.lower(), label=tenant.number,
                status=status, state=state, artifacts_ciphertext=encrypt_inventory_value(json.dumps(tenant.artifacts)))
            db.add(row)
        # Reimporting a completed probe never resets its subject/send timestamp.
        row.credentials_ciphertext = encrypt_inventory_value(json.dumps({'password':tenant.password}))
    await db.commit()
    return {'imported':len(body.tenants), 'recipient':engine.RECIPIENT}


@router.post('/import', dependencies=[Depends(authorized)])
async def import_checkpoints(body: ImportRequest, db: AsyncSession = Depends(get_db)):
    return await import_records(body, db)


@router.post('/upload', dependencies=[Depends(authorized)])
async def upload(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    raw = await file.read(5_000_001)
    if len(raw) > 5_000_000:
        raise HTTPException(413, 'The CSV must be under 5 MB.')
    try:
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
        tenants = [ImportedTenant(id=r['TargetTenantID'].strip(), admin=r['AdminUsername'].strip(),
            password=r['AdminPassword'], number=r.get('Tenant Number','').strip()) for r in reader]
        body = ImportRequest(tenants=tenants)
    except Exception:
        raise HTTPException(422, 'Use a CSV with TargetTenantID, AdminUsername and AdminPassword columns (up to 500 tenants).')
    return await import_records(body, db)


@router.get('/checks', dependencies=[Depends(authorized)])
async def checks(response: Response, db: AsyncSession = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    rows = (await db.scalars(select(ReadinessCheck).order_by(ReadinessCheck.created_at))).all()
    counts = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return {'checks':[view(row) for row in rows], 'counts':counts,
        'parallel_browsers':readiness_worker.WORKER.parallel if readiness_worker.WORKER else get_settings().readiness_parallel_browsers,
        'recipient':engine.RECIPIENT}


@router.get('/report.csv', dependencies=[Depends(authorized)])
async def report(db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(ReadinessCheck).order_by(ReadinessCheck.created_at))).all()
    output = io.StringIO()
    writer = csv.DictWriter(output, engine.REPORT_FIELDS, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for k,v in view(row).items()})
    return Response('\ufeff' + output.getvalue(), media_type='text/csv',
        headers={'Content-Disposition':'attachment; filename="tenant-readiness.csv"', 'Cache-Control':'no-store'})


@router.post('/{tenant_id}/retry', dependencies=[Depends(authorized)])
async def retry(tenant_id: UUID, db: AsyncSession = Depends(get_db)):
    row = await db.scalar(select(ReadinessCheck).where(ReadinessCheck.tenant_id == tenant_id).with_for_update())
    if not row:
        raise HTTPException(404, 'Tenant not found.')
    if row.status in engine.TERMINAL:
        raise HTTPException(409, 'This tenant already has a final result or was explicitly skipped.')
    if row.lease_owner and row.lease_until and row.lease_until > now():
        raise HTTPException(409, 'This tenant is still running.')
    row.status = 'queued'
    row.lease_owner = None
    row.lease_until = None
    row.state = {**row.state, 'status':'queued', 'detail':''}
    await db.commit()
    return {'queued':True}


class BrowserAction(BaseModel):
    op: str
    x: float = Field(default=0, ge=0, le=1440)
    y: float = Field(default=0, ge=0, le=1000)
    text: str = Field(default='', max_length=2000)
    key: str = Field(default='', max_length=30)


@router.get('/{tenant_id}/browser', dependencies=[Depends(authorized)])
def browser_image(tenant_id: UUID):
    live = LIVE.get(str(tenant_id))
    if not live or not live.get('waiting') or not live.get('image'):
        raise HTTPException(409, 'The browser is not waiting for input. Retry the tenant if it has been deferred.')
    return Response(live['image'], media_type='image/jpeg', headers={'Cache-Control':'no-store'})


@router.post('/{tenant_id}/browser', dependencies=[Depends(authorized)])
def browser_input(tenant_id: UUID, action: BrowserAction):
    live = LIVE.get(str(tenant_id))
    if not live or not live.get('waiting'):
        raise HTTPException(409, 'This browser is no longer waiting for input.')
    if action.op == 'continue':
        live['continue'] = True
    elif action.op in {'click','text'} or (action.op == 'key' and action.key in {'Enter','Tab','Shift+Tab','Backspace','Delete','Escape','Control+A','ArrowDown','ArrowUp'}):
        try:
            live['actions'].put_nowait(action.model_dump())
        except queue.Full:
            raise HTTPException(429, 'Wait for the previous browser input to finish.')
    else:
        raise HTTPException(422, 'Unsupported browser action.')
    return {'accepted':True}
