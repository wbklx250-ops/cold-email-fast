"""add persistent tenant audit inventory

Revision ID: 029_tenant_audit_inventory
Revises: 028_repair_account_schema
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "029_tenant_audit_inventory"
down_revision: Union[str, None] = "028_repair_account_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_audits",
        sa.Column("admin_email", sa.String(length=255), nullable=False),
        sa.Column("tenant_name", sa.String(length=255), nullable=False),
        sa.Column("disposition", sa.String(length=32), server_default="unreviewed", nullable=False),
        sa.Column("login_success", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("login_error", sa.Text(), nullable=True),
        sa.Column("is_used", sa.Boolean(), nullable=True),
        sa.Column("verified_domains", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("unverified_domains", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("custom_domain_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_job_id", sa.String(length=36), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("admin_email"),
    )
    op.create_index("ix_tenant_audits_admin_email", "tenant_audits", ["admin_email"])
    op.create_index("ix_tenant_audits_disposition", "tenant_audits", ["disposition"])
    op.create_index("ix_tenant_audits_is_used", "tenant_audits", ["is_used"])


def downgrade() -> None:
    op.drop_index("ix_tenant_audits_is_used", table_name="tenant_audits")
    op.drop_index("ix_tenant_audits_disposition", table_name="tenant_audits")
    op.drop_index("ix_tenant_audits_admin_email", table_name="tenant_audits")
    op.drop_table("tenant_audits")
