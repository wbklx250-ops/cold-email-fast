"""Track successful domain reads and invalidate unproven empty audits."""

import sqlalchemy as sa
from alembic import op

revision = "030_domain_check_success"
down_revision = "029_tenant_audit_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_audits", sa.Column(
        "domain_check_success", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    # Legacy empty results may have come from an MFA screen. Require a fresh check.
    op.execute("""
        UPDATE tenant_audits
        SET is_used = NULL,
            login_error = COALESCE(NULLIF(login_error, ''),
                'Previous domain check was inconclusive; run a fresh tenant check')
        WHERE custom_domain_count = 0 OR NOT login_success
            OR COALESCE(login_error, '') <> ''
    """)
    op.execute("""
        UPDATE tenant_audits SET domain_check_success = true
        WHERE login_success AND custom_domain_count > 0
            AND COALESCE(login_error, '') = ''
    """)


def downgrade() -> None:
    op.drop_column("tenant_audits", "domain_check_success")
