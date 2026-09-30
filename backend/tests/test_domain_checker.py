from itertools import count
from unittest.mock import Mock

import pytest
from selenium.webdriver.common.by import By

from app.services.selenium import domain_checker as checker
from app.api.routes.domain_checker import checker_jobs, get_job_status, download_results_csv


def make_driver(row_texts=(), url="https://admin.cloud.microsoft/#/Domains", body=""):
    driver = Mock()
    driver.current_url = url
    rows = []
    for text in row_texts:
        row = Mock()
        row.text = text
        row.is_displayed.return_value = True
        rows.append(row)
    driver.find_elements.return_value = rows
    driver.find_element.return_value.text = body
    return driver


@pytest.mark.parametrize("url", [
    "https://login.microsoftonline.com/common/oauth2/authorize?redirect_uri=https%3A%2F%2Fadmin.cloud.microsoft%2Flanding",
    "https://login.microsoftonline.com/common/reprocess",
    "https://admin.cloud.microsoft.example.org/#/Domains",
])
def test_oauth_redirect_is_not_admin_center(url):
    assert checker._is_admin_url(url) is False


def test_mfa_account_email_does_not_prove_domains_loaded(monkeypatch):
    driver = make_driver(
        url="https://login.microsoftonline.com/common/reprocess",
        body="admin@one.onmicrosoft.com\nEnter code",
    )
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)
    assert checker._has_domain_table(driver, "one") is False
    with pytest.raises(RuntimeError, match="usage is unknown"):
        checker._wait_for_domains_page(driver, "one", timeout=2)


def test_wrong_tenant_table_is_not_accepted():
    assert not checker._has_domain_table(make_driver(["other.onmicrosoft.com\nHealthy"]), "one")


def test_live_domain_row_shape_includes_custom_default_domain(monkeypatch):
    driver = make_driver([
        "Domain selection\nDomain name\nStatus",
        "\uea3a\naizohfundinggroup.com (Default)\n\uf2bc\n\uec61\nHealthy",
        "\uea3a\noptinexstack1692.onmicrosoft.com\n\uf2bc\n\uec61\nHealthy",
    ])
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)
    domains = checker._scrape_domains(driver, "optinexstack1692")
    result = checker.TenantCheckResult(
        admin_email="admin@optinexstack1692.onmicrosoft.com",
        login_success=True, domain_check_success=True, domains=domains,
    ).to_dict()
    assert result["verified_domains"] == [{"name": "aizohfundinggroup.com", "status": "Healthy"}]
    assert result["custom_domain_count"] == 1


def test_not_verified_does_not_match_verified(monkeypatch):
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)
    driver = make_driver(["one.onmicrosoft.com\nHealthy", "example.com\nNot verified"])
    domains = checker._scrape_domains(driver, "one")
    assert next(domain for domain in domains if domain.name == "example.com").is_verified is False


def test_confirmed_initial_domain_only_is_valid_empty_custom_list(monkeypatch):
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)
    domains = checker._scrape_domains(make_driver(["one.onmicrosoft.com (Default)\nHealthy"]), "one")
    result = checker.TenantCheckResult(
        admin_email="admin@one.onmicrosoft.com", login_success=True,
        domain_check_success=True, domains=domains,
    ).to_dict()
    assert result["domain_check_success"] is True
    assert result["custom_domain_count"] == 0


def test_interrupted_row_read_does_not_return_partial_results(monkeypatch):
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)
    driver = make_driver(["one.onmicrosoft.com\nHealthy"])
    broken_row = Mock()
    broken_row.is_displayed.side_effect = RuntimeError("Detached row")
    driver.find_elements.return_value.append(broken_row)
    with pytest.raises(RuntimeError, match="read was interrupted"):
        checker._scrape_domains(driver, "one")


def test_post_login_read_failure_is_not_success(monkeypatch):
    driver = make_driver()
    monkeypatch.setattr(checker, "create_driver", lambda **_: driver)
    monkeypatch.setattr(checker, "cleanup_driver", lambda _: None)
    monkeypatch.setattr(checker, "_do_login", lambda *_: True)
    monkeypatch.setattr(checker, "_wait_for_domains_page", Mock(side_effect=RuntimeError("Domain table did not load")))
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)
    result = checker.check_tenant_domains("admin@one.onmicrosoft.com", "test-password")
    assert result.login_success is True
    assert result.domain_check_success is False
    assert "Domain table did not load" in result.login_error


