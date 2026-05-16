"""
Validation service for the upfront collection form.
Parses all 3 files, cross-validates, and returns a complete preview.
"""
import csv
import io
import re
import logging
from typing import Dict, List, Any, Optional, Tuple

# Reuse the explicit-domain header detection helpers from the import service
# so both the validation pre-flight and the actual import recognise the
# "Domain N to link tenant" columns identically.
from app.services.tenant_import import (
    _is_explicit_domain_column,
    _is_password_column,
    _is_totp_secret_column,
    _extract_column_index,
    _normalize_domain_name,
    _normalize_totp_secret,
)

logger = logging.getLogger(__name__)



def parse_domains_csv_content(content: str) -> Tuple[List[Dict], List[str]]:
    """
    Parse domains CSV. Expected columns:
      - domain (required)
      - redirect_url (optional)
      - first_name + last_name (optional per-domain persona, preferred form)
      - firstname + lastname (same, alt spelling)
      - display_name / displayname (split on LAST whitespace so last token wins:
        "Mary Jane Smith" -> first="Mary Jane", last="Smith")

    Per-row persona falls back to the batch-level first/last on empty.
    Returns (parsed_domains, errors).
    """
    errors = []
    domains = []

    try:
        reader = csv.DictReader(io.StringIO(content))
        columns = reader.fieldnames or []

        # --- Column detection (case/whitespace-insensitive) ---
        def _find(*candidates: str) -> Optional[str]:
            wanted = {c.lower() for c in candidates}
            for col in columns:
                if col.strip().lower() in wanted:
                    return col
            return None

        domain_col = _find("domain", "domain_name", "name")
        if not domain_col:
            errors.append(f"CSV must have a 'domain' column. Found: {', '.join(columns)}")
            return domains, errors

        redirect_col = _find("redirect_url", "redirect", "url")
        first_col = _find("first_name", "firstname")
        last_col = _find("last_name", "lastname")
        display_col = _find("display_name", "displayname")

        for i, row in enumerate(reader, start=2):
            domain_name = row.get(domain_col, "").strip().lower()
            if not domain_name:
                continue

            # Basic domain format validation
            if not re.match(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z]{2,})+$', domain_name):
                errors.append(f"Row {i}: Invalid domain format: {domain_name}")
                continue

            redirect_url = row.get(redirect_col, "").strip() if redirect_col else ""

            # --- Per-row persona parsing ---
            parsed_first = ""
            parsed_last = ""
            if first_col or last_col:
                # Explicit first/last columns (preferred, unambiguous)
                parsed_first = (row.get(first_col, "") if first_col else "").strip()
                parsed_last = (row.get(last_col, "") if last_col else "").strip()
            elif display_col:
                raw_display = (row.get(display_col, "") or "").strip()
                if raw_display:
                    # Split on LAST whitespace so the last token becomes the surname:
                    # "Mary Jane Smith" -> first="Mary Jane", last="Smith".
                    # This matches the parse_display_name convention used by
                    # email_generator.py (last = parts[-1]).
                    parts = raw_display.rsplit(None, 1)
                    if len(parts) == 2:
                        parsed_first, parsed_last = parts[0].strip(), parts[1].strip()
                    else:
                        # Single token: put it all in first_name, leave last blank.
                        # cross_validate will flag this row if no batch default exists.
                        parsed_first = parts[0].strip()
                        parsed_last = ""

            domains.append({
                "name": domain_name,
                "redirect_url": redirect_url,
                "first_name": parsed_first or "",   # "" means "use batch-level fallback"
                "last_name": parsed_last or "",
            })

    except Exception as e:
        errors.append(f"Failed to parse domains CSV: {str(e)}")

    return domains, errors


