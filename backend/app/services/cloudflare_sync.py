import httpx
import os
import logging

logger = logging.getLogger(__name__)
CF_API = "https://api.cloudflare.com/client/v4"

# Cache: zone_id -> account index (0=primary, 1=secondary)
_zone_account_cache: dict[str, int] = {}


def _get_all_creds() -> list[tuple[str, str]]:
    """Get credentials for ALL configured Cloudflare accounts.
    
    Returns:
        List of (email, api_key) tuples. Primary first, secondary second.
    """
    accounts = []
    env_vars = {}
    
    # Read from environment first
    for key in ["CLOUDFLARE_EMAIL", "CLOUDFLARE_API_KEY", 
                "CLOUDFLARE_EMAIL_2", "CLOUDFLARE_API_KEY_2"]:
        env_vars[key] = os.environ.get(key)
    
    # Fall back to .env files if any creds are missing
    any_missing = any(not env_vars.get(k) for k in env_vars)
    if any_missing:
        for env_path in [".env", "../.env", "backend/.env"]:
            try:
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if "=" in line and not line.startswith("#"):
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k in env_vars and not env_vars[k]:
                                env_vars[k] = v
            except:
                continue
    
    # Primary account
    if env_vars.get("CLOUDFLARE_EMAIL") and env_vars.get("CLOUDFLARE_API_KEY"):
        accounts.append((env_vars["CLOUDFLARE_EMAIL"], env_vars["CLOUDFLARE_API_KEY"]))
    
    # Secondary account
    if env_vars.get("CLOUDFLARE_EMAIL_2") and env_vars.get("CLOUDFLARE_API_KEY_2"):
        accounts.append((env_vars["CLOUDFLARE_EMAIL_2"], env_vars["CLOUDFLARE_API_KEY_2"]))
    
    if not accounts:
        raise ValueError("Cloudflare credentials missing!")
    
    return accounts


def _get_creds(account_index: int = 0):
    """Get Cloudflare credentials for a specific account.
    
    Args:
        account_index: 0 for primary, 1 for secondary
    """
    accounts = _get_all_creds()
    if account_index >= len(accounts):
        return accounts[0]  # Fallback to primary
    return accounts[account_index]


def _headers(account_index: int = 0):
    email, key = _get_creds(account_index)
    return {"X-Auth-Email": email, "X-Auth-Key": key, "Content-Type": "application/json"}


def _headers_for_zone(zone_id: str):
    """Get headers for the account that owns this zone_id."""
    account_idx = _zone_account_cache.get(zone_id, 0)
    return _headers(account_idx)


