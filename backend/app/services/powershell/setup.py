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

# Keep the SDK modules on one release: mixed Authentication dependencies load
# different assemblies with the same name into the same PowerShell process.
GRAPH_MODULE_VERSION = "2.41.0"

# Required PowerShell modules for current M365 operations
REQUIRED_MODULES = [
    "ExchangeOnlineManagement",
    "Microsoft.Graph.Authentication",
    "Microsoft.Graph.Users",
    "Microsoft.Graph.Users.Actions",
    "Microsoft.Graph.Identity.DirectoryManagement",
]

# Legacy/fallback code paths still reference MSOnline, but PSGallery may no
# longer provide it in newer Linux containers. Do not fail startup if it is
# unavailable; those specific legacy paths will report their own error if used.
OPTIONAL_MODULES = [
    "MSOnline",
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

    for module in OPTIONAL_MODULES:
        if not _is_module_installed(module):
            logger.info(f"Installing optional legacy PowerShell module: {module}")
            if not _install_module(module):
                logger.warning(
                    "Optional legacy PowerShell module unavailable: %s. "
                    "Current Graph/Exchange automation can continue.",
                    module,
                )
            else:
                logger.info(f"Successfully installed optional module: {module}")
        else:
            logger.info(f"Optional module already installed: {module}")
    
    if all_success:
        all_success = _verify_graph_imports()
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
    version_filter = (
        f" | Where-Object {{ $_.Version -eq [version]'{GRAPH_MODULE_VERSION}' }}"
        if module_name.startswith("Microsoft.Graph.") else ""
    )
    # Emit a stable marker: PowerShell's formatted tables truncate long names
    # such as Microsoft.Graph.Identity.DirectoryManagement in captured stdout.
    script = (
        f'$module = Get-Module -ListAvailable -Name {module_name}{version_filter} | Select-Object -First 1; '
        'if ($module) { Write-Output "MODULE_AVAILABLE" }'
    )
    
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
        return result.returncode == 0 and "MODULE_AVAILABLE" in result.stdout
        
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
    required_version = (
        f"-RequiredVersion {GRAPH_MODULE_VERSION}"
        if module_name.startswith("Microsoft.Graph.") else ""
    )
    script = f'''
# Enable TLS 1.2 for PSGallery (required on older systems)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Trust PSGallery to avoid prompts
$null = Set-PSRepository -Name PSGallery -InstallationPolicy Trusted -ErrorAction SilentlyContinue

# Install the module
try {{
    Install-Module -Name {module_name} {required_version} -Force -AllowClobber -Scope CurrentUser -ErrorAction Stop
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


def _verify_graph_imports() -> bool:
    """Actually load the SDK together; installed files alone do not prove readiness."""
    script = "$ErrorActionPreference = 'Stop'; " + "; ".join(
        f"Import-Module {name} -RequiredVersion {GRAPH_MODULE_VERSION} -ErrorAction Stop"
        for name in REQUIRED_MODULES if name.startswith("Microsoft.Graph.")
    )
    try:
        result = subprocess.run(
            [PWSH_PATH, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            logger.error("Graph SDK import verification failed: %s", result.stderr[-2000:])
        return result.returncode == 0
    except Exception as exc:
        logger.error("Graph SDK import verification failed: %s", exc)
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
