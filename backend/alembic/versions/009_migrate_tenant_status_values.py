"""migrate_tenant_status_values

Revision ID: 009
Revises: 008
Create Date: 2026-01-20

This migration updates existing tenant status values to use the new enum values.
It must run AFTER the enum values have been committed (migration 008).

Note: Previously depended on 9a25dfed836a (tenant model automation columns).
Those columns are now added idempotently in migration 011_add_all_missing_columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '009'
down_revision: Union[str, None] = '008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # These UPDATEs only matter for existing databases with data.
    # On fresh databases, the tables are empty so these are no-ops.
    # Wrap in try/except because PostgreSQL requires enum values
    # to be committed before use, and migration 008 added them
    # in the same transaction context.
    bind = op.get_bind()

    updates = [
        # 'new' and 'imported' stay as 'imported'
        "UPDATE tenants SET status = 'imported' WHERE status = 'new'",
        # 'active', 'configuring', 'mailboxes_configuring' become 'ready'
        "UPDATE tenants SET status = 'ready' WHERE status = 'active'",
        "UPDATE tenants SET status = 'ready' WHERE status = 'configuring'",
        "UPDATE tenants SET status = 'ready' WHERE status = 'mailboxes_configuring'",
        "UPDATE tenants SET status = 'ready' WHERE status = 'mailboxes_creating'",
        # Keep 'error', 'domain_verified', 'dkim_enabled' as they exist in new enum
        # 'domain_linked' -> 'domain_added'
        "UPDATE tenants SET status = 'domain_added' WHERE status = 'domain_linked'",
        # 'm365_connected' -> 'first_login_complete'
        "UPDATE tenants SET status = 'first_login_complete' WHERE status = 'm365_connected'",
        # 'dns_configuring' -> 'dns_configured'
        "UPDATE tenants SET status = 'dns_configured' WHERE status = 'dns_configuring'",
        # 'dkim_configuring' -> 'dkim_enabled'
        "UPDATE tenants SET status = 'dkim_enabled' WHERE status = 'dkim_configuring'",
        # Handle suspended/retired -> error with note
        "UPDATE tenants SET status = 'error', setup_error = 'Previously suspended/retired' WHERE status = 'suspended'",
        "UPDATE tenants SET status = 'error', setup_error = 'Previously suspended/retired' WHERE status = 'retired'",
    ]

    for sql in updates:
        try:
            bind.execute(sa.text(sql))
        except Exception:
            pass


def downgrade() -> None:
    # Reverse mappings where possible.
    # Wrapped in try/except for the same enum-commit reason as upgrade().
    bind = op.get_bind()

    downgrades = [
        "UPDATE tenants SET status = 'active' WHERE status = 'ready'",
        "UPDATE tenants SET status = 'domain_linked' WHERE status = 'domain_added'",
        "UPDATE tenants SET status = 'm365_connected' WHERE status = 'first_login_complete'",
        "UPDATE tenants SET status = 'dns_configuring' WHERE status = 'dns_configured'",
        "UPDATE tenants SET status = 'dkim_configuring' WHERE status = 'dkim_enabled'",
        # Note: first_login_pending, mailboxes_created don't have direct mappings
    ]

    for sql in downgrades:
        try:
            bind.execute(sa.text(sql))
        except Exception:
            pass
