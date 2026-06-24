"""add per-domain persona name columns

Revision ID: 026_add_domain_persona_names
Revises: 025_add_skip_flags
Create Date: 2026-04-23

Adds persona_first_name and persona_last_name to the domains table so each
domain can carry its own persona (for multi-client batches). Batch-level
persona_first_name / persona_last_name on setup_batches remain the fallback
default when the per-domain values are NULL.

Uses ADD COLUMN IF NOT EXISTS so the stale-revision recovery path can safely
replay this migration against databases where the columns already exist.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '026_add_domain_persona_names'
down_revision: Union[str, None] = '025_add_skip_flags'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS persona_first_name VARCHAR(100);")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS persona_last_name VARCHAR(100);")


def downgrade() -> None:
    op.drop_column("domains", "persona_last_name")
    op.drop_column("domains", "persona_first_name")
