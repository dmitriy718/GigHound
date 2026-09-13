"""Observed confirmed submission/outcome times; historical unknowns stay null."""
from alembic import op
import sqlalchemy as sa
revision = "d9f602153e4b"
down_revision = "c8e5f1042d3a"
branch_labels = depends_on = None

def upgrade():
    op.add_column("proposal_queue", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("proposal_queue", sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True))

def downgrade():
    op.drop_column("proposal_queue", "outcome_at")
    op.drop_column("proposal_queue", "submitted_at")
