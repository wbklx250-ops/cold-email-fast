from unittest.mock import AsyncMock
import asyncio

import pytest

from app.services import domain_recipient_cleanup as cleanup
from app.services.domain_license_cleanup import _select_users


def user(uid='shared', upn='person@old.example', **kwargs):
    return dict(id=uid, userPrincipalName=upn, assignedLicenses=[], proxyAddresses=[], **kwargs)


def recipient(uid='shared', primary='person@old.example', kind='SharedMailbox', addresses=None):
    return dict(id=uid, primary=primary, type=kind, addresses=addresses or [])


def plan(users, recipients, known=(), licensed='licensed'):
    return cleanup.plan_cleanup(users, recipients, 'old.example', licensed, known, {'admin'}, 'admin@tenant.onmicrosoft.com')


def test_domain_scope_and_recorded_ids_find_renamed_mailboxes_without_name_matching():
    users = [user(), user('renamed', 'person2@tenant.onmicrosoft.com'),
             user('other', 'person@new.example'), user('licensed', 'me1@tenant.onmicrosoft.com')]
    inventory = [recipient(), recipient('renamed', 'person2@tenant.onmicrosoft.com'),
                 recipient('other', 'person@new.example')]
    shared, licensed = plan(users, inventory, ['renamed'])
    assert shared == ['renamed', 'shared']
    assert [u['id'] for u in licensed] == ['licensed']


@pytest.mark.parametrize('uid,upn,error', [
    ('admin', 'person@old.example', 'administrator'),
    ('shared', 'admin@tenant.onmicrosoft.com', 'administrator'),
    ('shared', 'person@new.example', 'another custom domain'),
])
def test_preflight_protects_admin_and_reassigned_users(uid, upn, error):
    with pytest.raises(ValueError, match=error):
        plan([user(uid, upn)], [recipient(uid)])


def test_mixed_domain_mailbox_is_protected_even_if_old_alias_matches():
    with pytest.raises(ValueError, match='another custom domain'):
        plan([user()], [recipient(primary='person@new.example', addresses=['smtp:person@old.example'])])


@pytest.mark.parametrize('kind', ['UserMailbox', 'MailUniversalDistributionGroup', 'MailUser'])
def test_unexpected_recipient_type_blocks_before_mutation(kind):
    with pytest.raises(ValueError, match='non-shared'):
        plan([user()], [recipient(kind=kind)])


def test_synchronized_and_licensed_shared_users_block():
    synced = user(onPremisesSyncEnabled=True)
    with pytest.raises(ValueError, match='on-premises'):
        plan([synced], [recipient()])
    licensed_shared = user()
    licensed_shared['assignedLicenses'] = [{'skuId': 'sku'}]
    with pytest.raises(ValueError, match='shared mailbox has a license'):
        plan([licensed_shared], [recipient()])


def test_known_user_cannot_silently_survive_missing_exchange_inventory():
    with pytest.raises(ValueError, match='without a confirmed shared mailbox'):
        plan([user('renamed', 'person@tenant.onmicrosoft.com')], [], ['renamed'])
    assert plan([], [], ['deleted'])[0] == []


def test_licensed_user_can_be_discovered_from_old_smtp_after_force_delete_rename():
    old = user('licensed', 'me1@tenant.onmicrosoft.com', mail='me1@old.example')
    assert _select_users([old], 'old.example') == [old]


def test_legacy_recovery_requires_recorded_address_and_exact_tenant_initial_domain():
    inventory = [recipient('renamed', 'person@tenant.onmicrosoft.com'),
                 recipient('unrelated', 'different@tenant.onmicrosoft.com'),
                 recipient('new', 'person@new.example'),
                 recipient('wrongtenant', 'person@another.onmicrosoft.com')]
    records = [{'id': None, 'email': 'person@old.example'}]
    assert cleanup.recover_mailbox_ids(inventory, records, 'old.example', 'tenant.onmicrosoft.com') == {'renamed'}
    assert cleanup.recover_mailbox_ids(inventory, [{'email':'person@new.example'}], 'old.example', 'tenant.onmicrosoft.com') == set()


def test_old_smtp_alias_cannot_select_user_on_another_custom_domain():
    with pytest.raises(ValueError, match='another custom domain'):
        _select_users([user('licensed', 'me1@new.example', mail='me1@old.example')], 'old.example')


def test_group_is_preserved_not_selected_for_deletion():
    shared, licensed = plan([user('group')], [recipient('group', kind='GroupMailbox')])
    assert shared == [] and licensed == []


class Response:
    def __init__(self, status, payload=None):
        self.status, self.payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.payload


