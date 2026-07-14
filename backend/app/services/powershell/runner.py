"""
PowerShell Runner

Executes PowerShell scripts from Python.
Works on both Windows (powershell.exe) and Linux (pwsh).
Supports Exchange Online with Access Token authentication for DKIM operations.
Supports MFA handling via Selenium with stored TOTP secrets.

NOTE: Domain operations (add_domain_with_mfa, verify_domain_with_mfa) now use
Admin Portal UI automation instead of OAuth tokens. See admin_portal.py.
"""

import asyncio
import json
import os
import sys
import subprocess
import tempfile
import logging
import time
import threading
import re
import traceback
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import pyotp

logger = logging.getLogger(__name__)

# Auto-detect PowerShell path based on OS
if sys.platform == "win32":
    PWSH_PATH = os.environ.get("PWSH_PATH", "powershell.exe")
else:
    PWSH_PATH = os.environ.get("PWSH_PATH", "/usr/bin/pwsh")


@dataclass
class PowerShellResult:
    """Result of PowerShell execution."""
    success: bool
    output: str
    error: Optional[str] = None
    json_data: Optional[Dict[str, Any]] = None


class PowerShellRunner:
    """Execute PowerShell scripts from Python."""
    
    def __init__(self, timeout: int = 300):
        self.timeout = timeout
    
    async def run(self, script: str, timeout: int = None) -> PowerShellResult:
        """
        Execute a PowerShell script using subprocess.run() in a thread pool.
        
        Uses asyncio.to_thread() for Windows compatibility - asyncio.create_subprocess_exec()
        throws NotImplementedError on Windows with the default event loop.
        """
        timeout = timeout or self.timeout
        
        # Write script to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ps1', delete=False, encoding='utf-8') as f:
            f.write(script)
            script_path = f.name
        
        def _run_sync():
            """Synchronous subprocess execution."""
            try:
                result = subprocess.run(
                    [PWSH_PATH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script_path],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    encoding='utf-8',
                    errors='replace'
                )
                return result.returncode == 0, result.stdout or "", result.stderr or ""
            except subprocess.TimeoutExpired:
                return False, "", f"Timed out after {timeout}s"
            except Exception as e:
                return False, "", str(e)
        
        try:
            # Run synchronous subprocess in thread pool to not block async event loop
            success, output, error = await asyncio.to_thread(_run_sync)
            
            # Try to parse JSON from output
            json_data = None
            if "<<<JSON>>>" in output:
                try:
                    json_str = output.split("<<<JSON>>>")[1].split("<<<END>>>")[0].strip()
                    json_data = json.loads(json_str)
                except:
                    pass
            
            return PowerShellResult(
                success=success,
                output=output,
                error=error if error else None,
                json_data=json_data
            )
            
        finally:
            try:
                os.unlink(script_path)
            except:
                pass
    
    async def run_exchange(
        self,
        access_token: str,
        organization: str,
        commands: List[str]
    ) -> PowerShellResult:
        """Run commands connected to Exchange Online."""
        
        script = f'''
$ErrorActionPreference = "Stop"

Import-Module ExchangeOnlineManagement -ErrorAction Stop

try {{
    Connect-ExchangeOnline -AccessToken "{access_token}" -Organization "{organization}" -ShowBanner:$false
    
    {chr(10).join(commands)}
    
}} finally {{
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}}
'''
        return await self.run(script)
    
    async def run_command(self, command: str, timeout: int = None) -> PowerShellResult:
        """
        Run a single PowerShell command using subprocess.run() in a thread pool.
        
        Uses asyncio.to_thread() for Windows compatibility.
        """
        timeout = timeout or self.timeout
        
        def _run_sync():
            """Synchronous subprocess execution."""
            try:
                result = subprocess.run(
                    [PWSH_PATH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    encoding='utf-8',
                    errors='replace'
                )
                return result.returncode == 0, result.stdout or "", result.stderr or ""
            except subprocess.TimeoutExpired:
                return False, "", f"Timed out after {timeout}s"
            except Exception as e:
                return False, "", str(e)
        
        try:
            # Run synchronous subprocess in thread pool to not block async event loop
            success, output, error = await asyncio.to_thread(_run_sync)
            
            return PowerShellResult(
                success=success,
                output=output,
                error=error if error else None
            )
            
        except Exception as e:
            return PowerShellResult(
                success=False,
                output="",
                error=str(e)
            )
    
    async def get_dkim_config(
        self,
        access_token: str,
        organization: str,
        domain: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Get DKIM selector CNAME values from Exchange Online.
        
        Returns: (success, selector1_cname, selector2_cname)
        """
        logger.info(f"Getting DKIM config for {domain}")
        
        script = f'''
$ErrorActionPreference = "Stop"

try {{
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
    Connect-ExchangeOnline -AccessToken "{access_token}" -Organization "{organization}" -ShowBanner:$false
    
    # Check if DKIM config exists, create if not
    $dkim = Get-DkimSigningConfig -Identity "{domain}" -ErrorAction SilentlyContinue
    
    if (-not $dkim) {{
        Write-Host "Creating DKIM signing config..."
        New-DkimSigningConfig -DomainName "{domain}" -Enabled $false -ErrorAction Stop | Out-Null
        Start-Sleep -Seconds 5
        $dkim = Get-DkimSigningConfig -Identity "{domain}" -ErrorAction Stop
    }}
    
    # Output the CNAME values (these are what need to be added to DNS)
    Write-Output "<<<JSON>>>"
    @{{
        "success" = $true
        "selector1_cname" = $dkim.Selector1CNAME
        "selector2_cname" = $dkim.Selector2CNAME
        "enabled" = $dkim.Enabled
    }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
    
}} catch {{
    Write-Output "<<<JSON>>>"
    @{{
        "success" = $false
        "error" = $_.Exception.Message
    }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
}} finally {{
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}}
'''
        result = await self.run(script, timeout=300)
        
        if result.json_data:
            if result.json_data.get("success"):
                return (
                    True,
                    result.json_data.get("selector1_cname"),
                    result.json_data.get("selector2_cname")
                )
            else:
                logger.error(f"DKIM config failed: {result.json_data.get('error')}")
        
        logger.error(f"Failed to get DKIM config: {result.error or result.output}")
        return False, None, None
    
    async def enable_dkim(
        self,
        access_token: str,
        organization: str,
        domain: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Enable DKIM signing for a domain.
        
        Returns: (success, error_message)
        """
        logger.info(f"Enabling DKIM for {domain}")
        
        script = f'''
$ErrorActionPreference = "Stop"

try {{
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
    Connect-ExchangeOnline -AccessToken "{access_token}" -Organization "{organization}" -ShowBanner:$false
    
    # Enable DKIM
    Set-DkimSigningConfig -Identity "{domain}" -Enabled $true -ErrorAction Stop
    
    # Verify it's enabled
    $dkim = Get-DkimSigningConfig -Identity "{domain}" -ErrorAction Stop
    
    Write-Output "<<<JSON>>>"
    @{{
        "success" = $dkim.Enabled
        "status" = $dkim.Status
    }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
    
}} catch {{
    Write-Output "<<<JSON>>>"
    @{{
        "success" = $false
        "error" = $_.Exception.Message
    }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
}} finally {{
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}}
'''
        result = await self.run(script, timeout=180)
        
        if result.json_data:
            if result.json_data.get("success"):
                return True, None
            else:
                error_msg = result.json_data.get("error", "Unknown error")
                logger.error(f"DKIM enable failed: {error_msg}")
                return False, error_msg
        
        error_msg = result.error or "Unknown PowerShell error"
        logger.error(f"Failed to enable DKIM: {error_msg}")
        return False, error_msg
    
    async def check_exchange_module(self) -> bool:
        """Check if ExchangeOnlineManagement module is installed."""
        result = await self.run_command(
            "Get-Module -ListAvailable ExchangeOnlineManagement | Select-Object -First 1"
        )
        return result.success and "ExchangeOnlineManagement" in result.output
    
    async def check_msol_module(self) -> bool:
        """Check if MSOnline module is installed."""
        result = await self.run_command(
            "Get-Module -ListAvailable MSOnline | Select-Object -First 1"
        )
        return result.success and "MSOnline" in result.output
    
    # ============================================================
    # CREDENTIAL-BASED METHODS (No OAuth Required)
    # ============================================================
    
    async def run_msol_with_credentials(
        self,
        admin_email: str,
        admin_password: str,
        commands: List[str]
    ) -> PowerShellResult:
        """
        Run commands connected to MSOnline using username/password credentials.
        
        Note: May fail if MFA is required even with Security Defaults disabled.
        """
        # Escape password for PowerShell (handle special characters)
        escaped_password = admin_password.replace("'", "''")
        
        script = f'''
$ErrorActionPreference = "Stop"

try {{
    Import-Module MSOnline -ErrorAction Stop
    
    $secpasswd = ConvertTo-SecureString '{escaped_password}' -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential ('{admin_email}', $secpasswd)
    
    Connect-MsolService -Credential $credential -ErrorAction Stop
    
    {chr(10).join(commands)}
    
}} catch {{
    Write-Error "MSOnline error: $($_.Exception.Message)"
    exit 1
}}
'''
        return await self.run(script)
    
    async def run_exchange_with_credentials(
        self,
        admin_email: str,
        admin_password: str,
        commands: List[str]
    ) -> PowerShellResult:
        """
        Run commands connected to Exchange Online using username/password credentials.
        
        Note: May fail if MFA is required even with Security Defaults disabled.
        """
        # Escape password for PowerShell
        escaped_password = admin_password.replace("'", "''")
        
        script = f'''
$ErrorActionPreference = "Stop"

try {{
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
    
    $secpasswd = ConvertTo-SecureString '{escaped_password}' -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential ('{admin_email}', $secpasswd)
    
    Connect-ExchangeOnline -Credential $credential -ShowBanner:$false -ErrorAction Stop
    
    {chr(10).join(commands)}
    
}} catch {{
    Write-Error "Exchange Online error: $($_.Exception.Message)"
    exit 1
}} finally {{
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}}
'''
        return await self.run(script)
    
    async def add_domain_msol(
        self,
        admin_email: str,
        admin_password: str,
        domain_name: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Add domain to M365 via MSOnline and get verification TXT record.
        
        Returns: (success, txt_value)
        """
        # DEBUG LOGGING - Entry point
        logger.info(f"[{domain_name}] ENTERED add_domain_msol")
        logger.info(f"[{domain_name}] admin_email: {admin_email}")
        logger.info(f"[{domain_name}] admin_password length: {len(admin_password) if admin_password else 0}")
        
        try:
            escaped_password = admin_password.replace("'", "''")
            logger.info(f"[{domain_name}] Password escaped successfully")
        except Exception as e:
            logger.error(f"[{domain_name}] FAILED to escape password: {e}")
            logger.error(f"[{domain_name}] TRACEBACK:\n{traceback.format_exc()}")
            return False, None
        
        logger.info(f"[{domain_name}] Building PowerShell script...")
        
        script = f'''
$ErrorActionPreference = "Stop"

try {{
    Import-Module MSOnline -ErrorAction Stop
    
    $secpasswd = ConvertTo-SecureString '{escaped_password}' -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential ('{admin_email}', $secpasswd)
    
    Connect-MsolService -Credential $credential -ErrorAction Stop
    
    # Check if domain already exists
    $existingDomain = Get-MsolDomain -DomainName "{domain_name}" -ErrorAction SilentlyContinue
    
    if (-not $existingDomain) {{
        # Add the domain
        New-MsolDomain -Name "{domain_name}" -ErrorAction Stop | Out-Null
        Write-Host "Domain added successfully"
    }} else {{
        Write-Host "Domain already exists"
    }}
    
    # Get verification DNS record
    $dnsRecords = Get-MsolDomainVerificationDns -DomainName "{domain_name}" -Mode DnsTxtRecord -ErrorAction Stop
    
    Write-Output "<<<JSON>>>"
    @{{
        "success" = $true
        "txt_value" = $dnsRecords.Text
        "label" = $dnsRecords.Label
    }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
    
}} catch {{
    Write-Output "<<<JSON>>>"
    @{{
        "success" = $false
        "error" = $_.Exception.Message
    }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
}}
'''
        logger.info(f"[{domain_name}] Script built ({len(script)} chars), calling self.run()...")
        
        try:
            result = await self.run(script, timeout=120)
            logger.info(f"[{domain_name}] self.run() completed")
            logger.info(f"[{domain_name}] Result success: {result.success}")
            logger.info(f"[{domain_name}] Result output (first 500): {result.output[:500] if result.output else 'empty'}")
            logger.info(f"[{domain_name}] Result error: {result.error[:500] if result.error else 'None'}")
            logger.info(f"[{domain_name}] Result json_data: {result.json_data}")
        except Exception as e:
            logger.error(f"[{domain_name}] EXCEPTION in self.run(): {str(e)}")
            logger.error(f"[{domain_name}] TRACEBACK:\n{traceback.format_exc()}")
            return False, None
        
        if result.json_data:
            if result.json_data.get("success"):
                txt_value = result.json_data.get("txt_value")
                logger.info(f"[{domain_name}] Domain added successfully, TXT: {txt_value}")
                return True, txt_value
            else:
                logger.error(f"[{domain_name}] Add domain failed (json error): {result.json_data.get('error')}")
                return False, None
        
        logger.error(f"[{domain_name}] Add domain failed (no json): error={result.error}, output={result.output[:200] if result.output else 'empty'}")
        return False, None
    
    async def verify_domain_msol(
        self,
        admin_email: str,
        admin_password: str,
        domain_name: str
    ) -> bool:
        """
        Verify domain ownership in M365 via MSOnline.
        
        Returns: True if verified
        """
        logger.info(f"Verifying domain {domain_name} via MSOnline")
        
        escaped_password = admin_password.replace("'", "''")
        
        script = f'''
$ErrorActionPreference = "Stop"

try {{
    Import-Module MSOnline -ErrorAction Stop
    
    $secpasswd = ConvertTo-SecureString '{escaped_password}' -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential ('{admin_email}', $secpasswd)
    
    Connect-MsolService -Credential $credential -ErrorAction Stop
    
    # Check if already verified
    $domain = Get-MsolDomain -DomainName "{domain_name}" -ErrorAction Stop
    
    if ($domain.Status -eq "Verified") {{
        Write-Output "<<<JSON>>>"
        @{{ "success" = $true; "already_verified" = $true }} | ConvertTo-Json -Compress
        Write-Output "<<<END>>>"
        exit 0
    }}
    
    # Attempt verification
    Confirm-MsolDomain -DomainName "{domain_name}" -ErrorAction Stop
    
    Write-Output "<<<JSON>>>"
    @{{ "success" = $true; "already_verified" = $false }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
    
}} catch {{
    Write-Output "<<<JSON>>>"
    @{{ "success" = $false; "error" = $_.Exception.Message }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
}}
'''
        result = await self.run(script, timeout=120)
        
        if result.json_data:
            if result.json_data.get("success"):
                logger.info(f"Domain {domain_name} verified")
                return True
            else:
                logger.error(f"Domain verification failed: {result.json_data.get('error')}")
                return False
        
        logger.error(f"Domain verification failed: {result.error or result.output}")
        return False
    
    async def get_dkim_config_with_credentials(
        self,
        admin_email: str,
        admin_password: str,
        domain_name: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Get DKIM selector CNAME values from Exchange Online using credentials.
        
        Returns: (success, selector1_cname, selector2_cname)
        """
        logger.info(f"Getting DKIM config for {domain_name} with credentials")
        
        escaped_password = admin_password.replace("'", "''")
        
        script = f'''
$ErrorActionPreference = "Stop"

try {{
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
    
    $secpasswd = ConvertTo-SecureString '{escaped_password}' -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential ('{admin_email}', $secpasswd)
    
    Connect-ExchangeOnline -Credential $credential -ShowBanner:$false -ErrorAction Stop
    
    # Check if DKIM config exists, create if not
    $dkim = Get-DkimSigningConfig -Identity "{domain_name}" -ErrorAction SilentlyContinue
    
    if (-not $dkim) {{
        Write-Host "Creating DKIM signing config..."
        New-DkimSigningConfig -DomainName "{domain_name}" -Enabled $false -ErrorAction Stop | Out-Null
        Start-Sleep -Seconds 5
        $dkim = Get-DkimSigningConfig -Identity "{domain_name}" -ErrorAction Stop
    }}
    
    Write-Output "<<<JSON>>>"
    @{{
        "success" = $true
        "selector1_cname" = $dkim.Selector1CNAME
        "selector2_cname" = $dkim.Selector2CNAME
        "enabled" = $dkim.Enabled
    }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
    
}} catch {{
    Write-Output "<<<JSON>>>"
    @{{
        "success" = $false
        "error" = $_.Exception.Message
    }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
}} finally {{
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}}
'''
        result = await self.run(script, timeout=300)
        
        if result.json_data:
            if result.json_data.get("success"):
                return (
                    True,
                    result.json_data.get("selector1_cname"),
                    result.json_data.get("selector2_cname")
                )
            else:
                logger.error(f"DKIM config failed: {result.json_data.get('error')}")
        
        logger.error(f"Failed to get DKIM config: {result.error or result.output}")
        return False, None, None
    
    async def enable_dkim_with_credentials(
        self,
        admin_email: str,
        admin_password: str,
        domain_name: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Enable DKIM signing for a domain using credentials.
        
        Returns: (success, error_message)
        """
        logger.info(f"Enabling DKIM for {domain_name} with credentials")
        
        escaped_password = admin_password.replace("'", "''")
        
        script = f'''
$ErrorActionPreference = "Stop"

try {{
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
    
    $secpasswd = ConvertTo-SecureString '{escaped_password}' -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential ('{admin_email}', $secpasswd)
    
    Connect-ExchangeOnline -Credential $credential -ShowBanner:$false -ErrorAction Stop
    
    # Enable DKIM
    Set-DkimSigningConfig -Identity "{domain_name}" -Enabled $true -ErrorAction Stop
    
    # Verify it's enabled
    $dkim = Get-DkimSigningConfig -Identity "{domain_name}" -ErrorAction Stop
    
    Write-Output "<<<JSON>>>"
    @{{
        "success" = $dkim.Enabled
        "status" = $dkim.Status
    }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
    
}} catch {{
    Write-Output "<<<JSON>>>"
    @{{
        "success" = $false
        "error" = $_.Exception.Message
    }} | ConvertTo-Json -Compress
    Write-Output "<<<END>>>"
}} finally {{
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}}
'''
        result = await self.run(script, timeout=180)
        
        if result.json_data:
            if result.json_data.get("success"):
                return True, None
            else:
                error_msg = result.json_data.get("error", "Unknown error")
                logger.error(f"DKIM enable failed: {error_msg}")
                return False, error_msg
        
        error_msg = result.error or "Unknown PowerShell error"
        logger.error(f"Failed to enable DKIM: {error_msg}")
        return False, error_msg
    
    # ============================================================
    # GRAPH API METHODS
    # Modern approach using Microsoft Graph API for domain operations
    # ============================================================
    
    async def add_domain_via_graph(
        self,
        access_token: str,
        domain_name: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Add domain to M365 tenant via Microsoft Graph API.
        
        This is the modern approach - more reliable than MSOnline PowerShell.
        
        Args:
            access_token: OAuth access token with Directory.ReadWrite.All scope
            domain_name: Domain to add (e.g., example.com)
        
        Returns:
            (success, txt_verification_value)
        """
        import aiohttp
        
        logger.info(f"Adding domain {domain_name} via Graph API")
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Add domain to tenant
                async with session.post(
                    "https://graph.microsoft.com/v1.0/domains",
                    headers=headers,
                    json={"id": domain_name}
                ) as resp:
                    if resp.status in (200, 201):
                        logger.info(f"Domain {domain_name} added successfully")
                    elif resp.status == 409:
                        # Domain already exists - this is OK
                        logger.info(f"Domain {domain_name} already exists in tenant")
                    else:
                        error = await resp.text()
                        logger.error(f"Failed to add domain: {resp.status} - {error}")
                        return False, None
                
                # Step 2: Get verification DNS records
                async with session.get(
                    f"https://graph.microsoft.com/v1.0/domains/{domain_name}/verificationDnsRecords",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for record in data.get("value", []):
                            if record.get("recordType") == "Txt":
                                txt_value = record.get("text")
                                logger.info(f"Got verification TXT record: {txt_value}")
                                return True, txt_value
                        
                        logger.warning("No TXT verification record found in response")
                        return False, None
                    else:
                        error = await resp.text()
                        logger.error(f"Failed to get verification records: {resp.status} - {error}")
                        return False, None
                        
        except Exception as e:
            logger.exception(f"Graph API add_domain error: {e}")
            return False, None
    
    async def verify_domain_via_graph(
        self,
        access_token: str,
        domain_name: str
    ) -> bool:
        """
        Verify domain ownership in M365 via Graph API.
        
        Args:
            access_token: OAuth access token
            domain_name: Domain to verify
        
        Returns:
            True if domain is verified, False otherwise
        """
        import aiohttp
        
        logger.info(f"Verifying domain {domain_name} via Graph API")
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                # First check if already verified
                async with session.get(
                    f"https://graph.microsoft.com/v1.0/domains/{domain_name}",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("isVerified"):
                            logger.info(f"Domain {domain_name} is already verified")
                            return True
                    elif resp.status == 404:
                        logger.error(f"Domain {domain_name} not found in tenant")
                        return False
                
                # Trigger verification
                async with session.post(
                    f"https://graph.microsoft.com/v1.0/domains/{domain_name}/verify",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("isVerified"):
                            logger.info(f"Domain {domain_name} verified successfully!")
                            return True
                        else:
                            logger.warning(f"Domain verification returned but isVerified=False")
                            return False
                    else:
                        error = await resp.text()
                        logger.error(f"Domain verification failed: {resp.status} - {error}")
                        return False
                        
        except Exception as e:
            logger.exception(f"Graph API verify_domain error: {e}")
            return False
    
    async def get_domain_status_via_graph(
        self,
        access_token: str,
        domain_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get domain status information from Graph API.
        
        Args:
            access_token: OAuth access token
            domain_name: Domain to check
        
        Returns:
            Domain info dict or None
        """
        import aiohttp
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://graph.microsoft.com/v1.0/domains/{domain_name}",
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        logger.error(f"Get domain status failed: {resp.status}")
                        return None
        except Exception as e:
            logger.exception(f"Graph API get_domain_status error: {e}")
            return None
    
    # ============================================================
    # SELENIUM + MFA-ENABLED METHODS
    # Uses Selenium to obtain access token with MFA, then passes to PowerShell
    # ============================================================
    
    async def _get_access_token_via_selenium(
        self,
        admin_email: str,
        admin_password: str,
        totp_secret: str,
        tenant_domain: str = None
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Get OAuth access token via Selenium browser automation with MFA.
        
        This handles the full Microsoft login flow including MFA using stored TOTP secret.
        
        Args:
            admin_email: Admin UPN (e.g., admin@tenant.onmicrosoft.com)
            admin_password: Admin password
            totp_secret: TOTP secret for MFA (base32 encoded)
            tenant_domain: Optional tenant domain (extracted from email if not provided)
        
        Returns:
            tuple: (success, access_token, error_message)
        """
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, NoSuchElementException
        
        logger.info(f"Starting Selenium OAuth flow for {admin_email}")
        
        # Use Azure AD PowerShell app ID (well-known, supports token flow)
        client_id = "1b730954-1685-4b74-9bfd-dac224a7b894"
        redirect_uri = "https://login.microsoftonline.com/common/oauth2/nativeclient"
        scope = "https://graph.microsoft.com/.default"
        
        # Extract tenant from email if not provided
        if not tenant_domain:
            tenant_domain = admin_email.split("@")[1]
        
        auth_url = (
            f"https://login.microsoftonline.com/{tenant_domain}/oauth2/v2.0/authorize"
            f"?client_id={client_id}"
            f"&response_type=token"
            f"&redirect_uri={redirect_uri}"
            f"&scope={scope}"
            f"&response_mode=fragment"
        )
        
        # Chrome options - HEADLESS BY DEFAULT for production
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")  # Headless by default
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        
        # Debug mode - show browser window
        import os
        if os.environ.get("SELENIUM_DEBUG") == "1":
            logger.info("SELENIUM_DEBUG=1: Running browser in visible mode")
            try:
                options.arguments.remove("--headless=new")
            except ValueError:
                pass  # Already removed
        
        driver = None
        try:
            driver = webdriver.Chrome(options=options)
            wait = WebDriverWait(driver, 30)
            
            # Navigate to auth URL
            logger.info(f"Navigating to Azure OAuth URL...")
            driver.get(auth_url)
            time.sleep(2)
            
            # ========== STEP 1: Enter email ==========
            logger.info("Step 1: Entering email...")
            try:
                email_input = wait.until(EC.presence_of_element_located((
                    By.CSS_SELECTOR, 'input[type="email"], input[name="loginfmt"]'
                )))
                email_input.clear()
                email_input.send_keys(admin_email)
                time.sleep(0.5)
                
                # Click Next button
                next_btn = wait.until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR, 'input[type="submit"], button[type="submit"]'
                )))
                next_btn.click()
                time.sleep(2)
                logger.info("Email entered, clicked Next")
                
            except TimeoutException:
                logger.error("Could not find email input field")
                screenshot_path = self._save_debug_screenshot(driver, admin_email, "email_step")
                return False, None, f"Could not find email input field. Screenshot: {screenshot_path}"
            
            # ========== STEP 2: Enter password ==========
            logger.info("Step 2: Entering password...")
            try:
                password_input = wait.until(EC.presence_of_element_located((
                    By.CSS_SELECTOR, 'input[type="password"], input[name="passwd"]'
                )))
                password_input.clear()
                password_input.send_keys(admin_password)
                time.sleep(0.5)
                
                # Click Sign In button
                signin_btn = wait.until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR, 'input[type="submit"], button[type="submit"]'
                )))
                signin_btn.click()
                time.sleep(3)
                logger.info("Password entered, clicked Sign In")
                
            except TimeoutException:
                logger.error("Could not find password input field")
                screenshot_path = self._save_debug_screenshot(driver, admin_email, "password_step")
                return False, None, f"Could not find password input field. Screenshot: {screenshot_path}"
            
            # ========== STEP 2.5: Check for login errors ==========
            logger.info("Checking for login errors...")
            time.sleep(2)  # Wait for error messages to appear
            
            page_source = driver.page_source.lower()
            
            # Check for common error messages
            error_messages = [
                ("password is incorrect", "Wrong password"),
                ("your account or password is incorrect", "Wrong credentials"),
                ("account doesn't exist", "Account not found"),
                ("account has been locked", "Account locked"),
                ("sign-in was blocked", "Sign-in blocked"),
                ("too many failed attempts", "Too many attempts"),
                ("we couldn't find an account", "Account not found"),
                ("this account has been blocked", "Account blocked"),
                ("sign in again or try with different account", "Session expired"),
            ]
            
            for error_text, error_description in error_messages:
                if error_text in page_source:
                    screenshot_path = self._save_debug_screenshot(driver, admin_email, f"login_error_{int(time.time())}")
                    logger.error(f"Login failed for {admin_email}: {error_description}")
                    logger.error(f"Screenshot saved: {screenshot_path}")
                    return False, None, f"Login failed: {error_description}"
            
            # Also check for error elements
            error_selectors = [
                '#passwordError',
                '#usernameError',
                '.alert-error',
                '#errorText',
                'div[data-bind*="error"]',
                '.error-message',
                '#passwordError',
                '#error',
            ]
            
            for selector in error_selectors:
                try:
                    error_elem = driver.find_element(By.CSS_SELECTOR, selector)
                    if error_elem.is_displayed():
                        error_text = error_elem.text
                        if error_text:
                            screenshot_path = self._save_debug_screenshot(driver, admin_email, f"login_error_elem_{int(time.time())}")
                            logger.error(f"Login error element found: {error_text}")
                            return False, None, f"Login failed: {error_text}"
                except NoSuchElementException:
                    continue
                except Exception:
                    continue
            
            logger.info("No login errors detected, proceeding...")
            
            # ========== STEP 3: Handle MFA if required ==========
            time.sleep(2)
            page_text = driver.page_source.lower()
            
            # Check for various MFA prompts
            mfa_indicators = [
                "verify your identity",
                "enter code",
                "authenticator app",
                "verification code",
                "approve sign in",
                "enter the code",
                "two-factor",
                "additional security",
                "use a verification code"
            ]
            
            mfa_detected = any(indicator in page_text for indicator in mfa_indicators)
            
            if mfa_detected:
                logger.info("Step 3: MFA prompt detected, entering TOTP code...")
                
                # Generate TOTP code
                try:
                    totp = pyotp.TOTP(totp_secret)
                    code = totp.now()
                    logger.info(f"Generated TOTP code: {code[:2]}****")
                except Exception as e:
                    logger.error(f"Failed to generate TOTP code: {e}")
                    screenshot_path = self._save_debug_screenshot(driver, admin_email, "totp_error")
                    return False, None, f"Failed to generate TOTP code: {e}"
                
                # Find the code input - try multiple selectors
                code_input = None
                code_selectors = [
                    'input[name="otc"]',
                    'input#idTxtBx_SAOTCC_OTC',
                    'input[type="tel"]',
                    'input[aria-label*="code"]',
                    'input[aria-label*="Code"]',
                    'input[placeholder*="code"]',
                    'input[placeholder*="Code"]',
                    'input[autocomplete="one-time-code"]',
                    'input.form-control',
                ]
                
                for selector in code_selectors:
                    try:
                        elements = driver.find_elements(By.CSS_SELECTOR, selector)
                        for elem in elements:
                            if elem.is_displayed() and elem.is_enabled():
                                code_input = elem
                                logger.info(f"Found MFA input with selector: {selector}")
                                break
                        if code_input:
                            break
                    except NoSuchElementException:
                        continue
                
                if code_input:
                    code_input.clear()
                    code_input.send_keys(code)
                    time.sleep(1)
                    
                    # Click verify button - try multiple selectors
                    verify_selectors = [
                        'input[type="submit"]',
                        'button[type="submit"]',
                        'input#idSubmit_SAOTCC_Continue',
                        'button#idSubmit_SAOTCC_Continue',
                        'input[value="Verify"]',
                        'input[value="verify"]',
                        'button:contains("Verify")',
                    ]
                    
                    clicked = False
                    for selector in verify_selectors:
                        try:
                            buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                            for btn in buttons:
                                if btn.is_displayed() and btn.is_enabled():
                                    btn.click()
                                    clicked = True
                                    logger.info(f"Clicked verify button with selector: {selector}")
                                    break
                            if clicked:
                                break
                        except:
                            continue
                    
                    if not clicked:
                        logger.warning("Could not find verify button, trying Enter key")
                        from selenium.webdriver.common.keys import Keys
                        code_input.send_keys(Keys.RETURN)
                    
                    time.sleep(3)
                else:
                    logger.warning("Could not find MFA code input field")
                    self._save_debug_screenshot(driver, admin_email, "mfa_input_not_found")
            else:
                logger.info("No MFA prompt detected")
            
            # ========== STEP 4: Handle "Stay signed in?" prompt ==========
            time.sleep(2)
            try:
                # Look for "No" button first
                no_selectors = [
                    'input#idBtn_Back',
                    'button#idBtn_Back',
                    'input[value="No"]',
                ]
                for selector in no_selectors:
                    try:
                        no_btn = driver.find_element(By.CSS_SELECTOR, selector)
                        if no_btn.is_displayed():
                            no_btn.click()
                            logger.info("Clicked 'No' on 'Stay signed in?' prompt")
                            time.sleep(2)
                            break
                    except:
                        continue
            except:
                pass
            
            # ========== STEP 5: Extract token from URL ==========
            time.sleep(2)
            current_url = driver.current_url
            logger.info(f"Final URL: {current_url[:100]}...")
            
            if "#access_token=" in current_url:
                # Parse token from URL fragment
                try:
                    fragment = current_url.split("#")[1]
                    params = {}
                    for pair in fragment.split("&"):
                        if "=" in pair:
                            key, value = pair.split("=", 1)
                            params[key] = value
                    
                    import urllib.parse
                    access_token = urllib.parse.unquote(params.get("access_token", ""))
                    
                    if access_token:
                        logger.info(f"Successfully obtained access token ({len(access_token)} chars)")
                        return True, access_token, None
                except Exception as e:
                    logger.error(f"Failed to parse token from URL: {e}")
            
            # Check for errors in URL
            if "error=" in current_url:
                logger.error(f"OAuth error in URL: {current_url}")
                screenshot_path = self._save_debug_screenshot(driver, admin_email, "oauth_error")
                return False, None, f"OAuth error in URL: {current_url}"
            
            logger.error(f"Could not extract token from URL")
            screenshot_path = self._save_debug_screenshot(driver, admin_email, "token_extraction_failed")
            return False, None, f"Could not extract token from URL. Screenshot: {screenshot_path}"
            
        except TimeoutException as e:
            logger.error(f"Timeout during login for {admin_email}: {e}")
            if driver:
                screenshot_path = self._save_debug_screenshot(driver, admin_email, f"timeout_{int(time.time())}")
            return False, None, f"Login timeout: {str(e)}"
            
        except Exception as e:
            logger.error(f"Selenium OAuth error: {str(e)}")
            logger.error(traceback.format_exc())
            if driver:
                screenshot_path = self._save_debug_screenshot(driver, admin_email, "exception")
            return False, None, f"Selenium error: {str(e)}"
            
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
    
    def _save_debug_screenshot(self, driver, admin_email: str, step: str):
        """Save a debug screenshot for troubleshooting."""
        try:
            import os
            username = admin_email.split('@')[0]
            # Use temp directory that works on Windows
            temp_dir = os.environ.get('TEMP', os.environ.get('TMP', '/tmp'))
            screenshot_path = os.path.join(temp_dir, f"mfa_debug_{username}_{step}.png")
            driver.save_screenshot(screenshot_path)
            logger.info(f"Debug screenshot saved: {screenshot_path}")
        except Exception as e:
            logger.warning(f"Could not save debug screenshot: {e}")
    
    async def add_domain_with_mfa(
        self,
        admin_email: str,
        admin_password: str,
        totp_secret: str,
        domain_name: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Add domain to M365 with MFA support using Admin Portal UI automation.
        
        This uses Selenium to automate the M365 Admin Portal directly,
        which is more reliable than the OAuth token approach that gets blocked.
        
        Returns: (success, txt_value)
        """
        logger.info(f"[{domain_name}] add_domain_with_mfa: Using Admin Portal UI automation")
        logger.info(f"[{domain_name}] Params: email={admin_email}, pass_len={len(admin_password) if admin_password else 0}, totp_len={len(totp_secret) if totp_secret else 0}")
        
        # Use Admin Portal automation instead of OAuth tokens
        from app.services.selenium.admin_portal import add_domain_via_admin_portal
        
        try:
            success, txt_value = await add_domain_via_admin_portal(
                admin_email=admin_email,
                admin_password=admin_password,
                totp_secret=totp_secret,
                domain_name=domain_name,
                headless=True  # Headless mode enabled for production
            )
            
            if success and txt_value:
                logger.info(f"[{domain_name}] Domain added via Admin Portal, TXT: {txt_value}")
                return True, txt_value
            else:
                logger.error(f"[{domain_name}] Admin Portal automation failed")
                return False, None
                
        except Exception as e:
            logger.exception(f"[{domain_name}] Admin Portal automation error: {e}")
            return False, None
    
    def setup_domain_complete_with_mfa(
        self,
        admin_email: str,
        admin_password: str,
        totp_secret: str,
        domain_name: str,
        cloudflare_zone_id: str,
        cloudflare_service
    ) -> dict:
        """
        Complete domain setup in a SINGLE browser session with MFA support.
        
        IMPORTANT: This is a SYNCHRONOUS function (not async) because:
        1. Selenium automation is synchronous
        2. This runs in thread pool workers that have their own event loops
        3. Mixing async DB operations with threaded Selenium causes "Future attached to different loop" errors
        
        The flow:
        1. Login (with MFA)
        2. Add domain
        3. Get TXT value
        4. Add TXT to Cloudflare
        5. Wait for DNS propagation
        6. Click Verify on the SAME page
        
        Returns: dict with {success, verified, dns_configured, error}
        """
        logger.info(f"[{domain_name}] setup_domain_complete_with_mfa: Using single-session Admin Portal automation (SYNC)")
        
        from app.services.selenium.admin_portal import setup_domain_complete_via_admin_portal
        
        try:
            # Synchronous call to synchronous function
            result = setup_domain_complete_via_admin_portal(
                domain=domain_name,
                zone_id=cloudflare_zone_id,
                admin_email=admin_email,
                admin_password=admin_password,
                totp_secret=totp_secret
            )
            
            return {
                "success": result.get("success", False),
                "verified": result.get("verified", False),
                "dns_configured": result.get("dns_configured", False),
                "error": result.get("error")
            }
            
        except Exception as e:
            logger.exception(f"[{domain_name}] setup_domain_complete error: {e}")
            return {
                "success": False,
                "verified": False,
                "dns_configured": False,
                "error": str(e)
            }
    
    async def verify_domain_with_mfa(
        self,
        admin_email: str,
        admin_password: str,
        totp_secret: str,
        domain_name: str
    ) -> bool:
        """
        Verify domain in M365 with MFA support using Admin Portal UI automation.
        
        This uses Selenium to automate the M365 Admin Portal directly,
        which is more reliable than the OAuth token approach that gets blocked.
        
        Returns: True if verified
        """
        logger.info(f"[{domain_name}] verify_domain_with_mfa: Using Admin Portal UI automation")
        
        # Use Admin Portal automation instead of OAuth tokens
        from app.services.selenium.admin_portal import verify_domain_via_admin_portal
        
        try:
            success = await verify_domain_via_admin_portal(
                admin_email=admin_email,
                admin_password=admin_password,
                totp_secret=totp_secret,
                domain_name=domain_name,
                headless=True  # Headless mode enabled for production
            )
            
            if success:
                logger.info(f"[{domain_name}] Domain verified via Admin Portal")
                return True
            else:
                logger.error(f"[{domain_name}] Admin Portal domain verification failed")
                return False
                
        except Exception as e:
            logger.exception(f"[{domain_name}] Admin Portal verification error: {e}")
            return False
    
    async def get_dkim_config_with_mfa(
        self,
        admin_email: str,
        admin_password: str,
        totp_secret: str,
        domain_name: str,
        onmicrosoft_domain: str = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Get DKIM configuration with MFA support.
        
        Uses Exchange Admin Center UI automation only.
        
        Returns: (success, selector1_cname, selector2_cname)
        """
        logger.info(f"Getting DKIM config for {domain_name} with MFA support")
        
        # PRIMARY METHOD: Use Exchange Admin Center UI automation
        # This works even when ExchangeOnlineManagement module isn't installed
        logger.info(f"[{domain_name}] Trying Exchange Admin Center UI automation for DKIM config...")
        try:
            from app.services.selenium.admin_portal import get_dkim_config_via_admin_portal
            
            result = await get_dkim_config_via_admin_portal(
                admin_email=admin_email,
                admin_password=admin_password,
                totp_secret=totp_secret,
                domain_name=domain_name,
                onmicrosoft_domain=onmicrosoft_domain,
                headless=True  # Headless mode enabled for production
            )
            
            if result.success and result.selector1_cname and result.selector2_cname:
                logger.info(f"[{domain_name}] Got DKIM config via Exchange Admin Center UI")
                return True, result.selector1_cname, result.selector2_cname
            else:
                logger.warning(f"[{domain_name}] Exchange Admin Center UI failed: {result.error}")
        except Exception as e:
            logger.warning(f"[{domain_name}] Exchange Admin Center UI exception: {e}")
        
        logger.error(f"[{domain_name}] Could not get DKIM config via Exchange Admin Center UI")
        return False, None, None
    
    async def enable_dkim_with_mfa(
        self,
        admin_email: str,
        admin_password: str,
        totp_secret: str,
        domain_name: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Enable DKIM with MFA support.
        
        Uses Exchange Admin Center UI automation only.
        
        Returns: (success, error_message)
        """
        logger.info(f"Enabling DKIM for {domain_name} with MFA support")
        
        # PRIMARY METHOD: Use Exchange Admin Center UI automation
        # This works even when ExchangeOnlineManagement module isn't installed
        logger.info(f"[{domain_name}] Trying Exchange Admin Center UI automation to enable DKIM...")
        try:
            from app.services.selenium.admin_portal import enable_dkim_via_admin_portal
            
            result = await enable_dkim_via_admin_portal(
                admin_email=admin_email,
                admin_password=admin_password,
                totp_secret=totp_secret,
                domain_name=domain_name,
                headless=True  # Headless mode enabled for production
            )
            
            if result.success:
                logger.info(f"[{domain_name}] DKIM enabled via Exchange Admin Center UI")
                return True, None
            else:
                logger.warning(f"[{domain_name}] Exchange Admin Center UI failed: {result.error}")
        except Exception as e:
            logger.warning(f"[{domain_name}] Exchange Admin Center UI exception: {e}")
        
        logger.error(f"[{domain_name}] Could not enable DKIM via Exchange Admin Center UI")
        return False, "Exchange Admin Center UI DKIM enable failed"


# Singleton
powershell = PowerShellRunner()
