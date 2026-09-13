"""Fence delayed enrollment from a deleted/recreated numeric account ID.

Outstanding OAuth transactions without an epoch require a new OAuth start.
"""
from uuid import uuid4
from alembic import op
import sqlalchemy as sa
revision = 'a28f935ec724'
down_revision = 'f17e824db613'
branch_labels = depends_on = None


def upgrade():
    op.add_column('platform_accounts', sa.Column('identity_epoch', sa.String(36), nullable=True))
    bind = op.get_bind()
    accounts = sa.table('platform_accounts', sa.column('id', sa.Integer()), sa.column('identity_epoch', sa.String(36)))
    # Stream by key so the migration does not load all tenants in memory.
    last = 0
    while True:
        ids = bind.execute(sa.select(accounts.c.id).where(accounts.c.id > last).order_by(accounts.c.id).limit(500)).scalars().all()
        if not ids:
            break
        for account_id in ids:
            bind.execute(accounts.update().where(accounts.c.id == account_id).values(identity_epoch=str(uuid4())))
        last = ids[-1]
    with op.batch_alter_table('platform_accounts') as batch:
        batch.alter_column('identity_epoch', existing_type=sa.String(36), nullable=False)


def downgrade():
    with op.batch_alter_table('platform_accounts') as batch:
        batch.drop_column('identity_epoch')
