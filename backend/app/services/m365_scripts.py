"""
M365 Script Generator

Generates PowerShell scripts for:
- Adding domain to tenant
- Getting verification TXT
- Verifying domain
- Getting DKIM values
- Enabling DKIM
"""

from datetime import datetime


class M365ScriptGenerator:
    """Generate M365 setup scripts."""
    
    def generate_add_domain_script(self, tenant_id: str, domain: str) -> str:
        """Script to add domain and get verification TXT."""
        return f'''# Add Domain to M365 Tenant
# Domain: {domain}
# Generated: {datetime.utcnow().isoformat()}

$ErrorActionPreference = "Stop"

Import-Module Microsoft.Graph.Authentication -RequiredVersion 2.41.0
Import-Module Microsoft.Graph.Identity.DirectoryManagement -RequiredVersion 2.41.0

Disconnect-MgGraph -ErrorAction SilentlyContinue

Write-Host "Connecting to tenant..." -ForegroundColor Cyan
Connect-MgGraph -TenantId "{tenant_id}" -Scopes "Domain.ReadWrite.All" -NoWelcome

$domain = "{domain}"

# Check if exists
$existing = Get-MgDomain -DomainId $domain -ErrorAction SilentlyContinue
if ($existing -and $existing.IsVerified) {{
    Write-Host "Domain already verified!" -ForegroundColor Green
    exit 0
}}

if (-not $existing) {{
    Write-Host "Adding domain..." -ForegroundColor Yellow
    New-MgDomain -Id $domain | Out-Null
}}

# Get verification TXT
$records = Get-MgDomainVerificationDnsRecord -DomainId $domain
$txt = $records | Where-Object {{ $_.RecordType -eq "Txt" }}

Write-Host ""
Write-Host "=== COPY THIS VALUE ===" -ForegroundColor Yellow
Write-Host "MS_VERIFICATION_TXT=$($txt.Text)"
Write-Host "=== END ===" -ForegroundColor Yellow

Disconnect-MgGraph
'''

    def generate_verify_domain_script(self, tenant_id: str, domain: str) -> str:
        """Script to verify domain ownership."""
        return f'''# Verify Domain
# Domain: {domain}
# Generated: {datetime.utcnow().isoformat()}

Import-Module Microsoft.Graph.Authentication -RequiredVersion 2.41.0
Import-Module Microsoft.Graph.Identity.DirectoryManagement -RequiredVersion 2.41.0

Disconnect-MgGraph -ErrorAction SilentlyContinue
Connect-MgGraph -TenantId "{tenant_id}" -Scopes "Domain.ReadWrite.All" -NoWelcome

try {{
    Confirm-MgDomain -DomainId "{domain}"
    Write-Host "Domain verified!" -ForegroundColor Green
}} catch {{
    Write-Host "Verification failed. DNS may not have propagated." -ForegroundColor Red
    Write-Host $_.Exception.Message
}}

Disconnect-MgGraph
'''

    def generate_get_dkim_script(self, tenant_id: str, domain: str) -> str:
        """Script to get DKIM CNAME values."""
        return f'''# Get DKIM Values
# Domain: {domain}
# Generated: {datetime.utcnow().isoformat()}

Import-Module ExchangeOnlineManagement

Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
Connect-ExchangeOnline -Organization "{tenant_id}" -ShowBanner:$false

$dkim = Get-DkimSigningConfig -Identity "{domain}" -ErrorAction SilentlyContinue

if (-not $dkim) {{
    Write-Host "Initializing DKIM..." -ForegroundColor Yellow
    New-DkimSigningConfig -DomainName "{domain}" -Enabled $false | Out-Null
    $dkim = Get-DkimSigningConfig -Identity "{domain}"
}}

Write-Host ""
Write-Host "=== COPY THESE VALUES ===" -ForegroundColor Yellow
Write-Host "DKIM_SELECTOR1=$($dkim.Selector1CNAME)"
Write-Host "DKIM_SELECTOR2=$($dkim.Selector2CNAME)"
Write-Host "=== END ===" -ForegroundColor Yellow

Disconnect-ExchangeOnline -Confirm:$false
'''

    def generate_enable_dkim_script(self, tenant_id: str, domain: str) -> str:
        """Script to enable DKIM signing."""
        return f'''# Enable DKIM
# Domain: {domain}
# Generated: {datetime.utcnow().isoformat()}

Import-Module ExchangeOnlineManagement

Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
Connect-ExchangeOnline -Organization "{tenant_id}" -ShowBanner:$false

try {{
    Set-DkimSigningConfig -Identity "{domain}" -Enabled $true
    Write-Host "DKIM enabled!" -ForegroundColor Green
}} catch {{
    Write-Host "DKIM enable failed. CNAMEs may not have propagated." -ForegroundColor Red
    Write-Host $_.Exception.Message
}}

Disconnect-ExchangeOnline -Confirm:$false
'''

    def get_mail_dns_values(self, domain: str) -> dict:
        """Get MX, SPF, Autodiscover values."""
        mx_target = domain.replace('.', '-') + ".mail.protection.outlook.com"
        
        return {
            "mx": {
                "name": "@",
                "priority": 0,
                "target": mx_target
            },
            "spf": {
                "name": "@",
                "value": "v=spf1 include:spf.protection.outlook.com ~all"
            },
            "autodiscover": {
                "name": "autodiscover",
                "target": "autodiscover.outlook.com",
                "proxied": False
            }
        }


m365_scripts = M365ScriptGenerator()