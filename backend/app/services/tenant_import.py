"""
Tenant Import Service

Parses and merges:
- Tenant List CSV (Company info, UUIDs, optional explicit domain assignments)
- Credentials TXT (Admin emails + passwords)

IMPORTANT: Files are NOT in the same order - matching is done by onmicrosoft domain.

EXPLICIT DOMAIN ASSIGNMENT (NEW):
The tenant CSV may include optional columns named "Domain 1 to link tenant",
"Domain 2 to link tenant", ... (any column whose header contains both the word
"domain" and a digit, OR contains "link tenant"). When filled in, those exact
domain names are linked to that tenant in order. When left blank (or columns
absent), the system falls back to the legacy sequential N:1 auto-link behavior.
"""

import csv
import io
import logging
import re
import uuid as _uuid
from typing import List, Dict, Any, Optional, Tuple
from uuid import UUID
from dataclasses import dataclass

logger = logging.getLogger(__name__)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.tenant import Tenant, TenantStatus
from app.models.domain import Domain, DomainStatus


@dataclass
class CredentialData:
    """Credential data extracted from TXT file."""
    email: str
    password: str
    onmicrosoft_domain: str  # The domain part for matching


# Header patterns that identify "Domain N to link tenant" columns.
_DOMAIN_LINK_DIGIT_RE = re.compile(r"\bdomain\b.*?\d+", re.IGNORECASE)
_DOMAIN_LINK_PHRASE_RE = re.compile(r"link\s*tenant", re.IGNORECASE)


def _normalize_domain_name(raw: str) -> str:
    """
    Normalize a domain name from a CSV cell so name-based lookup is reliable.

    - Strip whitespace
    - Lowercase
    - Strip leading scheme (http://, https://)
    - Strip leading 'www.'
    - Strip trailing '/' or '.'
    - Strip surrounding quotes
    """
    if not raw:
        return ""
    s = raw.strip().strip('"').strip("'").lower()
    # Strip URL scheme
    s = re.sub(r"^https?://", "", s)
    # Strip 'www.' prefix
    if s.startswith("www."):
        s = s[4:]
    # Strip trailing slash / dot
    s = s.rstrip("/.")
    return s


def _is_explicit_domain_column(column_name: str) -> bool:
    """
    Decide whether a CSV column header is an explicit
    "Domain N to link tenant" assignment column.
    """
    if not column_name:
        return False
    name = column_name.strip()
    if not name:
        return False
    lower = name.lower()
    # Must mention "domain" somewhere — eliminates "onmicrosoft", "company name", etc.
    if "domain" not in lower:
        return False
    # Two ways to qualify:
    #  (a) "domain 1", "domain_2", "Domain3 to link tenant" — has a digit near "domain"
    #  (b) "Domain to link tenant" — explicit phrase
    return bool(_DOMAIN_LINK_DIGIT_RE.search(name) or _DOMAIN_LINK_PHRASE_RE.search(name))


def _extract_column_index(column_name: str) -> int:
    """
    Pull the digit out of a 'Domain N to link tenant' column so we can
    sort columns in user intent order (1, 2, 3, ...).
    Returns 0 if no digit found (so it sorts first / falls back to header order).
    """
    m = re.search(r"\d+", column_name or "")
    return int(m.group(0)) if m else 0


