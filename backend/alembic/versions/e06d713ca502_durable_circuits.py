"""Durable circuits; existing tenants must review automation before resuming.

Old Redis state cannot be proven during a database migration. Preserve safety
by pausing existing tenants until their operator explicitly closes each scope.
"""
from alembic import op
import sqlalchemy as sa
revision = 'e06d713ca502'
down_revision = 'df5c687b9401'
branch_labels = depends_on = None


def upgrade():
    table = op.create_table('automation_circuits',
        sa.Column('key', sa.String(150), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('opened_at', sa.Float(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('manual_stop', sa.Boolean(), nullable=False),
        sa.Column('trial_until', sa.Float(), nullable=False))
    op.create_index('ix_automation_circuits_user_id', 'automation_circuits', ['user_id'])
    bind = op.get_bind()
    for platform in ('upwork','fiverr','freelancer','peopleperhour','guru','linkedin','indeed'):
        source = sa.select(
            sa.literal('circuit:' + platform + ':') + sa.cast(sa.column('id'), sa.String()),
            sa.column('id'), sa.literal(1), sa.literal('open'), sa.cast(sa.null(), sa.Float()),
            sa.literal('Review migrated automation state before resuming'), sa.true(), sa.literal(0.0),
        ).select_from(sa.table('users', sa.column('id')))
        bind.execute(table.insert().from_select(['key','user_id','revision','state','opened_at','reason','manual_stop','trial_until'], source))


def downgrade():
    op.drop_index('ix_automation_circuits_user_id', table_name='automation_circuits')
    op.drop_table('automation_circuits')
