"""Audited, explicit old/new user license transfer for Futurehouse (5).

Dry run by default. --apply releases the old assignments and transfers enabled
seats only. Suspended subscriptions are reported separately and never purchased.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DEBUG'] = 'false'

import aiohttp
from sqlalchemy import select
from app.db.session import async_session_factory
from app.models.domain import Domain
from app.models.tenant import Tenant
from app.services.selenium.domain_removal import _get_access_token_via_msal
from app.services.step7_fast import ensure_licensed_user_for_domain

BATCH_ID = UUID('82a8ba8b-f0d0-47b6-a541-fa86fd2ba352')
SKU = '21502a13-c8dc-4744-be9c-177fd9d2eafc'
# Object IDs were read from Graph in the specified tenants before this repair.
TARGETS = {
    'dunhill.financial': ('orionstackflow1701', '33733c7f-9db4-46b1-b0ab-07aa4773af6b', 'f27a8b23-3222-4609-b346-70ee174a9eb8'),
    'dunhill.finance': ('orbitcorehub1696', 'fa0ac3aa-aec1-4945-b9e1-8ee577ca0e38', '39e799e6-7777-48dc-9fa5-a79d7bfdb1b1'),
    'dunhill.ventures': ('orionstackgrid1702', '92101762-5efa-4682-9707-626d890485ac', '81c1c01c-0746-48c2-b9fc-ae0204c35274'),
    'familyofficeforum.co': ('orbitcoreworks1699', '40a1f6da-8b0c-40f0-a991-84f648c0fe0a', '1c1c99ff-c1a6-4c2d-afbd-788d6a2e0b5f'),
    'dunhill.investments': ('orionstackhub1703', 'a8ccde56-a502-408d-b5b8-f0cdc3b1785a', '2755c8cb-2f13-4b4e-8e60-d2eae22e5b59'),
}

async def repair(domain, tenant, apply):
    expected_tenant, old_id, new_id = TARGETS[domain.name]
    assert tenant.onmicrosoft_domain == expected_tenant + '.onmicrosoft.com'
    assert tenant.batch_id == BATCH_ID and domain.batch_id == BATCH_ID
    ok, token, error = await asyncio.to_thread(_get_access_token_via_msal, tenant.admin_email, tenant.admin_password)
    if not ok:
        raise RuntimeError(error)
    async with aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + token}) as session:
        async def graph(method, path, body=None):
            async with session.request(method, 'https://graph.microsoft.com/v1.0/' + path, json=body) as response:
                data = await response.json()
                if response.status not in (200, 201):
                    raise RuntimeError(f'Graph {response.status}: {data}')
                return data
        async def user(uid):
            return await graph('GET', f'users/{uid}?$select=id,userPrincipalName,assignedLicenses,licenseAssignmentStates')
        old, new = await user(old_id), await user(new_id)
        assert old['userPrincipalName'].lower().endswith('@' + tenant.onmicrosoft_domain)
        assert old['userPrincipalName'].lower() != tenant.admin_email.lower()
        assert new['userPrincipalName'].lower() == 'me1@' + domain.name
        assert not any(s.get('assignedByGroup') for s in old.get('licenseAssignmentStates', []))
        licenses = old.get('assignedLicenses', [])
        assert all(lic['skuId'] == SKU for lic in licenses), 'Unexpected license on source user'
        skus = (await graph('GET', 'subscribedSkus'))['value']
        sku = next(s for s in skus if s['skuId'] == SKU)
        active = sku['capabilityStatus'] == 'Enabled' and sku['prepaidUnits']['enabled'] > 0
        result = {'domain': domain.name, 'tenant': tenant.onmicrosoft_domain, 'old_user_id': old_id,
                  'new_user_id': new_id, 'new_upn': new['userPrincipalName'], 'subscription_status': sku['capabilityStatus']}
        print(json.dumps({**result, 'phase': 'before', 'old_licenses': licenses, 'new_licenses': new['assignedLicenses']}), flush=True)
        if not apply:
            return {**result, 'status': 'would_transfer' if active else 'would_release_suspended'}
        if licenses:
            await graph('POST', f'users/{old_id}/assignLicense', {'addLicenses': [], 'removeLicenses': [SKU]})
        for _ in range(10):
            if not (await user(old_id))['assignedLicenses']:
                break
            await asyncio.sleep(3)
        else:
            raise RuntimeError('Old license release did not verify')
        if not active:
            async with async_session_factory() as db:
                d = await db.get(Domain, domain.id)
                d.error_message = 'Step 7 blocked: Microsoft 365 Business Basic subscription is Suspended (0 enabled, 1 suspended seat). Old user license released; reseller must reactivate subscription.'
                await db.commit()
            return {**result, 'status': 'blocked_subscription_suspended', 'old_license_released': True}
        assignment = await ensure_licensed_user_for_domain(domain.name, domain.id, tenant.admin_email, tenant.admin_password)
        if not assignment.get('success'):
            # Return the enabled seat to its former holder if transfer fails.
            current = await user(new_id)
            if not any(lic['skuId'] == SKU for lic in current['assignedLicenses']):
                await graph('POST', f'users/{old_id}/assignLicense', {'addLicenses': licenses, 'removeLicenses': []})
            raise RuntimeError(assignment.get('error', 'Assignment failed'))
        for attempt in range(20):
            current = await user(new_id)
            if any(lic['skuId'] == SKU for lic in current['assignedLicenses']):
                break
            await asyncio.sleep(3)
        else:
            raise RuntimeError('New license assignment did not verify within 60 seconds')
        assert not (await user(old_id))['assignedLicenses'], 'Old license assignment reappeared'
        return {**result, 'status': 'transferred_verified', 'old_license_released': True,
                'new_licenses': current['assignedLicenses'], 'assignment_states': current['licenseAssignmentStates']}

async def main(apply):
    async with async_session_factory() as db:
        rows = (await db.execute(select(Domain, Tenant).join(Tenant, Domain.tenant_id == Tenant.id).where(Domain.batch_id == BATCH_ID))).all()
    assert {d.name for d,t in rows} == set(TARGETS), 'Batch membership changed'
    results = []
    for d,t in sorted(rows, key=lambda row: row[0].name):
        try:
            result = await repair(d,t,apply)
        except Exception as exc:
            result = {'domain': d.name, 'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
        results.append(result)
        print(json.dumps(result), flush=True)
    print(json.dumps({'apply': apply, 'results': results}), flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    asyncio.run(main(parser.parse_args().apply))
