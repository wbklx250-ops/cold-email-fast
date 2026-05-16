"""
Test tenant import matching by onmicrosoft domain.

Run with: python test_tenant_import.py
"""

import sys
sys.path.insert(0, 'app')

from app.services.tenant_import import TenantImportService
from app.services.validation_service import parse_tenants_csv_content, cross_validate


def test_domain_matching():
    """Test that CSV and TXT are matched by domain, not row position."""
    
    service = TenantImportService()
    
    # CSV with tenants in alphabetical order
    csv_content = """Company Name,onmicrosoft,Address,Admin Name,Admin Email,Admin Phone,UUID
Acme Corp,acmecorp123,123 Main St,John Doe,john@gmail.com,555-0001,uuid-acme-123
Beta Inc,betainc456,456 Oak Ave,Jane Smith,jane@gmail.com,555-0002,uuid-beta-456
Gamma LLC,gammallc789,789 Pine Rd,Bob Wilson,bob@gmail.com,555-0003,uuid-gamma-789
Delta Co,deltaco000,000 Elm St,Alice Brown,alice@gmail.com,555-0004,uuid-delta-000"""

    # TXT with credentials in DIFFERENT order (not alphabetical!)
    txt_content = """admin@gammallc789.onmicrosoft.com
GammaPassword789
admin@acmecorp123.onmicrosoft.com
AcmePassword123
admin@deltaco000.onmicrosoft.com
DeltaPassword000
admin@betainc456.onmicrosoft.com
BetaPassword456"""

    # Parse both
    tenants = service.parse_tenant_csv(csv_content)
    credentials = service.parse_credentials_txt(txt_content)
    
    print("=" * 60)
    print("PARSED DATA")
    print("=" * 60)
    
    print(f"\nTenants from CSV ({len(tenants)} total):")
    for i, t in enumerate(tenants):
        print(f"  {i+1}. {t['company_name']} -> {t['onmicrosoft_domain']}")
    
    print(f"\nCredentials from TXT ({len(credentials)} total):")
    for domain, cred in credentials.items():
        print(f"  {domain} -> {cred.email} / {cred.password}")
    
    # Merge with domain matching
    merged, unmatched_tenants, unmatched_creds = service.merge_data(tenants, credentials)
    
    print("\n" + "=" * 60)
    print("MERGED RESULTS (matched by domain)")
    print("=" * 60)
    
    all_matched = True
    for m in merged:
        expected_domain = m['onmicrosoft_domain'].replace('.onmicrosoft.com', '')
        password = m['admin_password']
        
        # Check that password matches the tenant (e.g., acmecorp123 -> AcmePassword123)
        password_matches = expected_domain.lower() in password.lower() or password != ""
        
        status = "✓" if password else "✗ NO PASSWORD"
        print(f"  {m['name']}: {m['admin_email']} / {password} {status}")
        
        if not password:
            all_matched = False
    
    print(f"\nUnmatched tenants: {len(unmatched_tenants)}")
    print(f"Unmatched credentials: {len(unmatched_creds)}")
    
    # Verify specific matches
    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)
    
    # Find Acme Corp and verify it got AcmePassword123
    acme = next((m for m in merged if 'Acme' in m['name']), None)
    assert acme is not None, "Acme Corp should be in merged results"
    assert acme['admin_password'] == 'AcmePassword123', f"Acme should have AcmePassword123, got {acme['admin_password']}"
    print(f"✓ Acme Corp correctly matched with AcmePassword123")
    
    # Find Gamma LLC and verify it got GammaPassword789
    gamma = next((m for m in merged if 'Gamma' in m['name']), None)
    assert gamma is not None, "Gamma LLC should be in merged results"
    assert gamma['admin_password'] == 'GammaPassword789', f"Gamma should have GammaPassword789, got {gamma['admin_password']}"
    print(f"✓ Gamma LLC correctly matched with GammaPassword789")
    
    # Verify all matched
    assert all_matched, "All tenants should have passwords"
    assert len(unmatched_tenants) == 0, "No tenants should be unmatched"
    assert len(unmatched_creds) == 0, "No credentials should be unmatched"
    
    print(f"\n✓ ALL TESTS PASSED - Domain matching works correctly!")
    print(f"  Even though CSV row 1 (Acme) corresponds to TXT lines 3-4,")
    print(f"  the matching correctly used the onmicrosoft domain as the key.")


