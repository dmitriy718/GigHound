"""Missing metrics are unknown, not measured zeros; preserve historical values."""

from alembic import op
import sqlalchemy as sa

revision = "ce4b576a8390"
down_revision = "bd3a4659728f"
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table("gig_metrics") as b:
        for name in ("impressions", "clicks", "orders", "revenue"):
            b.alter_column(
                name,
                nullable=True,
                existing_type=sa.Float() if name == "revenue" else sa.Integer(),
            )


def downgrade():
    connection = op.get_bind()
    missing = connection.execute(
        sa.text(
            "SELECT count(*) FROM gig_metrics WHERE impressions IS NULL OR clicks IS NULL OR orders IS NULL OR revenue IS NULL"
        )
    ).scalar()
    if missing:
        raise RuntimeError(
            "Cannot downgrade unknown metric values to fabricated zeros; export and explicitly reconcile these rows first"
        )
    with op.batch_alter_table("gig_metrics") as b:
        for name in ("impressions", "clicks", "orders", "revenue"):
            b.alter_column(
                name,
                nullable=False,
                existing_type=sa.Float() if name == "revenue" else sa.Integer(),
            )