class Session(Response):
    def __init__(self, gets, delete_status=204):
        self.gets = iter(gets)
        self.delete_status = delete_status
        self.deleted = []

    def get(self, url):
        return next(self.gets)

    def delete(self, url):
        self.deleted.append(url)
        return Response(self.delete_status)


@pytest.mark.parametrize('status', [204, 404])
async def test_delete_verifies_absence_and_accepts_already_deleted(monkeypatch, status):
    session = Session([Response(200, user()), Response(404)], status)
    await cleanup.delete_user(session, 'shared', 'old.example', {'admin'}, 'admin@tenant.onmicrosoft.com')
    assert session.deleted == [cleanup.GRAPH_ROOT + '/users/shared']


@pytest.mark.parametrize('read_status,delete_status', [(403, 204), (200, 403)])
async def test_delete_errors_are_not_treated_as_absence(read_status, delete_status):
    session = Session([Response(read_status, user())], delete_status)
    with pytest.raises(RuntimeError, match='403'):
        await cleanup.delete_user(session, 'shared', 'old.example', {'admin'}, 'admin@tenant.onmicrosoft.com')


async def test_license_reassignment_between_preflight_and_delete_blocks():
    value = user()
    value['assignedLicenses'] = [{'skuId': 'new-sku'}]
    session = Session([Response(200, value)])
    with pytest.raises(RuntimeError, match='license release'):
        await cleanup.delete_user(session, 'shared', 'old.example', {'admin'}, 'admin@tenant.onmicrosoft.com')
    assert session.deleted == []


@pytest.mark.parametrize('fail_at', [None, 'shared', 'license', 'licensed', 'exchange', 'final_exchange'])
async def test_cleanup_order_and_partial_failure_stop_later_steps(monkeypatch, fail_at):
    users = [user(), user('licensed', 'me1@old.example')]
    session = Session([Response(200, {'id': 'admin'}), Response(200, {'value': users})])
    monkeypatch.setattr(cleanup.aiohttp, 'ClientSession', lambda **kw: session)
    monkeypatch.setattr(cleanup, 'exchange_recipients', AsyncMock(return_value=[recipient()]))
    events = []

    async def delete(session, uid, *args):
        events.append(uid)
        if fail_at == uid:
            raise RuntimeError('delete blocked')

    async def release(*args, **kw):
        events.append('license')
        return {'success': fail_at != 'license', 'error': 'release blocked'}

    async def verify(*args, final=False):
        events.append('exchange' if final else 'shared_verified')
        if fail_at == 'exchange' or (fail_at == 'final_exchange' and final):
            raise RuntimeError('Exchange not ready')

    monkeypatch.setattr(cleanup, 'delete_user', delete)
    monkeypatch.setattr(cleanup, 'release_domain_user_licenses', release)
    monkeypatch.setattr(cleanup, 'verify_exchange_absent', verify)
    result = await cleanup.cleanup_domain_recipients('token', 'old.example', 'admin@tenant.onmicrosoft.com', 'password')
    assert result['success'] == (fail_at is None)
    full_order = ['shared', 'shared_verified', 'license', 'licensed', 'exchange']
    if fail_at:
        # Exchange failure here is simulated during the initial verification too.
        cut = 'shared_verified' if fail_at == 'exchange' else 'exchange' if fail_at == 'final_exchange' else fail_at
        assert events == full_order[:full_order.index(cut) + 1]
    else:
        assert events == full_order


async def test_exchange_propagation_must_finish(monkeypatch):
    inventory = AsyncMock(return_value=[recipient()])
    monkeypatch.setattr(cleanup, 'exchange_recipients', inventory)
    monkeypatch.setattr(cleanup.asyncio, 'sleep', AsyncMock())
    with pytest.raises(RuntimeError, match='Exchange still references'):
        await cleanup.verify_exchange_absent({'shared'}, 'old.example', 'admin', 'password')
    assert inventory.await_count == 6


async def test_incomplete_discovery_does_not_mutate(monkeypatch):
    session = Session([Response(200, {'id': 'admin'}), Response(200, {
        'value': [user()], '@odata.nextLink': cleanup.GRAPH_ROOT + '/users?$skiptoken=next',
    }), Response(403)])
    monkeypatch.setattr(cleanup.aiohttp, 'ClientSession', lambda **kw: session)
    delete = AsyncMock()
    monkeypatch.setattr(cleanup, 'delete_user', delete)
    result = await cleanup.cleanup_domain_recipients('token', 'old.example', 'admin', 'password')
    assert not result['success']
    delete.assert_not_awaited()


