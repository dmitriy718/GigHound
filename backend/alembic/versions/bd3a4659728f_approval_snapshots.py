"""Versioned material approval snapshots; legacy approvals require renewed review."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "bd3a4659728f"
down_revision = "ac293548617e"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "proposal_queue",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "proposal_queue",
        sa.Column(
            "approved_snapshot",
            sa.JSON().with_variant(JSONB, "postgresql"),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("proposal_queue", "approved_snapshot")
    op.drop_column("proposal_queue", "revision")
