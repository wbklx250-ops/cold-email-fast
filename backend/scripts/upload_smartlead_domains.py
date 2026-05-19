"""Targeted Smartlead upload for specific tenant/domain mailboxes.

This is for recovery runs where a full batch-level Smartlead upload would be
too broad. It reuses the production Smartlead OAuth uploader, but limits the
work to an explicit allowlist of tenant admin/domain pairs.
"""

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
from app.services.smartlead import (  # noqa: E402
    SmartleadAPI,
    SmartleadOAuthUploader,
    _is_resource_failure,
    process_smartlead_mailbox_sync,
)


logger = logging.getLogger("upload_smartlead_domains")


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


async def load_target_mailboxes(
    batch_id: UUID,
    targets: list[tuple[str, list[str]]],
    include_db_uploaded: bool,
) -> tuple[str, list[dict], dict[str, dict[str, int]]]:
    pairs = iter_target_pairs(targets)
    domains = {domain for _, domain in pairs}
    target_predicates = [
        and_(
            func.lower(Tenant.admin_email) == admin_email,
            func.lower(Mailbox.email).like(f"%@{domain}"),
        )
        for admin_email, domain in pairs
    ]

    async with async_session_factory() as session:
        batch = await session.get(SetupBatch, batch_id)
        if not batch:
            raise RuntimeError(f"Batch not found: {batch_id}")

        query = (
            select(
                Mailbox.id,
                Mailbox.email,
                Mailbox.initial_password,
                Mailbox.password,
                Mailbox.created_in_exchange,
                Mailbox.delegated,
                Mailbox.password_set,
                Mailbox.account_enabled,
                Mailbox.smartlead_uploaded,
                Mailbox.smartlead_upload_error,
                Tenant.admin_email,
            )
            .join(Tenant, Mailbox.tenant_id == Tenant.id)
            .where(Tenant.batch_id == batch_id, or_(*target_predicates))
            .order_by(Mailbox.email)
        )
        if not include_db_uploaded:
            query = query.where(Mailbox.smartlead_uploaded == False)  # noqa: E712

        result = await session.execute(query)
        rows = result.all()

    stats = {
        domain: {
            "total": 0,
            "db_uploaded": 0,
            "db_failed": 0,
            "with_password": 0,
            "upload_ready": 0,
            "not_ready": 0,
        }
        for domain in sorted(domains)
    }
    mailboxes: list[dict] = []
    missing_passwords: list[str] = []
    not_ready: list[str] = []

    for row in rows:
        domain = domain_for_email(row.email, domains)
        password = row.initial_password or row.password
        if not domain:
            continue
        stats[domain]["total"] += 1
        if row.smartlead_uploaded:
            stats[domain]["db_uploaded"] += 1
        if row.smartlead_upload_error:
            stats[domain]["db_failed"] += 1
        if password:
            stats[domain]["with_password"] += 1
        else:
            missing_passwords.append(row.email)

        ready = (
            row.created_in_exchange
            and row.delegated
            and row.password_set
            and row.account_enabled
            and bool(password)
        )
        if ready:
            stats[domain]["upload_ready"] += 1
        else:
            stats[domain]["not_ready"] += 1
            not_ready.append(
                f"{row.email} "
                f"(created={row.created_in_exchange}, delegated={row.delegated}, "
                f"password_set={row.password_set}, enabled={row.account_enabled}, "
                f"password_present={bool(password)})"
            )
            continue

        mailboxes.append(
            {
                "id": str(row.id),
                "email": row.email,
                "password": password,
                "domain": domain,
                "admin_email": row.admin_email,
                "db_uploaded": bool(row.smartlead_uploaded),
            }
        )

    if missing_passwords:
        preview = ", ".join(missing_passwords[:10])
        raise RuntimeError(f"{len(missing_passwords)} target mailbox(es) are missing passwords: {preview}")
    if not_ready:
        preview = "; ".join(not_ready[:10])
        print({"skipped_not_upload_ready": len(not_ready), "preview": preview}, flush=True)

    return batch.name, mailboxes, stats


async def mark_existing(mailbox_id: str) -> None:
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


