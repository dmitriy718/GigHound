"""Durable generation intent and bounded leases."""
from alembic import op
import sqlalchemy as sa
revision = "c8e5f1042d3a"
down_revision = "b7d4e0f31c29"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("generation_work",
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(20), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(64)), sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(500), nullable=False))
    op.create_index("ix_generation_work_user_id", "generation_work", ["user_id"])
    op.create_index("ix_generation_work_state", "generation_work", ["state"])


def downgrade():
    op.drop_table("generation_work")
