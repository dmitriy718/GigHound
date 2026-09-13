"""Persist the account selected during proposal review.

Existing approval snapshots retain their account identity in application reads;
pending proposals resolve an explicit account at their next approval.
"""
from alembic import op
import sqlalchemy as sa
revision = 'f17e824db613'
down_revision = 'e06d713ca502'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('proposal_queue') as batch:
        batch.add_column(sa.Column('platform_account_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_proposal_account', 'platform_accounts', ['platform_account_id'], ['id'], ondelete='SET NULL')


def downgrade():
    with op.batch_alter_table('proposal_queue') as batch:
        batch.drop_constraint('fk_proposal_account', type_='foreignkey')
        batch.drop_column('platform_account_id')
