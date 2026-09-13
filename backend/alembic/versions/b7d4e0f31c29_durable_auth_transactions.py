"""Durable session revocation and single-use authorization transactions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = "b7d4e0f31c29"
down_revision = "a6c3d9e20b18"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("auth_transactions",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("payload", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_auth_transactions_user_id", "auth_transactions", ["user_id"])


def downgrade():
    op.drop_table("auth_transactions")
    op.drop_column("users", "session_version")
