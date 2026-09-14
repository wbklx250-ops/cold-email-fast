"""Read-only Microsoft Graph and database audit of Futurehouse (5) licenses."""
import os, json, asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aiohttp
os.environ['DEBUG'] = 'false'
from sqlalchemy import select
from app.db.session import async_session_factory
from app.models.batch import SetupBatch
from app.models.domain import Domain
from app.models.tenant import Tenant
from uuid import UUID
from app.services.selenium.domain_removal import _get_access_token_via_msal

async def graph_audit(d, t):
    ok, token, error = await asyncio.to_thread(_get_access_token_via_msal, t.admin_email, t.admin_password)
    if not ok:
        return {'domain': d.name, 'error': error}
    async with aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + token}) as session:
        result = {'domain': d.name}
        for key, path in [('skus','subscribedSkus'),('domains','domains'),('users','users?$select=id,userPrincipalName,mail,mailNickname,assignedLicenses,licenseAssignmentStates,accountEnabled&$top=999')]:
            values = []
            url = 'https://graph.microsoft.com/v1.0/' + path
            while url:
                async with session.get(url) as response:
                    data = await response.json()
                    if response.status != 200:
                        raise RuntimeError(str(data))
                    values.extend(data.get('value', []))
                    url = data.get('@odata.nextLink')
            if key == 'users':
                values = [u for u in values if u.get('assignedLicenses') or (u.get('userPrincipalName') or '').startswith('me1')]
            result[key] = values
        return result

async def main():
    async with async_session_factory() as db:
        bid = UUID('82a8ba8b-f0d0-47b6-a541-fa86fd2ba352')
        b = await db.get(SetupBatch, bid)
        print(json.dumps({'deployment': {k: os.getenv(k) for k in ['RAILWAY_GIT_COMMIT_SHA', 'RAILWAY_GIT_BRANCH', 'RAILWAY_DEPLOYMENT_ID']}, 'batch': {k: getattr(b,k,None) for k in ['name','current_step','completed_steps','pipeline_status','pipeline_step','mailboxes_per_tenant','auto_run_state']}}, default=str))
        rows = (await db.execute(select(Domain, Tenant).join(Tenant, Domain.tenant_id == Tenant.id).where(Domain.batch_id == bid))).all()
        for d,t in rows:
            print(json.dumps({'domain': {k: getattr(d,k,None) for k in ['id','name','licensed_user_upn','licensed_user_id','licensed_user_created','error_message','step7_complete','mailboxes_created','domain_index_in_tenant']}, 'tenant': {k: getattr(t,k,None) for k in ['id','name','admin_email','onmicrosoft_domain','batch_id','error_message']}}, default=str))
        for d,t in rows:
            print(json.dumps(await graph_audit(d,t), default=str), flush=True)
if __name__ == '__main__':
    asyncio.run(main())