def resolve_zone_id(zone_id, domain):
    """
    Validate that zone_id matches the domain, and auto-correct if wrong.
    Searches ALL configured Cloudflare accounts.
    
    This is CRITICAL for domains that were pre-existing in Cloudflare before
    being imported into the system. The database may have a stale/wrong zone_id.
    
    Flow:
    1. For each account, GET /zones/{zone_id} - check if zone exists and name matches
    2. If zone doesn't exist OR name doesn't match:
       a. For each account, GET /zones?name={domain} - find the correct zone
       b. Prefer active zones over pending
       c. Return the correct zone_id
    3. If domain not found in ANY Cloudflare account, raise ValueError
    
    Args:
        zone_id: The zone_id stored in the database (may be wrong)
        domain: The domain name we expect this zone to be for
        
    Returns:
        tuple: (correct_zone_id, was_corrected)
        - correct_zone_id: The validated/corrected zone_id
        - was_corrected: True if the zone_id was wrong and had to be looked up
    """
    logger.info(f"[{domain}] Validating zone_id={zone_id}")
    all_creds = _get_all_creds()
    domain_lower = domain.lower().strip()
    
    # Step 1: Check if the stored zone_id is valid and matches the domain (check ALL accounts)
    # BUT: If the stored zone is "pending", DON'T return immediately - check other accounts for "active"
    stored_zone_match = None  # (zone_id, acct_idx, status)
    if zone_id:
        for acct_idx in range(len(all_creds)):
            try:
                headers = _headers(acct_idx)
                resp = httpx.get(f"{CF_API}/zones/{zone_id}", headers=headers, timeout=30)
                if _cf_success(resp):
                    result = resp.json().get("result", {})
                    zone_name = result.get("name", "").lower().strip()
                    zone_status = result.get("status", "pending")
                    if zone_name == domain_lower:
                        if zone_status == "active":
                            # Active zone found - use it immediately
                            logger.info(f"[{domain}] Zone ID validated (ACTIVE): {zone_id} -> {zone_name} (account #{acct_idx})")
                            _zone_account_cache[zone_id] = acct_idx
                            return zone_id, False
                        else:
                            # Pending zone - note it but keep looking for an active one
                            logger.info(f"[{domain}] Zone ID matches but PENDING: {zone_id} (account #{acct_idx}). Checking other accounts for active zone...")
                            stored_zone_match = (zone_id, acct_idx, zone_status)
                    else:
                        logger.warning(
                            f"[{domain}] zone_id={zone_id} belongs to '{zone_name}' in account #{acct_idx}, "
                            f"not '{domain_lower}'. Continuing search..."
                        )
            except Exception as e:
                logger.warning(f"[{domain}] Error checking zone {zone_id} in account #{acct_idx}: {e}")
    
    # Step 2: Look up the correct zone by domain name across ALL accounts (prefer active)
    best_zone = None
    best_acct_idx = 0
    for acct_idx in range(len(all_creds)):
        try:
            headers = _headers(acct_idx)
            resp = httpx.get(f"{CF_API}/zones?name={domain_lower}", headers=headers, timeout=30)
            if _cf_success(resp):
                zones = resp.json().get("result", [])
                for z in zones:
                    if z.get("name", "").lower().strip() == domain_lower:
                        status = z.get("status", "")
                        logger.info(f"[{domain}] Found zone {z['id']} in account #{acct_idx} (status={status})")
                        # Prefer active zones
                        if status == "active" or best_zone is None:
                            best_zone = z
                            best_acct_idx = acct_idx
                        if status == "active":
                            break  # Active zone found, no need to check more
        except Exception as e:
            logger.warning(f"[{domain}] Error searching account #{acct_idx}: {e}")
    
    if best_zone:
        correct_zone_id = best_zone["id"]
        correct_name = best_zone["name"]
        correct_status = best_zone.get("status", "unknown")
        _zone_account_cache[correct_zone_id] = best_acct_idx
        was_corrected = correct_zone_id != zone_id
        logger.info(
            f"[{domain}] RESOLVED correct zone: {correct_zone_id} (name={correct_name}, "
            f"status={correct_status}, account #{best_acct_idx}). Old zone_id was: {zone_id}, corrected={was_corrected}"
        )
        return correct_zone_id, was_corrected
    
    # Step 3: If we found a pending zone match in Step 1 but no active zone anywhere, use the pending one
    if stored_zone_match:
        pending_zone_id, pending_acct_idx, pending_status = stored_zone_match
        _zone_account_cache[pending_zone_id] = pending_acct_idx
        logger.info(
            f"[{domain}] No active zone found anywhere. Using pending zone: {pending_zone_id} "
            f"(status={pending_status}, account #{pending_acct_idx})"
        )
        return pending_zone_id, False
    
    error_msg = f"[{domain}] Domain not found in ANY Cloudflare account! Cannot resolve zone_id."
    logger.error(error_msg)
    raise ValueError(error_msg)


def _cf_success(resp) -> bool:
    """
    Check if a Cloudflare API response was truly successful.
    
    Cloudflare API v4 can return HTTP 200 with {"success": false} in the body.
    We must check BOTH the HTTP status code AND the JSON body's success field.
    
    Args:
        resp: httpx.Response object
        
    Returns:
        True only if HTTP status is 200 AND body has "success": true
    """
    if resp.status_code != 200:
        return False
    try:
        data = resp.json()
        return data.get("success", False) is True
    except Exception:
        return False


def _cf_error_message(resp) -> str:
    """
    Extract a human-readable error message from a Cloudflare API response.
    
    Args:
        resp: httpx.Response object
        
    Returns:
        Error message string
    """
    try:
        data = resp.json()
        errors = data.get("errors", [])
        if errors:
            # Cloudflare errors have "code" and "message" fields
            messages = [f"{e.get('code', '?')}: {e.get('message', 'Unknown')}" for e in errors]
            return "; ".join(messages)
        if not data.get("success", True):
            return f"API returned success=false (HTTP {resp.status_code})"
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:200]}"


