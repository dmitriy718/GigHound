"""Tenant-scoped evidence, conversations, feedback and attributable business records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "fb182437506d"
down_revision = "ea0713264f5c"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "workbench_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("reference", sa.String(250)),
        sa.Column("data", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "reference", name="uq_workbench_reference"),
    )
    op.create_index("ix_workbench_records_user_id", "workbench_records", ["user_id"])
    op.create_index("ix_workbench_records_kind", "workbench_records", ["kind"])


def downgrade():
    op.drop_table("workbench_records")
