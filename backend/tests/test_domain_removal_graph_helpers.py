from app.services.selenium.domain_removal import (
    _is_default_domain_deletion_error,
    _select_default_fallback_domain,
)


def test_default_domain_deletion_error_detects_graph_detail():
    body = (
        '{"error":{"code":"Request_BadRequest","message":"Domain deletion attempt failed.",'
        '"details":[{"code":"DefaultDomainDeletion","message":"Cannot delete the default domain.",'
        '"target":"isDefault"}]}}'
    )

    assert _is_default_domain_deletion_error(body)


def test_select_default_fallback_prefers_initial_onmicrosoft_domain():
    domains = [
        {"id": "custom.example", "isVerified": True, "isDefault": True},
        {"id": "tenant.mail.onmicrosoft.com", "isVerified": True, "isInitial": False},
        {"id": "other.onmicrosoft.com", "isVerified": True, "isInitial": False},
        {"id": "tenant.onmicrosoft.com", "isVerified": True, "isInitial": True},
    ]

    assert (
        _select_default_fallback_domain(domains, "custom.example")
        == "tenant.onmicrosoft.com"
    )


def test_select_default_fallback_excludes_target_and_unverified_domains():
    domains = [
        {"id": "target.onmicrosoft.com", "isVerified": True, "isInitial": True},
        {"id": "unverified.onmicrosoft.com", "isVerified": False, "isInitial": False},
        {"id": "tenant.mail.onmicrosoft.com", "isVerified": True, "isInitial": False},
    ]

    assert _select_default_fallback_domain(domains, "target.onmicrosoft.com") is None
