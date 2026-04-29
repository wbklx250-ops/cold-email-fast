#!/usr/bin/env python3
"""Repair known stale Alembic version markers before startup migrations.

This is intentionally narrow: it only handles revision ids that existed in
deployed code and were later renamed or removed from the migration graph.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


OLD_COND_ACCESS_REV = "027_add_conditional_access_columns"
COND_ACCESS_REV = "027_cond_access_cols"
DOMAIN_PERSONA_REV = "026_add_domain_persona_names"
SKIP_FLAGS_REV = "025_add_skip_flags"

COND_ACCESS_COLUMNS = {
    "conditional_access_disabled",
    "conditional_access_disabled_at",
    "conditional_access_error",
    "conditional_access_policies_disabled_count",
}
DOMAIN_PERSONA_COLUMNS = {"persona_first_name", "persona_last_name"}


def backend_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def load_database_url() -> str:
    load_dotenv(backend_dir() / ".env")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return database_url


def sync_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        database_url = database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    elif database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)

    parsed = urlparse(database_url)
    query = parse_qs(parsed.query, keep_blank_values=True)

    ssl_values = [value.lower() for value in query.pop("ssl", [])]
    if "sslmode" not in query and any(value in {"true", "1", "require"} for value in ssl_values):
        query["sslmode"] = ["require"]

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query, doseq=True),
            parsed.fragment,
        )
    )


def get_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = :table_name
              AND table_schema = ANY(current_schemas(false))
            """
        ),
        {"table_name": table_name},
    )
    return {row[0] for row in rows}


def get_versions(conn) -> list[str]:
    return list(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())


def set_version(conn, bad_revision: str, target_revision: str, existing_versions: list[str]) -> None:
    if target_revision in existing_versions:
        conn.execute(
            text("DELETE FROM alembic_version WHERE version_num = :bad_revision"),
            {"bad_revision": bad_revision},
        )
        return

    conn.execute(
        text(
            """
            UPDATE alembic_version
            SET version_num = :target_revision
            WHERE version_num = :bad_revision
            """
        ),
        {"target_revision": target_revision, "bad_revision": bad_revision},
    )


def repair_conditional_access_revision(conn) -> int:
    versions = get_versions(conn)
    if OLD_COND_ACCESS_REV not in versions:
        current = ", ".join(versions) if versions else "<none>"
        print(
            f"[MIGRATION-REPAIR] {OLD_COND_ACCESS_REV} is not present; "
            f"current alembic_version rows: {current}"
        )
        return 1

    tenant_columns = get_columns(conn, "tenants")
    domain_columns = get_columns(conn, "domains")

    if COND_ACCESS_COLUMNS.issubset(tenant_columns):
        target_revision = COND_ACCESS_REV
        reason = "conditional access columns already exist"
    elif DOMAIN_PERSONA_COLUMNS.issubset(domain_columns):
        target_revision = DOMAIN_PERSONA_REV
        reason = "conditional access columns are missing, but domain persona columns exist"
    else:
        target_revision = SKIP_FLAGS_REV
        reason = "conditional access and domain persona columns are missing"

    set_version(conn, OLD_COND_ACCESS_REV, target_revision, versions)
    current = ", ".join(get_versions(conn))
    print(
        f"[MIGRATION-REPAIR] {reason}; remapped "
        f"{OLD_COND_ACCESS_REV} -> {target_revision}. Current rows: {current}"
    )
    return 0


def main() -> int:
    requested_revision = sys.argv[1] if len(sys.argv) > 1 else OLD_COND_ACCESS_REV
    if requested_revision != OLD_COND_ACCESS_REV:
        print(f"[MIGRATION-REPAIR] Unsupported revision repair: {requested_revision}")
        return 2

    try:
        engine = create_engine(sync_database_url(load_database_url()), pool_pre_ping=True)
        with engine.begin() as conn:
            return repair_conditional_access_revision(conn)
    except Exception as exc:
        print(f"[MIGRATION-REPAIR] Repair failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
