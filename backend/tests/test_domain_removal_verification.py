from unittest.mock import AsyncMock, Mock

import pytest
import requests

from app.services.selenium import domain_removal as removal


def clock(monkeypatch):
    current = [0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        current[0] += seconds

    monkeypatch.setattr(removal.time, 'monotonic', lambda: current[0])
    monkeypatch.setattr(removal.time, 'sleep', sleep)
    return waits


def test_graph_absence_succeeds_immediately_without_cached_discovery_or_auth(monkeypatch):
    waits = clock(monkeypatch)
    verify = Mock(return_value=True)
    auth = Mock(side_effect=AssertionError('Existing cleanup token should be reused'))
    discovery = Mock(side_effect=AssertionError('Stale public discovery must not affect removal'))
    monkeypatch.setattr(removal, '_run_async_graph_verify', verify)
    monkeypatch.setattr(removal, '_get_access_token_via_msal', auth)
    monkeypatch.setattr(requests, 'get', discovery)
    result = removal._verify_domain_actually_removed('old.example', access_token='token')
    assert result['verified_removed'] and waits == []
    assert result['checks']['graph_api'] == {'domain_exists': False, 'removed': True}
    verify.assert_called_once_with('token', 'old.example')


def test_pending_deletion_polls_then_stops_on_first_confirmation(monkeypatch):
    waits = clock(monkeypatch)
    monkeypatch.setattr(removal, '_run_async_graph_verify', Mock(side_effect=[False, False, True]))
    assert removal._verify_domain_actually_removed('old.example', access_token='token')['verified_removed']
    assert waits == [2, 4]


@pytest.mark.parametrize('state', [False, None])
def test_present_or_unverifiable_domain_never_becomes_success(monkeypatch, state):
    clock(monkeypatch)
    monkeypatch.setattr(removal, '_run_async_graph_verify', Mock(return_value=state))
    result = removal._verify_domain_actually_removed('old.example', access_token='token', max_wait=6)
    assert not result['verified_removed'] and result['error']


def test_authentication_failure_is_not_treated_as_deletion(monkeypatch):
    clock(monkeypatch)
    auth = Mock(return_value=(False, None, 'denied'))
    monkeypatch.setattr(removal, '_get_access_token_via_msal', auth)
    result = removal._verify_domain_actually_removed('old.example', 'admin', 'password')
    assert not result['verified_removed']
    auth.assert_called_once()


def test_authentication_happens_once_across_pending_checks(monkeypatch):
    clock(monkeypatch)
    auth = Mock(return_value=(True, 'token', None))
    monkeypatch.setattr(removal, '_get_access_token_via_msal', auth)
    monkeypatch.setattr(removal, '_run_async_graph_verify', Mock(side_effect=[False, True]))
    assert removal._verify_domain_actually_removed('old.example', 'admin', 'password')['verified_removed']
    auth.assert_called_once()


def test_verified_graph_result_skips_duplicate_verification_and_fallbacks(monkeypatch):
    monkeypatch.setattr(removal, '_remove_domain_tier1_graph_api', Mock(return_value={'success': True, 'verified': True}))
    verify = Mock(side_effect=AssertionError('Duplicate verification'))
    fallback = Mock(side_effect=AssertionError('Unnecessary fallback'))
    monkeypatch.setattr(removal, '_verify_domain_actually_removed', verify)
    monkeypatch.setattr(removal, '_remove_domain_tier2_powershell', fallback)
    result = removal._remove_domain_after_license_cleanup('old.example', 'admin', 'password', access_token='token')
    assert result['success'] and result['verified']


def test_unverified_accepted_request_reaches_existing_fallback(monkeypatch):
    monkeypatch.setattr(removal, '_remove_domain_tier1_graph_api', Mock(return_value={'success': True}))
    monkeypatch.setattr(removal, '_remove_domain_tier2_powershell', Mock(return_value={'success': True}))
    monkeypatch.setattr(removal, '_verify_domain_actually_removed', Mock(side_effect=[
        {'verified_removed': False, 'error': 'still exists'},
        {'verified_removed': True, 'error': None},
    ]))
    result = removal._remove_domain_after_license_cleanup('old.example', 'admin', 'password', access_token='token')
    assert result['success'] and result['method'] == 'powershell'


def test_tier_one_already_absent_has_no_fixed_waits_or_extra_get(monkeypatch):
    waits = clock(monkeypatch)
    monkeypatch.setattr(removal, '_graph_api_force_delete', AsyncMock(return_value={'success': True, 'verified': True}))
    monkeypatch.setattr(removal, '_run_async_graph_verify', Mock(side_effect=AssertionError('Already verified')))
    result = removal._remove_domain_tier1_graph_api('old.example', 'admin', 'password', access_token='token')
    assert result['verified'] and waits == []