def parse_tenants_csv_content(content: str) -> Tuple[List[Dict], List[str]]:
    """
    Parse tenant CSV from reseller.
    Flexible column detection — handles various reseller formats.

    Also recognises optional "Domain N to link tenant" columns and attaches
    them to each parsed tenant as `explicit_domains: List[str]` (normalized,
    in slot order). Empty / absent columns yield an empty list, which means
    "fall back to legacy auto-link".

    Returns (parsed_tenants, errors).
    """
    errors = []
    tenants = []

    try:
        reader = csv.DictReader(io.StringIO(content))
        columns = [c.strip() for c in (reader.fieldnames or [])]
        columns_lower = [c.lower() for c in columns]

        # Detect explicit "Domain N to link tenant" columns FIRST so we
        # don't misidentify them as the onmicrosoft column. They're sorted
        # by the embedded digit so slot order matches user intent.
        explicit_domain_cols = [c for c in columns if _is_explicit_domain_column(c)]
        explicit_domain_cols.sort(key=lambda c: (_extract_column_index(c), c.lower()))

        # Set of lowercase explicit-column names so the generic matchers below
        # can skip them.
        explicit_set_lower = {c.lower() for c in explicit_domain_cols}

        # Find the onmicrosoft domain column - check names first
        onmicrosoft_col = None
        for i, col in enumerate(columns_lower):
            if col in explicit_set_lower:
                continue
            if "onmicrosoft" in col or "domain" in col or "username" in col or "email" in col or "pattern" in col:
                onmicrosoft_col = columns[i]
                break

        # Fallback: scan first data row values for onmicrosoft.com
        if not onmicrosoft_col:
            # Peek at rows to find which column contains onmicrosoft.com values
            peek_reader = csv.DictReader(io.StringIO(content))
            for peek_row in peek_reader:
                for col, val in peek_row.items():
                    if val and "onmicrosoft.com" in val.lower():
                        onmicrosoft_col = col
                        break
                break  # Only check first data row

        # Find tenant name column
        name_col = None
        for i, col in enumerate(columns_lower):
            if col in explicit_set_lower:
                continue
            if "company" in col or "name" in col or "tenant" in col:
                name_col = columns[i]
                break

        # Find tenant ID column
        id_col = None
        for i, col in enumerate(columns_lower):
            if col in explicit_set_lower:
                continue
            if "uuid" in col or "tenant_id" in col or "id" in col:
                id_col = columns[i]
                break

        # Find password column, including provider typo "Passoword"
        password_col = None
        for i, col in enumerate(columns_lower):
            if col in explicit_set_lower:
                continue
            if _is_password_column(columns[i]):
                password_col = columns[i]
                break

        # Find preconfigured TOTP/MFA secret column
        totp_col = None
        for i, col in enumerate(columns_lower):
            if col in explicit_set_lower:
                continue
            if _is_totp_secret_column(columns[i]):
                totp_col = columns[i]
                break

        if not onmicrosoft_col:
            errors.append(f"Tenant CSV must have a column containing 'onmicrosoft' or 'domain'. Found: {', '.join(columns)}")
            return tenants, errors

        for i, row in enumerate(reader, start=2):
            onmicrosoft = row.get(onmicrosoft_col, "").strip()
            if not onmicrosoft or not onmicrosoft.strip():
                continue
            onmicrosoft = onmicrosoft.strip()

            # If value is an email address, extract the domain part
            if "@" in onmicrosoft:
                onmicrosoft = onmicrosoft.split("@")[1].strip()

            # Normalize: add .onmicrosoft.com if just the prefix
            if not onmicrosoft.endswith(".onmicrosoft.com"):
                onmicrosoft = f"{onmicrosoft}.onmicrosoft.com"

            # Extract explicit domain assignments (normalized) in slot order
            explicit_domains: List[str] = []
            for col in explicit_domain_cols:
                raw_val = row.get(col, "") or ""
                normalized = _normalize_domain_name(raw_val)
                if normalized:
                    explicit_domains.append(normalized)

            tenant_name = row.get(name_col, "").strip() if name_col and name_col != onmicrosoft_col else ""
            if tenant_name and "onmicrosoft.com" in tenant_name.lower():
                tenant_name = ""

            tenants.append({
                "name": tenant_name or onmicrosoft.split(".")[0],
                "onmicrosoft_domain": onmicrosoft,
                "microsoft_tenant_id": row.get(id_col, "").strip() if id_col else "",
                "password": row.get(password_col, "").strip() if password_col else "",
                "totp_secret": _normalize_totp_secret(row.get(totp_col, "")) if totp_col else "",
                "explicit_domains": explicit_domains,
                "_row_number": i,
            })

    except Exception as e:
        errors.append(f"Failed to parse tenant CSV: {str(e)}")

    return tenants, errors



