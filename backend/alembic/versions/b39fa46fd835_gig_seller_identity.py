"""Retain explicit seller identity on tracked listings; legacy rows stay unassigned."""
from alembic import op
import sqlalchemy as sa
revision = 'b39fa46fd835'
down_revision = 'a28f935ec724'
branch_labels = depends_on = None


def upgrade():
    op.add_column('gigs', sa.Column('account_id', sa.Integer(), nullable=True))
    op.add_column('gigs', sa.Column('account_epoch', sa.String(36), nullable=True))
    op.add_column('gigs', sa.Column('account_binding_version', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('gigs') as batch:
        batch.drop_column('account_binding_version')
        batch.drop_column('account_epoch')
        batch.drop_column('account_id')