async def mark_result(mailbox_id: str, success: bool, error: str | None) -> None:
    values = {
        "smartlead_uploaded": success,
        "smartlead_upload_error": None if success else error,
    }
    if success:
        values["smartlead_uploaded_at"] = utc_now()

    async with async_session_factory() as session:
        await session.execute(update(Mailbox).where(Mailbox.id == mailbox_id).values(**values))
        await session.commit()


async def configure_account(
    api: SmartleadAPI,
    email: str,
    sending: dict,
    warmup: dict,
) -> tuple[bool, bool]:
    account_id = await api.find_account_id(email)
    if not account_id:
        return False, False

    sending_ok = await api.update_sending_settings(account_id, **sending)
    warmup_ok = await api.update_warmup_settings(account_id, **warmup)
    return sending_ok, warmup_ok


async def upload_targets(args: argparse.Namespace) -> dict:
    api_key = os.getenv("SMARTLEAD_API_KEY")
    oauth_url = os.getenv("SMARTLEAD_OAUTH_URL")
    if not api_key:
        raise RuntimeError("SMARTLEAD_API_KEY environment variable is required")
    if not oauth_url:
        raise RuntimeError("SMARTLEAD_OAUTH_URL environment variable is required")

    batch_id = UUID(args.batch_id)
    batch_name, mailboxes, stats = await load_target_mailboxes(
        batch_id=batch_id,
        targets=args.target,
        include_db_uploaded=args.include_db_uploaded,
    )

    print({"batch_id": str(batch_id), "batch_name": batch_name, "domain_stats": stats}, flush=True)
    total = len(mailboxes)
    if args.expected_count is not None and total != args.expected_count:
        raise RuntimeError(f"Expected {args.expected_count} target mailbox(es), found {total}")

    if args.limit:
        mailboxes = mailboxes[: args.limit]
        print({"limited_to": len(mailboxes)}, flush=True)

    api = SmartleadAPI(api_key)
    existing_emails: set[str] = set()
    try:
        existing_emails = await api.get_existing_emails()
        existing_targets = sum(1 for mb in mailboxes if mb["email"].lower() in existing_emails)
        print(
            {
                "smartlead_accounts_seen": len(existing_emails),
                "target_accounts_already_in_smartlead": existing_targets,
            },
            flush=True,
        )

        if args.only_missing_smartlead:
            mailboxes = [mb for mb in mailboxes if mb["email"].lower() not in existing_emails]
            print({"filtered_to_missing_smartlead": len(mailboxes)}, flush=True)

        if args.dry_run:
            return {
                "total": len(mailboxes),
                "already_in_smartlead": existing_targets,
                "to_upload": len(mailboxes) if args.only_missing_smartlead else len(mailboxes) - existing_targets,
                "uploaded": 0,
                "failed": 0,
                "skipped": 0,
            }

        sending = {
            "max_per_day": args.max_email_per_day,
            "wait_mins": args.time_to_wait_in_mins,
            "tracking_url": args.custom_tracking_url,
        }
        warmup = {
            "enabled": args.warmup_enabled,
            "per_day": args.total_warmup_per_day,
            "rampup": args.daily_rampup,
            "reply_rate": args.reply_rate_percentage,
        }

        to_upload: list[dict] = []
        skipped = 0
        settings_configured = 0
        warmup_configured = 0

        for mailbox in mailboxes:
            if mailbox["email"].lower() in existing_emails:
                skipped += 1
                await mark_existing(mailbox["id"])
                if args.configure_settings and args.configure_existing:
                    sending_ok, warmup_ok = await configure_account(api, mailbox["email"], sending, warmup)
                    settings_configured += int(sending_ok)
                    warmup_configured += int(warmup_ok)
                print({"status": "skipped_existing", "email": mailbox["email"]}, flush=True)
            else:
                to_upload.append(mailbox)

        if not to_upload:
            return {
                "total": len(mailboxes),
                "uploaded": 0,
                "failed": 0,
                "skipped": skipped,
                "settings_configured": settings_configured,
                "warmup_configured": warmup_configured,
                "errors": [],
            }

        uploaded = 0
        failed = 0
        errors: list[str] = []
        configured_max_workers = 1
        try:
            configured_max_workers = int(os.getenv("SMARTLEAD_MAX_WORKERS", "1"))
        except ValueError:
            configured_max_workers = 1
        worker_count = max(1, min(args.workers, configured_max_workers, len(to_upload)))
        print(
            {
                "starting_upload": len(to_upload),
                "workers": worker_count,
                "requested_workers": args.workers,
                "headless": args.headless,
                "configure_settings": args.configure_settings,
            },
            flush=True,
        )

        preflight = SmartleadOAuthUploader(headless=args.headless, worker_id="preflight")
        preflight_ok, preflight_error = preflight.chrome_preflight()
        if not preflight_ok:
            raise RuntimeError(f"Smartlead Chrome preflight failed: {preflight_error}")

        uploader = SmartleadOAuthUploader(headless=args.headless, worker_id=0)
        consecutive_resource_failures = 0
        resource_failure_limit = 2

        for mailbox in to_upload:
            result = await asyncio.to_thread(
                process_smartlead_mailbox_sync,
                uploader,
                mailbox,
                oauth_url,
                args.max_retries,
            )

            if result["success"]:
                consecutive_resource_failures = 0
                uploaded += 1
                await mark_result(mailbox["id"], True, None)
                if args.configure_settings:
                    await asyncio.sleep(args.post_upload_settings_delay)
                    sending_ok, warmup_ok = await configure_account(
                        api,
                        mailbox["email"],
                        sending,
                        warmup,
                    )
                    settings_configured += int(sending_ok)
                    warmup_configured += int(warmup_ok)
                status = "uploaded"
            else:
                error = result.get("error") or "OAuth upload failed"
                if _is_resource_failure(error):
                    consecutive_resource_failures += 1
                    errors.append(f"{mailbox['email']}: {error}")
                    if consecutive_resource_failures >= resource_failure_limit:
                        raise RuntimeError(
                            "Stopping targeted Smartlead upload after "
                            f"{consecutive_resource_failures} browser resource failure(s): {error}"
                        )
                    status = "resource_retry_later"
                else:
                    consecutive_resource_failures = 0
                    failed += 1
                    errors.append(f"{mailbox['email']}: {error}")
                    await mark_result(mailbox["id"], False, error)
                    status = "failed"

            print(
                {
                    "status": status,
                    "email": mailbox["email"],
                    "processed": uploaded + failed,
                    "to_upload": len(to_upload),
                    "uploaded": uploaded,
                    "failed": failed,
                    "skipped": skipped,
                },
                flush=True,
            )

        return {
            "total": len(mailboxes),
            "uploaded": uploaded,
            "failed": failed,
            "skipped": skipped,
            "settings_configured": settings_configured,
            "warmup_configured": warmup_configured,
            "errors": errors[:20],
        }
    finally:
        await api.close()


