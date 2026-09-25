"""An accepted Graph deletion must not bypass the existing removal fallbacks."""
from unittest.mock import Mock

import pytest
import requests

from app.services.selenium import domain_removal as removal


def install_tiers(monkeypatch, results):
    names = ["_remove_domain_tier1_graph_api", "_remove_domain_tier2_powershell",
             "_remove_domain_tier3_selenium_graph", "remove_domain_from_m365"]
    mocks = []
    for name, result in zip(names, results):
        callback = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
        monkeypatch.setattr(removal, name, callback)
        mocks.append(callback)
    return mocks


def run():
    return removal._remove_domain_after_license_cleanup(
        "old.example", "admin@tenant.onmicrosoft.com", "password", "totp", True,
    )


def test_accepted_but_failed_graph_deletion_reaches_existing_portal(monkeypatch):
    tiers = install_tiers(monkeypatch, [
        {"success": True}, {"success": False, "error": "MSOnline unavailable"},
        {"success": True}, {"success": True, "method": "tier4_selenium_portal"},
    ])
    verify = Mock(side_effect=[
        {"verified_removed": False, "error": "Exchange reference remains"},
        {"verified_removed": False, "error": "Exchange reference remains"},
        {"verified_removed": True},
    ])
    monkeypatch.setattr(removal, "_verify_domain_actually_removed", verify)
    result = run()
    assert result["success"] and result["verified"]
    assert result["method"] == "tier4_selenium_portal"
    assert all(tier.call_count == 1 for tier in tiers)
    assert verify.call_count == 3
    tiers[-1].assert_called_once_with("old.example", "admin@tenant.onmicrosoft.com", "password", "totp", headless=True)


def test_verified_graph_deletion_stops_without_portal(monkeypatch):
    tiers = install_tiers(monkeypatch, [{"success": True}] * 4)
    monkeypatch.setattr(removal, "_verify_domain_actually_removed", Mock(return_value={"verified_removed": True}))
    assert run()["success"]
    assert [tier.call_count for tier in tiers] == [1, 0, 0, 0]


def test_no_method_can_report_success_without_verification(monkeypatch):
    tiers = install_tiers(monkeypatch, [{"success": True, "error": None}] * 4)
    monkeypatch.setattr(removal, "_verify_domain_actually_removed", Mock(return_value={"verified_removed": False, "error": "Still present"}))
    result = run()
    assert not result["success"] and not result["verified"] and result["needs_retry"]
    assert all(tier.call_count == 1 for tier in tiers)
    assert len(result["attempts"]) == 4


def test_verification_exception_continues_to_remaining_methods(monkeypatch):
    tiers = install_tiers(monkeypatch, [{"success": True}] * 4)
    monkeypatch.setattr(removal, "_verify_domain_actually_removed", Mock(side_effect=[RuntimeError("timeout"), {"verified_removed": True}]))
    assert run()["success"]
    assert [tier.call_count for tier in tiers] == [1, 1, 0, 0]


@pytest.mark.parametrize("graph_absent", [None, False, True])
def test_network_failures_are_not_evidence_of_removal(monkeypatch, graph_absent):
    monkeypatch.setattr(requests, "get", Mock(side_effect=requests.Timeout("unavailable")))
    monkeypatch.setattr(removal.time, "sleep", Mock())
    monkeypatch.setattr(removal, "_get_access_token_via_msal", Mock(return_value=(True, "test-token", None)))
    monkeypatch.setattr(removal, "_run_async_graph_verify", Mock(return_value=graph_absent))
    result = removal._verify_domain_actually_removed("old.example", "admin@tenant.onmicrosoft.com", "password")
    assert result["verified_removed"] is (graph_absent is True)
