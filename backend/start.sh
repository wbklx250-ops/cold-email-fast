#!/bin/bash
set -o pipefail

echo "=========================================="
echo " Cold Email Platform - Starting Up"
echo "=========================================="
echo ""

run_migrations() {
    echo "[MIGRATIONS] Attempting database migrations..."

    # Try upgrade first
    output=$(alembic upgrade head 2>&1)
    exit_code=$?

    if [ $exit_code -eq 0 ]; then
        echo "[MIGRATIONS] ✓ Migrations applied successfully"
        return 0
    fi

    # If upgrade failed, the DB likely references a deleted revision from the
    # old branched migration chain (012-024, 9a25dfed836a, 41f54eb8b052,
    # 50013c48d54b, 011_multi_domain, etc.). These were consolidated into
    # 011_add_all_missing_columns which uses ADD COLUMN IF NOT EXISTS.
    # Fix: stamp to 010 (last common ancestor) then upgrade again.
    if echo "$output" | grep -qiE "can't locate revision|unknown revision|multiple head"; then
        echo "[MIGRATIONS] Detected stale/broken revision — stamping to 010 and retrying..."
        alembic stamp 010 2>&1 || true
        output=$(alembic upgrade head 2>&1)
        if [ $? -eq 0 ]; then
            echo "[MIGRATIONS] ✓ Migrations applied after stamp fix"
            return 0
        fi
    fi

    if echo "$output" | grep -qiE "connection refused|timeout|could not connect|connection reset"; then
        echo "[MIGRATIONS] Database connection issue - waiting 5s and retrying..."
        sleep 5
        output=$(alembic upgrade head 2>&1)
        if [ $? -eq 0 ]; then
            echo "[MIGRATIONS] ✓ Migrations applied on retry"
            return 0
        fi
    fi
    
    echo "[MIGRATIONS] ✗ Migration failed (server will start anyway):"
    echo "$output" | head -20
    echo ""
    echo "[MIGRATIONS] ⚠ Check logs and run migrations manually if needed"
    return 1
}

run_migrations || true

echo ""
echo "=========================================="
echo " Starting Uvicorn Server"
echo "=========================================="
echo ""

exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
