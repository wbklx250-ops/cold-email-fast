"""Allow app-only domain assignments and opt-in saved checker credentials."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "031_inventory_credentials"
down_revision = "030_domain_check_success"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_audits", sa.Column("assigned_custom_domain", sa.String(255), nullable=True))
    op.create_unique_constraint(
        "uq_tenant_audits_assigned_custom_domain", "tenant_audits", ["assigned_custom_domain"],
    )
    op.alter_column("tenant_audits", "last_checked_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.create_table(
        "tenant_audit_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant_audits.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("password_ciphertext", sa.Text(), nullable=False),
        sa.Column("totp_ciphertext", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tenant_audit_credentials")
    op.execute("UPDATE tenant_audits SET last_checked_at = now() WHERE last_checked_at IS NULL")
    op.alter_column("tenant_audits", "last_checked_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.drop_constraint("uq_tenant_audits_assigned_custom_domain", "tenant_audits", type_="unique")
    op.drop_column("tenant_audits", "assigned_custom_domain")