def delete_records_by_type(zone_id, record_type, name_contains=None):
    """Delete ALL records of a specific type, optionally filtering by name."""
    logger.info(f"Deleting {record_type} records{' containing ' + name_contains if name_contains else ''}...")
    try:
        headers = _headers_for_zone(zone_id)
        resp = httpx.get(f"{CF_API}/zones/{zone_id}/dns_records?type={record_type}", headers=headers, timeout=30)
        if _cf_success(resp):
            records = resp.json().get("result", [])
            for r in records:
                should_delete = True
                if name_contains and name_contains not in r.get("name", ""):
                    should_delete = False
                
                if should_delete:
                    del_resp = httpx.delete(f"{CF_API}/zones/{zone_id}/dns_records/{r['id']}", headers=headers, timeout=30)
                    if _cf_success(del_resp):
                        logger.info(f"Deleted {record_type}: {r.get('name')} -> {r.get('content', '')[:50]}")
                    else:
                        logger.warning(f"Failed to delete {r['id']}: {_cf_error_message(del_resp)}")
        elif resp.status_code == 200:
            logger.warning(f"Cloudflare returned 200 but success=false listing {record_type} records: {_cf_error_message(resp)}")
        return True
    except Exception as e:
        logger.error(f"Error deleting {record_type} records: {e}")
        return False

def cleanup_before_verification(zone_id):
    """
    Clean up DNS records that would interfere with M365 domain verification.
    
    M365 verification checks TXT records at @ and flags ANY unexpected TXT records
    as "Invalid entry", causing verification to fail. This function removes:
    - ALL old MS=ms* verification codes (from previous attempts)
    - SPF records (v=spf1...) left over from previous failed Step 5 runs
    
    Does NOT touch: proxied CNAMEs (redirects), DMARC, A/AAAA records, etc.
    """
    logger.info(f"=== CLEANUP BEFORE VERIFICATION ===")
    try:
        headers = _headers_for_zone(zone_id)
        resp = httpx.get(f"{CF_API}/zones/{zone_id}/dns_records?type=TXT", headers=headers, timeout=30)
        if _cf_success(resp):
            for r in resp.json().get("result", []):
                content = r.get("content", "")
                record_name = r.get("name", "")
                
                # Delete ALL MS= verification records (old codes from previous attempts)
                if content.startswith("MS=") or content.startswith("ms="):
                    del_resp = httpx.delete(f"{CF_API}/zones/{zone_id}/dns_records/{r['id']}", headers=headers, timeout=30)
                    if _cf_success(del_resp):
                        logger.info(f"Deleted old verification TXT: {content}")
                    else:
                        logger.warning(f"Failed to delete old verification TXT {content}: {_cf_error_message(del_resp)}")
                
                # Delete SPF records at root (leftover from previous failed runs)
                # These cause M365 to flag as "Invalid entry" during verification
                elif "v=spf1" in content.lower():
                    del_resp = httpx.delete(f"{CF_API}/zones/{zone_id}/dns_records/{r['id']}", headers=headers, timeout=30)
                    if _cf_success(del_resp):
                        logger.info(f"Deleted leftover SPF TXT: {content[:60]}")
                    else:
                        logger.warning(f"Failed to delete leftover SPF TXT: {_cf_error_message(del_resp)}")
        elif resp.status_code == 200:
            logger.warning(f"Cloudflare returned 200 but success=false listing TXT records: {_cf_error_message(resp)}")
        else:
            logger.error(f"Failed to list TXT records for cleanup: {_cf_error_message(resp)}")
        
        logger.info(f"=== CLEANUP BEFORE VERIFICATION COMPLETE ===")
        return True
    except Exception as e:
        logger.error(f"Cleanup before verification error: {e}")
        return False


