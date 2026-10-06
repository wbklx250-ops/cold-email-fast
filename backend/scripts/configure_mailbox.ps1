# configure_mailbox.ps1
# Configures a mailbox in Exchange Online:
# - Enable account
# - Set password
# - Fix UPN to match email
# - Setup delegation (FullAccess, SendAs, SendOnBehalf)
# Outputs JSON with configuration status

param(
    [Parameter(Mandatory=$true)]
    [string]$AdminEmail,
    
    [Parameter(Mandatory=$true)]
    [string]$AdminPassword,
    
    [Parameter(Mandatory=$true)]
    [string]$Email,  # Mailbox email to configure
    
    [Parameter(Mandatory=$true)]
    [string]$Password,  # Password to set for the mailbox
    
    [Parameter(Mandatory=$true)]
    [string]$LicensedUserEmail,  # Email of licensed user to grant delegation

    [Parameter(Mandatory=$false)]
    [bool]$SkipDelegation = $false
)

# Set error action preference
$ErrorActionPreference = "Stop"

# Function to output error as JSON and exit
function Write-ErrorJson {
    param([string]$Message)
    $errorResult = @{
        success = $false
        email = $Email
        error = $Message
    } | ConvertTo-Json -Compress
    Write-Output $errorResult
    exit 1
}

# Function to output success as JSON and exit
function Write-SuccessJson {
    param([hashtable]$Data)
    $Data.success = $true
    $Data.email = $Email
    $result = $Data | ConvertTo-Json -Depth 3 -Compress
    Write-Output $result
    exit 0
}

