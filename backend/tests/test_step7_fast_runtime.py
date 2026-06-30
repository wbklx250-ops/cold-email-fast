from app.services.step7_fast import (
    _effective_step7_parallel,
    _is_transient_license_error,
    _mailbox_objective_complete,
    _powershell_exit_error,
    _powershell_result_error,
)


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
