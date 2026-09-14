from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import domain_license_cleanup as cleanup
from app.services.domain_removal_service import DomainRemovalService
from app.services.selenium import domain_removal as removal


def user(uid='old', upn='me1@old.example', licenses=('sku1',), states=()):
    return {'id': uid, 'userPrincipalName': upn,
            'assignedLicenses': [{'skuId': sku} for sku in licenses],
            'licenseAssignmentStates': list(states)}


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.payload

    async def text(self):
        return str(self.payload)


class Session(Response):
    def __init__(self, gets, posts=()):
        self.get_responses = iter(gets)
        self.post_responses = iter(posts)
        self.get_calls, self.post_calls = [], []

    def get(self, url):
        self.get_calls.append(url)
        return next(self.get_responses)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return next(self.post_responses)


def install_session(monkeypatch, gets, posts=()):
    session = Session(gets, posts)
    monkeypatch.setattr(cleanup.aiohttp, 'ClientSession', lambda **kwargs: session)
    monkeypatch.setattr(cleanup.asyncio, 'sleep', AsyncMock())
    return session


async def test_paginated_discovery_releases_only_domain_user_and_verifies(monkeypatch):
    session = install_session(monkeypatch, [
        Response({'value': [user('other', 'me1@other.example'), user('admin', 'admin@tenant.onmicrosoft.com')], '@odata.nextLink': 'page2'}),
        Response({'value': [user(licenses=('sku1', 'sku2'))]}),
        Response({'assignedLicenses': [{'skuId': 'sku1'}]}),
        Response({'assignedLicenses': []}),
    ], [Response({})])
    result = await cleanup.release_domain_user_licenses('token', 'OLD.EXAMPLE')
    assert result['success'] and result['licenses_removed'] == 2
    assert session.get_calls[1] == 'page2'
    assert session.post_calls == [(cleanup.GRAPH_ROOT + '/users/old/assignLicense',
                                  {'json': {'addLicenses': [], 'removeLicenses': ['sku1', 'sku2']}})]


@pytest.mark.parametrize('users', [[], [user(licenses=())]])
async def test_absent_or_already_unlicensed_user_is_idempotent(monkeypatch, users):
    session = install_session(monkeypatch, [Response({'value': users})])
    assert (await cleanup.release_domain_user_licenses('token', 'old.example'))['success']
    assert not session.post_calls


def test_stable_id_can_find_renamed_user_without_touching_other_domains():
    old = user('old', 'random@tenant.onmicrosoft.com')
    other = user('other', 'me1@other.example')
    assert cleanup._select_users([old, other], 'old.example', 'old') == [old]
    with pytest.raises(ValueError, match='another custom domain'):
        cleanup._select_users([other], 'old.example', 'other')
    with pytest.raises(ValueError, match='administrator'):
        cleanup._select_users([old], 'old.example', 'old', old['userPrincipalName'])


async def test_discovery_failure_prevents_partial_mutation(monkeypatch):
    session = install_session(monkeypatch, [
        Response({'value': [user()], '@odata.nextLink': 'page2'}), Response({}, 403)
    ])
    assert not (await cleanup.release_domain_user_licenses('token', 'old.example'))['success']
    assert not session.post_calls


async def test_group_license_blocks_removal(monkeypatch):
    session = install_session(monkeypatch, [Response({'value': [user(states=({'assignedByGroup': 'group'},))]})])
    result = await cleanup.release_domain_user_licenses('token', 'old.example')
    assert not result['success'] and 'group-assigned' in result['error']
    assert not session.post_calls


async def test_assignment_api_error_is_not_success(monkeypatch):
    install_session(monkeypatch, [Response({'value': [user()]})], [Response({'error': 'denied'}, 403)])
    result = await cleanup.release_domain_user_licenses('token', 'old.example')
    assert not result['success'] and '403' in result['error']


async def test_accepted_release_must_verify(monkeypatch):
    install_session(monkeypatch, [Response({'value': [user()]})] +
                    [Response({'assignedLicenses': [{'skuId': 'sku1'}]}) for _ in range(6)], [Response({})])
    result = await cleanup.release_domain_user_licenses('token', 'old.example')
    assert not result['success'] and 'still assigned' in result['error']


