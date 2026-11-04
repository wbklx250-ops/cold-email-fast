from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.selenium import admin_portal


@pytest.fixture
def provider_error(monkeypatch):
    monkeypatch.setattr(admin_portal, "_safe_page_text", lambda _: (
        "Refresh the page or try again later. The Domain Connect Uri for domain's "
        "DNS hosting provider cannot be obtained at this time"
    ))
    monkeypatch.setattr(admin_portal.time, "sleep", lambda _: None)
    monkeypatch.setattr(admin_portal, "screenshot", Mock())
    field = Mock()
    field.get_attribute.return_value = "example.com"
    monkeypatch.setattr(admin_portal, "_find_first_visible", Mock(return_value=(field, "input")))
    click = Mock(return_value=True)
    monkeypatch.setattr(admin_portal, "_click_first_visible", click)
    return field, click


def test_provider_error_dismisses_banner_then_resubmits_same_domain(provider_error):
    _, click = provider_error
    driver = SimpleNamespace()
    assert admin_portal._recover_domain_connect_provider_error(driver, "example.com", "connect page wait")
    assert [call.args[3] for call in click.call_args_list] == [
        "DNS-provider error Dismiss", "Use this domain after DNS-provider lookup error",
    ]
    assert driver._domain_connect_retries == {"example.com": 1}


def test_provider_error_stops_after_three_attempts_instead_of_looping(provider_error):
    _, click = provider_error
    driver = SimpleNamespace()
    for _ in range(3):
        admin_portal._recover_domain_connect_provider_error(driver, "example.com", "connect page wait")
    with pytest.raises(RuntimeError, match="after 3 wizard resubmissions"):
        admin_portal._recover_domain_connect_provider_error(driver, "example.com", "connect page wait")
    assert click.call_count == 6


def test_provider_error_never_submits_a_different_domain(provider_error):
    field, click = provider_error
    field.get_attribute.return_value = "other.com"
    with pytest.raises(RuntimeError, match="unexpected domain-entry value"):
        admin_portal._recover_domain_connect_provider_error(SimpleNamespace(), "example.com", "connect page wait")
    click.assert_not_called()


def test_other_wizard_pages_are_untouched(monkeypatch):
    monkeypatch.setattr(admin_portal, "_safe_page_text", lambda _: "How do you want to connect your domain?")
    assert not admin_portal._recover_domain_connect_provider_error(SimpleNamespace(), "example.com", "connect page wait")
