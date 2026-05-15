"""Add or link custom domains to existing tenants from a CSV mapping.

This is for small correction/import jobs where tenants already exist and the
operator has a spreadsheet with explicit domain slots per tenant.

Default mode is dry-run. Pass --apply to write changes.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Assignment:
    row_number: int
    admin_email: str
    domain: str
    domain_index: int | None
    redirect_url: str | None = None


@dataclass(frozen=True)
class PlannedLink:
    admin_email: str
    tenant_id: UUID
    batch_id: UUID
    domain: str
    domain_index: int
    operation: str
    redirect_url: str | None


def normalize_domain(raw: str) -> str:
    value = (raw or "").strip().strip('"').strip("'").lower()
    value = re.sub(r"^https?://", "", value)
    if value.startswith("www."):
        value = value[4:]
    return value.rstrip("/.")


def normalize_email(raw: str) -> str:
    return (raw or "").strip().strip('"').strip("'").lower()


def normalized_header(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (raw or "").strip().lower()).strip()


def find_admin_column(fieldnames: list[str]) -> str | None:
    preferred = {
        "admin email",
        "admin user",
        "admin username",
        "username",
        "user",
    }
    for field in fieldnames:
        if normalized_header(field) in preferred:
            return field
    for field in fieldnames:
        header = normalized_header(field)
        if "admin" in header and ("email" in header or "user" in header):
            return field
    for field in fieldnames:
        header = normalized_header(field)
        if "email" in header and "contact" not in header:
            return field
    return None


def domain_column_index(fieldname: str) -> int | None | bool:
    """Return explicit zero-based slot, None for generic domain, False if not domain."""
    header = normalized_header(fieldname)
    if not header or "onmicrosoft" in header:
        return False
    if "domain" not in header:
        return False

    match = re.search(r"\b(\d+)\b", header)
    if match:
        return max(0, int(match.group(1)) - 1)

    if header in {"domain", "domain name", "custom domain"}:
        return None

    return False


def find_redirect_column(fieldnames: list[str]) -> str | None:
    for field in fieldnames:
        header = normalized_header(field)
        if header in {"redirect", "redirect url", "redirect to", "url"}:
            return field
    return None


def find_index_column(fieldnames: list[str]) -> str | None:
    for field in fieldnames:
        header = normalized_header(field)
        if header in {"domain index", "domain slot", "slot", "index"}:
            return field
    return None


def parse_index(raw: str) -> int | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid domain_index value '{raw}'") from exc
    if parsed < 0:
        raise ValueError(f"Invalid domain_index value '{raw}'")
    return parsed


def load_assignments(path: Path, default_redirect_url: str | None) -> list[Assignment]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            raise ValueError("Mapping CSV has no header row")

        admin_col = find_admin_column(fieldnames)
        if not admin_col:
            raise ValueError(
                "Mapping CSV needs an admin column, such as 'Admin User' or 'admin_email'"
            )

        redirect_col = find_redirect_column(fieldnames)
        index_col = find_index_column(fieldnames)

        candidates = [
            (field, domain_column_index(field))
            for field in fieldnames
        ]
        explicit_domain_cols = [
            (field, index)
            for field, index in candidates
            if type(index) is int
        ]
        generic_domain_cols = [
            field
            for field, index in candidates
            if index is None
        ]

        if explicit_domain_cols:
            domain_cols: list[tuple[str, int | None]] = explicit_domain_cols
        elif generic_domain_cols:
            domain_cols = [(generic_domain_cols[0], None)]
        else:
            raise ValueError(
                "Mapping CSV needs domain columns, such as 'Domain 1', 'Domain 2', "
                "or a single 'domain' column"
            )

        assignments: list[Assignment] = []
        seen_pairs: set[tuple[str, str, int | None]] = set()
        seen_domains: dict[str, str] = {}

        for row_number, row in enumerate(reader, start=2):
            admin_email = normalize_email(row.get(admin_col, ""))
            if not admin_email:
                continue
            if "@" not in admin_email:
                raise ValueError(f"Row {row_number}: admin email '{admin_email}' is invalid")

            redirect_url = (
                (row.get(redirect_col, "") if redirect_col else "").strip()
                or default_redirect_url
                or None
            )

            for domain_col, explicit_index in domain_cols:
                domain = normalize_domain(row.get(domain_col, ""))
                if not domain:
                    continue
                if not DOMAIN_RE.match(domain):
                    raise ValueError(f"Row {row_number}: domain '{domain}' is invalid")

                domain_index = explicit_index
                if domain_index is None and index_col:
                    domain_index = parse_index(row.get(index_col, ""))

                pair = (admin_email, domain, domain_index)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                previous_admin = seen_domains.get(domain)
                if previous_admin and previous_admin != admin_email:
                    raise ValueError(
                        f"Domain '{domain}' is assigned to both {previous_admin} and {admin_email}"
                    )
                seen_domains[domain] = admin_email

                assignments.append(
                    Assignment(
                        row_number=row_number,
                        admin_email=admin_email,
                        domain=domain,
                        domain_index=domain_index,
                        redirect_url=redirect_url,
                    )
                )

    return assignments


def load_environment(env_file: str | None) -> None:
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv(ROOT / ".env")

    # Some deployment envs use values like DEBUG=release. The app expects bool.
    if os.getenv("DEBUG") not in (None, "", "true", "false", "True", "False", "1", "0"):
        os.environ["DEBUG"] = "false"


def reset_domain_progress(domain) -> None:
    domain.domain_added_to_m365 = False
    domain.m365_verification_txt = None
    domain.domain_verified_in_m365 = False
    domain.domain_verified_at = None
    domain.mx_record_added = False
    domain.spf_record_added = False
    domain.autodiscover_added = False
    domain.mx_value = None
    domain.spf_value = None
    domain.dkim_selector1 = None
    domain.dkim_selector2 = None
    domain.dkim_selector1_cname = None
    domain.dkim_selector2_cname = None
    domain.dkim_cnames_added = False
    domain.dkim_enabled = False
    domain.dkim_enabled_at = None
    domain.step5_complete = False
    domain.step5_retry_count = 0
    domain.step5_skipped = False
    domain.licensed_user_upn = None
    domain.licensed_user_password = None
    domain.licensed_user_created = False
    domain.licensed_user_id = None
    domain.step6_complete = False
    domain.step6_mailboxes_created = 0
    domain.step6_skipped = False
    domain.error_message = None


async def plan_and_apply(args: argparse.Namespace, assignments: list[Assignment]) -> int:
    from sqlalchemy import func, select

    from app.db.session import async_session_factory
    from app.models.batch import SetupBatch
    from app.models.domain import Domain, DomainStatus
    from app.models.tenant import Tenant

    if not assignments:
        print("No assignments found in mapping CSV.")
        return 0

    admin_emails = sorted({assignment.admin_email for assignment in assignments})
    domain_names = sorted({assignment.domain for assignment in assignments})

    async with async_session_factory() as db:
        tenant_rows = (
            await db.execute(
                select(Tenant).where(func.lower(Tenant.admin_email).in_(admin_emails))
            )
        ).scalars().all()

        tenants_by_admin: dict[str, Tenant] = {}
        duplicate_admins: set[str] = set()
        for tenant in tenant_rows:
            key = (tenant.admin_email or "").lower()
            if key in tenants_by_admin:
                duplicate_admins.add(key)
            tenants_by_admin[key] = tenant

        domain_rows = (
            await db.execute(
                select(Domain).where(func.lower(Domain.name).in_(domain_names))
            )
        ).scalars().all()
        domains_by_name = {domain.name.lower(): domain for domain in domain_rows}

        affected_tenant_ids = [tenant.id for tenant in tenants_by_admin.values()]
        linked_domains = []
        if affected_tenant_ids:
            linked_domains = (
                await db.execute(
                    select(Domain).where(Domain.tenant_id.in_(affected_tenant_ids))
                )
            ).scalars().all()

        occupied: dict[UUID, dict[int, str]] = {}
        for domain in linked_domains:
            occupied.setdefault(domain.tenant_id, {})[domain.domain_index_in_tenant or 0] = domain.name.lower()

        errors: list[str] = []
        plans: list[PlannedLink] = []

        if duplicate_admins:
            errors.append(
                "Duplicate tenants found for admin email(s): "
                + ", ".join(sorted(duplicate_admins))
            )

        for assignment in assignments:
            tenant = tenants_by_admin.get(assignment.admin_email)
            if not tenant:
                errors.append(
                    f"Row {assignment.row_number}: tenant not found for {assignment.admin_email}"
                )
                continue
            if not tenant.batch_id:
                errors.append(
                    f"Row {assignment.row_number}: tenant {assignment.admin_email} has no batch_id"
                )
                continue

            domain = domains_by_name.get(assignment.domain)
            if domain and domain.tenant_id and domain.tenant_id != tenant.id and not args.reassign:
                errors.append(
                    f"Row {assignment.row_number}: domain {assignment.domain} is already linked "
                    "to another tenant; pass --reassign if that is intended"
                )
                continue
            if domain and domain.batch_id and domain.batch_id != tenant.batch_id and not args.reassign:
                errors.append(
                    f"Row {assignment.row_number}: domain {assignment.domain} belongs to another "
                    "batch; pass --reassign if that is intended"
                )
                continue

            tenant_slots = occupied.setdefault(tenant.id, {})
            if assignment.domain_index is None:
                domain_index = 0
                while domain_index in tenant_slots:
                    domain_index += 1
            else:
                domain_index = assignment.domain_index

            occupant = tenant_slots.get(domain_index)
            if occupant and occupant != assignment.domain:
                errors.append(
                    f"Row {assignment.row_number}: {assignment.admin_email} already has "
                    f"{occupant} in domain slot {domain_index + 1}; refusing to overwrite"
                )
                continue

            old_index = domain.domain_index_in_tenant if domain and domain.tenant_id == tenant.id else None
            if old_index is not None and old_index in tenant_slots:
                tenant_slots.pop(old_index, None)
            tenant_slots[domain_index] = assignment.domain

            if not domain:
                operation = "create_and_link"
            elif domain.tenant_id == tenant.id:
                operation = "already_linked_update_slot"
            elif domain.tenant_id:
                operation = "reassign_and_link"
            else:
                operation = "link_existing"

            plans.append(
                PlannedLink(
                    admin_email=assignment.admin_email,
                    tenant_id=tenant.id,
                    batch_id=tenant.batch_id,
                    domain=assignment.domain,
                    domain_index=domain_index,
                    operation=operation,
                    redirect_url=assignment.redirect_url,
                )
            )

        print(
            {
                "mode": "apply" if args.apply else "dry_run",
                "assignments": len(assignments),
                "planned": len(plans),
                "errors": len(errors),
            }
        )
        for plan in plans:
            print(
                {
                    "operation": plan.operation,
                    "admin_email": plan.admin_email,
                    "domain": plan.domain,
                    "slot": plan.domain_index + 1,
                    "tenant_id": str(plan.tenant_id),
                    "batch_id": str(plan.batch_id),
                }
            )

        if errors:
            print("ERRORS")
            for error in errors:
                print(f"- {error}")
            await db.rollback()
            return 2

        if not args.apply:
            print("Dry run complete. Re-run with --apply to write these changes.")
            await db.rollback()
            return 0

        max_slot_by_batch: dict[UUID, int] = {}
        for plan in plans:
            tenant = tenants_by_admin[plan.admin_email]
            domain = domains_by_name.get(plan.domain)

            if not domain:
                tld = plan.domain.rsplit(".", 1)[1] if "." in plan.domain else ""
                domain = Domain(
                    batch_id=plan.batch_id,
                    name=plan.domain,
                    tld=tld,
                    status=DomainStatus.PURCHASED,
                    redirect_url=plan.redirect_url,
                    cloudflare_zone_status="pending",
                    cloudflare_nameservers=[],
                )
                db.add(domain)
                await db.flush()
                domains_by_name[plan.domain] = domain
            elif args.reset_existing_progress:
                reset_domain_progress(domain)

            if domain.tenant_id and domain.tenant_id != tenant.id:
                old_tenant = await db.get(Tenant, domain.tenant_id)
                if old_tenant and old_tenant.domain_id == domain.id:
                    replacement = (
                        await db.execute(
                            select(Domain)
                            .where(Domain.tenant_id == old_tenant.id, Domain.id != domain.id)
                            .order_by(Domain.domain_index_in_tenant)
                        )
                    ).scalars().first()
                    old_tenant.domain_id = replacement.id if replacement else None
                    old_tenant.custom_domain = replacement.name if replacement else None

            domain.batch_id = plan.batch_id
            domain.tenant_id = tenant.id
            domain.domain_index_in_tenant = plan.domain_index
            domain.status = DomainStatus.TENANT_LINKED
            if plan.redirect_url:
                domain.redirect_url = plan.redirect_url

            if not tenant.domain_id or plan.domain_index == 0:
                tenant.domain_id = domain.id
                tenant.custom_domain = domain.name

            max_slot_by_batch[plan.batch_id] = max(
                max_slot_by_batch.get(plan.batch_id, 0),
                plan.domain_index + 1,
            )

        if args.bump_domains_per_tenant:
            for batch_id, needed_slots in max_slot_by_batch.items():
                batch = await db.get(SetupBatch, batch_id)
                if batch and (batch.domains_per_tenant or 1) < needed_slots:
                    batch.domains_per_tenant = needed_slots

        await db.commit()
        print({"applied": len(plans)})
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping-csv", required=True, help="CSV containing admin/domain assignments")
    parser.add_argument("--env-file", help="Optional .env path. Defaults to backend/.env")
    parser.add_argument("--default-redirect-url", help="Optional redirect URL for new domain records")
    parser.add_argument("--apply", action="store_true", help="Write changes. Omit for dry-run")
    parser.add_argument(
        "--reassign",
        action="store_true",
        help="Allow existing domains to move from another tenant/batch",
    )
    parser.add_argument(
        "--reset-existing-progress",
        action="store_true",
        help="Clear M365/DKIM/mailbox progress for existing domain records that are linked",
    )
    parser.add_argument(
        "--no-bump-domains-per-tenant",
        dest="bump_domains_per_tenant",
        action="store_false",
        help="Do not increase the batch domains_per_tenant setting to fit assigned slots",
    )
    parser.set_defaults(bump_domains_per_tenant=True)
    return parser


async def async_main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    load_environment(args.env_file)
    assignments = load_assignments(Path(args.mapping_csv), args.default_redirect_url)
    return await plan_and_apply(args, assignments)


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
