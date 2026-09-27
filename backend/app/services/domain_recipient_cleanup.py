"""Delete a domain's shared mailboxes and licensed application user before removal."""

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import aiohttp

from app.services.domain_license_cleanup import GRAPH_ROOT, _select_users, release_domain_user_licenses
from app.services.powershell.runner import PowerShellRunner

logger = logging.getLogger(__name__)
USER_FIELDS = "id,userPrincipalName,mail,proxyAddresses,assignedLicenses,licenseAssignmentStates,onPremisesSyncEnabled"


async def exchange_recipients(admin_email, admin_password, *, groups=None, domain=None, initial_domain=None):
    """Read active recipients, optionally retiring specified groups' old SMTP addresses."""
    payload = base64.b64encode(json.dumps({"email": admin_email, "password": admin_password,
        "groups": groups or [], "domain": domain, "initial_domain": initial_domain}).encode()).decode()
    script = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__')) | ConvertFrom-Json
$credential = [PSCredential]::new($p.email, (ConvertTo-SecureString $p.password -AsPlainText -Force))
try {
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
    Connect-ExchangeOnline -Credential $credential -ShowBanner:$false -ErrorAction Stop
    foreach ($id in $p.groups) {
        $matches = @(Get-UnifiedGroup -ResultSize Unlimited -ErrorAction Stop | Where-Object {
            [string]$_.ExternalDirectoryObjectId -eq $id
        })
        if ($matches.Count -ne 1) { throw 'Could not uniquely identify the Microsoft 365 group' }
        $group = $matches[0]
        $identity = [string]$group.Guid
        if ([string]::IsNullOrWhiteSpace($identity)) { throw 'Group GUID is missing' }
        $primary = [string]$group.PrimarySmtpAddress
        if ($primary.Split('@')[-1] -ieq $p.domain) {
            $replacement = $primary.Split('@')[0] + '@' + $p.initial_domain
            Set-UnifiedGroup -Identity $identity -PrimarySmtpAddress $replacement -ErrorAction Stop
        }
        $group = Get-UnifiedGroup -Identity $identity -ErrorAction Stop
        $old = @($group.EmailAddresses | ForEach-Object { [string]$_ } | Where-Object {
            $_ -imatch '^smtp:' -and $_.Split('@')[-1] -ieq $p.domain
        })
        if ($old.Count -gt 0) {
            Set-UnifiedGroup -Identity $identity -EmailAddresses @{Remove=$old} -ErrorAction Stop
        }
    }
    $recipients = @(Get-Recipient -ResultSize Unlimited -ErrorAction Stop | ForEach-Object {
        @{
            id = [string]$_.ExternalDirectoryObjectId
            type = [string]$_.RecipientTypeDetails
            primary = [string]$_.PrimarySmtpAddress
            addresses = @($_.EmailAddresses | ForEach-Object { [string]$_ })
        }
    })
    Write-Output '<<<JSON>>>'
    ConvertTo-Json -InputObject @{recipients=$recipients} -Depth 5 -Compress
    Write-Output '<<<END>>>'
} finally {
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}
'''.replace('__PAYLOAD__', payload)
    result = await PowerShellRunner(timeout=180).run(script)
    if not result.success or not isinstance(result.json_data, dict):
        # PowerShell diagnostics can include script fragments containing credentials.
        action = "group address cleanup" if groups else "mailbox discovery"
        raise RuntimeError(f"Exchange {action} failed; check Exchange sign-in and permissions")
    recipients = result.json_data.get("recipients")
    if not isinstance(recipients, list):
        raise RuntimeError("Exchange returned an incomplete recipient inventory")
    return recipients


def address_domain(value):
    value = (value or "").lower()
    if ":" in value:
        kind, value = value.split(":", 1)
        if kind != "smtp":
            return None
    return value.rsplit("@", 1)[1] if "@" in value else None


def check_user(user, domain, protected_ids, admin_email):
    if user["id"] in protected_ids or (user.get("userPrincipalName") or "").lower() == admin_email.lower():
        raise ValueError("Refusing to delete the tenant administrator")
    if user.get("onPremisesSyncEnabled"):
        raise ValueError("A cleanup target is synchronized from on-premises; remove it at its source")
    for address in [user.get("userPrincipalName"), user.get("mail"), *user.get("proxyAddresses", [])]:
        suffix = address_domain(address)
        if suffix and suffix != domain and not suffix.endswith(".onmicrosoft.com"):
            raise ValueError("A cleanup target now references another custom domain; refusing to delete it")
    if any(s.get("assignedByGroup") for s in user.get("licenseAssignmentStates", [])):
        raise ValueError("A cleanup target has group-assigned licenses; release those assignments first")


def plan_cleanup(users, recipients, domain, licensed_user_id, mailbox_ids, protected_ids, admin_email):
    """Select by SMTP domain or recorded object ID, never by display name/alias alone."""
    licensed = _select_users(users, domain, licensed_user_id, admin_email)
    licensed_ids = {u["id"] for u in licensed}
    by_id = {u["id"]: u for u in users}
    known_ids = set(mailbox_ids or ())
    shared = set()
    for recipient in recipients:
        addresses = [recipient.get("primary"), *recipient.get("addresses", [])]
        matches = any(address_domain(a) == domain for a in addresses)
        if not matches and recipient.get("id") not in known_ids:
            continue
        uid = recipient.get("id")
        if not uid:
            raise ValueError("An Exchange recipient on this domain has no directory object ID")
        if uid in licensed_ids:
            continue
        if recipient.get("type") == "GroupMailbox":
            # Preserve the group; its old SMTP addresses are moved separately.
            continue
        if recipient.get("type") != "SharedMailbox":
            raise ValueError("Domain still has a non-shared recipient; resolve it before domain removal")
        for address in addresses:
            suffix = address_domain(address)
            if suffix and suffix != domain and not suffix.endswith(".onmicrosoft.com"):
                raise ValueError("A shared mailbox also references another custom domain; refusing to delete it")
        if uid in by_id:
            check_user(by_id[uid], domain, protected_ids, admin_email)
            if by_id[uid].get("assignedLicenses"):
                raise ValueError("A shared mailbox has a license; resolve that assignment before deletion")
        shared.add(uid)
    for user in licensed:
        check_user(user, domain, protected_ids, admin_email)
    # Recorded users left behind by a previous partially completed deletion must
    # not silently survive just because Exchange has already removed the mailbox.
    for uid in known_ids - shared - licensed_ids:
        if uid in by_id:
            check_user(by_id[uid], domain, protected_ids, admin_email)
            raise ValueError("A recorded mailbox user exists without a confirmed shared mailbox; review it before removal")
    return sorted(shared), licensed


def recover_mailbox_ids(recipients, mailbox_records, domain, initial_domain):
    """Recover legacy rows without IDs using their full original mailbox address.

    Only the same local part on this tenant's initial domain is eligible. A
    matching display name, another custom domain, or another tenant is not.
    """
    ids = {r['id'] for r in mailbox_records if r.get('id') and address_domain(r.get('email')) == domain}
    for record in mailbox_records:
        if record.get('id') or address_domain(record.get('email')) != domain:
            continue
        expected = record['email'].lower().rsplit('@', 1)[0] + '@' + initial_domain
        candidates = [r for r in recipients if r.get('type') == 'SharedMailbox'
                      and (r.get('primary') or '').lower() == expected]
        if len(candidates) > 1:
            raise ValueError('Ambiguous renamed mailbox; record its Microsoft object ID before cleanup')
        if candidates:
            ids.add(candidates[0]['id'])
    return ids


def retry_delay(headers, attempt):
    value = headers.get('Retry-After', '')
    if value.isdigit():
        return int(value)
    try:
        return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 2 ** attempt


async def graph_request(session, method, path, *, absent_ok=False):
    for attempt in range(3):
        async with getattr(session, method)(f"{GRAPH_ROOT}/{path}") as response:
            if absent_ok and response.status == 404:
                return None
            if method == 'get' and response.status == 200:
                return await response.json()
            if method == 'delete' and response.status == 204:
                return None
            if response.status not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"Graph recipient {method} failed: HTTP {response.status}")
            delay = retry_delay(getattr(response, 'headers', {}), attempt)
        logger.info('Graph recipient %s temporarily unavailable; retrying in %ss', method, delay)
        await asyncio.sleep(delay)


async def graph_get(session, path, *, absent_ok=False):
    return await graph_request(session, 'get', path, absent_ok=absent_ok)


async def delete_user(session, uid, domain, protected_ids, admin_email):
    path = f"users/{quote(uid, safe='')}"
    user = await graph_get(session, f"{path}?$select={USER_FIELDS}", absent_ok=True)
    if user is None:
        return
    check_user(user, domain, protected_ids, admin_email)
    if user.get("assignedLicenses"):
        raise RuntimeError("Refusing to delete a user whose license release has not been verified")
    await graph_request(session, 'delete', path, absent_ok=True)
    for attempt in range(6):
        if await graph_get(session, f"{path}?$select=id", absent_ok=True) is None:
            return
        if attempt < 5:
            await asyncio.sleep(5)
    raise RuntimeError("Deleted user is still active in Microsoft; retry after propagation")


async def delete_shared_users(session, ids, domain, protected_ids, admin_email, progress):
    # Bound concurrency to avoid a burst of requests across a 100-mailbox tenant.
    # Drain the whole group on error before proceeding or returning to the caller.
    for offset in range(0, len(ids), 4):
        results = await asyncio.gather(*(
            delete_user(session, uid, domain, protected_ids, admin_email)
            for uid in ids[offset:offset + 4]
        ), return_exceptions=True)
        failures = []
        for outcome in results:
            if isinstance(outcome, BaseException):
                failures.append(outcome)
            else:
                progress['removed'] += 1
        if failures:
            raise failures[0]


async def verify_exchange_absent(ids, domain, admin_email, admin_password, *, final=False):
    for attempt in range(6):
        recipients = await exchange_recipients(admin_email, admin_password)
        remaining = [r for r in recipients if r.get("id") in ids or (final and any(
            address_domain(a) == domain for a in [r.get("primary"), *r.get("addresses", [])]
        ))]
        if not remaining:
            return
        if attempt < 5:
            await asyncio.sleep(10)
    raise RuntimeError("Exchange still references removed accounts or the old domain; retry after propagation")


async def cleanup_domain_recipients(access_token, domain_name, admin_email, admin_password,
                                    licensed_user_id=None, mailbox_ids=None, mailbox_records=None):
    domain = domain_name.strip().lower()
    result = {"success": False, "shared_mailboxes": {"success": False, "removed": 0},
              "license_cleanup": {"skipped": True}, "licensed_user_deletion": {"skipped": True}}
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {access_token}"},
                                         timeout=aiohttp.ClientTimeout(total=60)) as session:
            me = await graph_get(session, "me?$select=id,userPrincipalName")
            protected = {me["id"]}
            users = []
            path = f"users?$select={USER_FIELDS}&$top=999"
            while path:
                payload = await graph_get(session, path)
                users.extend(payload["value"])
                next_link = payload.get("@odata.nextLink")
                if next_link and not next_link.startswith(GRAPH_ROOT + "/"):
                    raise RuntimeError("Unexpected Graph pagination URL")
                path = next_link[len(GRAPH_ROOT) + 1:] if next_link else None
            recipients = await exchange_recipients(admin_email, admin_password)
            initial_domain = None
            group_ids = [r['id'] for r in recipients if r.get('type') == 'GroupMailbox' and any(
                address_domain(a) == domain for a in [r.get('primary'), *r.get('addresses', [])])]
            if mailbox_records or group_ids:
                domains = await graph_get(session, 'domains?$select=id,isInitial')
                initial = [d['id'].lower() for d in domains['value'] if d.get('isInitial')]
                if len(initial) != 1 or not initial[0].endswith('.onmicrosoft.com') or domains.get('@odata.nextLink'):
                    raise RuntimeError('Could not verify the tenant initial domain')
                initial_domain = initial[0]
                mailbox_ids = set(mailbox_ids or ()) | recover_mailbox_ids(
                    recipients, mailbox_records or [], domain, initial_domain)
            shared, licensed = plan_cleanup(users, recipients, domain, licensed_user_id,
                                            mailbox_ids, protected, admin_email)
            logger.info("[%s] Recipient cleanup: %d shared mailboxes, %d application users", domain, len(shared), len(licensed))
            await delete_shared_users(session, shared, domain, protected, admin_email,
                                      result['shared_mailboxes'])
            if shared:
                await verify_exchange_absent(set(shared), domain, admin_email, admin_password)
            result["shared_mailboxes"]["success"] = True
            # Shared mailboxes must be gone before the delegate's license is released.
            license_result = await release_domain_user_licenses(
                access_token, domain, licensed_user_id, admin_email,
                expected_user_ids={u['id'] for u in licensed},
            )
            result["license_cleanup"] = license_result
            if not license_result.get("success"):
                raise RuntimeError(license_result.get("error", "License release failed"))
            deletion = result["licensed_user_deletion"] = {"success": False, "removed": 0}
            for user in licensed:
                await delete_user(session, user["id"], domain, protected, admin_email)
                deletion["removed"] += 1
            if group_ids:
                await exchange_recipients(admin_email, admin_password, groups=group_ids,
                                          domain=domain, initial_domain=initial_domain)
                result['group_addresses'] = {'success': True, 'updated': len(group_ids)}
            await verify_exchange_absent(set(shared) | {u['id'] for u in licensed}, domain,
                                         admin_email, admin_password, final=True)
            deletion["success"] = True
        result["success"] = True
    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("[%s] Recipient cleanup stopped: %s", domain, exc)
    return result
