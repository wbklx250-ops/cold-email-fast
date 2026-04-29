#!/bin/bash
set -o pipefail

echo "=========================================="
echo " Cold Email Platform - Starting Up"
echo "=========================================="
echo ""

# Patterns that indicate a transient connectivity problem (worth retrying).
# Anything NOT matching this is treated as a hard schema/DDL error and we
# fail fast — retrying schema bugs just wastes deploy time and (worse) makes
# the failure look intermittent.
CONN_ERROR_REGEX="connection refused|timeout|could not connect|connection reset|server closed the connection|Network is unreachable|Connection.*timed out|Temporary failure in name resolution"

run_migrations_once() {
    alembic upgrade head 2>&1
}

repair_known_stale_revision() {
    local failed_output="$1"

    if [[ "$failed_output" == *"027_add_conditional_access_columns"* ]]; then
        echo "[MIGRATIONS] Detected renamed conditional-access revision; repairing alembic_version..."
        python scripts/repair_alembic_version.py 027_add_conditional_access_columns
        return $?
    fi

    return 1
}

run_migrations() {
    echo "[MIGRATIONS] Attempting database migrations..."

    output=$(run_migrations_once)
    exit_code=$?
    if [ $exit_code -eq 0 ]; then
        echo "[MIGRATIONS] ✓ Migrations applied successfully"
        return 0
    fi

    if repair_known_stale_revision "$output"; then
        output=$(run_migrations_once)
        if [ $? -eq 0 ]; then
            echo "[MIGRATIONS] ✓ Migrations applied after revision repair"
            return 0
        fi
    fi

    # Stale-revision auto-recovery: the DB references a deleted revision from
    # the old branched migration chain (012-024, 9a25dfed836a, 41f54eb8b052,
    # 50013c48d54b, 011_multi_domain, etc.). These were consolidated into
    # 011_add_all_missing_columns which uses ADD COLUMN IF NOT EXISTS.
    # Fix: purge the unreadable version marker, stamp to 010 (last common
    # ancestor), then upgrade again.
    if echo "$output" | grep -qiE "can't locate revision|unknown revision|multiple head"; then
        echo "[MIGRATIONS] Detected stale/broken revision — purging version marker to 010 and retrying..."
        alembic stamp --purge 010 2>&1 || true
        output=$(run_migrations_once)
        if [ $? -eq 0 ]; then
            echo "[MIGRATIONS] ✓ Migrations applied after stamp fix"
            return 0
        fi
    fi

    # Connection-error retries with exponential backoff.
    # Stop early if the error stops looking like a connection problem.
    if echo "$output" | grep -qiE "$CONN_ERROR_REGEX"; then
        for d in 5 10 20 40 60; do
            echo "[MIGRATIONS] Database connection issue — retrying in ${d}s..."
            sleep "$d"
            output=$(run_migrations_once)
            if [ $? -eq 0 ]; then
                echo "[MIGRATIONS] ✓ Migrations applied after connection retry"
                return 0
            fi
            if ! echo "$output" | grep -qiE "$CONN_ERROR_REGEX"; then
                echo "[MIGRATIONS] Non-connection error after retry — failing fast"
                break
            fi
        done
    fi

    # Hard failure — print FULL error so deploy logs are actionable.
    # No more `head -20` truncation that hides the actual SQL error.
    echo ""
    echo "############################################################"
    echo "# [MIGRATIONS] ✗ MIGRATION FAILED                          #"
    echo "############################################################"
    echo "$output"
    echo "############################################################"
    return 1
}

if ! run_migrations; then
    if [ "${ALLOW_MIGRATION_FAILURE:-false}" = "true" ]; then
        echo ""
        echo "############################################################"
        echo "# ⚠  ALLOW_MIGRATION_FAILURE=true — STARTING UVICORN ANYWAY"
        echo "# ⚠  The database schema is BROKEN. API will likely 500."
        echo "# ⚠  This flag should NEVER be set as a default in Railway."
        echo "############################################################"
        echo ""
    else
        echo ""
        echo "[MIGRATIONS] Refusing to start uvicorn with a broken schema."
        echo "[MIGRATIONS] Set ALLOW_MIGRATION_FAILURE=true to override (NOT recommended)."
        exit 1
    fi
fi

echo ""
echo "=========================================="
echo " Starting Uvicorn Server"
echo "=========================================="
echo ""

exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
