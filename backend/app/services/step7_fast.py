"""
Step 7 Fast — Mailbox creation WITHOUT Chrome/Selenium.
Uses ROPC credential auth for both Exchange Online and Microsoft Graph.

Drop-in replacement for azure_step6.run_step6_for_batch().
Each worker is a lightweight PowerShell process (~50MB) instead of Chrome (~400MB),
allowing 25+ parallel workers on 8GB RAM.
"""

import asyncio
import hashlib
import logging
import json
import os
import re
import signal
import time
from typing import Dict, Any, List
from uuid import UUID
from datetime import datetime

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory, BackgroundSessionLocal
from app.models.tenant import Tenant, TenantStatus
from app.models.domain import Domain
from app.models.mailbox import Mailbox, MailboxStatus
from app.models.batch import SetupBatch
from app.services.email_generator import generate_emails_for_domain, MAILBOX_PASSWORD
from app.services.azure_step6 import save_to_db_with_retry, _format_error
from app.core.config import get_settings

logger = logging.getLogger(__name__)

DOMAIN_ERROR_MAX_LENGTH = 1000


def _domain_error_message(error: str | None) -> str | None:
    """Fit diagnostic text into domains.error_message (VARCHAR(1000))."""
    if error is None:
        return None
    return str(error)[:DOMAIN_ERROR_MAX_LENGTH]


def _cgroup_memory_limit_bytes() -> int | None:
    """Return the container memory limit when running under cgroup v1/v2."""
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            raw_value = open(path, encoding="ascii").read().strip()
            if raw_value and raw_value != "max":
                return int(raw_value)
        except (OSError, ValueError):
            continue
    return None


def _powershell_exit_error(
    returncode: int | None,
    stderr: str = "",
    stdout: str = "",
) -> str:
    """Build an actionable error when PowerShell exits without its JSON result."""
    if returncode is not None and returncode < 0:
        signal_number = -returncode
        if signal_number == 9:
            signal_name = "SIGKILL"
        else:
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = f"signal {signal_number}"
        message = f"PowerShell terminated by {signal_name}"
        if signal_number == 9:
            message += " (likely container OOM kill)"
    elif returncode not in (None, 0):
        message = f"PowerShell exited with code {returncode}"
    else:
        message = "PowerShell completed without a JSON result"

    detail = (stderr or stdout).strip()
    if detail:
        message += f": {detail[-2000:]}"
    return message


def _powershell_result_error(result: Dict[str, Any]) -> str:
    """Extract the most useful error detail from a PowerShell result."""
    details: List[str] = []

    if result.get("error"):
        details.append(str(result["error"]))

    result_errors = _as_list(result.get("errors"))
    if result_errors:
        details.append("; ".join(result_errors[:10]))

    if not details and result.get("stderr"):
        details.append(str(result["stderr"]).strip()[-2000:])
    if not details and result.get("stdout"):
        details.append(str(result["stdout"]).strip()[-2000:])

    return " | ".join(detail for detail in details if detail) or "Unknown PowerShell error"


def _mailbox_objective_complete(expected: int, ready: int) -> bool:
    """A domain is complete only when every expected mailbox is objectively ready."""
    return expected > 0 and ready == expected


def _effective_step7_parallel(configured: int, memory_limit: int | None) -> int:
    """Cap Exchange session concurrency according to the container memory limit."""
    parallel = max(1, min(int(configured or 1), 5))
    if memory_limit and memory_limit < 2 * 1024**3:
        return 1
    return parallel


def _is_transient_license_error(error: str) -> bool:
    """Return whether Microsoft is likely to succeed after propagation/backoff."""
    normalized = (error or "").lower()
    transient_markers = (
        "resource_notfound",
        "resource '",
        "queried reference-property",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "service unavailable",
        "internal server error",
        "too many requests",
        "throttl",
        "connection",
        "invalid usage location",
    )
    return any(marker in normalized for marker in transient_markers)


def _ps_escape(value: str) -> str:
    """Escape string for PowerShell double-quoted strings."""
    if value is None:
        return ""
    return value.replace("`", "``").replace('"', '`"').replace("'", "''")


def _mailbox_password_for_local_part(local_part: str, default_password: str) -> str:
    """
    Microsoft rejects passwords containing the username. Single-letter aliases
    make the shared default unsafe, so choose a deterministic alternate.
    """
    default_password = default_password or MAILBOX_PASSWORD
    local = (local_part or "").strip().lower()
    if not local or local not in default_password.lower():
        return default_password

    fixed = "#QwXz9874!"
    if local not in fixed.lower():
        return fixed

    available = [ch for ch in "QwZyVkTbPrLs" if ch.lower() not in set(local)]
    letters = "".join(available[:6]) or "QwZyVk"
    return f"#{letters}9874!"


async def _run_powershell(script: str, timeout: int = 300) -> Dict[str, Any]:
    """
    Run a PowerShell script and return parsed JSON output.
    Sets environment variables to disable WAM/broker auth (forces ROPC).
    """
    env = os.environ.copy()
    env["MSAL_FORCE_BROKER_DISABLED"] = "true"
    env["MSAL_DISABLE_WAM"] = "true"
    env["EXO_DISABLE_WAM"] = "true"

    import tempfile as _tf
    with _tf.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False, dir=_tf.gettempdir()) as _f:
        _f.write(script)
        _script_path = _f.name
    try:
        process_kwargs = {}
        if os.name != "nt":
            process_kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            "pwsh", "-NoProfile", "-NonInteractive", "-File", _script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **process_kwargs,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if os.name != "nt":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            await proc.communicate()
            return {
                "success": False,
                "error": f"PowerShell script timed out after {timeout} seconds",
                "returncode": proc.returncode,
            }
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if stderr_text:
            logger.warning("PS stderr (exit %s): %s", proc.returncode, stderr_text[-2000:])
        # Try to parse JSON from stdout (last JSON object wins)
        for line in reversed(stdout_text.split("\n")):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                result = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            result.setdefault("returncode", proc.returncode)
            if proc.returncode not in (None, 0):
                result["success"] = False
                result.setdefault(
                    "error",
                    _powershell_exit_error(proc.returncode, stderr_text, stdout_text),
                )
            else:
                result.setdefault("success", True)
            return result

        error = _powershell_exit_error(proc.returncode, stderr_text, stdout_text)
        logger.error("%s", error)
        return {
            "success": False,
            "error": error,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "returncode": proc.returncode,
        }
    finally:
        try:
            os.unlink(_script_path)
        except Exception:
            pass


