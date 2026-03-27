"""
Step 7 Fast — Mailbox creation WITHOUT Chrome/Selenium.
Uses ROPC credential auth for both Exchange Online and Microsoft Graph.

Drop-in replacement for azure_step6.run_step6_for_batch().
Each worker is a lightweight PowerShell process (~50MB) instead of Chrome (~400MB),
allowing 25+ parallel workers on 8GB RAM.
"""

import asyncio
import logging
import json
import os
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


def _ps_escape(value: str) -> str:
    """Escape string for PowerShell double-quoted strings."""
    if value is None:
        return ""
    return value.replace("`", "``").replace('"', '`"').replace("'", "''")


async def _run_powershell(script: str, timeout: int = 300) -> Dict[str, Any]:
    """
    Run a PowerShell script and return parsed JSON output.
    Sets environment variables to disable WAM/broker auth (forces ROPC).
    """
    env = os.environ.copy()
    env["MSAL_FORCE_BROKER_DISABLED"] = "true"
    env["MSAL_DISABLE_WAM"] = "true"
    env["EXO_DISABLE_WAM"] = "true"

    proc = await asyncio.create_subprocess_exec(
        "pwsh", "-NoProfile", "-NonInteractive", "-Command", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return {"success": False, "error": "PowerShell script timed out"}

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if stderr_text:
        logger.debug("PS stderr: %s", stderr_text[:500])

    # Try to parse JSON from stdout (last JSON object wins)
    try:
        for line in reversed(stdout_text.split("\n")):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        pass

    return {
        "success": proc.returncode == 0,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "returncode": proc.returncode,
    }


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
) -> Dict[str, Any]:
    """
    Process a single domain: create licensed user, generate mailboxes, create in Exchange,
    set passwords — ALL via PowerShell/ROPC, ZERO Chrome.

    This is the fast replacement for run_step6_for_tenant in azure_step6.py.
    """
    domain = domain_name
    first_name, last_name = (
        display_name.strip().split(" ", 1) if " " in display_name else (display_name, "")
    )
    escaped_email = _ps_escape(admin_email)
    escaped_password = _ps_escape(admin_password)
    mailbox_password = MAILBOX_PASSWORD
    mailbox_start_index = domain_index * 50 + 1

    logger.info("[%s] === FAST PROCESSING START (no Chrome) ===", domain)
    start_time = time.time()

    try:
        # ================================================================
        # PHASE 1: Connect + Create Licensed User (all PowerShell, ~20 sec)
        # ================================================================
        logger.info("[%s] Phase 1: Connect + Licensed User via Graph API", domain)

        licensed_user_upn = None
        async with BackgroundSessionLocal() as db:
            # Check domain-level first, then tenant-level
            d = await db.get(Domain, domain_id)
            if d and d.licensed_user_created and d.licensed_user_upn:
                licensed_user_upn = d.licensed_user_upn
                logger.info("[%s] Licensed user already exists (domain): %s", domain, licensed_user_upn)
            else:
                t = await db.get(Tenant, tenant_id)
                if t and t.licensed_user_created and t.licensed_user_upn:
                    licensed_user_upn = t.licensed_user_upn
                    logger.info("[%s] Licensed user already exists (tenant): %s", domain, licensed_user_upn)

        if not licensed_user_upn:
            create_user_script = f'''
$ErrorActionPreference = "Stop"
try {{
    Import-Module Microsoft.Graph.Users -ErrorAction Stop
    Import-Module Microsoft.Graph.Users.Actions -ErrorAction SilentlyContinue

    $sp = ConvertTo-SecureString "{escaped_password}" -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential("{escaped_email}", $sp)
    Connect-MgGraph -Credential $cred -NoWelcome -ErrorAction Stop

    # Check if user already exists
    $existing = Get-MgUser -Filter "userPrincipalName eq ''me1@{domain}''" -ErrorAction SilentlyContinue
    if ($existing) {{
        @{{ success=$true; email="me1@{domain}"; action="already_exists" }} | ConvertTo-Json -Compress
        Disconnect-MgGraph -ErrorAction SilentlyContinue
        exit 0
    }}

    # Create user with license
    $passwordProfile = @{{
        Password = "{_ps_escape(mailbox_password)}"
        ForceChangePasswordNextSignIn = $false
    }}

    $newUser = New-MgUser -DisplayName "me1" -MailNickname "me1" -UserPrincipalName "me1@{domain}" -PasswordProfile $passwordProfile -AccountEnabled -ErrorAction Stop

    # Get available license SKU
    $skus = Get-MgSubscribedSku -ErrorAction Stop
    $sku = $skus | Where-Object {{ $_.SkuPartNumber -like "*EXCHANGE*" -or $_.SkuPartNumber -like "*BUSINESS*" -or $_.SkuPartNumber -like "*ENTERPRISE*" }} | Select-Object -First 1

    if ($sku) {{
        Set-MgUserLicense -UserId $newUser.Id -AddLicenses @(@{{SkuId=$sku.SkuId}}) -RemoveLicenses @() -ErrorAction Stop
    }}

    @{{ success=$true; email="me1@{domain}"; user_id=$newUser.Id; action="created" }} | ConvertTo-Json -Compress
    Disconnect-MgGraph -ErrorAction SilentlyContinue
}} catch {{
    @{{ success=$false; error=$_.Exception.Message }} | ConvertTo-Json -Compress
    Disconnect-MgGraph -ErrorAction SilentlyContinue
}}
'''
            result = await _run_powershell(create_user_script, timeout=120)

            if not result.get("success"):
                raise Exception(
                    f"Licensed user creation failed: {result.get('error', result.get('stderr', 'Unknown'))}"
                )

            licensed_user_upn = f"me1@{domain}"

            # Save to DB (both tenant and domain records)
            async def _save_licensed_user(db):
                t = await db.get(Tenant, tenant_id)
                if t:
                    t.licensed_user_created = True
                    t.licensed_user_upn = licensed_user_upn
                    t.licensed_user_password = mailbox_password
                d = await db.get(Domain, domain_id)
                if d:
                    d.licensed_user_created = True
                    d.licensed_user_upn = licensed_user_upn
                    d.licensed_user_password = mailbox_password

            await save_to_db_with_retry(_save_licensed_user, description=f"{domain} licensed user save")
            logger.info(
                "[%s] Licensed user created: %s (%.1fs)", domain, licensed_user_upn, time.time() - start_time
            )

        # ================================================================
        # PHASE 2: Generate emails + save to DB (~1 sec)
        # ================================================================
        logger.info("[%s] Phase 2: Generate emails", domain)

        async with BackgroundSessionLocal() as db:
            existing_count = (
                await db.scalar(
                    select(func.count(Mailbox.id)).where(
                        Mailbox.tenant_id == tenant_id,
                        Mailbox.email.like(f"%@{domain}"),
                    )
                )
                or 0
            )

        if existing_count >= 50:
            logger.info("[%s] Mailboxes already generated (%s exist)", domain, existing_count)
        else:
            # Check for custom mailbox map (CSV-imported emails)
            custom_emails_for_domain = None
            if batch_data and batch_data.get("custom_mailbox_map"):
                custom_emails_for_domain = batch_data["custom_mailbox_map"].get(domain.lower())

            if custom_emails_for_domain:
                logger.info("[%s] Using %d custom email addresses from CSV", domain, len(custom_emails_for_domain))
                mailbox_data = []
                for entry in custom_emails_for_domain:
                    email = entry.get("email", "").strip().lower()
                    if not email or "@" not in email:
                        continue
                    local_part = email.split("@")[0]
                    dn = entry.get("display_name", "").strip() or display_name
                    pw = entry.get("password", "").strip() or mailbox_password
                    mailbox_data.append({"email": email, "local_part": local_part, "display_name": dn, "password": pw})
                if not mailbox_data:
                    mailbox_data = generate_emails_for_domain(display_name=display_name, domain=domain, count=50)
            else:
                mailbox_data = generate_emails_for_domain(display_name=display_name, domain=domain, count=50)

            async with BackgroundSessionLocal() as gen_db:
                for mb in mailbox_data:
                    mailbox = Mailbox(
                        email=mb["email"],
                        local_part=mb["local_part"],
                        display_name=mb["display_name"],
                        password=mb["password"],
                        tenant_id=tenant_id,
                        batch_id=batch_id,
                        status=MailboxStatus.PENDING,
                        warmup_stage="none",
                    )
                    gen_db.add(mailbox)
                await gen_db.commit()

            logger.info("[%s] Generated %s mailboxes (%.1fs)", domain, len(mailbox_data), time.time() - start_time)

        # Reload mailboxes from DB
        async with BackgroundSessionLocal() as db:
            result = await db.execute(
                select(Mailbox).where(Mailbox.tenant_id == tenant_id, Mailbox.email.like(f"%@{domain}"))
            )
            mailboxes = result.scalars().all()
            mailbox_list = [
                {"email": mb.email, "display_name": mb.display_name, "password": mb.password or mailbox_password}
                for mb in mailboxes
            ]

        if not mailbox_list:
            raise Exception("No mailboxes found after generation")

        # ================================================================
        # PHASE 3: Create mailboxes + delegate + set passwords
        #          ALL in one PowerShell session (~12-15 min)
        # ================================================================
        logger.info("[%s] Phase 3: PowerShell mailbox creation (ROPC auth, no browser)", domain)

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
        mailbox_array = ",`n".join(mailbox_entries)

        # ONE PowerShell script that does EVERYTHING: connect, create, delegate, passwords
        master_script = _build_master_script(
            escaped_email=escaped_email,
            escaped_password=escaped_password,
            domain=domain,
            base_display_name=base_display_name,
            mailbox_array=mailbox_array,
        )

        ps_result = await _run_powershell(master_script, timeout=900)  # 15 min timeout

        created = ps_result.get("created", 0)
        delegated = ps_result.get("delegated", 0)
        passwords_set = ps_result.get("passwords_set", 0)
        upns_fixed = ps_result.get("upns_fixed", 0)
        ps_errors = ps_result.get("errors", [])

        if ps_errors:
            logger.warning("[%s] PowerShell errors: %s", domain, "; ".join(str(e) for e in ps_errors[:5]))

        logger.info(
            "[%s] PowerShell results: created=%s, delegated=%s, passwords=%s, upns=%s",
            domain, created, delegated, passwords_set, upns_fixed,
        )

        # ================================================================
        # PHASE 4: Save results + completion check
        # ================================================================
        step6_complete = False
        _domain_id = domain_id  # capture for closure

        async def _save_results(db):
            nonlocal step6_complete

            # Update mailbox records
            if created > 0:
                await db.execute(
                    update(Mailbox)
                    .where(Mailbox.tenant_id == tenant_id, Mailbox.email.like(f"%@{domain}"))
                    .values(created_in_exchange=True, display_name_fixed=True)
                )
            if delegated > 0:
                await db.execute(
                    update(Mailbox)
                    .where(Mailbox.tenant_id == tenant_id, Mailbox.email.like(f"%@{domain}"))
                    .values(delegated=True)
                )
            if passwords_set > 0:
                await db.execute(
                    update(Mailbox)
                    .where(Mailbox.tenant_id == tenant_id, Mailbox.email.like(f"%@{domain}"))
                    .values(password_set=True, account_enabled=True, password=mailbox_password)
                )
            if upns_fixed > 0:
                await db.execute(
                    update(Mailbox)
                    .where(Mailbox.tenant_id == tenant_id, Mailbox.email.like(f"%@{domain}"))
                    .values(upn_fixed=True)
                )

            # Update tenant counters
            t = await db.get(Tenant, tenant_id)
            if t:
                t.step6_mailboxes_created = created
                t.step6_delegations_done = delegated
                t.step6_passwords_set = passwords_set
                t.step6_upns_fixed = upns_fixed

                # Completion check (90% threshold — same as existing code)
                total = len(mailbox_list)
                threshold = total * 0.9
                if created >= threshold and delegated >= threshold and passwords_set >= threshold:
                    t.step6_complete = True
                    t.step6_completed_at = datetime.utcnow()
                    t.status = TenantStatus.READY
                    t.step6_error = None
                    step6_complete = True
                else:
                    missing = []
                    if created < threshold:
                        missing.append(f"mailboxes({created}/{total})")
                    if delegated < threshold:
                        missing.append(f"delegation({delegated}/{total})")
                    if passwords_set < threshold:
                        missing.append(f"passwords({passwords_set}/{total})")
                    t.step6_error = f"Incomplete - missing: {', '.join(missing)}"

            # Update domain record
            d = await db.get(Domain, _domain_id)
            if d:
                d.step6_complete = step6_complete
                d.step6_mailboxes_created = created

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
$results = @{{ created=0; delegated=0; passwords_set=0; upns_fixed=0; errors=@() }}

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