def test_delayed_mfa_is_submitted_before_accepting_admin_center(monkeypatch):
    driver = make_driver(url="https://login.microsoftonline.com/common/reprocess?redirect_uri=https://admin.cloud.microsoft")
    code_input = Mock()
    code_input.is_displayed.return_value = True
    submitted = False

    def send_keys(value):
        nonlocal submitted
        if value == checker.Keys.RETURN:
            submitted = True
            driver.current_url = "https://admin.cloud.microsoft/#/homepage"

    code_input.send_keys.side_effect = send_keys
    driver.find_elements.side_effect = lambda by, selector: (
        ([] if submitted else [code_input]) if (by, selector) == (By.NAME, "otc")
        else [Mock()] if selector == "#O365_MainLink_NavMenu, [role='navigation'], nav" and submitted
        else []
    )
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)
    monkeypatch.setattr(checker.time, "time", lambda: 60)
    assert checker._finish_admin_login(driver, "one", "JBSWY3DPEHPK3PXP")
    assert submitted


def test_push_mfa_switches_to_authenticator_code(monkeypatch):
    driver = make_driver(url="https://login.microsoftonline.com/common/SAS/ProcessAuth", body="Approve sign-in request")
    stage = {"value": "push"}
    switch = Mock()
    switch.is_displayed.return_value = True
    switch.click.side_effect = lambda: stage.update(value="methods")
    method = Mock()
    method.is_displayed.return_value = True
    method.click.side_effect = lambda: stage.update(value="code")
    code_input = Mock()
    code_input.is_displayed.return_value = True

    def send_keys(value):
        if value == checker.Keys.RETURN:
            stage["value"] = "admin"
            driver.current_url = "https://admin.cloud.microsoft/#/homepage"

    code_input.send_keys.side_effect = send_keys

    def find_elements(by, selector):
        if stage["value"] == "push" and (by, selector) == (By.ID, "signInAnotherWay"):
            return [switch]
        if stage["value"] == "methods" and (by, selector) == (By.CSS_SELECTOR, "[data-value='PhoneAppOTP']"):
            return [method]
        if stage["value"] == "code" and (by, selector) == (By.NAME, "otc"):
            return [code_input]
        if stage["value"] == "admin" and selector == "#O365_MainLink_NavMenu, [role='navigation'], nav":
            return [Mock()]
        return []

    driver.find_elements.side_effect = find_elements
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)
    monkeypatch.setattr(checker.time, "time", lambda: 60)
    assert checker._finish_admin_login(driver, "one", "JBSWY3DPEHPK3PXP")
    assert stage["value"] == "admin"
    switch.click.assert_called_once()
    method.click.assert_called_once()


def test_admin_shell_without_navigation_can_proceed_to_verified_domain_read(monkeypatch):
    driver = make_driver(url="https://admin.cloud.microsoft/#/homepage", body="Microsoft 365 admin center")
    driver.find_elements.return_value = []
    ticks = count(100)
    monkeypatch.setattr(checker.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)
    assert checker._finish_admin_login(driver, "one", None, timeout=20)


def test_push_mfa_without_secret_fails_promptly(monkeypatch):
    driver = make_driver(url="https://login.microsoftonline.com/common/SAS/ProcessAuth", body="Approve sign-in request")
    driver.find_elements.return_value = []
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)
    assert checker._finish_admin_login(driver, "one", None, timeout=1) is False


def test_stalled_login_retries_in_fresh_browser(monkeypatch):
    drivers = [make_driver(), make_driver()]
    make_browser = Mock(side_effect=drivers)
    login = Mock(side_effect=[checker.TransientLoginError("Microsoft login stalled"), True])
    cleanup = Mock()
    monkeypatch.setattr(checker, "create_driver", make_browser)
    monkeypatch.setattr(checker, "_do_login", login)
    monkeypatch.setattr(checker, "cleanup_driver", cleanup)
    kill_browsers = Mock()
    monkeypatch.setattr(checker, "kill_all_browsers", kill_browsers)
    monkeypatch.setattr(checker, "_wait_for_domains_page", Mock())
    monkeypatch.setattr(checker, "_scrape_domains", lambda *_: [checker.DomainInfo("one.onmicrosoft.com")])
    monkeypatch.setattr(checker.time, "sleep", lambda _: None)

    result = checker.check_tenant_domains("admin@one.onmicrosoft.com", "test-password")
    assert result.login_success is True
    assert result.domain_check_success is True
    assert make_browser.call_count == 2
    assert cleanup.call_count == 2
    kill_browsers.assert_not_called()


async def test_summary_and_csv_do_not_call_incomplete_check_empty():
    checker_jobs["test-incomplete"] = {
        "status": "complete", "total": 1, "processed": 1,
        "started_at": "", "completed_at": "",
        "results": [{"login_success": True, "domain_check_success": False,
                     "custom_domain_count": 0, "login_error": "Domain table did not load"}],
    }
    try:
        status = await get_job_status("test-incomplete")
        assert status.summary["tenants_no_domains"] == 0
        assert status.summary["domain_checks_failed"] == 1
        response = await download_results_csv("test-incomplete")
        chunks = [chunk async for chunk in response.body_iterator]
        text = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
        assert "Domain Check Success" in text
        assert "False,,,," in text
    finally:
        checker_jobs.pop("test-incomplete")