@pytest.mark.parametrize('success', [True, False])
def test_all_removal_tiers_are_gated_by_cleanup(monkeypatch, success):
    monkeypatch.setattr(removal, '_get_access_token_via_msal', Mock(return_value=(True, 'token', None)))
    release = AsyncMock(return_value={'success': success, 'error': 'failed'})
    monkeypatch.setattr(cleanup, 'release_domain_user_licenses', release)
    tiers = Mock(return_value={'success': True, 'verified': True})
    monkeypatch.setattr(removal, '_remove_domain_after_license_cleanup', tiers)
    result = removal.remove_domain_robust('old.example', 'admin@tenant.onmicrosoft.com', 'password', licensed_user_id='old')
    assert result['success'] == success
    assert tiers.call_count == int(success)
    release.assert_awaited_once_with('token', 'old.example', 'old', 'admin@tenant.onmicrosoft.com')


def test_mfa_authentication_fallback_still_releases_licenses(monkeypatch):
    monkeypatch.setattr(removal, '_get_access_token_via_msal', Mock(return_value=(False, None, 'MFA')))
    monkeypatch.setattr(removal, '_get_access_token_via_selenium', Mock(return_value=(True, 'browser-token', None)))
    release = AsyncMock(return_value={'success': True})
    monkeypatch.setattr(cleanup, 'release_domain_user_licenses', release)
    monkeypatch.setattr(removal, '_remove_domain_after_license_cleanup', Mock(return_value={'success': True}))
    assert removal.remove_domain_robust('old.example', 'admin@tenant.onmicrosoft.com', 'password')['success']
    assert release.await_args.args[0] == 'browser-token'


async def test_failed_cleanup_preserves_dns(monkeypatch):
    monkeypatch.setattr(removal, 'remove_domain_robust', Mock(return_value={
        'success': False, 'license_cleanup': {'success': False}, 'error': 'release failed'}))
    service = DomainRemovalService()
    cf = Mock()
    monkeypatch.setattr(service, '_get_cf_service', cf)
    result = await service._execute_removal('old.example', 'admin@tenant.onmicrosoft.com', 'password', None, 'zone', False, True, max_retries=0)
    assert not result['steps']['m365_removal']['success']
    assert result['steps']['cloudflare_cleanup']['skipped']
    cf.assert_not_called()


def test_license_database_flags_reset_only_after_real_removal():
    domain = SimpleNamespace(licensed_user_id='old', licensed_user_created=True)
    DomainRemovalService._reset_license_state(domain, skip_m365=True)
    assert domain.licensed_user_id == 'old'
    DomainRemovalService._reset_license_state(domain)
    assert domain.licensed_user_id is None and not domain.licensed_user_created
    assert not domain.domain_added_to_m365 and not domain.domain_verified_in_m365


@pytest.mark.parametrize('mode', ['db', 'csv'])
@pytest.mark.parametrize('success', [True, False])
async def test_both_removal_modes_preserve_identity_on_failure_and_reset_on_success(monkeypatch, mode, success):
    from uuid import uuid4
    from app.models.domain import Domain, DomainStatus
    from app.models.tenant import Tenant

    tenant = Tenant(id=uuid4(), name='tenant', admin_email='admin@tenant.onmicrosoft.com', admin_password='password')
    domain = Domain(id=uuid4(), name='old.example', tenant_id=tenant.id, cloudflare_zone_id='zone',
                    licensed_user_id='old', licensed_user_created=True, licensed_user_upn='me1@old.example')
    domain.tenant = tenant
    tenant.domain_id = domain.id
    db = AsyncMock()
    lookup = Mock()
    lookup.scalar_one_or_none.return_value = domain
    lookup.scalars.return_value.all.return_value = []
    db.execute.return_value = lookup
    service = DomainRemovalService()
    execute = AsyncMock(return_value={'steps': {'m365_removal': {'success': success, 'error': 'license release failed'}}})
    monkeypatch.setattr(service, '_execute_removal', execute)
    if mode == 'db':
        result = await service.remove_domain_from_db(db, domain.name)
    else:
        result = await service.remove_domain_from_csv({'domain': domain.name, 'admin_email': tenant.admin_email, 'admin_password': 'password'}, db=db)
    assert execute.await_args.kwargs['licensed_user_id'] == 'old'
    assert result['success'] == success
    if success:
        assert domain.licensed_user_id is None and domain.tenant_id is None
        assert not domain.licensed_user_created
    else:
        assert domain.licensed_user_id == 'old' and domain.tenant_id == tenant.id
        assert domain.status == DomainStatus.PROBLEM