# === STEP 1: CREATE SHARED MAILBOXES ===
Write-Host "STEP1_CREATE"
foreach ($mb in $mailboxes) {{
    try {{
        $existing = Get-Mailbox -Identity $mb.Email -ErrorAction SilentlyContinue
        if ($existing) {{
            $results.created++
        }} else {{
            $tempName = "$("{escaped_display}") $($mb.Index)"
            New-Mailbox -Shared -Name $tempName -DisplayName $tempName -PrimarySmtpAddress $mb.Email -ErrorAction Stop | Out-Null
            $results.created++
        }}
    }} catch {{
        $errMsg = $_.Exception.Message
        if ($errMsg -like "*already being used*" -or $errMsg -like "*already exists*") {{
            $results.created++
        }} else {{
            $results.errors += "Create failed: $($mb.Email): $errMsg"
        }}
    }}
    Start-Sleep -Milliseconds 200
}}

# Wait for provisioning
Start-Sleep -Seconds 10

# === STEP 2: FIX DISPLAY NAMES ===
Write-Host "STEP2_NAMES"
foreach ($mb in $mailboxes) {{
    try {{
        Set-Mailbox -Identity $mb.Email -DisplayName "{escaped_display}" -Name "{escaped_display}" -ErrorAction SilentlyContinue
    }} catch {{}}
    Start-Sleep -Milliseconds 100
}}

