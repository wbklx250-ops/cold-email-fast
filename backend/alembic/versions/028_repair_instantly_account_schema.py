"""repair saved sequencer account schema drift

Revision ID: 028_repair_account_schema
Revises: 027_cond_access_cols
Create Date: 2026-05-21

The consolidated 011 migration created an older instantly_accounts shape when
the table did not exist, while the runtime model expects label/password/default
metadata. Production can be stamped at head while still missing those columns,
so this migration is intentionally idempotent.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "028_repair_account_schema"
down_revision: Union[str, None] = "027_cond_access_cols"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE instantly_accounts ADD COLUMN IF NOT EXISTS label VARCHAR(100);")
    op.execute(
        """
        UPDATE instantly_accounts
        SET label = COALESCE(NULLIF(label, ''), email, 'Instantly Account')
        WHERE label IS NULL OR label = '';
        """
    )
    op.execute("ALTER TABLE instantly_accounts ALTER COLUMN label SET NOT NULL;")
    op.execute("ALTER TABLE instantly_accounts ADD COLUMN IF NOT EXISTS password VARCHAR(255);")
    op.execute("ALTER TABLE instantly_accounts ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE instantly_accounts ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP WITH TIME ZONE;")
    op.execute("ALTER TABLE instantly_accounts ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now();")
    op.execute("ALTER TABLE instantly_accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now();")

    # Keep the generic upload dashboard consistent for historical Smartlead rows.
    op.execute(
        """
        UPDATE mailboxes
        SET uploaded_to_sequencer = TRUE,
            sequencer_name = COALESCE(sequencer_name, 'smartlead'),
            uploaded_at = COALESCE(uploaded_at, smartlead_uploaded_at)
        WHERE smartlead_uploaded = TRUE
          AND uploaded_to_sequencer = FALSE;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE instantly_accounts DROP COLUMN IF EXISTS last_used_at;")
    op.execute("ALTER TABLE instantly_accounts DROP COLUMN IF EXISTS is_default;")
    op.execute("ALTER TABLE instantly_accounts DROP COLUMN IF EXISTS password;")
    op.execute("ALTER TABLE instantly_accounts DROP COLUMN IF EXISTS label;")