def parse_credentials_txt_content(content: str) -> Tuple[Dict[str, Dict], List[str]]:
    """
    Parse credentials TXT from reseller.
    
    Handles THREE formats:
    
    1. Tab-separated with header:
       Username\tPassword
       admin@xxx.onmicrosoft.com\tP@ssw0rd
    
    2. Line-pair with prefixes:
       Username: admin@xxx.onmicrosoft.com
       Password: P@ssw0rd
    
    3. Alternating lines (no prefix):
       admin@xxx.onmicrosoft.com
       P@ssw0rd
    
    Returns (credentials_by_domain, errors).
    """
    errors = []
    credentials = {}

    try:
        # Normalize line endings
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in content.strip().split("\n") if line.strip()]

        if not lines:
            # Empty/no credentials file is OK — return empty dict with no errors
            return credentials, errors

        # Detect format by checking first non-empty line
        first_line = lines[0]

        # FORMAT 1: Tab-separated (with or without header)
        if "\t" in first_line:
            start_idx = 0
            # Skip header row if present
            first_lower = first_line.lower()
            if "username" in first_lower or "email" in first_lower or "password" in first_lower:
                start_idx = 1

            for i, line in enumerate(lines[start_idx:], start=start_idx + 1):
                if "\t" not in line:
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                email = parts[0].strip()
                password = parts[1].strip()
                if not email or not password:
                    continue
                if "@" not in email:
                    continue
                domain_key = email.split("@")[1].lower()
                credentials[domain_key] = {
                    "email": email,
                    "password": password,
                }

        # FORMAT 2: "Username: xxx" / "Password: xxx" line pairs
        elif first_line.lower().startswith("username:") or first_line.lower().startswith("password:"):
            current_email = None
            for line in lines:
                if line.lower().startswith("username:"):
                    current_email = line.split(":", 1)[1].strip()
                elif line.lower().startswith("password:") and current_email:
                    password = line.split(":", 1)[1].strip()
                    if "@" in current_email:
                        domain_key = current_email.split("@")[1].lower()
                        credentials[domain_key] = {
                            "email": current_email,
                            "password": password,
                        }
                    current_email = None

        # FORMAT 3: Alternating lines (email on odd lines, password on even lines)
        else:
            i = 0
            while i < len(lines) - 1:
                email = lines[i]
                password = lines[i + 1]
                if "@" in email and "onmicrosoft" in email.lower():
                    domain_key = email.split("@")[1].lower()
                    credentials[domain_key] = {
                        "email": email,
                        "password": password,
                    }
                    i += 2
                else:
                    i += 1  # Skip unrecognized lines

    except Exception as e:
        errors.append(f"Failed to parse credentials TXT: {str(e)}")

    if not credentials and not errors:
        errors.append("No valid credentials found. Expected tab-separated (Username\\tPassword) or line-pair (Username: xxx / Password: xxx) format.")

    return credentials, errors