async def async_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--target", action="append", required=True, type=parse_target)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--expected-count", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-db-uploaded", action="store_true")
    parser.add_argument("--only-missing-smartlead", action="store_true")
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.set_defaults(headless=True)

    parser.add_argument("--no-configure-settings", dest="configure_settings", action="store_false")
    parser.set_defaults(configure_settings=True)
    parser.add_argument("--no-configure-existing", dest="configure_existing", action="store_false")
    parser.set_defaults(configure_existing=True)
    parser.add_argument("--max-email-per-day", type=int, default=6)
    parser.add_argument("--time-to-wait-in-mins", type=int, default=60)
    parser.add_argument("--custom-tracking-url", default="")
    parser.add_argument("--no-warmup", dest="warmup_enabled", action="store_false")
    parser.set_defaults(warmup_enabled=True)
    parser.add_argument("--total-warmup-per-day", type=int, default=40)
    parser.add_argument("--daily-rampup", type=int, default=5)
    parser.add_argument("--reply-rate-percentage", type=int, default=79)
    parser.add_argument("--post-upload-settings-delay", type=float, default=3.0)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if args.workers < 1 or args.workers > 5:
        raise RuntimeError("--workers must be between 1 and 5")

    summary = await upload_targets(args)
    print({"summary": summary}, flush=True)
    return 1 if summary.get("failed") else 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
