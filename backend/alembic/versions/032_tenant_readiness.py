"""Persist resumable tenant mail readiness checks."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = '032_tenant_readiness'
down_revision = '031_inventory_credentials'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('readiness_checks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column('admin_email', sa.String(255), nullable=False),
        sa.Column('label', sa.String(120), nullable=False),
        sa.Column('status', sa.String(40), nullable=False),
        sa.Column('state', sa.JSON(), nullable=False),
        sa.Column('credentials_ciphertext', sa.Text(), nullable=False),
        sa.Column('artifacts_ciphertext', sa.Text(), nullable=False),
        sa.Column('lease_owner', sa.String(36), nullable=True),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_readiness_checks_status', 'readiness_checks', ['status'])
    op.create_index('ix_readiness_checks_tenant_id', 'readiness_checks', ['tenant_id'], unique=True)


def downgrade():
    op.drop_table('readiness_checks')