# === STEP 3: DELEGATE ===
Write-Host "STEP3_DELEGATE"
foreach ($mb in $mailboxes) {{
    try {{
        Add-MailboxPermission -Identity $mb.Email -User $licensedUser -AccessRights FullAccess -AutoMapping $true -ErrorAction SilentlyContinue | Out-Null
        Add-RecipientPermission -Identity $mb.Email -Trustee $licensedUser -AccessRights SendAs -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        $results.delegated++
    }} catch {{
        $results.errors += "Delegate failed: $($mb.Email): $($_.Exception.Message)"
    }}
    Start-Sleep -Milliseconds 100
}}

# === STEP 4: FIX UPNs via Exchange ===
Write-Host "STEP4_UPNS"
foreach ($mb in $mailboxes) {{
    try {{
        Set-Mailbox -Identity $mb.Email -MicrosoftOnlineServicesID $mb.Email -ErrorAction SilentlyContinue
        $results.upns_fixed++
    }} catch {{}}
    Start-Sleep -Milliseconds 100
}}

Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue

# === STEP 5: GRAPH — Enable accounts + set passwords ===
Write-Host "STEP5_GRAPH"
try {{
    Import-Module Microsoft.Graph.Users -ErrorAction Stop
    $sp2 = ConvertTo-SecureString "{escaped_password}" -AsPlainText -Force
    $cred2 = New-Object System.Management.Automation.PSCredential("{escaped_email}", $sp2)
    Connect-MgGraph -Credential $cred2 -NoWelcome -ErrorAction Stop

    foreach ($mb in $mailboxes) {{
        try {{
            $user = Get-MgUser -Filter "mail eq ''$($mb.Email)''" -ErrorAction SilentlyContinue
            if (-not $user) {{
                $user = Get-MgUser -Filter "userPrincipalName eq ''$($mb.Email)''" -ErrorAction SilentlyContinue
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
        }

        result = await db.execute(
            select(Domain)
            .join(Tenant, Domain.tenant_id == Tenant.id)
            .where(
                Tenant.batch_id == batch_id,
                Domain.domain_verified_in_m365 == True,
                Domain.dkim_enabled == True,
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
            })

    total = len(domain_work_items)
    logger.info("Step 7 Fast: %s eligible domains", total)

    if total == 0:
        return {"success": True, "message": "No eligible domains", "total": 0, "successful": 0, "failed": 0}

    # Process with semaphore — can go MUCH higher without Chrome
    settings = get_settings()
    max_parallel = int(settings.max_parallel_browsers) if hasattr(settings, "max_parallel_browsers") else 20
    max_parallel = max(1, min(max_parallel, 30))  # Allow up to 30 parallel
    semaphore = asyncio.Semaphore(max_parallel)

    logger.info(
        "Processing %s domains with max_parallel=%s (FAST MODE — no Chrome)", total, max_parallel
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
