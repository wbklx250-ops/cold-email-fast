"""
PowerShell Module Setup - Ensures required modules are installed.
Runs automatically on first use or app startup.

This enables production deployment without manual intervention - modules
are installed automatically when the system first starts.
"""

import subprocess
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Required PowerShell modules for M365 operations
REQUIRED_MODULES = [
    "MSOnline",
    "ExchangeOnlineManagement",
    "Microsoft.Graph.Authentication",
    "Microsoft.Graph.Users",
]

# Auto-detect PowerShell path based on OS
if sys.platform == "win32":
    PWSH_PATH = os.environ.get("PWSH_PATH", "powershell.exe")
else:
    PWSH_PATH = os.environ.get("PWSH_PATH", "/usr/bin/pwsh")

# Track if modules have been verified this session
_modules_verified = False


def ensure_powershell_modules() -> bool:
    """
    Ensure all required PowerShell modules are installed.
    
    Call this once at startup. It will:
    1. Check if each required module is installed
    2. Install missing modules automatically
    3. Use -Scope CurrentUser to avoid needing admin rights
    
    Returns:
        True if all modules are available, False if any failed to install
    """
    global _modules_verified
    
    if _modules_verified:
        return True
    
    logger.info("Checking PowerShell modules...")
    
    all_success = True
    for module in REQUIRED_MODULES:
        if not _is_module_installed(module):
            logger.info(f"Installing PowerShell module: {module}")
            if not _install_module(module):
                logger.error(f"Failed to install module: {module}")
                all_success = False
            else:
                logger.info(f"Successfully installed: {module}")
        else:
            logger.info(f"Module already installed: {module}")
    
    if all_success:
        _modules_verified = True
        logger.info("All PowerShell modules verified and ready")
    
    return all_success


def _is_module_installed(module_name: str) -> bool:
    """
    Check if a PowerShell module is installed.
    
    Args:
        module_name: Name of the module to check
        
    Returns:
        True if module is installed, False otherwise
    """
    script = f'Get-Module -ListAvailable -Name {module_name} | Select-Object -First 1'
    
    try:
        result = subprocess.run(
            [PWSH_PATH, "-ExecutionPolicy", "Bypass", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            encoding='utf-8',
            errors='replace'
        )
        
        # Module is installed if its name appears in the output
        return module_name.lower() in result.stdout.lower()
        
    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout checking module {module_name}")
        return False
    except Exception as e:
        logger.warning(f"Error checking module {module_name}: {e}")
        return False


def _install_module(module_name: str) -> bool:
    """
    Install a PowerShell module from PSGallery.
    
    Uses -Scope CurrentUser to avoid needing administrator rights.
    Sets TLS 1.2 and trusts PSGallery for automated installation.
    
    Args:
        module_name: Name of the module to install
        
    Returns:
        True if installation succeeded, False otherwise
    """
    # PowerShell script to install module
    script = f'''
# Enable TLS 1.2 for PSGallery (required on older systems)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Trust PSGallery to avoid prompts
$null = Set-PSRepository -Name PSGallery -InstallationPolicy Trusted -ErrorAction SilentlyContinue

# Install the module
try {{
    Install-Module -Name {module_name} -Force -AllowClobber -Scope CurrentUser -ErrorAction Stop
    Write-Output "MODULE_INSTALLED_SUCCESSFULLY"
}} catch {{
    Write-Error "Installation failed: $($_.Exception.Message)"
    exit 1
}}
'''
    
    try:
        result = subprocess.run(
            [PWSH_PATH, "-ExecutionPolicy", "Bypass", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=300,  # Module installation can take several minutes
            encoding='utf-8',
            errors='replace'
        )
        
        if "MODULE_INSTALLED_SUCCESSFULLY" in result.stdout:
            return True
        
        # Log detailed error information
        logger.error(f"Module install stdout: {result.stdout}")
        logger.error(f"Module install stderr: {result.stderr}")
        logger.error(f"Module install return code: {result.returncode}")
        return False
        
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout installing module {module_name} (took >300s)")
        return False
    except Exception as e:
        logger.error(f"Exception installing module {module_name}: {e}")
        return False


def get_module_status() -> dict:
    """
    Get the installation status of all required modules.
    
    Returns:
        Dict with module names as keys and installation status as values
    """
    status = {}
    for module in REQUIRED_MODULES:
        status[module] = _is_module_installed(module)
    return status


def check_powershell_available() -> bool:
    """
    Check if PowerShell is available on this system.
    
    Returns:
        True if PowerShell is available, False otherwise
    """
    try:
        result = subprocess.run(
            [PWSH_PATH, "-NoProfile", "-Command", "echo 'PowerShell OK'"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return "PowerShell OK" in result.stdout
    except Exception as e:
        logger.error(f"PowerShell not available: {e}")
        return False


# Export for easy importing
__all__ = [
    'ensure_powershell_modules',
    'get_module_status',
    'check_powershell_available',
    'REQUIRED_MODULES'
]