try {
    # Create credential object for Exchange Online
    $secureAdminPassword = ConvertTo-SecureString $AdminPassword -AsPlainText -Force
    $adminCredential = New-Object System.Management.Automation.PSCredential($AdminEmail, $secureAdminPassword)
    
    # Check if required modules are available
    if (-not (Get-Module -ListAvailable -Name ExchangeOnlineManagement)) {
        Write-ErrorJson -Message "ExchangeOnlineManagement module is not installed. Please run: Install-Module -Name ExchangeOnlineManagement -Force"
    }
    
    if (-not (Get-Module -ListAvailable -Name Microsoft.Graph)) {
        Write-ErrorJson -Message "Microsoft.Graph module is not installed. Please run: Install-Module -Name Microsoft.Graph -Force"
    }
    
    # Import modules
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
    Import-Module Microsoft.Graph.Users -RequiredVersion 2.41.0 -ErrorAction Stop
    
    # Connect to Exchange Online
    Connect-ExchangeOnline -Credential $adminCredential -ShowBanner:$false -ErrorAction Stop
    
    # Track configuration status
    $configStatus = @{
        mailbox_found = $false
        upn_fixed = $false
        password_set = $false
        account_enabled = $false
        full_access_granted = $false
        send_as_granted = $false
        send_on_behalf_granted = $false
        errors = @()
    }
    
    # Get the mailbox
    $mailbox = Get-Mailbox -Identity $Email -ErrorAction Stop
    $configStatus.mailbox_found = $true
    
    # Get the user's UPN (may be different from email)
    $userPrincipalName = $mailbox.UserPrincipalName
    
    # Connect to Microsoft Graph for user management (password, UPN, enable)
    try {
        # Use client credential flow with admin credentials
        $securePassword = ConvertTo-SecureString $AdminPassword -AsPlainText -Force
        $credential = New-Object System.Management.Automation.PSCredential($AdminEmail, $securePassword)
        Connect-MgGraph -Credential $credential -NoWelcome -ErrorAction Stop
        
        # Get the user from Azure AD
        $user = Get-MgUser -UserId $userPrincipalName -ErrorAction Stop
        
        # Fix UPN to match email if needed
        if ($user.UserPrincipalName -ne $Email) {
            try {
                Update-MgUser -UserId $user.Id -UserPrincipalName $Email -ErrorAction Stop
                $configStatus.upn_fixed = $true
            } catch {
                $configStatus.errors += "Failed to update UPN: $($_.Exception.Message)"
            }
        } else {
            $configStatus.upn_fixed = $true  # Already correct
        }
        
        # Set password
        try {
            $passwordProfile = @{
                Password = $Password
                ForceChangePasswordNextSignIn = $false
            }
            Update-MgUser -UserId $user.Id -PasswordProfile $passwordProfile -ErrorAction Stop
            $configStatus.password_set = $true
        } catch {
            $configStatus.errors += "Failed to set password: $($_.Exception.Message)"
        }
        
        # Enable account
        try {
            Update-MgUser -UserId $user.Id -AccountEnabled:$true -ErrorAction Stop
            $configStatus.account_enabled = $true
        } catch {
            $configStatus.errors += "Failed to enable account: $($_.Exception.Message)"
        }
        
        Disconnect-MgGraph -ErrorAction SilentlyContinue
        
    } catch {
        $configStatus.errors += "Graph API connection failed: $($_.Exception.Message)"
    }
    
    if (-not $SkipDelegation) {
        # Grant Full Access permission
        try {
            Add-MailboxPermission -Identity $Email -User $LicensedUserEmail -AccessRights FullAccess -InheritanceType All -AutoMapping $true -ErrorAction Stop
            $configStatus.full_access_granted = $true
        } catch {
            if ($_.Exception.Message -like "*already has*" -or $_.Exception.Message -like "*already exists*") {
                $configStatus.full_access_granted = $true  # Already granted
            } else {
                $configStatus.errors += "Failed to grant FullAccess: $($_.Exception.Message)"
            }
        }
        
        # Grant Send As permission
        try {
            Add-RecipientPermission -Identity $Email -Trustee $LicensedUserEmail -AccessRights SendAs -Confirm:$false -ErrorAction Stop
            $configStatus.send_as_granted = $true
        } catch {
            if ($_.Exception.Message -like "*already has*" -or $_.Exception.Message -like "*already exists*") {
                $configStatus.send_as_granted = $true  # Already granted
            } else {
                $configStatus.errors += "Failed to grant SendAs: $($_.Exception.Message)"
            }
        }
        
        # Grant Send On Behalf permission
        try {
            Set-Mailbox -Identity $Email -GrantSendOnBehalfTo @{Add=$LicensedUserEmail} -ErrorAction Stop
            $configStatus.send_on_behalf_granted = $true
        } catch {
            if ($_.Exception.Message -like "*already*") {
                $configStatus.send_on_behalf_granted = $true  # Already granted
            } else {
                $configStatus.errors += "Failed to grant SendOnBehalf: $($_.Exception.Message)"
            }
        }
    }
    
    # Disconnect from Exchange Online
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
    
    # Determine overall success
    $allSuccess = $configStatus.mailbox_found
    
    $result = @{
        mailbox_found = $configStatus.mailbox_found
        upn_fixed = $configStatus.upn_fixed
        password_set = $configStatus.password_set
        account_enabled = $configStatus.account_enabled
        delegation_configured = ($configStatus.full_access_granted -and $configStatus.send_as_granted -and $configStatus.send_on_behalf_granted)
        full_access_granted = $configStatus.full_access_granted
        send_as_granted = $configStatus.send_as_granted
        send_on_behalf_granted = $configStatus.send_on_behalf_granted
        licensed_user = $LicensedUserEmail
    }
    
    if ($configStatus.errors.Count -gt 0) {
        $result.warnings = $configStatus.errors
    }
    
    Write-SuccessJson -Data $result
    
} catch {
    # Make sure to disconnect if connected
    try {
        Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
        Disconnect-MgGraph -ErrorAction SilentlyContinue
    } catch {
        # Ignore disconnect errors
    }
    
    # Check for common errors
    $errorMessage = $_.Exception.Message
    
    if ($errorMessage -like "*couldn't be found*" -or $errorMessage -like "*not found*") {
        Write-ErrorJson -Message "Mailbox '$Email' was not found in Exchange Online."
    } else {
        Write-ErrorJson -Message $errorMessage
    }
}