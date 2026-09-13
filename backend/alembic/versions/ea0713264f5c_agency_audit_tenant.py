"""Tenant-bind future agency audit records; ambiguous historical ownership stays null."""
from alembic import op
import sqlalchemy as sa
revision = "ea0713264f5c"
down_revision = "d9f602153e4b"
branch_labels = depends_on = None

def upgrade():
    with op.batch_alter_table("agency_audit_log") as batch:
        batch.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_agency_audit_user", "users", ["user_id"], ["id"], ondelete="CASCADE")
        batch.create_index("ix_agency_audit_log_user_id", ["user_id"])

def downgrade():
    with op.batch_alter_table("agency_audit_log") as batch:
        batch.drop_index("ix_agency_audit_log_user_id")
        batch.drop_constraint("fk_agency_audit_user", type_="foreignkey")
        batch.drop_column("user_id")
