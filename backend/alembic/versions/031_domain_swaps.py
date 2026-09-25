"""Persist bulk domain swaps and reserve tenants/domains during execution."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "031_domain_swaps"
down_revision = "030_domain_check_success"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "domain_swap_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("mappings", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "domain_swap_reservations",
        sa.Column("resource", sa.String(300), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("domain_swap_jobs.id", ondelete="CASCADE"), nullable=False),
    )
    op.create_index("ix_domain_swap_reservations_job_id", "domain_swap_reservations", ["job_id"])


def downgrade():
    op.drop_table("domain_swap_reservations")
    op.drop_table("domain_swap_jobs")
