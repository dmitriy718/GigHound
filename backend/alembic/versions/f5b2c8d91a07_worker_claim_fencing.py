"""Add per-claim worker fencing tokens. Existing claims must expire before reuse."""
from alembic import op
import sqlalchemy as sa

revision = "f5b2c8d91a07"
down_revision = "e4a91b6c2d08"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stealth_tasks", sa.Column("claim_token", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("stealth_tasks", "claim_token")
