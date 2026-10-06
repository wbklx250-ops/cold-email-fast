"""
Mailbox Script Generator

Generates PowerShell scripts for:
- Creating shared mailboxes
- Enabling accounts
- Setting passwords
- Fixing UPNs and display names
- Delegating to licensed user

Based on master_script_v2.ps1
"""

from datetime import datetime
from typing import List, Dict
import csv
import io


class MailboxScriptGenerator:
    """Generate mailbox creation scripts."""
    
    def generate_master_script(
        self,
        tenant_id: str,
        licensed_user_upn: str,
        mailboxes: List[Dict[str, str]]
    ) -> str:
        """
        Generate complete mailbox creation script.
        
        Args:
            tenant_id: Microsoft tenant ID
            licensed_user_upn: User to delegate mailboxes to
            mailboxes: List of {display_name, email, password}
        """
        # Create embedded CSV data
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["DisplayName", "EmailAddress", "Password"])
        for mb in mailboxes:
            writer.writerow([mb["display_name"], mb["email"], mb["password"]])
        csv_data = csv_buffer.getvalue()
        
        return f'''# Master Mailbox Creation Script
# Tenant: {tenant_id}
# Mailboxes: {len(mailboxes)}
# Generated: {datetime.utcnow().isoformat()}
#
# This script:
#   1. Creates shared mailboxes
#   2. Enables user accounts
#   3. Sets passwords
#   4. Fixes UPNs to match email
#   5. Fixes display names
#   6. Delegates to licensed user

$ErrorActionPreference = "Stop"

# Embedded mailbox data
$csvData = @"
{csv_data}"@

$mailboxes = $csvData | ConvertFrom-Csv

Write-Host "Mailboxes to create: $($mailboxes.Count)" -ForegroundColor Cyan

# Connect to services
Import-Module Microsoft.Graph.Authentication -RequiredVersion 2.41.0
Import-Module Microsoft.Graph.Users -RequiredVersion 2.41.0
Import-Module ExchangeOnlineManagement

Disconnect-MgGraph -ErrorAction SilentlyContinue
Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "Connecting to Microsoft Graph..." -ForegroundColor Yellow
Connect-MgGraph -TenantId "{tenant_id}" -Scopes "User.ReadWrite.All", "Directory.AccessAsUser.All" -NoWelcome

Write-Host "Connecting to Exchange Online..." -ForegroundColor Yellow
Connect-ExchangeOnline -Organization "{tenant_id}" -ShowBanner:$false

# Licensed user for delegation
$licensedUser = "{licensed_user_upn}"

# ============================================================
# STEP 1: CREATE SHARED MAILBOXES
# ============================================================
Write-Host "`n=== STEP 1: Creating Shared Mailboxes ===" -ForegroundColor Magenta

foreach ($mb in $mailboxes) {{
    $email = $mb.EmailAddress
    $displayName = $mb.DisplayName
    
    $existing = Get-Mailbox -Identity $email -ErrorAction SilentlyContinue
    if ($existing) {{
        Write-Host "  EXISTS: $email" -ForegroundColor Yellow
        continue
    }}
    
    # Create with temp name to avoid conflicts
    $tempName = "$displayName $(Get-Random -Maximum 9999)"
    
    try {{
        New-Mailbox -Shared -Name $tempName -DisplayName $tempName -PrimarySmtpAddress $email | Out-Null
        Write-Host "  CREATED: $email" -ForegroundColor Green
    }} catch {{
        Write-Host "  ERROR: $email - $($_.Exception.Message)" -ForegroundColor Red
    }}
}}

Write-Host "Waiting 30s for provisioning..." -ForegroundColor Yellow
Start-Sleep -Seconds 30

# ============================================================
# STEP 2: FIX DISPLAY NAMES
# ============================================================
Write-Host "`n=== STEP 2: Fixing Display Names ===" -ForegroundColor Magenta

foreach ($mb in $mailboxes) {{
    $email = $mb.EmailAddress
    $displayName = $mb.DisplayName
    
    $mailbox = Get-Mailbox -Identity $email -ErrorAction SilentlyContinue
    if ($mailbox -and $mailbox.DisplayName -ne $displayName) {{
        Set-Mailbox -Identity $email -DisplayName $displayName -Name $displayName
        Write-Host "  FIXED: $email -> $displayName" -ForegroundColor Green
    }}
}}

# ============================================================
# STEP 3: ENABLE USER ACCOUNTS
# ============================================================
Write-Host "`n=== STEP 3: Enabling Accounts ===" -ForegroundColor Magenta

foreach ($mb in $mailboxes) {{
    $email = $mb.EmailAddress
    
    $user = Get-MgUser -Filter "mail eq '$email'" -ErrorAction SilentlyContinue
    if (-not $user) {{
        $user = Get-MgUser -Filter "proxyAddresses/any(p:p eq 'SMTP:$email')" -ErrorAction SilentlyContinue
    }}
    
    if ($user -and -not $user.AccountEnabled) {{
        Update-MgUser -UserId $user.Id -AccountEnabled:$true
        Write-Host "  ENABLED: $email" -ForegroundColor Green
    }}
}}

# ============================================================
# STEP 4: SET PASSWORDS
# ============================================================
Write-Host "`n=== STEP 4: Setting Passwords ===" -ForegroundColor Magenta

foreach ($mb in $mailboxes) {{
    $email = $mb.EmailAddress
    $password = $mb.Password
    
    $user = Get-MgUser -Filter "mail eq '$email'" -ErrorAction SilentlyContinue
    if ($user) {{
        $params = @{{
            PasswordProfile = @{{
                Password = $password
                ForceChangePasswordNextSignIn = $false
            }}
        }}
        Update-MgUser -UserId $user.Id -BodyParameter $params
        Write-Host "  SET: $email" -ForegroundColor Green
    }}
}}

# ============================================================
# STEP 5: FIX UPNs
# ============================================================
Write-Host "`n=== STEP 5: Fixing UPNs ===" -ForegroundColor Magenta

foreach ($mb in $mailboxes) {{
    $email = $mb.EmailAddress
    
    $user = Get-MgUser -Filter "mail eq '$email'" -ErrorAction SilentlyContinue
    if ($user -and $user.UserPrincipalName -ne $email) {{
        Update-MgUser -UserId $user.Id -UserPrincipalName $email
        Write-Host "  FIXED: $($user.UserPrincipalName) -> $email" -ForegroundColor Green
    }}
}}

# ============================================================
# STEP 6: DELEGATE TO LICENSED USER
# ============================================================
Write-Host "`n=== STEP 6: Delegating to $licensedUser ===" -ForegroundColor Magenta

foreach ($mb in $mailboxes) {{
    $email = $mb.EmailAddress
    
    try {{
        Add-MailboxPermission -Identity $email -User $licensedUser -AccessRights FullAccess -InheritanceType All -AutoMapping $false -ErrorAction SilentlyContinue | Out-Null
        Add-RecipientPermission -Identity $email -Trustee $licensedUser -AccessRights SendAs -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        Set-Mailbox -Identity $email -GrantSendOnBehalfTo $licensedUser -ErrorAction SilentlyContinue | Out-Null
        Write-Host "  DELEGATED: $email" -ForegroundColor Green
    }} catch {{
        Write-Host "  ERROR: $email - $($_.Exception.Message)" -ForegroundColor Red
    }}
}}

# Done
Disconnect-MgGraph -ErrorAction SilentlyContinue
Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "`n=== COMPLETE ===" -ForegroundColor Green
Write-Host "Created and configured $($mailboxes.Count) mailboxes."
'''


mailbox_scripts = MailboxScriptGenerator()