def cleanup_before_dns_setup(zone_id):
    """
    Clean up DNS records that would conflict with M365 email DNS setup.
    
    Removes records that conflict with what Step 5 is about to add:
    - Old MX records (will be replaced with M365 MX)
    - Old SPF TXT records (will be replaced with M365 SPF)
    - Old autodiscover CNAMEs (will be replaced)
    - Old DKIM selector CNAMEs (will be replaced)
    - A/AAAA records for 'autodiscover' subdomain (conflict with CNAME)
    - A/AAAA records for selector1/selector2._domainkey (conflict with CNAME)
    
    Does NOT touch: proxied CNAME at @ (redirect), DMARC records, other TXT records
    """
    logger.info(f"=== CLEANUP BEFORE DNS SETUP ===")
    try:
        headers = _headers_for_zone(zone_id)
        
        # Get ALL DNS records for the zone
        resp = httpx.get(f"{CF_API}/zones/{zone_id}/dns_records", headers=headers, timeout=30)
        if not _cf_success(resp):
            logger.error(f"Failed to list DNS records: {_cf_error_message(resp)}")
            return False
        
        records = resp.json().get("result", [])
        
        for r in records:
            record_type = r.get("type", "")
            record_name = r.get("name", "")
            content = r.get("content", "")
            proxied = r.get("proxied", False)
            
            should_delete = False
            reason = ""
            
            # Delete old MX records (will be replaced)
            if record_type == "MX":
                should_delete = True
                reason = "old MX record"
            
            # Delete old SPF TXT records (will be replaced)
            elif record_type == "TXT" and "v=spf1" in content.lower():
                should_delete = True
                reason = "old SPF TXT"
            
            # NOTE: Do NOT delete MS= verification records here!
            # They don't conflict with MX/SPF/CNAME setup and deleting them
            # breaks M365 verification if it hasn't completed yet.
            
            # Delete CNAME/A/AAAA for autodiscover (conflicts with new CNAME)
            elif "autodiscover" in record_name.lower() and record_type in ("CNAME", "A", "AAAA"):
                should_delete = True
                reason = f"old autodiscover {record_type}"
            
            # Delete CNAME/A/AAAA for DKIM selectors (conflicts with new CNAMEs)
            elif ("selector1._domainkey" in record_name.lower() or "selector2._domainkey" in record_name.lower()) and record_type in ("CNAME", "A", "AAAA"):
                should_delete = True
                reason = f"old DKIM {record_type}"
            
            if should_delete:
                del_resp = httpx.delete(f"{CF_API}/zones/{zone_id}/dns_records/{r['id']}", headers=headers, timeout=30)
                if _cf_success(del_resp):
                    logger.info(f"Deleted {reason}: {record_type} {record_name} -> {content[:60]}")
                else:
                    logger.warning(f"Failed to delete {reason} ({record_type} {record_name}): {_cf_error_message(del_resp)}")
        
        logger.info(f"=== CLEANUP BEFORE DNS SETUP COMPLETE ===")
        return True
    except Exception as e:
        logger.error(f"Cleanup before DNS setup error: {e}")
        return False


def _verify_record_exists(zone_id, record_type, content_search, headers):
    """
    Verify a DNS record actually exists in Cloudflare after creation.
    
    Args:
        zone_id: Cloudflare zone ID
        record_type: DNS record type (TXT, MX, CNAME)
        content_search: String to search for in record content
        headers: API headers
        
    Returns:
        True if record found, False otherwise
    """
    try:
        resp = httpx.get(f"{CF_API}/zones/{zone_id}/dns_records?type={record_type}", headers=headers, timeout=30)
        if _cf_success(resp):
            for r in resp.json().get("result", []):
                if content_search.lower() in r.get("content", "").lower():
                    return True
        return False
    except Exception as e:
        logger.warning(f"Verification check failed: {e}")
        return False


