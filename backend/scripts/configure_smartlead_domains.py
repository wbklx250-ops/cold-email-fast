"""Verify and bulk-configure Smartlead accounts for targeted domains."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.getenv("DEBUG") not in (None, "", "true", "false", "True", "False", "1", "0"):
    os.environ["DEBUG"] = "false"


from sqlalchemy import and_, func, or_, select, update  # noqa: E402

from app.db.session import async_session_factory  # noqa: E402
from app.models.batch import SetupBatch  # noqa: E402
from app.models.mailbox import Mailbox  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.services.smartlead import SmartleadAPI  # noqa: E402


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_target(value: str) -> tuple[str, list[str]]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("target must be admin@tenant=domain1,domain2")
    admin_email, raw_domains = value.split("=", 1)
    admin_email = admin_email.strip().lower()
    domains = [
        part.strip().lower()
        for chunk in raw_domains.replace("\t", ",").split(",")
        for part in chunk.split()
        if part.strip()
    ]
    if not admin_email or not domains:
        raise argparse.ArgumentTypeError("target must include an admin email and at least one domain")
    return admin_email, domains


def iter_target_pairs(targets: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for admin_email, domains in targets:
        for domain in domains:
            pair = (admin_email, domain)
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


def domain_for_email(email: str, domains: set[str]) -> str:
    lower_email = email.lower()
    for domain in domains:
        if lower_email.endswith(f"@{domain}"):
            return domain
    return ""


async def load_mailboxes(batch_id: UUID, targets: list[tuple[str, list[str]]]) -> tuple[str, list[dict]]:
    pairs = iter_target_pairs(targets)
    target_predicates = [
        and_(
            func.lower(Tenant.admin_email) == admin_email,
            func.lower(Mailbox.email).like(f"%@{domain}"),
        )
        for admin_email, domain in pairs
    ]
    domains = {domain for _, domain in pairs}

    async with async_session_factory() as session:
        batch = await session.get(SetupBatch, batch_id)
        if not batch:
            raise RuntimeError(f"Batch not found: {batch_id}")

        result = await session.execute(
            select(Mailbox.id, Mailbox.email)
            .join(Tenant, Mailbox.tenant_id == Tenant.id)
            .where(Tenant.batch_id == batch_id, or_(*target_predicates))
            .order_by(Mailbox.email)
        )
        mailboxes = [
            {
                "id": str(row.id),
                "email": row.email,
                "domain": domain_for_email(row.email, domains),
            }
            for row in result.all()
        ]

    return batch.name, mailboxes


async def mark_present(mailbox_id: str) -> None:
    async with async_session_factory() as session:
        await session.execute(
            update(Mailbox)
            .where(Mailbox.id == mailbox_id)
            .values(
                smartlead_uploaded=True,
                smartlead_uploaded_at=utc_now(),
                smartlead_upload_error=None,
            )
        )
        await session.commit()


async def mark_missing(mailbox_id: str) -> None:
    async with async_session_factory() as session:
        await session.execute(
            update(Mailbox)
            .where(Mailbox.id == mailbox_id)
            .values(
                smartlead_uploaded=False,
                smartlead_upload_error="Missing from Smartlead after targeted upload",
            )
        )
        await session.commit()


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--target", action="append", required=True, type=parse_target)
    parser.add_argument("--expected-count", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--skip-sending", action="store_true")
    parser.add_argument("--skip-warmup", action="store_true")
    parser.add_argument("--max-email-per-day", type=int, default=6)
    parser.add_argument("--time-to-wait-in-mins", type=int, default=60)
    parser.add_argument("--custom-tracking-url", default="")
    parser.add_argument("--no-warmup", dest="warmup_enabled", action="store_false")
    parser.set_defaults(warmup_enabled=True)
    parser.add_argument("--total-warmup-per-day", type=int, default=40)
    parser.add_argument("--daily-rampup", type=int, default=5)
    parser.add_argument("--reply-rate-percentage", type=int, default=79)
    parser.add_argument("--delay", type=float, default=0.1)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    api_key = os.getenv("SMARTLEAD_API_KEY")
    if not api_key:
        raise RuntimeError("SMARTLEAD_API_KEY environment variable is required")

    batch_id = UUID(args.batch_id)
    batch_name, mailboxes = await load_mailboxes(batch_id, args.target)
    if args.expected_count is not None and len(mailboxes) != args.expected_count:
        raise RuntimeError(f"Expected {args.expected_count} target mailbox(es), found {len(mailboxes)}")

    api = SmartleadAPI(api_key)
    try:
        accounts = await api.get_all_accounts()
        account_id_by_email = {
            account.get("from_email", "").lower(): account.get("id")
            for account in accounts
            if account.get("from_email") and account.get("id")
        }

        present = [mb for mb in mailboxes if mb["email"].lower() in account_id_by_email]
        missing = [mb for mb in mailboxes if mb["email"].lower() not in account_id_by_email]

        domain_stats: dict[str, dict[str, int]] = {}
        for mailbox in mailboxes:
            domain = mailbox["domain"]
            if domain not in domain_stats:
                domain_stats[domain] = {"total": 0, "present": 0, "missing": 0}
            domain_stats[domain]["total"] += 1
            if mailbox in present:
                domain_stats[domain]["present"] += 1
            else:
                domain_stats[domain]["missing"] += 1

        print(
            {
                "batch_id": str(batch_id),
                "batch_name": batch_name,
                "target_total": len(mailboxes),
                "smartlead_accounts_seen": len(accounts),
                "present": len(present),
                "missing": len(missing),
                "domain_stats": domain_stats,
            },
            flush=True,
        )

        if missing and not args.allow_missing:
            print({"missing_preview": [mb["email"] for mb in missing[:25]]}, flush=True)
            if not args.dry_run:
                for mailbox in missing:
                    await mark_missing(mailbox["id"])
            return 1

        if args.dry_run:
            return 0 if not missing else 1

        configured_sending = 0
        configured_warmup = 0
        settings_failed: list[str] = []

        for index, mailbox in enumerate(present, 1):
            account_id = account_id_by_email[mailbox["email"].lower()]
            await mark_present(mailbox["id"])

            sending_ok = True
            warmup_ok = True
            if not args.skip_sending:
                sending_ok = await api.update_sending_settings(
                    account_id,
                    max_per_day=args.max_email_per_day,
                    wait_mins=args.time_to_wait_in_mins,
                    tracking_url=args.custom_tracking_url,
                )
                configured_sending += int(sending_ok)
            if not args.skip_warmup:
                warmup_ok = await api.update_warmup_settings(
                    account_id,
                    enabled=args.warmup_enabled,
                    per_day=args.total_warmup_per_day,
                    rampup=args.daily_rampup,
                    reply_rate=args.reply_rate_percentage,
                )
                configured_warmup += int(warmup_ok)
            if not (sending_ok and warmup_ok):
                settings_failed.append(mailbox["email"])

            if index % 25 == 0 or index == len(present):
                print(
                    {
                        "configured": index,
                        "present": len(present),
                        "settings_failed": len(settings_failed),
                    },
                    flush=True,
                )
            if args.delay:
                await asyncio.sleep(args.delay)

        print(
            {
                "summary": {
                    "total": len(mailboxes),
                    "present": len(present),
                    "missing": len(missing),
                    "sending_configured": configured_sending,
                    "warmup_configured": configured_warmup,
                    "settings_failed": len(settings_failed),
                    "settings_failed_preview": settings_failed[:20],
                }
            },
            flush=True,
        )
        return 0 if not missing and not settings_failed else 1
    finally:
        await api.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
