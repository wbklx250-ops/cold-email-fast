from unittest.mock import Mock, patch

import pytest

from app.services.tenant_automation import BrowserWorker, LoginState


def worker():
    result = BrowserWorker.__new__(BrowserWorker)
    result.worker_id = 0
    result.driver = Mock()
    result.driver.current_url = 'https://login.microsoftonline.com/common/SAS/ProcessAuth'
    result.driver.page_source = '<script>set up your account; authenticator</script>'
    result.driver.find_element.return_value.text = 'Enter code\nEnter the code displayed in the authenticator app'
    return result


def test_existing_challenge_does_not_become_new_enrollment_from_scripts_or_generic_word():
    item = worker()
    assert item._find_mfa_enrollment_keywords() == []
    item.driver.find_element.return_value.text = 'Keep your account secure\nInstall Microsoft Authenticator'
    assert item._find_mfa_enrollment_keywords()


def test_visible_code_input_takes_precedence_over_security_heading():
    item = worker()
    item.driver.find_element.return_value.text = "Verify it's you\nEnter code"
    item._find_element = Mock(return_value=Mock())
    assert item._detect_login_state() == LoginState.NEEDS_MFA_CODE


def test_delayed_code_after_stay_signed_in_uses_saved_key_and_reaches_authenticated_page():
    item = worker()
    item._detect_login_state = Mock(side_effect=[LoginState.NEEDS_STAY_SIGNED_IN,
        LoginState.NEEDS_MFA_CODE, LoginState.LOGGED_IN, LoginState.LOGGED_IN])
    item._handle_stay_signed_in = Mock()
    item._submit_existing_mfa_code = Mock(return_value=True)
    with patch('app.services.tenant_automation.time.sleep'):
        assert item._wait_for_mfa_enrollment_decision(timeout=3, existing_totp_secret='synthetic-key') == []
    item._handle_stay_signed_in.assert_called_once()
    item._submit_existing_mfa_code.assert_called_once_with('synthetic-key')


def test_existing_mfa_without_a_key_is_reported_as_missing_authentication():
    item = worker()
    item._detect_login_state = Mock(return_value=LoginState.NEEDS_MFA_CODE)
    with pytest.raises(RuntimeError, match='no saved TOTP key'):
        item._wait_for_mfa_enrollment_decision(timeout=2)


def test_rejected_code_is_not_repeated_in_same_window_or_counted_as_success():
    item = worker()
    item._detect_login_state = Mock(return_value=LoginState.NEEDS_MFA_CODE)
    item._submit_existing_mfa_code = Mock(return_value=True)
    with patch('app.services.tenant_automation.time.sleep'), patch('app.services.tenant_automation.time.time', return_value=100):
        with pytest.raises(RuntimeError, match='did not accept'):
            item._wait_for_mfa_enrollment_decision(timeout=3, existing_totp_secret='synthetic-key')
    item._submit_existing_mfa_code.assert_called_once()


def test_unfinished_redirect_cannot_be_counted_as_success():
    item = worker()
    item._detect_login_state = Mock(return_value=LoginState.UNKNOWN)
    with patch('app.services.tenant_automation.time.sleep'):
        with pytest.raises(RuntimeError, match='has not reached an authenticated page'):
            item._wait_for_mfa_enrollment_decision(timeout=1)