def _mail_nickname_for_domain(domain: str) -> str:
    """
    Generate a tenant-unique mailNickname for the per-domain licensed user.

    `mailNickname`/alias has to be unique across the tenant, so every domain
    cannot safely use plain "me1" even though the UPN is me1@{domain}.
    """
    clean = re.sub(r"[^a-z0-9-]+", "-", (domain or "").lower()).strip("-")
    if not clean:
        clean = "domain"
    nickname = f"me1-{clean}"
    if len(nickname) <= 64:
        return nickname
    digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[:10]
    return f"{nickname[:53].rstrip('-')}-{digest}"


def _as_list(value: Any) -> List[str]:
    """Normalize PowerShell JSON scalar/list output into a Python string list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str) and value:
        return [value]
    return []


def _build_licensed_user_script(
    escaped_email: str,
    escaped_password: str,
    domain: str,
    mailbox_password: str,
) -> str:
    """Build idempotent Graph script that ensures me1@domain exists and is licensed."""
    target_upn = f"me1@{domain}"
    escaped_target_upn = _ps_escape(target_upn)
    escaped_mail_nickname = _ps_escape(_mail_nickname_for_domain(domain))
    escaped_mailbox_password = _ps_escape(mailbox_password)

    return f'''
$ErrorActionPreference = "Stop"
try {{
    Import-Module Microsoft.Graph.Users -ErrorAction Stop
    Import-Module Microsoft.Graph.Users.Actions -ErrorAction Stop
    Import-Module Microsoft.Graph.Identity.DirectoryManagement -ErrorAction SilentlyContinue

    $body2 = @{{
        grant_type = "password"
        client_id = "1b730954-1685-4b74-9bfd-dac224a7b894"
        scope = "https://graph.microsoft.com/.default"
        username = "{escaped_email}"
        password = "{escaped_password}"
    }}
    $td = "{escaped_email}".Split("@")[1]
    $tok2 = Invoke-RestMethod -Method Post -Uri "https://login.microsoftonline.com/$td/oauth2/v2.0/token" -Body $body2 -ErrorAction Stop
    $sec2 = ConvertTo-SecureString $tok2.access_token -AsPlainText -Force
    Connect-MgGraph -AccessToken $sec2 -NoWelcome -ErrorAction Stop

    $targetUpn = "{escaped_target_upn}"
    $userId = $null
    $wasCreated = $false

    try {{
        $existing = Get-MgUser -UserId $targetUpn -ErrorAction Stop
        $userId = $existing.Id
    }} catch {{
        $passwordProfile = @{{
            Password = "{escaped_mailbox_password}"
            ForceChangePasswordNextSignIn = $false
        }}

        try {{
            $newUser = New-MgUser `
                -DisplayName "me1" `
                -MailNickname "{escaped_mail_nickname}" `
                -UserPrincipalName $targetUpn `
                -PasswordProfile $passwordProfile `
                -AccountEnabled:$true `
                -ErrorAction Stop
            $userId = $newUser.Id
            $wasCreated = $true
        }} catch {{
            if ($_.Exception.Message -like "*already exists*") {{
                $fallback = Get-MgUser -UserId $targetUpn -ErrorAction Stop
                $userId = $fallback.Id
            }} else {{
                throw
            }}
        }}
    }}

    $businessBasicSkuPartNumbers = @(
        "O365_BUSINESS_ESSENTIALS",
        "SMB_BUSINESS_ESSENTIALS",
        "Microsoft_365_Business_Basic_(no Teams)",
        "Microsoft_365_Business_Basic_(no_Teams)",
        "Microsoft_365_Business_Basic_EEA_(no_Teams)",
        "Microsoft_365_Business_Basic_EEA_(no Teams)"
    )
    # SPB is Microsoft 365 Business Premium, including trial subscriptions. It
    # provides the Exchange mailbox required by this workflow and is commonly
    # the only available SKU on newly provisioned reseller tenants.
    $businessPremiumSkuPartNumbers = @(
        "SPB",
        "O365_BUSINESS_PREMIUM"
    )
    $allowedSkuPartNumbers = @(
        $businessBasicSkuPartNumbers +
        $businessPremiumSkuPartNumbers +
        @("EXCHANGESTANDARD")
    )
    $allSkus = Get-MgSubscribedSku -ErrorAction Stop
    $allowedSkus = @($allSkus | Where-Object {{
        ($allowedSkuPartNumbers -contains $_.SkuPartNumber) -and
        ($_.AppliesTo -eq "User")
    }})

    $userWithLic = Get-MgUser -UserId $userId -Property "AssignedLicenses" -ErrorAction Stop
    $assignedSkuIds = @($userWithLic.AssignedLicenses | ForEach-Object {{ [string]$_.SkuId }})
    $existingAllowedSku = $allowedSkus | Where-Object {{ $assignedSkuIds -contains ([string]$_.SkuId) }} | Select-Object -First 1
    $hasAllowedLicense = $null -ne $existingAllowedSku
    $licenseAction = "already_licensed"
    $licenseSku = if ($existingAllowedSku) {{ $existingAllowedSku.SkuPartNumber }} else {{ $null }}

    if (-not $hasAllowedLicense) {{
        $usageLocation = $env:M365_USAGE_LOCATION
        if ([string]::IsNullOrWhiteSpace($usageLocation)) {{
            try {{
                $org = Get-MgOrganization -Property "CountryLetterCode" -ErrorAction SilentlyContinue | Select-Object -First 1
                $usageLocation = $org.CountryLetterCode
            }} catch {{}}
        }}
        if ([string]::IsNullOrWhiteSpace($usageLocation)) {{
            $usageLocation = "US"
        }}
        $usageLocation = $usageLocation.Trim().ToUpperInvariant()

        $userForLicense = Get-MgUser -UserId $userId -Property "UsageLocation" -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($userForLicense.UsageLocation) -or $userForLicense.UsageLocation.Length -ne 2) {{
            Update-MgUser -UserId $userId -UsageLocation $usageLocation -ErrorAction Stop
        }}

        $sku = $allowedSkus | Where-Object {{
            ($_.PrepaidUnits.Enabled -gt 0) -and
            ($_.ConsumedUnits -lt $_.PrepaidUnits.Enabled)
        }} | Sort-Object @{{ Expression = {{ if ($businessBasicSkuPartNumbers -contains $_.SkuPartNumber) {{ 0 }} else {{ 1 }} }} }} | Select-Object -First 1

        if ($sku) {{
            # Use Graph REST directly. Some Microsoft.Graph.Users.Actions
            # versions silently coerce the hashtable passed to
            # Set-MgUserLicense into an empty addLicenses array.
            $assignBody = @{{
                addLicenses = @(@{{ skuId = [string]$sku.SkuId }})
                removeLicenses = @()
            }} | ConvertTo-Json -Depth 5
            $assignHeaders = @{{
                Authorization = "Bearer $($tok2.access_token)"
                "Content-Type" = "application/json"
            }}
            Invoke-RestMethod `
                -Method Post `
                -Uri "https://graph.microsoft.com/v1.0/users/$userId/assignLicense" `
                -Headers $assignHeaders `
                -Body $assignBody `
                -ErrorAction Stop | Out-Null
            $hasAllowedLicense = $true
            $licenseAction = "assigned"
            $licenseSku = $sku.SkuPartNumber
        }} else {{
            $seenSkus = @($allSkus | ForEach-Object {{
                "$($_.SkuPartNumber):$($_.ConsumedUnits)/$($_.PrepaidUnits.Enabled)"
            }}) -join ", "
            throw "No available Business Basic, Business Premium (SPB, including trial), or Exchange Online Plan 1 license with a free seat in this tenant. Seen SKUs consumed/enabled: $seenSkus"
        }}
    }}

    @{{ success=$true; email=$targetUpn; user_id=$userId; was_created=$wasCreated; has_license=$hasAllowedLicense; license_action=$licenseAction; license_sku=$licenseSku }} | ConvertTo-Json -Compress
    Disconnect-MgGraph -ErrorAction SilentlyContinue
}} catch {{
    $detail = $_.ErrorDetails.Message
    $msg = $_.Exception.Message
    if (-not [string]::IsNullOrWhiteSpace($detail)) {{
        $msg = "$msg :: $detail"
    }}
    @{{ success=$false; error=$msg }} | ConvertTo-Json -Compress
    Disconnect-MgGraph -ErrorAction SilentlyContinue
}}
'''


async def ensure_licensed_user_for_domain(
    domain_name: str,
    domain_id: UUID,
    admin_email: str,
    admin_password: str,
    mailbox_password: str = MAILBOX_PASSWORD,
    save_to_domain: bool = True,
) -> Dict[str, Any]:
    """
    Ensure the per-domain licensed user exists in Microsoft 365 and has one of
    an allowed Exchange-capable SKU. This intentionally verifies Graph every time instead
    of trusting cached DB flags.
    """
    domain = (domain_name or "").strip().lower()
    if not domain or domain.endswith(".onmicrosoft.com"):
        return {"success": False, "domain": domain_name, "error": "onmicrosoft domains are not eligible"}

    script = _build_licensed_user_script(
        escaped_email=_ps_escape(admin_email),
        escaped_password=_ps_escape(admin_password),
        domain=domain,
        mailbox_password=mailbox_password,
    )
    settings = get_settings()
    max_attempts = max(1, int(settings.step7_license_max_attempts or 3))
    timeout = max(120, int(settings.step7_license_timeout_seconds or 300))
    result: Dict[str, Any] = {}

    for attempt in range(1, max_attempts + 1):
        result = await _run_powershell(script, timeout=timeout)
        if result.get("success"):
            break

        error = _powershell_result_error(result)
        if attempt >= max_attempts or not _is_transient_license_error(error):
            break

        delay = 15 * attempt
        logger.warning(
            "[%s] Licensed user provisioning attempt %s/%s failed transiently: %s. "
            "Retrying in %ss",
            domain,
            attempt,
            max_attempts,
            error,
            delay,
        )
        await asyncio.sleep(delay)

    if not result.get("success"):
        return {
            "success": False,
            "domain": domain,
            "email": f"me1@{domain}",
            "error": _powershell_result_error(result),
        }

    result["domain"] = domain
    result["email"] = result.get("email") or f"me1@{domain}"

    if save_to_domain and domain_id:
        async def _save_licensed_user(db):
            d = await db.get(Domain, domain_id)
            if d:
                d.licensed_user_created = True
                d.licensed_user_upn = result["email"]
                d.licensed_user_password = mailbox_password
                d.licensed_user_id = result.get("user_id")

        await save_to_db_with_retry(_save_licensed_user, description=f"{domain} licensed user save")

    return result


def build_expected_mailbox_data(
    domain: str,
    display_name: str,
    batch_data: Dict[str, Any] = None,
    mailboxes_per_tenant: int = 50,
    mailbox_password: str = MAILBOX_PASSWORD,
) -> List[Dict[str, str]]:
    """
    Build the authoritative mailbox list for a domain from batch config.

    This is intentionally count/list based, not DB-state based: if stale DB rows
    exist, they do not define completion.
    """
    domain = (domain or "").strip().lower()
    custom_emails_for_domain = None
    if batch_data and batch_data.get("custom_mailbox_map"):
        custom_emails_for_domain = batch_data["custom_mailbox_map"].get(domain)

    if custom_emails_for_domain:
        mailbox_data = []
        for entry in custom_emails_for_domain:
            email = (entry.get("email") or "").strip().lower()
            if not email or "@" not in email:
                continue
            local_part = email.split("@", 1)[0]
            mailbox_data.append(
                {
                    "email": email,
                    "local_part": local_part,
                    "display_name": (entry.get("display_name") or "").strip() or display_name,
                    "password": _mailbox_password_for_local_part(
                        local_part,
                        (entry.get("password") or "").strip() or mailbox_password,
                    ),
                }
            )
    else:
        mailbox_data = generate_emails_for_domain(
            display_name=display_name,
            domain=domain,
            count=mailboxes_per_tenant,
        )

    deduped: List[Dict[str, str]] = []
    seen = set()
    for mb in mailbox_data:
        email = (mb.get("email") or "").strip().lower()
        if not email or email in seen:
            continue
        local_part = mb.get("local_part") or email.split("@", 1)[0]
        password = _mailbox_password_for_local_part(
            local_part,
            mb.get("password") or mailbox_password,
        )
        seen.add(email)
        deduped.append(
            {
                "email": email,
                "local_part": local_part,
                "display_name": mb.get("display_name") or display_name,
                "password": password,
            }
        )
    return deduped


def _clear_mailbox_evidence(mailbox):
    for field in ("created_in_exchange", "display_name_fixed", "account_enabled",
                  "password_set", "upn_fixed", "delegated", "setup_complete"):
        setattr(mailbox, field, False)
    mailbox.setup_completed_at = None
    mailbox.status = MailboxStatus.PENDING


async def _prepare_expected_mailboxes(db, domain_id, tenant_id, batch_id, desired_mailboxes):
    """Reuse globally unique addresses after a verified domain changes tenant."""
    domain = await db.scalar(select(Domain).where(Domain.id == domain_id).with_for_update())
    if not domain or domain.tenant_id != tenant_id or domain.batch_id != batch_id or not domain.domain_verified_in_m365:
        raise RuntimeError("Mailbox destination must match the domain's verified tenant and batch")
    desired = {item["email"].lower(): item for item in desired_mailboxes}
    if not desired or any(email.rsplit("@", 1)[-1] != domain.name.lower() for email in desired):
        raise RuntimeError("Expected mailbox addresses must belong to the verified domain")
    rows = list((await db.execute(select(Mailbox).where(
        func.lower(Mailbox.email).in_(list(desired))
    ).with_for_update())).scalars().all())
    existing = {row.email.lower(): row for row in rows}
    inserted = 0
    for email, item in desired.items():
        row = existing.get(email)
        if row is None:
            row = Mailbox(email=email, local_part=item["local_part"], display_name=item["display_name"],
                password=item["password"], tenant_id=tenant_id, batch_id=batch_id,
                status=MailboxStatus.PENDING, warmup_stage="none")
            db.add(row)
            inserted += 1
        elif row.tenant_id != tenant_id:
            # Phase 1 has already verified/created the licensed user in the destination.
            # These are local records; no mailbox is deleted from the previous tenant.
            row.tenant_id = tenant_id
            _clear_mailbox_evidence(row)
            row.microsoft_object_id = None
            row.upn = None
            row.created_at_exchange = None
            row.photo_set = False
            row.uploaded_to_sequencer = False
            row.uploaded_at = None
            row.sequencer_name = None
            row.instantly_uploaded = False
            row.instantly_uploaded_at = None
            row.smartlead_uploaded = False
            row.smartlead_uploaded_at = None
            row.warmup_stage = "none"
        row.batch_id = batch_id
        row.display_name = item["display_name"]
        row.password = item["password"]
        row.initial_password = item["password"]
        row.error_message = None
    await db.commit()
    return inserted


async def process_domain_fast(
    domain_name: str,
    domain_id: UUID,
    tenant_id: UUID,
    admin_email: str,
    admin_password: str,
    display_name: str,
    batch_id: UUID,
    batch_data: Dict[str, Any] = None,
    domain_index: int = 0,
    mailboxes_per_tenant: int = 50,
    persona_first_name: str = None,
    persona_last_name: str = None,
) -> Dict[str, Any]:
    """
    Process a single domain: create licensed user, generate mailboxes, create in Exchange,
    set passwords — ALL via PowerShell/ROPC, ZERO Chrome.

    This is the fast replacement for run_step6_for_tenant in azure_step6.py.
    """
    domain = (domain_name or "").strip().lower()

    # Resolve effective persona: domain-level overrides batch-level,
    # which overrides the legacy display_name parameter.
    eff_first = (persona_first_name or "").strip()
    eff_last = (persona_last_name or "").strip()
    if not eff_first and batch_data:
        eff_first = (batch_data.get("persona_first_name") or "").strip()
    if not eff_last and batch_data:
        eff_last = (batch_data.get("persona_last_name") or "").strip()
    effective_display_name = f"{eff_first} {eff_last}".strip()
    if not effective_display_name:
        # Fallback to legacy display_name parameter
        effective_display_name = (display_name or "").strip()
    if not effective_display_name:
        logger.error("[%s] No persona name available (domain, batch, or legacy)", domain)
        return {
            "success": False,
            "domain": domain_name,
            "error": "No persona name available (not set on domain or batch)",
            "elapsed_seconds": 0,
        }

    display_name = effective_display_name
    first_name, last_name = (
        display_name.rsplit(None, 1) if " " in display_name else (display_name, "")
    )
    escaped_email = _ps_escape(admin_email)
    escaped_password = _ps_escape(admin_password)
    mailbox_password = MAILBOX_PASSWORD
    # Each domain gets its own mailbox range within the tenant:
    # domain_index=0 → 1..N, domain_index=1 → N+1..2N, etc.
    mailbox_start_index = domain_index * mailboxes_per_tenant + 1

    logger.info("[%s] === FAST PROCESSING START (no Chrome) ===", domain)
    start_time = time.time()

    try:
        # ================================================================
        # PHASE 1: Connect + Create Licensed User (all PowerShell, ~20 sec)
        # ================================================================
        logger.info("[%s] Phase 1: Connect + Licensed User via Graph API", domain)

        license_result = await ensure_licensed_user_for_domain(
            domain_name=domain,
            domain_id=domain_id,
            admin_email=admin_email,
            admin_password=admin_password,
            mailbox_password=mailbox_password,
            save_to_domain=True,
        )
        if not license_result.get("success"):
            raise Exception(
                f"Licensed user creation/licensing failed: {license_result.get('error', 'Unknown')}"
            )

        licensed_user_upn = license_result.get("email") or f"me1@{domain}"
        logger.info(
            "[%s] Licensed user ready: %s action=%s sku=%s created=%s (%.1fs)",
            domain,
            licensed_user_upn,
            license_result.get("license_action"),
            license_result.get("license_sku"),
            license_result.get("was_created"),
            time.time() - start_time,
        )

        # ================================================================
        # PHASE 2: Generate emails + save to DB (~1 sec)
        # ================================================================
        logger.info("[%s] Phase 2: Generate emails", domain)

        desired_mailboxes = build_expected_mailbox_data(
            domain=domain,
            display_name=display_name,
            batch_data=batch_data,
            mailboxes_per_tenant=mailboxes_per_tenant,
            mailbox_password=mailbox_password,
        )
        desired_emails = [mb["email"] for mb in desired_mailboxes]
        desired_password_by_email = {
            mb["email"].lower(): mb["password"] or mailbox_password
            for mb in desired_mailboxes
        }
        if not desired_emails:
            raise Exception("No expected mailboxes could be generated")

        async with BackgroundSessionLocal() as db:
            inserted_count = await _prepare_expected_mailboxes(
                db, domain_id, tenant_id, batch_id, desired_mailboxes
            )

        logger.info(
            "[%s] Expected mailbox rows: %s total, %s inserted (%.1fs)",
            domain,
            len(desired_mailboxes),
            inserted_count,
            time.time() - start_time,
        )

        # Reload only the authoritative mailbox set from DB.
        async with BackgroundSessionLocal() as db:
            result = await db.execute(
                select(Mailbox).where(
                    Mailbox.tenant_id == tenant_id,
                    Mailbox.email.in_(desired_emails),
                )
            )
            mailboxes = result.scalars().all()
            mailboxes_by_email = {mb.email.lower(): mb for mb in mailboxes}
            mailbox_list = []
            for desired in desired_mailboxes:
                mb = mailboxes_by_email.get(desired["email"].lower())
                mailbox_list.append(
                    {
                        "email": desired["email"],
                        "display_name": (mb.display_name if mb else None) or desired["display_name"],
                        "password": desired["password"] or (mb.password if mb else None) or mailbox_password,
                    }
                )

        if not mailbox_list:
            raise Exception("No mailboxes found after generation")

        # ================================================================
        # PHASE 3: Create mailboxes + delegate + set passwords
        #          ALL in one PowerShell session (~12-15 min)
        # ================================================================
        logger.info(
            "[%s] Phase 3: PowerShell mailbox creation (ROPC auth, no browser; can be silent up to 30 min)",
            domain,
        )

        base_display_name = display_name

        # Build the mailbox data as a PowerShell array
        mailbox_entries = []
        for i, mb in enumerate(mailbox_list):
            mailbox_entries.append(
                f'    @{{ Email="{_ps_escape(mb["email"])}"; '
                f'DisplayName="{_ps_escape(mb["display_name"])}"; '
                f'Password="{_ps_escape(mb["password"])}"; '
                f'Index={mailbox_start_index + i} }}'
            )
        mailbox_array = ",\n".join(mailbox_entries)

        # ONE PowerShell script that does EVERYTHING: connect, create, delegate, passwords
        master_script = _build_master_script(
            escaped_email=escaped_email,
            escaped_password=escaped_password,
            domain=domain,
            base_display_name=base_display_name,
            mailbox_array=mailbox_array,
        )

        settings = get_settings()
        ps_timeout = max(
            300,
            int(settings.step7_fast_powershell_timeout_seconds or 3600),
        )
        ps_result = await _run_powershell(master_script, timeout=ps_timeout)

        if not ps_result.get("success"):
            raise Exception(
                f"Mailbox PowerShell failed: {_powershell_result_error(ps_result)}"
            )

        created = ps_result.get("created", 0)
        create_requested = ps_result.get("create_requested", 0)
        delegated = ps_result.get("delegated", 0)
        passwords_set = ps_result.get("passwords_set", 0)
        upns_fixed = ps_result.get("upns_fixed", 0)
        ps_errors = ps_result.get("errors", [])
        created_emails = _as_list(ps_result.get("created_emails"))
        delegated_emails = _as_list(ps_result.get("delegated_emails"))
        password_emails = _as_list(ps_result.get("password_emails"))
        upn_emails = _as_list(ps_result.get("upn_emails"))

        if ps_errors:
            logger.warning("[%s] PowerShell errors: %s", domain, "; ".join(str(e) for e in ps_errors[:5]))

        logger.info(
            "[%s] PowerShell results: requested=%s, created=%s, delegated=%s, passwords=%s, upns=%s",
            domain, create_requested, created, delegated, passwords_set, upns_fixed,
        )

        # ================================================================
        # PHASE 4: Save results + completion check
        # ================================================================
        step6_complete = False
        ready_count = 0
        completion_error = None
        _domain_id = domain_id  # capture for closure

        async def _save_results(db):
            nonlocal step6_complete, ready_count, completion_error

            # Previous-tenant or previous-run flags are not current evidence.
            await db.execute(update(Mailbox).where(
                Mailbox.tenant_id == tenant_id, Mailbox.email.in_(desired_emails)
            ).values(created_in_exchange=False, display_name_fixed=False, delegated=False,
                password_set=False, account_enabled=False, upn_fixed=False,
                setup_complete=False, setup_completed_at=None, status=MailboxStatus.PENDING))
            # Apply only results confirmed during this PowerShell run.
            if created_emails:
                await db.execute(
                    update(Mailbox)
                    .where(Mailbox.tenant_id == tenant_id, Mailbox.email.in_(created_emails))
                    .values(created_in_exchange=True, display_name_fixed=True)
                )
            if delegated_emails:
                await db.execute(
                    update(Mailbox)
                    .where(Mailbox.tenant_id == tenant_id, Mailbox.email.in_(delegated_emails))
                    .values(delegated=True)
                )
            for email in password_emails:
                await db.execute(
                    update(Mailbox)
                    .where(Mailbox.tenant_id == tenant_id, Mailbox.email == email)
                    .values(
                        password_set=True,
                        account_enabled=True,
                        password=desired_password_by_email.get(email.lower(), mailbox_password),
                    )
                )
            if upn_emails:
                await db.execute(
                    update(Mailbox)
                    .where(Mailbox.tenant_id == tenant_id, Mailbox.email.in_(upn_emails))
                    .values(upn_fixed=True)
                )

            await db.execute(
                update(Mailbox)
                .where(
                    Mailbox.tenant_id == tenant_id,
                    Mailbox.email.in_(desired_emails),
                    Mailbox.created_in_exchange == True,
                    Mailbox.account_enabled == True,
                    Mailbox.password_set == True,
                    Mailbox.upn_fixed == True,
                    Mailbox.delegated == True,
                )
                .values(
                    setup_complete=True,
                    setup_completed_at=datetime.utcnow(),
                    status=MailboxStatus.READY,
                    error_message=None,
                )
            )

            await db.flush()

            total = len(desired_emails)
            ready_count = await db.scalar(
                select(func.count(Mailbox.id)).where(
                    Mailbox.tenant_id == tenant_id,
                    Mailbox.email.in_(desired_emails),
                    Mailbox.created_in_exchange == True,
                    Mailbox.account_enabled == True,
                    Mailbox.password_set == True,
                    Mailbox.upn_fixed == True,
                    Mailbox.delegated == True,
                    Mailbox.setup_complete == True,
                )
            ) or 0
            step6_complete = _mailbox_objective_complete(total, ready_count)

            if not step6_complete:
                metrics = (
                    f"ready({ready_count}/{total}), "
                    f"created({created}/{total}), "
                    f"delegated({delegated}/{total}), "
                    f"passwords({passwords_set}/{total}), "
                    f"upns({upns_fixed}/{total})"
                )
                ps_detail = "; ".join(str(error) for error in ps_errors[:5])
                completion_error = f"Incomplete mailbox objective: {metrics}"
                if ps_detail:
                    completion_error += f". PowerShell: {ps_detail}"

            # Update domain record
            d = await db.get(Domain, _domain_id)
            if d:
                d.step6_complete = step6_complete
                d.step6_skipped = False
                d.step6_mailboxes_created = ready_count
                d.error_message = _domain_error_message(
                    None if step6_complete else completion_error
                )

            await db.flush()

            # Tenant readiness is derived from all of its linked domains.
            incomplete_tenant_domains = await db.scalar(
                select(func.count(Domain.id)).where(
                    Domain.tenant_id == tenant_id,
                    Domain.step6_complete.is_not(True),
                )
            ) or 0

            t = await db.get(Tenant, tenant_id)
            if t:
                t.step6_mailboxes_created = created
                t.step6_delegations_done = delegated
                t.step6_passwords_set = passwords_set
                t.step6_upns_fixed = upns_fixed
                t.step6_complete = incomplete_tenant_domains == 0
                if t.step6_complete:
                    t.step6_completed_at = datetime.utcnow()
                    t.status = TenantStatus.READY
                    t.step6_error = None
                else:
                    t.step6_completed_at = None
                    t.step6_error = completion_error or (
                        f"{incomplete_tenant_domains} linked domain(s) still "
                        "require mailbox completion"
                    )

        await save_to_db_with_retry(_save_results, description=f"{domain} results save")

        elapsed = time.time() - start_time
        logger.info(
            "[%s] === FAST PROCESSING %s (%.1f min) ===",
            domain,
            "COMPLETE" if step6_complete else "INCOMPLETE",
            elapsed / 60,
        )

        return {
            "success": step6_complete,
            "domain": domain,
            "created": created,
            "delegated": delegated,
            "passwords_set": passwords_set,
            "upns_fixed": upns_fixed,
            "ready": ready_count,
            "expected": len(desired_emails),
            "error": completion_error,
            "elapsed_seconds": elapsed,
        }

    except Exception as exc:
        elapsed = time.time() - start_time
        error_msg = _format_error(exc)
        logger.error("[%s] FAST PROCESSING FAILED (%.1fs): %s", domain, elapsed, error_msg)

        try:
            async def _save_error(db):
                t = await db.get(Tenant, tenant_id)
                if t:
                    t.step6_error = error_msg
                    t.step6_complete = False
                d = await db.get(Domain, domain_id)
                if d:
                    d.step6_complete = False
                    d.step6_skipped = False
                    d.error_message = _domain_error_message(error_msg)

            await save_to_db_with_retry(_save_error, description=f"{domain} error save")
        except Exception:
            pass

        return {"success": False, "domain": domain, "error": error_msg, "elapsed_seconds": elapsed}


def _build_master_script(
    escaped_email: str,
    escaped_password: str,
    domain: str,
    base_display_name: str,
    mailbox_array: str,
) -> str:
    """
    Build the master PowerShell script that does ALL mailbox operations
    in a single process: EXO connect, create, fix names, delegate, then
    Graph connect, set passwords, fix UPNs.
    """
    escaped_display = _ps_escape(base_display_name)

    return f'''
$ErrorActionPreference = "Continue"
$results = @{{
    created=0
    create_requested=0
    delegated=0
    passwords_set=0
    upns_fixed=0
    created_emails=@()
    delegated_emails=@()
    password_emails=@()
    upn_emails=@()
    errors=@()
}}

# === CONNECT TO EXCHANGE ONLINE (ROPC — no browser) ===
try {{
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
    $sp = ConvertTo-SecureString "{escaped_password}" -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential("{escaped_email}", $sp)
    Connect-ExchangeOnline -Credential $cred -ShowBanner:$false -ErrorAction Stop
    Write-Host "EXO_CONNECTED"
}} catch {{
    $results.errors += "EXO connect failed: $($_.Exception.Message)"
    $results | ConvertTo-Json -Compress
    exit 1
}}

# === MAILBOX DATA ===
$mailboxes = @(
{mailbox_array}
)

$licensedUser = "me1@{domain}"

function Get-StableMailboxAlias([string]$Email) {{
    $normalized = $Email.ToLowerInvariant()
    $clean = (($normalized -replace "@", "-at-") -replace "[^a-z0-9-]", "-").Trim("-")
    if ([string]::IsNullOrWhiteSpace($clean)) {{
        $clean = "shared-mailbox"
    }}
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
    $hash = [System.BitConverter]::ToString($sha1.ComputeHash($bytes)).Replace("-", "").Substring(0, 8).ToLowerInvariant()
    if ($clean.Length -gt 54) {{
        $clean = $clean.Substring(0, 54).Trim("-")
    }}
    return "$clean-$hash"
}}

function Get-StableMailboxName([object]$Mailbox) {{
    $alias = Get-StableMailboxAlias $Mailbox.Email
    return "shared-$alias"
}}

# === STEP 1: CREATE SHARED MAILBOXES ===
Write-Host "STEP1_CREATE"
foreach ($mb in $mailboxes) {{
    try {{
        $existing = Get-Mailbox -Identity $mb.Email -ErrorAction SilentlyContinue
        if ($existing) {{
            $results.create_requested++
        }} else {{
            $tempName = Get-StableMailboxName $mb
            $alias = Get-StableMailboxAlias $mb.Email
            New-Mailbox -Shared -Name $tempName -Alias $alias -DisplayName "{escaped_display}" -PrimarySmtpAddress $mb.Email -ErrorAction Stop | Out-Null
            $results.create_requested++
        }}
    }} catch {{
        $errMsg = $_.Exception.Message
        if ($errMsg -like "*already being used*" -or $errMsg -like "*already exists*") {{
            $afterConflict = Get-Mailbox -Identity $mb.Email -ErrorAction SilentlyContinue
            if ($afterConflict) {{
                $results.create_requested++
            }} else {{
                $results.errors += "Create failed: $($mb.Email): $errMsg"
            }}
        }} else {{
            $results.errors += "Create failed: $($mb.Email): $errMsg"
        }}
    }}
    Start-Sleep -Milliseconds 200
}}

# Wait until Exchange can actually resolve the mailbox objects. New-Mailbox can
# return before permissions or Graph user operations can see the object.
Write-Host "STEP1_WAIT_VISIBLE"
$pending = @{{}}
foreach ($mb in $mailboxes) {{
    $pending[$mb.Email.ToLowerInvariant()] = $mb.Email
}}
$visibleLookup = @{{}}
$waitDeadline = (Get-Date).AddSeconds(480)
do {{
    foreach ($mb in $mailboxes) {{
        $key = $mb.Email.ToLowerInvariant()
        if (-not $pending.ContainsKey($key)) {{
            continue
        }}
        try {{
            $visible = Get-Mailbox -Identity $mb.Email -ErrorAction SilentlyContinue
            if ($visible) {{
                $visibleLookup[$key] = $mb.Email
                [void]$pending.Remove($key)
            }}
        }} catch {{}}
        Start-Sleep -Milliseconds 100
    }}
    if ($pending.Count -gt 0 -and (Get-Date) -lt $waitDeadline) {{
        Start-Sleep -Seconds 15
    }}
}} while ($pending.Count -gt 0 -and (Get-Date) -lt $waitDeadline)

foreach ($email in $visibleLookup.Values) {{
    $results.created++
    $results.created_emails += $email
}}
if ($pending.Count -gt 0) {{
    $results.errors += "Mailbox not visible after wait: $($pending.Values -join ', ')"
}}

$readyMailboxes = @()
foreach ($mb in $mailboxes) {{
    if ($visibleLookup.ContainsKey($mb.Email.ToLowerInvariant())) {{
        $readyMailboxes += $mb
    }}
}}

# === STEP 2: FIX DISPLAY NAMES ===
Write-Host "STEP2_NAMES"
foreach ($mb in $readyMailboxes) {{
    try {{
        Set-Mailbox -Identity $mb.Email -DisplayName "{escaped_display}" -ErrorAction SilentlyContinue
    }} catch {{}}
    Start-Sleep -Milliseconds 100
}}

# === STEP 3: DELEGATE ===
Write-Host "STEP3_DELEGATE"
foreach ($mb in $readyMailboxes) {{
    $delegateSucceeded = $false
    $lastDelegateErrors = @()
    for ($attempt = 1; $attempt -le 6 -and -not $delegateSucceeded; $attempt++) {{
        $delegateErrors = @()
        try {{
            Add-MailboxPermission -Identity $mb.Email -User $licensedUser -AccessRights FullAccess -AutoMapping $true -ErrorAction Stop | Out-Null
        }} catch {{
            if ($_.Exception.Message -notlike "*already*") {{
                $delegateErrors += "FullAccess: $($_.Exception.Message)"
            }}
        }}
        try {{
            Add-RecipientPermission -Identity $mb.Email -Trustee $licensedUser -AccessRights SendAs -Confirm:$false -ErrorAction Stop | Out-Null
        }} catch {{
            if ($_.Exception.Message -notlike "*already*") {{
                $delegateErrors += "SendAs: $($_.Exception.Message)"
            }}
        }}

        if ($delegateErrors.Count -eq 0) {{
            $results.delegated++
            $results.delegated_emails += $mb.Email
            $delegateSucceeded = $true
        }} else {{
            $lastDelegateErrors = $delegateErrors
            if ($attempt -lt 6) {{
                Start-Sleep -Seconds 15
            }}
        }}
    }}

    if (-not $delegateSucceeded) {{
        $results.errors += "Delegate failed: $($mb.Email): $($lastDelegateErrors -join '; ')"
    }}
    Start-Sleep -Milliseconds 100
}}

# === STEP 4: FIX UPNs via Exchange ===
Write-Host "STEP4_UPNS"
foreach ($mb in $readyMailboxes) {{
    try {{
        Set-Mailbox -Identity $mb.Email -MicrosoftOnlineServicesID $mb.Email -ErrorAction Stop
        $results.upns_fixed++
        $results.upn_emails += $mb.Email
    }} catch {{
        $results.errors += "UPN fix failed: $($mb.Email): $($_.Exception.Message)"
    }}
    Start-Sleep -Milliseconds 100
}}

Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue

# === STEP 5: GRAPH — Enable accounts + set passwords ===
Write-Host "STEP5_GRAPH"
try {{
    Import-Module Microsoft.Graph.Users -ErrorAction Stop
    $body = @{{
        grant_type = "password"
        client_id = "1b730954-1685-4b74-9bfd-dac224a7b894"
        scope = "https://graph.microsoft.com/.default"
        username = "{escaped_email}"
        password = "{escaped_password}"
    }}
    $tenantDomain = "{escaped_email}".Split("@")[1]
    $tok = Invoke-RestMethod -Method Post -Uri "https://login.microsoftonline.com/$tenantDomain/oauth2/v2.0/token" -Body $body -ErrorAction Stop
    $sec = ConvertTo-SecureString $tok.access_token -AsPlainText -Force
    Connect-MgGraph -AccessToken $sec -NoWelcome -ErrorAction Stop

    foreach ($mb in $readyMailboxes) {{
        try {{
            $user = $null
            for ($attempt = 1; $attempt -le 12 -and -not $user; $attempt++) {{
                try {{
                    $user = Get-MgUser -UserId $mb.Email -ErrorAction Stop
                }} catch {{
                    $user = Get-MgUser -Filter "mail eq '$($mb.Email)'" -ErrorAction SilentlyContinue
                }}
                if (-not $user) {{
                    $user = Get-MgUser -Filter "userPrincipalName eq '$($mb.Email)'" -ErrorAction SilentlyContinue
                }}
                if (-not $user -and $attempt -lt 12) {{
                    Start-Sleep -Seconds 10
                }}
            }}

            if ($user) {{
                $params = @{{
                    AccountEnabled = $true
                    PasswordProfile = @{{
                        Password = $mb.Password
                        ForceChangePasswordNextSignIn = $false
                    }}
                }}
                Update-MgUser -UserId $user.Id -BodyParameter $params -ErrorAction Stop
                $results.passwords_set++
                $results.password_emails += $mb.Email
            }} else {{
                $results.errors += "Graph user not found: $($mb.Email)"
            }}
        }} catch {{
            $results.errors += "Graph failed: $($mb.Email): $($_.Exception.Message)"
        }}
        Start-Sleep -Milliseconds 200
    }}

    Disconnect-MgGraph -ErrorAction SilentlyContinue
}} catch {{
    $results.errors += "Graph connect failed: $($_.Exception.Message)"
}}

Write-Host "COMPLETE"
$results | ConvertTo-Json -Compress
'''


async def run_step7_fast(batch_id: UUID, display_name: str) -> Dict[str, Any]:
    """
    Fast Step 7: Process all eligible domains using ROPC auth (no Chrome).
    Drop-in replacement for run_step6_for_batch in azure_step6.py.
    """
    logger.info("=== STEP 7 FAST MODE (no Chrome) for batch %s ===", batch_id)

    # Collect work items
    domain_work_items = []
    batch_data = None

    async with async_session_factory() as db:
        batch = await db.get(SetupBatch, batch_id)
        if not batch:
            return {"success": False, "error": "Batch not found"}

        # Capture batch data for custom mailbox map support
        batch_data = {
            "persona_first_name": batch.persona_first_name,
            "persona_last_name": batch.persona_last_name,
            "custom_mailbox_map": batch.custom_mailbox_map,
            "mailboxes_per_tenant": batch.mailboxes_per_tenant or 50,
        }

        result = await db.execute(
            select(Domain)
            .join(Tenant, Domain.tenant_id == Tenant.id)
            .where(
                Domain.batch_id == batch_id,
                Tenant.batch_id == batch_id,
                Domain.step5_complete == True,
                Domain.domain_verified_in_m365 == True,
                Domain.dkim_enabled == True,
                Domain.dmarc_configured == True,
                Domain.step6_complete.is_not(True),
                Domain.step6_skipped.is_not(True),
            )
            .order_by(Tenant.created_at, Domain.domain_index_in_tenant)
        )
        domains = result.scalars().all()

        for d in domains:
            tenant = await db.get(Tenant, d.tenant_id)
            if not tenant:
                continue
            if not tenant.admin_email or not tenant.admin_password:
                logger.warning("[%s] Skipping — missing admin credentials", d.name)
                continue
            domain_work_items.append({
                "domain_name": d.name,
                "domain_id": d.id,
                "tenant_id": d.tenant_id,
                "admin_email": tenant.admin_email,
                "admin_password": tenant.admin_password,
                "domain_index": d.domain_index_in_tenant or 0,
                "mailboxes_per_tenant": batch.mailboxes_per_tenant or 50,
                "persona_first_name": d.persona_first_name,
                "persona_last_name": d.persona_last_name,
            })

    total = len(domain_work_items)
    logger.info("Step 7 Fast: %s eligible domains", total)

    if total == 0:
        return {"success": True, "message": "No eligible domains", "total": 0, "successful": 0, "failed": 0}

    # Process with semaphore. Fast mode avoids Chrome, but Phase 3 opens
    # Exchange Online PowerShell sessions, so keep concurrency conservative.
    settings = get_settings()
    memory_limit = _cgroup_memory_limit_bytes()
    configured_parallel = int(getattr(settings, "step7_fast_parallel", 1) or 1)
    max_parallel = _effective_step7_parallel(configured_parallel, memory_limit)
    if (
        memory_limit
        and memory_limit < 2 * 1024**3
        and configured_parallel > 1
    ):
        logger.warning(
            "Step 7 Fast: forcing max_parallel=1 because container memory limit is %.2f GB",
            memory_limit / 1024**3,
        )
    semaphore = asyncio.Semaphore(max_parallel)

    logger.info(
        "Processing %s domains with max_parallel=%s (FAST MODE — no Chrome; Exchange sessions capped)",
        total,
        max_parallel,
    )

    successful = 0
    failed = 0

    async def _process_one(idx: int, item: Dict):
        nonlocal successful, failed
        async with semaphore:
            logger.info(
                "BATCH PROGRESS: Domain %s/%s - %s", idx, total, item["domain_name"]
            )
            result = await process_domain_fast(
                domain_name=item["domain_name"],
                domain_id=item["domain_id"],
                tenant_id=item["tenant_id"],
                admin_email=item["admin_email"],
                admin_password=item["admin_password"],
                display_name=display_name,
                batch_id=batch_id,
                batch_data=batch_data,
                domain_index=item["domain_index"],
                mailboxes_per_tenant=item["mailboxes_per_tenant"],
                persona_first_name=item.get("persona_first_name"),
                persona_last_name=item.get("persona_last_name"),
            )
            if result.get("success"):
                successful += 1
            else:
                failed += 1
            return result

    tasks = [_process_one(i, item) for i, item in enumerate(domain_work_items, 1)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Count any exceptions that weren't caught
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            logger.error("Domain task exception: %s", _format_error(r))

    logger.info(
        "=== STEP 7 FAST COMPLETE: %s/%s successful, %s failed ===",
        successful, total, failed,
    )

    return {"success": failed == 0, "total": total, "successful": successful, "failed": failed}
