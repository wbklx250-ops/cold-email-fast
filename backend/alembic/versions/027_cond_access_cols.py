"""add conditional access tracking columns

Revision ID: 027_cond_access_cols
Revises: 026_add_domain_persona_names
Create Date: 2026-04-29

Adds conditional_access_* columns to the tenants table so we can track and
report when the Microsoft-managed MFA Conditional Access policies have been
disabled (the alternative path to "disable Security Defaults" for tenants
where Microsoft hides the SD link entirely).

Uses ADD COLUMN IF NOT EXISTS so the stale-revision recovery path can safely
replay this migration against databases where the columns already exist.

NOTE: Revision id is intentionally kept ≤ 32 chars because Alembic's default
``alembic_version.version_num`` column is ``VARCHAR(32)``. A previous version
of this migration used the id "027_add_conditional_access_columns" (38 chars)
which caused every deploy to fail with
``StringDataRightTruncationError: value too long for type character varying(32)``
when Alembic tried to record the new head. Do NOT rename this id back.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "027_cond_access_cols"
down_revision: Union[str, None] = "026_add_domain_persona_names"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tenants
        ADD COLUMN IF NOT EXISTS conditional_access_disabled BOOLEAN NOT NULL DEFAULT FALSE;
        """
    )
    op.execute(
        """
        ALTER TABLE tenants
        ADD COLUMN IF NOT EXISTS conditional_access_disabled_at TIMESTAMP WITH TIME ZONE;
        """
    )
    op.execute(
        """
        ALTER TABLE tenants
        ADD COLUMN IF NOT EXISTS conditional_access_error TEXT;
        """
    )
    op.execute(
        """
        ALTER TABLE tenants
        ADD COLUMN IF NOT EXISTS conditional_access_policies_disabled_count INTEGER NOT NULL DEFAULT 0;
        """
    )


def downgrade() -> None:
    op.drop_column("tenants", "conditional_access_policies_disabled_count")
    op.drop_column("tenants", "conditional_access_error")
    op.drop_column("tenants", "conditional_access_disabled_at")
    op.drop_column("tenants", "conditional_access_disabled")