def add_txt(zone_id, value):
    """Add TXT record, preserving the exact verification value if it exists.
    
    IMPORTANT: Checks both HTTP status code AND Cloudflare API success field
    to ensure the record was actually created.
    """
    logger.info(f"Adding TXT: {value}")
    try:
        headers = _headers_for_zone(zone_id)
        exact_exists = False
        
        # Delete stale MS= verification records, but do not delete/recreate the
        # exact page-scraped value. Microsoft verification can fail if the TXT
        # keeps disappearing during retry propagation windows.
        resp = httpx.get(f"{CF_API}/zones/{zone_id}/dns_records?type=TXT", headers=headers, timeout=30)
        if _cf_success(resp):
            for r in resp.json().get("result", []):
                content = r.get("content", "")
                if content.startswith("MS=") or content.startswith("ms="):
                    if content.strip().lower() == value.strip().lower():
                        exact_exists = True
                        logger.info(f"TXT already present in Cloudflare, preserving stable record: {value}")
                        continue
                    del_resp = httpx.delete(f"{CF_API}/zones/{zone_id}/dns_records/{r['id']}", headers=headers, timeout=30)
                    if _cf_success(del_resp):
                        logger.info(f"Deleted old TXT: {content}")
                    else:
                        logger.warning(f"Failed to delete old TXT {content}: {_cf_error_message(del_resp)}")
        elif resp.status_code == 200:
            logger.warning(f"Cloudflare returned 200 but success=false listing TXT records: {_cf_error_message(resp)}")

        if exact_exists:
            if _verify_record_exists(zone_id, "TXT", value, headers):
                logger.info(f"TXT record verified in Cloudflare: {value}")
                return True
            logger.warning(f"TXT record was listed but verification check did not find it; attempting create: {value}")
        
        # Add new
        resp = httpx.post(f"{CF_API}/zones/{zone_id}/dns_records", headers=headers,
                          json={"type": "TXT", "name": "@", "content": value, "ttl": 1}, timeout=30)
        
        if _cf_success(resp):
            record_id = resp.json().get("result", {}).get("id", "unknown")
            logger.info(f"TXT added successfully (record_id={record_id})")
            
            # Double-check: Verify the record actually exists
            if _verify_record_exists(zone_id, "TXT", value, headers):
                logger.info(f"TXT record verified in Cloudflare: {value}")
            else:
                logger.warning(f"TXT record NOT found in Cloudflare after creation! API said success but record missing.")
            
            return True
        elif "already exists" in resp.text.lower():
            logger.info("TXT already exists")
            return True
        else:
            error_msg = _cf_error_message(resp)
            logger.error(f"TXT add FAILED: {error_msg}")
            logger.error(f"TXT full response: status={resp.status_code}, body={resp.text[:500]}")
            return False
    except Exception as e:
        logger.error(f"TXT error: {e}")
        return False

def add_mx(zone_id, target, priority=0):
    """Add MX record, deleting ALL existing MX records first.
    
    IMPORTANT: Checks both HTTP status code AND Cloudflare API success field.
    """
    logger.info(f"Adding MX: {target} (priority {priority})")
    try:
        headers = _headers_for_zone(zone_id)
        
        # DELETE ALL existing MX records
        resp = httpx.get(f"{CF_API}/zones/{zone_id}/dns_records?type=MX", headers=headers, timeout=30)
        if _cf_success(resp):
            for r in resp.json().get("result", []):
                del_resp = httpx.delete(f"{CF_API}/zones/{zone_id}/dns_records/{r['id']}", headers=headers, timeout=30)
                if _cf_success(del_resp):
                    logger.info(f"Deleted existing MX: {r.get('content')}")
                else:
                    logger.warning(f"Failed to delete existing MX: {_cf_error_message(del_resp)}")
        
        # Add new
        resp = httpx.post(f"{CF_API}/zones/{zone_id}/dns_records", headers=headers,
                          json={"type": "MX", "name": "@", "content": target, "priority": priority, "ttl": 1}, timeout=30)
        
        if _cf_success(resp):
            record_id = resp.json().get("result", {}).get("id", "unknown")
            logger.info(f"MX added successfully (record_id={record_id})")
            return True
        else:
            error_msg = _cf_error_message(resp)
            logger.error(f"MX add FAILED: {error_msg}")
            logger.error(f"MX full response: status={resp.status_code}, body={resp.text[:500]}")
            return False
    except Exception as e:
        logger.error(f"MX error: {e}")
        return False

def add_spf(zone_id, value):
    """Add SPF record, deleting ALL existing SPF records first.
    
    IMPORTANT: Checks both HTTP status code AND Cloudflare API success field.
    """
    logger.info(f"Adding SPF: {value}")
    try:
        headers = _headers_for_zone(zone_id)
        
        # DELETE ALL existing SPF records (TXT records containing v=spf1)
        resp = httpx.get(f"{CF_API}/zones/{zone_id}/dns_records?type=TXT", headers=headers, timeout=30)
        if _cf_success(resp):
            for r in resp.json().get("result", []):
                if "v=spf1" in r.get("content", ""):
                    del_resp = httpx.delete(f"{CF_API}/zones/{zone_id}/dns_records/{r['id']}", headers=headers, timeout=30)
                    if _cf_success(del_resp):
                        logger.info(f"Deleted existing SPF: {r.get('content')[:50]}")
                    else:
                        logger.warning(f"Failed to delete existing SPF: {_cf_error_message(del_resp)}")
        
        # Add new
        resp = httpx.post(f"{CF_API}/zones/{zone_id}/dns_records", headers=headers,
                          json={"type": "TXT", "name": "@", "content": value, "ttl": 1}, timeout=30)
        
        if _cf_success(resp):
            record_id = resp.json().get("result", {}).get("id", "unknown")
            logger.info(f"SPF added successfully (record_id={record_id})")
            return True
        else:
            error_msg = _cf_error_message(resp)
            logger.error(f"SPF add FAILED: {error_msg}")
            logger.error(f"SPF full response: status={resp.status_code}, body={resp.text[:500]}")
            return False
    except Exception as e:
        logger.error(f"SPF error: {e}")
        return False