async def test_legacy_mailbox_recovery_and_group_preservation_in_full_flow(monkeypatch):
    users = [user('renamed', 'person@tenant.onmicrosoft.com'), user('licensed', 'me1@old.example')]
    session = Session([Response(200, {'id': 'admin'}), Response(200, {'value': users}),
                       Response(200, {'value': [{'id': 'tenant.onmicrosoft.com', 'isInitial': True}]})])
    monkeypatch.setattr(cleanup.aiohttp, 'ClientSession', lambda **kw: session)
    inventory = [recipient('renamed', 'person@tenant.onmicrosoft.com'),
                 recipient('new', 'person@new.example'), recipient('group', 'group@old.example', 'GroupMailbox')]
    exchange = AsyncMock(return_value=inventory)
    monkeypatch.setattr(cleanup, 'exchange_recipients', exchange)
    delete = AsyncMock()
    monkeypatch.setattr(cleanup, 'delete_user', delete)
    monkeypatch.setattr(cleanup, 'release_domain_user_licenses', AsyncMock(return_value={'success': True}))
    monkeypatch.setattr(cleanup, 'verify_exchange_absent', AsyncMock())
    result = await cleanup.cleanup_domain_recipients('token', 'old.example', 'admin', 'password',
        mailbox_records=[{'id': None, 'email': 'person@old.example'}])
    assert result['success']
    assert [call.args[1] for call in delete.await_args_list] == ['renamed', 'licensed']
    assert exchange.await_args.kwargs == {'groups': ['group'], 'domain': 'old.example',
                                          'initial_domain': 'tenant.onmicrosoft.com'}
    assert result['group_addresses']['updated'] == 1


async def test_delete_acceptance_without_absence_is_failure(monkeypatch):
    session = Session([Response(200, user())] + [Response(200, {'id': 'shared'}) for _ in range(6)])
    monkeypatch.setattr(cleanup.asyncio, 'sleep', AsyncMock())
    with pytest.raises(RuntimeError, match='still active'):
        await cleanup.delete_user(session, 'shared', 'old.example', {'admin'}, 'admin')


async def test_exchange_failure_or_malformed_output_is_not_empty_inventory(monkeypatch):
    from types import SimpleNamespace
    run = AsyncMock(return_value=SimpleNamespace(success=False, json_data=None, error='sensitive script'))
    monkeypatch.setattr(cleanup.PowerShellRunner, 'run', run)
    with pytest.raises(RuntimeError, match='mailbox discovery') as error:
        await cleanup.exchange_recipients('admin', 'secret')
    assert 'sensitive' not in str(error.value) and 'secret' not in str(error.value)
    run.return_value = SimpleNamespace(success=True, json_data={'recipients': None})
    with pytest.raises(RuntimeError, match='incomplete'):
        await cleanup.exchange_recipients('admin', 'secret')


@pytest.mark.parametrize('fail', [False, True])
async def test_shared_deletion_is_bounded_and_drains_before_failure(monkeypatch, fail):
    active = 0
    peak = 0
    started, finished = [], []

    async def delete(session, uid, *args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.append(uid)
        await asyncio.sleep(0)
        active -= 1
        finished.append(uid)
        if fail and uid == '0':
            raise RuntimeError('blocked')

    monkeypatch.setattr(cleanup, 'delete_user', delete)
    progress = {'removed': 0}
    call = cleanup.delete_shared_users(None, [str(i) for i in range(9)], 'old.example', {'admin'}, 'admin', progress)
    if fail:
        with pytest.raises(RuntimeError, match='blocked'):
            await call
        assert started == ['0', '1', '2', '3'] and progress['removed'] == 3
    else:
        await call
        assert progress['removed'] == 9
    assert peak == 4 and active == 0 and len(finished) == len(started)


async def test_graph_throttle_respects_retry_after_without_restarting_cleanup(monkeypatch):
    throttle = Response(429)
    throttle.headers = {'Retry-After': '7'}
    session = Session([throttle, Response(200, {'id': 'user'})])
    sleep = AsyncMock()
    monkeypatch.setattr(cleanup.asyncio, 'sleep', sleep)
    assert await cleanup.graph_get(session, 'users/user') == {'id': 'user'}
    sleep.assert_awaited_once_with(7)


async def test_delete_transient_failure_retries_same_id(monkeypatch):
    session = Session([])
    replies = iter([Response(503), Response(204)])
    calls = []

    def delete(url):
        calls.append(url)
        return next(replies)

    session.delete = delete
    monkeypatch.setattr(cleanup.asyncio, 'sleep', AsyncMock())
    await cleanup.graph_request(session, 'delete', 'users/user', absent_ok=True)
    assert calls == [cleanup.GRAPH_ROOT + '/users/user'] * 2