def test_tab_separated_format():
    """Test backwards compatibility with tab-separated format."""
    
    service = TenantImportService()
    
    txt_content = """Username\tPassword
admin@company1.onmicrosoft.com\tPassword1
admin@company2.onmicrosoft.com\tPassword2"""

    credentials = service.parse_credentials_txt(txt_content)
    
    print("\n" + "=" * 60)
    print("TAB-SEPARATED FORMAT TEST")
    print("=" * 60)
    
    assert len(credentials) == 2, f"Expected 2 credentials, got {len(credentials)}"
    assert 'company1.onmicrosoft.com' in credentials
    assert 'company2.onmicrosoft.com' in credentials
    assert credentials['company1.onmicrosoft.com'].password == 'Password1'
    
    print(f"✓ Tab-separated format works correctly")


def test_unmatched_reporting():
    """Test that unmatched tenants and credentials are reported."""
    
    service = TenantImportService()
    
    csv_content = """Company Name,onmicrosoft,Address,Admin Name,Admin Email,Admin Phone,UUID
Existing Tenant,existing123,123 St,John,j@g.com,555,uuid-1
Missing Creds,missingcreds,456 St,Jane,j2@g.com,556,uuid-2"""

    txt_content = """admin@existing123.onmicrosoft.com
Password123
admin@orphan999.onmicrosoft.com
OrphanPassword"""

    tenants = service.parse_tenant_csv(csv_content)
    credentials = service.parse_credentials_txt(txt_content)
    merged, unmatched_tenants, unmatched_creds = service.merge_data(tenants, credentials)
    
    print("\n" + "=" * 60)
    print("UNMATCHED REPORTING TEST")
    print("=" * 60)
    
    assert len(unmatched_tenants) == 1, f"Expected 1 unmatched tenant, got {len(unmatched_tenants)}"
    assert len(unmatched_creds) == 1, f"Expected 1 unmatched credential, got {len(unmatched_creds)}"
    assert unmatched_tenants[0]['onmicrosoft_domain'] == 'missingcreds.onmicrosoft.com'
    assert 'orphan999.onmicrosoft.com' in unmatched_creds
    
    print(f"✓ Unmatched tenant reported: {unmatched_tenants[0]['onmicrosoft_domain']}")
    print(f"✓ Unmatched credential reported: {unmatched_creds[0]}")


def test_provider_csv_with_preconfigured_totp():
    """Test provider CSV where password and TOTP secret are embedded."""

    service = TenantImportService()

    csv_content = """Tenant Name,Passoword,secret key
admin@nexorahubs1679.onmicrosoft.com,ghazi@110,2mpccgbhxdltz6vm
admin@nexorapath1680.onmicrosoft.com,ghazi@110,YZGZFSVN5YFM6CDH"""

    tenants = service.parse_tenant_csv(csv_content)
    assert len(tenants) == 2
    assert tenants[0]["company_name"] == "nexorahubs1679"
    assert tenants[0]["onmicrosoft_domain"] == "nexorahubs1679.onmicrosoft.com"
    assert tenants[0]["admin_email"] == "admin@nexorahubs1679.onmicrosoft.com"
    assert tenants[0]["admin_password"] == "ghazi@110"
    assert tenants[0]["totp_secret"] == "2MPCCGBHXDLTZ6VM"

    merged, unmatched_tenants, unmatched_creds = service.merge_data(tenants, {})
    assert len(merged) == 2
    assert not unmatched_tenants
    assert not unmatched_creds
    assert merged[1]["admin_email"] == "admin@nexorapath1680.onmicrosoft.com"
    assert merged[1]["admin_password"] == "ghazi@110"
    assert merged[1]["totp_secret"] == "YZGZFSVN5YFM6CDH"

    parsed_preview, errors = parse_tenants_csv_content(csv_content)
    assert not errors
    assert parsed_preview[0]["name"] == "nexorahubs1679"
    assert parsed_preview[0]["password"] == "ghazi@110"
    assert parsed_preview[0]["totp_secret"] == "2MPCCGBHXDLTZ6VM"

    validation = cross_validate(
        domains=[{"name": "example.com", "first_name": "Tom", "last_name": "Tenant"}],
        tenants=parsed_preview[:1],
        credentials={},
        first_name="",
        last_name="",
    )
    assert validation["summary"]["credentials_matched"] == 1
    assert validation["summary"]["first_login_precompleted"] == 1

    print("✓ Provider CSV with preconfigured TOTP parses and validates correctly")


if __name__ == '__main__':
    test_domain_matching()
    test_tab_separated_format()
    test_unmatched_reporting()
    test_provider_csv_with_preconfigured_totp()
    
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED!")
    print("=" * 60)
