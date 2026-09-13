"""Compound indexes for tenant queue, retention, attention and outbox predicates."""
from alembic import op
revision = 'df5c687b9401'
down_revision = 'ce4b576a8390'
branch_labels = depends_on = None
INDEXES = [
    ('ix_jobs_tenant_status_fetched','jobs',['user_id','status','fetched_at']),
    ('ix_proposals_tenant_platform_status_id','proposal_queue',['user_id','platform','status','id']),
    ('ix_tasks_tenant_platform_status_created','stealth_tasks',['user_id','platform','status','created_at']),
    ('ix_audit_tenant_action_created','audit_log',['user_id','action_type','created_at']),
    ('ix_generation_state_attempts_lease','generation_work',['state','attempts','lease_until']),
]

def upgrade():
    for name,table,columns in INDEXES:op.create_index(name,table,columns)

def downgrade():
    for name,table,_ in reversed(INDEXES):op.drop_index(name,table_name=table)
