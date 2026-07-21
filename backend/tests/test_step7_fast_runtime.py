from app.services.step7_fast import (
    _build_licensed_user_script,
    _domain_error_message,
    _effective_step7_parallel,
    _is_transient_license_error,
    _mailbox_objective_complete,
    _powershell_exit_error,
    _powershell_result_error,
)


def test_domain_error_message_fits_database_column():
    assert _domain_error_message(None) is None
    assert _domain_error_message("short") == "short"
    assert len(_domain_error_message("x" * 1200)) == 1000


def test_licensed_user_script_accepts_business_premium_trials():
    script = _build_licensed_user_script(
        escaped_email="admin@example.onmicrosoft.com",
        escaped_password="password",
        domain="example.com",
        mailbox_password="mailbox-password",
    )

    assert '"SPB"' in script
    assert '"O365_BUSINESS_PREMIUM"' in script
    assert 'users/$userId/assignLicense' in script
    assert "Set-MgUserLicense -UserId" not in script
    assert 'SkuPartNumber -notlike "*TRIAL*"' not in script
    assert "Business Premium (SPB, including trial)" in script


def test_powershell_exit_error_identifies_sigkill():
    message = _powershell_exit_error(-9)

    assert "SIGKILL" in message
    assert "OOM" in message


def test_powershell_exit_error_includes_stderr_tail():
    message = _powershell_exit_error(1, stderr="exchange connection failed")

    assert message == "PowerShell exited with code 1: exchange connection failed"


def test_powershell_exit_error_rejects_missing_json():
    message = _powershell_exit_error(0, stdout="COMPLETE")

    assert message == "PowerShell completed without a JSON result: COMPLETE"


def test_powershell_result_error_preserves_script_errors():
    message = _powershell_result_error(
        {
            "error": "PowerShell exited with code 1",
            "errors": ["EXO connect failed", "Mailbox not visible"],
        }
    )

    assert message == (
        "PowerShell exited with code 1 | "
        "EXO connect failed; Mailbox not visible"
    )


def test_mailbox_objective_requires_every_expected_mailbox():
    assert _mailbox_objective_complete(100, 100) is True
    assert _mailbox_objective_complete(100, 99) is False
    assert _mailbox_objective_complete(0, 0) is False


def test_step7_parallel_is_serial_below_two_gib():
    assert _effective_step7_parallel(5, 999_997_440) == 1
    assert _effective_step7_parallel(2, 2 * 1024**3) == 2
    assert _effective_step7_parallel(10, None) == 5


def test_transient_license_errors_are_retryable():
    assert _is_transient_license_error(
        "Request_ResourceNotFound: queried reference-property objects are not present"
    )
    assert _is_transient_license_error("PowerShell script timed out after 300 seconds")
    assert not _is_transient_license_error("No available Business Basic license")