def cross_validate(
    domains: List[Dict],
    tenants: List[Dict],
    credentials: Dict[str, Dict],
    first_name: str,
    last_name: str,
    mailboxes_per_tenant: int = 50,
    domains_per_tenant: int = 1,
) -> Dict[str, Any]:
    """
    Cross-validate all inputs and return a complete preview.
    """
    errors = []
    warnings = []

    # 1. Match credentials to tenants
    matched_count = 0
    unmatched_tenants = []
    for tenant in tenants:
        domain_key = tenant["onmicrosoft_domain"].lower()
        if domain_key in credentials:
            tenant["admin_email"] = credentials[domain_key]["email"]
            tenant["admin_password"] = credentials[domain_key]["password"]
            matched_count += 1
        elif tenant.get("password"):
            matched_count += 1
        else:
            unmatched_tenants.append(tenant["onmicrosoft_domain"])

    tenants_with_preconfigured_totp = sum(1 for t in tenants if t.get("totp_secret"))

    if unmatched_tenants:
        warnings.append(f"{len(unmatched_tenants)} tenant(s) have no matching credentials")

    # 2. Check domain-tenant capacity (allow partial fills, error only on overflow)
    max_domain_capacity = len(tenants) * domains_per_tenant
    if len(domains) > max_domain_capacity:
        errors.append(
            f"Too many domains: {len(domains)} domains exceed capacity of "
            f"{len(tenants)} tenants × {domains_per_tenant} = {max_domain_capacity}. "
            f"Reduce domain count or add more tenants."
        )
    elif len(domains) < max_domain_capacity:
        # Partial fill — ceiling division to get tenants actually used
        tenants_used = -(-len(domains) // domains_per_tenant)
        tenants_unused = len(tenants) - tenants_used
        warnings.append(
            f"{len(domains)} domains will fill {tenants_used} tenant(s); "
            f"{tenants_unused} tenant(s) will remain unused."
        )

    # 3. Per-domain effective-name check.
    # The global first/last are no longer required; each domain row may supply
    # its own. For each domain, compute the effective name (row value or
    # batch-level fallback) and flag rows where first OR last is < 2 chars.
    global_first = (first_name or "").strip()
    global_last = (last_name or "").strip()
    domains_with_custom_persona = 0
    missing_name_domains = []
    for d in domains:
        row_first = (d.get("first_name") or "").strip()
        row_last = (d.get("last_name") or "").strip()
        if row_first and row_last:
            domains_with_custom_persona += 1
        eff_first = row_first or global_first
        eff_last = row_last or global_last
        if len(eff_first) < 2 or len(eff_last) < 2:
            missing_name_domains.append(d.get("name", "?"))

    for name in missing_name_domains:
        errors.append(
            f"Row for '{name}': missing first/last name "
            f"(not in CSV and no batch default provided)"
        )

    # 3b. Range guard for mailboxes_per_tenant (must be 25-100)
    if mailboxes_per_tenant < 25 or mailboxes_per_tenant > 100:
        errors.append(f"mailboxes_per_tenant must be between 25 and 100 (got {mailboxes_per_tenant})")

    # 3c. Warn when a short GLOBAL persona name is combined with a high mailbox
    # count — the pattern generator may not produce enough unique variations.
    # Only evaluated against the global default so a blank global + per-domain
    # CSV doesn't trip this.
    if (
        mailboxes_per_tenant > 50
        and global_first
        and global_last
        and (len(global_first) + len(global_last)) < 9
    ):
        warnings.append(
            f"Short name ({global_first} {global_last}) may not generate "
            f"{mailboxes_per_tenant} unique patterns. "
            f"Consider a longer persona name or fewer mailboxes."
        )

    # 4. Calculate expected mailboxes (based on actual domains, not tenant capacity)
    expected_mailboxes = len(domains) * mailboxes_per_tenant

    # 5. Explicit "Domain N to link tenant" cross-validation
    # ------------------------------------------------------------------
    # The tenant CSV may explicitly list which custom domains belong to
    # each tenant. We must verify:
    #   (a) every explicit domain exists in the domains CSV (HARD ERROR)
    #   (b) no domain is listed on more than one tenant row (WARNING — first wins)
    #   (c) no tenant exceeds the per-tenant cap (WARNING — overflow ignored)
    domain_names_set = {(d.get("name") or "").lower() for d in domains}
    seen_explicit: Dict[str, str] = {}  # domain_name_lower -> first tenant onmicrosoft
    duplicates_reported: set = set()
    tenants_with_explicit_domains = 0
    domains_explicitly_linked = 0
    overflow_total = 0

    for tenant in tenants:
        ed = tenant.get("explicit_domains") or []
        if not ed:
            continue
        tenants_with_explicit_domains += 1
        tenant_om = tenant.get("onmicrosoft_domain", "?")

        # Cap warning
        if len(ed) > max(0, domains_per_tenant):
            overflow_count = len(ed) - max(0, domains_per_tenant)
            overflow_total += overflow_count
            warnings.append(
                f"Tenant '{tenant_om}' lists {len(ed)} explicit domains but "
                f"domains_per_tenant cap is {domains_per_tenant}; "
                f"the last {overflow_count} will be ignored."
            )
            effective_ed = ed[:max(0, domains_per_tenant)]
        else:
            effective_ed = ed

        for d_name in effective_ed:
            key = (d_name or "").lower()
            if not key:
                continue

            # (a) Hard error: explicit domain not in domains CSV
            if key not in domain_names_set:
                errors.append(
                    f"Tenant '{tenant_om}' references domain '{d_name}' "
                    f"that is not present in the domains CSV."
                )
                continue

            # (b) Warning: same domain on multiple tenants — first wins
            if key in seen_explicit and key not in duplicates_reported:
                warnings.append(
                    f"Domain '{d_name}' is assigned to multiple tenants "
                    f"('{seen_explicit[key]}' and '{tenant_om}'); "
                    f"the first occurrence wins, the rest are ignored."
                )
                duplicates_reported.add(key)
            elif key not in seen_explicit:
                seen_explicit[key] = tenant_om
                domains_explicitly_linked += 1

    # Summary stats
    domains_auto_linked = max(
        0,
        min(len(domains), len(tenants) * domains_per_tenant) - domains_explicitly_linked,
    )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "domains_count": len(domains),
            "tenants_count": len(tenants),
            "domains_per_tenant": domains_per_tenant,
            "credentials_matched": matched_count,
            "domains_linked": min(len(domains), len(tenants) * domains_per_tenant),
            "tenants_used": -(-len(domains) // domains_per_tenant) if domains_per_tenant else 0,
            "expected_mailboxes": expected_mailboxes,
            "domains_with_custom_persona": domains_with_custom_persona,
            "tenants_with_explicit_domains": tenants_with_explicit_domains,
            "domains_explicitly_linked": domains_explicitly_linked,
            "domains_auto_linked": domains_auto_linked,
            "explicit_overflow_count": overflow_total,
            "tenants_with_preconfigured_totp": tenants_with_preconfigured_totp,
            "first_login_precompleted": tenants_with_preconfigured_totp,
        }
    }