def add_cname(zone_id, name, target):
    """Add CNAME record, deleting existing record with same name first.
    
    IMPORTANT: Checks both HTTP status code AND Cloudflare API success field.
    """
    logger.info(f"Adding CNAME: {name} -> {target}")
    try:
        headers = _headers_for_zone(zone_id)
        
        # Get zone details to get the domain name
        zone_resp = httpx.get(f"{CF_API}/zones/{zone_id}", headers=headers, timeout=30)
        domain = ""
        if _cf_success(zone_resp):
            domain = zone_resp.json().get("result", {}).get("name", "")
        
        # Build full record name
        if domain and not name.endswith(domain):
            full_name = f"{name}.{domain}"
        else:
            full_name = name
        
        # DELETE existing CNAME with this name
        resp = httpx.get(f"{CF_API}/zones/{zone_id}/dns_records?type=CNAME", headers=headers, timeout=30)
        if _cf_success(resp):
            for r in resp.json().get("result", []):
                if name in r.get("name", "") or r.get("name", "") == full_name:
                    del_resp = httpx.delete(f"{CF_API}/zones/{zone_id}/dns_records/{r['id']}", headers=headers, timeout=30)
                    if _cf_success(del_resp):
                        logger.info(f"Deleted existing CNAME: {r.get('name')}")
                    else:
                        logger.warning(f"Failed to delete existing CNAME {r.get('name')}: {_cf_error_message(del_resp)}")
        
        # Add new
        resp = httpx.post(f"{CF_API}/zones/{zone_id}/dns_records", headers=headers,
                          json={"type": "CNAME", "name": name, "content": target, "ttl": 1, "proxied": False}, timeout=30)
        
        if _cf_success(resp):
            record_id = resp.json().get("result", {}).get("id", "unknown")
            logger.info(f"CNAME added successfully: {name} -> {target} (record_id={record_id})")
            return True
        elif "already exists" in resp.text.lower():
            logger.info("CNAME already exists")
            return True
        else:
            error_msg = _cf_error_message(resp)
            logger.error(f"CNAME add FAILED ({name} -> {target}): {error_msg}")
            logger.error(f"CNAME full response: status={resp.status_code}, body={resp.text[:500]}")
            return False
    except Exception as e:
        logger.error(f"CNAME error: {e}")
        return False

def add_dkim(zone_id, selector1_target, selector2_target):
    """Add both DKIM CNAME records, deleting existing first."""
    logger.info(f"Adding DKIM records")
    
    # Delete existing DKIM records
    try:
        headers = _headers_for_zone(zone_id)
        resp = httpx.get(f"{CF_API}/zones/{zone_id}/dns_records?type=CNAME", headers=headers, timeout=30)
        if _cf_success(resp):
            for r in resp.json().get("result", []):
                if "selector1" in r.get("name", "") or "selector2" in r.get("name", ""):
                    del_resp = httpx.delete(f"{CF_API}/zones/{zone_id}/dns_records/{r['id']}", headers=headers, timeout=30)
                    if _cf_success(del_resp):
                        logger.info(f"Deleted existing DKIM: {r.get('name')}")
                    else:
                        logger.warning(f"Failed to delete existing DKIM {r.get('name')}: {_cf_error_message(del_resp)}")
    except Exception as e:
        logger.warning(f"Error cleaning up old DKIM records: {e}")
    
    # Add selector1
    result1 = add_cname(zone_id, "selector1._domainkey", selector1_target)
    
    # Add selector2
    result2 = add_cname(zone_id, "selector2._domainkey", selector2_target)
    
    return result1 and result2
