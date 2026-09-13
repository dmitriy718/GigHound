"""Align bid_advice with the model's PostgreSQL JSONB type."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "a6c3d9e20b18"
down_revision = "f5b2c8d91a07"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("proposal_queue", "bid_advice", existing_type=sa.JSON(),
                        type_=JSONB(), postgresql_using="bid_advice::jsonb",
                        existing_nullable=True)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("proposal_queue", "bid_advice", existing_type=JSONB(),
                        type_=sa.JSON(), postgresql_using="bid_advice::json",
                        existing_nullable=True)