class TenantImportService:
    """Import tenants from reseller files."""
    
    def _extract_onmicrosoft_domain(self, text: str) -> Optional[str]:
        """
        Extract onmicrosoft domain from email or domain string.
        
        Examples:
        - "admin@acmecorp123.onmicrosoft.com" -> "acmecorp123.onmicrosoft.com"
        - "acmecorp123.onmicrosoft.com" -> "acmecorp123.onmicrosoft.com"
        - "acmecorp123" -> "acmecorp123.onmicrosoft.com" (prefix only)
        """
        text = text.strip().lower()
        
        # Try to extract full onmicrosoft.com domain
        match = re.search(r'([a-zA-Z0-9-]+\.onmicrosoft\.com)', text, re.IGNORECASE)
        if match:
            return match.group(1).lower()
        
        # If it looks like just a prefix (no dots, no @), add .onmicrosoft.com
        if text and '@' not in text and '.' not in text:
            return f"{text}.onmicrosoft.com"
        
        return None
    
    def _find_column(self, columns: List[str], keywords: List[str]) -> Optional[str]:
        """
        Find a column by checking if any keyword appears in the column name.
        Uses case-insensitive substring matching — same logic as
        parse_tenants_csv_content() in validation_service.py.

        Skips columns recognised as "Domain N to link tenant" assignment columns
        so they aren't misinterpreted as the onmicrosoft / name / id column.
        """
        columns_lower = [c.lower() for c in columns]
        for i, col_lower in enumerate(columns_lower):
            if _is_explicit_domain_column(columns[i]):
                continue
            for keyword in keywords:
                if keyword in col_lower:
                    return columns[i]
        return None

    def _detect_explicit_domain_columns(self, columns: List[str]) -> List[str]:
        """
        Find every "Domain N to link tenant" column in the CSV header,
        sorted by the embedded digit (Domain 1, Domain 2, Domain 3 ...).
        Returns a list of original column names in slot order.
        """
        explicit_cols = [c for c in (columns or []) if _is_explicit_domain_column(c)]
        explicit_cols.sort(key=lambda c: (_extract_column_index(c), c.lower()))
        return explicit_cols

    def parse_tenant_csv(self, csv_content: str) -> List[Dict[str, Any]]:
        """
        Parse tenant list CSV with flexible column detection.

        Matches the same logic as parse_tenants_csv_content() in
        validation_service.py so both parsers handle the same CSV
        identically.

        Each returned dict carries an `explicit_domains` key:
          - empty list  -> tenant did NOT specify domains; auto-link as before
          - non-empty   -> tenant explicitly listed these domain names (in slot
                           order); the linker will assign exactly these.
        """
        # Handle BOM
        csv_content = csv_content.replace('\ufeff', '')

        logger.info(f"Raw CSV content length: {len(csv_content)}")
        logger.info(f"CSV first 300 chars: {csv_content[:300]!r}")

        reader = csv.DictReader(io.StringIO(csv_content))
        columns = [c.strip() for c in (reader.fieldnames or [])]

        logger.info(f"CSV headers found: {columns}")

        # --- Detect explicit "Domain N to link tenant" columns FIRST so the
        # general column matcher below can ignore them. ---
        explicit_domain_cols = self._detect_explicit_domain_columns(columns)
        if explicit_domain_cols:
            logger.info(f"Explicit domain assignment columns detected (in order): {explicit_domain_cols}")

        # --- Flexible column detection (mirrors validation_service.py) ---
        # Onmicrosoft column
        onmicrosoft_col = self._find_column(
            columns, ["onmicrosoft", "username", "email", "pattern", "domain"]
        )
        # If the matcher picked up a stray "domain" column that isn't
        # an onmicrosoft one, prefer a smarter fallback below.
        if onmicrosoft_col and "onmicrosoft" not in onmicrosoft_col.lower() \
                and "username" not in onmicrosoft_col.lower() \
                and "email" not in onmicrosoft_col.lower() \
                and "pattern" not in onmicrosoft_col.lower():
            # picked something like "Domain ID" — keep as last resort but try
            # the data-row scan first.
            tentative_om_col = onmicrosoft_col
            onmicrosoft_col = None
        else:
            tentative_om_col = None

        # Fallback: scan first data row for a value containing onmicrosoft.com
        if not onmicrosoft_col:
            peek_reader = csv.DictReader(io.StringIO(csv_content))
            for peek_row in peek_reader:
                for col, val in peek_row.items():
                    if val and "onmicrosoft.com" in val.lower():
                        onmicrosoft_col = col
                        break
                break

        # If we still don't have one, fall back to the tentative pick.
        if not onmicrosoft_col and tentative_om_col:
            onmicrosoft_col = tentative_om_col

        # Tenant / company name column
        name_col = self._find_column(columns, ["company", "name", "tenant"])

        # Tenant ID column
        id_col = self._find_column(columns, ["uuid", "tenant_id", "id"])

        # Password column (may be embedded in the CSV)
        password_col = self._find_column(columns, ["password"])

        # Admin email column (may be embedded in the CSV)
        admin_email_col = self._find_column(columns, ["admin_email", "admin email"])
        # Avoid matching the name column again — admin_email_col should
        # contain "email"; if _find_column matched something without "email"
        # in it, clear it.
        if admin_email_col and "email" not in admin_email_col.lower():
            admin_email_col = None

        # Contact / address columns (optional — not every CSV has these)
        address_col = self._find_column(columns, ["address"])
        contact_name_col = self._find_column(columns, ["contact_name", "contact name", "admin name"])
        contact_email_col = self._find_column(columns, ["contact_email", "contact email", "gmail"])
        contact_phone_col = self._find_column(columns, ["phone"])

        # Provider column (optional)
        provider_col = self._find_column(columns, ["provider"])

        logger.info(
            f"Column mapping: onmicrosoft={onmicrosoft_col}, name={name_col}, "
            f"id={id_col}, password={password_col}, admin_email={admin_email_col}, "
            f"address={address_col}, contact_name={contact_name_col}, "
            f"contact_email={contact_email_col}, phone={contact_phone_col}, "
            f"provider={provider_col}, explicit_domain_cols={explicit_domain_cols}"
        )

        if not onmicrosoft_col:
            logger.error(
                f"Tenant CSV: could not find onmicrosoft column. Headers: {columns}"
            )
            return []

        tenants = []
        for row in reader:
            clean = {k.strip(): v.strip() for k, v in row.items() if k}

            # --- Extract onmicrosoft domain ---
            onmicrosoft_raw = clean.get(onmicrosoft_col, "").strip()
            if not onmicrosoft_raw:
                continue

            onmicrosoft_domain = self._extract_onmicrosoft_domain(onmicrosoft_raw)

            if not onmicrosoft_domain:
                # Fallback: scan all values for onmicrosoft.com
                for value in clean.values():
                    if value and "onmicrosoft" in value.lower():
                        onmicrosoft_domain = self._extract_onmicrosoft_domain(value)
                        if onmicrosoft_domain:
                            break

            if not onmicrosoft_domain:
                continue

            # --- Build tenant dict ---
            company_name = clean.get(name_col, "").strip() if name_col else ""
            if not company_name:
                # Derive from onmicrosoft prefix
                company_name = onmicrosoft_domain.split(".")[0]

            # --- Extract explicit domain assignments (in slot order) ---
            explicit_domains: List[str] = []
            for col in explicit_domain_cols:
                raw_val = clean.get(col, "")
                normalized = _normalize_domain_name(raw_val)
                if normalized:
                    explicit_domains.append(normalized)

            tenant = {
                "company_name": company_name,
                "onmicrosoft_domain": onmicrosoft_domain,
                "address": clean.get(address_col, "").strip() if address_col else "",
                "contact_name": clean.get(contact_name_col, "").strip() if contact_name_col else "",
                "contact_email": clean.get(contact_email_col, "").strip() if contact_email_col else "",
                "contact_phone": clean.get(contact_phone_col, "").strip() if contact_phone_col else "",
                "tenant_id": clean.get(id_col, "").strip() if id_col else "",
                "admin_email": clean.get(admin_email_col, "").strip() if admin_email_col else "",
                "admin_password": clean.get(password_col, "").strip() if password_col else "",
                "provider": clean.get(provider_col, "").strip() if provider_col else "",
                "explicit_domains": explicit_domains,
            }

            tenants.append(tenant)

        logger.info(
            f"parse_tenant_csv: parsed {len(tenants)} tenants "
            f"({sum(1 for t in tenants if t['explicit_domains'])} with explicit domain assignments)"
        )
        return tenants
    
    def parse_credentials_txt(self, txt_content: str) -> Dict[str, CredentialData]:
        """
        Parse credentials TXT file.
        
        Supports TWO formats:
        
        1. Tab-separated (legacy):
           Username<TAB>Password
           admin@xxx.onmicrosoft.com<TAB>P@ssw0rd
        
        2. Line-pair format:
           admin@company1.onmicrosoft.com
           Password123
           admin@company2.onmicrosoft.com
           Password456
        
        Returns: Dict mapping onmicrosoft_domain -> CredentialData
        """
        credentials = {}
        lines = [line.strip() for line in txt_content.strip().split('\n')]
        
        # Filter out empty lines and header
        lines = [l for l in lines if l and not l.lower().startswith('username')]
        
        # Detect format: if first non-empty line has tab, it's tab-separated
        is_tab_separated = any('\t' in line for line in lines[:5])
        
        if is_tab_separated:
            # Tab-separated format
            for line in lines:
                parts = line.split('\t')
                if len(parts) >= 2:
                    email = parts[0].strip()
                    password = parts[1].strip()
                    
                    domain = self._extract_onmicrosoft_domain(email)
                    if domain and email and password:
                        credentials[domain] = CredentialData(
                            email=email,
                            password=password,
                            onmicrosoft_domain=domain
                        )
        else:
            # Line-pair format (email on one line, password on next)
            i = 0
            while i < len(lines) - 1:
                email = lines[i]
                password = lines[i + 1]
                
                # Check if this looks like an email (contains @)
                if '@' in email and 'onmicrosoft.com' in email.lower():
                    domain = self._extract_onmicrosoft_domain(email)
                    if domain:
                        credentials[domain] = CredentialData(
                            email=email,
                            password=password,
                            onmicrosoft_domain=domain
                        )
                    i += 2  # Move to next pair
                else:
                    i += 1  # Skip this line, try next
        
        return credentials
    
    def merge_data(
        self,
        tenant_list: List[Dict[str, Any]],
        credentials: Dict[str, CredentialData]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """
        Merge tenant list with credentials by onmicrosoft domain.
        
        IMPORTANT: Matches by domain, NOT by row position.
        
        Returns:
            - merged: List of complete tenant records with credentials
              (each carries an `explicit_domains` list, possibly empty)
            - unmatched_tenants: Tenants without matching credentials
            - unmatched_creds: Credential domains without matching tenant
        """
        merged = []
        unmatched_tenants = []
        used_domains = set()
        
        for tenant in tenant_list:
            domain = tenant["onmicrosoft_domain"].lower()
            
            # Sanitise microsoft_tenant_id: blank/empty → unique placeholder
            # so multiple rows with no ID don't violate the DB unique constraint.
            raw_tid = tenant.get("tenant_id", "").strip()
            ms_tenant_id = raw_tid if raw_tid else f"PENDING-{_uuid.uuid4()}"

            explicit_domains = list(tenant.get("explicit_domains") or [])

            # Look up credentials by domain
            cred = credentials.get(domain)
            
            if cred:
                # Found matching credentials in TXT file
                merged.append({
                    "name": tenant["company_name"],
                    "onmicrosoft_domain": tenant["onmicrosoft_domain"],
                    "microsoft_tenant_id": ms_tenant_id,
                    "address": tenant["address"],
                    "contact_name": tenant["contact_name"],
                    "contact_email": tenant["contact_email"],
                    "contact_phone": tenant["contact_phone"],
                    "admin_email": cred.email,
                    "admin_password": cred.password,
                    "provider": tenant.get("provider", ""),
                    "explicit_domains": explicit_domains,
                })
                used_domains.add(domain)
            else:
                # No credentials in TXT — fall back to embedded CSV columns
                csv_email = tenant.get("admin_email", "")
                csv_password = tenant.get("admin_password", "")

                if not csv_email and not csv_password:
                    unmatched_tenants.append(tenant)

                merged.append({
                    "name": tenant["company_name"],
                    "onmicrosoft_domain": tenant["onmicrosoft_domain"],
                    "microsoft_tenant_id": ms_tenant_id,
                    "address": tenant["address"],
                    "contact_name": tenant["contact_name"],
                    "contact_email": tenant["contact_email"],
                    "contact_phone": tenant["contact_phone"],
                    "admin_email": csv_email or f"admin@{tenant['onmicrosoft_domain']}",
                    "admin_password": csv_password or "",
                    "provider": tenant.get("provider", ""),
                    "explicit_domains": explicit_domains,
                })
        
        # Find credentials that weren't matched to any tenant
        unmatched_creds = [
            domain for domain in credentials.keys()
            if domain not in used_domains
        ]
        
        return merged, unmatched_tenants, unmatched_creds
    
    async def import_tenants(
        self,
        db: AsyncSession,
        batch_id: UUID,
        csv_content: str,
        credentials_content: str,
        provider: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Import tenants from both files.

        Matches by onmicrosoft domain, NOT by row position.

        Only imports as many tenants as there are domains needing tenants
        in the batch (domains without a linked tenant). Tenants already
        in the system are skipped and do not count toward this limit.

        Returns the standard summary dict plus a NEW key `explicit_domain_map`
        keyed by `onmicrosoft_domain` -> [normalized domain names]. The caller
        (pipeline / wizard) should resolve these to tenant UUIDs after commit
        and pass them to `auto_link_domains(..., explicit_map=...)`.
        """

        # Count total domains in this batch (import all tenants up to domain count)
        total_domains_in_batch = await db.scalar(
            select(func.count(Domain.id)).where(Domain.batch_id == batch_id)
        ) or 0
        needed_count = total_domains_in_batch
        logger.info(f"Tenant import: needed_count={needed_count}, batch_id={batch_id}")

        tenant_list = self.parse_tenant_csv(csv_content)
        credentials = self.parse_credentials_txt(credentials_content)
        merged, unmatched_tenants, unmatched_creds = self.merge_data(tenant_list, credentials)
        logger.info(
            f"Tenant import parse results: csv_tenants={len(tenant_list)}, "
            f"credentials={len(credentials)}, merged={len(merged)}, "
            f"unmatched_tenants={len(unmatched_tenants)}, unmatched_creds={len(unmatched_creds)}"
        )

        # Build the explicit-domain map keyed by onmicrosoft_domain (case-insensitive).
        # The pipeline resolves these to tenant UUIDs after commit and feeds
        # them into auto_link_domains(...).
        explicit_domain_map: Dict[str, List[str]] = {}
        for data in merged:
            ed = data.get("explicit_domains") or []
            if ed:
                explicit_domain_map[data["onmicrosoft_domain"].lower()] = list(ed)

        imported = 0
        skipped = 0
        reassigned = 0
        skipped_limit = 0
        skipped_empty = 0
        missing_pwd = 0

        for data in merged:
            # Skip guard: if both microsoft_tenant_id is missing/placeholder
            # AND onmicrosoft_domain is empty, there's nothing useful to import.
            tid = data.get("microsoft_tenant_id", "")
            tid_is_empty = (not tid) or tid.startswith("PENDING-")
            domain_is_empty = not data.get("onmicrosoft_domain", "").strip()
            if tid_is_empty and domain_is_empty:
                logger.warning(
                    f"Skipping row with no microsoft_tenant_id and no "
                    f"onmicrosoft_domain: {data.get('name', '?')}"
                )
                skipped_empty += 1
                continue

            # Stop importing once we have enough tenants for the available domains
            if imported >= needed_count:
                skipped_limit += 1
                continue

            # Check duplicate by tenant ID or domain
            existing = None
            if data["microsoft_tenant_id"]:
                existing = await db.execute(
                    select(Tenant).where(
                        Tenant.microsoft_tenant_id == data["microsoft_tenant_id"]
                    )
                )
                existing = existing.scalar_one_or_none()

            if not existing:
                # Also check by domain
                existing = await db.execute(
                    select(Tenant).where(
                        Tenant.onmicrosoft_domain == data["onmicrosoft_domain"]
                    )
                )
                existing = existing.scalar_one_or_none()

            if existing:
                if existing.batch_id == batch_id:
                    skipped += 1
                    continue
                else:
                    # Tenant exists in a different batch - reassign to current batch
                    existing.batch_id = batch_id
                    existing.name = data["name"]
                    existing.onmicrosoft_domain = data["onmicrosoft_domain"]
                    existing.microsoft_tenant_id = data["microsoft_tenant_id"]
                    existing.address = data["address"]
                    existing.contact_name = data["contact_name"]
                    existing.contact_email = data["contact_email"]
                    existing.contact_phone = data["contact_phone"]
                    existing.admin_email = data["admin_email"]
                    if data["admin_password"]:
                        existing.admin_password = data["admin_password"]
                        existing.initial_password = data["admin_password"]
                    existing.provider = provider or data.get("provider") or existing.provider
                    existing.status = TenantStatus.IMPORTED

                    # === CRITICAL: Reset ALL step progress flags ===
                    # When a tenant moves to a new batch, it must go through
                    # ALL steps again from scratch.
                    existing.first_login_completed = False
                    existing.first_login_at = None
                    existing.password_changed = False
                    existing.setup_error = None
                    existing.setup_step = None

                    # Step 5 (M365 domain setup)
                    existing.domain_verified_in_m365 = False
                    if hasattr(existing, 'step5_complete'):
                        existing.step5_complete = False
                    if hasattr(existing, 'step5_retry_count'):
                        existing.step5_retry_count = 0

                    # Step 6 (Mailbox creation)
                    if hasattr(existing, 'step6_complete'):
                        existing.step6_complete = False
                    if hasattr(existing, 'step6_started'):
                        existing.step6_started = False
                    if hasattr(existing, 'step6_error'):
                        existing.step6_error = None
                    if hasattr(existing, 'step6_retry_count'):
                        existing.step6_retry_count = 0

                    # Step 7 (SMTP Auth)
                    if hasattr(existing, 'step7_complete'):
                        existing.step7_complete = False
                    if hasattr(existing, 'step7_smtp_auth_enabled'):
                        existing.step7_smtp_auth_enabled = False
                    if hasattr(existing, 'step7_error'):
                        existing.step7_error = None
                    if hasattr(existing, 'step7_retry_count'):
                        existing.step7_retry_count = 0
                    if hasattr(existing, 'step7_app_consent_granted'):
                        existing.step7_app_consent_granted = False

                    # Retry counts
                    if hasattr(existing, 'step4_retry_count'):
                        existing.step4_retry_count = 0

                    # Licensed user (Step 6 creates this)
                    if hasattr(existing, 'licensed_user_created'):
                        existing.licensed_user_created = False
                    if hasattr(existing, 'licensed_user_upn'):
                        existing.licensed_user_upn = None

                    # DO NOT reset these — they're permanent tenant properties:
                    # - totp_secret (needed for all future logins)
                    # - admin_password (keep the CURRENT working password)
                    # - security_defaults_disabled (permanent M365 setting)

                    # Clear domain linkage since it's a new batch
                    if existing.domain_id:
                        old_domain = await db.execute(
                            select(Domain).where(Domain.id == existing.domain_id)
                        )
                        old_domain = old_domain.scalar_one_or_none()
                        if old_domain:
                            old_domain.tenant_id = None
                    existing.domain_id = None
                    existing.custom_domain = None
                    reassigned += 1
                    imported += 1
                    continue

            if not data["admin_password"]:
                missing_pwd += 1

            initial_pwd = data["admin_password"] or ""

            tenant = Tenant(
                batch_id=batch_id,
                name=data["name"],
                onmicrosoft_domain=data["onmicrosoft_domain"],
                microsoft_tenant_id=data["microsoft_tenant_id"],
                address=data["address"],
                contact_name=data["contact_name"],
                contact_email=data["contact_email"],
                contact_phone=data["contact_phone"],
                admin_email=data["admin_email"],
                admin_password=initial_pwd,       # Current password (will be updated after change)
                initial_password=initial_pwd,     # Original password (never changes)
                provider=provider or data.get("provider") or "Unknown",
                status=TenantStatus.IMPORTED
            )
            db.add(tenant)
            imported += 1

        logger.info(
            f"Tenant import pre-commit: imported={imported}, skipped_dup={skipped}, "
            f"reassigned={reassigned}, skipped_limit={skipped_limit}, "
            f"skipped_empty={skipped_empty}, missing_pwd={missing_pwd}, "
            f"explicit_assignments={len(explicit_domain_map)}"
        )
        await db.commit()
        logger.info(f"Tenant import: db.commit() completed successfully for batch {batch_id}")

        return {
            "total_csv": len(tenant_list),
            "total_credentials": len(credentials),
            "imported": imported,
            "skipped_duplicate": skipped,
            "reassigned": reassigned,
            "skipped_not_needed": skipped_limit,
            "domains_needing_tenants": needed_count,
            "skipped_empty_rows": skipped_empty,
            "missing_password": missing_pwd,
            "unmatched_tenants": len(unmatched_tenants),
            "unmatched_credentials": len(unmatched_creds),
            "explicit_domain_map": explicit_domain_map,  # NEW
            "warnings": {
                "tenants_without_creds": [t["onmicrosoft_domain"] for t in unmatched_tenants],
                "creds_without_tenant": unmatched_creds
            }
        }
    
    async def auto_link_domains(
        self,
        db: AsyncSession,
        batch_id: UUID,
        domains_per_tenant: int = 1,
        explicit_map: Optional[Dict[str, List[str]]] = None,
    ) -> Dict[str, Any]:
        """
        Link domains to tenants for a batch.

        Two phases:

        1. **Explicit phase** — when `explicit_map` is provided. The map is
           keyed by tenant onmicrosoft_domain (lowercase) and gives an
           ordered list of normalized custom-domain names to assign to that
           tenant. Each named domain must (a) exist in this batch and
           (b) not already be linked to a different tenant. Up to
           `domains_per_tenant` slots per tenant are honored — anything
           beyond that is ignored with a warning.

        2. **Auto-fill phase** — exactly the legacy N:1 sequential grouping,
           but operating only on the *remaining* unlinked domains and the
           *remaining* tenants that didn't receive any explicit assignment.

        Returns a structured summary:
            {
              "linked": int,                  # total domains linked across both phases
              "linked_explicit": int,         # phase 1 count
              "linked_auto": int,             # phase 2 count
              "domains_per_tenant": int,
              "unmatched_domains": [str],     # explicit names not found in batch
              "conflicting_domains": [str],   # explicit names already on another tenant
              "overflow_domains": [str],      # explicit names beyond domains_per_tenant cap
              "tenants_with_explicit": int,
            }
        """

        explicit_map = explicit_map or {}

        # Get all tenants for this batch, ordered by creation time
        tenants_result = await db.execute(
            select(Tenant)
            .where(Tenant.batch_id == batch_id)
            .order_by(Tenant.created_at)
        )
        tenants: List[Tenant] = list(tenants_result.scalars().all())

        # Get all domains for this batch, ordered by creation time, indexed by name
        domains_result = await db.execute(
            select(Domain)
            .where(Domain.batch_id == batch_id)
            .order_by(Domain.created_at)
        )
        all_domains: List[Domain] = list(domains_result.scalars().all())
        domains_by_name: Dict[str, Domain] = {d.name.lower(): d for d in all_domains}

        unmatched_domains: List[str] = []
        conflicting_domains: List[str] = []
        overflow_domains: List[str] = []
        linked_explicit = 0
        explicitly_assigned_tenant_ids: set = set()

        # --- Phase 1: explicit assignment ---
        if explicit_map:
            # Index tenants by onmicrosoft_domain so we can resolve the map
            tenants_by_om: Dict[str, Tenant] = {
                t.onmicrosoft_domain.lower(): t for t in tenants if t.onmicrosoft_domain
            }

            for om_key, raw_domain_names in explicit_map.items():
                tenant = tenants_by_om.get(om_key.lower())
                if not tenant:
                    # Tenant referenced in the map but not in the DB for this batch.
                    # The names here aren't custom domains — they're tenant keys —
                    # so report them under "unmatched_domains" only if they look
                    # like a custom domain. Just skip silently otherwise.
                    logger.warning(
                        f"auto_link_domains: tenant key '{om_key}' from explicit "
                        f"map not found in batch {batch_id}; skipping its assignments"
                    )
                    continue

                if not raw_domain_names:
                    continue

                explicitly_assigned_tenant_ids.add(tenant.id)

                # Cap to domains_per_tenant (overflow is reported, not linked)
                effective = list(raw_domain_names)[:max(0, domains_per_tenant)]
                overflow = list(raw_domain_names)[max(0, domains_per_tenant):]
                for d_name in overflow:
                    overflow_domains.append(d_name)

                slot = 0
                first_linked: Optional[Domain] = None
                for d_name in effective:
                    key = (d_name or "").lower()
                    domain_obj = domains_by_name.get(key)
                    if not domain_obj:
                        unmatched_domains.append(d_name)
                        logger.warning(
                            f"auto_link_domains: explicit domain '{d_name}' for tenant "
                            f"'{tenant.onmicrosoft_domain}' not found in batch {batch_id}"
                        )
                        continue

                    if domain_obj.tenant_id and domain_obj.tenant_id != tenant.id:
                        conflicting_domains.append(d_name)
                        logger.warning(
                            f"auto_link_domains: explicit domain '{d_name}' for tenant "
                            f"'{tenant.onmicrosoft_domain}' is already linked to a "
                            f"different tenant; skipping"
                        )
                        continue

                    domain_obj.tenant_id = tenant.id
                    domain_obj.domain_index_in_tenant = slot
                    domain_obj.status = DomainStatus.TENANT_LINKED
                    if first_linked is None:
                        first_linked = domain_obj
                    slot += 1
                    linked_explicit += 1

                if first_linked is not None:
                    tenant.domain_id = first_linked.id
                    tenant.custom_domain = first_linked.name

        # --- Phase 2: auto-fill remaining ---
        # Refresh which domains are still unlinked AFTER phase 1.
        unlinked_domains = [d for d in all_domains if d.tenant_id is None]
        # Tenants that didn't receive an explicit assignment AND don't already
        # have any domain linked (defensive — covers retried/resumed batches).
        eligible_tenant_ids: set = set()
        for t in tenants:
            if t.id in explicitly_assigned_tenant_ids:
                continue
            # Skip tenants that already have at least one domain linked.
            already = next((d for d in all_domains if d.tenant_id == t.id), None)
            if already is not None:
                continue
            eligible_tenant_ids.add(t.id)

        eligible_tenants = [t for t in tenants if t.id in eligible_tenant_ids]

        linked_auto = 0
        per = max(1, int(domains_per_tenant or 1))
        for tenant_idx, tenant in enumerate(eligible_tenants):
            group_start = tenant_idx * per
            group_end = group_start + per
            tenant_domains = unlinked_domains[group_start:group_end]
            if not tenant_domains:
                break

            for domain_position, domain in enumerate(tenant_domains):
                domain.tenant_id = tenant.id
                domain.domain_index_in_tenant = domain_position
                domain.status = DomainStatus.TENANT_LINKED
                linked_auto += 1

            tenant.domain_id = tenant_domains[0].id
            tenant.custom_domain = tenant_domains[0].name

        await db.flush()

        result = {
            "linked": linked_explicit + linked_auto,
            "linked_explicit": linked_explicit,
            "linked_auto": linked_auto,
            "domains_per_tenant": per,
            "unmatched_domains": unmatched_domains,
            "conflicting_domains": conflicting_domains,
            "overflow_domains": overflow_domains,
            "tenants_with_explicit": len(explicitly_assigned_tenant_ids),
        }
        logger.info(f"auto_link_domains result: {result}")
        return result


tenant_import_service = TenantImportService()
